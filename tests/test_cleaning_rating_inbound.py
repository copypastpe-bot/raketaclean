"""Ответ-цифра клиента — к уборке или к химчистке (bot.py, `handle_wahelp_inbound`,
`_select_pending_rating_order`, `_process_rating_response`, `_notify_rating_admins`).

ТЗ docs/plans/2026-09-30-cleaning-ratings.md, задача 2 (2026-09-30).

Раньше цифра «1–5» от клиента шла только к заказу химчистки. Теперь кандидаты —
работы клиента за 30 дней без оценки: заказы — как раньше; уборки — только те, по
которым оценку просили (`rating_requested_at`, решение владельца 2: у старых уборок
оценку не ловим) и которые не удалены. Берётся одна — с самой свежей просьбой, по
обеим таблицам вместе. Оценка пишется в таблицу своего вида, письма клиенту и ветки
5 / 4 / 1–3 — те же, в `payload` добавлен вид работы (`kind`). Админам по уборке —
«Оценка N по уборке №M», по химчистке текст прежний.

На настоящем Postgres (DSN в `TEST_DB_DSN`, без него класс пропускается): своя
схема с минимальными `clients` и `promo_reengagements` (что нужно маршруту), у
`orders` поля оценки ставит сама `ensure_orders_rating_schema` бота, таблицы уборок —
ровно теми файлами миграций, что уходят на прод (0006, 0010, 0016). Постановка писем
подменена: проверяется, какое письмо и с каким `payload` поставлено.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import asyncpg

import bot

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "app" / "migrations"
SCHEMA = "cleaning_rating_inbound_test"

TABLES_SQL = f"""
CREATE SCHEMA {SCHEMA};
CREATE TABLE {SCHEMA}.clients (
    id serial PRIMARY KEY,
    full_name text,
    phone text,
    phone_digits text,
    wahelp_preferred_channel text,
    wahelp_user_id_wa bigint,
    wahelp_user_id_tg bigint,
    wahelp_user_id_max bigint,
    wahelp_requires_connection boolean
);
CREATE TABLE {SCHEMA}.promo_reengagements (
    client_id integer PRIMARY KEY,
    last_variant_sent smallint NOT NULL DEFAULT 0,
    last_sent_at timestamptz,
    next_send_at timestamptz,
    responded_at timestamptz,
    response_kind text
);
CREATE TABLE {SCHEMA}.orders (
    id serial PRIMARY KEY,
    client_id integer,
    created_at timestamptz NOT NULL DEFAULT NOW()
);
"""

CLIENT_PHONE = "+7 916 111-22-33"
CLIENT_DIGITS = "79161112233"


def _migration(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def _payload(text: str, phone: str = "+79161112233") -> dict:
    return {"data": {"destination": "from_client", "message": text, "user": {"phone": phone}}}


def _ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class CleaningRatingInboundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            await admin.execute(TABLES_SQL)
            await admin.execute(f"SET search_path TO {SCHEMA}")
            await bot.ensure_orders_rating_schema(admin)
            await admin.execute(_migration("0006_cleaning.sql"))
            await admin.execute(_migration("0010_cleaning_orders_comment.sql"))
            await admin.execute(_migration("0016_cleaning_orders_rating.sql"))
        finally:
            await admin.close()
        self.pool = await asyncpg.create_pool(
            dsn=TEST_DB_DSN, min_size=1, max_size=2,
            server_settings={"search_path": SCHEMA},
        )
        self.enqueue = mock.AsyncMock()
        self.tg = mock.MagicMock()
        self.tg.send_message = mock.AsyncMock()
        self.patches = [
            mock.patch.object(bot, "pool", self.pool),
            mock.patch.object(bot, "CLIENT_MESSAGING_ENABLED", False),
            mock.patch.object(bot, "_try_enqueue_notification", self.enqueue),
            mock.patch.object(bot, "bot", self.tg),
            mock.patch.object(bot, "ADMIN_TG_IDS", {111}),
        ]
        for p in self.patches:
            p.start()
        self.client_id = await self._client()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        await self.pool.close()
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        finally:
            await admin.close()

    # --- помощники ---

    async def _client(self) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO clients (full_name, phone, phone_digits) VALUES ($1, $2, $3) RETURNING id",
                "Анна Клиентова", CLIENT_PHONE, CLIENT_DIGITS,
            )

    async def _order(self, *, requested_days_ago: int | None, created_days_ago: int = 5) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO orders (client_id, created_at, rating_requested_at) "
                "VALUES ($1, $2, $3) RETURNING id",
                self.client_id, _ago(created_days_ago),
                None if requested_days_ago is None else _ago(requested_days_ago),
            )

    async def _cleaning(self, *, requested_days_ago: int | None, created_days_ago: int = 5,
                        deleted: bool = False) -> int:
        async with self.pool.acquire() as conn:
            foreman_id = await conn.fetchval(
                "INSERT INTO cleaning_foremen (fn) VALUES ('Бригадир') RETURNING id"
            )
            return await conn.fetchval(
                "INSERT INTO cleaning_orders (client_id, foreman_id, address, total_amount, "
                "created_at, rating_requested_at, deleted_at) "
                "VALUES ($1, $2, 'адрес', 5000, $3, $4, $5) RETURNING id",
                self.client_id, foreman_id, _ago(created_days_ago),
                None if requested_days_ago is None else _ago(requested_days_ago),
                _ago(1) if deleted else None,
            )

    async def _rating(self, table: str, row_id: int) -> asyncpg.Record:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                f"SELECT rating_score, rating_comment, rating_replied_at FROM {table} WHERE id=$1",
                row_id,
            )

    def _assert_untouched(self, row: asyncpg.Record) -> None:
        self.assertIsNone(row["rating_score"])
        self.assertIsNone(row["rating_comment"])
        self.assertIsNone(row["rating_replied_at"])

    # --- тесты ---

    async def test_cleaning_rating_five_is_stored_at_cleaning(self):
        """Тест 1 ТЗ: уборка с отметкой «попросили», ответ «5» — оценка у уборки."""
        cleaning_id = await self._cleaning(requested_days_ago=1)

        handled = await bot.handle_wahelp_inbound(_payload("5"))

        self.assertTrue(handled)
        row = await self._rating("cleaning_orders", cleaning_id)
        self.assertEqual(row["rating_score"], 5)
        self.assertEqual(row["rating_comment"], "5")
        self.assertIsNotNone(row["rating_replied_at"])
        self.enqueue.assert_awaited_once()
        self.assertEqual(self.enqueue.await_args.kwargs["event_key"], "order_rating_response_high_client")
        self.assertEqual(self.enqueue.await_args.kwargs["client_id"], self.client_id)
        self.assertEqual(
            self.enqueue.await_args.kwargs["payload"],
            {"order_id": cleaning_id, "score": 5, "kind": "cleaning"},
        )
        self.tg.send_message.assert_not_awaited()                   # на 5 админам — ничего, как у химчистки

    async def test_fresher_cleaning_request_wins_over_order(self):
        """Тест 2 ТЗ: просьба по уборке свежее — оценка у уборки, заказ не тронут."""
        order_id = await self._order(requested_days_ago=3)
        cleaning_id = await self._cleaning(requested_days_ago=1)

        handled = await bot.handle_wahelp_inbound(_payload("4"))

        self.assertTrue(handled)
        self.assertEqual((await self._rating("cleaning_orders", cleaning_id))["rating_score"], 4)
        self._assert_untouched(await self._rating("orders", order_id))
        self.assertEqual(self.enqueue.await_args.kwargs["event_key"], "order_rating_response_mid_client")
        self.assertEqual(self.enqueue.await_args.kwargs["payload"]["kind"], "cleaning")

    async def test_fresher_order_request_wins_over_cleaning(self):
        """Тест 2 ТЗ, наоборот: просьба по химчистке свежее — оценка у заказа."""
        cleaning_id = await self._cleaning(requested_days_ago=3)
        order_id = await self._order(requested_days_ago=1)

        handled = await bot.handle_wahelp_inbound(_payload("4"))

        self.assertTrue(handled)
        self.assertEqual((await self._rating("orders", order_id))["rating_score"], 4)
        self._assert_untouched(await self._rating("cleaning_orders", cleaning_id))
        self.assertEqual(
            self.enqueue.await_args.kwargs["payload"],
            {"order_id": order_id, "score": 4, "kind": "order"},
        )

    async def test_cleaning_without_request_is_not_candidate(self):
        """Тест 3 ТЗ: старая уборка без отметки «попросили» — не кандидат.

        Заказа нет, промо не было — поведение как сейчас без кандидата: сообщение
        не наше, ничего не пишется и не отправляется.
        """
        cleaning_id = await self._cleaning(requested_days_ago=None, created_days_ago=1)

        handled = await bot.handle_wahelp_inbound(_payload("5"))

        self.assertFalse(handled)
        self._assert_untouched(await self._rating("cleaning_orders", cleaning_id))
        self.enqueue.assert_not_awaited()
        self.tg.send_message.assert_not_awaited()

    async def test_deleted_cleaning_is_not_candidate(self):
        """Тест 4 ТЗ: удалённая уборка — не кандидат."""
        cleaning_id = await self._cleaning(requested_days_ago=1, deleted=True)

        handled = await bot.handle_wahelp_inbound(_payload("5"))

        self.assertFalse(handled)
        self._assert_untouched(await self._rating("cleaning_orders", cleaning_id))
        self.enqueue.assert_not_awaited()
        self.tg.send_message.assert_not_awaited()

    async def test_admin_text_for_cleaning_names_cleaning(self):
        """Тест 5 ТЗ: админам по уборке — «Оценка N по уборке №M»."""
        cleaning_id = await self._cleaning(requested_days_ago=1)

        handled = await bot.handle_wahelp_inbound(_payload("2 плохо помыли окна"))

        self.assertTrue(handled)
        self.tg.send_message.assert_awaited_once()
        admin_id, text = self.tg.send_message.await_args.args
        self.assertEqual(admin_id, 111)
        self.assertIn(f"Оценка 2 по уборке №{cleaning_id}", text)
        self.assertNotIn("по заказу", text)
        self.assertEqual(self.enqueue.await_args.kwargs["event_key"], "order_rating_response_low_client")

    async def test_admin_text_for_order_is_unchanged(self):
        """Охрана решения 1: по химчистке текст админам прежний — «по заказу #id»."""
        order_id = await self._order(requested_days_ago=1)

        handled = await bot.handle_wahelp_inbound(_payload("3"))

        self.assertTrue(handled)
        self.tg.send_message.assert_awaited_once()
        _, text = self.tg.send_message.await_args.args
        self.assertIn(f"Оценка 3 по заказу #{order_id}", text)


if __name__ == "__main__":
    unittest.main()
