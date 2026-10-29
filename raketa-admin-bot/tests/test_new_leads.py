"""Чистка «Новых лидов»: что закрывается, а что нет.

Скрипт закрывает сделки пачкой в боевой CRM, цена ошибки несимметрична:
закрыть клиента, который заказал, хуже, чем оставить лишнюю строку. Поэтому
проверяется в первую очередь «не трогать»: есть запись в календаре — не
закрываем, нет телефона — не закрываем.
"""

import os
from datetime import date, datetime
from pathlib import Path

import pytest

from adminbot import db
from adminbot.amo import ids
from adminbot.amo.client import AmoClient
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.amo.new_leads import (
    CalendarMark, created_day_msk, fetch_calendar_marks, fetch_contact_phones,
    fetch_new_leads, find_loss_reason, plan_cleanup)


def a_lead(lead_id: int, created: date, *contact_ids: int) -> dict:
    stamp = int(datetime(created.year, created.month, created.day, 12,
                         tzinfo=MOSCOW_TZ).timestamp())
    return {"id": lead_id, "created_at": stamp,
            "_embedded": {"contacts": [{"id": cid} for cid in contact_ids]}}


# --- раскладка ---

def test_lead_without_calendar_record_is_closed():
    lead = a_lead(1, date(2026, 10, 10), 501)
    plan = plan_cleanup([lead], {501: ["9601861067"]}, {})
    assert plan.to_close == [lead]
    assert not plan.with_order and not plan.no_phone


def test_record_after_lead_keeps_it_open():
    lead = a_lead(1, date(2026, 10, 10), 501)
    plan = plan_cleanup([lead], {501: ["9601861067"]},
                        {"9601861067": [date(2026, 11, 2)]})
    assert plan.to_close == []
    assert plan.with_order == [(lead, CalendarMark("9601861067", date(2026, 11, 2)))]


def test_record_on_the_same_day_counts():
    lead = a_lead(1, date(2026, 10, 10), 501)
    plan = plan_cleanup([lead], {501: ["9601861067"]},
                        {"9601861067": [date(2026, 10, 10)]})
    assert plan.to_close == []


def test_order_before_the_lead_does_not_count():
    """Прошлый заказ клиента — про прошлую сделку, эту заявку он не закрывает."""
    lead = a_lead(1, date(2026, 10, 10), 501)
    plan = plan_cleanup([lead], {501: ["9601861067"]},
                        {"9601861067": [date(2026, 9, 1)]})
    assert plan.to_close == [lead]


def test_any_phone_of_any_contact_counts():
    lead = a_lead(1, date(2026, 10, 10), 501, 502)
    plan = plan_cleanup([lead], {501: ["9601861067"], 502: ["9161234567"]},
                        {"9161234567": [date(2026, 10, 20)]})
    assert plan.with_order[0][1].phone10 == "9161234567"


def test_no_phone_is_not_closed():
    no_contacts = a_lead(1, date(2026, 10, 10))
    contact_without_phone = a_lead(2, date(2026, 10, 10), 501)
    plan = plan_cleanup([no_contacts, contact_without_phone], {501: []}, {})
    assert plan.to_close == []
    assert plan.no_phone == [no_contacts, contact_without_phone]


def test_unknown_creation_day_any_record_stops():
    lead = {"id": 1, "_embedded": {"contacts": [{"id": 501}]}}
    plan = plan_cleanup([lead], {501: ["9601861067"]},
                        {"9601861067": [date(2020, 1, 1)]})
    assert plan.to_close == []


def test_creation_day_is_moscow():
    """23:30 по Москве 10.10 — это уже 10.10, хотя в UTC ещё 10.10 20:30."""
    stamp = int(datetime(2026, 10, 10, 23, 30, tzinfo=MOSCOW_TZ).timestamp())
    assert created_day_msk({"created_at": stamp}) == date(2026, 10, 10)
    stamp = int(datetime(2026, 10, 11, 1, 30, tzinfo=MOSCOW_TZ).timestamp())
    assert created_day_msk({"created_at": stamp}) == date(2026, 10, 11)


# --- причина отказа ---

def test_loss_reason_found_by_name_ignoring_case():
    reasons = [{"id": 7, "name": "Дорого"}, {"id": 9, "name": " Пропала потребность "}]
    assert find_loss_reason(reasons, "пропала потребность") == 9


def test_loss_reason_missing():
    assert find_loss_reason([{"id": 7, "name": "Дорого"}], "Пропала потребность") is None


async def test_move_lead_carries_loss_reason():
    amo = AmoClient(base_url="https://x.amocrm.ru", token="t", dry_run=True)
    intent = await amo.move_lead(1, ids.PIPELINE_PRIMARY, ids.STATUS_CLOSED,
                                 loss_reason_id=9)
    assert intent.payload == {"pipeline_id": ids.PIPELINE_PRIMARY,
                              "status_id": ids.STATUS_CLOSED, "loss_reason_id": 9}
    plain = await amo.move_lead(1, ids.PIPELINE_PRIMARY, ids.STATUS_CLOSED)
    assert "loss_reason_id" not in plain.payload
    await amo.close()


# --- чтение из амо ---

class FakeLists:
    def __init__(self, items):
        self.items = items
        self.calls = []

    async def get_all(self, path, key, params=None):
        self.calls.append((path, key, list(params or [])))
        return self.items(path, params) if callable(self.items) else self.items


async def test_new_leads_asked_by_stage_with_contacts():
    amo = FakeLists([])
    await fetch_new_leads(amo)
    path, key, params = amo.calls[0]
    assert (path, key) == ("/api/v4/leads", "leads")
    assert ("filter[statuses][0][status_id]", ids.PRIM_STAGE_NEW_LEAD) in params
    assert ("filter[statuses][0][pipeline_id]", ids.PIPELINE_PRIMARY) in params
    assert ("with", "contacts") in params


async def test_contact_phones_in_batches():
    def contacts(path, params):
        return [{"id": value, "custom_fields_values": [
            {"field_code": "PHONE", "values": [{"value": f"+7 960 186-{value:04d}"}]}]}
            for name, value in params if name == "filter[id][]"]

    amo = FakeLists(contacts)
    phones = await fetch_contact_phones(amo, list(range(1, 121)) + [5])
    assert len(amo.calls) == 3                      # 120 контактов по 50
    assert phones[5] == ["960186" + "0005"]
    assert len(phones) == 120


# --- память календаря (нужен Postgres) ---

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATIONS = sorted((Path(__file__).resolve().parent.parent / "migrations").glob("*.sql"))


@pytest.fixture
async def pool():
    if not TEST_DB_DSN:
        pytest.skip("TEST_DB_DSN не задан — нужен Postgres")
    pool = await db.create_pool(TEST_DB_DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS adminbot CASCADE")
        for migration in MIGRATIONS:
            await conn.execute(migration.read_text())
        await conn.execute(
            """
            INSERT INTO adminbot.gcal_events
                (event_id, kind, order_date, phone10, event_data, status, created_at) VALUES
              ('order',     'order',     '2026-10-20', '9601861067', NULL,        'done',      '2026-10-15 10:00+03'),
              ('second',    'order',     '2026-10-25', '9001112233',
                   '{"phones": ["9001112233", "9161234567"]}',                    'in_progress', '2026-10-16 10:00+03'),
              ('deleted',   'order',     '2026-10-21', '9602220000', NULL,        'cancelled', '2026-10-15 10:00+03'),
              ('day_off',   'block',     '2026-10-22', '9603330000', NULL,        'skipped',   '2026-10-15 10:00+03'),
              ('not_firm',  'unsettled', NULL,         '9604440000', NULL,        'waiting_owner', '2026-10-17 23:30+03'),
              ('oldest',    'order',     '2026-08-27', '9605550000', NULL,        'done',      '2026-08-27 09:00+03')
            """)
    yield pool
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS adminbot CASCADE")
    await pool.close()


async def test_calendar_marks(pool):
    marks, memory_from = await fetch_calendar_marks(pool, [
        "9601861067", "9161234567", "9602220000", "9603330000", "9604440000",
        "9999999999"])
    assert marks == {
        "9601861067": [date(2026, 10, 20)],
        "9161234567": [date(2026, 10, 25)],          # второй телефон записи
        "9604440000": [date(2026, 10, 17)],          # «⁉️» без даты — день записи по Москве
    }
    assert memory_from == date(2026, 8, 27)


async def test_calendar_marks_without_phones(pool):
    marks, memory_from = await fetch_calendar_marks(pool, [])
    assert marks == {}
    assert memory_from == date(2026, 8, 27)
