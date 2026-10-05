"""Перемещение денег клининга, задача 4 (docs/plans/2026-10-05-olya-money-move.md):
команда админов `/cleaning_move_delete` — удаление перемещения между кучками.

Без номера — список последних перемещений; с номером — проверка (не найдено /
удаление уведёт кучку в минус) и подтверждение «Провести». «Провести» удаляет с
повторной проверкой под замком (`delete_cash_move`), в чат — решение 6.

Сценарии гоняются через настоящий диспетчер `bot.dp` (`feed_update`), как в
`tests/test_cleaning_move_cmd.py`: заглушка `unknown` и «Отмена» из `bot.py`
стоят раньше роутера клининга. Без базы: слой данных мокнут, сеть подменена
сессией, которая только записывает запросы.
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
from cleaning.admin_ops import CashMoveDeleteGoesNegative
from cleaning.constants import CASH_HOLDER_DIMA, CASH_HOLDER_OLYA
from cleaning.format import format_cash_move_delete_alert
from cleaning.fsm import CleaningCashMoveDeleteFSM

UNRECOGNIZED = "Команда не распознана. Выберите действие на клавиатуре ниже."
CLEANING_MAIN_BUTTONS = ["🧹 Провести уборку", "🔍 Клиент", "💰 Баланс", "➖ Добавить расход"]
# Админ после операции — в своём главном меню (docs/plans/2026-10-05-admin-menu.md, задача 3).
ADMIN_ROOT_BUTTONS = [b.text for row in bot.admin_root_kb().keyboard for b in row]

_user_ids = itertools.count(930_001)
_update_ids = itertools.count(1)


def _move(move_id, cash_holder, amount, happened_at, comment=None):
    """Строка перемещения в том виде, как её отдаёт слой данных."""
    route = (
        "Деньги Ольга → Касса (Дима)"
        if cash_holder == CASH_HOLDER_OLYA
        else "Касса (Дима) → Деньги Ольга"
    )
    return {
        "id": move_id,
        "happened_at": happened_at,
        "cash_holder": cash_holder,
        "method": route,
        "amount": amount,
        "comment": comment,
    }


MOVE_12 = _move(
    12, CASH_HOLDER_OLYA, D("10000"), datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc)
)
MOVE_11 = _move(
    11, CASH_HOLDER_DIMA, D("3000"), datetime(2026, 10, 4, 6, 5, tzinfo=timezone.utc),
    comment="на расходы",
)


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


class _NullTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    """Роль из «staff», право — по флагу; остальное в сценариях мокнуто."""

    def __init__(self, role, allowed):
        self.role = role
        self.allowed = allowed

    def transaction(self):
        return _NullTx()

    async def fetchval(self, query, *args):
        if "FROM staff" in query:
            return self.role
        if "role_permissions" in query:
            return 1 if self.allowed else None
        raise AssertionError(f"unexpected fetchval: {query}")


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


class _DispatchCase(unittest.IsolatedAsyncioTestCase):
    """Один пользователь гонит сообщения через `bot.dp`."""

    role = "admin"
    allowed = True

    async def asyncSetUp(self):
        self.session = _RecordingSession()
        self.tg_bot = Bot("123456:TEST-cash-move-delete", session=self.session)
        self.user_id = next(_user_ids)
        self.pool = _Pool(_Conn(self.role, self.allowed))
        self.fsm = bot.dp.fsm.get_context(
            bot=self.tg_bot, chat_id=self.user_id, user_id=self.user_id
        )

        self.list_moves = AsyncMock(return_value=[MOVE_12, MOVE_11])
        self.check_delete = AsyncMock(return_value=MOVE_12)
        self.delete_move = AsyncMock(return_value=MOVE_12)
        self.get_olya_balance = AsyncMock(return_value=D("13500"))
        self.get_dima_balance = AsyncMock(return_value=D("41024"))
        self.send_flow = AsyncMock()
        patches = [
            mock.patch.object(cleaning_handlers, "list_recent_cash_moves", self.list_moves),
            mock.patch.object(cleaning_handlers, "check_cash_move_delete", self.check_delete),
            mock.patch.object(cleaning_handlers, "delete_cash_move", self.delete_move),
            mock.patch.object(cleaning_handlers, "get_olya_balance", self.get_olya_balance),
            mock.patch.object(cleaning_handlers, "get_dima_balance", self.get_dima_balance),
            mock.patch.object(cleaning_handlers, "send_cleaning_money_flow", self.send_flow),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.fsm.clear()

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

    def last_markup(self):
        return [m for m in self.session.sent if isinstance(m, SendMessage)][-1].reply_markup

    def last_markup_texts(self):
        return [button.text for row in self.last_markup().keyboard for button in row]

    async def state(self):
        return await self.fsm.get_state()

    async def data(self):
        return await self.fsm.get_data()

    def chat_text(self):
        self.send_flow.assert_awaited_once()
        return self.send_flow.await_args.args[1]


# ---------- сообщение в чат (решение 6) ----------


class CashMoveDeleteAlertFormatTests(unittest.TestCase):
    def test_olya_to_dima(self):
        text = format_cash_move_delete_alert(
            move_id=12,
            route="Деньги Ольга → Касса (Дима)",
            amount=D("10000"),
            olya_balance=D("13500"),
            dima_balance=D("41024"),
        )
        self.assertEqual(
            text,
            "🗑 Перемещение #12 удалено\n"
            "Деньги Ольга → Касса (Дима): 10 000₽\n"
            "\n"
            "Деньги Ольга: 13 500₽\n"
            "Касса (Дима): 41 024₽",
        )

    def test_dima_to_olya_negative_and_kopecks(self):
        text = format_cash_move_delete_alert(
            move_id=7,
            route="Касса (Дима) → Деньги Ольга",
            amount=D("2500.50"),
            olya_balance=D("0"),
            dima_balance=D("-1000"),
        )
        self.assertEqual(
            text,
            "🗑 Перемещение #7 удалено\n"
            "Касса (Дима) → Деньги Ольга: 2 500.50₽\n"
            "\n"
            "Деньги Ольга: 0₽\n"
            "Касса (Дима): -1 000₽",
        )


# ---------- доступ ----------


class NoPermissionTests(_DispatchCase):
    role = "cleaner"
    allowed = False

    async def test_refused_without_cleaning_manage_cash(self):
        for text in ("/cleaning_move_delete", "/cleaning_move_delete 12"):
            with self.subTest(text=text):
                replies = await self.send(text)
                self.assertEqual(replies, ["Команда доступна только администраторам."])
                self.assertIsNone(await self.state())
        self.list_moves.assert_not_awaited()
        self.check_delete.assert_not_awaited()


# ---------- без номера: список ----------


class ListTests(_DispatchCase):
    EXPECTED = (
        "Последние перемещения:\n"
        "#12 — Деньги Ольга → Касса (Дима): 10 000₽, 05.10 14:00\n"
        "#11 — Касса (Дима) → Деньги Ольга: 3 000₽, 04.10 09:05\n"
        "Удалить: /cleaning_move_delete N"
    )

    async def test_list_recent_moves(self):
        replies = await self.send("/cleaning_move_delete")
        self.assertEqual(replies, [self.EXPECTED])
        self.list_moves.assert_awaited_once()
        self.assertEqual(self.list_moves.await_args.kwargs, {})
        self.check_delete.assert_not_awaited()
        self.assertIsNone(await self.state())

    async def test_not_a_number_shows_list(self):
        replies = await self.send("/cleaning_move_delete abc")
        self.assertEqual(replies, [self.EXPECTED])
        self.check_delete.assert_not_awaited()

    async def test_no_moves_yet(self):
        self.list_moves.return_value = []
        replies = await self.send("/cleaning_move_delete")
        self.assertEqual(replies, ["Перемещений пока не было."])


# ---------- с номером: проверка и подтверждение ----------


class WithNumberTests(_DispatchCase):
    async def test_asks_confirmation(self):
        replies = await self.send("/cleaning_move_delete 12")
        self.assertEqual(
            replies, ["Удалить перемещение #12?\nДеньги Ольга → Касса (Дима): 10 000₽"]
        )
        self.assertEqual(self.last_markup_texts(), ["Провести", "Отменить"])
        self.assertEqual(await self.state(), CleaningCashMoveDeleteFSM.confirm.state)
        self.assertEqual((await self.data())["move_id"], 12)
        self.assertEqual(self.check_delete.await_args.kwargs, {"move_id": 12})
        self.delete_move.assert_not_awaited()

    async def test_asks_confirmation_dima_to_olya(self):
        self.check_delete.return_value = MOVE_11
        replies = await self.send("/cleaning_move_delete 11")
        self.assertEqual(
            replies, ["Удалить перемещение #11?\nКасса (Дима) → Деньги Ольга: 3 000₽"]
        )

    async def test_not_found_or_deleted(self):
        # Админ — в своём главном меню (docs/plans/2026-10-05-admin-menu.md, задача 3,
        # правка П2 из task-3-fixes.md).
        self.check_delete.return_value = None
        replies = await self.send("/cleaning_move_delete 99")
        self.assertEqual(replies, ["Перемещение #99 не найдено или уже удалено."])
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)
        self.delete_move.assert_not_awaited()

    async def test_number_beyond_int4_is_not_found(self):
        # Номер не помещается в integer базы — до базы не доходим, отвечаем
        # как на «не найдено». Админ — в своём главном меню (правка П2).
        replies = await self.send("/cleaning_move_delete 2147483648")
        self.assertEqual(replies, ["Перемещение #2147483648 не найдено или уже удалено."])
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)
        self.check_delete.assert_not_awaited()

    async def test_int4_max_goes_to_check(self):
        self.check_delete.return_value = None
        replies = await self.send("/cleaning_move_delete 2147483647")
        self.assertEqual(replies, ["Перемещение #2147483647 не найдено или уже удалено."])
        self.assertEqual(self.check_delete.await_args.kwargs, {"move_id": 2147483647})

    async def test_goes_negative_refused(self):
        # Админ — в своём главном меню (правка П2).
        self.check_delete.side_effect = CashMoveDeleteGoesNegative(
            CASH_HOLDER_DIMA, D("-3000")
        )
        replies = await self.send("/cleaning_move_delete 12")
        self.assertEqual(replies, ["Нельзя: в «Касса (Дима)» станет -3 000₽."])
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)
        self.delete_move.assert_not_awaited()

    async def test_new_number_replaces_pending_confirmation(self):
        # Подтверждение одного номера, потом команда с несуществующим номером:
        # старое подтверждение не должно остаться висеть. Второй ответ — «не
        # найдено», админ уходит в своё главное меню (правка П2).
        await self.send("/cleaning_move_delete 12")
        self.check_delete.return_value = None
        await self.send("/cleaning_move_delete 99")
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)


# ---------- «Провести» ----------


class ProvestiTests(_DispatchCase):
    async def _until_confirm(self):
        await self.send("/cleaning_move_delete 12")
        self.assertEqual(await self.state(), CleaningCashMoveDeleteFSM.confirm.state)

    async def test_deleted(self):
        # Админ — в своём главном меню (правка П2, task-3-fixes.md).
        await self._until_confirm()
        replies = await self.send("Провести")
        self.assertNotIn(UNRECOGNIZED, replies)
        self.assertEqual(replies, ["Перемещение #12 удалено."])
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)
        self.delete_move.assert_awaited_once()
        self.assertEqual(self.delete_move.await_args.kwargs, {"move_id": 12})
        self.assertEqual(
            self.chat_text(),
            "🗑 Перемещение #12 удалено\n"
            "Деньги Ольга → Касса (Дима): 10 000₽\n"
            "\n"
            "Деньги Ольга: 13 500₽\n"
            "Касса (Дима): 41 024₽",
        )

    async def test_deleted_dima_to_olya(self):
        self.check_delete.return_value = MOVE_11
        self.delete_move.return_value = MOVE_11
        await self.send("/cleaning_move_delete 11")
        self.get_olya_balance.return_value = D("500")
        self.get_dima_balance.return_value = D("54024")
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Перемещение #11 удалено."])
        self.assertEqual(
            self.chat_text(),
            "🗑 Перемещение #11 удалено\n"
            "Касса (Дима) → Деньги Ольга: 3 000₽\n"
            "\n"
            "Деньги Ольга: 500₽\n"
            "Касса (Дима): 54 024₽",
        )

    async def test_goes_negative_on_provesti(self):
        # Остаток изменился между подтверждением и «Провести». Админ — в своём
        # главном меню (правка П2, task-3-fixes.md).
        await self._until_confirm()
        self.delete_move.side_effect = CashMoveDeleteGoesNegative(
            CASH_HOLDER_DIMA, D("-2500")
        )
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Нельзя: в «Касса (Дима)» станет -2 500₽."])
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)
        self.send_flow.assert_not_awaited()
        self.get_olya_balance.assert_not_awaited()
        self.get_dima_balance.assert_not_awaited()

    async def test_already_deleted_on_provesti(self):
        # Админ — в своём главном меню (правка П2, task-3-fixes.md).
        await self._until_confirm()
        self.delete_move.return_value = None
        replies = await self.send("Провести")
        self.assertEqual(replies, ["Перемещение #12 не найдено или уже удалено."])
        self.assertEqual(self.last_markup_texts(), ADMIN_ROOT_BUTTONS)
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)
        self.send_flow.assert_not_awaited()

    async def test_control_provesti_without_state_unknown_answers(self):
        # Контроль самой проверки: вне сценария «Провести» ловит заглушка.
        with mock.patch.object(bot, "has_permission", AsyncMock(return_value=True)):
            replies = await self.send("Провести")
        self.assertEqual(replies, [UNRECOGNIZED])
        self.delete_move.assert_not_awaited()


# ---------- «Отмена» ----------


class CancelTests(_DispatchCase):
    async def test_otmena_on_confirm(self):
        # «Отмена» ловит `cancel_any` в bot.py. Право на отчёты выключено, чтобы
        # ответ решался по `cleaning_prefixes`: без группы там он пошёл бы в базу.
        with mock.patch.object(bot, "has_permission", AsyncMock(return_value=False)):
            await self.fsm.set_state(CleaningCashMoveDeleteFSM.confirm)
            await self.fsm.set_data({"move_id": 12})
            replies = await self.send("Отмена")
        self.assertEqual(replies, ["Отменено."])
        self.assertEqual(self.last_markup_texts(), CLEANING_MAIN_BUTTONS)
        self.assertIsNone(await self.state())
        self.delete_move.assert_not_awaited()
        self.send_flow.assert_not_awaited()

    async def test_otmenit_on_confirm(self):
        # Кнопка «Отменить» из `_confirm_kb()` — обработчик `cancel` роутера клининга.
        # Админ после неё — в своём главном меню, как после «Отмена»
        # (docs/plans/2026-10-05-admin-menu.md, задача 3).
        await self.send("/cleaning_move_delete 12")
        replies = await self.send("Отменить")
        self.assertEqual(replies, ["Отменено."])
        self.assertEqual(
            self.last_markup_texts(),
            [b.text for row in bot.admin_root_kb().keyboard for b in row],
        )
        self.assertEqual(await self.state(), bot.AdminMenuFSM.root.state)
        self.delete_move.assert_not_awaited()
        self.send_flow.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
