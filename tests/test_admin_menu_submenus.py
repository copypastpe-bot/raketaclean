"""Меню админа, задача 3 (docs/plans/2026-10-05-admin-menu.md): кнопки внизу у админа.

Главное меню: `[Операции, Отчёты]`, `[Клиенты, Клининг]`. Подменю «Операции»:
`[Приход, Расход, Изъятие]`, `[Привязать, Рассчитать ЗП]`, `[Назад]`; подменю
«Клининг»: `[Приход, Расход, Баланс]`, `[Перемещение, Выплата прибыли]`, `[Назад]`.
«Приход» и «Расход» различает состояние подменю; без него — «Выберите раздел:».
Кнопки «Мастера» нет, команды мастеров работают. Возврат админа в главное меню
после клининговых операций — `tests/test_admin_menu_cleaning_return.py`.

Сценарии гоняются через настоящий диспетчер `bot.dp` (`feed_update`), как в
`tests/test_admin_menu_buttons.py`. Без базы: права и роль — из подделки
соединения, запись в кассу и остатки мокнуты, сеть подменена сессией, которая
только записывает запросы.
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
from cleaning.constants import CASH_HOLDER_DIMA, CASH_HOLDER_OLYA
from cleaning.fsm import (
    CleaningCashAddFSM,
    CleaningCashMoveFSM,
    CleaningDividendFSM,
    CleaningForemanExpenseFSM,
)

UNRECOGNIZED = "Команда не распознана. Выберите действие на клавиатуре ниже."
PICK_SECTION = "Выберите раздел:"
ADMIN_MENU = "Меню администратора:"
SALARY_PROMPT = "Выберите мастера для расчёта ЗП:"
CLEANING_MAIN_BUTTONS = ["🧹 Провести уборку", "🔍 Клиент", "💰 Баланс", "➖ Добавить расход"]

ROOT_ROWS = [["Операции", "Отчёты"], ["Клиенты", "Клининг"]]
OPERATIONS_ROWS = [["Приход", "Расход", "Изъятие"], ["Привязать", "Рассчитать ЗП"], ["Назад"]]
CLEANING_ROWS = [["Приход", "Расход", "Баланс"], ["Перемещение", "Выплата прибыли"], ["Назад"]]

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

# Кнопки с уникальным текстом: срабатывают без состояния и в любом состоянии меню.
UNIQUE_BUTTONS = {
    **MAIN_BUTTONS,
    **{t: v for t, v in OPERATIONS_BUTTONS.items() if t not in {"Приход", "Расход"}},
    **{t: v for t, v in CLEANING_BUTTONS.items() if t not in {"Приход", "Расход"}},
}

MENU_STATES = (
    None,
    bot.AdminMenuFSM.root,
    bot.AdminMenuFSM.operations,
    bot.AdminMenuFSM.cleaning,
)

ALL_TEXTS = sorted(set(MAIN_BUTTONS) | set(OPERATIONS_BUTTONS) | set(CLEANING_BUTTONS))

_user_ids = itertools.count(960_001)
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
        self.tg_bot = Bot("123456:TEST-admin-submenus", session=self.session)
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


# ---------- раскладка ----------


class LayoutTests(unittest.TestCase):
    def test_root(self):
        self.assertEqual(_rows(bot.admin_root_kb()), ROOT_ROWS)

    def test_operations(self):
        self.assertEqual(_rows(bot.admin_operations_kb()), OPERATIONS_ROWS)

    def test_cleaning(self):
        self.assertEqual(_rows(bot.admin_cleaning_kb()), CLEANING_ROWS)

    def test_tables_cover_every_button(self):
        self.assertEqual(sorted(MAIN_BUTTONS), sorted(_kb_texts(bot.admin_root_kb())))
        self.assertEqual(
            sorted([*OPERATIONS_BUTTONS, "Назад"]),
            sorted(_kb_texts(bot.admin_operations_kb())),
        )
        self.assertEqual(
            sorted([*CLEANING_BUTTONS, "Назад"]),
            sorted(_kb_texts(bot.admin_cleaning_kb())),
        )

    def test_no_masters_section(self):
        self.assertFalse(hasattr(bot.AdminMenuFSM, "masters"))
        for name in (
            "AdminMastersFSM",
            "admin_masters_kb",
            "admin_masters_remove_kb",
            "admin_masters_root",
            "admin_masters_list",
            "admin_masters_add",
            "admin_masters_remove_start",
            "admin_masters_remove_phone",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(bot, name))


# ---------- главное меню и подменю ----------


class SubmenuEntryTests(_DispatchCase):
    async def test_operations_opens_submenu(self):
        replies = await self.send("Операции")
        self.assertEqual(replies, ["Операции: выбери действие."])
        self.assertEqual(_rows(self.last_markup()), OPERATIONS_ROWS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.operations.state)

    async def test_cleaning_opens_submenu(self):
        replies = await self.send("Клининг")
        self.assertEqual(replies, ["Клининг: выбери действие."])
        self.assertEqual(_rows(self.last_markup()), CLEANING_ROWS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.cleaning.state)

    async def test_back_returns_to_main_menu(self):
        for submenu in ("Операции", "Клининг"):
            with self.subTest(submenu=submenu):
                await self.set_state(None)
                await self.send(submenu)
                replies = await self.send("Назад")
                self.assertEqual(replies, [ADMIN_MENU])
                await self.assert_admin_root()

    async def test_operations_buttons_lead_to_their_scenarios(self):
        for text, (reply, state) in OPERATIONS_BUTTONS.items():
            with self.subTest(button=text):
                await self.set_state(bot.AdminMenuFSM.operations)
                replies = await self.send(text)
                self.assertEqual(replies, [reply])
                self.assertEqual(await self.state(), state.state)

    async def test_cleaning_buttons_lead_to_their_scenarios(self):
        for text, (reply, state) in CLEANING_BUTTONS.items():
            with self.subTest(button=text):
                await self.set_state(bot.AdminMenuFSM.cleaning)
                replies = await self.send(text)
                self.assertEqual(replies, [reply])
                self.assertEqual(await self.state(), state.state)

    async def test_income_and_expense_differ_by_submenu(self):
        for text in ("Приход", "Расход"):
            with self.subTest(button=text):
                await self.set_state(bot.AdminMenuFSM.operations)
                await self.send(text)
                operations_state = await self.state()
                await self.set_state(bot.AdminMenuFSM.cleaning)
                await self.send(text)
                cleaning_state = await self.state()
                self.assertEqual(operations_state, OPERATIONS_BUTTONS[text][1].state)
                self.assertEqual(cleaning_state, CLEANING_BUTTONS[text][1].state)
                self.assertNotEqual(operations_state, cleaning_state)

    async def test_income_and_expense_without_submenu_ask_section(self):
        # После перезапуска (None) и в главном меню (root) раздел неизвестен.
        for start in (None, bot.AdminMenuFSM.root):
            for text in ("Приход", "Расход"):
                with self.subTest(state=start, button=text):
                    await self.set_state(start)
                    replies = await self.send(text)
                    self.assertEqual(replies, [PICK_SECTION])
                    await self.assert_admin_root()

    async def test_unique_buttons_work_from_any_menu_state(self):
        # Правило кнопок админа: без /start и из любого подменю.
        for start in MENU_STATES:
            for text, (reply, state) in UNIQUE_BUTTONS.items():
                with self.subTest(state=start, button=text):
                    await self.set_state(start)
                    replies = await self.send(text)
                    self.assertEqual(replies, [reply])
                    self.assertEqual(await self.state(), state.state)

    async def test_other_text_in_submenu_keeps_submenu(self):
        for state, rows in (
            (bot.AdminMenuFSM.operations, OPERATIONS_ROWS),
            (bot.AdminMenuFSM.cleaning, CLEANING_ROWS),
        ):
            with self.subTest(state=state.state):
                await self.set_state(state)
                replies = await self.send("что-то непонятное")
                self.assertEqual(replies, ["Выберите действие на клавиатуре ниже."])
                self.assertEqual(_rows(self.last_markup()), rows)
                self.assertEqual(await self.state(), state.state)

    async def test_masters_text_is_not_a_button_any_more(self):
        replies = await self.send("Мастера")
        self.assertEqual(replies, [UNRECOGNIZED])
        await self.assert_admin_root()


class ButtonTextInsideCleaningScenarioTests(_DispatchCase):
    async def test_not_intercepted(self):
        # Посреди клинингового сценария текст любой кнопки — ответ на шаг сценария.
        # «Отчёты» без фильтра состояния ловится везде, как раньше, — его не берём.
        for text in ALL_TEXTS + ["Назад"]:
            if text == "Отчёты":
                continue
            with self.subTest(button=text):
                await self.set_state(CleaningCashAddFSM.amount)
                await self.fsm.set_data({"method": "Наличные"})
                replies = await self.send(text)
                self.assertEqual(replies, ["Нужно число > 0."])
                self.assertEqual(await self.state(), CleaningCashAddFSM.amount.state)


# ---------- команды мастеров работают без кнопки «Мастера» ----------


class MasterCommandsTests(_DispatchCase):
    async def test_add_master(self):
        replies = await self.send("/add_master")
        self.assertEqual(replies, ["Введите tg id мастера (число):"])
        self.assertEqual(await self.state(), bot.AddMasterFSM.waiting_tg_id.state)

    async def test_add_master_cancel_returns_to_admin_menu(self):
        await self.send("/add_master")
        replies = await self.send("Отмена")
        self.assertEqual(replies, ["Добавление мастера отменено."])
        await self.assert_admin_root()

    async def test_list_masters(self):
        self.assertEqual(await self.send("/list_masters"), ["Активных мастеров нет."])

    async def test_masters_all(self):
        self.assertEqual(await self.send("/masters_all"), ["В базе мастеров не найдено."])

    async def test_remove_master(self):
        self.assertEqual(
            await self.send("/remove_master"), ["Формат: /remove_master <tg_user_id>"]
        )


if __name__ == "__main__":
    unittest.main()
