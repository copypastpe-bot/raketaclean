"""Реестр денег Оли, задача 3 (docs/plans/2026-10-05-olya-money-register.md):
выбор «Оля / Дима» в админских операциях с кассой клининга.

Шаг выбора стоит после суммы, до комментария (у выплаты — до подтверждения) в
пяти сценариях: приход `/cleaning_cash_add`, расход `/cleaning_cash_expense`,
изъятие `/cleaning_cash_withdrawal`, выплата прибыли `/cleaning_dividend` и
расход бригадира `/cleaning_expense`, если его ведёт не клинер. Клинер в
своём расходе по-прежнему тратит деньги Оли без вопроса.

Подтверждение показывает выбор; строка кассы получает `cash_holder`; строка
`Деньги Оли: N₽` в чат клининговых денег — только при выборе «Оля».

Сценарии гоняются через настоящий диспетчер `bot.dp` (`feed_update`), то есть
вместе с обработчиками самого диспетчера: заглушка `unknown` и мост кнопок
клинера в `bot.py` стоят раньше роутера клининга. Тест показывает, что ответ
«Оля» / «Дима» внутри сценария до неё не доходит. Без базы: запись в кассу и
остатки мокнуты, сеть подменена сессией, которая только записывает запросы.
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
from cleaning.fsm import (
    CleaningCashAddFSM,
    CleaningCashExpenseFSM,
    CleaningCashWithdrawalFSM,
    CleaningDividendFSM,
    CleaningForemanExpenseFSM,
)

SPEND_Q = "Источник? «Оля» — Деньги Ольга, «Дима» — обычная касса."
INCOME_Q = "Куда вносим? «Оля» — Деньги Ольга, «Дима» — обычная касса."
UNRECOGNIZED = "Команда не распознана. Выберите действие на клавиатуре ниже."

_user_ids = itertools.count(910_001)
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
    """Права и роль из «staff»; остальное в сценариях мокнуто."""

    def __init__(self, role):
        self.role = role

    def transaction(self):
        return _NullTx()

    async def fetchval(self, query, *args):
        if "FROM staff" in query:
            return self.role
        if "role_permissions" in query:
            return 1
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

    role = "superadmin"

    async def asyncSetUp(self):
        self.session = _RecordingSession()
        self.tg_bot = Bot("123456:TEST-olya-choice", session=self.session)
        self.user_id = next(_user_ids)
        self.pool = _Pool(_Conn(self.role))
        self.fsm = bot.dp.fsm.get_context(
            bot=self.tg_bot, chat_id=self.user_id, user_id=self.user_id
        )

        self.add_cash_income = AsyncMock(return_value=1)
        self.add_cash_expense = AsyncMock(return_value=2)
        self.add_cash_withdrawal = AsyncMock(return_value=3)
        self.record_dividend = AsyncMock(return_value=77)
        self.get_olya_balance = AsyncMock(return_value=D("4200"))
        self.send_flow = AsyncMock()
        patches = [
            mock.patch.object(cleaning_handlers, "add_cash_income", self.add_cash_income),
            mock.patch.object(cleaning_handlers, "add_cash_expense", self.add_cash_expense),
            mock.patch.object(
                cleaning_handlers, "add_cash_withdrawal", self.add_cash_withdrawal
            ),
            mock.patch.object(cleaning_handlers, "record_dividend", self.record_dividend),
            mock.patch.object(
                cleaning_handlers, "get_cleaning_balance", AsyncMock(return_value=D("50000"))
            ),
            mock.patch.object(cleaning_handlers, "get_olya_balance", self.get_olya_balance),
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

    def last_markup_texts(self):
        markup = [m for m in self.session.sent if isinstance(m, SendMessage)][-1].reply_markup
        return [button.text for row in markup.keyboard for button in row]

    async def state(self):
        return await self.fsm.get_state()

    async def data(self):
        return await self.fsm.get_data()

    def chat_text(self):
        self.send_flow.assert_awaited_once()
        return self.send_flow.await_args.args[1]


# ---------- заглушка unknown не перехватывает ответ внутри сценария ----------


class UnknownStubDoesNotInterceptTests(_DispatchCase):
    """Ответ «Оля» / «Дима» на шаге выбора доходит до роутера клининга."""

    STEPS = (
        # (состояние, данные до шага, состояние после, текст следующего вопроса)
        (CleaningCashAddFSM.cash_holder, {"method": "Наличные", "amount": "1000"},
         CleaningCashAddFSM.comment, "Комментарий (или «-»):"),
        (CleaningCashExpenseFSM.cash_holder, {"category": "Химия", "amount": "500"},
         CleaningCashExpenseFSM.comment, "Комментарий (или «-»):"),
        (CleaningCashWithdrawalFSM.cash_holder, {"amount": "700"},
         CleaningCashWithdrawalFSM.comment, "Комментарий (или «-»):"),
        (CleaningDividendFSM.cash_holder,
         {"amount": "2000", "balance": "50000", "shares": ["1000", "1000"]},
         CleaningDividendFSM.confirm, None),
        (CleaningForemanExpenseFSM.cash_holder,
         {"amount": "300", "category": "ГСМ", "ask_cash_holder": True},
         CleaningForemanExpenseFSM.comment,
         "Комментарий? (введите текст или нажмите «Без комментария»)"),
    )

    async def _check(self, answer, expected_holder):
        for state, data, next_state, next_question in self.STEPS:
            with self.subTest(state=state.state, answer=answer):
                await self.fsm.set_state(state)
                await self.fsm.set_data(dict(data))
                replies = await self.send(answer)
                self.assertNotIn(UNRECOGNIZED, replies)
                self.assertEqual(await self.state(), next_state.state)
                self.assertEqual((await self.data())["cash_holder"], expected_holder)
                self.assertEqual(len(replies), 1)
                if next_question is not None:
                    self.assertEqual(replies[0], next_question)
                await self.fsm.clear()

    async def test_olya_reaches_cleaning_router_in_every_scenario(self):
        await self._check("Оля", CASH_HOLDER_OLYA)

    async def test_dima_reaches_cleaning_router_in_every_scenario(self):
        await self._check("Дима", CASH_HOLDER_DIMA)

    async def test_other_text_reasks_and_stays_on_step(self):
        for state, data, _next_state, _q in self.STEPS:
            with self.subTest(state=state.state):
                await self.fsm.set_state(state)
                await self.fsm.set_data(dict(data))
                replies = await self.send("Женя")
                self.assertEqual(replies, ["Выберите «Оля» или «Дима» кнопками ниже."])
                self.assertEqual(await self.state(), state.state)
                self.assertNotIn("cash_holder", await self.data())
                self.assertEqual(self.last_markup_texts(), ["Оля", "Дима", "Отмена"])
                await self.fsm.clear()

    async def test_control_without_state_unknown_answers(self):
        # Контроль самой проверки: вне сценария тот же «Оля» ловит заглушка.
        with mock.patch.object(bot, "has_permission", AsyncMock(return_value=True)):
            replies = await self.send("Оля")
        self.assertEqual(replies, [UNRECOGNIZED])
        self.assertIsNone(await self.state())


# ---------- сценарии целиком ----------


class CashExpenseChoiceTests(_DispatchCase):
    async def _until_choice(self):
        await self.send("/cleaning_cash_expense")
        await self.send("Химия")
        replies = await self.send("500")
        self.assertEqual(replies, [SPEND_Q])
        self.assertEqual(self.last_markup_texts(), ["Оля", "Дима", "Отмена"])
        self.assertEqual(await self.state(), CleaningCashExpenseFSM.cash_holder.state)

    async def test_olya(self):
        await self._until_choice()
        self.assertEqual(await self.send("Оля"), ["Комментарий (или «-»):"])
        confirm = await self.send("хлорка")
        self.assertEqual(
            confirm,
            ["Подтвердите расход: Химия 500₽\nИсточник: Деньги Ольга\nКомментарий: хлорка"],
        )
        await self.send("Провести")
        self.assertEqual(
            self.add_cash_expense.await_args.kwargs["cash_holder"], CASH_HOLDER_OLYA
        )
        self.get_olya_balance.assert_awaited_once()
        self.assertEqual(self.chat_text().split("\n")[-1], "Деньги Ольга: 4 200₽")
        self.assertIsNone(await self.state())

    async def test_dima(self):
        await self._until_choice()
        await self.send("Дима")
        confirm = await self.send("-")
        self.assertEqual(confirm, ["Подтвердите расход: Химия 500₽\nИсточник: касса (Дима)"])
        await self.send("Провести")
        self.assertEqual(
            self.add_cash_expense.await_args.kwargs["cash_holder"], CASH_HOLDER_DIMA
        )
        self.get_olya_balance.assert_not_awaited()
        self.assertNotIn("Деньги Ольга", self.chat_text())


class CashAddChoiceTests(_DispatchCase):
    async def _until_choice(self):
        await self.send("/cleaning_cash_add")
        await self.send("Наличные")
        replies = await self.send("1000")
        self.assertEqual(replies, [INCOME_Q])
        self.assertEqual(await self.state(), CleaningCashAddFSM.cash_holder.state)

    async def test_olya(self):
        await self._until_choice()
        await self.send("Оля")
        confirm = await self.send("-")
        self.assertEqual(confirm, ["Подтвердите приход: Наличные 1 000₽\nКуда: Деньги Ольга"])
        await self.send("Провести")
        self.assertEqual(
            self.add_cash_income.await_args.kwargs["cash_holder"], CASH_HOLDER_OLYA
        )
        self.assertEqual(self.chat_text().split("\n")[-1], "Деньги Ольга: 4 200₽")

    async def test_dima(self):
        await self._until_choice()
        await self.send("Дима")
        confirm = await self.send("внесение")
        self.assertEqual(
            confirm,
            ["Подтвердите приход: Наличные 1 000₽\nКуда: касса (Дима)\nКомментарий: внесение"],
        )
        await self.send("Провести")
        self.assertEqual(
            self.add_cash_income.await_args.kwargs["cash_holder"], CASH_HOLDER_DIMA
        )
        self.get_olya_balance.assert_not_awaited()
        self.assertNotIn("Деньги Ольга", self.chat_text())


class CashWithdrawalChoiceTests(_DispatchCase):
    async def _until_choice(self):
        await self.send("/cleaning_cash_withdrawal")
        replies = await self.send("700")
        self.assertEqual(replies, [SPEND_Q])
        self.assertEqual(await self.state(), CleaningCashWithdrawalFSM.cash_holder.state)

    async def test_olya(self):
        await self._until_choice()
        await self.send("Оля")
        confirm = await self.send("-")
        self.assertEqual(confirm, ["Подтвердите изъятие: 700₽\nИсточник: Деньги Ольга"])
        await self.send("Провести")
        self.assertEqual(
            self.add_cash_withdrawal.await_args.kwargs["cash_holder"], CASH_HOLDER_OLYA
        )
        self.assertEqual(self.chat_text().split("\n")[-1], "Деньги Ольга: 4 200₽")

    async def test_dima(self):
        await self._until_choice()
        await self.send("Дима")
        confirm = await self.send("-")
        self.assertEqual(confirm, ["Подтвердите изъятие: 700₽\nИсточник: касса (Дима)"])
        await self.send("Провести")
        self.assertEqual(
            self.add_cash_withdrawal.await_args.kwargs["cash_holder"], CASH_HOLDER_DIMA
        )
        self.get_olya_balance.assert_not_awaited()
        self.assertNotIn("Деньги Ольга", self.chat_text())


class DividendChoiceTests(_DispatchCase):
    async def _until_choice(self):
        await self.send("/cleaning_dividend")
        replies = await self.send("2000")
        self.assertEqual(replies, [SPEND_Q])
        self.assertEqual(await self.state(), CleaningDividendFSM.cash_holder.state)

    async def test_olya(self):
        await self._until_choice()
        confirm = await self.send("Оля")
        self.assertEqual(
            confirm,
            [
                "Выплата прибыли 2 000₽:\n"
                "Дима — 1 000₽\n"
                "Женя — 1 000₽\n"
                "В кассе 50 000₽, останется 48 000₽.\n"
                "Источник: Деньги Ольга\n"
                "Подтвердить?"
            ],
        )
        self.assertEqual(await self.state(), CleaningDividendFSM.confirm.state)
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Выплата #77 проведена. Касса клининга: 50 000₽"])
        self.assertEqual(
            self.record_dividend.await_args.kwargs["cash_holder"], CASH_HOLDER_OLYA
        )
        self.assertEqual(self.chat_text().split("\n")[-1], "Деньги Ольга: 4 200₽")

    async def test_dima(self):
        await self._until_choice()
        confirm = await self.send("Дима")
        self.assertIn("\nИсточник: касса (Дима)\nПодтвердить?", confirm[0])
        await self.send("Провести")
        self.assertEqual(
            self.record_dividend.await_args.kwargs["cash_holder"], CASH_HOLDER_DIMA
        )
        self.get_olya_balance.assert_not_awaited()
        self.assertNotIn("Деньги Ольга", self.chat_text())


class ForemanExpenseAdminChoiceTests(_DispatchCase):
    """Расход бригадира, который ведёт админ: тот же шаг выбора."""

    async def _until_choice(self):
        await self.send("/cleaning_expense")
        await self.send("300")
        replies = await self.send("ГСМ")
        self.assertEqual(replies, [SPEND_Q])
        self.assertEqual(await self.state(), CleaningForemanExpenseFSM.cash_holder.state)

    async def test_olya(self):
        await self._until_choice()
        await self.send("Оля")
        confirm = await self.send("Без комментария")
        self.assertEqual(
            confirm,
            [
                "Подтвердите расход:\nКатегория: ГСМ\nСумма: 300₽\n"
                "Источник: Деньги Ольга\nКомментарий: Расход"
            ],
        )
        await self.send("Провести")
        self.assertEqual(
            self.add_cash_expense.await_args.kwargs["cash_holder"], CASH_HOLDER_OLYA
        )
        self.assertEqual(self.chat_text().split("\n")[-1], "Деньги Ольга: 4 200₽")

    async def test_dima(self):
        await self._until_choice()
        await self.send("Дима")
        confirm = await self.send("бензин")
        self.assertEqual(
            confirm,
            [
                "Подтвердите расход:\nКатегория: ГСМ\nСумма: 300₽\n"
                "Источник: касса (Дима)\nКомментарий: бензин"
            ],
        )
        await self.send("Провести")
        self.assertEqual(
            self.add_cash_expense.await_args.kwargs["cash_holder"], CASH_HOLDER_DIMA
        )
        self.get_olya_balance.assert_not_awaited()
        self.assertNotIn("Деньги Ольга", self.chat_text())


class ForemanExpenseCleanerNoChoiceTests(_DispatchCase):
    """Клинер (Оля): вопроса нет, расход из её денег, как раньше."""

    role = "cleaner"

    async def test_cleaner_skips_choice_and_spends_olya_money(self):
        await self.send("/cleaning_expense")
        await self.send("300")
        replies = await self.send("ГСМ")
        self.assertEqual(
            replies, ["Комментарий? (введите текст или нажмите «Без комментария»)"]
        )
        self.assertEqual(await self.state(), CleaningForemanExpenseFSM.comment.state)
        confirm = await self.send("Без комментария")
        self.assertEqual(
            confirm,
            ["Подтвердите расход:\nКатегория: ГСМ\nСумма: 300₽\nКомментарий: Расход"],
        )
        await self.send("Провести")
        self.assertEqual(
            self.add_cash_expense.await_args.kwargs["cash_holder"], CASH_HOLDER_OLYA
        )
        self.assertEqual(self.chat_text().split("\n")[-1], "Деньги Ольга: 4 200₽")


if __name__ == "__main__":
    unittest.main()
