"""amoCRM API polling helpers for admin alerts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping, Sequence

import aiohttp

@dataclass(slots=True, frozen=True)
class AmoCRMLead:
    lead_id: int
    name: str | None
    pipeline_id: int | None
    status_id: int | None
    created_at: int | None
    contact_ids: list[int]
    payload: dict[str, Any]

class AmoCRMAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"amoCRM API error {status}: {message}")
        self.status = status
        self.message = message


class AmoCRMAPIAuthError(AmoCRMAPIError):
    pass


class AmoCRMAPIRateLimitError(AmoCRMAPIError):
    pass


class AmoCRMAPIClient:
    def __init__(
        self,
        api_base: str,
        token: str,
        *,
        session: aiohttp.ClientSession | Any | None = None,
        timeout_sec: float = 15.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token.strip()
        self.session = session
        self.timeout_sec = timeout_sec
        self._owns_session = session is None

    async def __aenter__(self) -> "AmoCRMAPIClient":
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._owns_session and self.session is not None:
            await self.session.close()

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self.session is None:
            self.session = aiohttp.ClientSession()
            self._owns_session = True
        url = f"{self.api_base}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        # Список пар — для повторяющихся ключей вроде filter[id][]: в словарь
        # они не укладываются, второй такой ключ затёр бы первый.
        query: Any = list(params) if isinstance(params, (list, tuple)) else dict(params or {})
        async with self.session.get(url, headers=headers, params=query, timeout=self.timeout_sec) as resp:
            try:
                payload = await resp.json()
            except Exception:
                payload = {"text": await resp.text()}
            if resp.status == 401:
                raise AmoCRMAPIAuthError(resp.status, str(payload))
            if resp.status == 429:
                raise AmoCRMAPIRateLimitError(resp.status, str(payload))
            if resp.status >= 400:
                raise AmoCRMAPIError(resp.status, str(payload))
            if not isinstance(payload, dict):
                raise AmoCRMAPIError(resp.status, "unexpected non-object response")
            return payload

    async def patch(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Единственная запись в CRM: перевод подтверждённой сделки на этап.

        Права на запись у токена могут быть не выданы — тогда amoCRM отвечает
        401/403, и вызывающий обязан сказать об этом владельцу вслух. Молчаливо
        проглоченный отказ означал бы, что клиент подтвердил заказ, а в CRM
        этого никто не увидел.
        """
        if self.session is None:
            self.session = aiohttp.ClientSession()
            self._owns_session = True
        url = f"{self.api_base}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        async with self.session.patch(url, headers=headers, json=dict(payload),
                                      timeout=self.timeout_sec) as resp:
            try:
                body = await resp.json()
            except Exception:
                body = {"text": await resp.text()}
            if resp.status == 401:
                raise AmoCRMAPIAuthError(resp.status, str(body))
            if resp.status == 429:
                raise AmoCRMAPIRateLimitError(resp.status, str(body))
            if resp.status >= 400:
                raise AmoCRMAPIError(resp.status, str(body))
            return body if isinstance(body, dict) else {}

    async def update_lead_status(self, lead_id: int, status_id: int) -> dict[str, Any]:
        """Перевести сделку на другой этап."""
        return await self.patch(f"/api/v4/leads/{int(lead_id)}",
                                {"status_id": int(status_id)})

    async def fetch_events(
        self,
        *,
        event_types: list[str],
        created_from: int,
        limit: int = 100,
        entity: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "filter[type]": ",".join(event_types),
            "filter[created_at][from]": created_from,
            "limit": limit,
        }
        if entity:
            params["filter[entity]"] = entity
        payload = await self.get("/api/v4/events", params=params)
        return list(((payload.get("_embedded") or {}).get("events") or []))

    async def fetch_lead(self, lead_id: int) -> dict[str, Any]:
        return await self.get(f"/api/v4/leads/{lead_id}", params={"with": "contacts"})

    async def fetch_contact(self, contact_id: int) -> dict[str, Any]:
        return await self.get(f"/api/v4/contacts/{contact_id}")

    async def fetch_contact_leads(self, contact_id: int, *, pipeline_id: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "filter[contacts][id]": contact_id,
            "with": "contacts",
            "limit": 50,
        }
        if pipeline_id:
            params["filter[pipeline_id]"] = pipeline_id
        payload = await self.get("/api/v4/leads", params=params)
        return list(((payload.get("_embedded") or {}).get("leads") or []))

    async def find_contacts_by_phone(self, phone10: str) -> list[dict[str, Any]]:
        """Контакты по десяти цифрам номера — сразу со списком их сделок.

        Поиск у amoCRM подстрочный по всем полям карточки, поэтому среди
        ответа может оказаться чужой контакт: сверять номер обязан вызывающий.
        """
        payload = await self.get(
            "/api/v4/contacts",
            params={"query": phone10, "with": "leads", "limit": 50},
        )
        return list(((payload.get("_embedded") or {}).get("contacts") or []))

    async def fetch_leads_by_ids(self, lead_ids: Iterable[int]) -> list[dict[str, Any]]:
        """Сделки по их номерам — пачками, повторяющимся ключом filter[id][].

        Сделки клиента берутся именно так, через номера из его контакта:
        фильтр `filter[contacts][id]` amoCRM молча игнорирует и отдаёт чужие
        сделки (проверено админ-ботом 2026-08-25).
        """
        batch_size = 50
        unique: list[int] = []
        seen: set[int] = set()
        for raw_id in lead_ids:
            try:
                lead_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if lead_id in seen:
                continue
            seen.add(lead_id)
            unique.append(lead_id)
        leads: list[dict[str, Any]] = []
        for start in range(0, len(unique), batch_size):
            params: list[tuple[str, Any]] = [
                ("filter[id][]", lead_id) for lead_id in unique[start:start + batch_size]
            ]
            params.append(("limit", 250))
            payload = await self.get("/api/v4/leads", params=params)
            leads.extend((payload.get("_embedded") or {}).get("leads") or [])
        return leads

    async def fetch_lead_notes(self, lead_id: int) -> list[dict[str, Any]]:
        payload = await self.get(f"/api/v4/leads/{lead_id}/notes", params={"limit": 100})
        return list(((payload.get("_embedded") or {}).get("notes") or []))

    async def fetch_unsorted(self, *, pipeline_id: int, created_from: int, limit: int = 100) -> list[dict[str, Any]]:
        payload = await self.get(
            "/api/v4/leads/unsorted",
            params={
                "filter[pipeline_id]": pipeline_id,
                "filter[created_at][from]": created_from,
                "limit": limit,
            },
        )
        return list(((payload.get("_embedded") or {}).get("unsorted") or []))


def extract_contact_phone(contact: Mapping[str, Any] | None) -> str | None:
    if not contact:
        return None
    for field in contact.get("custom_fields_values") or []:
        if not isinstance(field, Mapping):
            continue
        if str(field.get("field_code") or "").upper() != "PHONE":
            continue
        for value in field.get("values") or []:
            if isinstance(value, Mapping) and value.get("value"):
                return str(value["value"]).strip()
    return None


def extract_lead_contact_ids(lead: Mapping[str, Any]) -> list[int]:
    contacts = ((lead.get("_embedded") or {}).get("contacts") or [])
    ids: list[int] = []
    for contact in contacts:
        if not isinstance(contact, Mapping):
            continue
        try:
            ids.append(int(contact["id"]))
        except Exception:
            continue
    return ids

def extract_event_entity_id(event: Mapping[str, Any]) -> int | None:
    for key in ("entity_id", "lead_id"):
        if event.get(key) is not None:
            try:
                return int(event[key])
            except Exception:
                return None
    for change in event.get("value_after") or []:
        if not isinstance(change, Mapping):
            continue
        lead = change.get("lead")
        if isinstance(lead, Mapping) and lead.get("id") is not None:
            try:
                return int(lead["id"])
            except Exception:
                return None
    return None


def normalize_lead(payload: Mapping[str, Any]) -> AmoCRMLead:
    return AmoCRMLead(
        lead_id=int(payload["id"]),
        name=str(payload.get("name") or "").strip() or None,
        pipeline_id=int(payload["pipeline_id"]) if payload.get("pipeline_id") is not None else None,
        status_id=int(payload["status_id"]) if payload.get("status_id") is not None else None,
        created_at=int(payload["created_at"]) if payload.get("created_at") is not None else None,
        contact_ids=extract_lead_contact_ids(payload),
        payload=dict(payload),
    )

def build_lead_link(api_base: str, lead_id: int | str | None) -> str | None:
    if not api_base or not lead_id:
        return None
    return f"{api_base.rstrip('/')}/leads/detail/{lead_id}"


def _extract_phone(value: str) -> str | None:
    match = re.search(r"(?:\+7|8)\D*\d{3}\D*\d{3}\D*\d{2}\D*\d{2}", value)
    return match.group(0).strip() if match else None


def _first_text(*values: str | None) -> str | None:
    for value in values:
        if value:
            cleaned = str(value).strip()
            if cleaned:
                return cleaned
    return None


def _nested_text(payload: Mapping[str, Any], *path: str) -> str | None:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return str(current).strip() if current else None

__all__ = [
    "AmoCRMAPIAuthError",
    "AmoCRMAPIClient",
    "AmoCRMAPIError",
    "AmoCRMAPIRateLimitError",
    "AmoCRMLead",
    "build_lead_link",
    "extract_contact_phone",
    "extract_event_entity_id",
    "extract_lead_contact_ids",
    "normalize_lead",
]
