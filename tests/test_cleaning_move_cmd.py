"""Перемещение денег клининга, задача 3 (docs/plans/2026-10-05-olya-money-move.md):
команда админов `/cleaning_move`.

Ход: «Откуда перемещаем?» (кнопки «Деньги Ольга», «Касса (Дима)», «Отмена») →
маршрут и остаток источника, сумма → комментарий → подтверждение → «Провести».
Сумма больше остатка — отказ на шаге суммы; остаток источника ≤ 0 — сразу
«перемещать нечего»; остаток изменился между шагами — отказ на «Провести»
(запись перепроверяет под замком и бросает `CashMoveExceedsBalance`).
Сообщение в чат — по решению 5: маршрут, сумма, комментарий, две кучки.

Сценарии гоняются через настоящий диспетчер `bot.dp` (`feed_update`), как в
`tests/test_cleaning_olya_choice.py`: заглушка `unknown` и «Отмена» из `bot.py`
стоят раньше роутера клининга. Без базы: запись и остатки мокнуты, сеть
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
from aiogram.types import Chat, Message, Update, User

import bot
import cleaning.handlers as cleaning_handlers
from cleaning.admin_ops import CashMoveExceedsBalance
from cleaning.constants import CASH_HOLDER_DIMA, CASH_HOLDER_OLYA
from cleaning.format import format_cash_move_alert
from cleaning.fsm import CleaningCashMoveFSM

UNRECOGNIZED = "Команда не распознана. Выберите действие на клавиатуре ниже."
ASK_SOURCE = "Откуда перемещаем?"
SOURCE_BUTTONS = ["Деньги Ольга", "Касса (Дима)", "Отмена"]
CLEANING_MAIN_BUTTONS = ["🧹 Провести уборку", "🔍 Клиент", "💰 Баланс", "➖ Добавить расход"]
# Админ после операции — в своём главном меню (docs/plans/2026-10-05-admin-menu.md, задача 3).
ADMIN_ROOT_BUTTONS = [b.text for row in bot.admin_root_kb().keyboard for b in row]

_user_ids = itertools.count(920_001)
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
    balances = {CASH_HOLDER_OLYA: D("13500"), CASH_HOLDER_DIMA: D("41024")}

    async def asyncSetUp(self):
        self.session = _RecordingSession()
        self.tg_bot = Bot("123456:TEST-cash-move", session=self.session)
        self.user_id = next(_user_ids)
        self.pool = _Pool(_Conn(self.role, self.allowed))
        self.fsm = bot.dp.fsm.get_context(
            bot=self.tg_bot, chat_id=self.user_id, user_id=self.user_id
        )

        self.holder_balances = dict(self.balances)
        self.get_holder_balance = AsyncMock(
            side_effect=lambda conn, holder: self.holder_balances[holder]
        )
        self.record_cash_move = AsyncMock(return_value=41)
        self.get_olya_balance = AsyncMock(return_value=D("3500"))
        self.get_dima_balance = AsyncMock(return_value=D("51024"))
        self.send_flow = AsyncMock()
        patches = [
            mock.patch.object(
                cleaning_handlers, "get_holder_balance", self.get_holder_balance
            ),
            mock.patch.object(cleaning_handlers, "record_cash_move", self.record_cash_move),
            mock.patch.object(cleaning_handlers, "get_olya_balance", self.get_olya_balance),
            mock.patch.object(cleaning_handlers, "get_dima_balance", self.get_dima_balance),
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
        return [button.text for row in self.last_markup().keyboard for button in row]

    async def state(self):
        return await self.fsm.get_state()

    async def data(self):
        return await self.fsm.get_data()

    def chat_text(self):
        self.send_flow.assert_awaited_once()
        return self.send_flow.await_args.args[1]


# ---------- сообщение в чат (решение 5) ----------


class CashMoveAlertFormatTests(unittest.TestCase):
    def test_with_comment(self):
        text = format_cash_move_alert(
            route="Деньги Ольга → Касса (Дима)",
            amount=D("10000"),
            comment="сдала наличные",
            olya_balance=D("3500"),
            dima_balance=D("51024"),
        )
        self.assertEqual(
            text,
            "🔁 Перемещение денег\n"
            "Деньги Ольга → Касса (Дима): 10 000₽\n"
            "Комментарий: сдала наличные\n"
            "\n"
            "Деньги Ольга: 3 500₽\n"
            "Касса (Дима): 51 024₽",
        )

    def test_without_comment_and_negative_balance(self):
        text = format_cash_move_alert(
            route="Касса (Дима) → Деньги Ольга",
            amount=D("2500.50"),
            comment=None,
            olya_balance=D("-1000"),
            dima_balance=D("7000"),
        )
        self.assertEqual(
            text,
            "🔁 Перемещение денег\n"
            "Касса (Дима) → Деньги Ольга: 2 500.50₽\n"
            "\n"
            "Деньги Ольга: -1 000₽\n"
            "Касса (Дима): 7 000₽",
        )


# ---------- доступ ----------


class NoPermissionTests(_DispatchCase):
    role = "cleaner"
    allowed = False

    async def test_refused_without_cleaning_manage_cash(self):
        replies = await self.send("/cleaning_move")
        self.assertEqual(replies, ["Команда доступна только администраторам."])
        self.assertIsNone(await self.state())
        self.get_holder_balance.assert_not_awaited()


# ---------- сценарий целиком ----------


class OlyaToDimaTests(_DispatchCase):
    async def test_full_flow_with_comment(self):
        replies = await self.send("/cleaning_move")
        self.assertEqual(replies, [ASK_SOURCE])
        self.assertEqual(self.last_markup_texts(), SOURCE_BUTTONS)
        self.assertEqual(await self.state(), CleaningCashMoveFSM.source.state)

        replies = await self.send("Деньги Ольга")
        self.assertNotIn(UNRECOGNIZED, replies)
        self.assertEqual(
            replies,
            ["Деньги Ольга → Касса (Дима). Сейчас в «Деньги Ольга»: 13 500₽. Сумма (руб):"],
        )
        self.assertEqual(await self.state(), CleaningCashMoveFSM.amount.state)

        replies = await self.send("10000")
        self.assertEqual(replies, ["Комментарий (или «-»):"])
        self.assertEqual(await self.state(), CleaningCashMoveFSM.comment.state)

        replies = await self.send("сдала наличные")
        self.assertEqual(
            replies,
            ["Подтвердите перемещение\n"
             "Деньги Ольга → Касса (Дима): 10 000₽\n"
             "Комментарий: сдала наличные"],
        )
        self.assertEqual(self.last_markup_texts(), ["Провести", "Отменить"])
        self.assertEqual(await self.state(), CleaningCashMoveFSM.confirm.state)

        replies = await self.send("Провести")
        self.assertEqual(replies, ["Перемещение #41 проведено."])
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)

        self.record_cash_move.assert_awaited_once()
        self.assertEqual(
            self.record_cash_move.await_args.kwargs,
            {"source": CASH_HOLDER_OLYA, "amount": D("10000"), "comment": "сдала наличные"},
        )
        self.assertEqual(
            self.chat_text(),
            "🔁 Перемещение денег\n"
            "Деньги Ольга → Касса (Дима): 10 000₽\n"
            "Комментарий: сдала наличные\n"
            "\n"
            "Деньги Ольга: 3 500₽\n"
            "Касса (Дима): 51 024₽",
        )


class DimaToOlyaTests(_DispatchCase):
    async def test_full_flow_without_comment(self):
        await self.send("/cleaning_move")
        replies = await self.send("Касса (Дима)")
        self.assertNotIn(UNRECOGNIZED, replies)
        self.assertEqual(
            replies,
            ["Касса (Дима) → Деньги Ольга. Сейчас в «Касса (Дима)»: 41 024₽. Сумма (руб):"],
        )
        await self.send("3000")
        replies = await self.send("-")
        self.assertEqual(
            replies, ["Подтвердите перемещение\nКасса (Дима) → Деньги Ольга: 3 000₽"]
        )
        self.get_olya_balance.return_value = D("16500")
        self.get_dima_balance.return_value = D("38024")
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Перемещение #41 проведено."])
        self.assertEqual(
            self.record_cash_move.await_args.kwargs,
            {"source": CASH_HOLDER_DIMA, "amount": D("3000"), "comment": None},
        )
        self.assertEqual(
            self.chat_text(),
            "🔁 Перемещение денег\n"
            "Касса (Дима) → Деньги Ольга: 3 000₽\n"
            "\n"
            "Деньги Ольга: 16 500₽\n"
            "Касса (Дима): 38 024₽",
        )


# ---------- выбор источника ----------


class SourceStepTests(_DispatchCase):
    async def test_other_text_reasks_and_stays(self):
        await self.send("/cleaning_move")
        replies = await self.send("Оля")
        self.assertEqual(
            replies, ["Выберите «Деньги Ольга» или «Касса (Дима)» кнопками ниже."]
        )
        self.assertEqual(self.last_markup_texts(), SOURCE_BUTTONS)
        self.assertEqual(await self.state(), CleaningCashMoveFSM.source.state)
        self.get_holder_balance.assert_not_awaited()

    async def test_zero_balance_closes_scenario(self):
        self.holder_balances[CASH_HOLDER_OLYA] = D("0")
        await self.send("/cleaning_move")
        replies = await self.send("Деньги Ольга")
        self.assertEqual(replies, ["В «Деньги Ольга» 0₽ — перемещать нечего."])
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)

    async def test_negative_balance_closes_scenario(self):
        self.holder_balances[CASH_HOLDER_DIMA] = D("-3000")
        await self.send("/cleaning_move")
        replies = await self.send("Касса (Дима)")
        self.assertEqual(replies, ["В «Касса (Дима)» -3 000₽ — перемещать нечего."])
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)

    async def test_control_without_state_unknown_answers(self):
        # Контроль самой проверки: вне сценария текст кнопки ловит заглушка.
        with mock.patch.object(bot, "has_permission", AsyncMock(return_value=True)):
            replies = await self.send("Касса (Дима)")
        self.assertEqual(replies, [UNRECOGNIZED])
        # Админу заглушка ставит главное меню (docs/plans/2026-10-05-admin-menu.md, задача 1).
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)


# ---------- шаг суммы ----------


class AmountStepTests(_DispatchCase):
    async def _until_amount(self):
        self.holder_balances[CASH_HOLDER_OLYA] = D("3500")
        await self.send("/cleaning_move")
        await self.send("Деньги Ольга")
        self.assertEqual(await self.state(), CleaningCashMoveFSM.amount.state)

    async def test_more_than_balance_refused_and_asked_again(self):
        await self._until_amount()
        replies = await self.send("5000")
        self.assertEqual(replies, ["Нельзя больше остатка: в «Деньги Ольга» 3 500₽."])
        self.assertEqual(await self.state(), CleaningCashMoveFSM.amount.state)
        self.assertNotIn("amount", await self.data())

        replies = await self.send("3500")
        self.assertEqual(replies, ["Комментарий (или «-»):"])
        self.assertEqual((await self.data())["amount"], "3500")

    async def test_balance_rechecked_on_amount_step(self):
        await self._until_amount()
        self.holder_balances[CASH_HOLDER_OLYA] = D("1000")
        replies = await self.send("2000")
        self.assertEqual(replies, ["Нельзя больше остатка: в «Деньги Ольга» 1 000₽."])
        self.assertEqual(await self.state(), CleaningCashMoveFSM.amount.state)

    async def test_not_a_number(self):
        await self._until_amount()
        for text in ("abc", "0", "-5"):
            with self.subTest(text=text):
                replies = await self.send(text)
                self.assertEqual(replies, ["Нужно число > 0."])
                self.assertEqual(await self.state(), CleaningCashMoveFSM.amount.state)


# ---------- отказ на «Провести» ----------


class ProvestiRefusedTests(_DispatchCase):
    async def test_balance_changed_between_steps(self):
        await self.send("/cleaning_move")
        await self.send("Деньги Ольга")
        await self.send("10000")
        await self.send("-")
        self.record_cash_move.side_effect = CashMoveExceedsBalance(
            CASH_HOLDER_OLYA, D("2000")
        )
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Нельзя больше остатка: в «Деньги Ольга» 2 000₽."])
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)
        self.send_flow.assert_not_awaited()
        self.get_olya_balance.assert_not_awaited()
        self.get_dima_balance.assert_not_awaited()


# ---------- сценарий сбрасывается до ответа админу (П1 итогового ревью) ----------


class ProvestiAnswerFailsTests(_DispatchCase):
    async def test_state_cleared_before_answer_so_retry_does_not_duplicate(self):
        await self.send("/cleaning_move")
        await self.send("Деньги Ольга")
        await self.send("10000")
        await self.send("-")

        # Запись прошла, а ответ админу «падает» — как при тайм-ауте Telegram
        # через прокси.
        orig_make_request = self.session.make_request

        async def failing_make_request(bot_, method, timeout=None):
            raise RuntimeError("Telegram timeout")

        self.session.make_request = failing_make_request
        with self.assertRaises(RuntimeError):
            await self.send("Провести")

        self.record_cash_move.assert_awaited_once()
        # Сценарий закрыт до ответа: админ уже в главном меню (задача 3 ТЗ меню админа).
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)

        self.session.make_request = orig_make_request
        with mock.patch.object(bot, "has_permission", AsyncMock(return_value=True)):
            replies = await self.send("Провести")
        self.assertEqual(replies, ["Выберите действие на клавиатуре ниже."])
        self.record_cash_move.assert_awaited_once()


# ---------- «Отмена» ----------


class CancelTests(_DispatchCase):
    STEPS = (
        (CleaningCashMoveFSM.source, {}),
        (CleaningCashMoveFSM.amount, {"source": CASH_HOLDER_OLYA}),
        (CleaningCashMoveFSM.comment, {"source": CASH_HOLDER_OLYA, "amount": "1000"}),
        (CleaningCashMoveFSM.confirm,
         {"source": CASH_HOLDER_OLYA, "amount": "1000", "comment": ""}),
    )

    async def test_otmena_on_every_step(self):
        # «Отмена» ловит `cancel_any` в bot.py. Право на отчёты выключено, чтобы
        # ответ решался по `cleaning_prefixes`: без группы там он пошёл бы в базу.
        with mock.patch.object(bot, "has_permission", AsyncMock(return_value=False)):
            for state, data in self.STEPS:
                with self.subTest(state=state.state):
                    await self.fsm.set_state(state)
                    await self.fsm.set_data(dict(data))
                    replies = await self.send("Отмена")
                    self.assertEqual(replies, ["Отменено."])
                    self.assertEqual(self.last_markup_texts(), CLEANING_MAIN_BUTTONS)
                    self.assertIsNone(await self.state())
        self.record_cash_move.assert_not_awaited()
        self.send_flow.assert_not_awaited()

    async def test_otmenit_on_confirm(self):
        # Кнопка «Отменить» из `_confirm_kb()` — обработчик `cancel` роутера клининга.
        await self.send("/cleaning_move")
        await self.send("Деньги Ольга")
        await self.send("1000")
        await self.send("-")
        replies = await self.send("Отменить")
        self.assertEqual(replies, ["Отменено."])
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)
        self.record_cash_move.assert_not_awaited()
        self.send_flow.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
