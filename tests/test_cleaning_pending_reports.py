"""Сообщение о проведённой уборке ждёт связки — очередь `pending_order_reports`
делит на два контура явным признаком `kind` (bot.py).

ТЗ 2026-09-17 «адреса в клининге (уборки)», задачи 3 (очередь) и 4 (сигналы
в LOGS_CHAT_ID). Очередь та же, что и у химчистки (docs/plans/2026-09-16-addresses.md,
задача 5, tests/test_pending_order_reports.py) — здесь проверяется только то,
что у неё появилось: колонка `kind`, чтение `adminbot.cleaning_links` вместо
`adminbot.amo_links`, свой формат сообщения (`cleaning.format.format_order_provided_alert`)
и то, что пересекающиеся номера («заказ №5» и «уборка №5») не путаются.

Деньги в кассу клининга пишутся сразу в `do_provesti` — это не меняется и не
тестируется здесь.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock
from unittest.mock import AsyncMock

import bot
import cleaning.handlers as cleaning_handlers
import cleaning.notify as cleaning_notify
from cleaning.handlers import _enqueue_cleaning_order_report

CLEANING_CHAT = -100444
ORDERS_CHAT = -100111
MONEY_CHAT = -100222
LOGS_CHAT = -100333


def _cleaning_payload(**over):
    base = {
        "order_id": 5,
        "foreman_name": "Ольга Бригадир",
        "client_phone": "79161234567",
        "client_name": "Иван",
        "comment": "домофон 55",
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


def _order_payload(**over):
    base = {
        "order_id": 5,
        "client_display_masked": "Пётр …4321",
        "client_phone": "79261234567",
        "birthday_display": "—",
        "payment_parts": [{"method": "Наличные", "amount": "2000"}],
        "payment_method": "Наличные",
        "cash_payment": "2000",
        "amount_total": "2000",
        "bonus_spent": 0,
        "bonus_earned": 100,
        "upsell": "0",
        "master_names": "Сергей",
        "is_wire_payment": False,
        "has_non_wire_income": False,  # без сообщения в MONEY_CHAT — короче ассерты
        "total_non_wire_amount": "0",
    }
    base.update(over)
    return base


class FakeConn:
    """pending_order_reports.fetch + связки обоих контуров + execute-лог."""

    def __init__(self, *, pending_rows=(), amo_links=None, cleaning_links=None):
        self._pending_rows = list(pending_rows)
        self._amo_links = amo_links or {}
        self._cleaning_links = cleaning_links or {}
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        if "pending_order_reports" in query:
            return list(self._pending_rows)
        return []

    async def fetchrow(self, query, *args):
        if "adminbot.cleaning_links" in query:
            return self._cleaning_links.get(args[0])
        if "adminbot.amo_links" in query:
            return self._amo_links.get(args[0])
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


class EnqueueCleaningOrderReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_inserts_row_with_cleaning_kind_and_jsonb_payload(self):
        conn = FakeConn()

        from decimal import Decimal

        await _enqueue_cleaning_order_report(
            conn,
            order_id=9,
            foreman_name="Ольга",
            client_phone="79161234567",
            client_name="Иван",
            comment="домофон 55",
            total_amount=Decimal("3500"),
            payments=[{"method": "Наличные", "amount": "3500"}],
            expenses=[],
            bonuses_used=Decimal("0"),
            bonuses_earned=Decimal("175"),
            profit=Decimal("3500"),
            balance_after=Decimal("50000"),
        )

        self.assertEqual(len(conn.executed), 1)
        query, args = conn.executed[0]
        self.assertIn("INSERT INTO pending_order_reports", query)
        self.assertIn("'cleaning'", query)
        self.assertEqual(args[0], 9)
        payload = json.loads(args[1])
        self.assertEqual(payload["order_id"], 9)
        self.assertEqual(payload["foreman_name"], "Ольга")
        self.assertEqual(payload["comment"], "домофон 55")


class RunPendingOrderReportsCleaningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patches = [
            mock.patch.object(bot, "ORDERS_CONFIRM_CHAT_ID", ORDERS_CHAT),
            mock.patch.object(bot, "MONEY_FLOW_CHAT_ID", MONEY_CHAT),
            mock.patch.object(bot, "LOGS_CHAT_ID", LOGS_CHAT),
            mock.patch.object(bot, "DEAL_LINK_WAIT_TIMEOUT_SEC", 1800),
            mock.patch.object(cleaning_notify, "CLEANING_MONEY_FLOW_CHAT_ID", CLEANING_CHAT),
        ]
        for p in patches:
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

    def _text_to(self, chat_id):
        return next(
            call.args[1] for call in self.send_message.call_args_list if call.args[0] == chat_id
        )

    async def test_link_with_address_sends_single_message_with_address(self):
        now = datetime.now(timezone.utc)
        conn = FakeConn(
            pending_rows=[
                {"kind": "cleaning", "order_id": 5, "payload": _cleaning_payload(order_id=5), "created_at": now}
            ],
            cleaning_links={5: {"path": "A", "deal_address": "Мира, 10"}},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        self.assertEqual(self._chats_sent_to(), [CLEANING_CHAT])
        text = self._text_to(CLEANING_CHAT)
        self.assertIn("Уборка проведена #5", text)
        self.assertIn("Адрес: Мира, 10", text)
        self.assertIn("Комментарий: домофон 55", text)
        self.assertTrue(any("SET sent_at" in q for q, _ in conn.executed))
        # обновление отмечает именно кассу клининга, не заказ
        update_query, update_args = conn.executed[-1]
        self.assertEqual(update_args, ("cleaning", 5))

    async def test_link_without_address_sends_without_address_line(self):
        now = datetime.now(timezone.utc)
        conn = FakeConn(
            pending_rows=[
                {"kind": "cleaning", "order_id": 6, "payload": _cleaning_payload(order_id=6), "created_at": now}
            ],
            cleaning_links={6: {"path": "A", "deal_address": None}},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        text = self._text_to(CLEANING_CHAT)
        self.assertNotIn("Адрес", text)
        self.assertNotIn("None", text)

    async def test_no_link_before_timeout_waits(self):
        now = datetime.now(timezone.utc)
        conn = FakeConn(
            pending_rows=[
                {
                    "kind": "cleaning", "order_id": 7,
                    "payload": _cleaning_payload(order_id=7),
                    "created_at": now - timedelta(minutes=5),
                }
            ],
            cleaning_links={},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        self.send_message.assert_not_awaited()
        self.assertEqual(conn.executed, [])

    async def test_no_link_after_timeout_sends_without_address_and_alerts_as_cleaning(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(minutes=31)
        conn = FakeConn(
            pending_rows=[
                {"kind": "cleaning", "order_id": 8, "payload": _cleaning_payload(order_id=8), "created_at": old}
            ],
            cleaning_links={},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        text = self._text_to(CLEANING_CHAT)
        self.assertNotIn("Адрес", text)
        alert_text = self._text_to(LOGS_CHAT)
        self.assertIn("не ответил за 30 минут", alert_text)
        self.assertIn("Уборка №8", alert_text)
        self.assertNotIn("Заказ №8", alert_text)

    async def test_path_c_without_address_alerts_with_cleaning_label(self):
        now = datetime.now(timezone.utc)
        conn = FakeConn(
            pending_rows=[
                {"kind": "cleaning", "order_id": 9, "payload": _cleaning_payload(order_id=9), "created_at": now}
            ],
            cleaning_links={9: {"path": "C", "deal_address": None}},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        alert_text = self._text_to(LOGS_CHAT)
        self.assertIn("Сделка создана без адреса", alert_text)
        self.assertIn("Уборка №9", alert_text)
        self.assertNotIn("Заказ №9", alert_text)
        self.assertIn("79161234567", alert_text)  # телефон — полный, не маскированный
        # уборка всё равно уходит своим чередом, без адреса
        self.assertEqual(self._chats_sent_to().count(CLEANING_CHAT), 1)

    async def test_path_a_without_address_does_not_alert(self):
        now = datetime.now(timezone.utc)
        conn = FakeConn(
            pending_rows=[
                {"kind": "cleaning", "order_id": 10, "payload": _cleaning_payload(order_id=10), "created_at": now}
            ],
            cleaning_links={10: {"path": "A", "deal_address": None}},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        self.assertEqual(self._chats_sent_to().count(LOGS_CHAT), 0)

    async def test_overlapping_order_ids_do_not_cross_talk(self):
        """Заказ №5 (химчистка) и уборка №5 существуют одновременно — очередь
        должна отправить оба, каждый в свой чат, со своим адресом, не перепутав
        связку. Контур — явный признак `kind`, а не номер заказа."""
        now = datetime.now(timezone.utc)
        conn = FakeConn(
            pending_rows=[
                {"kind": "order", "order_id": 5, "payload": _order_payload(order_id=5), "created_at": now},
                {"kind": "cleaning", "order_id": 5, "payload": _cleaning_payload(order_id=5), "created_at": now},
            ],
            amo_links={5: {"path": "A", "deal_address": "Ленина, 1"}},
            cleaning_links={5: {"path": "A", "deal_address": "Мира, 10"}},
        )
        self._install_pool(conn)

        await bot.run_pending_order_reports()

        self.assertEqual(self._chats_sent_to().count(ORDERS_CHAT), 1)
        self.assertEqual(self._chats_sent_to().count(CLEANING_CHAT), 1)
        order_text = self._text_to(ORDERS_CHAT)
        cleaning_text = self._text_to(CLEANING_CHAT)
        self.assertIn("Ленина, 1", order_text)
        self.assertIn("Мира, 10", cleaning_text)
        self.assertNotIn("Мира, 10", order_text)
        self.assertNotIn("Ленина, 1", cleaning_text)
        # оба помечены отправленными раздельно — по (kind, order_id)
        updates = {args for q, args in conn.executed if "SET sent_at" in q}
        self.assertEqual(updates, {("order", 5), ("cleaning", 5)})


class _NullTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ProvestiFakeConn:
    """Минимум, нужный do_provesti: остальные шаги (запись дохода, расхода,
    баланс кассы, уведомления клиенту) — не про эту задачу, они мокнуты."""

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
            # задача 6, проверка 1: адрес в этой самой команде — пуст
            assert args[2] is None, "адрес не должен заполняться при проведении"
            return {"id": self.order_id}
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "INSERT 1"


class _ProvestiFakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _ProvestiFakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _ProvestiFakeAcquire(self.conn)


class _ProvestiFakeMessage:
    def __init__(self):
        self.from_user = type("U", (), {"id": 999})()
        self.answer = AsyncMock()


class _ProvestiFakeState:
    def __init__(self, data):
        self._data = dict(data)

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self._data = {}


class DoProvestiCleaningChainTests(unittest.IsolatedAsyncioTestCase):
    """Задача 6 ТЗ «адреса в клининге»: сквозная проверка цепочки — проверка 1
    (уборка проводится без адреса, в базе он пуст) и проверка 4 (отчёт ставится
    в очередь с явным признаком контура и с комментарием бригадира). Проверка 2
    (админ-бот пишет deal_address) — работа другого репозитория, здесь не
    тестируется; проверка 3 (фоновый бэкфилл) уже покрыта
    tests/test_address_backfill.py:test_cleaning_orders_are_backfilled_too."""

    async def test_provesti_writes_null_address_and_enqueues_cleaning_report(self):
        conn = _ProvestiFakeConn(order_id=777)
        pool = _ProvestiFakePool(conn)
        state = _ProvestiFakeState({
            "client_id": 42,
            "total_amount": "3500",
            "bonus_spend": 0,
            "payments": [{"method": "Наличные", "amount": "3500"}],
            "client_op_id": "op-777",
            "comment": "домофон 55",
            "expenses": [],
            "is_wire_payment": False,
            "phone_norm": "+79161234567",
            "client_name": None,
        })
        msg = _ProvestiFakeMessage()

        with mock.patch.object(cleaning_handlers, "record_income", AsyncMock()), \
             mock.patch.object(cleaning_handlers, "record_expense", AsyncMock()), \
             mock.patch.object(
                 cleaning_handlers, "get_cleaning_balance", AsyncMock(return_value=Decimal("50000"))
             ), \
             mock.patch.object(
                 cleaning_handlers, "_enqueue_cleaning_completed_notifications", AsyncMock()
             ):
            await cleaning_handlers.do_provesti(msg, state, pool=pool)

        # проверка 4: отчёт в очереди — явный признак контура, комментарий на месте
        insert_calls = [
            (q, a) for q, a in conn.executed if "INSERT INTO pending_order_reports" in q
        ]
        self.assertEqual(len(insert_calls), 1)
        query, args = insert_calls[0]
        self.assertIn("'cleaning'", query)
        self.assertEqual(args[0], 777)
        payload = json.loads(args[1])
        self.assertEqual(payload["order_id"], 777)
        self.assertEqual(payload["comment"], "домофон 55")
        self.assertEqual(payload["client_phone"], "+79161234567")
        self.assertTrue(msg.answer.await_args)  # бригадир получил подтверждение сразу


if __name__ == "__main__":
    unittest.main()
