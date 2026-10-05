"""Реестр денег Оли: строка `Деньги Оли: N₽` в сообщениях об отменах.

ТЗ docs/plans/2026-10-05-olya-money-register.md, решения владельца 05.10:
п.6 — остаток после операции, задевшей деньги Оли, строкой в чат клининговых
денег; п.8 — отмена уборки и отмена выплаты откатывают их строки из реестра.
Замечание ревью: у отмен строки не было.

Отмена задела деньги Оли, если у отменённой уборки была хоть одна строка кассы
с `cash_holder='olya'`, а у отменённой выплаты `cash_holder='olya'`. Тогда
сообщение об отмене получает последней строкой остаток Оли после отмены,
прочитанный в той же транзакции. Иначе строки нет.

Формат — без базы. Обработчики `cancel_order_confirmed` и
`dividend_cancel_confirmed` — на настоящем Postgres (DSN в `TEST_DB_DSN`, без
него класс пропускается): своя схема с минимальной `clients`, таблицы уборок —
теми же файлами миграций, что уходят на прод (0006, 0010, 0017). Отправка в чат
подменена: проверяется текст сообщения.
"""

import os
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

import asyncpg

import cleaning.handlers as cleaning_handlers
from cleaning import cashbook
from cleaning.constants import CASH_HOLDER_DIMA, CASH_HOLDER_OLYA
from cleaning.format import format_cancel_order_alert, format_dividend_cancel_alert

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "app" / "migrations"
SCHEMA = "cleaning_olya_cancel_test"


def _migration(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def _cancel_order_kwargs(**over):
    kwargs = dict(
        order_id=812,
        address="Мира, 10",
        total_amount=D("6000"),
        bonuses_used=0,
        bonuses_earned=0,
        cashbook_rows_deleted=3,
        balance_after=D("81540"),
    )
    kwargs.update(over)
    return kwargs


class CancelOrderAlertOlyaLineTests(unittest.TestCase):
    def test_without_olya_balance_no_line(self):
        text = format_cancel_order_alert(**_cancel_order_kwargs())
        self.assertNotIn("Деньги Ольга", text)
        self.assertEqual(text.split("\n")[-1], "Касса клининга: 81 540₽")

    def test_with_olya_balance_last_line(self):
        text = format_cancel_order_alert(**_cancel_order_kwargs(), olya_balance=D("6500"))
        lines = text.split("\n")
        self.assertEqual(lines[-1], "Деньги Ольга: 6 500₽")
        self.assertEqual(lines[-2], "Касса клининга: 81 540₽")


class DividendCancelAlertOlyaLineTests(unittest.TestCase):
    def test_without_olya_balance_text_unchanged(self):
        text = format_dividend_cancel_alert(
            payout_id=17, amount=D("9999"), balance_after=D("20459")
        )
        self.assertEqual(
            text,
            "↩️ Отменена выплата прибыли #17\n"
            "Сумма: 9 999₽\n"
            "Остаток в кассе: 20 459₽",
        )

    def test_with_olya_balance_last_line(self):
        text = format_dividend_cancel_alert(
            payout_id=17, amount=D("9999"), balance_after=D("20459"), olya_balance=D("0")
        )
        lines = text.split("\n")
        self.assertEqual(lines[-1], "Деньги Ольга: 0₽")
        self.assertEqual(lines[-2], "Остаток в кассе: 20 459₽")


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    """Пул из одного настоящего соединения: транзакция обработчика — настоящая."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class _Message:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


class _State:
    def __init__(self, data):
        self._data = dict(data)

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self._data = {}


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class CancelHandlersOlyaLineTests(unittest.IsolatedAsyncioTestCase):
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

    async def _order(self) -> int:
        client_id = await self.conn.fetchval("INSERT INTO clients DEFAULT VALUES RETURNING id")
        foreman_id = await self.conn.fetchval(
            "INSERT INTO cleaning_foremen (fn) VALUES ('Ольга') RETURNING id"
        )
        return await self.conn.fetchval(
            """
            INSERT INTO cleaning_orders (client_id, foreman_id, total_amount, address)
            VALUES ($1, $2, 6000, 'Мира, 10') RETURNING id
            """,
            client_id,
            foreman_id,
        )

    async def _deposit_olya(self, amount: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO cleaning_cashbook (kind, method, amount, cash_holder)
            VALUES ('deposit', 'Внесение', $1, $2)
            """,
            D(amount),
            CASH_HOLDER_OLYA,
        )

    async def _cancel_order(self, order_id: int) -> str:
        send = AsyncMock()
        msg = _Message()
        with mock.patch.object(cleaning_handlers, "send_cleaning_money_flow", send):
            await cleaning_handlers.cancel_order_confirmed(
                msg,
                _State({"cancel_order_id": order_id}),
                pool=_Pool(self.conn),
                bot=object(),
            )
        send.assert_awaited_once()
        return send.await_args.args[1]

    async def _cancel_dividend(self, payout_id: int) -> str:
        send = AsyncMock()
        msg = _Message()
        with mock.patch.object(cleaning_handlers, "send_cleaning_money_flow", send):
            await cleaning_handlers.dividend_cancel_confirmed(
                msg,
                _State({"cancel_payout_id": payout_id}),
                pool=_Pool(self.conn),
                bot=object(),
            )
        send.assert_awaited_once()
        return send.await_args.args[1]

    # --- отмена уборки ---

    async def test_cancel_order_with_olya_rows_shows_olya_balance_after_cancel(self):
        await self._deposit_olya("1000")
        order_id = await self._order()
        await cashbook.record_income(
            self.conn, method="Наличные", amount=D("4000"), order_id=order_id,
            cash_holder=CASH_HOLDER_OLYA,
        )
        await cashbook.record_income(
            self.conn, method="Расчётный", amount=D("2000"), order_id=order_id,
            cash_holder=CASH_HOLDER_DIMA,
        )
        await cashbook.record_expense(
            self.conn, category="Химия", amount=D("300"), order_id=order_id,
            cash_holder=CASH_HOLDER_OLYA,
        )
        self.assertEqual(await cashbook.get_olya_balance(self.conn), D("4700"))

        text = await self._cancel_order(order_id)

        self.assertIn(f"↩️ Отменён заказ уборки #{order_id}", text)
        self.assertIn("Откатано строк кассы: 3", text)
        # 1 000 внесения остаются, приход 4 000 и расход 300 откатились.
        self.assertEqual(text.split("\n")[-1], "Деньги Ольга: 1 000₽")
        self.assertEqual(await cashbook.get_olya_balance(self.conn), D("1000"))

    async def test_cancel_order_without_olya_rows_no_line(self):
        await self._deposit_olya("1000")
        order_id = await self._order()
        await cashbook.record_income(
            self.conn, method="Расчётный", amount=D("6000"), order_id=order_id,
            cash_holder=CASH_HOLDER_DIMA,
        )

        text = await self._cancel_order(order_id)

        self.assertIn(f"↩️ Отменён заказ уборки #{order_id}", text)
        self.assertNotIn("Деньги Ольга", text)
        self.assertEqual(text.split("\n")[-1], "Касса клининга: 1 000₽")

    # --- отмена выплаты ---

    async def test_cancel_olya_dividend_shows_olya_balance_after_cancel(self):
        await self._deposit_olya("1000")
        payout_id = await cashbook.record_dividend(
            self.conn, amount=D("200"), comment="Выплата", cash_holder=CASH_HOLDER_OLYA,
        )
        self.assertEqual(await cashbook.get_olya_balance(self.conn), D("800"))

        text = await self._cancel_dividend(payout_id)

        self.assertIn(f"↩️ Отменена выплата прибыли #{payout_id}", text)
        self.assertEqual(text.split("\n")[-1], "Деньги Ольга: 1 000₽")

    async def test_cancel_dima_dividend_no_line(self):
        await self._deposit_olya("1000")
        payout_id = await cashbook.record_dividend(
            self.conn, amount=D("200"), comment="Выплата", cash_holder=CASH_HOLDER_DIMA,
        )

        text = await self._cancel_dividend(payout_id)

        self.assertIn(f"↩️ Отменена выплата прибыли #{payout_id}", text)
        self.assertNotIn("Деньги Ольга", text)
        self.assertEqual(text.split("\n")[-1], "Остаток в кассе: 1 000₽")


if __name__ == "__main__":
    unittest.main()
