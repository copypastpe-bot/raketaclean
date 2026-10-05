"""Меню админа, задача 3 (docs/plans/2026-10-05-admin-menu.md): после клининговой
операции с кнопок «Клининг» админ возвращается в своё главное меню.

Приход, расход, перемещение, выплата прибыли, баланс — при успехе, отказе (по
остатку, касса изменилась), «Отмена» и «Отменить»: главное меню админа
(`admin_root_kb()`) и состояние `AdminMenuFSM.root`. У Оли (клинер) ответы и её
клавиатура — как раньше.

Сценарии гоняются через настоящий диспетчер `bot.dp` (`feed_update`), как в
`tests/test_admin_menu_submenus.py` (подделки оттуда же). Без базы: права и
роль — из подделки соединения, запись в кассу и остатки мокнуты, сеть
подменена сессией, которая только записывает запросы.
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
from aiogram.types import Chat, KeyboardButton, Message, ReplyKeyboardMarkup, Update, User

import bot
import cleaning.handlers as cleaning_handlers
from cleaning.admin_ops import CashMoveExceedsBalance
from cleaning.constants import CASH_HOLDER_DIMA, CASH_HOLDER_OLYA
from cleaning.fsm import (
    CleaningCashAddFSM,
    CleaningCashMoveFSM,
    CleaningDividendFSM,
    CleaningForemanExpenseFSM,
)

UNRECOGNIZED = "Команда не распознана. Выберите действие на клавиатуре ниже."
SALARY_PROMPT = "Выберите мастера для расчёта ЗП:"
CLEANING_MAIN_BUTTONS = ["🧹 Провести уборку", "🔍 Клиент", "💰 Баланс", "➖ Добавить расход"]

ROOT_ROWS = [["Операции", "Отчёты"], ["Клиенты", "Клининг"]]

BALANCE_REPLY = "Касса клининга: 50 000₽\nДеньги Ольга: 4 200₽"

# Кнопки главного меню → (ответ, состояние после него).
MAIN_BUTTONS = {
    "Операции": ("Операции: выбери действие.", bot.AdminMenuFSM.operations),
    "Отчёты": ("Отчёты: выбери раздел.", bot.ReportsFSM.waiting_root),
    "Клиенты": (
        "Введите номер телефона клиента (8/ +7/ 9...):",
        bot.AdminClientsFSM.find_wait_phone,
    ),
    "Клининг": ("Клининг: выбери действие.", bot.AdminMenuFSM.cleaning),
}

# Подменю «Операции» — то, что эти кнопки делали в главном меню.
OPERATIONS_BUTTONS = {
    "Приход": ("Выберите способ оплаты:", bot.IncomeFSM.waiting_method),
    "Расход": ("Введите сумму расхода:", bot.ExpenseFSM.waiting_amount),
    "Изъятие": (
        "Выберите мастера, у которого нужно изъять наличные:",
        bot.WithdrawFSM.waiting_master,
    ),
    "Привязать": ("Что привязываем?", bot.WireLinkFSM.waiting_mode),
    "Рассчитать ЗП": (SALARY_PROMPT, bot.AdminPayrollFSM.waiting_master),
}

# Подменю «Клининг» — существующие клининговые сценарии.
CLEANING_BUTTONS = {
    "Приход": (
        "Ручной приход в кассу клининга.\nВыберите метод:",
        CleaningCashAddFSM.method,
    ),
    "Расход": ("Введите сумму расхода:", CleaningForemanExpenseFSM.amount),
    "Баланс": (BALANCE_REPLY, bot.AdminMenuFSM.root),
    "Перемещение": ("Откуда перемещаем?", CleaningCashMoveFSM.source),
    "Выплата прибыли": (
        "Выплата прибыли. В кассе 50 000₽.\nКакую сумму хотите выдать?",
        CleaningDividendFSM.amount,
    ),
}

ALL_TEXTS = sorted(set(MAIN_BUTTONS) | set(OPERATIONS_BUTTONS) | set(CLEANING_BUTTONS))

_user_ids = itertools.count(970_001)
_update_ids = itertools.count(1)


def _rows(markup):
    return [[button.text for button in row] for row in markup.keyboard]


def _kb_texts(markup):
    return [text for row in _rows(markup) for text in row]


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
    """Роль из «staff», права — по функции теста; остальное в сценариях мокнуто."""

    def __init__(self, role, permission):
        self.role = role
        self.permission = permission

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
            return 1 if self.permission(args[1]) else None
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetch(self, query, *args):
        if "FROM staff" in query:
            return []
        raise AssertionError(f"unexpected fetch: {query}")


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

    def permission(self, perm):
        return True

    async def asyncSetUp(self):
        self.session = _RecordingSession()
        self.tg_bot = Bot("123456:TEST-admin-cleaning-return", session=self.session)
        self.user_id = next(_user_ids)
        self.pool = _Pool(_Conn(self.role, self.permission))
        self.fsm = bot.dp.fsm.get_context(
            bot=self.tg_bot, chat_id=self.user_id, user_id=self.user_id
        )
        masters_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Иван")], [KeyboardButton(text="Отмена")]],
            resize_keyboard=True,
        )
        self.cleaning_balance = D("50000")
        self.holder_balances = {CASH_HOLDER_OLYA: D("13500"), CASH_HOLDER_DIMA: D("41024")}
        self.add_cash_income = AsyncMock(return_value=1)
        self.add_cash_expense = AsyncMock(return_value=2)
        self.record_dividend = AsyncMock(return_value=77)
        self.record_cash_move = AsyncMock(return_value=41)
        self.send_flow = AsyncMock()
        patches = [
            mock.patch.object(bot, "pool", self.pool),
            mock.patch.object(
                bot,
                "has_permission",
                AsyncMock(side_effect=lambda uid, perm: self.permission(perm)),
            ),
            mock.patch.object(bot, "build_masters_kb", AsyncMock(return_value=masters_kb)),
            mock.patch.object(
                bot,
                "build_salary_master_kb",
                AsyncMock(return_value=(SALARY_PROMPT, masters_kb)),
            ),
            mock.patch.object(bot, "_link_store_message", AsyncMock()),
            mock.patch.object(bot, "_ensure_pending_wire_on_abort", AsyncMock()),
            mock.patch.object(cleaning_handlers, "add_cash_income", self.add_cash_income),
            mock.patch.object(cleaning_handlers, "add_cash_expense", self.add_cash_expense),
            mock.patch.object(cleaning_handlers, "record_dividend", self.record_dividend),
            mock.patch.object(cleaning_handlers, "record_cash_move", self.record_cash_move),
            mock.patch.object(
                cleaning_handlers,
                "get_cleaning_balance",
                AsyncMock(side_effect=lambda conn: self.cleaning_balance),
            ),
            mock.patch.object(
                cleaning_handlers, "get_olya_balance", AsyncMock(return_value=D("4200"))
            ),
            mock.patch.object(
                cleaning_handlers, "get_dima_balance", AsyncMock(return_value=D("51024"))
            ),
            mock.patch.object(
                cleaning_handlers,
                "get_holder_balance",
                AsyncMock(side_effect=lambda conn, holder: self.holder_balances[holder]),
            ),
            mock.patch.object(cleaning_handlers, "send_cleaning_money_flow", self.send_flow),
            mock.patch.object(
                cleaning_handlers, "configured_recipients", return_value=["Дима", "Женя"]
            ),
            mock.patch.object(cleaning_handlers, "dividend_comment", return_value="Дивиденды"),
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
        return _kb_texts(self.last_markup())

    async def state(self):
        return await self.fsm.get_state()

    async def set_state(self, state):
        await self.fsm.clear()
        if state is not None:
            await self.fsm.set_state(state)

    async def assert_admin_root(self):
        self.assertEqual(_rows(self.last_markup()), ROOT_ROWS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)


# ---------- клининговые операции: админ возвращается в главное меню ----------


class CleaningReturnsAdminToMenuTests(_DispatchCase):
    async def _cash_add_until_confirm(self):
        await self.send("Клининг")
        await self.send("Приход")
        await self.send("Наличные")
        await self.send("1000")
        await self.send("Дима")
        await self.send("-")
        self.assertEqual(await self.state(), CleaningCashAddFSM.confirm.state)

    async def _expense_until_confirm(self):
        await self.send("Клининг")
        await self.send("Расход")
        await self.send("300")
        await self.send("ГСМ")
        await self.send("Дима")
        await self.send("Без комментария")
        self.assertEqual(await self.state(), CleaningForemanExpenseFSM.confirm.state)

    async def _move_until_confirm(self):
        await self.send("Клининг")
        await self.send("Перемещение")
        await self.send("Деньги Ольга")
        await self.send("1000")
        await self.send("-")
        self.assertEqual(await self.state(), CleaningCashMoveFSM.confirm.state)

    async def _dividend_until_confirm(self):
        await self.send("Клининг")
        await self.send("Выплата прибыли")
        await self.send("1000")
        await self.send("Дима")
        self.assertEqual(await self.state(), CleaningDividendFSM.confirm.state)

    def _flows(self):
        return (
            ("приход", self._cash_add_until_confirm),
            ("расход", self._expense_until_confirm),
            ("перемещение", self._move_until_confirm),
            ("выплата", self._dividend_until_confirm),
        )

    async def test_cash_add_success(self):
        await self._cash_add_until_confirm()
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Приход зачислен. Касса: 50 000₽"])
        await self.assert_admin_root()
        self.add_cash_income.assert_awaited_once()

    async def test_expense_success(self):
        await self._expense_until_confirm()
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Расход списан. Касса: 50 000₽"])
        await self.assert_admin_root()
        self.add_cash_expense.assert_awaited_once()

    async def test_move_success(self):
        await self._move_until_confirm()
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Перемещение #41 проведено."])
        await self.assert_admin_root()

    async def test_move_refused_on_source_balance(self):
        self.holder_balances[CASH_HOLDER_OLYA] = D("0")
        await self.send("Клининг")
        await self.send("Перемещение")
        replies = await self.send("Деньги Ольга")
        self.assertEqual(replies, ["В «Деньги Ольга» 0₽ — перемещать нечего."])
        await self.assert_admin_root()

    async def test_move_refused_on_provesti(self):
        await self._move_until_confirm()
        self.record_cash_move.side_effect = CashMoveExceedsBalance(CASH_HOLDER_OLYA, D("500"))
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Нельзя больше остатка: в «Деньги Ольга» 500₽."])
        await self.assert_admin_root()

    async def test_dividend_success(self):
        await self._dividend_until_confirm()
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Выплата #77 проведена. Касса клининга: 50 000₽"])
        await self.assert_admin_root()
        self.record_dividend.assert_awaited_once()

    async def test_dividend_refused_when_cash_changed(self):
        await self._dividend_until_confirm()
        self.cleaning_balance = D("500")
        replies = await self.send("Провести")
        self.assertEqual(
            replies,
            ["Касса изменилась, выплата не проведена. Сейчас в кассе 500₽.\nНачните заново."],
        )
        await self.assert_admin_root()
        self.record_dividend.assert_not_awaited()

    async def test_balance(self):
        await self.send("Клининг")
        replies = await self.send("Баланс")
        self.assertEqual(replies, [BALANCE_REPLY])
        await self.assert_admin_root()

    async def test_otmenit_on_confirm(self):
        for name, until_confirm in self._flows():
            with self.subTest(flow=name):
                await self.set_state(None)
                await until_confirm()
                replies = await self.send("Отменить")
                self.assertEqual(replies, ["Отменено."])
                await self.assert_admin_root()

    async def test_otmena_on_confirm(self):
        for name, until_confirm in self._flows():
            with self.subTest(flow=name):
                await self.set_state(None)
                await until_confirm()
                replies = await self.send("Отмена")
                self.assertEqual(replies, ["Отменено."])
                await self.assert_admin_root()

    async def test_otmena_on_first_step(self):
        for text in ("Приход", "Расход", "Перемещение", "Выплата прибыли"):
            with self.subTest(button=text):
                await self.set_state(bot.AdminMenuFSM.cleaning)
                await self.send(text)
                replies = await self.send("Отмена")
                self.assertEqual(replies, ["Отменено."])
                await self.assert_admin_root()

    async def test_next_button_works_after_operation(self):
        await self._cash_add_until_confirm()
        await self.send("Провести")
        replies = await self.send("Клининг")
        self.assertEqual(replies, ["Клининг: выбери действие."])
        self.assertEqual(await self.state(), bot.AdminMenuFSM.cleaning.state)

    async def test_nothing_recorded_on_cancel(self):
        for name, until_confirm in self._flows():
            await self.set_state(None)
            await until_confirm()
            await self.send("Отменить")
        self.add_cash_income.assert_not_awaited()
        self.add_cash_expense.assert_not_awaited()
        self.record_cash_move.assert_not_awaited()
        self.record_dividend.assert_not_awaited()
        self.send_flow.assert_not_awaited()


class SuperadminReturnsToMenuTests(CleaningReturnsAdminToMenuTests):
    role = "superadmin"


# ---------- Оля: всё как раньше ----------


class OlyaUnchangedTests(_DispatchCase):
    role = "cleaner"

    def permission(self, perm):
        return perm.startswith("cleaning_") and perm not in {
            "cleaning_manage_cash", "cleaning_pay_dividend", "cleaning_view_reports",
            "cleaning_cancel_orders",
        }

    async def _expense_until_confirm(self):
        self.assertEqual(await self.send("➖ Добавить расход"), ["Введите сумму расхода:"])
        await self.send("300")
        await self.send("ГСМ")
        replies = await self.send("Без комментария")
        self.assertEqual(
            replies,
            ["Подтвердите расход:\nКатегория: ГСМ\nСумма: 300₽\nКомментарий: Расход"],
        )
        self.assertEqual(_kb_texts(self.last_markup()), ["Провести", "Отменить"])

    async def test_expense_success_keeps_her_keyboard(self):
        await self._expense_until_confirm()
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Расход списан. Касса: 50 000₽"])
        self.assertEqual(self.last_markup_texts(), CLEANING_MAIN_BUTTONS)
        self.assertIsNone(await self.state())
        self.assertEqual(
            self.add_cash_expense.await_args.kwargs["cash_holder"], CASH_HOLDER_OLYA
        )

    async def test_otmenit_keeps_her_keyboard(self):
        await self._expense_until_confirm()
        replies = await self.send("Отменить")
        self.assertEqual(replies, ["Отменено."])
        self.assertEqual(self.last_markup_texts(), CLEANING_MAIN_BUTTONS)
        self.assertIsNone(await self.state())

    async def test_otmena_keeps_her_keyboard(self):
        await self._expense_until_confirm()
        replies = await self.send("Отмена")
        self.assertEqual(replies, ["Отменено."])
        self.assertEqual(self.last_markup_texts(), CLEANING_MAIN_BUTTONS)
        self.assertIsNone(await self.state())

    async def test_balance_as_before(self):
        replies = await self.send("💰 Баланс")
        self.assertEqual(replies, ["Касса клининга: 50 000₽\nУ вас на руках: 4 200₽"])
        self.assertIsNone(self.last_markup())
        self.assertIsNone(await self.state())

    async def test_admin_texts_are_not_her_buttons(self):
        for text in ALL_TEXTS:
            if text == "Отчёты":
                continue
            with self.subTest(button=text):
                await self.set_state(None)
                replies = await self.send(text)
                self.assertEqual(replies, [UNRECOGNIZED])
                self.assertEqual(self.last_markup_texts(), CLEANING_MAIN_BUTTONS)
                self.assertIsNone(await self.state())



if __name__ == "__main__":
    unittest.main()
