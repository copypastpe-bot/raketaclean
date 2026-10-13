"""Перенос заказа и вопрос «подтверждаете заказ?» (bot.py).

ТЗ docs/plans/2026-10-13-reschedule-confirmation.md, задача 1. Живой случай
2026-10-10, сделка 31703481: заказ перенесли с 11.10 на 31.10 (админ-бот записал
новую дату в сделку 08.10), а вопрос про 11.10 всё равно ушёл клиенту — проверка
перед отправкой сверяла сделку, но не дату.

Здесь проверяется слой с базой: проверка перед отправкой
(`_confirmation_before_send`) видит перенос и пересчитывает вопрос
(`_confirmation_reschedule`) — старое письмо отменено, новое стоит за сутки до
новой даты, строка ожидания переписана, след только во внутреннем журнале.
Само правило «что делать» — чистая функция, её таблица случаев —
в tests/test_client_messaging.py. Задача 2 — обход раз в 10 минут
(`run_confirmation_reschedule_watch`): тот же пересчёт, но без письма в руках
работника очереди, по всем живым ожиданиям сразу.

На настоящем Postgres (DSN в TEST_DB_DSN, без него классы пропускаются): своя
схема, минимальная `clients`, остальные таблицы — теми же функциями, что
создают их на проде (`ensure_notification_schema`, `ensure_amocrm_api_schema`,
`ensure_client_messaging_schema`). amoCRM подменяется заглушкой.
"""

import json
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import asyncpg

import bot
from notifications.amo_exchange import AMO_FIELD_ADDRESS
from notifications.amocrm_api import AmoCRMAPIError
from notifications.client_messaging import (
    AMO_FIELD_ORDER_DATETIME,
    AMO_FIELD_OWNER_HANDLES,
    AMO_PIPELINE_REALIZATION,
    AMO_STAGE_CONFIRMED,
    AMO_STAGE_ORDER_CREATED,
    ASK_BEFORE,
    CONFIRM_REQUEST_EVENT,
    SILENCE_LIMIT,
)
from notifications.outbox import (
    PRE_SEND_OK,
    NotificationOutboxEntry,
    enqueue_notification,
    ensure_notification_schema,
)
from notifications.rules import load_notification_rules
from notifications.worker import NotificationWorker

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
SCHEMA = "confirmation_reschedule_test"
MSK = ZoneInfo("Europe/Moscow")
RULES = load_notification_rules(
    Path(__file__).resolve().parent.parent / "docs" / "notification_rules.json")

LEAD_ID = 31703400
DEAL_ID = 31703481
CLIENT_DIGITS = "79161112233"

CLIENTS_SQL = f"""
CREATE SCHEMA {SCHEMA};
SET search_path TO {SCHEMA};
-- Только то, что читают очередь писем и ожидания, — не копия прода.
CREATE TABLE clients (
    id                serial PRIMARY KEY,
    full_name         text,
    phone             text,
    phone_digits      text,
    bot_tg_user_id    bigint,
    bot_started       boolean,
    preferred_contact text
);
"""


def _fields(*, order_at, address):
    fields = []
    if order_at is not None:
        fields.append({"field_id": AMO_FIELD_ORDER_DATETIME,
                       "values": [{"value": int(order_at.timestamp())}]})
    if address:
        fields.append({"field_id": AMO_FIELD_ADDRESS, "values": [{"value": address}]})
    return fields


def _deal(*, order_at, status_id=AMO_STAGE_ORDER_CREATED, address="ул. Новая, 1"):
    return {"id": DEAL_ID, "pipeline_id": AMO_PIPELINE_REALIZATION,
            "status_id": status_id,
            "custom_fields_values": _fields(order_at=order_at, address=address)}


def _lead(*, address=None):
    return {"id": LEAD_ID, "pipeline_id": 1,
            "custom_fields_values": _fields(order_at=None, address=address)}


def _fake_amo(entities, calls=None):
    """Заглушка amoCRM: карточки по номеру, как `fetch_lead`.

    Исключение вместо карточки — CRM ответила на этот номер ошибкой.
    В `calls` пишутся открытия клиента (`"client"`) и запрошенные номера.
    """

    class FakeAmo:
        def __init__(self, *args, **kwargs):
            if calls is not None:
                calls.append("client")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch_lead(self, lead_id):
            if calls is not None:
                calls.append(int(lead_id))
            value = entities[int(lead_id)]
            if isinstance(value, Exception):
                raise value
            return value

    return FakeAmo


def _entry(outbox_id, client_id):
    """Письмо, как его видит проверка перед отправкой: ей нужны номер и вид."""
    return NotificationOutboxEntry(
        id=outbox_id, event_key=CONFIRM_REQUEST_EVENT, recipient_kind="client",
        client_id=client_id, template="", payload={}, locale="ru-RU",
        scheduled_at=datetime.now(MSK), attempts=1, client_phone="+79161112233",
        client_name="Анна", client_preferred_channel=None, client_user_id_wa=None,
        client_user_id_tg=None, client_user_id_max=None,
        client_requires_connection=False, notifications_enabled=True)


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class _RescheduleDbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            await admin.execute(CLIENTS_SQL)
            await ensure_notification_schema(admin)
            await bot.ensure_amocrm_api_schema(admin)
            await bot.ensure_client_messaging_schema(admin)
        finally:
            await admin.close()
        self.pool = await asyncpg.create_pool(
            dsn=TEST_DB_DSN, min_size=1, max_size=4,
            server_settings={"search_path": SCHEMA},
        )
        self.amo = {}
        self.amo_calls = []
        for target, value in (
            ("pool", self.pool),
            ("notification_rules", RULES),
            ("CLIENT_MESSAGING_ENABLED", True),
            ("CLIENT_MESSAGING_DRY_RUN", False),
            ("_amocrm_api_enabled", lambda: True),
            ("AmoCRMAPIClient", _fake_amo(self.amo, self.amo_calls)),
        ):
            patcher = mock.patch.object(bot, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Время на минуту назад от «сейчас» — письмо-вопрос уже пора отправлять.
        self.now = datetime.now(MSK).replace(second=0, microsecond=0)
        self.old_at = self.now + ASK_BEFORE - timedelta(minutes=1)
        self.new_at = (self.now + timedelta(days=10)).replace(hour=10, minute=0)
        async with self.pool.acquire() as conn:
            self.client_id = await conn.fetchval(
                "INSERT INTO clients (full_name, phone, phone_digits) "
                "VALUES ('Анна', '+7 916 111-22-33', $1) RETURNING id",
                CLIENT_DIGITS)

    async def asyncTearDown(self):
        await self.pool.close()
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        finally:
            await admin.close()

    # --- помощники ---

    async def _pending(self, *, status="planned", sent=False, answer=None,
                       rescheduled=False, letter_status="pending"):
        """Ожидание по старой дате и его письмо-вопрос. Возвращает номер письма."""
        async with self.pool.acquire() as conn:
            outbox_id = await enqueue_notification(
                conn, RULES, event_key=CONFIRM_REQUEST_EVENT,
                client_id=self.client_id,
                payload={"date": f"{self.old_at:%d.%m.%Y}",
                         "time": f"{self.old_at:%H:%M}", "address": "ул. Старая, 5"},
                scheduled_at=self.old_at - ASK_BEFORE)
            if letter_status != "pending":
                await conn.execute(
                    "UPDATE notification_outbox SET status=$2, sent_at=NOW() WHERE id=$1",
                    outbox_id, letter_status)
            await conn.execute(
                """
                INSERT INTO order_confirmations (
                    lead_id, deal_id, client_id, phone_digits, order_at, status,
                    outbox_id, asked_at, asked_sent_at, answered_at, answer_text,
                    notified_at, rescheduled_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                        CASE WHEN $9 THEN NOW() - interval '1 hour' END,
                        CASE WHEN $10::text IS NOT NULL THEN NOW() END, $10,
                        CASE WHEN $6 = 'owner_notified' THEN NOW() END,
                        CASE WHEN $11 THEN NOW() - interval '1 day' END)
                """,
                LEAD_ID, DEAL_ID, self.client_id, CLIENT_DIGITS, self.old_at, status,
                outbox_id, self.old_at - ASK_BEFORE, sent, answer, rescheduled)
        return outbox_id

    async def _row(self):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM order_confirmations WHERE lead_id = $1", LEAD_ID)

    async def _letter(self, outbox_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM notification_outbox WHERE id = $1", outbox_id)

    async def _letters(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM notification_outbox ORDER BY id")

    async def _journal(self, event_type="confirmation_reschedule"):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM amocrm_api_events WHERE event_type = $1", event_type)

    def _crm(self, deal, lead=None):
        self.amo[DEAL_ID] = deal
        self.amo[LEAD_ID] = lead if lead is not None else _lead()

    async def _run_worker(self):
        """Один проход работника очереди с той же проверкой, что на проде."""
        worker = NotificationWorker(
            self.pool, RULES,
            before_send=bot._confirmation_before_send,
            after_send=bot._confirmation_after_send)
        send = mock.AsyncMock(side_effect=AssertionError("письмо не должно уйти"))
        with mock.patch("notifications.worker.send_with_rules", send):
            self.assertTrue(await worker._process_once())
        send.assert_not_called()


class ReschedulePreSendTests(_RescheduleDbCase):
    async def test_moved_before_question_replans_through_the_worker(self):
        """Решение 1: перенесли до вопроса — старый вопрос отменён, новый
        стоит за сутки до новой даты, с новой датой в письме.

        Проходит через настоящего работника очереди: он отменяет письмо,
        которое держит в руках, а пересчёт его номер не трогает — двойной
        отмены и ошибки нет.
        """
        old_id = await self._pending()
        self._crm(_deal(order_at=self.new_at))

        await self._run_worker()

        old = await self._letter(old_id)
        self.assertEqual(old["status"], "cancelled")
        self.assertEqual(old["last_error"],
                         f"заказ перенесён на {self.new_at:%d.%m.%Y %H:%M}")
        letters = await self._letters()
        self.assertEqual(len(letters), 2)
        new = letters[1]
        self.assertEqual(new["event_key"], CONFIRM_REQUEST_EVENT)
        self.assertEqual(new["status"], "pending")
        self.assertEqual(new["client_id"], self.client_id)
        self.assertEqual(new["scheduled_at"], self.new_at - ASK_BEFORE)
        payload = json.loads(new["payload"])
        self.assertEqual(payload, {"date": f"{self.new_at:%d.%m.%Y}",
                                   "time": "10:00", "address": "ул. Новая, 1"})

        row = await self._row()
        self.assertEqual(row["order_at"], self.new_at)
        self.assertEqual(row["asked_at"], self.new_at - ASK_BEFORE)
        self.assertEqual(row["outbox_id"], new["id"])
        self.assertEqual(row["status"], "planned")
        self.assertIsNone(row["asked_sent_at"])
        self.assertIsNone(row["rescheduled_at"])  # replan: вопроса не было — отметка не ставится (правка 3)

        journal = await self._journal()
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["action"], "replan")
        self.assertEqual(journal[0]["entity_id"], str(LEAD_ID))
        details = json.loads(journal[0]["payload"])
        self.assertEqual(details["deal_id"], DEAL_ID)
        self.assertEqual(details["old_status"], "planned")
        self.assertEqual(datetime.fromisoformat(details["old_order_at"]), self.old_at)
        self.assertEqual(datetime.fromisoformat(details["new_order_at"]), self.new_at)
        self.assertEqual(datetime.fromisoformat(details["new_asked_at"]),
                         self.new_at - ASK_BEFORE)
        # Обычная отмена вопроса («заказа больше нет») здесь не пишется.
        self.assertEqual(await self._journal("confirmation_status"), [])

    async def test_moved_earlier_is_caught_too(self):
        """Перенос на более раннюю дату — тоже перенос (решение координатора)."""
        self.old_at = self.now + timedelta(days=20)
        old_id = await self._pending()
        earlier = (self.now + timedelta(days=5)).replace(hour=9, minute=30)
        self._crm(_deal(order_at=earlier))

        verdict = await bot._confirmation_before_send(_entry(old_id, self.client_id))

        self.assertEqual(verdict.action, "cancel")
        row = await self._row()
        self.assertEqual(row["order_at"], earlier)
        self.assertEqual(row["asked_at"], earlier - ASK_BEFORE)
        self.assertNotEqual(row["outbox_id"], old_id)

    async def test_moved_to_less_than_a_day_drops_the_question(self):
        """Решение владельца: до работы меньше суток — вопроса нет."""
        old_id = await self._pending()
        soon = self.now + timedelta(hours=5)
        self._crm(_deal(order_at=soon))

        await self._run_worker()

        self.assertEqual((await self._letter(old_id))["status"], "cancelled")
        self.assertEqual(len(await self._letters()), 1, "нового письма быть не должно")
        row = await self._row()
        self.assertEqual(row["status"], "dropped")
        self.assertEqual(row["order_at"], soon)
        self.assertIsNone(row["asked_at"])
        self.assertIsNone(row["outbox_id"])
        self.assertIsNone(row["rescheduled_at"])  # replan: вопроса не было — отметка не ставится (правка 3)

    async def test_unchanged_order_lets_the_question_go(self):
        """Дата не менялась — проверка как раньше: слать, ничего не трогать."""
        old_id = await self._pending()
        self._crm(_deal(order_at=self.old_at))

        verdict = await bot._confirmation_before_send(_entry(old_id, self.client_id))

        self.assertEqual(verdict, PRE_SEND_OK)
        row = await self._row()
        self.assertEqual(row["outbox_id"], old_id)
        self.assertIsNone(row["rescheduled_at"])
        self.assertEqual(await self._journal(), [])

    async def test_deal_without_date_lets_the_question_go(self):
        old_id = await self._pending()
        self._crm(_deal(order_at=None))

        verdict = await bot._confirmation_before_send(_entry(old_id, self.client_id))

        self.assertEqual(verdict, PRE_SEND_OK)
        self.assertEqual(await self._journal(), [])

    async def test_confirmed_deal_is_asked_again_only_after_reschedule(self):
        """Решение 4: сделка остаётся в «Заказ подтвержден», клиента спрашиваем
        заново. Пропуск этого этапа — только при отметке о переносе."""
        self._crm(_deal(order_at=self.old_at, status_id=AMO_STAGE_CONFIRMED))

        old_id = await self._pending()
        verdict = await bot._confirmation_before_send(_entry(old_id, self.client_id))
        self.assertEqual(verdict.action, "cancel")
        self.assertEqual((await self._row())["status"], "dropped")

        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM order_confirmations")
        again_id = await self._pending(rescheduled=True)
        verdict = await bot._confirmation_before_send(_entry(again_id, self.client_id))
        self.assertEqual(verdict, PRE_SEND_OK)
        self.assertEqual((await self._row())["status"], "planned")

    async def test_rehearsal_writes_nothing(self):
        """Репетиция: только строка в логе, в базу и очередь — ничего."""
        old_id = await self._pending()
        self._crm(_deal(order_at=self.new_at))

        with mock.patch.object(bot, "CLIENT_MESSAGING_DRY_RUN", True), \
                self.assertLogs(bot.logger, level="INFO") as logs:
            verdict = await bot._confirmation_before_send(_entry(old_id, self.client_id))

        self.assertEqual(verdict, PRE_SEND_OK)
        self.assertTrue(any("перенос" in line for line in logs.output), logs.output)
        self.assertEqual((await self._letter(old_id))["status"], "pending")
        self.assertEqual(len(await self._letters()), 1)
        row = await self._row()
        self.assertEqual(row["order_at"], self.old_at)
        self.assertIsNone(row["rescheduled_at"])
        self.assertEqual(await self._journal(), [])

    async def test_address_missing_in_deal_is_taken_from_lead(self):
        """Адрес — как при планировании: из сделки, недостающее — из лида."""
        old_id = await self._pending()
        self._crm(_deal(order_at=self.new_at, address=None),
                  _lead(address="ул. Лидовая, 7"))

        await bot._confirmation_before_send(_entry(old_id, self.client_id))

        new = (await self._letters())[-1]
        self.assertEqual(json.loads(new["payload"])["address"], "ул. Лидовая, 7")

    async def test_orphan_question_is_cancelled(self):
        """Правка 1 ревью 13.10: письмо-вопрос, на которое не ссылается ни одно
        ожидание, ни к какому заказу больше не относится — оно не уходит.

        Законного пути получить такое письмо нет: ожидание и письмо ставятся
        вместе. Но вопрос, переставленный пересчётом на новое письмо, а потом
        вернувшийся в очередь, — именно такой, и он спрашивал бы про старую дату.
        """
        async with self.pool.acquire() as conn:
            orphan_id = await enqueue_notification(
                conn, RULES, event_key=CONFIRM_REQUEST_EVENT,
                client_id=self.client_id,
                payload={"date": f"{self.old_at:%d.%m.%Y}",
                         "time": f"{self.old_at:%H:%M}", "address": "ул. Старая, 5"},
                scheduled_at=self.old_at - ASK_BEFORE)

        await self._run_worker()

        letter = await self._letter(orphan_id)
        self.assertEqual(letter["status"], "cancelled")
        self.assertEqual(letter["last_error"], "вопрос больше не связан с заказом")
        self.assertEqual(self.amo_calls, [], "в CRM за сиротой не ходим")

    async def test_question_without_deal_still_goes(self):
        """Ожидание есть, а связь со сделкой не нашлась — шлём, как раньше."""
        old_id = await self._pending()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE order_confirmations SET deal_id = NULL WHERE lead_id = $1", LEAD_ID)

        verdict = await bot._confirmation_before_send(_entry(old_id, self.client_id))

        self.assertEqual(verdict, PRE_SEND_OK)

    async def test_journal_is_not_in_daily_summary(self):
        """Решение 3: владельцу о перестановке не пишем — и строкой в суточной
        сводке тоже. След остаётся только во внутреннем журнале."""
        old_id = await self._pending()
        self._crm(_deal(order_at=self.new_at))
        await bot._confirmation_before_send(_entry(old_id, self.client_id))
        self.assertEqual(len(await self._journal()), 1)

        text = await bot.build_client_messaging_daily_summary_text()

        self.assertNotIn("replan", text)
        self.assertNotIn("перенес", text.lower())


class RescheduleRecomputeTests(_RescheduleDbCase):
    """Сам пересчёт — его же позовёт и обход раз в 10 минут (задача 2)."""

    def _order(self, order_at, address="ул. Новая, 1"):
        return bot.OrderDetails(order_at=order_at, address=address)

    async def _reschedule(self, order_at, **kwargs):
        async with self.pool.acquire() as conn:
            return await bot._confirmation_reschedule(
                conn, LEAD_ID, order=self._order(order_at),
                now=datetime.now(MSK), **kwargs)

    async def test_pending_letter_is_cancelled_and_replaced(self):
        old_id = await self._pending()

        self.assertEqual(await self._reschedule(self.new_at), "replan")

        old = await self._letter(old_id)
        self.assertEqual(old["status"], "cancelled")
        self.assertEqual(old["last_error"],
                         f"заказ перенесён на {self.new_at:%d.%m.%Y %H:%M}")
        self.assertEqual((await self._row())["outbox_id"], (await self._letters())[-1]["id"])

    async def test_in_hand_letter_is_left_to_the_worker(self):
        """Письмо, которое работник очереди держит в руках, отменяет он сам —
        по вердикту проверки. Пересчёт его не трогает."""
        old_id = await self._pending()

        self.assertEqual(await self._reschedule(self.new_at, in_hand_outbox_id=old_id),
                         "replan")

        self.assertEqual((await self._letter(old_id))["status"], "pending")
        self.assertEqual(len(await self._letters()), 2)

    async def test_letter_in_the_workers_hands_is_busy(self):
        """Правка 1 ревью 13.10: работник взял письмо в отправку, а пересчёт
        пришёл не от него (обход). Ничего не трогаем: письмо ещё может вернуться
        в очередь (ночная тишина, повтор проверки, сбой отправки) и уйти про
        старую дату. Обход повторит через 10 минут."""
        old_id = await self._pending()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE notification_outbox SET status='sending' WHERE id=$1", old_id)
        before = await self._row()

        self.assertEqual(await self._reschedule(self.new_at), "busy")

        self.assertEqual((await self._letter(old_id))["status"], "sending")
        self.assertEqual(len(await self._letters()), 1)
        row = await self._row()
        for column in ("order_at", "outbox_id", "status", "asked_at", "rescheduled_at"):
            self.assertEqual(row[column], before[column], column)
        self.assertEqual(await self._journal(), [])

    async def test_letter_already_gone_counts_as_reask(self):
        """Письмо успело уйти, пока шёл пересчёт: новое всё равно ставится."""
        old_id = await self._pending(letter_status="sent")

        self.assertEqual(await self._reschedule(self.new_at), "reask")

        self.assertEqual((await self._letter(old_id))["status"], "sent")
        letters = await self._letters()
        self.assertEqual(len(letters), 2)
        self.assertEqual(letters[-1]["scheduled_at"], self.new_at - ASK_BEFORE)
        self.assertEqual((await self._row())["status"], "planned")

    async def test_reask_closes_the_old_wait(self):
        """Решение 2: вопрос ушёл, клиент ответил — перенос на другой день
        закрывает старое ожидание и спрашивает заново."""
        await self._pending(status="confirmed", sent=True, answer="Да",
                            letter_status="sent")

        self.assertEqual(await self._reschedule(self.new_at), "reask")

        row = await self._row()
        self.assertEqual(row["status"], "planned")
        self.assertEqual(row["order_at"], self.new_at)
        self.assertEqual(row["asked_at"], self.new_at - ASK_BEFORE)
        for column in ("asked_sent_at", "answered_at", "answer_text", "notified_at"):
            self.assertIsNone(row[column], column)
        self.assertIsNotNone(row["rescheduled_at"])
        details = json.loads((await self._journal())[0]["payload"])
        self.assertEqual(details["old_status"], "confirmed")
        self.assertEqual(details["old_answer_text"], "Да")
        self.assertIsNotNone(details["old_asked_sent_at"])

    async def test_owner_notified_wait_is_closed_without_calling_again(self):
        """Молчание по старой дате: ожидание закрыто, отметка «звали» снята,
        новый вопрос — по новой дате."""
        await self._pending(status="owner_notified", sent=True, letter_status="sent")

        self.assertEqual(await self._reschedule(self.new_at), "reask")

        row = await self._row()
        self.assertEqual(row["status"], "planned")
        self.assertIsNone(row["notified_at"])
        self.assertIsNone(row["asked_sent_at"])

    async def test_same_day_new_time_only_updates_time(self):
        await self._pending(status="confirmed", sent=True, answer="Да",
                            letter_status="sent")
        before = await self._row()
        same_day = self.old_at + timedelta(minutes=30)
        if same_day.astimezone(MSK).date() != self.old_at.astimezone(MSK).date():
            same_day = self.old_at - timedelta(minutes=30)

        self.assertEqual(await self._reschedule(same_day), "update_time")

        row = await self._row()
        self.assertEqual(row["order_at"], same_day)
        for column in ("status", "outbox_id", "asked_at", "asked_sent_at",
                       "answer_text", "rescheduled_at"):
            self.assertEqual(row[column], before[column], column)
        self.assertEqual(len(await self._letters()), 1)
        journal = await self._journal()
        self.assertEqual([r["action"] for r in journal], ["update_time"])

    async def test_nothing_changed_writes_nothing(self):
        await self._pending()

        self.assertEqual(await self._reschedule(self.old_at), "none")

        self.assertEqual(len(await self._letters()), 1)
        self.assertIsNone((await self._row())["rescheduled_at"])
        self.assertEqual(await self._journal(), [])

    async def test_every_recompute_has_its_own_journal_line(self):
        """Ключ журнала уникален на каждый пересчёт: перенос туда и обратно —
        две строки, а не одна."""
        await self._pending()
        await self._reschedule(self.new_at)
        await self._reschedule(self.new_at + timedelta(days=1))

        self.assertEqual(len(await self._journal()), 2)


# Ещё заказы — для проходов обхода по нескольким ожиданиям сразу.
EXTRA = [(31703500 + n, 31703600 + n) for n in range(4)]


def _deal_for(deal_id, **kwargs):
    return {**_deal(**kwargs), "id": deal_id}


class RescheduleWatchTests(_RescheduleDbCase):
    """Обход раз в 10 минут (задача 2): ловит перенос сам, не дожидаясь письма.

    Проверка перед отправкой видит перенос только за сутки до старой даты, а
    перенос на более раннюю дату к этому часу уже опоздал бы. И вопрос, уже
    заданный клиенту, она не видит вовсе — письма в очереди больше нет.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Каждый тест начинается как первый проход после старта бота.
        patcher = mock.patch.object(bot, "_reschedule_watch_first_pass_done", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _other(self, lead_id, deal_id, *, order_at, status="planned", sent=False):
        """Ещё одно ожидание, без письма в очереди."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO order_confirmations (
                    lead_id, deal_id, client_id, phone_digits, order_at, status,
                    asked_at, asked_sent_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::timestamptz,
                        CASE WHEN $8 THEN $7::timestamptz END)
                """,
                lead_id, deal_id, self.client_id, CLIENT_DIGITS, order_at, status,
                order_at - ASK_BEFORE, sent)

    async def _row_of(self, lead_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM order_confirmations WHERE lead_id = $1", lead_id)

    async def test_moved_earlier_before_question_is_replanned(self):
        """Перенос на более раннюю дату: вопрос стоит на старое, позднее время,
        и проверка перед отправкой поймала бы перенос слишком поздно."""
        self.old_at = self.now + timedelta(days=20)
        old_id = await self._pending()
        earlier = (self.now + timedelta(days=5)).replace(hour=9, minute=30)
        self._crm(_deal(order_at=earlier))

        await bot.run_confirmation_reschedule_watch()

        old = await self._letter(old_id)
        self.assertEqual(old["status"], "cancelled")
        self.assertEqual(old["last_error"],
                         f"заказ перенесён на {earlier:%d.%m.%Y %H:%M}")
        letters = await self._letters()
        self.assertEqual(len(letters), 2)
        new = letters[-1]
        self.assertEqual(new["status"], "pending")
        self.assertEqual(new["scheduled_at"], earlier - ASK_BEFORE)
        self.assertEqual(json.loads(new["payload"]),
                         {"date": f"{earlier:%d.%m.%Y}", "time": "09:30",
                          "address": "ул. Новая, 1"})
        row = await self._row()
        self.assertEqual(row["order_at"], earlier)
        self.assertEqual(row["asked_at"], earlier - ASK_BEFORE)
        self.assertEqual(row["outbox_id"], new["id"])
        self.assertEqual(row["status"], "planned")
        self.assertIsNone(row["rescheduled_at"])  # replan: вопроса не было — отметка не ставится (правка 3)
        self.assertEqual([r["action"] for r in await self._journal()], ["replan"])

    async def test_letter_being_sent_is_left_until_it_is_back(self):
        """Правка 1 ревью 13.10: старое письмо в отправке — обход его не трогает
        и вопрос не перевешивает. Раньше перевешивал: письмо, вернувшееся потом
        в очередь, уходило про старую дату (ссылки на него уже не было — проверка
        перед отправкой пропускала), а следом — второй вопрос.

        Письмо вернулось в очередь — следующий проход пересчитывает как обычно.
        """
        old_id = await self._pending()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE notification_outbox SET status='sending' WHERE id=$1", old_id)
        self._crm(_deal(order_at=self.new_at))

        await bot.run_confirmation_reschedule_watch()

        self.assertEqual((await self._letter(old_id))["status"], "sending")
        self.assertEqual(len(await self._letters()), 1)
        row = await self._row()
        self.assertEqual(row["outbox_id"], old_id)
        self.assertEqual(row["order_at"], self.old_at)
        self.assertEqual(await self._journal(), [])

        # Ночная тишина вернула письмо в очередь до утра.
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE notification_outbox SET status='pending' WHERE id=$1", old_id)

        await bot.run_confirmation_reschedule_watch()

        self.assertEqual((await self._letter(old_id))["status"], "cancelled")
        row = await self._row()
        self.assertEqual(row["order_at"], self.new_at)
        self.assertEqual(row["outbox_id"], (await self._letters())[-1]["id"])
        self.assertEqual([r["action"] for r in await self._journal()], ["replan"])

    async def test_answered_yes_then_moved_is_asked_again(self):
        """Решения 2 и 4: клиент сказал «да», сделка в «Заказ подтвержден», а
        заказ перенесли на другой день. Спрашиваем заново за сутки до новой
        даты; сторож молчунов по старому вопросу владельца не зовёт."""
        await self._pending(status="confirmed", sent=True, answer="Да",
                            letter_status="sent")
        async with self.pool.acquire() as conn:
            # Вопрос ушёл дольше, чем сторож молчунов ждёт ответа: не сбрось
            # пересчёт отметку отправки — сторож позвал бы владельца сразу.
            await conn.execute(
                "UPDATE order_confirmations SET asked_sent_at = $2 WHERE lead_id = $1",
                LEAD_ID, datetime.now(MSK) - SILENCE_LIMIT - timedelta(hours=1))
        self._crm(_deal(order_at=self.new_at, status_id=AMO_STAGE_CONFIRMED))

        await bot.run_confirmation_reschedule_watch()

        row = await self._row()
        self.assertEqual(row["status"], "planned")
        self.assertEqual(row["order_at"], self.new_at)
        self.assertIsNotNone(row["rescheduled_at"])
        for column in ("asked_sent_at", "answered_at", "answer_text", "notified_at"):
            self.assertIsNone(row[column], column)
        new = (await self._letters())[-1]
        self.assertEqual(row["outbox_id"], new["id"])
        self.assertEqual(new["scheduled_at"], self.new_at - ASK_BEFORE)
        self.assertEqual([r["action"] for r in await self._journal()], ["reask"])

        notify = mock.AsyncMock()
        with mock.patch.object(bot, "_confirmation_notify_owner", notify):
            await bot.run_confirmation_silence_watch()
        notify.assert_not_called()

    async def test_replan_does_not_open_the_confirmed_stage(self):
        """Правка 3 ревью 13.10: отметка «переносился» — только при повторном
        вопросе. Решение владельца 4 — про сделку, которую робот сам перевёл в
        «Заказ подтвержден» по «да». При `replan` вопроса ещё не было, и сделка
        в «Заказ подтвержден» значит, что её перевёл человек: вопрос по прежнему
        правилу не нужен."""
        await self._pending()
        self._crm(_deal(order_at=self.new_at, status_id=AMO_STAGE_CONFIRMED))

        await bot.run_confirmation_reschedule_watch()

        row = await self._row()
        self.assertEqual([r["action"] for r in await self._journal()], ["replan"])
        self.assertIsNone(row["rescheduled_at"])
        new_id = row["outbox_id"]

        verdict = await bot._confirmation_before_send(_entry(new_id, self.client_id))

        self.assertEqual(verdict.action, "cancel")
        self.assertEqual(verdict.reason, "сделка ушла с этапа «Заказ оформлен»")
        self.assertEqual((await self._row())["status"], "dropped")

    async def test_reask_after_yes_lets_the_new_question_go(self):
        """Повторный вопрос после «да» по старой дате: сделка в «Заказ
        подтвержден» (её перевёл робот), новый вопрос уходит (решение 4)."""
        await self._pending(status="confirmed", sent=True, answer="Да",
                            letter_status="sent")
        self._crm(_deal(order_at=self.new_at, status_id=AMO_STAGE_CONFIRMED))

        await bot.run_confirmation_reschedule_watch()

        row = await self._row()
        self.assertEqual([r["action"] for r in await self._journal()], ["reask"])
        self.assertIsNotNone(row["rescheduled_at"])

        verdict = await bot._confirmation_before_send(
            _entry(row["outbox_id"], self.client_id))

        self.assertEqual(verdict, PRE_SEND_OK)
        self.assertEqual((await self._row())["status"], "planned")

    async def test_replan_keeps_an_earlier_reask_mark(self):
        """Заказ уже переносили после ответа, а потом передвинули ещё раз, пока
        новый вопрос ждал очереди: отметка остаётся — вопрос по-прежнему повторный."""
        await self._pending(rescheduled=True)
        before = (await self._row())["rescheduled_at"]
        self._crm(_deal(order_at=self.new_at, status_id=AMO_STAGE_CONFIRMED))

        await bot.run_confirmation_reschedule_watch()

        row = await self._row()
        self.assertEqual([r["action"] for r in await self._journal()], ["replan"])
        self.assertEqual(row["rescheduled_at"], before)

    async def test_same_day_new_time_only_updates_time(self):
        """Вопрос задан, день тот же, другое только время — заново не спрашиваем."""
        await self._pending(status="confirmed", sent=True, answer="Да",
                            letter_status="sent")
        same_day = self.old_at + timedelta(minutes=30)
        if same_day.date() != self.old_at.date():
            same_day = self.old_at - timedelta(minutes=30)
        self._crm(_deal(order_at=same_day, status_id=AMO_STAGE_CONFIRMED))

        await bot.run_confirmation_reschedule_watch()

        row = await self._row()
        self.assertEqual(row["order_at"], same_day)
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["answer_text"], "Да")
        self.assertIsNone(row["rescheduled_at"])
        self.assertEqual(len(await self._letters()), 1)
        self.assertEqual([r["action"] for r in await self._journal()], ["update_time"])

    async def test_first_pass_looks_back_30_days_then_only_ahead(self):
        """Живой случай 31703481: вопрос про 11.10 ушёл, клиент промолчал,
        владельца позвали, а заказ ещё 08.10 перенесли на 31.10. Первый проход
        после старта бота смотрит на 30 дней назад по сохранённой дате и
        подхватывает такие заказы; дальше — только заказы с датой впереди."""
        self.old_at = self.now - timedelta(days=3)
        await self._pending(status="owner_notified", sent=True, letter_status="sent")
        self._crm(_deal(order_at=self.new_at))
        (old_lead, old_deal), (late_lead, late_deal) = EXTRA[0], EXTRA[1]
        too_old = self.now - timedelta(days=40)
        await self._other(old_lead, old_deal, order_at=too_old,
                          status="owner_notified", sent=True)
        self.amo[old_deal] = _deal_for(old_deal, order_at=self.new_at)

        await bot.run_confirmation_reschedule_watch()

        row = await self._row()
        self.assertEqual(row["status"], "planned")
        self.assertEqual(row["order_at"], self.new_at)
        self.assertEqual(row["asked_at"], self.new_at - ASK_BEFORE)
        self.assertIsNone(row["notified_at"])
        self.assertIsNotNone(row["rescheduled_at"])
        self.assertEqual([r["action"] for r in await self._journal()], ["reask"])
        self.assertNotIn(old_deal, self.amo_calls, "старше 30 дней — не смотрим")
        self.assertEqual((await self._row_of(old_lead))["order_at"], too_old)

        # Второй проход: заказ, чья сохранённая дата уже прошла, не смотрим.
        passed = self.now - timedelta(days=2)
        await self._other(late_lead, late_deal, order_at=passed,
                          status="owner_notified", sent=True)
        self.amo[late_deal] = _deal_for(late_deal, order_at=self.new_at)
        self.amo_calls.clear()

        await bot.run_confirmation_reschedule_watch()

        self.assertNotIn(late_deal, self.amo_calls)
        self.assertIn(DEAL_ID, self.amo_calls, "заказ впереди сверяется каждый проход")
        self.assertEqual((await self._row_of(late_lead))["order_at"], passed)
        self.assertEqual(len(await self._journal()), 1, "второй раз переставлять нечего")

    async def test_first_pass_is_repeated_until_it_goes_clean(self):
        """Правка 2 ревью 13.10: первый проход «на 30 дней» засчитывается, только
        если прошёл чисто. Сбой CRM (429 клиент amo не повторяет) на заказе
        с прошедшей сохранённой датой — и раньше этот заказ не проверился бы
        больше никогда (живой случай 31703481, сохранена 11.10)."""
        self.old_at = self.now - timedelta(days=20)
        await self._pending(status="owner_notified", sent=True, letter_status="sent")
        self.amo[DEAL_ID] = AmoCRMAPIError(429, "слишком часто")
        self.amo[LEAD_ID] = _lead()

        await bot.run_confirmation_reschedule_watch()

        self.assertFalse(bot._reschedule_watch_first_pass_done)
        self.assertEqual((await self._row())["order_at"], self.old_at)

        # CRM ожила — второй проход снова смотрит на 30 дней и подхватывает заказ.
        self._crm(_deal(order_at=self.new_at))
        self.amo_calls.clear()

        await bot.run_confirmation_reschedule_watch()

        self.assertIn(DEAL_ID, self.amo_calls)
        row = await self._row()
        self.assertEqual(row["order_at"], self.new_at)
        self.assertEqual(row["status"], "planned")
        self.assertTrue(bot._reschedule_watch_first_pass_done)

        # Прошёл чисто — дальше обычная глубина: давний заказ не смотрим.
        (old_lead, old_deal) = EXTRA[0]
        await self._other(old_lead, old_deal, order_at=self.now - timedelta(days=20),
                          status="owner_notified", sent=True)
        self.amo[old_deal] = _deal_for(old_deal, order_at=self.new_at)
        self.amo_calls.clear()

        await bot.run_confirmation_reschedule_watch()

        self.assertNotIn(old_deal, self.amo_calls)

    async def test_first_pass_that_hit_the_limit_is_repeated(self):
        """Выборка упёрлась в лимит — дальние заказы не сверены, проход не чистый."""
        for lead_id, deal_id in EXTRA[:2]:
            await self._other(lead_id, deal_id, order_at=self.now + timedelta(days=3))
            self.amo[deal_id] = _deal_for(deal_id, order_at=self.now + timedelta(days=3))

        with mock.patch.object(bot, "RESCHEDULE_WATCH_LIMIT", 2):
            await bot.run_confirmation_reschedule_watch()
        self.assertFalse(bot._reschedule_watch_first_pass_done)

        with mock.patch.object(bot, "RESCHEDULE_WATCH_LIMIT", 3):
            await bot.run_confirmation_reschedule_watch()
        self.assertTrue(bot._reschedule_watch_first_pass_done)

    async def test_first_pass_with_a_busy_letter_is_repeated(self):
        """Письмо в отправке — пересчёт отложен (`busy`), обход обещал повторить.
        Засчитай первый проход — заказ с прошедшей датой больше не сверялся бы."""
        old_id = await self._pending()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE notification_outbox SET status='sending' WHERE id=$1", old_id)
        self._crm(_deal(order_at=self.new_at))

        await bot.run_confirmation_reschedule_watch()

        self.assertFalse(bot._reschedule_watch_first_pass_done)

    async def test_rehearsal_only_logs(self):
        """Репетиция: строка в логе, в базу и очередь — ничего."""
        old_id = await self._pending()
        self._crm(_deal(order_at=self.new_at))

        with mock.patch.object(bot, "CLIENT_MESSAGING_DRY_RUN", True), \
                self.assertLogs(bot.logger, level="INFO") as logs:
            await bot.run_confirmation_reschedule_watch()

        self.assertTrue(any("перенос" in line and str(DEAL_ID) in line
                            and "replan" in line for line in logs.output), logs.output)
        self.assertEqual((await self._letter(old_id))["status"], "pending")
        self.assertEqual(len(await self._letters()), 1)
        row = await self._row()
        self.assertEqual(row["order_at"], self.old_at)
        self.assertEqual(row["outbox_id"], old_id)
        self.assertIsNone(row["rescheduled_at"])
        self.assertEqual(await self._journal(), [])

    async def test_crm_failure_on_one_deal_does_not_stop_the_pass(self):
        """Сбой CRM на одной сделке — лог и дальше; следующий проход повторит."""
        (broken_lead, broken_deal), (silent_lead, silent_deal) = EXTRA[0], EXTRA[1]
        soon = self.now + timedelta(hours=30)
        # Сломанные сделки идут первыми: их даты раньше.
        await self._other(broken_lead, broken_deal, order_at=soon)
        await self._other(silent_lead, silent_deal, order_at=soon + timedelta(hours=1))
        self.amo[broken_deal] = AmoCRMAPIError(500, "amoCRM лежит")
        self.amo[silent_deal] = TimeoutError("сеть моргнула")
        self.old_at = self.now + timedelta(days=3)
        await self._pending(status="confirmed", sent=True, answer="Да",
                            letter_status="sent")
        self._crm(_deal(order_at=self.new_at, status_id=AMO_STAGE_CONFIRMED))

        await bot.run_confirmation_reschedule_watch()

        self.assertEqual((await self._row())["order_at"], self.new_at)
        self.assertEqual((await self._row_of(broken_lead))["order_at"], soon)
        self.assertEqual([r["action"] for r in await self._journal()], ["reask"])
        self.assertEqual(self.amo_calls.count("client"), 1, "один клиент CRM на проход")

    async def test_deals_that_are_not_ours_are_left_alone(self):
        """Сделки нет, она ушла из воронки реализации или заказ ведёт владелец —
        это работа отмены и проверки перед отправкой; обход их не трогает."""
        moved = _deal(order_at=self.new_at)
        owner_flag = {"field_id": AMO_FIELD_OWNER_HANDLES, "values": [{"value": "1"}]}
        cards = [
            AmoCRMAPIError(404, "сделку удалили"),
            {},                                   # amoCRM отвечает 204 без тела
            {**moved, "id": EXTRA[2][1], "pipeline_id": 1},
            {**moved, "id": EXTRA[3][1],
             "custom_fields_values": moved["custom_fields_values"] + [owner_flag]},
        ]
        start = self.now + timedelta(days=3)
        for (lead_id, deal_id), card in zip(EXTRA, cards):
            await self._other(lead_id, deal_id, order_at=start)
            self.amo[deal_id] = card

        await bot.run_confirmation_reschedule_watch()

        for lead_id, deal_id in EXTRA:
            self.assertIn(deal_id, self.amo_calls)
            row = await self._row_of(lead_id)
            self.assertEqual(row["order_at"], start, deal_id)
            self.assertIsNone(row["rescheduled_at"], deal_id)
        self.assertEqual(await self._journal(), [])
        self.assertEqual(await self._letters(), [])

    async def test_only_live_waits_with_a_deal_are_checked(self):
        """Брошенные ожидания (`dropped`) и заказы без сделки обход не читает."""
        start = self.now + timedelta(days=3)
        await self._other(EXTRA[0][0], EXTRA[0][1], order_at=start, status="dropped")
        await self._other(EXTRA[1][0], None, order_at=start)

        await bot.run_confirmation_reschedule_watch()

        self.assertEqual([call for call in self.amo_calls if call != "client"], [])

    async def test_address_missing_in_deal_is_taken_from_lead(self):
        """Адрес — как при планировании: из сделки, недостающее — из лида."""
        await self._pending()
        self._crm(_deal(order_at=self.new_at, address=None),
                  _lead(address="ул. Лидовая, 7"))

        await bot.run_confirmation_reschedule_watch()

        new = (await self._letters())[-1]
        self.assertEqual(json.loads(new["payload"])["address"], "ул. Лидовая, 7")


if __name__ == "__main__":
    unittest.main()
