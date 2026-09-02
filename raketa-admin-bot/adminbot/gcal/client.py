"""Обмен с Google Calendar: только чтение, только изменения.

Почему не «перечитывать календарь раз в час»: обычная выдача показывает лишь
существующие записи. Удалённая запись из неё просто исчезает, и отличить отмену
заказа от «запись уехала за край выборки» невозможно. Google для этого даёт
закладку (`syncToken`): робот говорит «что изменилось с прошлого раза» и получает
в том числе удалённые записи со `status: cancelled`.

Правила обмена (developers.google.com/workspace/calendar/api/guides/sync):

1. Первый обмен — полный, с фильтром `timeMin`. В конце приходит `nextSyncToken`.
2. Дальше — только закладка. **Фильтры слать нельзя**: Google ответит 400.
   Неизменяемые параметры (`singleEvents`, `maxResults`) должны совпадать с первым
   запросом, иначе поведение не определено.
3. Закладка живёт неделями, но протухает: 410 `fullSyncRequired` — читаем заново.
4. Пока в ответе есть `nextPageToken`, закладки ещё нет: дочитываем страницы.

Ошибку здесь нельзя проглатывать. Пустой ответ и сбой сети выглядят одинаково
(«изменений нет»), поэтому сбой — исключение: наблюдатель напишет о нём в журнал
и попробует снова, а не решит, что заказов не было.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger(__name__)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
API_BASE = "https://www.googleapis.com"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

# Сколько записей просим за раз. Значение обязано совпадать в полном и
# инкрементальном обмене — оно часть «неизменяемых параметров» Google.
PAGE_SIZE = 250
# Сколько страниц готовы пролистать, прежде чем решить, что что-то не так.
MAX_PAGES = 40


class GCalError(RuntimeError):
    """Google ответил ошибкой или не ответил вовсе."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Google Calendar {status}: {message}")
        self.status = status
        self.message = message


class GCalAuthError(GCalError):
    """Доступ к календарю потерян — ретраи не помогут, нужен человек."""

    def __init__(self, status: int, message: str):
        super().__init__(status, message)
        self.args = (
            f"Google Calendar {status}: доступ к календарю не работает. "
            "Проверьте, что календарь расшарен на служебный аккаунт и ключ не отозван. "
            f"Ответ Google: {message}",
        )


@dataclass(frozen=True)
class SyncBatch:
    """Что пришло за один обмен."""

    events: tuple[dict, ...]
    sync_token: Optional[str]
    full_resync: bool = False        # закладка протухла, читали всё заново


class GoogleCalendar:
    def __init__(
        self,
        *,
        calendar_id: str,
        token: Callable[[], Awaitable[str]],
        base_url: str = API_BASE,
        session: Optional[aiohttp.ClientSession] = None,
        timeout_sec: float = 20.0,
        max_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.calendar_id = calendar_id
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.max_attempts = max_attempts
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

    async def __aenter__(self) -> "GoogleCalendar":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # --- обмен ---

    async def fetch(self, *, sync_token: Optional[str], sync_from: date) -> SyncBatch:
        """Забрать изменения. Без закладки — полный обмен от `sync_from`."""
        try:
            events, token = await self._collect(sync_token=sync_token, sync_from=sync_from)
            return SyncBatch(events=tuple(events), sync_token=token)
        except GCalError as exc:
            if exc.status != 410 or sync_token is None:
                raise
            # Закладка протухла: перечитываем окно целиком и начинаем заново.
            log.warning("Закладка обмена устарела — читаю календарь заново")
            events, token = await self._collect(sync_token=None, sync_from=sync_from)
            return SyncBatch(events=tuple(events), sync_token=token, full_resync=True)

    async def get_event(self, event_id: str) -> Optional[dict]:
        """Одна запись по идентификатору. `None` — в этом календаре её нет.

        Нужна, чтобы отличить переезд записи от отмены заказа: в обмене Google
        и то и другое выглядит одинаково — `status: cancelled` в том календаре,
        откуда запись ушла. Спросив соседний календарь, робот видит разницу.

        Сбой связи наружу как исключение: молчаливое «нет» здесь означало бы
        закрытую сделку по живому заказу.
        """
        url = (f"{self.base_url}/calendar/v3/calendars/"
               f"{quote(self.calendar_id, safe='')}/events/{quote(event_id, safe='')}")
        payload = await self._request([], url=url, none_on_404=True)
        if payload is None or payload.get("status") == "cancelled":
            return None
        return payload

    async def _collect(self, *, sync_token: Optional[str],
                       sync_from: date) -> tuple[list[dict], Optional[str]]:
        events: list[dict] = []
        page_token: Optional[str] = None
        next_sync_token: Optional[str] = None

        for _ in range(MAX_PAGES):
            # None приходит только при none_on_404, а здесь его не просят.
            payload = await self._request(
                self._params(sync_token, sync_from, page_token)) or {}
            events.extend(payload.get("items") or [])

            next_sync_token = payload.get("nextSyncToken")
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        else:
            log.warning("Календарь: страниц больше %s — обмен оборван", MAX_PAGES)

        return events, next_sync_token

    def _params(self, sync_token: Optional[str], sync_from: date,
                page_token: Optional[str]) -> list[tuple[str, str]]:
        """Параметры запроса.

        `singleEvents` и `maxResults` одинаковы в обоих режимах — так требует
        Google. Фильтр `timeMin` допустим ТОЛЬКО в полном обмене: вместе
        с закладкой он даёт 400.
        """
        params: list[tuple[str, str]] = [
            ("singleEvents", "true"),
            ("maxResults", str(PAGE_SIZE)),
        ]
        if sync_token:
            params.append(("syncToken", sync_token))
        else:
            start = datetime.combine(sync_from, time.min, tzinfo=MOSCOW_TZ)
            params.append(("timeMin", start.isoformat()))
        if page_token:
            params.append(("pageToken", page_token))
        return params

    async def _request(self, params: list[tuple[str, str]], *,
                       url: Optional[str] = None,
                       none_on_404: bool = False) -> Optional[dict]:
        """Один запрос с ретраями. Сбой — исключение, а не пустой список.

        `none_on_404` для запроса конкретной записи: «её здесь нет» — это ответ,
        а не сбой, и повторять его незачем.
        """
        session = await self._ensure_session()
        if url is None:
            url = (f"{self.base_url}/calendar/v3/calendars/"
                   f"{quote(self.calendar_id, safe='')}/events")
        headers = {"Authorization": f"Bearer {await self._token()}",
                   "Accept": "application/json"}
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                async with session.get(
                    url, headers=headers, params=params,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
                ) as resp:
                    if resp.status in (401, 403):
                        raise GCalAuthError(resp.status, await resp.text())
                    if resp.status == 410:
                        raise GCalError(410, await resp.text())
                    if resp.status == 404 and none_on_404:
                        return None
                    if resp.status >= 400:
                        last_error = GCalError(resp.status, await resp.text())
                    else:
                        payload: Any = await resp.json(content_type=None)
                        if not isinstance(payload, dict):
                            raise GCalError(resp.status, "неожиданный ответ: не объект")
                        return payload
            except (GCalAuthError, GCalError) as exc:
                if isinstance(exc, GCalAuthError) or exc.status == 410:
                    raise
                last_error = exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc

            if attempt < self.max_attempts:
                await self._sleep(attempt)

        if isinstance(last_error, GCalError):
            raise last_error
        raise GCalError(0, f"календарь недоступен: {last_error}")
