"""Мост cleaner_button_bridge: кнопки клинера должны доходить до клининга.

Заглушка `unknown` в bot.py ловит любой нераспознанный текст раньше вложенного
cleaning_router — в aiogram обработчики самого диспетчера идут перед вложенными
роутерами. Поэтому мост cleaner_button_bridge перечисляет кнопки клавиатуры
cleaning_main_kb() вручную и вызывает нужную функцию клининга сам. В клавиатуре
пять кнопок; было заведено три, две («🔍 Клиент», «💸 Выплата») не доходили —
бригадир получал «Команда не распознана». Проверяем все пять и то, что
пользователь без роли cleaner по-прежнему получает отказ.
"""

import unittest
from unittest import mock

import bot


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn=None):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeMessage:
    def __init__(self, text, user_id=555):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answer = mock.AsyncMock()


class CleanerButtonBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pool_patch = mock.patch.object(bot, "pool", FakePool())
        pool_patch.start()
        self.addCleanup(pool_patch.stop)

    async def _call(self, text, role):
        msg = FakeMessage(text)
        state = mock.Mock()
        with mock.patch.object(bot, "get_user_role", mock.AsyncMock(return_value=role)):
            await bot.cleaner_button_bridge(msg, state)
        return msg, state

    async def test_provesti_uborku_reaches_cleaning_order(self):
        with mock.patch.object(bot, "start_cleaning_order", mock.AsyncMock()) as fn:
            msg, state = await self._call("🧹 Провести уборку", "cleaner")
        fn.assert_awaited_once_with(msg, state, pool=bot.pool)

    async def test_balans_reaches_balance_handler(self):
        with mock.patch.object(bot, "cleaning_balance_cmd", mock.AsyncMock()) as fn:
            msg, _state = await self._call("💰 Баланс", "cleaner")
        fn.assert_awaited_once_with(msg, pool=bot.pool)

    async def test_dobavit_raskhod_reaches_expense_handler(self):
        with mock.patch.object(bot, "foreman_expense_start", mock.AsyncMock()) as fn:
            msg, state = await self._call("➖ Добавить расход", "cleaner")
        fn.assert_awaited_once_with(msg, state, pool=bot.pool)

    async def test_klient_reaches_client_lookup(self):
        with mock.patch.object(bot, "cleaning_client_lookup_start", mock.AsyncMock()) as fn:
            msg, state = await self._call("🔍 Клиент", "cleaner")
        fn.assert_awaited_once_with(msg, state, pool=bot.pool)

    async def test_vyplata_reaches_payout_button(self):
        with mock.patch.object(bot, "start_cleaning_payout_button", mock.AsyncMock()) as fn:
            msg, state = await self._call("💸 Выплата", "cleaner")
        fn.assert_awaited_once_with(msg, state, pool=bot.pool)

    async def test_non_cleaner_gets_unrecognized_command_for_every_button(self):
        buttons = (
            "🧹 Провести уборку",
            "💰 Баланс",
            "➖ Добавить расход",
            "🔍 Клиент",
            "💸 Выплата",
        )
        cleaning_fns = (
            "start_cleaning_order",
            "cleaning_balance_cmd",
            "foreman_expense_start",
            "cleaning_client_lookup_start",
            "start_cleaning_payout_button",
        )
        with mock.patch.object(bot, "keyboard_for_user", mock.AsyncMock(return_value="KB")):
            patches = [mock.patch.object(bot, name, mock.AsyncMock()) for name in cleaning_fns]
            fns = [p.start() for p in patches]
            self.addCleanup(lambda: [p.stop() for p in patches])
            for text in buttons:
                msg, _state = await self._call(text, role="master")
                msg.answer.assert_awaited_once_with(
                    "Команда не распознана. Выберите действие на клавиатуре ниже.",
                    reply_markup="KB",
                )
            for fn in fns:
                fn.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
