"""Postgres для цикла «Повторный заказ»: оценённые заказы и своё состояние.

Нужен Postgres: DSN в переменной TEST_DB_DSN. Без него тесты пропускаются
(как в test_db_schema.py). `public.orders` здесь — свой урезанный набросок
(только колонки оценки), как `test_promo_callback_store.py` заводит свою
копию `public.promo_callbacks`: общий `tests/fixtures/bot_schema_min.sql`
для этого не трогаем — он не входит в задачу и им пользуются другие тесты.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adminbot import db
from adminbot.feedback.models import (
    MODE_LIVE,
    MODE_REHEARSAL,
    STATUS_CONTACT_SET,
    STATUS_NEW,
    RatedOrder,
)
from adminbot.feedback.store import PgFeedbackSource, PgFeedbackStore

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DB_DSN, reason="TEST_DB_DSN не задан — нужен Postgres")

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "migrations").glob("*.sql"))
FEEDBACK_MIGRATION = ROOT / "migrations" / "019_feedback_tasks.sql"

REPLIED = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
async def pool():
    pool = await db.create_pool(TEST_DB_DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS adminbot CASCADE")
        for migration in MIGRATIONS:
            await conn.execute(migration.read_text())
        await conn.execute("DROP TABLE IF EXISTS public.orders")
        await conn.execute(
            """
            CREATE TABLE public.orders (
                id                 bigint PRIMARY KEY,
                rating_score       smallint,
                rating_comment     text,
                rating_replied_at  timestamptz
            )
            """
        )
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS public.orders")
        await pool.close()


async def _order(pool, order_id, *, score=None, comment=None, replied_at=None):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO public.orders (id, rating_score, rating_comment, rating_replied_at) "
            "VALUES ($1, $2, $3, $4)",
            order_id, score, comment, replied_at,
        )


async def _link(pool, order_id, *, status="done", real_lead_id=None, phone10="9601861067"):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO adminbot.amo_links (order_id, phone10, status, real_lead_id) "
            "VALUES ($1, $2, $3, $4)",
            order_id, phone10, status, real_lead_id,
        )


async def _cleaning_link(pool, order_id, *, status="done", real_lead_id=None,
                          phone10="9601861067"):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO adminbot.cleaning_links (order_id, phone10, status, real_lead_id) "
            "VALUES ($1, $2, $3, $4)",
            order_id, phone10, status, real_lead_id,
        )


async def test_source_rated_orders_filters_link_status_lead_and_rating(pool):
    # 601 — связка done с real_lead_id и полная оценка: единственный, кто должен попасть.
    await _link(pool, 601, status="done", real_lead_id=41000)
    await _order(pool, 601, score=5, comment="5 отлично", replied_at=REPLIED)

    # 602 — связка не done.
    await _link(pool, 602, status="in_progress", real_lead_id=41001)
    await _order(pool, 602, score=4, comment="4", replied_at=REPLIED)

    # 603 — связка done, но без real_lead_id (сделка реализации ещё не заведена).
    await _link(pool, 603, status="done", real_lead_id=None)
    await _order(pool, 603, score=3, comment="3", replied_at=REPLIED)

    # 604 — связка в порядке, но клиент ещё не оценил (score пуст).
    await _link(pool, 604, status="done", real_lead_id=41002)
    await _order(pool, 604, score=None, comment=None, replied_at=None)

    # 605 — связка в порядке, но нет времени ответа (оценка неполная).
    await _link(pool, 605, status="done", real_lead_id=41004)
    await _order(pool, 605, score=4, comment="4", replied_at=None)

    # 606 — это уборка (cleaning_links), а не химчистка; amo_links для неё нет.
    await _cleaning_link(pool, 606, status="done", real_lead_id=41005)
    await _order(pool, 606, score=5, comment="5", replied_at=REPLIED)

    source = PgFeedbackSource(bot_pool=pool, own_pool=pool)
    rated = await source.rated_orders()

    assert rated == [RatedOrder(order_id=601, lead_id=41000, score=5,
                                comment="5 отлично", replied_at=REPLIED)]


async def test_store_started_at_is_set_once_and_per_mode(pool):
    store = PgFeedbackStore(pool)
    assert await store.started_at(MODE_LIVE) is None

    first = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
    later = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
    await store.save_started_at(MODE_LIVE, first)
    await store.save_started_at(MODE_LIVE, later)              # второй вызов не двигает закладку

    assert await store.started_at(MODE_LIVE) == first
    assert await store.started_at(MODE_REHEARSAL) is None      # у режима своя закладка


async def test_store_register_and_states_are_per_mode(pool):
    store = PgFeedbackStore(pool)
    await store.register(MODE_LIVE, [601, 602, 603])
    await store.update(601, MODE_LIVE, status=STATUS_CONTACT_SET, contact_task_id=777,
                       note_added=True, attempts=1, last_error="сбой")
    await store.register(MODE_LIVE, [601, 602, 603])            # повтор — уже заведённые не трогаем
    await store.register(MODE_REHEARSAL, [602])

    live_states = await store.states(MODE_LIVE)
    assert set(live_states) == {601, 602, 603}
    first = live_states[601]
    assert (first.status, first.contact_task_id, first.note_added,
           first.attempts, first.last_error) == (STATUS_CONTACT_SET, 777, True, 1, "сбой")
    assert live_states[602].status == STATUS_NEW

    rehearsal_states = await store.states(MODE_REHEARSAL)
    assert set(rehearsal_states) == {602}                       # режимы не видят строк друг друга


async def test_store_update_rejects_unknown_field(pool):
    store = PgFeedbackStore(pool)
    await store.register(MODE_LIVE, [601])

    with pytest.raises(ValueError):
        await store.update(601, MODE_LIVE, created_at=None)


async def test_store_rejects_unknown_status(pool):
    """Опечатка в статусе (миграция 019, CHECK на adminbot.feedback_state.status)
    падает в базе, а не тихо остаётся неверным значением."""
    import asyncpg

    store = PgFeedbackStore(pool)
    await store.register(MODE_LIVE, [601])

    with pytest.raises(asyncpg.CheckViolationError):
        await store.update(601, MODE_LIVE, status="in_progress")


async def test_migration_019_applies_twice_without_error(pool):
    """Фикстура уже применила миграции один раз (петлёй по всем файлам) — второй раз здесь."""
    async with pool.acquire() as conn:
        await conn.execute(FEEDBACK_MIGRATION.read_text())
