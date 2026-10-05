"""Реестр денег Оли, задача 2 (docs/plans/2026-10-05-olya-money-register.md):
проведение уборки и кнопка Оли.

- `do_provesti` помечает строки кассы: приход «Наличные»/«Карта» и расходы —
  деньги Оли (`olya`), «Расчётный» — касса (`dima`).
- Сообщения в чат клининговых денег по этим операциям получают строку
  `Деньги Оли: N₽` — остаток после операции. Если операция деньги Оли не
  задела (уборка целиком по «Расчётному» без расходов), строки нет.

Сообщение о проведённой уборке уходит не сразу, а через очередь
`pending_order_reports` (bot.py `_dispatch_cleaning_order_report`), поэтому
остаток едет в payload очереди. Старые строки очереди без этого поля строки
не дают.

Без базы: запись в кассу и остатки мокнуты, проверяется, что и с какой
меткой передаётся. Сам расчёт остатка Оли — tests/test_cleaning_olya_register.py.
"""

import json
import unittest
from decimal import Decimal as D
from unittest import mock
from unittest.mock import AsyncMock

import bot
import cleaning.handlers as cleaning_handlers
from cleaning.constants import CASH_HOLDER_DIMA, CASH_HOLDER_OLYA, CLEANING_GIFT_CERT_LABEL
from cleaning.format import format_cash_op_alert, format_order_provided_alert


def _order_alert_kwargs(**over):
    kwargs = dict(
        order_id=42,
        foreman_name="Ольга",
        client_phone="79161234567",
        client_name="Иван",
        address=None,
        total_amount=D("3500"),
        payments=[("Наличные", D("3500"))],
        expenses=[],
        bonuses_used=D("0"),
        bonuses_earned=D("0"),
        profit=D("3500"),
        balance_after=D("87540"),
    )
    kwargs.update(over)
    return kwargs


def _cash_op_kwargs(**over):
    kwargs = dict(
        op_label="Расход",
        bucket="Химия",
        amount=D("300"),
        comment="Расход",
        balance_after=D("50000"),
    )
    kwargs.update(over)
    return kwargs


class OrderAlertOlyaLineTests(unittest.TestCase):
    def test_without_olya_balance_no_line(self):
        # прочие вызовы (без нового параметра) не меняются
        text = format_order_provided_alert(**_order_alert_kwargs())
        self.assertNotIn("Деньги Оли", text)

    def test_with_olya_balance_last_line(self):
        text = format_order_provided_alert(**_order_alert_kwargs(olya_balance=D("12500")))
        lines = text.split("\n")
        self.assertEqual(lines[-1], "Деньги Оли: 12 500₽")
        self.assertEqual(lines[-2], "Касса клининга: 87 540₽")

    def test_zero_olya_balance_still_shown(self):
        # ноль — это остаток, а не «не задело»
        text = format_order_provided_alert(**_order_alert_kwargs(olya_balance=D("0")))
        self.assertIn("Деньги Оли: 0₽", text)


class CashOpAlertOlyaLineTests(unittest.TestCase):
    def test_without_olya_balance_no_line(self):
        text = format_cash_op_alert(**_cash_op_kwargs())
        self.assertNotIn("Деньги Оли", text)

    def test_with_olya_balance_last_line(self):
        text = format_cash_op_alert(**_cash_op_kwargs(olya_balance=D("3200.50")))
        lines = text.split("\n")
        self.assertEqual(lines[-1], "Деньги Оли: 3 200.50₽")
        self.assertEqual(lines[-2], "Остаток: 50 000₽")

    def test_negative_olya_balance_shown_with_minus(self):
        text = format_cash_op_alert(**_cash_op_kwargs(olya_balance=D("-1500")))
        self.assertIn("Деньги Оли: -1 500₽", text)


class IncomeCashHolderTests(unittest.TestCase):
    def test_cash_and_card_go_to_olya(self):
        self.assertEqual(cleaning_handlers._income_cash_holder("Наличные"), CASH_HOLDER_OLYA)
        self.assertEqual(cleaning_handlers._income_cash_holder("Карта"), CASH_HOLDER_OLYA)

    def test_wire_goes_to_dima(self):
        self.assertEqual(cleaning_handlers._income_cash_holder("Расчётный"), CASH_HOLDER_DIMA)


# ---------- do_provesti: фейки, как в tests/test_cleaning_pending_reports.py ----------


class _NullTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ProvestiConn:
    def __init__(self, order_id: int):
        self.executed: list[tuple[str, tuple]] = []
        self.order_id = order_id

    def transaction(self):
        return _NullTx()

    async def fetchrow(self, query, *args):
        if "cleaning_foremen" in query:
            return {"id": 1, "fn": "Ольга", "ln": "Бригадир"}
        if "FROM clients WHERE id=" in query:
            return {
                "id": 42, "full_name": "Иван Иванов", "phone": "+79161234567",
                "address": None, "bonus_balance": 0,
            }
        if "INSERT INTO cleaning_orders" in query:
            return {"id": self.order_id}
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "INSERT 1"


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Message:
    def __init__(self):
        self.from_user = type("U", (), {"id": 999})()
        self.answer = AsyncMock()


class _State:
    def __init__(self, data):
        self._data = dict(data)

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self._data = {}


def _provesti_data(*, payments, expenses=()):
    total = sum((D(p["amount"]) for p in payments), D("0"))
    return {
        "client_id": 42,
        "total_amount": str(total),
        "bonus_spend": 0,
        "payments": list(payments),
        "client_op_id": "op-777",
        "comment": None,
        "expenses": list(expenses),
        "is_wire_payment": False,
        "phone_norm": "+79161234567",
        "client_name": None,
    }


class DoProvestiOlyaTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, data, *, olya_balance=D("2700")):
        conn = _ProvestiConn(order_id=777)
        self.record_income = AsyncMock()
        self.record_expense = AsyncMock()
        self.get_olya_balance = AsyncMock(return_value=olya_balance)
        with mock.patch.object(cleaning_handlers, "record_income", self.record_income), \
             mock.patch.object(cleaning_handlers, "record_expense", self.record_expense), \
             mock.patch.object(cleaning_handlers, "get_olya_balance", self.get_olya_balance), \
             mock.patch.object(
                 cleaning_handlers, "get_cleaning_balance", AsyncMock(return_value=D("50000"))
             ), \
             mock.patch.object(
                 cleaning_handlers, "_enqueue_cleaning_completed_notifications", AsyncMock()
             ):
            await cleaning_handlers.do_provesti(_Message(), _State(data), pool=_Pool(conn))
        inserts = [a for q, a in conn.executed if "INSERT INTO pending_order_reports" in q]
        self.assertEqual(len(inserts), 1)
        return json.loads(inserts[0][1])

    async def test_cash_card_income_and_expenses_olya_wire_dima(self):
        payload = await self._run(_provesti_data(
            payments=[
                {"method": "Наличные", "amount": "2000"},
                {"method": "Карта", "amount": "1000"},
                {"method": "Расчётный", "amount": "500"},
            ],
            expenses=[
                {"category": "Химия", "amount": "300"},
                {"category": "ГСМ", "amount": "200"},
            ],
        ))

        income = {
            c.kwargs["method"]: c.kwargs["cash_holder"]
            for c in self.record_income.await_args_list
        }
        self.assertEqual(income, {
            "Наличные": CASH_HOLDER_OLYA,
            "Карта": CASH_HOLDER_OLYA,
            "Расчётный": CASH_HOLDER_DIMA,
        })
        self.assertEqual(
            [c.kwargs["cash_holder"] for c in self.record_expense.await_args_list],
            [CASH_HOLDER_OLYA, CASH_HOLDER_OLYA],
        )
        self.get_olya_balance.assert_awaited_once()
        self.assertEqual(payload["olya_balance"], "2700")

    async def test_wire_only_without_expenses_no_olya_line(self):
        payload = await self._run(_provesti_data(
            payments=[{"method": "Расчётный", "amount": "3500"}],
        ))

        self.assertEqual(
            [c.kwargs["cash_holder"] for c in self.record_income.await_args_list],
            [CASH_HOLDER_DIMA],
        )
        self.get_olya_balance.assert_not_awaited()
        self.assertIsNone(payload.get("olya_balance"))

    async def test_wire_with_expense_touches_olya(self):
        payload = await self._run(
            _provesti_data(
                payments=[{"method": "Расчётный", "amount": "3500"}],
                expenses=[{"category": "Химия", "amount": "300"}],
            ),
            olya_balance=D("-300"),
        )

        self.get_olya_balance.assert_awaited_once()
        self.assertEqual(payload["olya_balance"], "-300")

    async def test_gift_certificate_only_no_olya_line(self):
        # сертификат в кассу не пишется — деньги Оли не задеты
        payload = await self._run(_provesti_data(
            payments=[{"method": CLEANING_GIFT_CERT_LABEL, "amount": "3500"}],
        ))

        self.record_income.assert_not_awaited()
        self.get_olya_balance.assert_not_awaited()
        self.assertIsNone(payload.get("olya_balance"))


# ---------- очередь → сообщение в чат (bot.py) ----------


def _queued_payload(**over):
    base = {
        "order_id": 5,
        "foreman_name": "Ольга Бригадир",
        "client_phone": "79161234567",
        "client_name": "Иван",
        "comment": None,
        "total_amount": "3500",
        "payments": [{"method": "Наличные", "amount": "3500"}],
        "expenses": [],
        "bonuses_used": "0",
        "bonuses_earned": "175",
        "profit": "3500",
        "balance_after": "50000",
    }
    base.update(over)
    return base


class DispatchCleaningReportOlyaTests(unittest.IsolatedAsyncioTestCase):
    async def _dispatch(self, payload):
        send = AsyncMock()
        with mock.patch.object(bot, "send_cleaning_money_flow", send):
            await bot._dispatch_cleaning_order_report(5, payload, None)
        send.assert_awaited_once()
        return send.await_args.args[1]

    async def test_payload_with_olya_balance_adds_line(self):
        text = await self._dispatch(_queued_payload(olya_balance="12500"))
        self.assertIn("Деньги Оли: 12 500₽", text)

    async def test_payload_with_null_olya_balance_no_line(self):
        text = await self._dispatch(_queued_payload(olya_balance=None))
        self.assertNotIn("Деньги Оли", text)

    async def test_old_payload_without_key_no_line(self):
        # строки очереди, поставленные до выката, поля не знают
        text = await self._dispatch(_queued_payload())
        self.assertNotIn("Деньги Оли", text)
        self.assertIn("Уборка проведена #5", text)


# ---------- кнопка Оли «➖ Добавить расход» ----------


class _TxConn:
    def transaction(self):
        return _NullTx()


class ForemanExpenseOlyaTests(unittest.IsolatedAsyncioTestCase):
    async def test_expense_is_olya_and_message_has_olya_balance(self):
        add_cash_expense = AsyncMock()
        get_olya_balance = AsyncMock(return_value=D("1700"))
        send = AsyncMock()
        state = _State({"amount": "300", "category": "Химия", "comment": "Расход"})
        msg = _Message()
        with mock.patch.object(cleaning_handlers, "add_cash_expense", add_cash_expense), \
             mock.patch.object(cleaning_handlers, "get_olya_balance", get_olya_balance), \
             mock.patch.object(
                 cleaning_handlers, "get_cleaning_balance", AsyncMock(return_value=D("50000"))
             ), \
             mock.patch.object(cleaning_handlers, "send_cleaning_money_flow", send):
            await cleaning_handlers.foreman_expense_confirm(
                msg, state, pool=_Pool(_TxConn()), bot=object()
            )

        self.assertEqual(add_cash_expense.await_args.kwargs["cash_holder"], CASH_HOLDER_OLYA)
        get_olya_balance.assert_awaited_once()
        text = send.await_args.args[1]
        self.assertIn("📒 Касса клининга: Расход", text)
        self.assertEqual(text.split("\n")[-1], "Деньги Оли: 1 700₽")


if __name__ == "__main__":
    unittest.main()
