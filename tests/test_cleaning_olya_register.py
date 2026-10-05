"""Реестр денег Оли: поле «чьи деньги» у строки кассы клининга и остаток Оли.

ТЗ docs/plans/2026-10-05-olya-money-register.md, задача 1 (2026-10-05).

У `cleaning_cashbook` появляется `cash_holder` — 'olya' | 'dima' | NULL
(миграция 0017 и зеркало при запуске `cleaning/schema.py`
`ensure_cleaning_schema`). Остаток Оли (`cleaning/cashbook.py`
`get_olya_balance`) — приход и внесение плюс, расход, выплата и изъятие
минус, только строки 'olya' и только не отменённые. Строки до запуска
(NULL) не входят: реестр стартует с нуля (решение владельца 05.10, п.5).
Функции записи принимают `cash_holder` обязательным: новая точка записи
не может его забыть.

На настоящем Postgres (DSN в `TEST_DB_DSN`, без него класс пропускается):
своя схема с минимальной `clients`, таблицы уборок — теми же файлами
миграций, что уходят на прод (0006, 0010, 0017).
"""

import inspect
import os
import unittest
from decimal import Decimal
from pathlib import Path

import asyncpg

from cleaning import admin_ops, cashbook
from cleaning.constants import CASH_HOLDER_DIMA, CASH_HOLDER_OLYA
from cleaning.schema import ensure_cleaning_schema

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "app" / "migrations"
MIGRATION = "0017_cleaning_cashbook_cash_holder.sql"
SCHEMA = "cleaning_olya_register_test"


def _migration(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


class WriteFunctionsRequireCashHolderTests(unittest.TestCase):
    """Без значения по умолчанию: забытый cash_holder — ошибка, а не тихий NULL."""

    def test_every_write_function_has_required_keyword_cash_holder(self):
        for func in (
            cashbook.record_income,
            cashbook.record_expense,
            cashbook.record_dividend,
            admin_ops.add_cash_income,
            admin_ops.add_cash_expense,
            admin_ops.add_cash_withdrawal,
        ):
            with self.subTest(func=func.__name__):
                param = inspect.signature(func).parameters["cash_holder"]
                self.assertIs(param.kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIs(param.default, inspect.Parameter.empty)


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class OlyaRegisterTests(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self):
        await self.conn.close()
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        finally:
            await admin.close()

    # --- помощники ---

    async def _cash_holder_column(self):
        return await self.conn.fetchrow(
            """
            SELECT data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = 'cleaning_cashbook'
              AND column_name = 'cash_holder'
            """,
            SCHEMA,
        )

    async def _check_constraints(self) -> int:
        return await self.conn.fetchval(
            """
            SELECT count(*)
            FROM pg_constraint
            WHERE conrelid = 'cleaning_cashbook'::regclass
              AND conname = 'cleaning_cashbook_cash_holder_check'
            """
        )

    async def _order(self) -> int:
        client_id = await self.conn.fetchval("INSERT INTO clients DEFAULT VALUES RETURNING id")
        foreman_id = await self.conn.fetchval(
            "INSERT INTO cleaning_foremen (fn) VALUES ('Ольга') RETURNING id"
        )
        return await self.conn.fetchval(
            """
            INSERT INTO cleaning_orders (client_id, foreman_id, total_amount)
            VALUES ($1, $2, 5000) RETURNING id
            """,
            client_id,
            foreman_id,
        )

    async def _row(self, kind: str, amount: str, cash_holder, *, deleted: bool = False) -> int:
        return await self.conn.fetchval(
            """
            INSERT INTO cleaning_cashbook (kind, method, amount, cash_holder, deleted_at)
            VALUES ($1, 'm', $2, $3, CASE WHEN $4 THEN NOW() END)
            RETURNING id
            """,
            kind,
            Decimal(amount),
            cash_holder,
            deleted,
        )

    # --- поле ---

    async def test_migration_adds_nullable_text_column_and_is_repeatable(self):
        await self.conn.execute(_migration(MIGRATION))
        # Повторный прогон на выкате не падает и не дублирует ограничение.
        await self.conn.execute(_migration(MIGRATION))
        column = await self._cash_holder_column()
        self.assertEqual(column["data_type"], "text")
        self.assertEqual(column["is_nullable"], "YES")
        self.assertEqual(await self._check_constraints(), 1)

    async def test_startup_mirror_adds_column_and_check_and_is_repeatable(self):
        await ensure_cleaning_schema(self.conn)
        await ensure_cleaning_schema(self.conn)
        column = await self._cash_holder_column()
        self.assertEqual(column["data_type"], "text")
        self.assertEqual(await self._check_constraints(), 1)

    async def test_mirror_after_migration_does_not_duplicate_check(self):
        await self.conn.execute(_migration(MIGRATION))
        await ensure_cleaning_schema(self.conn)
        self.assertEqual(await self._check_constraints(), 1)

    async def test_check_allows_olya_dima_null_and_rejects_other(self):
        await self.conn.execute(_migration(MIGRATION))
        for value in (CASH_HOLDER_OLYA, CASH_HOLDER_DIMA, None):
            await self._row("income", "1", value)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self._row("income", "1", "jenya")

    async def test_old_rows_stay_null_after_migration(self):
        await self.conn.execute(
            "INSERT INTO cleaning_cashbook (kind, method, amount) VALUES ('income', 'Наличные', 100)"
        )
        await self.conn.execute(_migration(MIGRATION))
        self.assertIsNone(await self.conn.fetchval("SELECT cash_holder FROM cleaning_cashbook"))

    # --- запись ---

    async def test_write_functions_store_cash_holder(self):
        await self.conn.execute(_migration(MIGRATION))
        order_id = await self._order()
        await cashbook.record_income(
            self.conn, method="Наличные", amount=Decimal("100"), order_id=order_id,
            cash_holder=CASH_HOLDER_OLYA,
        )
        await cashbook.record_expense(
            self.conn, category="Химия", amount=Decimal("10"), order_id=order_id,
            cash_holder=CASH_HOLDER_OLYA,
        )
        await cashbook.record_dividend(
            self.conn, amount=Decimal("20"), comment="Выплата", cash_holder=CASH_HOLDER_DIMA,
        )
        await admin_ops.add_cash_income(
            self.conn, method="Наличные", amount=Decimal("30"), comment=None,
            cash_holder=CASH_HOLDER_OLYA,
        )
        await admin_ops.add_cash_expense(
            self.conn, category="ГСМ", amount=Decimal("5"), comment=None,
            cash_holder=CASH_HOLDER_DIMA,
        )
        await admin_ops.add_cash_withdrawal(
            self.conn, amount=Decimal("7"), comment=None, cash_holder=CASH_HOLDER_OLYA,
        )
        rows = await self.conn.fetch(
            "SELECT kind, cash_holder FROM cleaning_cashbook ORDER BY id"
        )
        self.assertEqual(
            [(r["kind"], r["cash_holder"]) for r in rows],
            [
                ("income", "olya"),
                ("expense", "olya"),
                ("dividend", "dima"),
                ("deposit", "olya"),
                ("expense", "dima"),
                ("withdrawal", "olya"),
            ],
        )

    # --- остаток Оли ---

    async def test_balance_counts_only_olya_with_plus_and_minus_kinds(self):
        await self.conn.execute(_migration(MIGRATION))
        await self._row("income", "1000", CASH_HOLDER_OLYA)
        await self._row("deposit", "200", CASH_HOLDER_OLYA)
        await self._row("expense", "150", CASH_HOLDER_OLYA)
        await self._row("dividend", "300", CASH_HOLDER_OLYA)
        await self._row("withdrawal", "50", CASH_HOLDER_OLYA)
        # Касса Димы и строки до запуска в остаток Оли не входят.
        await self._row("income", "7000", CASH_HOLDER_DIMA)
        await self._row("expense", "900", CASH_HOLDER_DIMA)
        await self._row("income", "5000", None)
        await self._row("withdrawal", "40", None)
        self.assertEqual(
            await cashbook.get_olya_balance(self.conn),
            Decimal("1000") + Decimal("200") - Decimal("150") - Decimal("300") - Decimal("50"),
        )
        # Общая касса по-прежнему считает всё.
        self.assertEqual(
            await cashbook.get_cleaning_balance(self.conn),
            Decimal("1000") + 200 - 150 - 300 - 50 + 7000 - 900 + 5000 - 40,
        )

    async def test_balance_is_zero_when_only_old_rows(self):
        await self.conn.execute(
            "INSERT INTO cleaning_cashbook (kind, method, amount) VALUES ('income', 'Наличные', 100)"
        )
        await self.conn.execute(_migration(MIGRATION))
        self.assertEqual(await cashbook.get_olya_balance(self.conn), Decimal("0"))

    async def test_balance_skips_deleted_rows(self):
        await self.conn.execute(_migration(MIGRATION))
        await self._row("income", "1000", CASH_HOLDER_OLYA)
        await self._row("income", "400", CASH_HOLDER_OLYA, deleted=True)
        await self._row("expense", "100", CASH_HOLDER_OLYA, deleted=True)
        self.assertEqual(await cashbook.get_olya_balance(self.conn), Decimal("1000"))

    async def test_cancel_order_and_cancel_dividend_drop_out_of_balance(self):
        """Решение 05.10, п.8: отмены идут через deleted_at — реестр откатывается сам."""
        await self.conn.execute(_migration(MIGRATION))
        await self._row("deposit", "1000", CASH_HOLDER_OLYA)
        order_id = await self._order()
        await cashbook.record_income(
            self.conn, method="Наличные", amount=Decimal("5000"), order_id=order_id,
            cash_holder=CASH_HOLDER_OLYA,
        )
        await cashbook.record_expense(
            self.conn, category="Химия", amount=Decimal("300"), order_id=order_id,
            cash_holder=CASH_HOLDER_OLYA,
        )
        payout_id = await cashbook.record_dividend(
            self.conn, amount=Decimal("200"), comment="Выплата", cash_holder=CASH_HOLDER_OLYA,
        )
        self.assertEqual(await cashbook.get_olya_balance(self.conn), Decimal("5500"))

        await admin_ops.cancel_order(self.conn, order_id=order_id)
        self.assertEqual(await cashbook.get_olya_balance(self.conn), Decimal("800"))

        await admin_ops.cancel_dividend(self.conn, payout_id=payout_id)
        self.assertEqual(await cashbook.get_olya_balance(self.conn), Decimal("1000"))

    # --- последние операции ---

    async def test_entries_only_olya_not_deleted_newest_first_with_limit(self):
        await self.conn.execute(_migration(MIGRATION))
        first = await self._row("income", "100", CASH_HOLDER_OLYA)
        await self._row("income", "999", CASH_HOLDER_DIMA)
        await self._row("income", "888", None)
        await self._row("expense", "777", CASH_HOLDER_OLYA, deleted=True)
        second = await self._row("expense", "20", CASH_HOLDER_OLYA)
        third = await self._row("withdrawal", "30", CASH_HOLDER_OLYA)

        entries = await cashbook.list_olya_entries(self.conn)
        self.assertEqual([e["id"] for e in entries], [third, second, first])
        self.assertEqual(entries[0]["kind"], "withdrawal")
        self.assertEqual(entries[0]["amount"], Decimal("30"))

        limited = await cashbook.list_olya_entries(self.conn, limit=2)
        self.assertEqual([e["id"] for e in limited], [third, second])


if __name__ == "__main__":
    unittest.main()
