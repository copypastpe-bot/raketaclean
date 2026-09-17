"""Диалог проведения уборки: адрес не спрашиваем, комментарий необязателен.

ТЗ 2026-09-17 «адреса в клининге (уборки)», задача 1 — тот же приём, что уже
применён к химчистке (docs/plans/2026-09-16-addresses.md). Проверяем ровно
переход состояний и то, что пишется в state: пустой комментарий/«Без
комментария» превращается в None и диалог идёт дальше к сумме чека.
"""

import unittest
from unittest import mock

from cleaning.fsm import CleaningOrderFSM
from cleaning.handlers import got_comment, got_name, got_phone


class FakeUser:
    def __init__(self, user_id=555):
        self.id = user_id


class FakeMessage:
    def __init__(self, text, user_id=555):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answer = mock.AsyncMock()


class FakeState:
    """Достаточно от FSMContext: get_data/update_data/set_state/clear."""

    def __init__(self):
        self._data: dict = {}
        self.state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self._data = {}
        self.state = None


class FakeConn:
    def __init__(self, client_row=None):
        self.client_row = client_row

    async def fetchrow(self, query, *args):
        return self.client_row


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


class GotPhoneSkipsAddressTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_client_with_address_goes_straight_to_comment(self):
        client_row = {
            "id": 7,
            "full_name": "Иванов",
            "phone": "+79040000000",
            "address": "ул. Ленина 10",
            "birthday": None,
            "status": "client",
            "bonus_balance": 100,
        }
        pool = FakePool(FakeConn(client_row))
        state = FakeState()
        msg = FakeMessage("+79040000000")

        await got_phone(msg, state, pool=pool)

        self.assertEqual(state.state, CleaningOrderFSM.comment)
        text = msg.answer.await_args.args[0]
        self.assertIn("Введите комментарий, например адрес", text)
        self.assertNotIn("Адрес из базы", text)

    async def test_existing_client_without_address_goes_straight_to_comment(self):
        client_row = {
            "id": 8,
            "full_name": "Петров",
            "phone": "+79040000001",
            "address": None,
            "birthday": None,
            "status": "client",
            "bonus_balance": 0,
        }
        pool = FakePool(FakeConn(client_row))
        state = FakeState()
        msg = FakeMessage("+79040000001")

        await got_phone(msg, state, pool=pool)

        self.assertEqual(state.state, CleaningOrderFSM.comment)

    async def test_new_client_reaches_comment_after_name(self):
        pool = FakePool(FakeConn(None))
        state = FakeState()
        msg = FakeMessage("+79040000002")

        await got_phone(msg, state, pool=pool)
        self.assertEqual(state.state, CleaningOrderFSM.name)

        name_msg = FakeMessage("Сидоров")
        await got_name(name_msg, state)
        self.assertEqual(state.state, CleaningOrderFSM.comment)


class GotCommentTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_without_comment_stores_none(self):
        state = FakeState()
        msg = FakeMessage("Без комментария")
        await got_comment(msg, state)
        data = await state.get_data()
        self.assertIsNone(data["comment"])
        self.assertEqual(state.state, CleaningOrderFSM.amount)

    async def test_empty_text_stores_none(self):
        state = FakeState()
        msg = FakeMessage("   ")
        await got_comment(msg, state)
        data = await state.get_data()
        self.assertIsNone(data["comment"])
        self.assertEqual(state.state, CleaningOrderFSM.amount)

    async def test_free_text_is_kept_as_comment(self):
        state = FakeState()
        msg = FakeMessage("ул. Ленина 10")
        await got_comment(msg, state)
        data = await state.get_data()
        self.assertEqual(data["comment"], "ул. Ленина 10")
        self.assertEqual(state.state, CleaningOrderFSM.amount)


if __name__ == "__main__":
    unittest.main()
