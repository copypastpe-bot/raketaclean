"""Перемещение денег между кучками кассы клининга: запись и расчёт.

ТЗ docs/plans/2026-10-05-olya-money-move.md, задача 2 (2026-10-05).

Перемещение — одна строка `cleaning_cashbook` вида `kind = 'move'`. `cash_holder`
строки — кучка-источник: 'olya' — «Деньги Ольга → Касса (Дима)», 'dima' —
«Касса (Дима) → Деньги Ольга»; `method` — подпись маршрута для людей.
«Касса (Дима)» = вся касса − «Деньги Ольга» (решение владельца 05.10, п.3),
поэтому перемещение не меняет всю кассу, а две кучки меняет в разные стороны.
Больше остатка источника переместить нельзя (п.4); удалить перемещение нельзя,
если кучка уйдёт в минус (п.7). Обе проверки — внутри транзакции записи под
`pg_advisory_xact_lock` с постоянным ключом.

На настоящем Postgres (DSN в `TEST_DB_DSN`, без него класс пропускается):
своя схема с минимальной `clients`, таблицы уборок — теми же файлами
миграций, что уходят на прод (0006, 0010, 0017). Образец изоляции —
tests/test_cleaning_olya_register.py.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import asyncpg

from cleaning import admin_ops, cashbook
from cleaning.constants import CASH_HOLDER_DIMA, CASH_HOLDER_OLYA, CASHBOOK_KIND_MOVE
from cleaning.format import format_olya_register

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "app" / "migrations"
SCHEMA = "cleaning_olya_move_test"

ROUTE_FROM_OLYA = "Деньги Ольга → Касса (Дима)"
ROUTE_FROM_DIMA = "Касса (Дима) → Деньги Ольга"


def _migration(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


class MoveConstantsTests(unittest.TestCase):
    def test_kind_and_routes(self):
        self.assertEqual(CASHBOOK_KIND_MOVE, "move")
        self.assertEqual(admin_ops.cash_move_route(CASH_HOLDER_OLYA), ROUTE_FROM_OLYA)
        self.assertEqual(admin_ops.cash_move_route(CASH_HOLDER_DIMA), ROUTE_FROM_DIMA)
        self.assertEqual(admin_ops.cash_holder_label(CASH_HOLDER_OLYA), "Деньги Ольга")
        self.assertEqual(admin_ops.cash_holder_label(CASH_HOLDER_DIMA), "Касса (Дима)")


class FormatOlyaRegisterMoveTests(unittest.TestCase):
    """Строка перемещения в `/cleaning_olya`: знак — по кучке-источнику."""

    def test_move_lines(self):
        at = datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc)  # 14:00 по Москве
        entries = [
            {
                "happened_at": at, "kind": "move", "method": ROUTE_FROM_OLYA,
                "amount": Decimal("10000"), "comment": None, "cash_holder": CASH_HOLDER_OLYA,
            },
            {
                "happened_at": at, "kind": "move", "method": ROUTE_FROM_DIMA,
                "amount": Decimal("3000"), "comment": "выдал", "cash_holder": CASH_HOLDER_DIMA,
            },
        ]
        text = format_olya_register(balance=Decimal("3500"), entries=entries)
        self.assertIn("05.10 14:00 | -10 000₽ | Перемещение → Касса (Дима) | —", text)
        self.assertIn("05.10 14:00 | +3 000₽ | Перемещение ← Касса (Дима) | выдал", text)


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class CashMoveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            await admin.execute(f"CREATE SCHEMA {SCHEMA}")
            await admin.execute(f"SET search_path TO {SCHEMA}")
            # Только то, на что ссылаются таблицы уборок, — не копия прода.
            await admin.execute("CREATE TABLE clients (id serial PRIMARY KEY)")
            await admin.execute(_migration("0006_cleaning.sql"))
            await admin.execute(_migration("0010_cleaning_orders_comment.sql"))
            await admin.execute(_migration("0017_cleaning_cashbook_cash_holder.sql"))
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

    async def _row(self, kind: str, amount: str, cash_holder) -> int:
        return await self.conn.fetchval(
            """
            INSERT INTO cleaning_cashbook (kind, method, amount, cash_holder)
            VALUES ($1, 'm', $2, $3)
            RETURNING id
            """,
            kind,
            Decimal(amount),
            cash_holder,
        )

    async def _piles(self) -> tuple[Decimal, Decimal, Decimal]:
        """(Деньги Ольга, Касса (Дима), вся касса)."""
        return (
            await cashbook.get_olya_balance(self.conn),
            await cashbook.get_dima_balance(self.conn),
            await cashbook.get_cleaning_balance(self.conn),
        )

    async def _move(self, source: str, amount: str, comment: str | None = None) -> int:
        return await admin_ops.record_cash_move(
            self.conn, source=source, amount=Decimal(amount), comment=comment
        )

    async def _moves_count(self) -> int:
        return await self.conn.fetchval(
            "SELECT count(*) FROM cleaning_cashbook WHERE kind = 'move'"
        )

    async def _is_deleted(self, row_id: int) -> bool:
        return await self.conn.fetchval(
            "SELECT deleted_at IS NOT NULL FROM cleaning_cashbook WHERE id = $1", row_id
        )

    async def _lock_held(self) -> int:
        return await self.conn.fetchval(
            """
            SELECT count(*)
            FROM pg_locks
            WHERE locktype = 'advisory'
              AND pid = pg_backend_pid()
              AND granted
              AND ((classid::bigint << 32) | objid::bigint) = $1
            """,
            admin_ops.CASH_MOVE_LOCK_KEY,
        )

    # --- остатки ---

    async def test_dima_is_total_minus_olya_and_holder_balance(self):
        await self._row("income", "5000", CASH_HOLDER_OLYA)
        await self._row("expense", "700", CASH_HOLDER_OLYA)
        await self._row("income", "20000", CASH_HOLDER_DIMA)
        # Строки до запуска реестра (NULL) — в «Касса (Дима)».
        await self._row("income", "1000", None)
        self.assertEqual(await self._piles(), (Decimal("4300"), Decimal("21000"), Decimal("25300")))
        self.assertEqual(
            await cashbook.get_holder_balance(self.conn, CASH_HOLDER_OLYA), Decimal("4300")
        )
        self.assertEqual(
            await cashbook.get_holder_balance(self.conn, CASH_HOLDER_DIMA), Decimal("21000")
        )

    # --- запись ---

    async def test_move_from_olya_changes_both_piles_not_total(self):
        await self._row("income", "5000", CASH_HOLDER_OLYA)
        await self._row("income", "20000", CASH_HOLDER_DIMA)
        await self._row("income", "1000", None)
        move_id = await self._move(CASH_HOLDER_OLYA, "3000", "сдала Диме")
        self.assertEqual(await self._piles(), (Decimal("2000"), Decimal("24000"), Decimal("26000")))
        row = await self.conn.fetchrow(
            "SELECT kind, cash_holder, method, amount, comment, order_id, deleted_at "
            "FROM cleaning_cashbook WHERE id = $1",
            move_id,
        )
        self.assertEqual(
            tuple(row),
            ("move", CASH_HOLDER_OLYA, ROUTE_FROM_OLYA, Decimal("3000.00"), "сдала Диме", None, None),
        )

    async def test_move_from_dima_changes_both_piles_not_total(self):
        await self._row("income", "5000", CASH_HOLDER_OLYA)
        await self._row("income", "20000", CASH_HOLDER_DIMA)
        move_id = await self._move(CASH_HOLDER_DIMA, "4000")
        self.assertEqual(await self._piles(), (Decimal("9000"), Decimal("16000"), Decimal("25000")))
        row = await self.conn.fetchrow(
            "SELECT cash_holder, method, comment FROM cleaning_cashbook WHERE id = $1", move_id
        )
        self.assertEqual(tuple(row), (CASH_HOLDER_DIMA, ROUTE_FROM_DIMA, None))

    async def test_move_more_than_source_balance_is_refused_nothing_written(self):
        await self._row("income", "5000", CASH_HOLDER_OLYA)
        await self._row("income", "20000", CASH_HOLDER_DIMA)
        with self.assertRaises(admin_ops.CashMoveExceedsBalance) as ctx:
            await self._move(CASH_HOLDER_OLYA, "5000.01")
        self.assertEqual(ctx.exception.holder, CASH_HOLDER_OLYA)
        self.assertEqual(ctx.exception.label, "Деньги Ольга")
        self.assertEqual(ctx.exception.balance, Decimal("5000"))
        self.assertIsInstance(ctx.exception, admin_ops.CashMoveRefused)
        self.assertEqual(await self._moves_count(), 0)

        with self.assertRaises(admin_ops.CashMoveExceedsBalance) as ctx:
            await self._move(CASH_HOLDER_DIMA, "20001")
        self.assertEqual(ctx.exception.holder, CASH_HOLDER_DIMA)
        self.assertEqual(ctx.exception.balance, Decimal("20000"))
        self.assertEqual(await self._moves_count(), 0)

        # Ровно остаток — можно.
        await self._move(CASH_HOLDER_OLYA, "5000")
        self.assertEqual(await self._piles(), (Decimal("0"), Decimal("25000"), Decimal("25000")))

    async def test_move_from_empty_or_negative_source_is_refused(self):
        await self._row("income", "20000", CASH_HOLDER_DIMA)
        await self._row("expense", "100", CASH_HOLDER_OLYA)  # Ольга доплатила — минус
        with self.assertRaises(admin_ops.CashMoveExceedsBalance) as ctx:
            await self._move(CASH_HOLDER_OLYA, "1")
        self.assertEqual(ctx.exception.balance, Decimal("-100"))
        self.assertEqual(await self._moves_count(), 0)

    async def test_move_rejects_bad_amount_and_source(self):
        await self._row("income", "5000", CASH_HOLDER_OLYA)
        for amount in ("0", "-1"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                await self._move(CASH_HOLDER_OLYA, amount)
        for source in (None, "jenya"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                await self._move(source, "1")
        self.assertEqual(await self._moves_count(), 0)

    async def test_move_takes_lock_until_callers_transaction_ends(self):
        await self._row("income", "5000", CASH_HOLDER_OLYA)
        async with self.conn.transaction():
            move_id = await self._move(CASH_HOLDER_OLYA, "100")
            self.assertEqual(await self._lock_held(), 1)
            async with self.conn.transaction():
                await admin_ops.delete_cash_move(self.conn, move_id=move_id)
            self.assertEqual(await self._lock_held(), 1)
        self.assertEqual(await self._lock_held(), 0)

    async def test_second_move_waits_for_the_first(self):
        """Две записи не пересекаются: вторая ждёт замок первой."""
        await self._row("income", "5000", CASH_HOLDER_OLYA)
        other = await asyncpg.connect(TEST_DB_DSN, server_settings={"search_path": SCHEMA})
        try:
            async with self.conn.transaction():
                await self._move(CASH_HOLDER_OLYA, "4000")
                await other.execute("SET lock_timeout = '300ms'")
                with self.assertRaises(asyncpg.LockNotAvailableError):
                    await admin_ops.record_cash_move(
                        other, source=CASH_HOLDER_OLYA, amount=Decimal("4000"), comment=None
                    )
            # Первая зафиксирована — вторая видит новый остаток и получает отказ.
            with self.assertRaises(admin_ops.CashMoveExceedsBalance) as ctx:
                await admin_ops.record_cash_move(
                    other, source=CASH_HOLDER_OLYA, amount=Decimal("4000"), comment=None
                )
            self.assertEqual(ctx.exception.balance, Decimal("1000"))
        finally:
            await other.close()

    # --- удаление ---

    async def test_deleted_move_is_not_counted(self):
        await self._row("income", "5000", CASH_HOLDER_OLYA)
        await self._row("income", "20000", CASH_HOLDER_DIMA)
        move_id = await self._move(CASH_HOLDER_OLYA, "3000", "сдала")
        deleted = await admin_ops.delete_cash_move(self.conn, move_id=move_id)
        self.assertEqual(deleted["id"], move_id)
        self.assertEqual(deleted["cash_holder"], CASH_HOLDER_OLYA)
        self.assertEqual(deleted["method"], ROUTE_FROM_OLYA)
        self.assertEqual(deleted["amount"], Decimal("3000"))
        self.assertEqual(deleted["comment"], "сдала")
        self.assertTrue(await self._is_deleted(move_id))
        self.assertEqual(await self._piles(), (Decimal("5000"), Decimal("20000"), Decimal("25000")))

        # Повторно — не найдено.
        self.assertIsNone(await admin_ops.delete_cash_move(self.conn, move_id=move_id))
        self.assertIsNone(await admin_ops.check_cash_move_delete(self.conn, move_id=move_id))
        self.assertIsNone(await admin_ops.get_cash_move(self.conn, move_id=move_id))

    async def test_delete_unknown_id_or_other_kind_returns_none(self):
        income_id = await self._row("income", "5000", CASH_HOLDER_OLYA)
        for move_id in (income_id, 999999):
            with self.subTest(move_id=move_id):
                self.assertIsNone(await admin_ops.get_cash_move(self.conn, move_id=move_id))
                self.assertIsNone(
                    await admin_ops.check_cash_move_delete(self.conn, move_id=move_id)
                )
                self.assertIsNone(await admin_ops.delete_cash_move(self.conn, move_id=move_id))
        self.assertFalse(await self._is_deleted(income_id))

    async def test_delete_refused_when_dima_would_go_negative(self):
        await self._row("income", "10000", CASH_HOLDER_OLYA)
        move_id = await self._move(CASH_HOLDER_OLYA, "10000")
        await self._row("expense", "7000", CASH_HOLDER_DIMA)  # в «Касса (Дима)» 3000
        for func in (admin_ops.check_cash_move_delete, admin_ops.delete_cash_move):
            with self.subTest(func=func.__name__):
                with self.assertRaises(admin_ops.CashMoveDeleteGoesNegative) as ctx:
                    await func(self.conn, move_id=move_id)
                self.assertEqual(ctx.exception.holder, CASH_HOLDER_DIMA)
                self.assertEqual(ctx.exception.label, "Касса (Дима)")
                self.assertEqual(ctx.exception.balance, Decimal("-7000"))
                self.assertIsInstance(ctx.exception, admin_ops.CashMoveRefused)
        self.assertFalse(await self._is_deleted(move_id))

    async def test_delete_refused_when_olya_would_go_negative(self):
        await self._row("income", "5000", CASH_HOLDER_DIMA)
        move_id = await self._move(CASH_HOLDER_DIMA, "5000")
        await self._row("expense", "2000", CASH_HOLDER_OLYA)  # в «Деньги Ольга» 3000
        with self.assertRaises(admin_ops.CashMoveDeleteGoesNegative) as ctx:
            await admin_ops.delete_cash_move(self.conn, move_id=move_id)
        self.assertEqual(ctx.exception.holder, CASH_HOLDER_OLYA)
        self.assertEqual(ctx.exception.balance, Decimal("-2000"))
        self.assertFalse(await self._is_deleted(move_id))

    async def test_delete_to_exactly_zero_is_allowed(self):
        await self._row("income", "10000", CASH_HOLDER_OLYA)
        move_id = await self._move(CASH_HOLDER_OLYA, "4000")
        await self._row("expense", "6000", CASH_HOLDER_OLYA)  # Ольга 0, Дима 4000
        checked = await admin_ops.check_cash_move_delete(self.conn, move_id=move_id)
        self.assertEqual(checked["id"], move_id)
        self.assertFalse(await self._is_deleted(move_id))
        await admin_ops.delete_cash_move(self.conn, move_id=move_id)
        self.assertEqual(await self._piles(), (Decimal("4000"), Decimal("0"), Decimal("4000")))

    # --- отчёты ---

    async def test_cash_report_and_pnl_for_period_do_not_change(self):
        await self._row("income", "5000", CASH_HOLDER_OLYA)
        await self._row("income", "20000", CASH_HOLDER_DIMA)
        await self._row("expense", "300", CASH_HOLDER_OLYA)
        await self._row("dividend", "1000", CASH_HOLDER_DIMA)
        await self._row("withdrawal", "200", CASH_HOLDER_DIMA)
        await self._row("deposit", "400", CASH_HOLDER_OLYA)
        now = datetime.now(timezone.utc)
        start, end = now - timedelta(days=1), now + timedelta(days=1)
        report_before = await cashbook.get_cleaning_cash_report(self.conn, start, end)
        pnl_before = await cashbook.get_cleaning_pnl(self.conn, start, end)

        await self._move(CASH_HOLDER_OLYA, "2000")
        await self._move(CASH_HOLDER_DIMA, "500")

        self.assertEqual(
            await cashbook.get_cleaning_cash_report(self.conn, start, end), report_before
        )
        self.assertEqual(await cashbook.get_cleaning_pnl(self.conn, start, end), pnl_before)

    # --- списки ---

    async def test_list_recent_moves_newest_first_not_deleted_default_ten(self):
        await self._row("income", "100000", CASH_HOLDER_OLYA)
        await self._row("expense", "10", CASH_HOLDER_OLYA)
        ids = [await self._move(CASH_HOLDER_OLYA, str(100 + i)) for i in range(12)]
        await admin_ops.delete_cash_move(self.conn, move_id=ids[-1])
        await self._row("dividend", "10", CASH_HOLDER_DIMA)

        moves = await admin_ops.list_recent_cash_moves(self.conn)
        self.assertEqual([m["id"] for m in moves], list(reversed(ids[1:11])))
        first = moves[0]
        self.assertEqual(first["cash_holder"], CASH_HOLDER_OLYA)
        self.assertEqual(first["method"], ROUTE_FROM_OLYA)
        self.assertEqual(first["amount"], Decimal("110"))
        self.assertIsNotNone(first["happened_at"])

        limited = await admin_ops.list_recent_cash_moves(self.conn, limit=2)
        self.assertEqual([m["id"] for m in limited], [ids[10], ids[9]])

    async def test_list_recent_moves_empty(self):
        self.assertEqual(await admin_ops.list_recent_cash_moves(self.conn), [])

    async def test_olya_entries_show_moves_both_directions(self):
        olya_income = await self._row("income", "5000", CASH_HOLDER_OLYA)
        await self._row("income", "9000", CASH_HOLDER_DIMA)
        from_olya = await self._move(CASH_HOLDER_OLYA, "1000")
        to_olya = await self._move(CASH_HOLDER_DIMA, "2000")
        await self._row("expense", "300", CASH_HOLDER_DIMA)  # обычная касса — не видна
        deleted = await self._move(CASH_HOLDER_DIMA, "50")
        await admin_ops.delete_cash_move(self.conn, move_id=deleted)

        entries = await cashbook.list_olya_entries(self.conn)
        self.assertEqual([e["id"] for e in entries], [to_olya, from_olya, olya_income])
        self.assertEqual(
            [e["cash_holder"] for e in entries],
            [CASH_HOLDER_DIMA, CASH_HOLDER_OLYA, CASH_HOLDER_OLYA],
        )
        self.assertEqual(await cashbook.get_olya_balance(self.conn), Decimal("6000"))


if __name__ == "__main__":
    unittest.main()
