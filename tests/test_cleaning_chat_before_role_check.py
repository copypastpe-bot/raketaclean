"""Правки по сверке задачи 3 (docs/plans/2026-10-05-admin-menu.md), пункт П1.

`_after_op_kb` после записи денег запрашивает в базе роль пользователя
(`is_cleaning_admin`), чтобы решить, какую клавиатуру отдать админу. Если этот
запрос стоит до `send_cleaning_money_flow`, сбой роли «съедает» уже
записанную операцию: деньги в кассе есть, а сообщение в чат не уходит.

Проверяем на двух сценариях из списка правок (`div_provesti`,
`cash_move_provesti`): роль не определить (исключение), но чат всё равно
получает сообщение, потому что оно отправляется раньше запроса роли.
Без базы: всё, что трогает БД внутри обработчика, замокано; `is_cleaning_admin`
подменена так, чтобы упасть.
"""

import unittest
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import cleaning.handlers as cleaning_handlers
from cleaning.constants import CASH_HOLDER_DIMA


class _Message:
    def __init__(self):
        self.answers = []
        self.from_user = SimpleNamespace(id=777_001)

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


class _State:
    def __init__(self, data):
        self._data = dict(data)

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self._data = {}


class _NullTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def transaction(self):
        return _NullTx()


class _Acquire:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def acquire(self):
        return _Acquire()


class RoleFailureDoesNotBlockChatMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_dividend_payout_chat_message_survives_role_failure(self):
        send_flow = AsyncMock()
        admin_menu_return = AsyncMock()
        kw = dict(pool=_Pool(), bot=object(), admin_menu_return=admin_menu_return)
        state = _State({"amount": "900", "cash_holder": CASH_HOLDER_DIMA})
        msg = _Message()
        patches = [
            mock.patch.object(
                cleaning_handlers, "get_cleaning_balance", AsyncMock(return_value=D("9000"))
            ),
            mock.patch.object(
                cleaning_handlers, "record_dividend", AsyncMock(return_value=55)
            ),
            mock.patch.object(
                cleaning_handlers, "configured_recipients", return_value=["Дима", "Женя"]
            ),
            mock.patch.object(cleaning_handlers, "dividend_comment", return_value="Дивиденды"),
            mock.patch.object(cleaning_handlers, "send_cleaning_money_flow", send_flow),
            mock.patch.object(
                cleaning_handlers,
                "is_cleaning_admin",
                AsyncMock(side_effect=RuntimeError("роль не определить")),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        with self.assertRaises(RuntimeError):
            await cleaning_handlers.div_provesti(msg, state, **kw)

        send_flow.assert_awaited_once()
        admin_menu_return.assert_not_awaited()

    async def test_cash_move_chat_message_survives_role_failure(self):
        send_flow = AsyncMock()
        admin_menu_return = AsyncMock()
        kw = dict(pool=_Pool(), bot=object(), admin_menu_return=admin_menu_return)
        state = _State({"source": CASH_HOLDER_DIMA, "amount": "500", "comment": ""})
        msg = _Message()
        patches = [
            mock.patch.object(
                cleaning_handlers, "record_cash_move", AsyncMock(return_value=41)
            ),
            mock.patch.object(
                cleaning_handlers, "get_olya_balance", AsyncMock(return_value=D("1000"))
            ),
            mock.patch.object(
                cleaning_handlers, "get_dima_balance", AsyncMock(return_value=D("2000"))
            ),
            mock.patch.object(cleaning_handlers, "send_cleaning_money_flow", send_flow),
            mock.patch.object(
                cleaning_handlers,
                "is_cleaning_admin",
                AsyncMock(side_effect=RuntimeError("роль не определить")),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        with self.assertRaises(RuntimeError):
            await cleaning_handlers.cash_move_provesti(msg, state, **kw)

        send_flow.assert_awaited_once()
        admin_menu_return.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
