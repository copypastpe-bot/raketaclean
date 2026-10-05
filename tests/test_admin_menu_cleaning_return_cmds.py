"""Правки по сверке задачи 3 (docs/plans/2026-10-05-admin-menu.md), пункт П2
(`.superpowers/sdd/2026-10-05-admin-menu/task-3-fixes.md`): команды кассы
клининга, доступные только админу (`cleaning_manage_cash` / `cleaning_cancel_orders`,
их нет у Оли — см. `OlyaUnchangedTests.permission` в
`tests/test_admin_menu_cleaning_return.py`), после ответа возвращают его в
главное меню (`admin_root_kb()`, состояние `AdminMenuFSM.root`), а не убирают
клавиатуру / требуют `/start`.

Покрыты: `/cleaning_cash_withdrawal` (успех), `/cleaning_dividend_cancel`
(«не найдена» и успех), `/cleaning_cancel_order` («не найден» и успех).
Отдельного теста на Олю нет — ей эти команды недоступны, доходит только
«Команда доступна только администраторам.» без смены клавиатуры (что и
проверяет `NoPermission`).

Сценарии гоняются через настоящий диспетчер `bot.dp` (`feed_update`), как в
`tests/test_admin_menu_cleaning_return.py` (та же подделка соединения). Без
базы: запись и остатки мокнуты, сеть подменена сессией, которая только
записывает запросы.
"""

import itertools
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from unittest import mock
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

import bot
import cleaning.handlers as cleaning_handlers
from cleaning.constants import CASH_HOLDER_DIMA, CASH_HOLDER_OLYA

ADMIN_ROOT_BUTTONS = [b.text for row in bot.admin_root_kb().keyboard for b in row]

_user_ids = itertools.count(980_001)
_update_ids = itertools.count(1)


class _RecordingSession(BaseSession):
    """Вместо Telegram: запоминает запросы, ничего не отправляет."""

    def __init__(self):
        super().__init__()
        self.sent = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        return None

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True):  # pragma: no cover
        if False:
            yield b""

    async def close(self):
        pass


class _NullTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    """Роль из «staff», право — по флагу; остальное в сценариях мокнуто."""

    def __init__(self, role, allowed):
        self.role = role
        self.allowed = allowed

    def transaction(self):
        return _NullTx()

    async def fetchrow(self, query, *args):
        if "FROM staff" in query:
            return {"role": self.role} if self.role else None
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetchval(self, query, *args):
        if "FROM staff" in query:
            return self.role
        if "role_permissions" in query:
            return 1 if self.allowed else None
        raise AssertionError(f"unexpected fetchval: {query}")


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


class _DispatchCase(unittest.IsolatedAsyncioTestCase):
    """Один пользователь гонит сообщения через `bot.dp`."""

    role = "admin"
    allowed = True

    async def asyncSetUp(self):
        self.session = _RecordingSession()
        self.tg_bot = Bot("123456:TEST-admin-cleaning-return-cmds", session=self.session)
        self.user_id = next(_user_ids)
        self.pool = _Pool(_Conn(self.role, self.allowed))
        self.fsm = bot.dp.fsm.get_context(
            bot=self.tg_bot, chat_id=self.user_id, user_id=self.user_id
        )
        self.add_cash_withdrawal = AsyncMock(return_value=9)
        self.cancel_dividend = AsyncMock(
            return_value={"amount": D("1000"), "cash_holder": CASH_HOLDER_DIMA}
        )
        self.cancel_order = AsyncMock(
            return_value={
                "order_id": 812,
                "address": "Мира, 10",
                "total_amount": D("6000"),
                "bonuses_used": 0,
                "bonuses_earned": 0,
                "cashbook_rows_deleted": 2,
                "olya_rows_deleted": 0,
            }
        )
        self.send_flow = AsyncMock()
        patches = [
            mock.patch.object(bot, "pool", self.pool),
            mock.patch.object(
                cleaning_handlers, "add_cash_withdrawal", self.add_cash_withdrawal
            ),
            mock.patch.object(cleaning_handlers, "cancel_dividend", self.cancel_dividend),
            mock.patch.object(cleaning_handlers, "cancel_order", self.cancel_order),
            mock.patch.object(
                cleaning_handlers,
                "get_cleaning_balance",
                AsyncMock(return_value=D("50000")),
            ),
            mock.patch.object(
                cleaning_handlers, "get_olya_balance", AsyncMock(return_value=D("4200"))
            ),
            mock.patch.object(cleaning_handlers, "send_cleaning_money_flow", self.send_flow),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.fsm.clear()

    async def send(self, text):
        """Сообщение от пользователя; ответы бота на него — списком текстов."""
        before = len(self.session.sent)
        update = Update(
            update_id=next(_update_ids),
            message=Message(
                message_id=next(_update_ids),
                date=datetime.now(timezone.utc),
                chat=Chat(id=self.user_id, type="private"),
                from_user=User(id=self.user_id, is_bot=False, first_name="Тест"),
                text=text,
            ),
        )
        await bot.dp.feed_update(self.tg_bot, update, pool=self.pool)
        return [m.text for m in self.session.sent[before:] if isinstance(m, SendMessage)]

    def last_markup(self):
        return [m for m in self.session.sent if isinstance(m, SendMessage)][-1].reply_markup

    def last_markup_texts(self):
        return [b.text for row in self.last_markup().keyboard for b in row]

    async def state(self):
        return await self.fsm.get_state()

    async def assert_admin_root(self):
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)


# ---------- /cleaning_cash_withdrawal ----------


class CashWithdrawalReturnsAdminToMenuTests(_DispatchCase):
    async def test_success(self):
        await self.send("/cleaning_cash_withdrawal")
        await self.send("700")
        await self.send("Дима")
        await self.send("-")
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Изъятие проведено. Касса: 50 000₽"])
        await self.assert_admin_root()
        self.add_cash_withdrawal.assert_awaited_once()
        self.send_flow.assert_awaited_once()


# ---------- /cleaning_dividend_cancel N ----------


class DividendCancelReturnsAdminToMenuTests(_DispatchCase):
    async def test_not_found(self):
        self.cancel_dividend.return_value = None
        await self.send("/cleaning_dividend_cancel 999")
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Выплата #999 не найдена или уже отменена."])
        await self.assert_admin_root()
        self.send_flow.assert_not_awaited()

    async def test_success(self):
        await self.send("/cleaning_dividend_cancel 77")
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Выплата #77 отменена. Касса клининга: 50 000₽"])
        await self.assert_admin_root()
        self.cancel_dividend.assert_awaited_once()
        self.send_flow.assert_awaited_once()


# ---------- /cleaning_cancel_order N ----------


class CancelOrderReturnsAdminToMenuTests(_DispatchCase):
    async def test_not_found(self):
        self.cancel_order.return_value = None
        await self.send("/cleaning_cancel_order 812")
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Заказ #812 не найден или уже отменён."])
        await self.assert_admin_root()
        self.send_flow.assert_not_awaited()

    async def test_success(self):
        await self.send("/cleaning_cancel_order 812")
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Заказ #812 отменён. Касса: 50 000₽"])
        await self.assert_admin_root()
        self.cancel_order.assert_awaited_once()
        self.send_flow.assert_awaited_once()


class SuperadminCashWithdrawalReturnsToMenuTests(CashWithdrawalReturnsAdminToMenuTests):
    role = "superadmin"


if __name__ == "__main__":
    unittest.main()
