"""Синее меню и /help по ролям (docs/plans/2026-10-05-admin-menu.md, задача 5).

Решение владельца 3 — синее меню по ролям, описания дословно; решение 4 и раздел
«Состав /help админов» — что показывает /help. Последний тест проверяет, что каждая
команда бота у admin и у superadmin лежит ровно в одном месте: синее меню, /help,
кнопки, «не включать» или недоступна роли. Новая команда без места уронит тест.
"""

import inspect
import re
import unittest
from unittest import mock

from aiogram.filters import Command
from aiogram.types import BotCommandScopeDefault

import bot

DEFAULT_MENU = [
    ("start", "Открыть меню"),
    ("help", "Помощь"),
]
ADMIN_MENU = DEFAULT_MENU + [
    ("order", "Добавить заказ"),
    ("dividend", "Выплата дивидендов"),
    ("investment", "Внесение инвестиций"),
    ("jenya_card", "Баланс Карты Жени"),
    ("add_master", "Добавить мастера"),
    ("list_masters", "Список мастеров"),
    ("masters_all", "Полный список мастеров"),
    ("remove_master", "Деактивировать мастера"),
    ("cleaning_cash_withdrawal", "Клининг: изъятие из кассы"),
    ("cleaning_move_delete", "Клининг: удалить перемещение"),
    ("cleaning_olya", "Клининг: Деньги Ольга"),
]
SUPERADMIN_MENU = ADMIN_MENU + [
    ("order_remove", "Удалить заказ"),
    ("tx_remove", "Удалить транзакцию"),
    ("bonus_backfill", "Пересчитать бонусы"),
    ("cleaning_cancel_order", "Клининг: отменить уборку"),
    ("cleaning_dividend_cancel", "Клининг: отменить выплату"),
]

SHORT_HELP = "Если возникли проблемы, напишите @pastushenko12"

ADMIN_HELP = {
    "Химчистка": [
        "whoami", "orders", "tx_last", "cash_balance", "daily_cash", "daily_profit",
        "daily_orders", "my_daily", "mysalary", "myincome",
    ],
    "Клининг": ["cleaning_cash", "cleaning_orders"],
}
SUPERADMIN_HELP = {
    "Химчистка": ADMIN_HELP["Химчистка"] + ["tx_delete"],
    "Клининг": ADMIN_HELP["Клининг"],
}

# «Не включать» из «Состава /help админов»: работа этих команд есть на кнопках.
ON_BUTTONS = {
    "find", "client_info", "client_add_bonus", "client_set_birthday", "client_set_bonus",
    "client_set_name", "client_set_phone", "income", "expense", "withdraw", "link_payment",
    "reports", "cash", "profit", "payments", "payroll", "cleaning_expense",
    "cleaning_cash_add", "cleaning_move", "cleaning_dividend", "cleaning_balance",
}
# «Не включать»: дубли /start и /cleaning_order (проводят только бригадиры).
NOT_INCLUDED = {"admin_menu", "admin_panel", "cleaning_order"}
# Недоступны admin: обработчик пускает только superadmin (проверяется ниже по коду).
SUPERADMIN_ONLY = {"order_remove", "tx_remove", "bonus_backfill", "tx_delete"}
# ВОПРОС ВЛАДЕЛЬЦУ (отчёт задачи 5): у admin есть права на эти команды
# (cleaning_cancel_orders, cleaning_manage_cash), а ТЗ ставит их только в синее меню
# superadmin. Места у admin в ТЗ нет — не придумываем, держим отдельной корзиной.
ADMIN_NO_PLACE = {"cleaning_cancel_order", "cleaning_dividend_cancel"}

CMD_LINE = re.compile(r"^/([a-z_]+)(?: \S.*?)? — \S.*$")


def _registered_handlers(router) -> dict[str, object]:
    found: dict[str, object] = {}
    for handler in router.message.handlers:
        for flt in handler.filters or ():
            if isinstance(flt.callback, Command):
                for c in flt.callback.commands:
                    if isinstance(c, str):
                        found[c] = handler.callback
    for sub in router.sub_routers:
        found.update(_registered_handlers(sub))
    return found


class FakeConn:
    def __init__(self, rows=None, role=None):
        self.rows = rows or []
        self.role = role

    async def fetch(self, *_args):
        return self.rows

    async def fetchrow(self, *_args):
        if self.role is None:
            return None
        return {"role": self.role, "is_active": True}


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


ROWS = [
    {"tg_user_id": 1, "role": "admin"},
    {"tg_user_id": 2, "role": "superadmin"},
    {"tg_user_id": 3, "role": "master"},
    {"tg_user_id": 4, "role": "cleaner"},
    {"tg_user_id": 5, "role": "operator"},  # роль без своего меню
]


async def _menus() -> dict:
    set_cmds = mock.AsyncMock()
    with mock.patch.object(bot, "pool", FakePool(FakeConn(rows=ROWS))), \
            mock.patch.object(bot.bot, "set_my_commands", set_cmds):
        await bot.set_commands()
    menus = {}
    for call in set_cmds.await_args_list:
        scope = call.kwargs.get("scope")
        key = "default" if isinstance(scope, BotCommandScopeDefault) else scope.chat_id
        menus[key] = [(cmd.command, cmd.description) for cmd in call.args[0]]
    return menus


async def _help_text(role) -> str:
    msg = mock.Mock()
    msg.from_user.id = 77
    msg.answer = mock.AsyncMock()
    with mock.patch.object(bot, "pool", FakePool(FakeConn(role=role))):
        await bot.help_cmd(msg)
    msg.answer.assert_awaited_once()
    return msg.answer.await_args.args[0]


def _help_groups(text: str) -> dict[str, list[str]]:
    """Разбор /help админа: «Группа:» и под ней строки «/команда [аргументы] — что делает»."""
    groups: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("/"):
            m = CMD_LINE.match(line)
            assert m, f"строка /help не по формату «команда — что делает»: {line!r}"
            assert current is not None, f"команда вне группы: {line!r}"
            groups[current].append(m.group(1))
        else:
            assert line.endswith(":"), f"лишняя строка в /help: {line!r}"
            current = line[:-1]
            groups[current] = []
    return groups


class BlueMenuByRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_menus_by_role(self):
        menus = await _menus()
        self.assertEqual(menus["default"], DEFAULT_MENU)
        self.assertEqual(menus[1], ADMIN_MENU)
        self.assertEqual(menus[2], SUPERADMIN_MENU)
        self.assertEqual(menus[3], DEFAULT_MENU)
        self.assertEqual(menus[4], DEFAULT_MENU)
        self.assertEqual(menus[5], DEFAULT_MENU)


class HelpByRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_help_for_cleaner_master_and_no_role(self):
        for role in ("cleaner", "master", None):
            with self.subTest(role=role):
                self.assertEqual(await _help_text(role), SHORT_HELP)

    async def test_admin_help(self):
        self.assertEqual(_help_groups(await _help_text("admin")), ADMIN_HELP)

    async def test_superadmin_help(self):
        self.assertEqual(_help_groups(await _help_text("superadmin")), SUPERADMIN_HELP)


class EveryCommandHasOnePlaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_command_in_exactly_one_place(self):
        handlers = _registered_handlers(bot.dp)
        self.assertIn("help", handlers)  # обход обработчиков действительно что-то видит
        menus = await _menus()
        cases = {
            "admin": (menus[1], await _help_text("admin"), SUPERADMIN_ONLY | ADMIN_NO_PLACE),
            "superadmin": (menus[2], await _help_text("superadmin"), set()),
        }
        for role, (menu, help_text, elsewhere) in cases.items():
            in_menu = {cmd for cmd, _ in menu}
            in_help = {c for cmds in _help_groups(help_text).values() for c in cmds}
            for command in sorted(handlers):
                with self.subTest(role=role, command=command):
                    places = [
                        name for name, bucket in (
                            ("синее меню", in_menu),
                            ("/help", in_help),
                            ("кнопки", ON_BUTTONS),
                            ("не включать", NOT_INCLUDED),
                            ("недоступна или без места", elsewhere),
                        ) if command in bucket
                    ]
                    self.assertEqual(len(places), 1, f"/{command} у {role}: {places}")
            listed = in_menu | in_help | ON_BUTTONS | NOT_INCLUDED | elsewhere
            self.assertEqual(listed - set(handlers), set(), f"{role}: в списках нет такой команды")

    def test_superadmin_only_commands_check_role(self):
        handlers = _registered_handlers(bot.dp)
        for command in SUPERADMIN_ONLY:
            with self.subTest(command=command):
                src = inspect.getsource(handlers[command])
                self.assertRegex(src, r"role\s*!=\s*[\"']superadmin[\"']")


if __name__ == "__main__":
    unittest.main()
