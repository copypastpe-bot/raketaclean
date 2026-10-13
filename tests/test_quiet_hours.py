"""Ночная тишина: с 20:00 до 10:00 по Москве робот клиентам по своей инициативе не пишет.

ТЗ docs/plans/2026-10-13-quiet-hours.md, задача 1 (2026-10-13).

Здесь — три части, на которых стоит тишина:

* расчёт «ночь ли сейчас и до какого часа ждать» (`notifications/quiet_hours.py`):
  границы 20:00 — уже ночь, 10:00 — уже можно; пояс входа роли не играет;
* признак «ответ клиенту» в правилах (`"reply": true` в
  `docs/notification_rules.json`): ровно у четырёх событий из решения
  владельца — на ответы тишина не действует;
* отсрочка письма в очереди (`defer_outbox_entry`): письмо возвращается
  в очередь к 10:00, попытка на него не тратится, ушедшее и отменённое
  не трогаются. На настоящем Postgres (DSN в `TEST_DB_DSN`, без него класс
  пропускается), своя схема.

Поведение работника очереди ночью — в `tests/test_notification_worker.py`.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg

from notifications.outbox import (
    defer_outbox_entry,
    enqueue_notification,
    ensure_notification_schema,
    pick_ready_batch,
)
from notifications.quiet_hours import quiet_until
from notifications.rules import load_notification_rules

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
SCHEMA = "quiet_hours_test"
MSK = ZoneInfo("Europe/Moscow")
RULES_PATH = Path(__file__).resolve().parent.parent / "docs" / "notification_rules.json"

REPLY_EVENTS = {
    "order_confirmed_thanks",
    "order_rating_response_high_client",
    "order_rating_response_mid_client",
    "order_rating_response_low_client",
}


def msk(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=MSK)


class QuietUntilTests(unittest.TestCase):
    """Вход — момент времени, выход — `None` (можно слать) или ближайшие 10:00 МСК."""

    def test_evening_before_eight_is_still_day(self):
        self.assertIsNone(quiet_until(msk(13, 19, 59)))

    def test_eight_pm_is_already_night_until_tomorrow_ten(self):
        self.assertEqual(quiet_until(msk(13, 20, 0)), msk(14, 10, 0))

    def test_before_midnight_waits_until_tomorrow_ten(self):
        self.assertEqual(quiet_until(msk(13, 23, 59)), msk(14, 10, 0))

    def test_after_midnight_waits_until_ten_the_same_morning(self):
        self.assertEqual(quiet_until(msk(14, 0, 30)), msk(14, 10, 0))

    def test_a_minute_before_ten_still_waits_until_ten(self):
        self.assertEqual(quiet_until(msk(14, 9, 59)), msk(14, 10, 0))

    def test_ten_am_is_already_day(self):
        self.assertIsNone(quiet_until(msk(14, 10, 0)))

    def test_utc_and_moscow_input_give_the_same_answer(self):
        """Работник берёт часы в UTC: 17:00 UTC — это 20:00 по Москве."""
        cases = (
            (datetime(2026, 10, 13, 17, 0, tzinfo=timezone.utc), msk(13, 20, 0)),
            (datetime(2026, 10, 13, 16, 59, tzinfo=timezone.utc), msk(13, 19, 59)),
            (datetime(2026, 10, 14, 6, 59, tzinfo=timezone.utc), msk(14, 9, 59)),
            (datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc), msk(14, 10, 0)),
        )
        for utc_moment, msk_moment in cases:
            with self.subTest(utc=utc_moment):
                self.assertEqual(quiet_until(utc_moment), quiet_until(msk_moment))

    def test_answer_is_ten_o_clock_moscow(self):
        until = quiet_until(datetime(2026, 10, 13, 22, 0, tzinfo=timezone.utc))
        local = until.astimezone(MSK)
        self.assertEqual((local.day, local.hour, local.minute), (14, 10, 0))

    def test_moment_without_time_zone_is_refused(self):
        """Наивное время — неизвестно, в каком поясе: ошибка в нём — письмо ночью."""
        with self.assertRaises(ValueError):
            quiet_until(datetime(2026, 10, 13, 21, 0))


class ReplyFlagInRulesTests(unittest.TestCase):
    """Признак «ответ клиенту» живёт в правилах, чтобы код и правила не расходились."""

    def setUp(self):
        self.rules = load_notification_rules(RULES_PATH)

    def test_exactly_four_events_are_replies(self):
        replies = {key for key, event in self.rules._events.items() if event.reply}
        self.assertEqual(replies, REPLY_EVENTS)

    def test_event_without_the_flag_is_not_a_reply(self):
        self.assertFalse(self.rules.get_event("order_confirm_request").reply)
        self.assertFalse(self.rules.get_event("order_completed_summary").reply)


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class DeferOutboxEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            await admin.execute(f"CREATE SCHEMA {SCHEMA}")
            await admin.execute(f"SET search_path TO {SCHEMA}")
            # Только то, что читает очередь писем, — не копия прода.
            await admin.execute(
                """
                CREATE TABLE clients (
                    id                serial PRIMARY KEY,
                    full_name         text,
                    phone             text,
                    bot_tg_user_id    bigint,
                    bot_started       boolean,
                    preferred_contact text
                )
                """
            )
            await ensure_notification_schema(admin)
        finally:
            await admin.close()
        self.conn = await asyncpg.connect(
            TEST_DB_DSN, server_settings={"search_path": SCHEMA}
        )
        self.rules = load_notification_rules(RULES_PATH)
        self.client_id = await self.conn.fetchval(
            "INSERT INTO clients (full_name, phone) VALUES ('Анна', '+79161112233') "
            "RETURNING id"
        )
        self.morning = msk(14, 10, 0)

    async def asyncTearDown(self):
        await self.conn.close()
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        finally:
            await admin.close()

    async def _letter(self, *, status="pending"):
        outbox_id = await enqueue_notification(
            self.conn, self.rules, event_key="order_completed_summary",
            client_id=self.client_id, payload={},
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        if status != "pending":
            await self.conn.execute(
                "UPDATE notification_outbox SET status=$2 WHERE id=$1", outbox_id, status)
        return outbox_id

    async def _row(self, outbox_id):
        return await self.conn.fetchrow(
            "SELECT status, attempts, scheduled_at FROM notification_outbox WHERE id=$1",
            outbox_id)

    async def test_letter_taken_by_the_worker_goes_back_to_the_queue_until_morning(self):
        """Работник взял письмо (`sending`, попытка засчитана) — ночью оно
        возвращается в очередь к 10:00, и попытка снимается."""
        outbox_id = await self._letter()
        [entry] = await pick_ready_batch(self.conn, 10)
        self.assertEqual(entry.id, outbox_id)
        self.assertEqual((await self._row(outbox_id))["attempts"], 1)

        done = await defer_outbox_entry(self.conn, outbox_id, self.morning)

        self.assertTrue(done)
        row = await self._row(outbox_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["scheduled_at"], self.morning)

    async def test_pending_letter_moves_without_spending_an_attempt(self):
        outbox_id = await self._letter()

        done = await defer_outbox_entry(self.conn, outbox_id, self.morning)

        self.assertTrue(done)
        row = await self._row(outbox_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["scheduled_at"], self.morning)

    async def test_sent_and_cancelled_letters_are_not_touched(self):
        for status in ("sent", "cancelled", "failed"):
            with self.subTest(status=status):
                outbox_id = await self._letter(status=status)
                before = await self._row(outbox_id)

                done = await defer_outbox_entry(self.conn, outbox_id, self.morning)

                self.assertFalse(done)
                self.assertEqual(await self._row(outbox_id), before)


if __name__ == "__main__":
    unittest.main()
