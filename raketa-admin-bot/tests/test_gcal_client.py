"""Тесты обмена с Google Calendar на поддельном сервере — живой Google не участвует.

Главное, что здесь проверяется, — правила инкрементального обмена, из-за которых
этап 2 вообще возможен:

- с закладкой (`syncToken`) нельзя слать фильтры: Google ответит 400, и робот
  ослепнет — а мы этого не заметим, потому что «изменений нет» выглядит нормально;
- удалённая запись приходит ТОЛЬКО в таком обмене, со `status: cancelled`. Это
  единственный способ узнать об отмене заказа;
- закладка протухает (410) — тогда нужно перечитать всё заново, а не упасть.
"""

from datetime import date
from typing import Any, NamedTuple

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from adminbot.gcal.client import GCalAuthError, GCalError, GoogleCalendar

CALENDAR = "raketaclean52@gmail.com"
EVENTS_PATH = f"/calendar/v3/calendars/{CALENDAR}/events"

ORDER = {"id": "p0rag0", "summary": "Сов! Матрас, Юлия", "status": "confirmed",
         "start": {"dateTime": "2026-08-24T14:30:00+03:00"}}
DELETED = {"id": "b5k0lf", "status": "cancelled"}


class Recorded(NamedTuple):
    query: dict
    auth: str
    path: str = ""


class FakeGoogle:
    """Поддельный Google Calendar: отдаёт заготовленные ответы и помнит запросы."""

    def __init__(self):
        self.requests: list[Recorded] = []
        self.responses: list[Any] = []

    def stub(self, *responses):
        """responses: dict (200 + json) или (код, json)."""
        self.responses = list(responses)

    async def handle(self, request: web.Request):
        self.requests.append(Recorded(dict(request.query),
                                      request.headers.get("Authorization", ""),
                                      request.path))
        item = self.responses.pop(0) if self.responses else {"items": []}
        if isinstance(item, tuple):
            status, body = item
            return web.json_response(body, status=status)
        return web.json_response(item)


@pytest.fixture
async def google():
    fake = FakeGoogle()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()

    async def token() -> str:
        return "ya29.test-token"

    async def no_sleep(_seconds: float) -> None:
        return None

    client = GoogleCalendar(calendar_id=CALENDAR, token=token,
                            base_url=str(server.make_url("")).rstrip("/"),
                            sleep=no_sleep)          # ретраи без реальных пауз
    try:
        yield client, fake
    finally:
        await client.close()
        await server.close()


async def test_first_exchange_is_limited_by_the_start_date(google):
    """Первый обмен — полный, но не глубже даты включения (решение владельца 8)."""
    client, fake = google
    fake.stub({"items": [ORDER], "nextSyncToken": "TOKEN-1"})

    batch = await client.fetch(sync_token=None, sync_from=date(2026, 8, 26))

    query = fake.requests[0].query
    assert query["timeMin"].startswith("2026-08-26T00:00:00")
    assert query["singleEvents"] == "true"
    assert "syncToken" not in query
    assert batch.events == (ORDER,)
    assert batch.sync_token == "TOKEN-1"
    assert batch.full_resync is False


async def test_incremental_exchange_sends_only_the_bookmark(google):
    """С закладкой фильтры слать нельзя — Google ответит 400 и робот ослепнет."""
    client, fake = google
    fake.stub({"items": [DELETED], "nextSyncToken": "TOKEN-2"})

    batch = await client.fetch(sync_token="TOKEN-1", sync_from=date(2026, 8, 26))

    query = fake.requests[0].query
    assert query["syncToken"] == "TOKEN-1"
    assert "timeMin" not in query
    assert "orderBy" not in query
    assert query["singleEvents"] == "true"      # неизменяемые параметры совпадают
    assert batch.events == (DELETED,)           # удалённая запись дошла как есть
    assert batch.sync_token == "TOKEN-2"


async def test_pages_are_followed_until_the_bookmark_arrives(google):
    """Пока приходит pageToken, закладки ещё нет: дочитываем до конца."""
    client, fake = google
    fake.stub({"items": [ORDER], "nextPageToken": "PAGE-2"},
              {"items": [DELETED], "nextSyncToken": "TOKEN-3"})

    batch = await client.fetch(sync_token="TOKEN-1", sync_from=date(2026, 8, 26))

    assert fake.requests[1].query["pageToken"] == "PAGE-2"
    assert fake.requests[1].query["syncToken"] == "TOKEN-1"
    assert batch.events == (ORDER, DELETED)
    assert batch.sync_token == "TOKEN-3"


async def test_expired_bookmark_triggers_a_full_reread(google):
    """410 = закладка протухла. Читаем всё заново, а не падаем."""
    client, fake = google
    fake.stub((410, {"error": {"errors": [{"reason": "fullSyncRequired"}]}}),
              {"items": [ORDER], "nextSyncToken": "TOKEN-NEW"})

    batch = await client.fetch(sync_token="OLD", sync_from=date(2026, 8, 26))

    assert batch.full_resync is True
    assert batch.sync_token == "TOKEN-NEW"
    assert batch.events == (ORDER,)
    assert "timeMin" in fake.requests[1].query      # второй заход — полный обмен


async def test_access_token_is_sent(google):
    client, fake = google
    fake.stub({"items": [], "nextSyncToken": "T"})

    await client.fetch(sync_token=None, sync_from=date(2026, 8, 26))

    assert fake.requests[0].auth == "Bearer ya29.test-token"


async def test_lost_access_is_a_separate_error(google):
    """Отозвали доступ к календарю — владельцу нужен понятный текст, а не стектрейс."""
    client, fake = google
    fake.stub((401, {"error": {"message": "Invalid Credentials"}}))

    with pytest.raises(GCalAuthError) as failure:
        await client.fetch(sync_token=None, sync_from=date(2026, 8, 26))

    assert "календар" in str(failure.value).lower()


async def test_google_outage_does_not_look_like_no_changes(google):
    """Сбой должен быть ошибкой: «изменений нет» на сбое — молчаливая слепота."""
    client, fake = google
    fake.stub((500, {"error": {"message": "Backend Error"}}),
              (500, {"error": {"message": "Backend Error"}}),
              (500, {"error": {"message": "Backend Error"}}))

    with pytest.raises(GCalError):
        await client.fetch(sync_token=None, sync_from=date(2026, 8, 26))


# --- одна запись по идентификатору: нужна, чтобы отличить переезд от отмены ---

async def test_single_event_is_fetched_by_id(google):
    """Запись, исчезнувшую из одного календаря, ищем в остальных по её id."""
    client, fake = google
    fake.stub(ORDER)

    found = await client.get_event("p0rag0")

    assert found == ORDER
    assert fake.requests[0].path.endswith("/events/p0rag0")


async def test_missing_event_is_not_an_error(google):
    """Записи в этом календаре нет — это ответ «нет», а не сбой.

    И повторять запрос незачем: 404 не станет другим от второй попытки.
    """
    client, fake = google
    fake.stub((404, {"error": {"message": "Not Found"}}))

    assert await client.get_event("p0rag0") is None
    assert len(fake.requests) == 1


async def test_deleted_event_counts_as_missing(google):
    """Запись удалена и здесь: Google отдаёт её со status cancelled."""
    client, fake = google
    fake.stub(DELETED)

    assert await client.get_event("b5k0lf") is None


async def test_outage_while_looking_for_the_event_is_an_error(google):
    """Сбой не должен выглядеть как «записи нет»: иначе переезд сочтут отменой."""
    client, fake = google
    fake.stub((500, {"error": {"message": "Backend Error"}}),
              (500, {"error": {"message": "Backend Error"}}),
              (500, {"error": {"message": "Backend Error"}}))

    with pytest.raises(GCalError):
        await client.get_event("p0rag0")
