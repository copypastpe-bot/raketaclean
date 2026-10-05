"""Права и синее меню по клинингу (решение владельца 2026-10-05).

Выплату прибыли клининга проводят админы, не клинер: у роли cleaner права
`cleaning_pay_dividend` нет. Расход кассы клининга у всех один — `/cleaning_expense`
(docs/plans/2026-10-05-admin-menu.md, решение 2): право на него у админов есть,
а `/cleaning_cash_expense` из бота и из синего меню удалена.
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
            self.assertIn("cleaning_manage_cash", perms)
            self.assertIn("cleaning_record_expense", perms)    # право на /cleaning_expense


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

    async def test_admin_menu_has_no_cleaning_cash_expense(self):
        menus = await self._menus()
        self.assertNotIn("cleaning_cash_expense", menus[1])

    async def test_cleaner_menu_has_no_dividend(self):
        menus = await self._menus()
        self.assertNotIn("cleaning_dividend", menus[2])

    async def test_admin_menu_has_olya_money(self):
        # Реестр денег Оли, задача 4: остаток и операции — командой в синем меню.
        menus = await self._menus()
        self.assertEqual(menus[1].get("cleaning_olya"), "Клининг: деньги Оли")
        self.assertNotIn("cleaning_olya", menus[2])


if __name__ == "__main__":
    unittest.main()
