"""Тесты записи в amoCRM: проверяем СФОРМИРОВАННЫЕ запросы на поддельном сервере.

Живая CRM здесь не участвует ни в одном тесте.
Главное правило: при включённой репетиции (dry_run) в сеть не уходит НИЧЕГО.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ, datetime_field, enum_field, text_field
from tests.test_amo_client import FakeAmo, _no_sleep      # переиспользуем поддельную амо

from aiohttp import web
from aiohttp.test_utils import TestServer

from adminbot.amo.client import AmoClient


@pytest.fixture
async def amo_write():
    """Клиент в БОЕВОМ режиме: намерения исполняются."""
    async for pair in _make_client(dry_run=False):
        yield pair


@pytest.fixture
async def amo_dry():
    """Клиент в режиме репетиции: намерения только описываются."""
    async for pair in _make_client(dry_run=True):
        yield pair


async def _make_client(dry_run: bool):
    fake = FakeAmo()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    client = AmoClient(base_url=str(server.make_url("")).rstrip("/"), token="t",
                       max_attempts=1, sleep=_no_sleep, dry_run=dry_run)
    try:
        yield client, fake
    finally:
        await client.close()
        await server.close()


# --- заполнение сделки ---

async def test_update_lead_sends_price_and_custom_fields(amo_write):
    client, fake = amo_write
    fake.stub("/api/v4/leads/500", {"id": 500})

    intent = await client.update_lead(
        500,
        price=Decimal("5950"),
        custom_fields=[text_field(ids.FIELD_ADDRESS, "ул. Ленина, 5"),
                       enum_field(ids.FIELD_SERVICE, 933165)],
    )

    record = fake.requests[0]
    assert record.method == "PATCH" and record.path == "/api/v4/leads/500"
    assert record.body["price"] == 5950            # бюджет = сумма чека, целое число
    assert record.body["custom_fields_values"] == [
        {"field_id": ids.FIELD_ADDRESS, "values": [{"value": "ул. Ленина, 5"}]},
        {"field_id": ids.FIELD_SERVICE, "values": [{"enum_id": 933165}]},
    ]
    assert intent.performed is True and intent.entity_id == 500


async def test_update_lead_without_changes_does_nothing(amo_write):
    client, fake = amo_write
    intent = await client.update_lead(500)
    assert intent is None and not fake.requests


# --- смена этапа ---

async def test_move_lead_sends_pipeline_and_status(amo_write):
    client, fake = amo_write
    fake.stub("/api/v4/leads/500", {"id": 500})

    await client.move_lead(500, pipeline_id=ids.PIPELINE_REALIZATION,
                           status_id=ids.STATUS_SUCCESS)

    record = fake.requests[0]
    assert record.method == "PATCH"
    assert record.body == {"pipeline_id": ids.PIPELINE_REALIZATION,
                           "status_id": ids.STATUS_SUCCESS}


# --- создание сделки с привязкой контакта ---

async def test_create_lead_links_contact_and_returns_new_id(amo_write):
    client, fake = amo_write
    fake.stub("/api/v4/leads", {"_embedded": {"leads": [{"id": 777}]}})

    intent = await client.create_lead(
        name="Заказ №596",
        pipeline_id=ids.PIPELINE_PRIMARY,
        status_id=ids.PRIM_STAGE_NEW_LEAD,
        price=Decimal("5950"),
        contact_id=111,
        custom_fields=[datetime_field(ids.FIELD_ORDER_DATETIME,
                                      datetime(2026, 8, 24, 15, 40, tzinfo=MOSCOW_TZ))],
    )

    record = fake.requests[0]
    assert record.method == "POST" and record.path == "/api/v4/leads"
    body = record.body[0]                      # амо принимает список сущностей
    assert body["name"] == "Заказ №596"
    assert body["pipeline_id"] == ids.PIPELINE_PRIMARY
    assert body["status_id"] == ids.PRIM_STAGE_NEW_LEAD
    assert body["price"] == 5950
    assert body["_embedded"]["contacts"] == [{"id": 111}]      # контакт привязан сразу
    assert body["custom_fields_values"][0]["field_id"] == ids.FIELD_ORDER_DATETIME
    assert intent.entity_id == 777             # id новой сделки вернулся наружу


async def test_create_lead_without_contact(amo_write):
    client, fake = amo_write
    fake.stub("/api/v4/leads", {"_embedded": {"leads": [{"id": 778}]}})

    await client.create_lead(name="Заказ №1", pipeline_id=ids.PIPELINE_PRIMARY,
                             status_id=ids.PRIM_STAGE_NEW_LEAD, price=Decimal("1000"))

    assert "_embedded" not in fake.requests[0].body[0]


async def test_create_lead_with_tag_sends_it_next_to_contact(amo_write):
    """Тег «Отклик на промо» — в `_embedded.tags` рядом с контактом (ТЗ 2026-09-23)."""
    client, fake = amo_write
    fake.stub("/api/v4/leads", {"_embedded": {"leads": [{"id": 779}]}})

    intent = await client.create_lead(name="Отклик на промо — Ирина",
                                      pipeline_id=ids.PIPELINE_PRIMARY,
                                      status_id=ids.PRIM_STAGE_NEW_LEAD,
                                      contact_id=111, tags=["Отклик на промо"])

    assert fake.requests[0].body == [{
        "name": "Отклик на промо — Ирина",
        "pipeline_id": ids.PIPELINE_PRIMARY,
        "status_id": ids.PRIM_STAGE_NEW_LEAD,
        "_embedded": {"contacts": [{"id": 111}], "tags": [{"name": "Отклик на промо"}]},
    }]
    assert intent.entity_id == 779


async def test_create_lead_rehearsal_with_tag_sends_nothing(amo_dry):
    client, fake = amo_dry

    intent = await client.create_lead(name="Отклик на промо — Ирина",
                                      pipeline_id=ids.PIPELINE_PRIMARY,
                                      status_id=ids.PRIM_STAGE_NEW_LEAD,
                                      contact_id=111, tags=["Отклик на промо"])

    assert fake.requests == []
    assert intent.performed is False
    assert intent.payload[0]["_embedded"]["tags"] == [{"name": "Отклик на промо"}]


# --- создание контакта ---

async def test_create_contact_sends_phone_in_standard_field(amo_write):
    client, fake = amo_write
    fake.stub("/api/v4/contacts", {"_embedded": {"contacts": [{"id": 999}]}})

    intent = await client.create_contact(name="Ирина", phone="+79601861067")

    body = fake.requests[0].body[0]
    assert body["name"] == "Ирина"
    assert body["custom_fields_values"] == [
        {"field_code": "PHONE", "values": [{"value": "+79601861067", "enum_code": "WORK"}]}
    ]
    assert intent.entity_id == 999


# --- закрытие автозадач ---

async def test_complete_task_marks_done_with_result(amo_write):
    client, fake = amo_write
    fake.stub("/api/v4/tasks/321", {"id": 321})

    await client.complete_task(321)

    record = fake.requests[0]
    assert record.method == "PATCH" and record.path == "/api/v4/tasks/321"
    assert record.body["is_completed"] is True
    assert "amo_sync" in record.body["result"]["text"]      # видно, что закрыл робот


# --- комментарий к сделке (лид-дубль) ---

async def test_add_note_writes_comment(amo_write):
    client, fake = amo_write
    fake.stub("/api/v4/leads/500/notes", {"_embedded": {"notes": [{"id": 42}]}})

    await client.add_note(500, "Похоже на дубль обращения — заказ проведён в сделке #501")

    record = fake.requests[0]
    assert record.method == "POST" and record.path == "/api/v4/leads/500/notes"
    body = record.body[0]
    assert body["note_type"] == "common"
    assert "дубль" in body["params"]["text"]


# --- РЕПЕТИЦИЯ: в сеть не уходит ничего ---

@pytest.mark.parametrize("call", ["update", "move", "create_lead", "create_contact",
                                  "complete_task", "note"])
async def test_dry_run_sends_nothing(amo_dry, call):
    client, fake = amo_dry

    if call == "update":
        intent = await client.update_lead(500, price=Decimal("5950"))
    elif call == "move":
        intent = await client.move_lead(500, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS)
    elif call == "create_lead":
        intent = await client.create_lead(name="Заказ", pipeline_id=ids.PIPELINE_PRIMARY,
                                          status_id=ids.PRIM_STAGE_NEW_LEAD,
                                          price=Decimal("100"), contact_id=1)
    elif call == "create_contact":
        intent = await client.create_contact(name="Ирина", phone="+79601861067")
    elif call == "complete_task":
        intent = await client.complete_task(321)
    else:
        intent = await client.add_note(500, "текст")

    assert not fake.requests                     # ни одного обращения к амо
    assert intent.performed is False             # намерение описано, но не исполнено
    assert intent.payload                        # и его видно целиком


async def test_dry_run_intent_describes_the_same_body(amo_dry, amo_write):
    """Репетиция показывает ровно то, что уйдёт в бою, а не «примерно то же»."""
    dry_client, dry_fake = amo_dry
    live_client, live_fake = amo_write
    live_fake.stub("/api/v4/leads/500", {"id": 500})

    dry_intent = await dry_client.update_lead(500, price=Decimal("5950"))
    await live_client.update_lead(500, price=Decimal("5950"))

    assert dry_intent.payload == live_fake.requests[0].body


# --- сборка значений полей ---

def test_field_builders():
    assert text_field(18639, "Адрес") == {"field_id": 18639, "values": [{"value": "Адрес"}]}
    assert enum_field(271915, 933165) == {"field_id": 271915, "values": [{"enum_id": 933165}]}

    moment = datetime(2026, 8, 24, 15, 40, tzinfo=MOSCOW_TZ)
    built = datetime_field(18701, moment)
    assert built["field_id"] == 18701
    assert built["values"][0]["value"] == int(moment.timestamp())    # амо ждёт unix-время


def test_datetime_field_assumes_moscow_for_naive_time():
    naive = datetime(2026, 8, 24, 15, 40)
    aware = datetime(2026, 8, 24, 15, 40, tzinfo=MOSCOW_TZ)
    assert datetime_field(18701, naive) == datetime_field(18701, aware)
