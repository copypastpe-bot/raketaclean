"""Команды `/wipe_test_data` в боте нет (решение владельца 05.10).

Она осталась со времён запуска и без подтверждения стирала заказы, клиентов,
кассу, бонусы и зарплаты химчистки; набрать её мог любой админ.
"""

import unittest

from aiogram.filters import Command

import bot


def _registered_commands(router) -> set[str]:
    found: set[str] = set()
    for handler in router.message.handlers:
        for flt in handler.filters or ():
            if isinstance(flt.callback, Command):
                found.update(c for c in flt.callback.commands if isinstance(c, str))
    for sub in router.sub_routers:
        found |= _registered_commands(sub)
    return found


class WipeCommandRemovedTests(unittest.TestCase):
    def test_no_handler_for_wipe_test_data(self):
        commands = _registered_commands(bot.dp)
        self.assertIn("help", commands)  # обход обработчиков действительно что-то видит
        self.assertNotIn("wipe_test_data", commands)


if __name__ == "__main__":
    unittest.main()
