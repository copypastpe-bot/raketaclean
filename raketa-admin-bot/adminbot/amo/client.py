"""Клиент amoCRM API v4 — чтение.

Порт `tgbot-v1/notifications/amocrm_api.py` (класс AmoCRMAPIClient) с добавлением
пагинации и ретраев. Телефоны в логи попадают только замаскированными.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence, Union

import aiohttp

from adminbot.amo.fields import contact_lead_ids

log = logging.getLogger(__name__)

# Текст в результате закрытой автозадачи: в истории амо видно, кто её закрыл.
ROBOT_TASK_RESULT = "Закрыто роботом amo_sync"


@dataclass(frozen=True)
class Intent:
    """Что робот собирается сделать (или уже сделал) в amoCRM.

    В режиме репетиции возвращается неисполненным: `performed=False`, запрос
    не отправлен, но payload — ровно тот, что ушёл бы в бою. Этого достаточно
    и для предпросмотра владельцу, и для записи в журнал `adminbot.amo_actions`.
    """

    action: str                       # update_lead | move_lead | create_lead | …
    entity: str                       # lead | contact | task
    entity_id: Optional[int]          # None у создания, пока сущность не создана
    payload: Any                      # тело запроса как есть
    performed: bool = False

# Параметры запроса: словарь либо список пар (для повторяющихся ключей filter[id][]).
Params = Union[Mapping[str, Any], Sequence[tuple[str, Any]]]

# Сколько записей просим за раз (потолок амо для большинства сущностей — 250).
PAGE_LIMIT = 250
# Сколько страниц готовы пролистать, прежде чем решить, что что-то не так.
MAX_PAGES = 20
# Сколько сделок запрашиваем одним пакетом: длинный URL амо обрезает.
LEADS_BATCH = 50


def _as_pairs(params: Optional[Params]) -> list[tuple[str, Any]]:
    """Привести параметры к списку пар: filter[id][] повторяется много раз."""
    if params is None:
        return []
    if isinstance(params, Mapping):
        return list(params.items())
    return list(params)


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
        params: Optional[Params] = None,
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
                    params=[(name, str(value)) for name, value in _as_pairs(params)],
                    json=json_body,
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

    async def get(self, path: str, *, params: Optional[Params] = None) -> Optional[dict]:
        return await self.request("GET", path, params=params)

    async def get_all(
        self, path: str, key: str, *, params: Optional[Params] = None
    ) -> list[dict]:
        """Собрать все страницы списка. Амо кладёт записи в _embedded[key]."""
        items: list[dict] = []
        page = 1
        base = _as_pairs(params)
        if not any(name == "limit" for name, _ in base):
            base.append(("limit", PAGE_LIMIT))

        while page <= MAX_PAGES:
            query = base + [("page", page)]
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
        """Контакты по телефону, сразу со списком их сделок (with=leads).

        Амо ищет подстрокой через параметр query. Список сделок приходит здесь же —
        это единственный надёжный способ узнать, какие сделки принадлежат клиенту
        (см. get_contact_leads о сломанном фильтре).
        """
        log.debug("amoCRM: ищу контакты по телефону %s", _mask(phone10))
        contacts = await self.get_all(
            "/api/v4/contacts", "contacts", params={"query": phone10, "with": "leads"}
        )
        log.debug("amoCRM: по телефону %s найдено контактов: %s", _mask(phone10), len(contacts))
        return contacts

    async def get_leads_by_ids(self, lead_ids: Iterable[int]) -> list[dict]:
        """Сделки по списку id, пакетами. Порядок id сохраняется, дубли отбрасываются."""
        unique: list[int] = []
        seen: set[int] = set()
        for lead_id in lead_ids:
            value = int(lead_id)
            if value not in seen:
                seen.add(value)
                unique.append(value)
        if not unique:
            return []

        leads: list[dict] = []
        for start in range(0, len(unique), LEADS_BATCH):
            chunk = unique[start:start + LEADS_BATCH]
            params: list[tuple[str, Any]] = [("filter[id][]", value) for value in chunk]
            leads.extend(await self.get_all("/api/v4/leads", "leads", params=params))
        return leads

    async def get_contact_leads(self, contact_id: int) -> list[dict]:
        """Все сделки контакта — кандидаты для матчера.

        ВНИМАНИЕ: НЕ использовать `/api/v4/leads?filter[contacts][id]=…` — амо молча
        игнорирует этот фильтр и отдаёт все сделки аккаунта подряд (проверено на
        боевом аккаунте 2026-08-25: 250 чужих сделок, ни одной своей). Правильный
        путь — спросить у самого контакта, какие сделки к нему привязаны.
        """
        contact = await self.get(f"/api/v4/contacts/{contact_id}", params={"with": "leads"})
        return await self.get_leads_by_ids(contact_lead_ids(contact))

    async def find_leads_by_phone(self, phone10: str) -> list[dict]:
        """Все сделки клиента по телефону — вход для матчера. Обычно 2 запроса."""
        contacts = await self.find_contacts_by_phone(phone10)
        lead_ids = [lead_id for contact in contacts for lead_id in contact_lead_ids(contact)]
        return await self.get_leads_by_ids(lead_ids)

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

    # --- запись ---
    #
    # Каждый метод сначала описывает НАМЕРЕНИЕ, а потом (если не репетиция) исполняет его.
    # В режиме репетиции в сеть не уходит ничего, а вызывающий слой всё равно получает
    # полное описание запроса — то самое, что ушло бы в бою. Так предпросмотр показывает
    # ровно то, что произойдёт, а журнал `adminbot.amo_actions` пишется одинаково
    # и в репетиции, и в бою.

    async def _perform(self, intent: Intent, method: str, path: str,
                       result_key: Optional[str] = None) -> Intent:
        if self.dry_run:
            log.info("amoCRM (репетиция): %s %s", intent.action, intent.entity_id or "")
            return intent

        payload = await self.request(method, path, json_body=intent.payload)
        entity_id = intent.entity_id
        if result_key and payload:
            created = ((payload.get("_embedded") or {}).get(result_key)) or []
            if created and isinstance(created[0], Mapping) and created[0].get("id") is not None:
                entity_id = int(created[0]["id"])
        return replace(intent, entity_id=entity_id, performed=True)

    async def update_lead(
        self,
        lead_id: int,
        *,
        price: Optional[Decimal] = None,
        custom_fields: Optional[Sequence[dict]] = None,
        name: Optional[str] = None,
    ) -> Optional[Intent]:
        """Заполнить сделку: бюджет, кастомные поля, название.

        None — если менять нечего: пустой запрос в амо не отправляем.
        """
        body: dict[str, Any] = {}
        if price is not None:
            body["price"] = int(price)          # амо хранит бюджет целым числом
        if name is not None:
            body["name"] = name
        if custom_fields:
            body["custom_fields_values"] = list(custom_fields)
        if not body:
            return None

        intent = Intent(action="update_lead", entity="lead", entity_id=lead_id, payload=body)
        return await self._perform(intent, "PATCH", f"/api/v4/leads/{lead_id}")

    async def move_lead(self, lead_id: int, pipeline_id: int, status_id: int) -> Intent:
        """Перевести сделку на другой этап (в том числе в другую воронку)."""
        body = {"pipeline_id": pipeline_id, "status_id": status_id}
        intent = Intent(action="move_lead", entity="lead", entity_id=lead_id, payload=body)
        return await self._perform(intent, "PATCH", f"/api/v4/leads/{lead_id}")

    async def create_lead(
        self,
        *,
        name: str,
        pipeline_id: int,
        status_id: int,
        price: Optional[Decimal] = None,
        contact_id: Optional[int] = None,
        custom_fields: Optional[Sequence[dict]] = None,
    ) -> Intent:
        """Завести сделку. Контакт привязывается сразу, отдельным запросом не надо."""
        entity: dict[str, Any] = {"name": name, "pipeline_id": pipeline_id, "status_id": status_id}
        if price is not None:
            entity["price"] = int(price)
        if custom_fields:
            entity["custom_fields_values"] = list(custom_fields)
        if contact_id is not None:
            entity["_embedded"] = {"contacts": [{"id": contact_id}]}

        intent = Intent(action="create_lead", entity="lead", entity_id=None, payload=[entity])
        return await self._perform(intent, "POST", "/api/v4/leads", result_key="leads")

    async def create_contact(self, *, name: str, phone: str) -> Intent:
        """Завести контакт с телефоном в стандартном поле амо."""
        entity = {
            "name": name,
            "custom_fields_values": [
                {"field_code": "PHONE", "values": [{"value": phone, "enum_code": "WORK"}]}
            ],
        }
        intent = Intent(action="create_contact", entity="contact", entity_id=None, payload=[entity])
        return await self._perform(intent, "POST", "/api/v4/contacts", result_key="contacts")

    async def update_contact(self, contact_id: int, *, name: str) -> Intent:
        """Переименовать контакт — например, заменить «Входящий 79…» на имя клиента."""
        body = {"name": name}
        intent = Intent(action="update_contact", entity="contact", entity_id=contact_id,
                        payload=body)
        return await self._perform(intent, "PATCH", f"/api/v4/contacts/{contact_id}")

    async def complete_task(self, task_id: int, result_text: str = ROBOT_TASK_RESULT) -> Intent:
        """Закрыть автозадачу. В тексте результата видно, что это сделал робот."""
        body = {"is_completed": True, "result": {"text": result_text}}
        intent = Intent(action="complete_task", entity="task", entity_id=task_id, payload=body)
        return await self._perform(intent, "PATCH", f"/api/v4/tasks/{task_id}")

    async def add_note(self, lead_id: int, text: str) -> Intent:
        """Написать комментарий к сделке — например, пометить лид-дубль."""
        entity = {"note_type": "common", "params": {"text": text}}
        intent = Intent(action="add_note", entity="lead", entity_id=lead_id, payload=[entity])
        return await self._perform(intent, "POST", f"/api/v4/leads/{lead_id}/notes",
                                   result_key="notes")

    async def get_lead_field_enums(self, field_id: int) -> list[dict]:
        """Варианты списочного поля сделки (например, все «Специалисты»).

        Читаем из амо, а не держим у себя: список мастеров меняется.
        """
        payload = await self.get(f"/api/v4/leads/custom_fields/{field_id}")
        return list((payload or {}).get("enums") or [])
