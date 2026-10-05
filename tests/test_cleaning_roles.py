"""Права и синее меню по клинингу (решение владельца 2026-10-05).

Выплату прибыли клининга проводят админы, не клинер: у роли cleaner права
`cleaning_pay_dividend` нет. Админам в синее меню добавлен расход кассы
клининга («Клининг: расход»), право на него у них было и раньше.
Права пишет в базу `init_permissions` из `ROLE_MATRIX` при каждом запуске,
поэтому проверяем саму матрицу.
"""

import unittest
from unittest import mock

import bot


class CleaningRolesTests(unittest.TestCase):
    def test_cleaner_cannot_pay_dividend(self):
        self.assertNotIn("cleaning_pay_dividend", bot.ROLE_MATRIX["cleaner"])

    def test_admins_keep_dividend_and_cash_expense_rights(self):
        for role in ("admin", "superadmin"):
            perms = bot.ROLE_MATRIX[role]
            self.assertIn("cleaning_pay_dividend", perms)
            self.assertIn("cleaning_manage_cash", perms)       # право на /cleaning_cash_expense


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, *_args):
        return self.rows


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, rows):
        self.conn = FakeConn(rows)

    def acquire(self):
        return FakeAcquire(self.conn)


class BlueMenuTests(unittest.IsolatedAsyncioTestCase):
    async def _menus(self):
        rows = [{"tg_user_id": 1, "role": "admin"}, {"tg_user_id": 2, "role": "cleaner"}]
        set_cmds = mock.AsyncMock()
        with mock.patch.object(bot, "pool", FakePool(rows)), \
                mock.patch.object(bot.bot, "set_my_commands", set_cmds):
            await bot.set_commands()
        menus = {}
        for call in set_cmds.await_args_list:
            scope = call.kwargs.get("scope")
            chat_id = getattr(scope, "chat_id", None)
            if chat_id is not None:
                menus[chat_id] = {cmd.command: cmd.description for cmd in call.args[0]}
        return menus

    async def test_admin_menu_has_cleaning_cash_expense(self):
        menus = await self._menus()
        self.assertEqual(menus[1].get("cleaning_cash_expense"), "Клининг: расход")

    async def test_cleaner_menu_has_no_dividend(self):
        menus = await self._menus()
        self.assertNotIn("cleaning_dividend", menus[2])


if __name__ == "__main__":
    unittest.main()
