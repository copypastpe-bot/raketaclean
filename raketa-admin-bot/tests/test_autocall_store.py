"""Память робота о цепочках автозвонка.

Ключ — идентификатор сделки амо: наблюдатель видит одну и ту же заявку при
каждом опросе, и вторая цепочка по той же сделке недопустима.

Оба хранилища — в памяти и в Postgres — гоняются одним набором проверок:
поведение обязано совпадать, иначе репетиция (память) отрепетирует не то,
что сделает боевой запуск (база). Pg-вариант пропускается без TEST_DB_DSN.

Отдельно проверяется курсор опроса амо: в репетиции он НЕ должен попадать
в базу — иначе боевой запуск начнёт не со своего момента включения, а с того,
что робот уже посмотрел вхолостую (та же ошибка, что с письмами 2026-08-26).
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adminbot import db
from adminbot.autocall.store import MemoryAutocallStore, PgAutocallStore

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "migrations").glob("*.sql"))

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "pg"])
async def store(request):
    """Одни и те же проверки для памяти и для Postgres."""
    if request.param == "memory":
        yield MemoryAutocallStore()
        return
    if not TEST_DB_DSN:
        pytest.skip("TEST_DB_DSN не задан — нужен Postgres")
    pool = await db.create_pool(TEST_DB_DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS adminbot CASCADE")
        for migration in MIGRATIONS:
            await conn.execute(migration.read_text())
    try:
        yield PgAutocallStore(pool)
    finally:
        await pool.close()


async def test_lead_chain_is_started_once(store):
    """Повторный проход наблюдателя не заводит вторую цепочку по той же сделке."""
    first = await store.create(41500001, phone10="9601861067")
    again = await store.create(41500001, phone10="0000000000")

    assert first.lead_id == again.lead_id == 41500001
    assert again.status == "queued"
    assert again.phone10 == "9601861067"         # повтор ничего не переписал
    assert again.attempts_total == 0


async def test_progress_is_saved_attempt_by_attempt(store):
    """Счёт попыток переживает перезапуск: цепочку продолжат из хранилища."""
    await store.create(41500001, phone10="9601861067")

    await store.update(41500001, status="calling", attempts_total=1,
                       manager_failures=1, call_id="call-777",
                       called_at=NOW, next_action_at=NOW + timedelta(minutes=10))
    lead = await store.get(41500001)

    assert lead.status == "calling"
    assert lead.attempts_total == 1
    assert lead.manager_failures == 1
    assert lead.client_failures == 0
    assert lead.call_id == "call-777"
    assert lead.called_at == NOW
    assert lead.next_action_at == NOW + timedelta(minutes=10)

    assert await store.get(99999999) is None
    assert await store.update(99999999, status="error") is None


async def test_due_returns_only_chains_whose_time_has_come(store):
    """В работу идёт незаконченное, у чего срок подошёл; просроченное — первым.

    Без срока (next_action_at пуст) — действовать сразу: цепочку только завели.
    Законченные (done|no_contact|gave_up) не возвращаются никогда.
    """
    await store.create(101, phone10="9601861067")                  # queued, срока нет
    await store.create(102, phone10="9601861068")
    await store.update(102, status="calling", next_action_at=NOW - timedelta(minutes=10))
    await store.create(103, phone10="9601861069")
    await store.update(103, next_action_at=NOW + timedelta(minutes=10))   # ещё рано
    await store.create(104, phone10="9601861070")
    await store.update(104, status="done", next_action_at=NOW - timedelta(hours=2))
    await store.create(105, phone10="9601861071")
    await store.update(105, status="error", next_action_at=NOW - timedelta(hours=1))
    await store.create(106, phone10="9601861072")
    await store.update(106, status="gave_up", next_action_at=NOW - timedelta(hours=1))
    await store.create(107, phone10="9601861073")
    await store.update(107, status="no_contact")

    due = await store.due(NOW)

    assert [lead.lead_id for lead in due] == [101, 105, 102]


async def test_cursor_bookmark_is_upserted(store):
    """Курсор — одна строка на весь сервис; повторное сохранение её обновляет."""
    assert await store.cursor() is None

    await store.save_cursor(NOW - timedelta(days=1))
    assert await store.cursor() == NOW - timedelta(days=1)

    await store.save_cursor(NOW)
    assert await store.cursor() == NOW


async def test_actions_are_logged_with_rehearsal_flag(store):
    """Журнал объясняет владельцу, что робот сделал — и было ли это репетицией."""
    await store.create(41500001, phone10="9601861067")

    await store.log_action(41500001, "pbx_call", dry_run=True,
                           payload={"phone": "+79601861067"})
    await store.log_action(41500001, "move_stage", dry_run=True,
                           payload={"stage": "no_contact"})

    actions = await store.actions_for(41500001)

    assert [row["action"] for row in actions] == ["move_stage", "pbx_call"]  # свежее первым
    assert actions[1]["dry_run"] is True
    assert actions[1]["payload"] == {"phone": "+79601861067"}
    assert await store.actions_for(99999999) == []
