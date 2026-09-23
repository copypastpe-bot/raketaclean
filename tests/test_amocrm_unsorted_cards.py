"""Дела «Неразобранного» amoCRM: опрос заводит, рассылка шлёт, нажатие закрывает (bot.py).

ТЗ docs/plans/2026-09-23-amo-unsorted-cards.md, задачи 3 и 4. Опрос
(`_amocrm_poll_unsorted_once`) по новой записи заводит дело в
`amocrm_unsorted_cards` и в Telegram не пишет; рассылка
(`run_unsorted_cards_dispatch`) раз в минуту шлёт карточку обоим админам и
напоминает раз в час; нажатие (`unsorted_card_done_cb`) закрывает дело и
переводит карточки в закрытый вид.

Всё на настоящем Postgres: таблица дел — ровно из миграции 0014 (как
tests/test_deleted_orders.py), а гонка нажатия и повторной отправки держится
блокировкой строки, которую без живой базы не проверить. DSN в TEST_DB_DSN,
без него классы пропускаются:
    export TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/raketaclean_test
"""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import asyncpg

import bot

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent / "app" / "migrations" / "0014_amocrm_unsorted_cards.sql"
).read_text(encoding="utf-8")

MSK = ZoneInfo("Europe/Moscow")
API_BASE = "https://example.amocrm.ru"
PIPELINE = 55
ADMIN_A = 1001
ADMIN_B = 1002


def _msk(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def _ts(dt):
    return int(dt.timestamp())


def _call_item(uid="u" * 60, *, lead_id=123, contact_id=10, category="sip"):
    at = int(datetime.now(timezone.utc).timestamp())
    return {
        "uid": uid,
        "category": category,
        "pipeline_id": PIPELINE,
        "created_at": at,
        "_embedded": {"leads": [{"id": lead_id}], "contacts": [{"id": contact_id}]},
        "metadata": {"called_at": at, "duration": 0, "from": "+79991234567"},
    }


CONTACT = {
    "id": 10,
    "name": "Иван <Петров> & Ко",
    "custom_fields_values": [{"field_code": "PHONE", "values": [{"value": "+79991234567"}]}],
}


class FakeAmoClient:
    def __init__(self, items=(), contact=CONTACT):
        self.items = list(items)
        self.contact = contact
        self.contact_calls = 0

    async def fetch_unsorted(self, *, pipeline_id, created_from):
        return list(self.items)

    async def fetch_contact(self, contact_id):
        self.contact_calls += 1
        return self.contact


class _RealDbCase(unittest.IsolatedAsyncioTestCase):
    """Своя таблица дел из миграции, таблицы опроса — из `ensure_amocrm_api_schema`."""

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(dsn=TEST_DB_DSN, min_size=1, max_size=4)
        async with self.pool.acquire() as conn:
            await self._drop(conn)
            await conn.execute(MIGRATION_SQL)
            await bot.ensure_amocrm_api_schema(conn)
        for target, value in (
            ("pool", self.pool),
            ("ADMIN_TG_IDS", {ADMIN_A, ADMIN_B}),
            ("AMOCRM_API_BASE", API_BASE),
            ("AMOCRM_PIPELINE_ID", PIPELINE),
            ("AMOCRM_UNSORTED_CARDS", True),
        ):
            patcher = mock.patch.object(bot, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.send_message = self._patch_bot("send_message")
        self.delete_message = self._patch_bot("delete_message")
        self.edit_message_text = self._patch_bot("edit_message_text")

    async def asyncTearDown(self):
        async with self.pool.acquire() as conn:
            await self._drop(conn)
        await self.pool.close()

    @staticmethod
    async def _drop(conn):
        await conn.execute(
            "DROP TABLE IF EXISTS amocrm_unsorted_cards, amocrm_unsorted_seen, "
            "amocrm_api_state, amocrm_api_events, amocrm_pending_incoming"
        )

    def _patch_bot(self, name):
        patcher = mock.patch.object(bot.bot, name, mock.AsyncMock())
        method = patcher.start()
        self.addCleanup(patcher.stop)
        return method

    async def _cards(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM amocrm_unsorted_cards ORDER BY id")

    async def _seen(self, uid):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT action, error FROM amocrm_unsorted_seen WHERE uid=$1", uid)


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class PollUnsortedTests(_RealDbCase):
    async def test_new_call_creates_card_and_sends_nothing(self):
        item = _call_item()
        before = datetime.now(timezone.utc)

        await bot._amocrm_poll_unsorted_once(FakeAmoClient([item]))

        cards = await self._cards()
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["uid"], item["uid"])
        self.assertEqual(card["kind"], "call")
        self.assertEqual(card["lead_id"], 123)
        self.assertEqual(card["contact_name"], "Иван <Петров> & Ко")
        self.assertEqual(card["phone"], "+79991234567")
        self.assertEqual(card["status"], "open")
        self.assertEqual(json.loads(card["messages"]), {})
        # Время первой отправки — по окну: в окне «сейчас», вне окна — 9:00 МСК.
        expected = bot.first_send_at(before)
        self.assertLess(abs((card["next_send_at"] - expected).total_seconds()), 60)
        seen = await self._seen(item["uid"])
        self.assertEqual(seen["action"], "carded")
        self.assertIsNone(seen["error"])
        self.send_message.assert_not_awaited()

    async def test_lead_already_notified_by_old_flow_still_gets_card(self):
        # Прежняя проверка «lead already notified» снята: запись по сделке, о
        # которой когда-то сообщал старый механизм, всё равно становится делом.
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO amocrm_unsorted_seen (uid, pipeline_id, payload, action) "
                "VALUES ('old', $1, $2::jsonb, 'notified')",
                PIPELINE,
                json.dumps({"_embedded": {"leads": [{"id": 123}]}}),
            )
        await bot._amocrm_poll_unsorted_once(FakeAmoClient([_call_item()]))
        self.assertEqual(len(await self._cards()), 1)

    async def test_switch_off_marks_skipped_without_card(self):
        item = _call_item()
        with mock.patch.object(bot, "AMOCRM_UNSORTED_CARDS", False):
            client = FakeAmoClient([item])
            await bot._amocrm_poll_unsorted_once(client)

        self.assertEqual(await self._cards(), [])
        seen = await self._seen(item["uid"])
        self.assertEqual((seen["action"], seen["error"]), ("ignored", "выключено"))
        self.assertEqual(client.contact_calls, 0)
        self.send_message.assert_not_awaited()

    async def test_unknown_category_ignored_with_category_name(self):
        item = _call_item(category="forms")
        await bot._amocrm_poll_unsorted_once(FakeAmoClient([item]))

        self.assertEqual(await self._cards(), [])
        seen = await self._seen(item["uid"])
        self.assertEqual((seen["action"], seen["error"]), ("ignored", "категория forms"))

    async def test_same_record_twice_makes_one_card(self):
        item = _call_item()
        await bot._amocrm_poll_unsorted_once(FakeAmoClient([item]))
        await bot._amocrm_poll_unsorted_once(FakeAmoClient([item]))
        self.assertEqual(len(await self._cards()), 1)

    async def test_cursor_touched_on_empty_pass_even_when_switch_off(self):
        # Сторож службы оповещений судит о живости опроса по updated_at курсора
        # `unsorted`: он обязан обновляться и на пустом проходе, и при выключенном
        # выключателе.
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO amocrm_api_state (stream, cursor_created_at, updated_at) "
                "VALUES ('unsorted', $1, $2)",
                _ts(datetime.now(timezone.utc)),
                old,
            )
        with mock.patch.object(bot, "AMOCRM_UNSORTED_CARDS", False):
            await bot._amocrm_poll_unsorted_once(FakeAmoClient([]))

        async with self.pool.acquire() as conn:
            updated_at = await conn.fetchval(
                "SELECT updated_at FROM amocrm_api_state WHERE stream='unsorted'"
            )
        self.assertGreater(updated_at, old + timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
