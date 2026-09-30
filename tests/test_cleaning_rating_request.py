"""Уборка: поля оценки и отметка «попросили оценить» (cleaning/handlers.py,
`_enqueue_cleaning_completed_notifications`).

ТЗ docs/plans/2026-09-30-cleaning-ratings.md, задача 1 (2026-09-30).

У уборок появляются те же четыре поля оценки, что у заказов химчистки
(`orders.rating_*`, bot.py `ensure_orders_rating_schema`): миграция 0016 и
зеркало при запуске `cleaning/schema.py` `ensure_cleaning_schema`. Когда клиенту
ставится просьба оценить уборку, у уборки ставится `rating_requested_at`, а
прошлый ответ стирается — как у химчистки (bot.py
`_enqueue_order_completed_notification`). Без правил уведомлений письма не
ставятся, и отметки нет.

На настоящем Postgres (DSN в `TEST_DB_DSN`, без него класс пропускается): своя
схема с минимальной `clients`, таблицы уборок — ровно теми файлами миграций, что
уходят на прод (0006, 0010, 0016). Постановка писем подменена: очередь писем
здесь не проверяется.
"""

import os
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import asyncpg

import cleaning.handlers as cleaning_handlers
from cleaning.schema import ensure_cleaning_schema

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "app" / "migrations"
SCHEMA = "cleaning_rating_request_test"

RATING_COLUMNS = {
    "rating_score": "smallint",
    "rating_comment": "text",
    "rating_requested_at": "timestamp with time zone",
    "rating_replied_at": "timestamp with time zone",
}


def _migration(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class CleaningRatingRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            await admin.execute(f"CREATE SCHEMA {SCHEMA}")
            await admin.execute(f"SET search_path TO {SCHEMA}")
            # Только то, на что ссылаются таблицы уборок, — не копия прода.
            await admin.execute("CREATE TABLE clients (id serial PRIMARY KEY)")
            # Таблицы уборок до этой задачи — теми же файлами, что на проде.
            await admin.execute(_migration("0006_cleaning.sql"))
            await admin.execute(_migration("0010_cleaning_orders_comment.sql"))
        finally:
            await admin.close()
        self.conn = await asyncpg.connect(
            TEST_DB_DSN, server_settings={"search_path": SCHEMA}
        )
        self.enqueue = mock.AsyncMock(return_value=1)
        self.patch = mock.patch.object(cleaning_handlers, "enqueue_notification", self.enqueue)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.conn.close()
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        finally:
            await admin.close()

    # --- помощники ---

    async def _rating_column_types(self) -> dict[str, str]:
        rows = await self.conn.fetch(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = 'cleaning_orders'
              AND column_name LIKE 'rating\\_%'
            """,
            SCHEMA,
        )
        return {r["column_name"]: r["data_type"] for r in rows}

    async def _order(self) -> int:
        client_id = await self.conn.fetchval("INSERT INTO clients DEFAULT VALUES RETURNING id")
        foreman_id = await self.conn.fetchval(
            "INSERT INTO cleaning_foremen (fn) VALUES ('Ольга') RETURNING id"
        )
        return await self.conn.fetchval(
            """
            INSERT INTO cleaning_orders (client_id, foreman_id, total_amount)
            VALUES ($1, $2, 3500) RETURNING id
            """,
            client_id,
            foreman_id,
        )

    async def _enqueue(self, order_id: int, rules) -> None:
        client_id = await self.conn.fetchval(
            "SELECT client_id FROM cleaning_orders WHERE id = $1", order_id
        )
        await cleaning_handlers._enqueue_cleaning_completed_notifications(
            self.conn,
            rules,
            order_id=order_id,
            client_id=client_id,
            total=Decimal("3500"),
            bonuses_used=0,
            bonuses_earned=175,
            bonus_balance=175,
            amount_due=Decimal("3500"),
            bonus_expires_at=None,
            is_wire_payment=False,
        )

    # --- поля ---

    async def test_migration_adds_rating_fields_with_orders_types(self):
        await self.conn.execute(_migration("0016_cleaning_orders_rating.sql"))
        # Повторный прогон на выкате не падает.
        await self.conn.execute(_migration("0016_cleaning_orders_rating.sql"))
        self.assertEqual(await self._rating_column_types(), RATING_COLUMNS)

    async def test_startup_mirror_adds_rating_fields_to_existing_table(self):
        await ensure_cleaning_schema(self.conn)
        self.assertEqual(await self._rating_column_types(), RATING_COLUMNS)

    # --- отметка «попросили» ---

    async def test_rating_request_marks_order_and_clears_old_reply(self):
        await self.conn.execute(_migration("0016_cleaning_orders_rating.sql"))
        order_id = await self._order()
        other_id = await self._order()
        await self.conn.execute(
            """
            UPDATE cleaning_orders
            SET rating_score = 3, rating_comment = 'старое', rating_replied_at = NOW()
            WHERE id = $1
            """,
            order_id,
        )
        before = await self.conn.fetchval("SELECT clock_timestamp()")

        await self._enqueue(order_id, mock.sentinel.rules)

        row = await self.conn.fetchrow(
            """
            SELECT rating_score, rating_comment, rating_requested_at, rating_replied_at
            FROM cleaning_orders WHERE id = $1
            """,
            order_id,
        )
        self.assertIsNotNone(row["rating_requested_at"])
        self.assertGreaterEqual(row["rating_requested_at"], before)
        self.assertIsNone(row["rating_score"])
        self.assertIsNone(row["rating_comment"])
        self.assertIsNone(row["rating_replied_at"])
        # Соседняя уборка не задета.
        self.assertIsNone(
            await self.conn.fetchval(
                "SELECT rating_requested_at FROM cleaning_orders WHERE id = $1", other_id
            )
        )
        # Просьба оценить поставлена в очередь, как и раньше.
        event_keys = [c.kwargs["event_key"] for c in self.enqueue.await_args_list]
        self.assertIn("cleaning_order_rating_reminder", event_keys)

    async def test_no_rules_no_mark(self):
        await self.conn.execute(_migration("0016_cleaning_orders_rating.sql"))
        order_id = await self._order()

        await self._enqueue(order_id, None)

        self.assertIsNone(
            await self.conn.fetchval(
                "SELECT rating_requested_at FROM cleaning_orders WHERE id = $1", order_id
            )
        )
        self.enqueue.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
