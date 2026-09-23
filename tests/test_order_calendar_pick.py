"""Выбор записи календаря после телефона в сценарии заказа (bot.py).

Задача 9 ТЗ «цепочка заказа» (docs/plans/2026-09-22-order-chain.md,
2026-09-22): при включённом `ORDER_CALENDAR_PICK` бот, после того как
мастер ввёл телефон клиента, смотрит в представление
`adminbot.calendar_jobs` (задача 8 того же ТЗ) — одна и та же таблица
`adminbot.gcal_events` под ним, которую заводит фикстура ниже, схемы
`adminbot` в тестовой базе может не быть. 0 записей — как раньше, 1 —
молча в данные сценария, 2+ — мастер выбирает из списка кнопками.

Запрос к представлению (три ветки 0/1/2+, окно дат, `order_id IS NULL`,
`ANY(phones)`) проверяется на настоящем Postgres: DSN в `TEST_DB_DSN`,
без него класс пропускается (см. `tests/test_deleted_orders.py` — тот же
сервер и та же тестовая база `raketaclean_test`). Выключатель, отказ
запроса и хендлер выбора («Не из списка», номер) не трогают базу вовсе —
проверяются без неё. Запись обеих колонок в `commit_order` — отдельным
классом, на FakeConn: полная схема `orders/clients/staff` не из миграций
и в тестовую базу не поднимается, поэтому здесь достаточно проверить,
что INSERT INTO orders несёт `calendar_event_id`/`deal_lead_id` с теми
значениями, что были в данных сценария.
"""

import json
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

import asyncpg

import bot

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")

VIEW_SQL = """
CREATE OR REPLACE VIEW adminbot.calendar_jobs AS
SELECT event_id,
       phone10,
       COALESCE(
         (SELECT array_agg(value) FROM jsonb_array_elements_text(event_data->'phones')),
         ARRAY[phone10]) AS phones,
       order_date,
       client_name,
       event_data->>'address' AS address,
       services,
       primary_lead_id,
       real_lead_id,
       status,
       order_id
FROM adminbot.gcal_events
WHERE kind = 'order' AND status IN ('done', 'in_progress', 'waiting_salesbot');
"""


class FakeUser:
    def __init__(self, user_id=777):
        self.id = user_id
        self.full_name = "Тест Мастеров"
        self.username = None


class FakeMessage:
    def __init__(self, text, user_id=777):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answer = mock.AsyncMock()


class FakeState:
    """Достаточно от FSMContext: get_data/update_data/set_state/clear."""

    def __init__(self, data=None):
        self._data: dict = dict(data or {})
        self.state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self._data = {}
        self.state = None


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class OrderCalendarPickQueryTests(unittest.IsolatedAsyncioTestCase):
    """Живая база: сам запрос к adminbot.calendar_jobs, три ветки 0/1/2+.

    Пример временной базы (тот же сервер, что для adminbot_test и
    raketaclean_test):
        createdb -h 127.0.0.1 -p 5432 -U postgres raketaclean_test
        export TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/raketaclean_test
    """

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(dsn=TEST_DB_DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as conn:
            await conn.execute("DROP VIEW IF EXISTS adminbot.calendar_jobs")
            await conn.execute("DROP TABLE IF EXISTS adminbot.gcal_events")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS adminbot")
            await conn.execute(
                """
                CREATE TABLE adminbot.gcal_events (
                    event_id text PRIMARY KEY,
                    kind text NOT NULL DEFAULT 'order',
                    phone10 text,
                    event_data jsonb NOT NULL DEFAULT '{}'::jsonb,
                    order_date date,
                    client_name text,
                    services text,
                    primary_lead_id bigint,
                    real_lead_id bigint,
                    status text NOT NULL DEFAULT 'done',
                    order_id bigint
                )
                """
            )
            await conn.execute(VIEW_SQL)
        self._patches = [
            mock.patch.object(bot, "pool", self.pool),
            mock.patch.object(bot, "ORDER_CALENDAR_PICK", True),
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        async with self.pool.acquire() as conn:
            await conn.execute("DROP VIEW IF EXISTS adminbot.calendar_jobs")
            await conn.execute("DROP TABLE IF EXISTS adminbot.gcal_events")
        await self.pool.close()

    async def _insert_job(self, conn, *, event_id, phone10="9998887766", phones=None,
                           address="Мира, 1", order_date, status="done", order_id=None,
                           real_lead_id=None):
        event_data = {"address": address}
        if phones is not None:
            event_data["phones"] = phones
        await conn.execute(
            """
            INSERT INTO adminbot.gcal_events
                (event_id, kind, phone10, event_data, order_date, status, order_id, real_lead_id)
            VALUES ($1, 'order', $2, $3::jsonb, $4, $5, $6, $7)
            """,
            event_id, phone10, json.dumps(event_data, ensure_ascii=False),
            order_date, status, order_id, real_lead_id,
        )

    async def test_zero_matches_continues_as_today(self):
        msg = FakeMessage("9998887766")
        state = FakeState()
        with mock.patch.object(bot, "_order_continue_after_phone", mock.AsyncMock()) as cont:
            await bot._order_pick_calendar_job(msg, state, "+79998887766")
        cont.assert_awaited_once_with(msg, state)
        self.assertNotIn("calendar_event_id", state._data)
        msg.answer.assert_not_called()

    async def test_one_match_is_stored_silently(self):
        today = datetime.now(bot.MOSCOW_TZ).date()
        async with self.pool.acquire() as conn:
            await self._insert_job(
                conn, event_id="evt-1", order_date=today,
                address="Мира, 1", real_lead_id=555,
            )
        msg = FakeMessage("9998887766")
        state = FakeState()
        with mock.patch.object(bot, "_order_continue_after_phone", mock.AsyncMock()) as cont:
            await bot._order_pick_calendar_job(msg, state, "+79998887766")
        cont.assert_awaited_once_with(msg, state)
        self.assertEqual(state._data["calendar_event_id"], "evt-1")
        self.assertEqual(state._data["deal_lead_id"], 555)
        self.assertEqual(state._data["calendar_address"], "Мира, 1")
        msg.answer.assert_not_called()

    async def test_match_by_any_phone_in_the_list(self):
        """Задача 8: `phones` — массив, звонивший номер может быть не первым."""
        today = datetime.now(bot.MOSCOW_TZ).date()
        async with self.pool.acquire() as conn:
            await self._insert_job(
                conn, event_id="evt-2", order_date=today, phone10="1112223344",
                phones=["1112223344", "9998887766"], real_lead_id=42,
            )
        msg = FakeMessage("9998887766")
        state = FakeState()
        with mock.patch.object(bot, "_order_continue_after_phone", mock.AsyncMock()):
            await bot._order_pick_calendar_job(msg, state, "+79998887766")
        self.assertEqual(state._data["calendar_event_id"], "evt-2")

    async def test_two_matches_ask_master_to_pick_and_skip_linked_and_old(self):
        today = datetime.now(bot.MOSCOW_TZ).date()
        yesterday = today - timedelta(days=1)
        too_old = today - timedelta(days=2)
        async with self.pool.acquire() as conn:
            await self._insert_job(
                conn, event_id="evt-today", order_date=today, address="Мира, 1", real_lead_id=1,
            )
            await self._insert_job(
                conn, event_id="evt-yesterday", order_date=yesterday, address="Ленина, 2", real_lead_id=2,
            )
            # уже заведён заказом — не должен попасть в список (order_id IS NULL)
            await self._insert_job(
                conn, event_id="evt-linked", order_date=today, address="Занятая, 3",
                order_id=90001, real_lead_id=3,
            )
            # за пределами окна «вчера/сегодня» — тоже не должен попасть
            await self._insert_job(
                conn, event_id="evt-old", order_date=too_old, address="Старая, 4", real_lead_id=4,
            )
        msg = FakeMessage("9998887766")
        state = FakeState()
        with mock.patch.object(bot, "_order_continue_after_phone", mock.AsyncMock()) as cont:
            await bot._order_pick_calendar_job(msg, state, "+79998887766")
        cont.assert_not_awaited()
        self.assertEqual(state.state, bot.OrderFSM.pick_job)
        choices = state._data["calendar_job_choices"]
        self.assertEqual(len(choices), 2)
        self.assertEqual([c["event_id"] for c in choices], ["evt-yesterday", "evt-today"])
        text = msg.answer.await_args.args[0]
        self.assertIn("2 записей", text)
        self.assertIn("Ленина, 2", text)
        self.assertIn("Мира, 1", text)
        kb = msg.answer.await_args.kwargs["reply_markup"]
        button_texts = [btn.text for row in kb.keyboard for btn in row]
        self.assertEqual(button_texts, ["1", "2", "Не из списка"])


class OrderCalendarPickSwitchAndFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Выключатель и сбой запроса — без базы вовсе."""

    async def test_switch_off_never_touches_the_pool(self):
        exploding_pool = mock.Mock()
        exploding_pool.acquire.side_effect = AssertionError("не должен ходить в базу")
        msg = FakeMessage("9998887766")
        state = FakeState()
        with mock.patch.object(bot, "pool", exploding_pool), \
             mock.patch.object(bot, "ORDER_CALENDAR_PICK", False), \
             mock.patch.object(bot, "_order_continue_after_phone", mock.AsyncMock()) as cont:
            await bot._order_pick_calendar_job(msg, state, "+79998887766")
        cont.assert_awaited_once_with(msg, state)
        exploding_pool.acquire.assert_not_called()

    async def test_query_failure_falls_back_without_raising(self):
        class BrokenConn:
            async def fetch(self, *a, **kw):
                raise RuntimeError("relation adminbot.calendar_jobs does not exist")

        class BrokenAcquire:
            async def __aenter__(self):
                return BrokenConn()

            async def __aexit__(self, *exc):
                return False

        class BrokenPool:
            def acquire(self):
                return BrokenAcquire()

        msg = FakeMessage("9998887766")
        state = FakeState()
        with mock.patch.object(bot, "pool", BrokenPool()), \
             mock.patch.object(bot, "ORDER_CALENDAR_PICK", True), \
             mock.patch.object(bot, "_order_continue_after_phone", mock.AsyncMock()) as cont:
            await bot._order_pick_calendar_job(msg, state, "+79998887766")  # не должно упасть
        cont.assert_awaited_once_with(msg, state)


class CalendarJobPickHandlerTests(unittest.IsolatedAsyncioTestCase):
    """Хендлер OrderFSM.pick_job: «Не из списка», номер, промах."""

    def _choices(self):
        return [
            {"event_id": "evt-1", "deal_lead_id": 111, "address": "Мира, 1"},
            {"event_id": "evt-2", "deal_lead_id": 222, "address": "Ленина, 2"},
        ]

    async def test_not_in_list_skips_the_link(self):
        msg = FakeMessage("Не из списка")
        state = FakeState({"calendar_job_choices": self._choices()})
        with mock.patch.object(bot, "_order_continue_after_phone", mock.AsyncMock()) as cont:
            await bot.got_calendar_job_pick(msg, state)
        cont.assert_awaited_once_with(msg, state)
        self.assertNotIn("calendar_event_id", state._data)

    async def test_numeric_choice_stores_the_picked_job(self):
        msg = FakeMessage("2")
        state = FakeState({"calendar_job_choices": self._choices()})
        with mock.patch.object(bot, "_order_continue_after_phone", mock.AsyncMock()) as cont:
            await bot.got_calendar_job_pick(msg, state)
        cont.assert_awaited_once_with(msg, state)
        self.assertEqual(state._data["calendar_event_id"], "evt-2")
        self.assertEqual(state._data["deal_lead_id"], 222)
        self.assertEqual(state._data["calendar_address"], "Ленина, 2")

    async def test_out_of_range_choice_reprompts_without_linking(self):
        msg = FakeMessage("9")
        state = FakeState({"calendar_job_choices": self._choices()})
        with mock.patch.object(bot, "_order_continue_after_phone", mock.AsyncMock()) as cont:
            await bot.got_calendar_job_pick(msg, state)
        cont.assert_not_awaited()
        self.assertNotIn("calendar_event_id", state._data)
        msg.answer.assert_awaited_once()


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Достаточно commit_order: клиент/заказ/мастер, всё остальное — execute-лог."""

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self.order_insert_query: str | None = None
        self.order_insert_args: tuple | None = None

    def transaction(self):
        return _FakeTransaction()

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if "INSERT INTO clients" in q:
            return {"id": 1, "bonus_balance": 0, "full_name": args[0], "phone": args[1], "birthday": args[2]}
        if "INSERT INTO orders" in q:
            self.order_insert_query = q
            self.order_insert_args = args
            return {"id": 501, "master_id": 42}
        if "FROM staff WHERE id=$1" in q:
            return {"first_name": "Тест", "last_name": "Мастеров"}
        return None

    async def fetchval(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))
        return "OK"


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


class CommitOrderCalendarColumnsTests(unittest.IsolatedAsyncioTestCase):
    """`commit_order` кладёт calendar_event_id/deal_lead_id в INSERT INTO orders.

    Полная схема orders/clients/staff/... не из миграций и в тестовую базу
    не поднимается (см. docstring модуля) — здесь FakeConn, а денежные и
    уведомительные ветки сведены к минимуму: оплата «р/с» и нулевые бонусы
    убирают из пути `_record_order_income`/`post_order_bonus_delta`,
    отправка отчёта и итоговое уведомление о заказе замоканы — они не
    относятся к задаче 9 и проверяются в своих тестах.
    """

    async def _run(self, extra_state=None):
        conn = _FakeConn()
        base_state = {
            "phone_in": "+79998887766",
            "client_name": "Иван Тестов",
            "payment_method": "р/с",
            "amount_cash": "0",
            "amount_total": "1500",
            "bonus_earned": 0,
            "bonus_spent": 0,
            "base_pay": "1000",
            "upsell_pay": "0",
            "fuel_pay": "150",
            "total_pay": "1150",
        }
        base_state.update(extra_state or {})
        msg = FakeMessage("подтвердить")
        state = FakeState(base_state)
        with mock.patch.object(bot, "pool", _FakePool(conn)), \
             mock.patch.object(bot, "_enqueue_order_completed_notification", mock.AsyncMock()), \
             mock.patch.object(bot, "_enqueue_order_report", mock.AsyncMock()):
            await bot.commit_order(msg, state)
        return conn

    async def test_insert_carries_calendar_columns_when_job_was_picked(self):
        conn = await self._run({"calendar_event_id": "evt-1", "deal_lead_id": 777})
        self.assertIn("calendar_event_id", conn.order_insert_query)
        self.assertIn("deal_lead_id", conn.order_insert_query)
        self.assertEqual(conn.order_insert_args[-2:], ("evt-1", 777))

    async def test_insert_carries_null_columns_when_no_job_was_picked(self):
        conn = await self._run()
        self.assertEqual(conn.order_insert_args[-2:], (None, None))


if __name__ == "__main__":
    unittest.main()
