"""Тесты клиента amoCRM на поддельном сервере — живая CRM не участвует."""

from decimal import Decimal
from typing import Any, NamedTuple

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from adminbot.amo import ids
from adminbot.amo.client import AmoAuthError, AmoClient, AmoError, AmoRateLimitError
from adminbot.amo.fields import contact_phones, field_value, order_date_msk, specialist_ids

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


class Recorded(NamedTuple):
    """Запрос, который клиент отправил в поддельную амо."""

    path: str
    query: dict
    method: str = "GET"
    body: Any = None


class FakeAmo:
    """Поддельная amoCRM: отдаёт заготовленные ответы и помнит запросы."""

    def __init__(self):
        self.requests: list[Recorded] = []
        self.responses: dict[str, list] = {}

    def stub(self, path: str, *responses):
        """responses: dict (200 + json), int (голый код) или (код, json)."""
        self.responses[path] = list(responses)

    def _next(self, path):
        queue = self.responses.get(path) or []
        return queue.pop(0) if len(queue) > 1 else (queue[0] if queue else 404)

    async def handle(self, request: web.Request):
        body = None
        if request.can_read_body:
            try:
                body = await request.json()
            except Exception:
                body = await request.text()
        self.requests.append(
            Recorded(request.path, dict(request.query), request.method, body)
        )
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
    assert fake.requests[0][1]["with"] == "leads"           # сразу со списком сделок клиента
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

    leads = await client.get_leads_by_ids([7])

    assert [lead["id"] for lead in leads] == [7]
    assert len(fake.requests) == 2          # первая попытка отбита, вторая прошла


async def test_persistent_server_error_gives_up_after_max_attempts(amo):
    client, fake = amo
    fake.stub("/api/v4/leads", (500, {"title": "Server error"}),
              (500, {"title": "Server error"}), (500, {"title": "Server error"}))

    with pytest.raises(AmoError):
        await client.get_leads_by_ids([7])

    assert len(fake.requests) == 3          # ровно max_attempts попыток


async def test_rate_limit_error_type_after_all_attempts(amo):
    client, fake = amo
    fake.stub("/api/v4/leads", (429, {}), (429, {}), (429, {}))

    with pytest.raises(AmoRateLimitError):
        await client.get_leads_by_ids([7])


async def test_leads_are_fetched_by_id_never_by_contact_filter(amo):
    """Амо МОЛЧА игнорирует filter[contacts][id] и отдаёт все сделки подряд.

    Проверено на боевом аккаунте 2026-08-25: запрос сделок контакта вернул
    250 чужих сделок и ни одной его собственной. Поэтому список сделок клиента
    берём из самого контакта (with=leads), а сами сделки — пакетом по id.
    """
    client, fake = amo
    fake.stub("/api/v4/contacts/111",
              {"id": 111, "_embedded": {"leads": [{"id": 501}, {"id": 502}]}})
    fake.stub("/api/v4/leads", {"_embedded": {"leads": [{"id": 501}, {"id": 502}]}})

    leads = await client.get_contact_leads(111)

    assert [lead["id"] for lead in leads] == [501, 502]
    paths = [record.path for record in fake.requests]
    assert paths == ["/api/v4/contacts/111", "/api/v4/leads"]
    all_params = " ".join(str(record.query) for record in fake.requests)
    assert "filter[contacts]" not in all_params      # сломанный фильтр не используем


async def test_get_leads_by_ids_splits_into_batches(amo):
    client, fake = amo
    fake.stub("/api/v4/leads", {"_embedded": {"leads": [{"id": 1}]}})

    await client.get_leads_by_ids(list(range(1, 121)))     # 120 сделок

    # по 50 за запрос → три пакета; иначе URL распухает и амо режет ответ
    assert len(fake.requests) == 3


async def test_get_leads_by_ids_deduplicates_and_ignores_empty(amo):
    client, fake = amo
    fake.stub("/api/v4/leads", {"_embedded": {"leads": [{"id": 5}]}})

    assert await client.get_leads_by_ids([]) == []
    assert not fake.requests                            # пустой список — без похода в сеть

    leads = await client.get_leads_by_ids([5, 5, 5])
    assert [lead["id"] for lead in leads] == [5]        # дубли id не удваивают запросы
    assert len(fake.requests) == 1


async def test_find_leads_by_phone_uses_two_requests(amo):
    """Полный путь «телефон → сделки клиента»: поиск контактов + пакет сделок."""
    client, fake = amo
    fake.stub("/api/v4/contacts", {
        "_embedded": {"contacts": [
            {"id": 111, "_embedded": {"leads": [{"id": 501}]}},
            {"id": 222, "_embedded": {"leads": [{"id": 502}]}},   # дубль контакта клиента
        ]}
    })
    fake.stub("/api/v4/leads", {"_embedded": {"leads": [{"id": 501}, {"id": 502}]}})

    leads = await client.find_leads_by_phone("9601861067")

    assert [lead["id"] for lead in leads] == [501, 502]
    assert len(fake.requests) == 2       # сделки обоих контактов берём одним пакетом


async def test_find_leads_by_phone_without_contacts(amo):
    client, fake = amo
    fake.stub("/api/v4/contacts", 204)

    assert await client.find_leads_by_phone("9999999999") == []
    assert len(fake.requests) == 1       # нет контакта — за сделками не идём


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
    query = fake.requests[0].query
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


async def test_get_lead_field_enums(amo):
    client, fake = amo
    fake.stub("/api/v4/leads/custom_fields/39243", {
        "id": 39243, "name": "Специалист",
        "enums": [{"id": 951507, "value": "Дмитрий Козлов +79306858534"}],
    })

    enums = await client.get_lead_field_enums(ids.FIELD_SPECIALIST)
    assert enums == [{"id": 951507, "value": "Дмитрий Козлов +79306858534"}]


def test_specialist_ids_read_from_lead():
    lead = {
        "custom_fields_values": [
            {"field_id": ids.FIELD_SPECIALIST, "values": [
                {"value": "Дмитрий Козлов +79306858534", "enum_id": 951507}]},
            {"field_id": ids.FIELD_SERVICE, "values": [
                {"value": "Чистка мебели", "enum_id": 933165}]},
        ]
    }
    assert specialist_ids(lead) == (951507,)
    assert specialist_ids({}) == ()
    # текстовое поле без enum_id в список не попадает
    assert specialist_ids({"custom_fields_values": [
        {"field_id": ids.FIELD_SPECIALIST, "values": [{"value": "Кто-то"}]}]}) == ()


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
