"""Команды `/cleaning_cash_expense` в боте нет (docs/plans/2026-10-05-admin-menu.md,
решение владельца 2, задача 2).

Расход кассы клининга — один сценарий для всех: `/cleaning_expense`
(«➖ Добавить расход»). Выбор «Оля / Дима» у админа и строки в чат клининговых
денег проверяются на нём в `tests/test_cleaning_olya_choice.py`, синее меню —
в `tests/test_cleaning_roles.py`.
"""

import inspect
import unittest

from aiogram.filters import Command

import bot
import cleaning.fsm as cleaning_fsm


def _registered_commands(router) -> set[str]:
    found: set[str] = set()
    for handler in router.message.handlers:
        for flt in handler.filters or ():
            if isinstance(flt.callback, Command):
                found.update(c for c in flt.callback.commands if isinstance(c, str))
    for sub in router.sub_routers:
        found |= _registered_commands(sub)
    return found


class CleaningCashExpenseRemovedTests(unittest.TestCase):
    def test_no_handler_for_cleaning_cash_expense(self):
        commands = _registered_commands(bot.dp)
        self.assertIn("cleaning_expense", commands)  # единый расход на месте
        self.assertNotIn("cleaning_cash_expense", commands)

    def test_no_fsm_group(self):
        self.assertFalse(hasattr(cleaning_fsm, "CleaningCashExpenseFSM"))

    def test_cancel_any_does_not_list_removed_group(self):
        self.assertNotIn("CleaningCashExpenseFSM", inspect.getsource(bot.cancel_any))


if __name__ == "__main__":
    unittest.main()
