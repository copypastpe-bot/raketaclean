"""Сторож проведения заказов: выбор кандидатов и проверка «проведён» (bot.py).

ТЗ docs/plans/2026-10-13-order-close-reminder.md, задача 1 (2026-10-13).

`_order_close_candidates(conn, today)` берёт записи календаря с датой от
«сегодня − 14» до «вчера» (МСК), которые не проведены, плюс все записи
с открытым напоминанием любой давности. «Проведён» — любое из (с
`order_date − 1 день`, без верхней границы): заказ химчистки, привязанный к
записи (`calendar_event_id`); заказ химчистки по любому номеру записи; не
удалённая уборка клининга клиента с любым номером записи. Открытое
напоминание, чья запись пропала из календаря, приходит с признаком `gone`.

На настоящем Postgres (DSN в `TEST_DB_DSN`, без него класс пропускается):
своя схема; `orders` и `clients` — минимальные (полная схема не из миграций),
уборки — файлом миграции 0006, таблица напоминаний — файлом 0018, как на
проде; представление `adminbot.calendar_jobs` заменено таблицей с теми же
колонками в своей схеме (`bot.ORDER_CLOSE_JOBS_SOURCE` подменяется).
"""

import os
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import asyncpg

import bot

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "app" / "migrations"
MIGRATION = "0018_order_close_reminders.sql"
SCHEMA = "order_close_candidates_test"
MSK = ZoneInfo("Europe/Moscow")

TODAY = date(2026, 10, 13)
PHONE_A = "9161112233"
PHONE_B = "9165554433"


def _migration(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def _msk(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MSK)


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class OrderCloseCandidatesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            await admin.execute(f"CREATE SCHEMA {SCHEMA}")
            await admin.execute(f"SET search_path TO {SCHEMA}")
            # Только то, что читает выборка, — не копия прода.
            await admin.execute(
                "CREATE TABLE clients (id serial PRIMARY KEY, phone_digits text)"
            )
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
            # Те же колонки, что у представления adminbot.calendar_jobs
            # (raketa-admin-bot/migrations/015_calendar_jobs.sql).
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
        finally:
            await admin.close()
        self.conn = await asyncpg.connect(TEST_DB_DSN, server_settings={"search_path": SCHEMA})
        # Таблица напоминаний — файлом миграции, дважды: повторный прогон на выкате не падает.
        await self.conn.execute(_migration(MIGRATION))
        await self.conn.execute(_migration(MIGRATION))
        patcher = mock.patch.object(bot, "ORDER_CLOSE_JOBS_SOURCE", f"{SCHEMA}.calendar_jobs")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.conn.close()
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        finally:
            await admin.close()

    # --- помощники ---

    async def _job(self, event_id, order_date, *, phones=(PHONE_A,), name="Валерий",
                   address="Афонино, Восточная, 342", services=("carpeting", "furniture")):
        await self.conn.execute(
            """
            INSERT INTO calendar_jobs
                (event_id, phone10, phones, order_date, client_name, address, services, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'done')
            """,
            event_id, phones[0] if phones else None, list(phones), order_date,
            name, address, list(services),
        )

    async def _order(self, *, phone_digits=None, created_at, event_id=None):
        await self.conn.execute(
            "INSERT INTO orders (phone_digits, created_at, calendar_event_id) VALUES ($1, $2, $3)",
            phone_digits, created_at, event_id,
        )

    async def _cleaning(self, phone_digits, *, happened_at, deleted=False):
        client_id = await self.conn.fetchval(
            "INSERT INTO clients (phone_digits) VALUES ($1) RETURNING id", phone_digits
        )
        foreman_id = await self.conn.fetchval(
            "INSERT INTO cleaning_foremen (fn) VALUES ('Ольга') RETURNING id"
        )
        await self.conn.execute(
            """
            INSERT INTO cleaning_orders
                (client_id, foreman_id, address, total_amount, happened_at, deleted_at)
            VALUES ($1, $2, 'Мира, 1', 5000, $3, CASE WHEN $4 THEN now() END)
            """,
            client_id, foreman_id, happened_at, deleted,
        )

    async def _reminder(self, event_id, order_date, *, status="open"):
        return await self.conn.fetchval(
            """
            INSERT INTO order_close_reminders (event_id, order_date, status, next_send_at)
            VALUES ($1, $2, $3, now())
            RETURNING id
            """,
            event_id, order_date, status,
        )

    async def _candidates(self):
        rows = await bot._order_close_candidates(self.conn, TODAY)
        return {row["event_id"]: row for row in rows}

    # --- окно дат ---

    async def test_window_from_today_minus_14_to_yesterday(self):
        await self._job("ev-today", TODAY)
        await self._job("ev-tomorrow", TODAY + timedelta(days=1))
        await self._job("ev-yesterday", TODAY - timedelta(days=1))
        await self._job("ev-14", TODAY - timedelta(days=14))
        await self._job("ev-15", TODAY - timedelta(days=15))

        got = await self._candidates()

        self.assertEqual(set(got), {"ev-yesterday", "ev-14"})
        row = got["ev-yesterday"]
        self.assertEqual(row["order_date"], TODAY - timedelta(days=1))
        self.assertEqual(row["client_name"], "Валерий")
        self.assertEqual(row["address"], "Афонино, Восточная, 342")
        self.assertEqual(list(row["services"]), ["carpeting", "furniture"])
        self.assertIsNone(row["reminder_id"])
        self.assertIsNone(row["reminder_status"])
        self.assertFalse(row["gone"])
        self.assertFalse(row["conducted"])

    # --- «проведён» ---

    async def test_order_linked_by_calendar_event_id(self):
        day = TODAY - timedelta(days=2)
        await self._job("ev-linked", day, phones=(PHONE_A,))
        await self._job("ev-other", day, phones=(PHONE_B,))
        # Номер у заказа чужой — проведён только по привязке к записи.
        await self._order(phone_digits="79990000000", created_at=_msk(day), event_id="ev-linked")

        got = await self._candidates()

        self.assertEqual(set(got), {"ev-other"})

    async def test_order_by_second_phone_of_record(self):
        day = TODAY - timedelta(days=2)
        await self._job("ev-two-phones", day, phones=(PHONE_A, PHONE_B))
        await self._order(phone_digits="7" + PHONE_B, created_at=_msk(day))

        self.assertEqual(await self._candidates(), {})

    async def test_order_a_week_later_counts(self):
        day = TODAY - timedelta(days=8)
        await self._job("ev-cafe", day)
        await self._order(phone_digits="8" + PHONE_A, created_at=_msk(day + timedelta(days=7)))

        self.assertEqual(await self._candidates(), {})

    async def test_order_day_before_counts_but_earlier_does_not(self):
        day = TODAY - timedelta(days=3)
        await self._job("ev-day-before", day, phones=(PHONE_A,))
        await self._job("ev-too-early", day, phones=(PHONE_B,))
        # С 00:00 МСК дня накануне записи — считается.
        await self._order(phone_digits="7" + PHONE_A, created_at=_msk(day - timedelta(days=1), 0, 0))
        # Раньше — это прошлый заказ клиента, не этот.
        await self._order(phone_digits="7" + PHONE_B, created_at=_msk(day - timedelta(days=2), 23, 59))

        got = await self._candidates()

        self.assertEqual(set(got), {"ev-too-early"})

    async def test_cleaning_order_counts(self):
        day = TODAY - timedelta(days=2)
        await self._job("ev-cleaning", day, phones=(PHONE_A,), services=("cleaning",))
        await self._cleaning("7" + PHONE_A, happened_at=_msk(day))

        self.assertEqual(await self._candidates(), {})

    async def test_deleted_cleaning_order_does_not_count(self):
        day = TODAY - timedelta(days=2)
        await self._job("ev-cleaning", day, phones=(PHONE_A,), services=("cleaning",))
        await self._cleaning("7" + PHONE_A, happened_at=_msk(day), deleted=True)

        got = await self._candidates()

        self.assertEqual(set(got), {"ev-cleaning"})
        self.assertFalse(got["ev-cleaning"]["conducted"])

    # --- открытые напоминания ---

    async def test_old_record_without_open_reminder_skipped_with_open_taken(self):
        old = TODAY - timedelta(days=20)
        await self._job("ev-old-open", old, phones=(PHONE_A,))
        await self._job("ev-old-none", old, phones=(PHONE_B,))
        await self._job("ev-old-closed", old, phones=("9160000001",))
        reminder_id = await self._reminder("ev-old-open", old)
        await self._reminder("ev-old-closed", old, status="closed")

        got = await self._candidates()

        self.assertEqual(set(got), {"ev-old-open"})
        row = got["ev-old-open"]
        self.assertEqual(row["reminder_id"], reminder_id)
        self.assertEqual(row["reminder_status"], "open")
        self.assertFalse(row["gone"])
        self.assertFalse(row["conducted"])

    async def test_open_reminder_conducted_comes_with_flag(self):
        day = TODAY - timedelta(days=2)
        await self._job("ev-done", day)
        await self._reminder("ev-done", day)
        await self._order(phone_digits="7" + PHONE_A, created_at=_msk(TODAY))

        got = await self._candidates()

        self.assertEqual(set(got), {"ev-done"})
        self.assertTrue(got["ev-done"]["conducted"])
        self.assertFalse(got["ev-done"]["gone"])

    async def test_record_gone_from_calendar(self):
        day = TODAY - timedelta(days=3)
        reminder_id = await self._reminder("ev-gone", day)
        # Строка уже закрытая (пропала раньше) — не возвращается.
        await self._reminder("ev-gone-before", day, status="gone")

        got = await self._candidates()

        self.assertEqual(set(got), {"ev-gone"})
        row = got["ev-gone"]
        self.assertEqual(row["reminder_id"], reminder_id)
        self.assertTrue(row["gone"])
        self.assertFalse(row["conducted"])
        self.assertEqual(row["order_date"], day)
        self.assertIsNone(row["client_name"])

    async def test_closed_reminder_in_window_not_conducted_comes_back_with_status(self):
        # Заказ удалили после закрытия — запись снова не проведена; что делать
        # со строкой, решает рассылка, выборка только сообщает её статус.
        day = TODAY - timedelta(days=2)
        await self._job("ev-reopen", day)
        await self._reminder("ev-reopen", day, status="closed")

        got = await self._candidates()

        self.assertEqual(set(got), {"ev-reopen"})
        self.assertEqual(got["ev-reopen"]["reminder_status"], "closed")
        self.assertFalse(got["ev-reopen"]["conducted"])


if __name__ == "__main__":
    unittest.main()
