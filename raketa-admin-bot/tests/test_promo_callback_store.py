"""Postgres для откликов на промо: заявки рабочего бота и своё состояние.

Нужен Postgres: DSN в переменной TEST_DB_DSN. Без него тесты пропускаются
(как в test_db_schema.py). Таблица `public.promo_callbacks` — договорённость с
рабочим ботом (ТЗ 2026-09-23, задача 2): создаётся здесь из его же миграции
`app/migrations/0015_promo_callbacks.sql` в корне монорепо, а не из копии, —
разойдётся договорённость, упадёт тест.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adminbot import db
from adminbot.promo_callback.store import PgPromoCallbackSource, PgPromoCallbackStore
from adminbot.promo_callback.sync import MODE_LIVE, MODE_REHEARSAL, PromoCallbackSync
from tests.fakes import FakeAmo

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DB_DSN, reason="TEST_DB_DSN не задан — нужен Postgres")

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "migrations").glob("*.sql"))
BOT_MIGRATION = ROOT.parent / "app" / "migrations" / "0015_promo_callbacks.sql"

CREATED = datetime(2026, 9, 23, 9, 5, tzinfo=timezone.utc)


@pytest.fixture
async def pool():
    pool = await db.create_pool(TEST_DB_DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS adminbot CASCADE")
        for migration in MIGRATIONS:
            await conn.execute(migration.read_text())
        await conn.execute("DROP TABLE IF EXISTS public.promo_callbacks")
        await conn.execute(BOT_MIGRATION.read_text())
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS public.promo_callbacks")
        await pool.close()


async def _insert(pool, *, source="client", phone="+79601861067", name="Ирина", text="1"):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO public.promo_callbacks
                (source, client_id, lead_id, phone, name, response_text, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
            """,
            source, 100 if source == "client" else None, 500 if source == "lead" else None,
            phone, name, text, CREATED)


# --- источник ---

async def test_source_reads_callbacks_after_id_in_order(pool):
    source = PgPromoCallbackSource(pool)
    assert await source.max_id() == 0

    first = await _insert(pool)
    second = await _insert(pool, source="lead", phone="89159496642", name=None, text=" 1. ")

    assert await source.max_id() == second
    rows = await source.after(first, 100)
    assert [row.id for row in rows] == [second]
    row = rows[0]
    assert (row.source, row.lead_id, row.client_id) == ("lead", 500, None)
    assert (row.phone, row.name, row.response_text) == ("89159496642", None, " 1. ")
    assert row.created_at == CREATED

    assert [row.id for row in await source.after(0, 1)] == [first]            # предел
    assert [row.id for row in await source.by_ids([second, first, 999])] == [first, second]
    assert await source.by_ids([]) == []


# --- своё состояние ---

async def test_store_bookmark_is_per_mode_and_only_moves_forward(pool):
    store = PgPromoCallbackStore(pool)
    assert await store.cursor(MODE_LIVE) is None

    await store.save_cursor(MODE_LIVE, 5)
    await store.save_cursor(MODE_LIVE, 3)                                     # назад не двигаем

    assert await store.cursor(MODE_LIVE) == 5
    assert await store.cursor(MODE_REHEARSAL) is None


async def test_store_register_is_idempotent_and_pending_keeps_open_rows(pool):
    store = PgPromoCallbackStore(pool)
    await store.register(MODE_LIVE, [1, 2, 3])
    await store.update(1, MODE_LIVE, status="lead_created", lead_id=777, contact_id=111,
                       attempts=1, last_error="сбой")
    await store.update(3, MODE_LIVE, status="queued", lead_id=778)
    await store.register(MODE_LIVE, [1, 2, 3])                                # повтор — без изменений
    await store.register(MODE_REHEARSAL, [2])

    pending = await store.pending(MODE_LIVE)

    assert [(s.callback_id, s.status) for s in pending] == [(1, "lead_created"), (2, "new")]
    first = pending[0]
    assert (first.lead_id, first.contact_id, first.attempts, first.last_error) == (
        777, 111, 1, "сбой")
    assert [s.callback_id for s in await store.pending(MODE_REHEARSAL)] == [2]


async def test_store_rejects_unknown_fields(pool):
    store = PgPromoCallbackStore(pool)
    await store.register(MODE_LIVE, [1])

    with pytest.raises(ValueError):
        await store.update(1, MODE_LIVE, created_at=None)


async def test_store_rejects_unknown_status(pool):
    """Опечатка в статусе падает в базе, а не прячет заявку навсегда."""
    import asyncpg

    store = PgPromoCallbackStore(pool)
    await store.register(MODE_LIVE, [1])

    with pytest.raises(asyncpg.CheckViolationError):
        await store.update(1, MODE_LIVE, status="done")


# --- цикл целиком на настоящей базе ---

async def test_cycle_end_to_end_on_postgres(pool):
    await _insert(pool)                                    # до включения — не наша
    source, store, amo = PgPromoCallbackSource(pool), PgPromoCallbackStore(pool), FakeAmo()
    sync = PromoCallbackSync(source=source, store=store, amo=amo, dry_run=False)

    await sync.tick()                                      # закладка
    assert amo.calls == []
    new_id = await _insert(pool, name="Юлия")
    amo.fail_on = "add_note"
    await sync.tick()                                      # сделка есть, примечание упало
    amo.fail_on = None
    await sync.tick()

    state = await store.get(new_id, MODE_LIVE)
    assert state.status == "queued" and state.lead_id is not None and state.attempts == 1
    assert len(amo.calls_of("create_lead")) == 1
    assert amo.calls_of("create_lead")[0]["name"] == "Отклик на промо — Юлия"
    assert await store.cursor(MODE_LIVE) == new_id
    assert await store.get(new_id - 1, MODE_LIVE) is None
