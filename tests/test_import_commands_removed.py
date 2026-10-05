"""Команд импорта и служебной `/db_apply_cash_trigger` в боте нет (решение владельца 05.10).

docs/plans/2026-10-05-admin-menu.md, решение 5 и задача 4: `/import_amocrm`,
`/upload_clients`, `/import_leads_dryrun`, `/import_leads` остались от ручной
загрузки клиентов CSV-файлами, сейчас сделки приходят обменом с amoCRM;
`/db_apply_cash_trigger` снимала старые триггеры кассы, которых на проде уже нет.
"""

import unittest
from unittest import mock

from aiogram.filters import Command

import bot

REMOVED = {
    "import_amocrm",
    "upload_clients",
    "import_leads_dryrun",
    "import_leads",
    "db_apply_cash_trigger",
}


def _registered_commands(router) -> set[str]:
    found: set[str] = set()
    for handler in router.message.handlers:
        for flt in handler.filters or ():
            if isinstance(flt.callback, Command):
                found.update(c for c in flt.callback.commands if isinstance(c, str))
    for sub in router.sub_routers:
        found |= _registered_commands(sub)
    return found


class ImportCommandsRemovedTests(unittest.TestCase):
    def test_no_handlers_for_removed_commands(self):
        commands = _registered_commands(bot.dp)
        self.assertIn("help", commands)  # обход обработчиков действительно что-то видит
        self.assertEqual(commands & REMOVED, set())


class _FakeConn:
    async def fetch(self, *_args):
        return [{"tg_user_id": 1, "role": "admin"}, {"tg_user_id": 2, "role": "superadmin"}]


class _FakeAcquire:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def acquire(self):
        return _FakeAcquire()


class BlueMenuWithoutImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_menus_have_no_removed_commands(self):
        set_cmds = mock.AsyncMock()
        with mock.patch.object(bot, "pool", _FakePool()), \
                mock.patch.object(bot.bot, "set_my_commands", set_cmds):
            await bot.set_commands()
        listed: set[str] = set()
        for call in set_cmds.await_args_list:
            listed.update(cmd.command for cmd in call.args[0])
        self.assertIn("start", listed)  # меню действительно собрано
        self.assertEqual(listed & REMOVED, set())


if __name__ == "__main__":
    unittest.main()
