"""Клиент amoCRM API v4 — чтение.

Порт `tgbot-v1/notifications/amocrm_api.py` (класс AmoCRMAPIClient) с добавлением
пагинации и ретраев. Телефоны в логи попадают только замаскированными.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Mapping, Optional

import aiohttp

log = logging.getLogger(__name__)

# Сколько записей просим за раз (потолок амо для большинства сущностей — 250).
PAGE_LIMIT = 250
# Сколько страниц готовы пролистать, прежде чем решить, что что-то не так.
MAX_PAGES = 20


class AmoError(RuntimeError):
    """Амо ответила ошибкой."""

    def __init__(self, status: int, message: str):
        super().__init__(f"amoCRM {status}: {message}")
        self.status = status
        self.message = message


class AmoAuthError(AmoError):
    """Токен протух или отозван — ретраи бесполезны, нужен человек."""


class AmoRateLimitError(AmoError):
    """Слишком часто стучимся — нужно подождать."""


def _mask(phone: str) -> str:
    """Телефон в логах — только последние 4 цифры (правило проекта по ПД)."""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    return "…" + digits[-4:] if digits else "…"


class AmoClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        session: Optional[aiohttp.ClientSession] = None,
        timeout_sec: float = 15.0,
        max_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        dry_run: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout_sec = timeout_sec
        self.max_attempts = max_attempts
        self.dry_run = dry_run
        self._sleep = sleep
        self._session = session
        self._owns_session = session is None

    # --- жизненный цикл ---

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "AmoClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # --- транспорт ---

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
    ) -> Optional[dict]:
        """Запрос с ретраями. None — если амо ответила «пусто» (204) или 404.

        Ретраим только то, что имеет шанс пройти со второй попытки: перегрузку
        (429), ошибки сервера (5xx) и обрывы сети. Протухший токен не ретраим.
        """
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                async with session.request(
                    method, url, headers=headers,
                    params=dict(params or {}), json=json_body,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
                ) as resp:
                    if resp.status in (204, 404):
                        return None
                    if resp.status == 401:
                        raise AmoAuthError(resp.status, await resp.text())
                    if resp.status == 429:
                        last_error = AmoRateLimitError(resp.status, await resp.text())
                    elif resp.status >= 400:
                        last_error = AmoError(resp.status, await resp.text())
                    else:
                        payload = await resp.json(content_type=None)
                        if payload is None:
                            return None
                        if not isinstance(payload, dict):
                            raise AmoError(resp.status, "неожиданный ответ: не объект")
                        return payload
            except AmoAuthError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc

            if attempt < self.max_attempts:
                pause = 2 ** attempt
                log.warning(
                    "amoCRM %s %s: попытка %s/%s не удалась (%s), пауза %s с",
                    method, path, attempt, self.max_attempts, last_error, pause,
                )
                await self._sleep(pause)

        assert last_error is not None
        if isinstance(last_error, AmoError):
            raise last_error
        raise AmoError(0, f"нет связи с amoCRM: {last_error}")

    async def get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Optional[dict]:
        return await self.request("GET", path, params=params)

    async def get_all(
        self, path: str, key: str, *, params: Optional[Mapping[str, Any]] = None
    ) -> list[dict]:
        """Собрать все страницы списка. Амо кладёт записи в _embedded[key]."""
        items: list[dict] = []
        page = 1
        query = dict(params or {})
        query.setdefault("limit", PAGE_LIMIT)

        while page <= MAX_PAGES:
            query["page"] = page
            payload = await self.get(path, params=query)
            if not payload:                       # 204 «ничего не найдено»
                break
            chunk = ((payload.get("_embedded") or {}).get(key)) or []
            items.extend(item for item in chunk if isinstance(item, dict))
            if not ((payload.get("_links") or {}).get("next")):
                break
            page += 1
        else:
            log.warning("amoCRM %s: достигнут предел в %s страниц", path, MAX_PAGES)

        return items

    # --- чтение ---

    async def find_contacts_by_phone(self, phone10: str) -> list[dict]:
        """Контакты по телефону. Амо ищет подстрокой через параметр query."""
        log.debug("amoCRM: ищу контакты по телефону %s", _mask(phone10))
        contacts = await self.get_all("/api/v4/contacts", "contacts", params={"query": phone10})
        log.debug("amoCRM: по телефону %s найдено контактов: %s", _mask(phone10), len(contacts))
        return contacts

    async def get_contact_leads(self, contact_id: int) -> list[dict]:
        """Все сделки контакта — кандидаты для матчера."""
        return await self.get_all(
            "/api/v4/leads", "leads",
            params={"filter[contacts][id]": contact_id, "with": "contacts"},
        )

    async def get_lead(self, lead_id: int) -> Optional[dict]:
        """Сделка целиком. None — если сделки нет (удалена)."""
        return await self.get(f"/api/v4/leads/{lead_id}", params={"with": "contacts"})

    async def get_lead_tasks(self, lead_id: int) -> list[dict]:
        """Открытые задачи сделки — их робот закрывает при проведении."""
        return await self.get_all(
            "/api/v4/tasks", "tasks",
            params={
                "filter[entity_type]": "leads",
                "filter[entity_id]": lead_id,
                "filter[is_completed]": 0,
            },
        )

    async def get_contact(self, contact_id: int) -> Optional[dict]:
        return await self.get(f"/api/v4/contacts/{contact_id}")
