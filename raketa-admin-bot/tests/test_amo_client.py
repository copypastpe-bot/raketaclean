"""Тесты клиента amoCRM на поддельном сервере — живая CRM не участвует."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from adminbot.amo import ids
from adminbot.amo.client import AmoAuthError, AmoClient, AmoError, AmoRateLimitError
from adminbot.amo.fields import contact_phones, field_value, order_date_msk

CONTACT_PAGE_1 = {
    "_page": 1,
    "_links": {"next": {"href": "/api/v4/contacts?page=2"}},
    "_embedded": {"contacts": [{"id": 111, "name": "Ирина"}]},
}
CONTACT_PAGE_2 = {
    "_page": 2,
    "_links": {},
    "_embedded": {"contacts": [{"id": 222, "name": "Ирина (дубль)"}]},
}


class FakeAmo:
    """Поддельная amoCRM: отдаёт заготовленные ответы и помнит запросы."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.responses: dict[str, list] = {}

    def stub(self, path: str, *responses):
        """responses: dict (200 + json), int (голый код) или (код, json)."""
        self.responses[path] = list(responses)

    def _next(self, path):
        queue = self.responses.get(path) or []
        return queue.pop(0) if len(queue) > 1 else (queue[0] if queue else 404)

    async def handle(self, request: web.Request):
        self.requests.append((request.path, dict(request.query)))
        item = self._next(request.path)
        if isinstance(item, int):
            return web.Response(status=item)
        if isinstance(item, tuple):
            status, body = item
            return web.json_response(body, status=status)
        if callable(item):
            return await item(request)
        return web.json_response(item)


@pytest.fixture
async def amo():
    fake = FakeAmo()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    client = AmoClient(
        base_url=str(server.make_url("")).rstrip("/"),
        token="test-token",
        max_attempts=3,
        sleep=_no_sleep,          # ретраи без реальных пауз
    )
    try:
        yield client, fake
    finally:
        await client.close()
        await server.close()


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_find_contacts_by_phone_follows_pagination(amo):
    client, fake = amo
    fake.stub("/api/v4/contacts", CONTACT_PAGE_1, CONTACT_PAGE_2)

    contacts = await client.find_contacts_by_phone("9601861067")

    assert [c["id"] for c in contacts] == [111, 222]        # обе страницы склеены
    assert fake.requests[0][1]["query"] == "9601861067"     # ищем по телефону
    assert fake.requests[1][1]["page"] == "2"               # вторая страница запрошена


async def test_no_contacts_returns_empty_list(amo):
    """Амо на «ничего не найдено» отвечает 204 без тела — это не ошибка."""
    client, fake = amo
    fake.stub("/api/v4/contacts", 204)

    assert await client.find_contacts_by_phone("9999999999") == []


async def test_expired_token_raises_auth_error(amo):
    client, fake = amo
    fake.stub("/api/v4/contacts", (401, {"title": "Unauthorized"}))

    with pytest.raises(AmoAuthError):
        await client.find_contacts_by_phone("9601861067")

    assert len(fake.requests) == 1          # протухший токен не ретраим — бесполезно


async def test_rate_limit_is_retried_then_succeeds(amo):
    client, fake = amo
    fake.stub("/api/v4/leads", (429, {"title": "Too Many Requests"}),
              {"_embedded": {"leads": [{"id": 7}]}})

    leads = await client.get_contact_leads(111)

    assert [lead["id"] for lead in leads] == [7]
    assert len(fake.requests) == 2          # первая попытка отбита, вторая прошла


async def test_persistent_server_error_gives_up_after_max_attempts(amo):
    client, fake = amo
    fake.stub("/api/v4/leads", (500, {"title": "Server error"}),
              (500, {"title": "Server error"}), (500, {"title": "Server error"}))

    with pytest.raises(AmoError):
        await client.get_contact_leads(111)

    assert len(fake.requests) == 3          # ровно max_attempts попыток


async def test_rate_limit_error_type_after_all_attempts(amo):
    client, fake = amo
    fake.stub("/api/v4/leads", (429, {}), (429, {}), (429, {}))

    with pytest.raises(AmoRateLimitError):
        await client.get_contact_leads(111)


async def test_get_lead_and_missing_lead(amo):
    client, fake = amo
    fake.stub("/api/v4/leads/500", {"id": 500, "pipeline_id": ids.PIPELINE_REALIZATION})
    fake.stub("/api/v4/leads/404", 404)

    lead = await client.get_lead(500)
    assert lead["pipeline_id"] == ids.PIPELINE_REALIZATION
    assert await client.get_lead(404) is None      # удалённая сделка — не авария


async def test_get_lead_tasks_filters_by_lead(amo):
    client, fake = amo
    fake.stub("/api/v4/tasks", {"_embedded": {"tasks": [{"id": 1, "task_type_id": 2270740}]}})

    tasks = await client.get_lead_tasks(500)

    assert [t["id"] for t in tasks] == [1]
    _, query = fake.requests[0]
    assert query["filter[entity_type]"] == "leads"
    assert query["filter[entity_id]"] == "500"
    assert query["filter[is_completed]"] == "0"   # закрытые задачи нас не интересуют


async def test_phone_is_masked_in_logs(amo, caplog):
    client, fake = amo
    fake.stub("/api/v4/contacts", 204)

    with caplog.at_level("DEBUG", logger="adminbot.amo.client"):
        await client.find_contacts_by_phone("9601861067")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "9601861067" not in logged            # ПД в логи не попадают
    assert "1067" in logged                       # но заказ опознать можно


# --- разбор полей сделки/контакта ---

LEAD_WITH_FIELDS = {
    "id": 500,
    "custom_fields_values": [
        {"field_id": ids.FIELD_ORDER_DATETIME, "values": [{"value": 1755000000}]},
        {"field_id": ids.FIELD_ADDRESS, "values": [{"value": "ул. Ленина, 5"}]},
    ],
}


def test_field_value_reads_custom_field():
    assert field_value(LEAD_WITH_FIELDS, ids.FIELD_ADDRESS) == "ул. Ленина, 5"
    assert field_value(LEAD_WITH_FIELDS, ids.FIELD_SERVICE) is None
    assert field_value({}, ids.FIELD_ADDRESS) is None            # поле не заполнено вовсе


def test_order_date_uses_moscow_time():
    # 1755000000 = 2025-08-12 12:40 UTC → в Москве 15:40 того же дня
    assert order_date_msk(LEAD_WITH_FIELDS).isoformat() == "2025-08-12"
    assert order_date_msk({}) is None                            # «Дата заказа» не заполнена


def test_contact_phones_normalized_to_last10():
    contact = {
        "custom_fields_values": [
            {"field_code": "PHONE", "values": [
                {"value": "+7 960 186-10-67"}, {"value": "89159496642"}]},
            {"field_code": "EMAIL", "values": [{"value": "a@b.ru"}]},
        ]
    }
    assert contact_phones(contact) == ["9601861067", "9159496642"]
    assert contact_phones({}) == []
