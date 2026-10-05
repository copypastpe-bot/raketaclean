"""Удаление привязанной оплаты по счёту откатывает заказ (bot.py, `_unlink_wire_entry`).

Решение владельца 2026-10-05: админ ввёл не ту сумму при привязке перевода,
удалил транзакцию — а заказ остался «оплаченным» с чужой суммой и зарплатой,
и привязать правильную оплату было нельзя, только удалить заказ. Теперь
удаление транзакции возвращает заказ в «ждёт оплату по счёту» с прежними
значениями: сумма 1 ₽, база и доплата мастера 0, бензин остаётся.

Живая база, как в tests/test_deleted_orders.py: DSN в TEST_DB_DSN, без него
тесты пропускаются. Таблицы — урезанные копии боевых в своей схеме, чтобы
не задеть соседние тесты той же базы.
"""

import os
import unittest
from decimal import Decimal

import asyncpg

import bot

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
SCHEMA = "wire_unlink_test"


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class UnlinkWireEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin = await asyncpg.connect(dsn=TEST_DB_DSN)
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await admin.execute(f"CREATE SCHEMA {SCHEMA}")
        await admin.close()
        self.pool = await asyncpg.create_pool(
            dsn=TEST_DB_DSN, min_size=1, max_size=2,
            server_settings={"search_path": SCHEMA})
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE orders (
                    id integer PRIMARY KEY,
                    payment_method text,
                    amount_total numeric(12,2),
                    amount_cash numeric(12,2),
                    awaiting_wire_payment boolean NOT NULL DEFAULT false
                );
                CREATE TABLE cashbook_entries (
                    id integer PRIMARY KEY,
                    kind text,
                    method text,
                    amount numeric(12,2),
                    order_id integer,
                    is_deleted boolean DEFAULT false
                );
                CREATE TABLE payroll_items (
                    id serial PRIMARY KEY,
                    order_id integer,
                    master_id integer,
                    base_pay numeric(12,2),
                    fuel_pay numeric(12,2),
                    upsell_pay numeric(12,2),
                    total_pay numeric(12,2),
                    calc_info jsonb
                );
            """)

    async def asyncTearDown(self):
        await self.pool.close()
        admin = await asyncpg.connect(dsn=TEST_DB_DSN)
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await admin.close()

    async def _linked_order(self, conn, *, order_id=700, entry_id=900, masters=(1,)):
        """Заказ по счёту после привязки перевода на 22 000 ₽ — как его оставляет
        `_apply_wire_link`, а транзакция уже помечена удалённой."""
        await conn.execute(
            "INSERT INTO orders VALUES ($1, 'р/с', 22000, 0, false)", order_id)
        await conn.execute(
            "INSERT INTO cashbook_entries VALUES ($1, 'income', 'р/с', 22000, $2, true)",
            entry_id, order_id)
        for master_id in masters:
            await conn.execute(
                """
                INSERT INTO payroll_items
                    (order_id, master_id, base_pay, fuel_pay, upsell_pay, total_pay, calc_info)
                VALUES ($1, $2, 3000, 150, 0, 3150,
                        '{"rules": "1000/3000 + 150 + 500/3000", "share": 0.5,
                          "base_amount": 1.00, "cash_payment": 1.00, "wire_manual": true}')
                """,
                order_id, master_id)

    async def test_order_and_payroll_return_to_waiting_for_wire(self):
        async with self.pool.acquire() as conn:
            await self._linked_order(conn, masters=(1, 2))

            async with conn.transaction():
                unlinked = await bot._unlink_wire_entry(conn, entry_id=900)

            order = await conn.fetchrow("SELECT * FROM orders WHERE id=700")
            pays = await conn.fetch(
                "SELECT base_pay, upsell_pay, fuel_pay, total_pay, calc_info "
                "FROM payroll_items WHERE order_id=700 ORDER BY master_id")

        self.assertEqual(unlinked, 700)
        self.assertEqual(order["amount_total"], Decimal("1.00"))
        self.assertEqual(order["amount_cash"], Decimal("1.00"))
        self.assertTrue(order["awaiting_wire_payment"])
        self.assertEqual(len(pays), 2)
        for pay in pays:
            self.assertEqual(pay["base_pay"], Decimal("0"))
            self.assertEqual(pay["upsell_pay"], Decimal("0"))
            self.assertEqual(pay["fuel_pay"], Decimal("150.00"))      # бензин остаётся
            self.assertEqual(pay["total_pay"], Decimal("150.00"))
            self.assertNotIn("wire_manual", pay["calc_info"])

    async def test_another_live_wire_payment_keeps_the_order_paid(self):
        """У заказа есть ещё одна живая привязанная оплата — откатывать нечего."""
        async with self.pool.acquire() as conn:
            await self._linked_order(conn)
            await conn.execute(
                "INSERT INTO cashbook_entries VALUES (901, 'income', 'р/с', 22000, 700, false)")

            unlinked = await bot._unlink_wire_entry(conn, entry_id=900)
            order = await conn.fetchrow("SELECT * FROM orders WHERE id=700")

        self.assertIsNone(unlinked)
        self.assertFalse(order["awaiting_wire_payment"])
        self.assertEqual(order["amount_total"], Decimal("22000.00"))

    async def test_entry_without_order_changes_nothing(self):
        """Обычный приход по р/с, ни к чему не привязанный, — заказов не трогаем."""
        async with self.pool.acquire() as conn:
            await self._linked_order(conn)
            await conn.execute(
                "INSERT INTO cashbook_entries VALUES (902, 'income', 'р/с', 500, NULL, true)")

            unlinked = await bot._unlink_wire_entry(conn, entry_id=902)
            order = await conn.fetchrow("SELECT * FROM orders WHERE id=700")

        self.assertIsNone(unlinked)
        self.assertFalse(order["awaiting_wire_payment"])

    async def test_non_wire_order_is_not_rolled_back(self):
        """Привязка к заказу за наличные — не наш случай, заказ не трогаем."""
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO orders VALUES (701, 'Наличные', 5000, 5000, false)")
            await conn.execute(
                "INSERT INTO cashbook_entries VALUES (903, 'income', 'р/с', 5000, 701, true)")

            unlinked = await bot._unlink_wire_entry(conn, entry_id=903)
            order = await conn.fetchrow("SELECT * FROM orders WHERE id=701")

        self.assertIsNone(unlinked)
        self.assertEqual(order["amount_total"], Decimal("5000.00"))


if __name__ == "__main__":
    unittest.main()
