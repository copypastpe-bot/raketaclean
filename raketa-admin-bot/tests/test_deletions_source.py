"""Источник неразобранных удалений (ТЗ 2026-09-17 «удаление заказа освобождает
сделку», задача 4).

Нужен Postgres: DSN в переменной TEST_DB_DSN. Без него тесты пропускаются
(как в test_db_schema.py). Таблица `public.deleted_orders` — регистр рабочего
бота (задача 1 того же ТЗ, миграция `app/migrations/0012` в корне монорепо) —
своей миграции в этом проекте не имеет: её создаёт другой исполнитель, и в
этой рабочей копии её может ещё не быть вовсе. Поэтому схема таблицы здесь не
берётся из чужой миграции, а воспроизведена в `tests/fixtures/bot_schema_min.sql`
(как и остальные таблицы `public` — та же урезанная копия «только для тестов»).

Дополнительно тест проверяет факт 3 ТЗ (обе схемы — `public` и `adminbot` —
живут в одной базе и JOIN между ними в одном запросе допустим): запросы ниже
читают `public.deleted_orders` / `public.cleaning_orders` и
`adminbot.order_deletions_seen` одним обращением к `own_pool`, без выгрузки
данных в Python. Если бы схемы были разделены (разные роли или базы), эти
запросы упали бы с ошибкой доступа, а не молча вернули пустоту.
"""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from adminbot import db
from adminbot.sync import deletions

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DB_DSN, reason="TEST_DB_DSN не задан — нужен Postgres")

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "migrations").glob("*.sql"))
BOT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bot_schema_min.sql"

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def pool():
    """Чистая тестовая база: схема adminbot (все миграции) + урезанные таблицы
    рабочего бота, включая `public.deleted_orders`, с тестовыми данными.

    Номера заказа и уборки намеренно совпадают (10) — это ровно та ситуация,
    из-за которой в `pending_order_reports` рабочего бота (миграция 0011)
    понадобился составной ключ (kind, order_id): голый order_id не различил бы
    удалённый заказ №10 и удалённую уборку №10.
    """
    pool = await db.create_pool(TEST_DB_DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS adminbot CASCADE")
        for migration in MIGRATIONS:
            await conn.execute(migration.read_text())
        await conn.execute(BOT_FIXTURE.read_text())

        # --- заказы химчистки, удалённые физически (регистр рабочего бота) ---
        await conn.execute(
            """
            INSERT INTO public.deleted_orders
                (order_id, phone_digits, client_id, amount_total, deleted_at, deleted_by) VALUES
                (10, '79601861067', 100, 5950.00, $1, 555),
                (11, '79159496642', 101, 3300.00, $2, 555)
            """,
            NOW - timedelta(hours=1), NOW - timedelta(hours=3),
        )

        # --- уборки, помечены deleted_at; №10 намеренно совпадает с заказом №10 ---
        await conn.execute(
            """
            INSERT INTO public.cleaning_orders
                (id, client_id, foreman_id, address, total_amount, happened_at, deleted_at) VALUES
                (10, 102, 1, 'Гагарина 1, кв 5',  9000.00, $1, $2),
                (20, 102, 1, 'Гагарина 1, кв 5',  4000.00, $1, NULL),
                (21, 101, 1, 'пр. Гагарина, 12',  2500.00, $1, $3)
            """,
            NOW - timedelta(days=1), NOW - timedelta(hours=2), NOW - timedelta(hours=4),
        )

        # --- отметки о разборе: заказ №11 и уборка №21 уже разобраны ---
        await conn.execute(
            """
            INSERT INTO adminbot.order_deletions_seen (kind, order_id, outcome) VALUES
                ('order', 11, 'сделки нет, связку убрал'),
                ('cleaning', 21, 'связка не найдена')
            """
        )
    try:
        yield pool
    finally:
        await pool.close()


async def test_fetch_pending_order_deletions_skips_seen(pool):
    rows = await db.fetch_pending_order_deletions(pool)

    assert [r.order_id for r in rows] == [10]          # №11 уже отмечен разобранным
    row = rows[0]
    assert row.kind == "order"
    assert row.phone_digits == "79601861067"
    assert row.client_id == 100
    assert row.amount_total == Decimal("5950.00")
    assert row.deleted_at == NOW - timedelta(hours=1)


async def test_fetch_pending_cleaning_deletions_skips_seen_and_live(pool):
    rows = await db.fetch_pending_cleaning_deletions(pool)

    # №20 не удалена (deleted_at NULL) — не попадает; №21 уже отмечена разобранной
    assert [r.order_id for r in rows] == [10]
    row = rows[0]
    assert row.kind == "cleaning"
    assert row.amount_total == Decimal("9000.00")
    assert row.phone_digits is None                    # у уборки в источнике телефона нет


async def test_overlapping_order_ids_are_kept_apart(pool):
    """Заказ №10 (химчистка) и уборка №10 — разные записи, обе должны выйти."""
    order_rows = await db.fetch_pending_order_deletions(pool)
    cleaning_rows = await db.fetch_pending_cleaning_deletions(pool)

    assert {r.kind for r in order_rows} == {"order"}
    assert {r.kind for r in cleaning_rows} == {"cleaning"}
    assert [r.order_id for r in order_rows] == [10]
    assert [r.order_id for r in cleaning_rows] == [10]


async def test_marking_one_kind_seen_does_not_hide_the_other(pool):
    """Отметка (kind='cleaning', order_id=21) не должна прятать заказ №21, если
    бы он тоже был удалён — составной ключ, а не голый order_id."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO public.deleted_orders (order_id, deleted_at) VALUES (21, $1)",
            NOW,
        )

    rows = await db.fetch_pending_order_deletions(pool)
    assert 21 in [r.order_id for r in rows]             # уборка №21 разобрана, заказ №21 — нет


async def test_fetch_pending_deletions_merges_both_kinds_oldest_first(pool):
    records = await deletions.fetch_pending_deletions(pool)

    assert [(r.kind, r.order_id) for r in records] == [("cleaning", 10), ("order", 10)]

    deleted_ats = [r.deleted_at for r in records]
    assert deleted_ats == sorted(deleted_ats)           # старые удаления первыми
