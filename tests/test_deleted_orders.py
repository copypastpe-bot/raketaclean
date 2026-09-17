"""Регистр удалённых заказов химчистки (bot.py, `_record_deleted_order`).

ТЗ 2026-09-17 «удаление заказа освобождает сделку в amoCRM», задача 2: обработчик
`order_remove_confirm` (bot.py) пишет в `deleted_orders` внутри той же транзакции,
что и `DELETE FROM orders` — если удаление откатится, регистр не должен соврать.
Урок ревью 17.09 (замечание 1 в tests/test_pending_order_reports.py): такие вещи
без настоящего Postgres не ловятся, поэтому здесь — живая база, как там.
DSN в TEST_DB_DSN, без него тесты пропускаются. Пример временной базы (тот же
сервер, что для adminbot_test и raketaclean_test):
    createdb -h 127.0.0.1 -p 5432 -U postgres raketaclean_test
    export TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/raketaclean_test
"""

import os
import unittest
from decimal import Decimal
from pathlib import Path

import asyncpg

import bot

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent / "app" / "migrations" / "0012_deleted_orders.sql"
).read_text(encoding="utf-8")


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class RecordDeletedOrderRealSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(dsn=TEST_DB_DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS deleted_orders")
            # Ровно та же миграция, что применяет владелец на проде (задача 1),
            # а не переписанная копия — так тест ловит и её собственные дефекты.
            await conn.execute(MIGRATION_SQL)

    async def asyncTearDown(self):
        async with self.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS deleted_orders")
        await self.pool.close()

    async def test_insert_lands_row_with_expected_fields(self):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await bot._record_deleted_order(
                    conn,
                    order_id=90101,
                    phone_digits="79161234567",
                    client_id=55,
                    amount_total=Decimal("1500.00"),
                    deleted_by=777,
                )

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT order_id, phone_digits, client_id, amount_total, deleted_by, deleted_at "
                "FROM deleted_orders WHERE order_id=$1",
                90101,
            )
        self.assertIsNotNone(row)
        self.assertEqual(row["order_id"], 90101)
        self.assertEqual(row["phone_digits"], "79161234567")
        self.assertEqual(row["client_id"], 55)
        self.assertEqual(row["amount_total"], Decimal("1500.00"))
        self.assertEqual(row["deleted_by"], 777)
        self.assertIsNotNone(row["deleted_at"])

    async def test_rollback_leaves_no_row(self):
        # Имитация отката удаления заказа: та же вставка внутри транзакции,
        # которая затем откатывается — регистр не должен соврать, что заказ
        # удалён, если DELETE FROM orders в реальности не применился.
        with self.assertRaises(RuntimeError):
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await bot._record_deleted_order(
                        conn,
                        order_id=90102,
                        phone_digits="79161234567",
                        client_id=55,
                        amount_total=Decimal("1500.00"),
                        deleted_by=777,
                    )
                    raise RuntimeError("имитация отката: удаление заказа не прошло")

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM deleted_orders WHERE order_id=$1", 90102
            )
        self.assertIsNone(row)

    async def test_insert_is_idempotent_by_order_id(self):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await bot._record_deleted_order(
                    conn,
                    order_id=90103,
                    phone_digits="79161234567",
                    client_id=55,
                    amount_total=Decimal("1500.00"),
                    deleted_by=777,
                )
            async with conn.transaction():
                await bot._record_deleted_order(
                    conn,
                    order_id=90103,
                    phone_digits="79160000000",  # повторный вызов не должен перезаписать
                    client_id=99,
                    amount_total=Decimal("1.00"),
                    deleted_by=1,
                )

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT phone_digits, client_id FROM deleted_orders WHERE order_id=$1", 90103
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["phone_digits"], "79161234567")
        self.assertEqual(rows[0]["client_id"], 55)

    async def test_missing_phone_and_client_are_stored_as_null(self):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await bot._record_deleted_order(
                    conn,
                    order_id=90104,
                    phone_digits=None,
                    client_id=None,
                    amount_total=None,
                    deleted_by=777,
                )

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT phone_digits, client_id, amount_total FROM deleted_orders WHERE order_id=$1",
                90104,
            )
        self.assertIsNone(row["phone_digits"])
        self.assertIsNone(row["client_id"])
        self.assertIsNone(row["amount_total"])


if __name__ == "__main__":
    unittest.main()
