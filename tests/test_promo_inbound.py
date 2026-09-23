"""Отклик «1» на промо во входящих WAHelp (bot.py, `handle_wahelp_inbound`).

ТЗ docs/plans/2026-09-23-promo-autocall.md, задачи 1 и 3 (2026-09-23).

Задача 1 — распознавание. Откликом считается только чистая «1» («1», «1.»,
«1!», «1)», с пробелами вокруг): раньше хватало единицы в начале, и «160 на
200» после «надо почистить матрас» уходило админам как отклик. Правило одно
для клиентов и лидов: промо приходило и на него ещё не отвечали. У клиентов
это давняя проверка `promo_reengagements`, у лидов — новая: рассылка из
`LEADS_PROMO_CAMPAIGNS` с `sent_at` и после самой поздней из них нет ответа
`interest`/`stop`.

Задача 3 — на засчитанный отклик строка в `promo_callbacks` (миграция 0015 —
договорённость с админ-ботом, он по ней заводит сделку). Старое сообщение
админам гасится выключателем `PROMO_INTEREST_ADMIN_MESSAGE`; заявка пишется
всегда. Сбой записи заявки не мешает автоответу.

Ветка подтверждения заказа базу не трогает — проверяется без неё. Остальное —
на настоящем Postgres (DSN в `TEST_DB_DSN`, без него класс пропускается):
полная схема `clients/leads` не из миграций, поэтому тест заводит свою схему
с минимальными таблицами и выставляет её в `search_path` пула, а
`promo_callbacks` создаёт ровно тем файлом миграции, что уйдёт на прод.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import asyncpg

import bot

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent / "app" / "migrations" / "0015_promo_callbacks.sql"
).read_text(encoding="utf-8")
SCHEMA = "promo_inbound_test"

# Только то, что читает и пишет обработчик входящих, — не копия прода.
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
CREATE TABLE {SCHEMA}.leads (
    id bigserial PRIMARY KEY,
    full_name text,
    name text,
    phone text,
    wahelp_user_id_leads bigint,
    wahelp_requires_connection boolean,
    promo_stop boolean NOT NULL DEFAULT false,
    promo_stop_at timestamptz,
    last_updated timestamptz
);
CREATE TABLE {SCHEMA}.lead_logs (
    id bigserial PRIMARY KEY,
    lead_id bigint NOT NULL,
    campaign text NOT NULL,
    variant smallint,
    wahelp_message_id text,
    status text NOT NULL,
    sent_at timestamptz,
    response_kind text,
    response_text text,
    response_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT NOW()
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
    rating_score integer,
    rating_requested_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT NOW()
);
"""

CLIENT_PHONE = "+7 916 111-22-33"
CLIENT_DIGITS = "79161112233"
LEAD_PHONE = "+7 926 444-55-66"
LEAD_DIGITS = "79264445566"


def _payload(text: str, phone: str) -> dict:
    return {"data": {"destination": "from_client", "message": text, "user": {"phone": phone}}}


class PromoInterestPatternTests(unittest.TestCase):
    """Решение владельца 1: откликом считается только чистая «1»."""

    def test_clean_one_is_interest(self):
        for text in ("1", " 1. ", "1.", "1!", "1)", "\n1\n"):
            with self.subTest(text=text):
                self.assertIsNotNone(bot.PROMO_INTEREST_RE.fullmatch(text.strip()))

    def test_anything_else_is_not_interest(self):
        for text in ("160 на 200", "1 диван", "10", "1,5", "11", "1..", "2", "один"):
            with self.subTest(text=text):
                self.assertIsNone(bot.PROMO_INTEREST_RE.fullmatch(text.strip()))


class _FakeConn:
    async def fetchrow(self, *args, **kwargs):
        return None


class _FakeAcquire:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def acquire(self):
        return _FakeAcquire()


class ConfirmationBranchTests(unittest.IsolatedAsyncioTestCase):
    """Следствие задачи 1: «160 на 200» больше не «известная ветка».

    Раньше такое сообщение от клиента, от которого ждут подтверждения заказа,
    проскакивало мимо владельца в промо-ветку. Теперь оно непонятный ответ на
    вопрос о заказе — и владельца зовут. Чистая «1» по-прежнему уходит старым
    веткам (отклик/оценка), как было.
    """

    def setUp(self):
        self.pending = {
            "lead_id": 501, "deal_id": 502, "client_id": 7, "status": "planned",
            "order_at": None, "phone_digits": CLIENT_DIGITS, "notified_at": None,
            "asked_sent_at": datetime.now(timezone.utc), "full_name": "Анна", "phone": CLIENT_PHONE,
        }
        patches = [
            mock.patch.object(bot, "pool", _FakePool()),
            mock.patch.object(bot, "CLIENT_MESSAGING_ENABLED", True),
            mock.patch.object(bot, "_confirmation_pending_for", mock.AsyncMock(return_value=self.pending)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.call_owner = mock.AsyncMock()
        p = mock.patch.object(bot, "_confirmation_call_owner", self.call_owner)
        p.start()
        self.addCleanup(p.stop)

    async def test_text_starting_with_one_goes_to_owner(self):
        handled = await bot.handle_wahelp_inbound(_payload("160 на 200", CLIENT_PHONE))

        self.assertTrue(handled)
        self.call_owner.assert_awaited_once()
        self.assertEqual(self.call_owner.await_args.kwargs["answer_text"], "160 на 200")

    async def test_clean_one_still_goes_to_old_branches(self):
        await bot.handle_wahelp_inbound(_payload("1", CLIENT_PHONE))

        self.call_owner.assert_not_awaited()


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class PromoInboundRealDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            await admin.execute(TABLES_SQL)
            await admin.execute(f"SET search_path TO {SCHEMA}")
            # Ровно та же миграция, что применяет владелец на проде.
            await admin.execute(MIGRATION_SQL)
        finally:
            await admin.close()
        self.pool = await asyncpg.create_pool(
            dsn=TEST_DB_DSN, min_size=1, max_size=2,
            server_settings={"search_path": SCHEMA},
        )
        self.send = mock.AsyncMock(return_value=mock.MagicMock(response=None))
        self.tg = mock.MagicMock()
        self.tg.send_message = mock.AsyncMock()
        self.patches = [
            mock.patch.object(bot, "pool", self.pool),
            mock.patch.object(bot, "CLIENT_MESSAGING_ENABLED", False),
            mock.patch.object(bot, "send_with_rules", self.send),
            mock.patch.object(bot, "bot", self.tg),
            mock.patch.object(bot, "ADMIN_TG_IDS", {111}),
            mock.patch.object(bot, "PROMO_INTEREST_ADMIN_MESSAGE", True),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        await self.pool.close()
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        finally:
            await admin.close()

    # --- данные ---

    async def _client(self, *, promo_sent: bool = True, responded: bool = False) -> int:
        async with self.pool.acquire() as conn:
            client_id = await conn.fetchval(
                "INSERT INTO clients (full_name, phone, phone_digits) VALUES ($1, $2, $3) RETURNING id",
                "Анна Клиентова", CLIENT_PHONE, CLIENT_DIGITS,
            )
            await conn.execute(
                "INSERT INTO promo_reengagements (client_id, last_variant_sent, last_sent_at, responded_at) "
                "VALUES ($1, $2, NOW() - INTERVAL '3 days', $3)",
                client_id, 1 if promo_sent else 0,
                datetime.now(timezone.utc) - timedelta(days=1) if responded else None,
            )
        return client_id

    async def _lead(self) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO leads (full_name, name, phone) VALUES (NULL, $1, $2) RETURNING id",
                "Борис", LEAD_PHONE,
            )

    async def _lead_log(self, lead_id: int, campaign: str, *, days_ago: int,
                        response_kind: str | None = None) -> None:
        at = datetime.now(timezone.utc) - timedelta(days=days_ago)
        async with self.pool.acquire() as conn:
            if response_kind is None:
                await conn.execute(
                    "INSERT INTO lead_logs (lead_id, campaign, variant, status, sent_at, created_at) "
                    "VALUES ($1, $2, 1, 'sent', $3, $3)",
                    lead_id, campaign, at,
                )
            else:
                await conn.execute(
                    "INSERT INTO lead_logs (lead_id, campaign, status, response_kind, response_text, "
                    "response_at, created_at) VALUES ($1, $2, 'received', $3, 'x', $4, $4)",
                    lead_id, campaign, response_kind, at,
                )

    async def _callbacks(self) -> list:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT source, client_id, lead_id, phone, name, response_text FROM promo_callbacks ORDER BY id"
            )

    async def _last_lead_response(self, lead_id: int) -> str | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT response_kind FROM lead_logs WHERE lead_id=$1 AND campaign='inbound' "
                "ORDER BY id DESC LIMIT 1",
                lead_id,
            )

    # --- клиенты ---

    async def test_client_interest_writes_one_callback_and_replies(self):
        client_id = await self._client()

        handled = await bot.handle_wahelp_inbound(_payload(" 1. ", "+79161112233"))

        self.assertTrue(handled)
        rows = await self._callbacks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(dict(rows[0]), {
            "source": "client", "client_id": client_id, "lead_id": None,
            "phone": CLIENT_PHONE, "name": "Анна Клиентова", "response_text": "1.",
        })
        self.send.assert_awaited_once()
        self.assertEqual(self.send.await_args.kwargs["text"], bot.CLIENT_PROMO_INTEREST_REPLY)
        self.tg.send_message.assert_awaited_once()
        async with self.pool.acquire() as conn:
            kind = await conn.fetchval(
                "SELECT response_kind FROM promo_reengagements WHERE client_id=$1", client_id)
        self.assertEqual(kind, "interest")

    async def test_client_text_starting_with_one_is_ignored(self):
        client_id = await self._client()

        for text in ("160 на 200", "1 диван", "10"):
            with self.subTest(text=text):
                handled = await bot.handle_wahelp_inbound(_payload(text, "+79161112233"))
                self.assertFalse(handled)

        self.assertEqual(await self._callbacks(), [])
        self.send.assert_not_awaited()
        self.tg.send_message.assert_not_awaited()
        async with self.pool.acquire() as conn:
            responded = await conn.fetchval(
                "SELECT responded_at FROM promo_reengagements WHERE client_id=$1", client_id)
        self.assertIsNone(responded)

    async def test_client_who_already_answered_is_not_interest(self):
        await self._client(responded=True)

        handled = await bot.handle_wahelp_inbound(_payload("1", "+79161112233"))

        self.assertFalse(handled)
        self.assertEqual(await self._callbacks(), [])
        self.send.assert_not_awaited()

    async def test_client_switch_off_keeps_callback_drops_admin_message(self):
        await self._client()

        with mock.patch.object(bot, "PROMO_INTEREST_ADMIN_MESSAGE", False):
            handled = await bot.handle_wahelp_inbound(_payload("1", "+79161112233"))

        self.assertTrue(handled)
        self.assertEqual(len(await self._callbacks()), 1)
        self.tg.send_message.assert_not_awaited()
        self.send.assert_awaited_once()

    async def test_callback_failure_does_not_stop_client_reply(self):
        await self._client()
        async with self.pool.acquire() as conn:
            await conn.execute("DROP TABLE promo_callbacks")

        with self.assertLogs(bot.logger, level="ERROR"):
            handled = await bot.handle_wahelp_inbound(_payload("1", "+79161112233"))

        self.assertTrue(handled)
        self.send.assert_awaited_once()
        self.assertEqual(self.send.await_args.kwargs["text"], bot.CLIENT_PROMO_INTEREST_REPLY)

    # --- лиды ---

    async def test_lead_without_promo_is_not_interest(self):
        lead_id = await self._lead()
        # Автоответ и прочие входящие — не рассылка промо.
        await self._lead_log(lead_id, "inbound_auto_reply_interest", days_ago=5)
        await self._lead_log(lead_id, "inbound", days_ago=6, response_kind="other")

        handled = await bot.handle_wahelp_inbound(_payload("1", "+79264445566"))

        self.assertTrue(handled)
        self.assertEqual(await self._callbacks(), [])
        self.assertEqual(await self._last_lead_response(lead_id), "other")
        self.send.assert_not_awaited()
        self.tg.send_message.assert_not_awaited()

    async def test_lead_with_unanswered_promo_is_interest(self):
        lead_id = await self._lead()
        await self._lead_log(lead_id, "week1", days_ago=300)

        handled = await bot.handle_wahelp_inbound(_payload("1!", "+79264445566"))

        self.assertTrue(handled)
        rows = await self._callbacks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(dict(rows[0]), {
            "source": "lead", "client_id": None, "lead_id": lead_id,
            "phone": LEAD_PHONE, "name": "Борис", "response_text": "1!",
        })
        self.assertEqual(await self._last_lead_response(lead_id), "interest")
        self.send.assert_awaited_once()
        self.assertEqual(self.send.await_args.kwargs["text"], bot.LEADS_AUTO_REPLY)
        self.tg.send_message.assert_awaited_once()

    async def test_lead_who_answered_after_last_promo_is_not_interest(self):
        lead_id = await self._lead()
        await self._lead_log(lead_id, "week1", days_ago=20)
        await self._lead_log(lead_id, "week2", days_ago=10)
        await self._lead_log(lead_id, "inbound", days_ago=5, response_kind="interest")

        handled = await bot.handle_wahelp_inbound(_payload("1", "+79264445566"))

        self.assertTrue(handled)
        self.assertEqual(await self._callbacks(), [])
        self.assertEqual(await self._last_lead_response(lead_id), "other")
        self.send.assert_not_awaited()

    async def test_lead_answer_before_newer_promo_does_not_block(self):
        lead_id = await self._lead()
        await self._lead_log(lead_id, "week1", days_ago=20)
        await self._lead_log(lead_id, "inbound", days_ago=15, response_kind="stop")
        await self._lead_log(lead_id, "week3", days_ago=10)

        handled = await bot.handle_wahelp_inbound(_payload("1", "+79264445566"))

        self.assertTrue(handled)
        self.assertEqual(len(await self._callbacks()), 1)

    async def test_lead_text_starting_with_one_writes_nothing(self):
        lead_id = await self._lead()
        await self._lead_log(lead_id, "week1", days_ago=3)

        await bot.handle_wahelp_inbound(_payload("160 на 200", "+79264445566"))
        await bot.handle_wahelp_inbound(_payload("1 диван", "+79264445566"))

        self.assertEqual(await self._callbacks(), [])
        self.send.assert_not_awaited()
        self.tg.send_message.assert_not_awaited()

    async def test_lead_switch_off_keeps_callback_drops_admin_message(self):
        lead_id = await self._lead()
        await self._lead_log(lead_id, "week4", days_ago=2)

        with mock.patch.object(bot, "PROMO_INTEREST_ADMIN_MESSAGE", False):
            handled = await bot.handle_wahelp_inbound(_payload("1)", "+79264445566"))

        self.assertTrue(handled)
        self.assertEqual(len(await self._callbacks()), 1)
        self.tg.send_message.assert_not_awaited()
        self.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
