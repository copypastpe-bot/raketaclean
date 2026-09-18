"""Клиент «положить событие» (`adminbot/notify_bus.py`, `put_event`).

ТЗ `docs/plans/2026-09-18-notifications-bus.md` (корень монорепо), задача 2:
одна вставка в `notify.outbox`, без похода в сеть; там, где событие рождается
внутри транзакции — в ней же; неудачная вставка не роняет вызывающий код.
Нужен настоящий Postgres (DSN в TEST_DB_DSN, как в test_deletions_source.py) —
без него тесты пропускаются:
    export TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/adminbot_test

Миграция берётся ровно та же, что применяет служба-почтальон (задача 1,
`raketa-notify/migrations/001_notify_schema.sql` в корне монорепо), а не
переписанная копия. Пул поднимается через `adminbot.db.create_pool`, а не
через голый `asyncpg.create_pool`: только так на соединении появляется
кодек jsonb↔dict, которым пользуется `put_event` этого бота (см. его
docstring) — без кодека передача словаря в reply_markup упала бы.

Схема `notify` не пересоздаётся с нуля (миграция идемпотентна): в этой же
ветке параллельно работает другой исполнитель над каталогом `raketa-notify/`,
и DROP SCHEMA здесь мог бы забрать данные у его собственной проверки. Поэтому
строки этого файла помечены префиксом `kind` и убираются точечно после теста.
"""

import os
from pathlib import Path

import pytest

from adminbot import db, notify_bus

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DB_DSN, reason="TEST_DB_DSN не задан — нужен Postgres")

MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent.parent
    / "raketa-notify" / "migrations" / "001_notify_schema.sql"
).read_text(encoding="utf-8")

_MARK = "test_notify_bus_"  # префикс kind для всех строк этого файла


@pytest.fixture
async def notify_pool():
    pool = await db.create_pool(TEST_DB_DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute(MIGRATION_SQL)
    yield pool
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM notify.outbox WHERE kind LIKE $1", f"{_MARK}%")
    await pool.close()


async def test_insert_lands_row_with_expected_fields(notify_pool):
    async with notify_pool.acquire() as conn:
        async with conn.transaction():
            event_id = await notify_bus.put_event(
                conn,
                kind=f"{_MARK}order_done",
                text="Сделка №123 закрыта",
                ref=123,
                reply_markup={"inline_keyboard": [[{"text": "ок", "callback_data": "ok"}]]},
            )
    assert event_id is not None

    async with notify_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT kind, text, reply_markup, ref, source, dry_run, status, expires_at "
            "FROM notify.outbox WHERE id=$1",
            event_id,
        )
    assert row is not None
    assert row["kind"] == f"{_MARK}order_done"
    assert row["text"] == "Сделка №123 закрыта"
    assert row["ref"] == "123"
    assert row["source"] == "adminbot"
    assert row["dry_run"] is False
    assert row["status"] == "pending"
    assert row["expires_at"] is not None
    # Кодек пула decoded jsonb в dict сам — без ручного json.loads.
    assert row["reply_markup"] == {"inline_keyboard": [[{"text": "ок", "callback_data": "ok"}]]}


async def test_rollback_leaves_no_row(notify_pool):
    kind = f"{_MARK}rollback"
    with pytest.raises(RuntimeError):
        async with notify_pool.acquire() as conn:
            async with conn.transaction():
                event_id = await notify_bus.put_event(conn, kind=kind, text="х")
                assert event_id is not None
                raise RuntimeError("имитация отката бизнес-операции")

    async with notify_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM notify.outbox WHERE kind=$1", kind)
    assert row is None


async def test_failed_insert_does_not_crash_or_abort_outer_transaction(notify_pool):
    good_kind_before = f"{_MARK}before"
    good_kind_after = f"{_MARK}after"
    async with notify_pool.acquire() as conn:
        async with conn.transaction():
            before_id = await notify_bus.put_event(conn, kind=good_kind_before, text="до")
            assert before_id is not None

            # kind=None ломает NOT NULL — но это не повод ронять транзакцию
            # вокруг: put_event гасит исключение сама.
            failed_id = await notify_bus.put_event(conn, kind=None, text="сломано")
            assert failed_id is None

            after_id = await notify_bus.put_event(conn, kind=good_kind_after, text="после")
            assert after_id is not None
        # Если бы savepoint внутри put_event не сработал, conn остался бы в
        # aborted-состоянии, и выход из внешней транзакции здесь упал бы.

    async with notify_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind FROM notify.outbox WHERE kind IN ($1, $2)",
            good_kind_before, good_kind_after,
        )
    assert {r["kind"] for r in rows} == {good_kind_before, good_kind_after}
