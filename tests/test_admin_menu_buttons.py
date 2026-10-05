"""Меню админа, задача 1 (docs/plans/2026-10-05-admin-menu.md): кнопки главного
меню админа срабатывают без /start.

Баг: кнопки ждали состояния `AdminMenuFSM.root`, а оно терялось (перезапуск,
`cancel_any`, заглушка `unknown`), и до /start всё уходило в «Команда не
распознана». Правило: кнопка срабатывает у админа, когда он не внутри сценария
(состояние None или любое `AdminMenuFSM`). `cancel_any` и `unknown` ставят
админу `AdminMenuFSM.root`. У мастеров и Оли те же тексты — как раньше.

Сценарии гоняются через настоящий диспетчер `bot.dp` (`feed_update`), как в
`tests/test_cleaning_move_cmd.py`. Без базы: права и роль мокнуты, построители
клавиатур с запросами в базу подменены, сеть подменена сессией, которая только
записывает запросы.
"""

import itertools
import unittest
from datetime import datetime, timezone
from unittest import mock
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, KeyboardButton, Message, ReplyKeyboardMarkup, Update, User

import bot
from cleaning.fsm import CleaningCashMoveFSM

UNRECOGNIZED = "Команда не распознана. Выберите действие на клавиатуре ниже."
ADMIN_ONLY = "Только для администраторов."
SALARY_PROMPT = "Выберите мастера для расчёта ЗП:"
CLEANING_MAIN_BUTTONS = ["🧹 Провести уборку", "🔍 Клиент", "💰 Баланс", "➖ Добавить расход"]

# Кнопка главного меню → (ответ её обработчика, состояние после него).
BUTTONS = {
    "Отчёты": ("Отчёты: выбери раздел.", bot.ReportsFSM.waiting_root),
    "Приход": ("Выберите способ оплаты:", bot.IncomeFSM.waiting_method),
    "Расход": ("Введите сумму расхода:", bot.ExpenseFSM.waiting_amount),
    "Изъятие": (
        "Выберите мастера, у которого нужно изъять наличные:",
        bot.WithdrawFSM.waiting_master,
    ),
    "Привязать": ("Что привязываем?", bot.WireLinkFSM.waiting_mode),
    "Мастера": ("Мастера: выбери действие.", bot.AdminMenuFSM.masters),
    "Клиенты": (
        "Введите номер телефона клиента (8/ +7/ 9...):",
        bot.AdminClientsFSM.find_wait_phone,
    ),
    "Рассчитать ЗП": (SALARY_PROMPT, bot.AdminPayrollFSM.waiting_master),
}

_user_ids = itertools.count(940_001)
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


class _Conn:
    """Роль из «staff»; остального в этих сценариях база не видит."""

    def __init__(self, role):
        self.role = role

    async def fetchrow(self, query, *args):
        if "FROM staff" in query:
            return {"role": self.role} if self.role else None
        raise AssertionError(f"unexpected fetchrow: {query}")


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


def _kb_texts(markup):
    return [button.text for row in markup.keyboard for button in row]


class _DispatchCase(unittest.IsolatedAsyncioTestCase):
    """Один пользователь гонит сообщения через `bot.dp`."""

    role = "admin"

    def permission(self, perm):
        return True

    async def asyncSetUp(self):
        self.session = _RecordingSession()
        self.tg_bot = Bot("123456:TEST-admin-menu", session=self.session)
        self.user_id = next(_user_ids)
        self.pool = _Pool(_Conn(self.role))
        self.fsm = bot.dp.fsm.get_context(
            bot=self.tg_bot, chat_id=self.user_id, user_id=self.user_id
        )
        masters_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Иван")], [KeyboardButton(text="Отмена")]],
            resize_keyboard=True,
        )
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

    def last_markup_texts(self):
        return _kb_texts([m for m in self.session.sent if isinstance(m, SendMessage)][-1].reply_markup)

    async def state(self):
        return await self.fsm.get_state()


class ButtonsTableTests(unittest.TestCase):
    def test_table_covers_every_main_menu_button(self):
        self.assertEqual(sorted(BUTTONS), sorted(_kb_texts(bot.admin_root_kb())))


# ---------- админ: кнопки без /start ----------


class AdminButtonsTests(_DispatchCase):
    async def test_every_button_works_without_state(self):
        # Перезапуск бота: состояние None, /start не нажимали.
        for text, (reply, state) in BUTTONS.items():
            with self.subTest(button=text):
                await self.fsm.clear()
                replies = await self.send(text)
                self.assertEqual(replies, [reply])
                self.assertEqual(await self.state(), state.state)

    async def test_every_button_works_in_root(self):
        # Как раньше: после /start.
        for text, (reply, state) in BUTTONS.items():
            with self.subTest(button=text):
                await self.fsm.clear()
                await self.fsm.set_state(bot.AdminMenuFSM.root)
                replies = await self.send(text)
                self.assertEqual(replies, [reply])
                self.assertEqual(await self.state(), state.state)

    async def test_button_inside_other_scenario_is_not_intercepted(self):
        # Посреди сценария текст кнопки обрабатывает сам сценарий, как раньше.
        # «Отчёты» без фильтра состояния ловится везде и раньше — его не берём.
        for text in BUTTONS:
            if text == "Отчёты":
                continue
            with self.subTest(button=text):
                await self.fsm.clear()
                await self.fsm.set_state(bot.ExpenseFSM.waiting_amount)
                replies = await self.send(text)
                self.assertEqual(
                    replies, ["Сумма должна быть числом. Повторите ввод или «Отмена»."]
                )
                self.assertEqual(await self.state(), bot.ExpenseFSM.waiting_amount.state)


# ---------- «Отмена» и заглушка ставят админу главное меню ----------


class AdminCancelTests(_DispatchCase):
    async def test_cancel_without_state_sets_root(self):
        replies = await self.send("Отмена")
        self.assertEqual(replies, ["Отменено."])
        self.assertEqual(self.last_markup_texts(), _kb_texts(bot.admin_root_kb()))
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)

    async def test_cancel_inside_cleaning_scenario_sets_root(self):
        # Админ отменил клининговый сценарий: раньше состояние оставалось None.
        await self.fsm.set_state(CleaningCashMoveFSM.amount)
        replies = await self.send("Отмена")
        self.assertEqual(replies, ["Отменено."])
        self.assertEqual(self.last_markup_texts(), _kb_texts(bot.admin_root_kb()))
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)

    async def test_every_button_works_after_cancel(self):
        for text, (reply, state) in BUTTONS.items():
            with self.subTest(button=text):
                await self.fsm.clear()
                await self.fsm.set_state(CleaningCashMoveFSM.amount)
                self.assertEqual(await self.send("Отмена"), ["Отменено."])
                replies = await self.send(text)
                self.assertEqual(replies, [reply])
                self.assertEqual(await self.state(), state.state)

    async def test_unknown_sets_root_and_next_button_works(self):
        replies = await self.send("что-то непонятное")
        self.assertEqual(replies, [UNRECOGNIZED])
        self.assertEqual(self.last_markup_texts(), _kb_texts(bot.admin_root_kb()))
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)
        replies = await self.send("Приход")
        self.assertEqual(replies, [BUTTONS["Приход"][0]])
        self.assertEqual(await self.state(), bot.IncomeFSM.waiting_method.state)


# ---------- не-админы: те же тексты — как раньше ----------


class MasterSameTextsTests(_DispatchCase):
    role = "master"
    expected_kb = [button.text for row in bot.master_kb.keyboard for button in row]

    def permission(self, perm):
        return perm in {"create_orders_clients", "view_own_salary", "view_own_income"}

    async def test_texts_go_where_they_went(self):
        for text in BUTTONS:
            with self.subTest(button=text):
                await self.fsm.clear()
                replies = await self.send(text)
                if text == "Отчёты":
                    # Обработчик «Отчёты» без фильтра состояния: отказ, как раньше.
                    self.assertEqual(replies, [ADMIN_ONLY])
                else:
                    self.assertEqual(replies, [UNRECOGNIZED])
                    self.assertEqual(self.last_markup_texts(), self.expected_kb)
                self.assertIsNone(await self.state())

    async def test_cancel_does_not_set_admin_root(self):
        replies = await self.send("Отмена")
        self.assertEqual(replies, ["Отменено."])
        self.assertEqual(self.last_markup_texts(), self.expected_kb)
        self.assertIsNone(await self.state())


class CleanerSameTextsTests(MasterSameTextsTests):
    role = "cleaner"
    expected_kb = CLEANING_MAIN_BUTTONS

    def permission(self, perm):
        return perm.startswith("cleaning_")


class NoRoleSameTextsTests(MasterSameTextsTests):
    role = None
    expected_kb = [button.text for row in bot.main_kb.keyboard for button in row]

    def permission(self, perm):
        return False


if __name__ == "__main__":
    unittest.main()
