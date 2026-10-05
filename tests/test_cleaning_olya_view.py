"""Реестр денег Оли, задача 4 (docs/plans/2026-10-05-olya-money-register.md):
где виден остаток денег Оли.

- «💰 Баланс» / `/cleaning_balance`: под строкой кассы клининга у роли `cleaner`
  строка `У вас на руках: N₽`, у остальных, кому доступна команда, — `Деньги Оли: N₽`.
- `/cleaning_olya` (право `cleaning_manage_cash`): остаток и последние 10 операций
  `дата | ±сумма | вид/категория | комментарий`; операций нет — «Операций пока нет.».

Всё гоняется через настоящий диспетчер `bot.dp` (`feed_update`): заглушка `unknown`
и мост кнопок клинера в `bot.py` стоят раньше роутера клининга, тест показывает,
что команда и кнопка доходят до своих обработчиков. Без базы: остатки и список
операций мокнуты, права — по `bot.ROLE_MATRIX`, сеть подменена сессией, которая
только записывает запросы.
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

UNRECOGNIZED = "Команда не распознана. Выберите действие на клавиатуре ниже."

_user_ids = itertools.count(920_001)
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


class _Conn:
    """Роль из «staff», права по `bot.ROLE_MATRIX`; остальное мокнуто."""

    def __init__(self, role):
        self.role = role

    async def fetchval(self, query, *args):
        if "FROM staff" in query:
            return self.role
        if "role_permissions" in query:
            role, permission = args
            return 1 if permission in bot.ROLE_MATRIX.get(role, ()) else None
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetchrow(self, query, *args):
        # bot.get_user_role (мост кнопок клинера) читает роль через fetchrow
        if "FROM staff" in query:
            return {"role": self.role} if self.role else None
        raise AssertionError(f"unexpected fetchrow: {query}")


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


def _at(day, hour, minute):
    """Время операции в UTC; в выводе — по Москве (UTC+3)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


class _DispatchCase(unittest.IsolatedAsyncioTestCase):
    role = "admin"

    async def asyncSetUp(self):
        self.session = _RecordingSession()
        self.tg_bot = Bot("123456:TEST-olya-view", session=self.session)
        self.user_id = next(_user_ids)
        self.pool = _Pool(_Conn(self.role))
        self.list_olya_entries = AsyncMock(return_value=[])
        patches = [
            # мост кнопок клинера в bot.py берёт глобальный пул
            mock.patch.object(bot, "pool", self.pool),
            mock.patch.object(
                cleaning_handlers, "get_cleaning_balance", AsyncMock(return_value=D("50000"))
            ),
            mock.patch.object(
                cleaning_handlers, "get_olya_balance", AsyncMock(return_value=D("4200"))
            ),
            mock.patch.object(cleaning_handlers, "list_olya_entries", self.list_olya_entries),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

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


# ---------- «💰 Баланс» ----------


class CleanerBalanceTests(_DispatchCase):
    role = "cleaner"

    async def test_button_shows_money_on_hands(self):
        # кнопка клинера идёт через мост в bot.py
        replies = await self.send("💰 Баланс")
        self.assertEqual(replies, ["Касса клининга: 50 000₽\nУ вас на руках: 4 200₽"])

    async def test_command_shows_money_on_hands(self):
        replies = await self.send("/cleaning_balance")
        self.assertEqual(replies, ["Касса клининга: 50 000₽\nУ вас на руках: 4 200₽"])


class AdminBalanceTests(_DispatchCase):
    async def test_admin_sees_olya_money(self):
        replies = await self.send("/cleaning_balance")
        self.assertEqual(replies, ["Касса клининга: 50 000₽\nДеньги Ольга: 4 200₽"])

    async def test_superadmin_sees_olya_money(self):
        self.pool.conn.role = "superadmin"
        replies = await self.send("/cleaning_balance")
        self.assertEqual(replies, ["Касса клининга: 50 000₽\nДеньги Ольга: 4 200₽"])


# ---------- /cleaning_olya ----------


class CleaningOlyaCommandTests(_DispatchCase):
    async def test_balance_and_last_operations(self):
        self.list_olya_entries.return_value = [
            {"id": 9, "happened_at": _at(5, 11, 30), "kind": "income",
             "method": "Наличные", "amount": D("5000"), "comment": "Заказ #12",
             "order_id": 12},
            {"id": 8, "happened_at": _at(5, 11, 30), "kind": "expense",
             "method": "Химия", "amount": D("300.50"), "comment": "Химия",
             "order_id": 12},
            {"id": 7, "happened_at": _at(4, 21, 5), "kind": "dividend",
             "method": "Касса клининга", "amount": D("1000"), "comment": "Дивиденды",
             "order_id": None},
            {"id": 6, "happened_at": _at(4, 6, 0), "kind": "deposit",
             "method": "Карта", "amount": D("2000"), "comment": None,
             "order_id": None},
            {"id": 5, "happened_at": _at(3, 9, 15), "kind": "withdrawal",
             "method": "Касса клининга", "amount": D("500"), "comment": "  ",
             "order_id": None},
        ]
        replies = await self.send("/cleaning_olya")
        self.assertEqual(
            replies,
            [
                "Деньги Ольга: 4 200₽\n"
                "\n"
                "Последние операции:\n"
                "05.10 14:30 | +5 000₽ | Приход/Наличные | Заказ #12\n"
                "05.10 14:30 | -300.50₽ | Расход/Химия | Химия\n"
                "05.10 00:05 | -1 000₽ | Выплата прибыли | Дивиденды\n"
                "04.10 09:00 | +2 000₽ | Внесение/Карта | —\n"
                "03.10 12:15 | -500₽ | Изъятие | —"
            ],
        )
        self.list_olya_entries.assert_awaited_once()
        self.assertEqual(self.list_olya_entries.await_args.kwargs, {"limit": 10})

    async def test_no_operations(self):
        replies = await self.send("/cleaning_olya")
        self.assertEqual(replies, ["Деньги Ольга: 4 200₽\nОпераций пока нет."])

    async def test_unknown_stub_does_not_intercept(self):
        # заглушка ловит только текст не с «/»: команда доходит до роутера клининга
        replies = await self.send("/cleaning_olya")
        self.assertNotIn(UNRECOGNIZED, replies)
        self.assertEqual(len(replies), 1)


class CleaningOlyaAccessTests(_DispatchCase):
    role = "cleaner"

    async def test_cleaner_without_manage_cash_is_refused(self):
        self.assertNotIn("cleaning_manage_cash", bot.ROLE_MATRIX["cleaner"])
        replies = await self.send("/cleaning_olya")
        self.assertEqual(replies, ["Команда доступна только администраторам."])
        self.list_olya_entries.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
