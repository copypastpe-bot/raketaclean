"""Сторож проведения заказов: рассылка, кнопка «Напомнить завтра», выключатели (bot.py).

ТЗ docs/plans/2026-10-13-order-close-reminder.md, задача 2 (2026-10-13).

`run_order_close_reminders(now)` раз в 5 минут обновляет напоминания по
кандидатам (`_order_close_candidates`, задача 1) и рассылает наступившие в два
рабочих чата: первое — на следующий день после записи с 10:00 МСК, повтор —
каждые 30 минут до 20:00 (прежнее сообщение удаляется, приходит новое).
Кнопка «Напомнить завтра» (`order_close_snooze_cb`) — тишина до 10:00
следующего дня, сообщение в обоих чатах меняется на «Отложено до завтра — <кто>».

На настоящем Postgres (DSN в `TEST_DB_DSN`, без него класс пропускается):
своя схема, как в `tests/test_order_close_candidates.py`; Telegram подменён
(записывает отправки, удаления и правки), часы — параметром `now` у рассылки
и `bot._order_close_now` у кнопки.
"""

import json
import os
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import asyncpg
from aiogram.types import CallbackQuery, Chat, Message, User

import bot
from notifications import order_close_reminder as ocr

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "app" / "migrations"
SCHEMA = "order_close_dispatch_test"
MSK = ZoneInfo("Europe/Moscow")

CONFIRM_CHAT = -1001111
STAFF_CHAT = -1002222
ORDER_DATE = date(2026, 10, 10)
PHONE = "9161112233"
EVENT = "evt-valery"
JOB = dict(client_name="Валерий", address="Афонино, Восточная, 342", services=["carpeting", "furniture"])


def _migration(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def _msk(day: int, hour: int, minute: int = 0) -> datetime:
    """Московское время 10.2026: `_msk(11, 10)` — 11.10.2026 10:00 МСК."""
    return datetime(2026, 10, day, hour, minute, tzinfo=MSK)


class FakeQuery:
    def __init__(self, reminder_id, *, chat_id, message_id, user_id=777, full_name="Иван Петров"):
        self.from_user = mock.Mock(id=user_id, full_name=full_name, username=None)
        self.data = f"{bot.ORDER_CLOSE_SNOOZE_CB_PREFIX}{reminder_id}"
        self.message = mock.Mock(chat=mock.Mock(id=chat_id), message_id=message_id)
        self.answer = mock.AsyncMock()


class _DispatchCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            await admin.execute(f"CREATE SCHEMA {SCHEMA}")
            await admin.execute(f"SET search_path TO {SCHEMA}")
            await admin.execute("CREATE TABLE clients (id serial PRIMARY KEY, phone_digits text)")
            await admin.execute(
                """
                CREATE TABLE orders (
                    id                serial PRIMARY KEY,
                    phone_digits      text,
                    created_at        timestamptz NOT NULL DEFAULT now(),
                    calendar_event_id text
                )
                """
            )
            await admin.execute(_migration("0006_cleaning.sql"))
            await admin.execute(
                """
                CREATE TABLE calendar_jobs (
                    event_id        text PRIMARY KEY,
                    phone10         text,
                    phones          text[],
                    order_date      date,
                    client_name     text,
                    address         text,
                    services        text[],
                    primary_lead_id bigint,
                    real_lead_id    bigint,
                    status          text,
                    order_id        integer
                )
                """
            )
            await admin.execute(_migration("0018_order_close_reminders.sql"))
        finally:
            await admin.close()
        self.pool = await asyncpg.create_pool(
            TEST_DB_DSN, min_size=1, max_size=4, server_settings={"search_path": SCHEMA}
        )
        for target, value in (
            ("pool", self.pool),
            ("ORDER_CLOSE_JOBS_SOURCE", f"{SCHEMA}.calendar_jobs"),
            ("ORDER_CLOSE_REMINDER_ENABLED", True),
            ("ORDERS_CONFIRM_CHAT_ID", CONFIRM_CHAT),
            ("STAFF_CHAT_ID", STAFF_CHAT),
        ):
            patcher = mock.patch.object(bot, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.send_message = self._patch_bot("send_message")
        self.delete_message = self._patch_bot("delete_message")
        self.edit_message_text = self._patch_bot("edit_message_text")
        self.edit_message_reply_markup = self._patch_bot("edit_message_reply_markup")
        self._next_message_id = 500
        self.send_message.side_effect = self._fake_send

    async def asyncTearDown(self):
        await self.pool.close()
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        finally:
            await admin.close()

    def _patch_bot(self, name):
        patcher = mock.patch.object(bot.bot, name, mock.AsyncMock())
        method = patcher.start()
        self.addCleanup(patcher.stop)
        return method

    async def _fake_send(self, chat_id, text, **kwargs):
        self._next_message_id += 1
        return mock.Mock(message_id=self._next_message_id)

    def _reset_telegram(self):
        for method in (self.send_message, self.delete_message, self.edit_message_text,
                       self.edit_message_reply_markup):
            method.reset_mock()

    async def _execute(self, sql, *args):
        async with self.pool.acquire() as conn:
            return await conn.execute(sql, *args)

    async def _job(self, event_id=EVENT, order_date=ORDER_DATE, *, phones=(PHONE,), **fields):
        job = {**JOB, **fields}
        await self._execute(
            """
            INSERT INTO calendar_jobs
                (event_id, phone10, phones, order_date, client_name, address, services, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'done')
            """,
            event_id, phones[0], list(phones), order_date,
            job["client_name"], job["address"], list(job["services"]),
        )

    async def _reminder(self, event_id=EVENT):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM order_close_reminders WHERE event_id=$1", event_id
            )

    async def _run(self, moment):
        await bot.run_order_close_reminders(moment)

    def _sent(self):
        return [(c.args[0], c.args[1]) for c in self.send_message.await_args_list]

    def _deleted(self):
        return sorted(c.args for c in self.delete_message.await_args_list)

    def _edits(self):
        return {
            (c.kwargs["chat_id"], c.kwargs["message_id"]): c.kwargs
            for c in self.edit_message_text.await_args_list
        }

    @staticmethod
    def _messages(row):
        return {int(chat): message for chat, message in json.loads(row["messages"]).items()}


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class DispatchTests(_DispatchCase):
    async def test_nothing_before_next_day_ten(self):
        await self._job()

        await self._run(_msk(10, 21))  # день работы: запись ещё не кандидат
        self.assertIsNone(await self._reminder())
        await self._run(_msk(11, 9, 55))  # следующий день, до 10:00

        self.send_message.assert_not_awaited()
        row = await self._reminder()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["next_send_at"], _msk(11, 10))

    async def test_at_ten_one_message_to_each_chat(self):
        await self._job()

        await self._run(_msk(11, 10))

        expected = ocr.render_text(order_date=ORDER_DATE, **JOB)
        self.assertEqual(self._sent(), [(CONFIRM_CHAT, expected), (STAFF_CHAT, expected)])
        row = await self._reminder()
        for call in self.send_message.await_args_list:
            self.assertIsNone(call.kwargs["parse_mode"])
            buttons = [b for line in call.kwargs["reply_markup"].inline_keyboard for b in line]
            self.assertEqual(
                [(b.text, b.callback_data) for b in buttons],
                [("Напомнить завтра", f"order_close_snooze:{row['id']}")],
            )
        self.assertEqual(self._messages(row), {CONFIRM_CHAT: 501, STAFF_CHAT: 502})
        self.assertEqual(row["next_send_at"], _msk(11, 10, 30))
        self.delete_message.assert_not_awaited()

    async def test_after_thirty_minutes_old_deleted_new_sent(self):
        await self._job()
        await self._run(_msk(11, 10))
        self._reset_telegram()

        await self._run(_msk(11, 10, 25))  # ещё рано
        self.send_message.assert_not_awaited()
        await self._run(_msk(11, 10, 30))

        self.assertEqual(self._deleted(), [(STAFF_CHAT, 502), (CONFIRM_CHAT, 501)])
        self.assertEqual([chat for chat, _ in self._sent()], [CONFIRM_CHAT, STAFF_CHAT])
        row = await self._reminder()
        self.assertEqual(self._messages(row), {CONFIRM_CHAT: 503, STAFF_CHAT: 504})
        self.assertEqual(row["next_send_at"], _msk(11, 11))

    async def test_evening_and_night_are_silent(self):
        await self._job()
        await self._run(_msk(11, 19, 40))
        row = await self._reminder()
        self.assertEqual(row["next_send_at"], _msk(12, 10))  # 20:10 вне окна → утро
        # Даже если время наступило (бот лежал), с 20:00 до 10:00 — тишина.
        await self._execute("UPDATE order_close_reminders SET next_send_at=$1", _msk(11, 19, 55))
        self._reset_telegram()

        for moment in (_msk(11, 20), _msk(11, 23), _msk(12, 3), _msk(12, 9, 59)):
            await self._run(moment)

        self.send_message.assert_not_awaited()
        self.delete_message.assert_not_awaited()
        await self._run(_msk(12, 10))
        self.assertEqual(self.send_message.await_count, 2)

    async def test_conducted_marks_done_and_goes_silent(self):
        await self._job()
        await self._run(_msk(11, 10))
        self._reset_telegram()
        await self._execute(
            "INSERT INTO orders (phone_digits, created_at, calendar_event_id) VALUES (NULL, $1, $2)",
            _msk(11, 10, 3), EVENT,
        )

        await self._run(_msk(11, 10, 5))

        edits = self._edits()
        self.assertEqual(set(edits), {(CONFIRM_CHAT, 501), (STAFF_CHAT, 502)})
        for kwargs in edits.values():
            self.assertEqual(kwargs["text"], ocr.render_done_text(order_date=ORDER_DATE, **JOB))
            self.assertIsNone(kwargs["parse_mode"])
            self.assertEqual(kwargs["reply_markup"].inline_keyboard, [])
        row = await self._reminder()
        self.assertEqual(row["status"], "closed")
        self.assertIsNotNone(row["closed_at"])
        self._reset_telegram()
        for moment in (_msk(11, 10, 30), _msk(11, 15), _msk(12, 10)):
            await self._run(moment)
        self.send_message.assert_not_awaited()
        self.edit_message_text.assert_not_awaited()
        self.delete_message.assert_not_awaited()

    async def test_gone_from_calendar_deletes_silently(self):
        await self._job()
        await self._run(_msk(11, 10))
        self._reset_telegram()
        await self._execute("DELETE FROM calendar_jobs WHERE event_id=$1", EVENT)

        await self._run(_msk(11, 10, 5))

        self.assertEqual(self._deleted(), [(STAFF_CHAT, 502), (CONFIRM_CHAT, 501)])
        self.send_message.assert_not_awaited()
        self.edit_message_text.assert_not_awaited()
        row = await self._reminder()
        self.assertEqual(row["status"], "gone")
        self.assertEqual(self._messages(row), {})
        self._reset_telegram()
        await self._run(_msk(11, 10, 30))
        await self._run(_msk(12, 10))
        self.send_message.assert_not_awaited()
        self.delete_message.assert_not_awaited()

    async def test_staff_chat_zero_sends_only_to_orders_chat(self):
        await self._job()
        with mock.patch.object(bot, "STAFF_CHAT_ID", 0):
            await self._run(_msk(11, 10))

        self.assertEqual([chat for chat, _ in self._sent()], [CONFIRM_CHAT])
        self.assertEqual(self._messages(await self._reminder()), {CONFIRM_CHAT: 501})

    async def test_switch_off_does_nothing(self):
        await self._job()
        with mock.patch.object(bot, "ORDER_CLOSE_REMINDER_ENABLED", False):
            await self._run(_msk(11, 10))

        self.send_message.assert_not_awaited()
        self.assertIsNone(await self._reminder())

    async def test_one_chat_fails_other_chat_and_other_orders_go_on(self):
        await self._job()
        await self._job("evt-second", phones=("9165554433",), client_name="Лора")

        async def flaky_send(chat_id, text, **kwargs):
            if chat_id == CONFIRM_CHAT:
                raise RuntimeError("chat not found")
            return await self._fake_send(chat_id, text, **kwargs)

        self.send_message.side_effect = flaky_send

        await self._run(_msk(11, 10))

        staff_texts = [text for chat, text in self._sent() if chat == STAFF_CHAT]
        self.assertEqual(len(staff_texts), 2)
        for event_id in (EVENT, "evt-second"):
            row = await self._reminder(event_id)
            self.assertEqual(list(self._messages(row)), [STAFF_CHAT])
            # Время сдвигается всё равно — без повторов каждые 5 минут.
            self.assertEqual(row["next_send_at"], _msk(11, 10, 30))

    async def test_record_moved_to_future_drops_message_and_waits_for_new_date(self):
        await self._job()
        await self._run(_msk(11, 10))
        self._reset_telegram()
        new_date = date(2026, 10, 13)
        await self._execute("UPDATE calendar_jobs SET order_date=$1", new_date)

        await self._run(_msk(11, 10, 5))

        self.assertEqual(self._deleted(), [(STAFF_CHAT, 502), (CONFIRM_CHAT, 501)])
        self.send_message.assert_not_awaited()
        row = await self._reminder()
        self.assertEqual(row["status"], "open")
        self.assertEqual(self._messages(row), {})
        self.assertEqual(row["order_date"], new_date)
        self.assertEqual(row["next_send_at"], _msk(14, 10))
        self._reset_telegram()
        for moment in (_msk(11, 10, 35), _msk(12, 12), _msk(13, 19)):
            await self._run(moment)
        self.send_message.assert_not_awaited()
        self.delete_message.assert_not_awaited()
        await self._run(_msk(14, 10))
        self.assertEqual(self.send_message.await_count, 2)

    async def test_closed_reminder_reopens_when_order_removed(self):
        await self._job()
        await self._run(_msk(11, 10))
        await self._execute(
            "INSERT INTO orders (phone_digits, created_at, calendar_event_id) VALUES (NULL, $1, $2)",
            _msk(11, 10, 3), EVENT,
        )
        await self._run(_msk(11, 10, 5))
        self.assertEqual((await self._reminder())["status"], "closed")
        self._reset_telegram()
        await self._execute("DELETE FROM orders")  # заказ в боте удалили

        await self._run(_msk(11, 13, 7))  # ближайшее напоминание — сразу

        self.assertEqual(self._deleted(), [(STAFF_CHAT, 502), (CONFIRM_CHAT, 501)])
        self.assertEqual([chat for chat, _ in self._sent()], [CONFIRM_CHAT, STAFF_CHAT])
        row = await self._reminder()
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["closed_at"])
        self.assertEqual(row["next_send_at"], _msk(11, 13, 37))

    async def test_gone_reminder_reopens_when_record_returns(self):
        await self._job()
        await self._run(_msk(11, 10))
        await self._execute("DELETE FROM calendar_jobs")
        await self._run(_msk(11, 10, 5))
        self.assertEqual((await self._reminder())["status"], "gone")
        self._reset_telegram()
        await self._job()  # запись вернули в календарь

        await self._run(_msk(11, 21))  # ночью — только открыть, без отправки
        self.send_message.assert_not_awaited()
        row = await self._reminder()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["next_send_at"], _msk(12, 10))
        await self._run(_msk(12, 10))
        self.assertEqual(self.send_message.await_count, 2)


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class SnoozeTests(_DispatchCase):
    async def _press(self, moment, **kwargs):
        row = await self._reminder()
        query = FakeQuery(row["id"], **kwargs)
        with mock.patch.object(bot, "_order_close_now", return_value=moment):
            await bot.order_close_snooze_cb(query)
        return query

    async def test_snooze_edits_both_chats_and_silences_until_tomorrow(self):
        await self._job()
        await self._run(_msk(11, 10))
        self._reset_telegram()

        query = await self._press(_msk(11, 10, 10), chat_id=STAFF_CHAT, message_id=502)

        query.answer.assert_awaited_once_with()
        edits = self._edits()
        self.assertEqual(set(edits), {(CONFIRM_CHAT, 501), (STAFF_CHAT, 502)})
        expected = ocr.render_snoozed_text(order_date=ORDER_DATE, **JOB, snoozed_by="Иван Петров")
        for kwargs in edits.values():
            self.assertEqual(kwargs["text"], expected)
            self.assertIsNone(kwargs["parse_mode"])
            self.assertEqual(kwargs["reply_markup"].inline_keyboard, [])
        row = await self._reminder()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["next_send_at"], _msk(12, 10))
        self.assertEqual(row["snoozed_by"], 777)
        self.assertEqual(row["snoozed_by_name"], "Иван Петров")
        self.assertEqual(row["snoozed_at"], _msk(11, 10, 10))
        self._reset_telegram()
        for moment in (_msk(11, 10, 40), _msk(11, 15), _msk(11, 19, 55), _msk(12, 9, 55)):
            await self._run(moment)
        self.send_message.assert_not_awaited()
        self.delete_message.assert_not_awaited()
        # Завтра заказ всё ещё не проведён — напоминание снова, с кнопкой.
        await self._run(_msk(12, 10))
        self.assertEqual(self._deleted(), [(STAFF_CHAT, 502), (CONFIRM_CHAT, 501)])
        self.assertEqual([chat for chat, _ in self._sent()], [CONFIRM_CHAT, STAFF_CHAT])
        self.assertEqual((await self._reminder())["next_send_at"], _msk(12, 10, 30))

    async def test_snooze_after_midnight_waits_for_next_calendar_day(self):
        await self._job()
        await self._run(_msk(11, 19, 40))

        await self._press(_msk(12, 1, 30), chat_id=CONFIRM_CHAT, message_id=501)

        self.assertEqual((await self._reminder())["next_send_at"], _msk(13, 10))

    async def test_press_on_conducted_order_answers_and_drops_button(self):
        await self._job()
        await self._run(_msk(11, 10))
        await self._execute(
            "INSERT INTO orders (phone_digits, created_at, calendar_event_id) VALUES (NULL, $1, $2)",
            _msk(11, 10, 3), EVENT,
        )
        await self._run(_msk(11, 10, 5))
        self._reset_telegram()

        query = await self._press(_msk(11, 10, 7), chat_id=CONFIRM_CHAT, message_id=501)

        query.answer.assert_awaited_once_with("Заказ уже проведён")
        self.edit_message_text.assert_not_awaited()
        call = self.edit_message_reply_markup.await_args
        self.assertEqual((call.kwargs["chat_id"], call.kwargs["message_id"]), (CONFIRM_CHAT, 501))
        self.assertEqual(call.kwargs["reply_markup"].inline_keyboard, [])
        row = await self._reminder()
        self.assertEqual(row["status"], "closed")
        self.assertIsNone(row["snoozed_by"])

    async def test_unknown_reminder(self):
        query = FakeQuery(999, chat_id=CONFIRM_CHAT, message_id=1)
        with mock.patch.object(bot, "_order_close_now", return_value=_msk(11, 10)):
            await bot.order_close_snooze_cb(query)
        query.answer.assert_awaited_once_with("Напоминание не найдено.")
        self.edit_message_text.assert_not_awaited()


class GroupCallbackMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    """Кнопка живёт в рабочих чатах (группах), а бот глушит всё не из личных чатов."""

    @staticmethod
    def _callback(data, chat_type):
        chat = Chat(id=STAFF_CHAT if chat_type != "private" else 777, type=chat_type)
        message = Message(message_id=502, date=datetime(2026, 10, 11, 7, 0), chat=chat)
        user = User(id=777, is_bot=False, first_name="Иван")
        return CallbackQuery(id="1", from_user=user, chat_instance="ci", data=data, message=message), chat

    async def _passes(self, data, chat_type):
        handler = mock.AsyncMock(return_value="handled")
        event, chat = self._callback(data, chat_type)
        result = await bot.IgnoreNonPrivateMiddleware()(handler, event, {"event_chat": chat})
        return handler.await_count == 1 and result == "handled"

    async def test_snooze_button_passes_in_group(self):
        self.assertTrue(await self._passes("order_close_snooze:5", "supergroup"))
        self.assertTrue(await self._passes("order_close_snooze:5", "group"))

    async def test_other_group_callbacks_still_swallowed(self):
        self.assertFalse(await self._passes("unsorted_done:5", "supergroup"))

    async def test_private_chat_unchanged(self):
        self.assertTrue(await self._passes("unsorted_done:5", "private"))

    async def test_group_message_still_swallowed(self):
        handler = mock.AsyncMock()
        chat = Chat(id=STAFF_CHAT, type="supergroup")
        message = Message(message_id=1, date=datetime(2026, 10, 11, 7, 0), chat=chat, text="order_close_snooze:5")
        await bot.IgnoreNonPrivateMiddleware()(handler, message, {"event_chat": chat})
        handler.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
