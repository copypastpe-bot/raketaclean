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


NOW = _msk(2026, 9, 23, 14, 0)


class _CardsCase(_RealDbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._next_message_id = 500
        self.send_message.side_effect = self._fake_send

    async def _fake_send(self, chat_id, text, **kwargs):
        self._next_message_id += 1
        return mock.Mock(message_id=self._next_message_id)

    async def _insert_card(self, *, uid="u1", lead_id=123, status="open", next_send_at=None, messages=None):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO amocrm_unsorted_cards (
                    uid, kind, lead_id, contact_name, phone, event_at, status, next_send_at, messages
                )
                VALUES ($1, 'call', $2, 'Иван <Петров> & Ко', '+79991234567', $3, $4, $5, $6::jsonb)
                RETURNING id
                """,
                uid,
                lead_id,
                NOW - timedelta(minutes=5),
                status,
                next_send_at or NOW - timedelta(minutes=1),
                json.dumps(messages or {}),
            )

    async def _card(self, card_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM amocrm_unsorted_cards WHERE id=$1", card_id)

    @staticmethod
    def _buttons(markup):
        return [button for row in markup.inline_keyboard for button in row]


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class DispatchTests(_CardsCase):
    async def test_first_send_goes_to_both_admins_as_plain_text_with_buttons(self):
        card_id = await self._insert_card()

        await bot.run_unsorted_cards_dispatch(NOW)

        self.assertEqual([c.args[0] for c in self.send_message.await_args_list], [ADMIN_A, ADMIN_B])
        row = await self._card(card_id)
        expected_text = bot.render_card_text(bot._unsorted_card_from_row(row))
        for call in self.send_message.await_args_list:
            self.assertEqual(call.args[1], expected_text)
            self.assertIsNone(call.kwargs["parse_mode"])
            buttons = self._buttons(call.kwargs["reply_markup"])
            self.assertEqual([b.text for b in buttons], ["Перейти в сделку", "Я перезвонил"])
            self.assertEqual(buttons[0].url, f"{API_BASE}/leads/detail/123")
            self.assertEqual(buttons[1].callback_data, f"unsorted_done:{card_id}")
        self.assertIn("Клиент: Иван <Петров> & Ко", expected_text)
        self.delete_message.assert_not_awaited()
        self.assertEqual(json.loads(row["messages"]), {str(ADMIN_A): 501, str(ADMIN_B): 502})
        self.assertEqual(row["next_send_at"], _msk(2026, 9, 23, 15, 0))

    async def test_reminder_deletes_previous_cards_and_sends_again(self):
        card_id = await self._insert_card(messages={str(ADMIN_A): 11, str(ADMIN_B): 22})
        # Удалить не вышло (например, старше 48 часов) — ошибка глотается, карточка всё равно уходит.
        self.delete_message.side_effect = [RuntimeError("message can't be deleted"), None]

        await bot.run_unsorted_cards_dispatch(NOW)

        self.assertEqual(
            sorted(c.args for c in self.delete_message.await_args_list),
            [(ADMIN_A, 11), (ADMIN_B, 22)],
        )
        self.assertEqual(self.send_message.await_count, 2)
        row = await self._card(card_id)
        self.assertEqual(json.loads(row["messages"]), {str(ADMIN_A): 501, str(ADMIN_B): 502})

    async def test_evening_reminder_moves_to_next_morning(self):
        card_id = await self._insert_card(next_send_at=_msk(2026, 9, 23, 19, 30))

        await bot.run_unsorted_cards_dispatch(_msk(2026, 9, 23, 19, 30))

        row = await self._card(card_id)
        self.assertEqual(row["next_send_at"], _msk(2026, 9, 24, 9, 0))

    async def test_not_due_and_closed_cards_are_left_alone(self):
        await self._insert_card(uid="later", next_send_at=NOW + timedelta(minutes=1))
        await self._insert_card(uid="done", status="done")

        await bot.run_unsorted_cards_dispatch(NOW)

        self.send_message.assert_not_awaited()
        self.delete_message.assert_not_awaited()

    async def test_card_without_lead_has_only_done_button(self):
        card_id = await self._insert_card(lead_id=None)

        await bot.run_unsorted_cards_dispatch(NOW)

        call = self.send_message.await_args_list[0]
        buttons = self._buttons(call.kwargs["reply_markup"])
        self.assertEqual([(b.text, b.callback_data) for b in buttons], [("Я перезвонил", f"unsorted_done:{card_id}")])
        self.assertIn("\nСсылка:", call.args[1])

    async def test_nothing_delivered_keeps_send_time_for_next_pass(self):
        card_id = await self._insert_card()
        self.send_message.side_effect = RuntimeError("telegram down")

        await bot.run_unsorted_cards_dispatch(NOW)

        row = await self._card(card_id)
        self.assertEqual(row["next_send_at"], NOW - timedelta(minutes=1))
        self.assertEqual(json.loads(row["messages"]), {})


class FakeQuery:
    def __init__(self, user_id, card_id, *, chat_id=None, message_id=0):
        self.from_user = mock.Mock(id=user_id)
        self.data = f"unsorted_done:{card_id}"
        self.message = mock.Mock(chat=mock.Mock(id=chat_id or user_id), message_id=message_id)
        self.answer = mock.AsyncMock()


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class PressTests(_CardsCase):
    def _edits(self):
        return {
            (c.kwargs["chat_id"], c.kwargs["message_id"]): c.kwargs
            for c in self.edit_message_text.await_args_list
        }

    def _assert_closed_view(self, kwargs):
        self.assertTrue(kwargs["text"].endswith("\n✅ Перезвонил"))
        self.assertIsNone(kwargs["parse_mode"])
        self.assertEqual(kwargs["reply_markup"].inline_keyboard, [])

    async def test_non_admin_cannot_close(self):
        card_id = await self._insert_card(messages={str(ADMIN_A): 11})
        query = FakeQuery(4242, card_id)

        await bot.unsorted_card_done_cb(query)

        query.answer.assert_awaited_once_with("Недостаточно прав.")
        self.assertEqual((await self._card(card_id))["status"], "open")
        self.edit_message_text.assert_not_awaited()

    async def test_admin_press_closes_case_and_both_cards(self):
        card_id = await self._insert_card(messages={str(ADMIN_A): 11, str(ADMIN_B): 22})
        query = FakeQuery(ADMIN_B, card_id, message_id=22)

        with self.assertLogs(bot.logger, level="INFO") as logs:
            await bot.unsorted_card_done_cb(query)

        row = await self._card(card_id)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["done_by"], ADMIN_B)
        self.assertIsNotNone(row["done_at"])
        self.assertTrue(any(f"дело {card_id}" in line and str(ADMIN_B) in line for line in logs.output))
        query.answer.assert_awaited_once_with()
        edits = self._edits()
        self.assertEqual(set(edits), {(ADMIN_A, 11), (ADMIN_B, 22)})
        for kwargs in edits.values():
            self._assert_closed_view(kwargs)
        # Закрытое дело больше не напоминает.
        await bot.run_unsorted_cards_dispatch(NOW + timedelta(hours=3))
        self.send_message.assert_not_awaited()
        self.delete_message.assert_not_awaited()

    async def test_second_press_answers_already_marked_and_drops_buttons(self):
        card_id = await self._insert_card(messages={str(ADMIN_A): 11, str(ADMIN_B): 22})
        await bot.unsorted_card_done_cb(FakeQuery(ADMIN_B, card_id, message_id=22))
        self.edit_message_text.reset_mock()
        late = FakeQuery(ADMIN_A, card_id, message_id=11)

        await bot.unsorted_card_done_cb(late)

        late.answer.assert_awaited_once_with("Уже отмечено")
        edits = self._edits()
        self.assertEqual(set(edits), {(ADMIN_A, 11)})
        self._assert_closed_view(edits[(ADMIN_A, 11)])
        self.assertEqual((await self._card(card_id))["done_by"], ADMIN_B)


if __name__ == "__main__":
    unittest.main()
