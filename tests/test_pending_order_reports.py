"""Отложенная отправка отчёта в чат и сообщения о деньгах (bot.py).

Задача 5 ТЗ «адреса до конца» (docs/plans/2026-09-16-addresses.md, 2026-09-16):
деньги в cashbook_entries по-прежнему пишутся сразу в commit_order (это не
меняем и не тестируем здесь — см. tests/test_order_address.py и существующие
тесты заказа). Отчёт в ORDERS_CONFIRM_CHAT_ID и сообщение в «Ракета деньги»
(MONEY_FLOW_CHAT_ID) теперь ставятся в очередь `pending_order_reports` и
уходят из фонового прохода `run_pending_order_reports`, когда по заказу
появится связка с CRM (`adminbot.amo_links.deal_address`) — с адресом или без
него, — либо когда связка не появилась за 30 минут (предохранитель).
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import bot

ORDERS_CHAT = -100111
MONEY_CHAT = -100222


class FakeConn:
    """pending_order_reports.fetch + adminbot.amo_links.fetchrow + execute-лог."""

    def __init__(self, *, pending_rows=(), links=None, balance=("0", "0")):
        self._pending_rows = list(pending_rows)
        self._links = links or {}
        self._balance = balance
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        if "pending_order_reports" in query:
            return list(self._pending_rows)
        return []

    async def fetchrow(self, query, *args):
        if "adminbot.amo_links" in query:
            return self._links.get(args[0])
        if "income_sum" in query:
            income, expense = self._balance
            return {"income_sum": income, "expense_sum": expense}
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


def _payload(**over):
    base = {
        "order_id": 637,
        "client_display_masked": "Иван …9510",
        "birthday_display": "—",
        "payment_parts": [{"method": "Наличные", "amount": "1500"}],
        "payment_method": "Наличные",
        "cash_payment": "1500",
        "amount_total": "1500",
        "bonus_spent": 0,
        "bonus_earned": 75,
        "upsell": "0",
        "master_names": "Ольга",
        "is_wire_payment": False,
        "has_non_wire_income": True,
        "total_non_wire_amount": "1500",
    }
    base.update(over)
    return base


class BuildOrderReportLinesTests(unittest.TestCase):
    """Чистая функция форматирования: подпись с адресом на месте отправки."""

    def test_address_line_present_when_resolved(self):
        lines = bot._build_order_report_lines(_payload(), "г. Москва, ул. Ленина, д. 5")
        joined = "\n".join(lines)
        self.assertIn("📍 Адрес: г. Москва, ул. Ленина, д. 5", joined)
        self.assertIn("Заказ №637", joined)

    def test_address_line_absent_when_not_resolved(self):
        lines = bot._build_order_report_lines(_payload(), None)
        joined = "\n".join(lines)
        self.assertNotIn("📍 Адрес", joined)

    def test_wire_payment_note(self):
        lines = bot._build_order_report_lines(
            _payload(payment_method="р/с", has_non_wire_income=False), None
        )
        self.assertIn("💼 Оплата по р/с (ожидаем поступление)", "\n".join(lines))

    def test_falls_back_to_payment_method_when_no_parts(self):
        lines = bot._build_order_report_lines(_payload(payment_parts=[]), None)
        joined = "\n".join(lines).replace("\xa0", " ")
        self.assertIn("Наличные — 1 500", joined)


class EnqueueOrderReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_inserts_row_with_jsonb_payload(self):
        conn = FakeConn()
        pool_ = FakePool(conn)

        from decimal import Decimal

        await bot._enqueue_order_report(
            pool_,
            order_id=777,
            client_display_masked="Иван …9510",
            birthday_display="—",
            payment_parts=[{"method": "Наличные", "amount": "1000"}],
            payment_method="Наличные",
            cash_payment=Decimal("1000"),
            amount_total=Decimal("1000"),
            bonus_spent=0,
            bonus_earned=50,
            upsell=Decimal("0"),
            master_names="Ольга",
            is_wire_payment=False,
            has_non_wire_income=True,
            total_non_wire_amount=Decimal("1000"),
        )

        self.assertEqual(len(conn.executed), 1)
        query, args = conn.executed[0]
        self.assertIn("INSERT INTO pending_order_reports", query)
        self.assertEqual(args[0], 777)
        payload = json.loads(args[1])
        self.assertEqual(payload["order_id"], 777)
        self.assertEqual(payload["master_names"], "Ольга")
        self.assertEqual(payload["has_non_wire_income"], True)


class RunPendingOrderReportsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        chat_patches = [
            mock.patch.object(bot, "ORDERS_CONFIRM_CHAT_ID", ORDERS_CHAT),
            mock.patch.object(bot, "MONEY_FLOW_CHAT_ID", MONEY_CHAT),
            mock.patch.object(bot, "DEAL_LINK_WAIT_TIMEOUT_SEC", 1800),
        ]
        for p in chat_patches:
            p.start()
            self.addCleanup(p.stop)
        send_patch = mock.patch.object(bot.bot, "send_message", mock.AsyncMock())
        self.send_message = send_patch.start()
        self.addCleanup(send_patch.stop)

    def _install_pool(self, conn):
        pool_patch = mock.patch.object(bot, "pool", FakePool(conn))
        pool_patch.start()
        self.addCleanup(pool_patch.stop)

    def _chats_sent_to(self):
        return [call.args[0] for call in self.send_message.call_args_list]

    async def test_no_pool_is_noop(self):
        with mock.patch.object(bot, "pool", None):
            await bot.run_pending_order_reports()
        self.send_message.assert_not_awaited()

    async def test_link_with_address_sends_report_and_money_with_street(self):
        now = datetime.now(timezone.utc)
        conn = FakeConn(
            pending_rows=[{"order_id": 637, "payload": _payload(order_id=637), "created_at": now}],
            links={637: {"path": "A", "deal_address": "Ленина, 5"}},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        self.assertEqual(self._chats_sent_to().count(ORDERS_CHAT), 1)
        self.assertEqual(self._chats_sent_to().count(MONEY_CHAT), 1)
        report_text = next(
            call.args[1] for call in self.send_message.call_args_list if call.args[0] == ORDERS_CHAT
        )
        self.assertIn("📍 Адрес: Ленина, 5", report_text)
        money_text = next(
            call.args[1] for call in self.send_message.call_args_list if call.args[0] == MONEY_CHAT
        )
        self.assertIn("Ленина / Заказ №637", money_text)
        # отправленная запись отмечена и не уйдёт повторно
        self.assertTrue(any("SET sent_at" in q for q, _ in conn.executed))

    async def test_link_without_address_sends_with_masked_name(self):
        now = datetime.now(timezone.utc)
        conn = FakeConn(
            pending_rows=[{"order_id": 638, "payload": _payload(order_id=638), "created_at": now}],
            links={638: {"path": "A", "deal_address": None}},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        report_text = next(
            call.args[1] for call in self.send_message.call_args_list if call.args[0] == ORDERS_CHAT
        )
        self.assertNotIn("📍 Адрес", report_text)
        money_text = next(
            call.args[1] for call in self.send_message.call_args_list if call.args[0] == MONEY_CHAT
        )
        self.assertIn("Иван …9510 / Заказ №638", money_text)

    async def test_no_link_before_timeout_waits(self):
        now = datetime.now(timezone.utc)
        conn = FakeConn(
            pending_rows=[{"order_id": 639, "payload": _payload(order_id=639), "created_at": now - timedelta(minutes=5)}],
            links={},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        self.send_message.assert_not_awaited()
        self.assertEqual(conn.executed, [])

    async def test_no_link_after_timeout_sends_without_address(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=31)
        conn = FakeConn(
            pending_rows=[{"order_id": 640, "payload": _payload(order_id=640), "created_at": old}],
            links={},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        report_text = next(
            call.args[1] for call in self.send_message.call_args_list if call.args[0] == ORDERS_CHAT
        )
        self.assertNotIn("📍 Адрес", report_text)
        money_text = next(
            call.args[1] for call in self.send_message.call_args_list if call.args[0] == MONEY_CHAT
        )
        self.assertIn("Иван …9510 / Заказ №640", money_text)
        self.assertTrue(any("SET sent_at" in q for q, _ in conn.executed))

    async def test_wire_only_order_has_no_money_message(self):
        now = datetime.now(timezone.utc)
        conn = FakeConn(
            pending_rows=[{
                "order_id": 641,
                "payload": _payload(order_id=641, has_non_wire_income=False, is_wire_payment=True),
                "created_at": now,
            }],
            links={641: {"path": "A", "deal_address": "Мира, 1"}},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        self.assertEqual(self._chats_sent_to().count(ORDERS_CHAT), 1)
        self.assertEqual(self._chats_sent_to().count(MONEY_CHAT), 0)


if __name__ == "__main__":
    unittest.main()
