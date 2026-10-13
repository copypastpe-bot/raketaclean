"""Точка подключения проверки перед отправкой письма.

Работник очереди — тупой курьер: он умеет только доставлять письма и ничего
не знает ни про amoCRM, ни про ожидания подтверждения. Но письмо-вопрос нельзя
отправлять вслепую: между планированием и отправкой проходят сутки, и за эти
сутки заказ мог исчезнуть.

Поэтому у курьера есть две точки подключения, которые заполняет `bot.py`:

* `before_send` — «можно ли ещё слать?». Три ответа: слать, отменить письмо
  (заказа больше нет) и отложить (CRM не ответила — это не повод молчать
  навсегда).
* `after_send` — «письмо ушло». По нему в ожидании появляется факт отправки,
  и только с этого момента ответ клиента считается ответом.

Главное, что здесь проверяется: **отметка об отправке ставится в той же
транзакции, что и сама отметка «письмо отправлено».** Разойдись они — робот
получил бы отправленный вопрос, про который не помнит, что задавал его.
"""

import dataclasses
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from notifications.outbox import PRE_SEND_OK, NotificationOutboxEntry, PreSendVerdict
from notifications.rules import load_notification_rules
from notifications.worker import NotificationWorker

MSK = ZoneInfo("Europe/Moscow")
RULES = load_notification_rules(
    Path(__file__).resolve().parent.parent / "docs" / "notification_rules.json")


class FakeTransaction:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        self.log.append("tx:begin")
        return self

    async def __aexit__(self, *exc):
        self.log.append("tx:commit")
        return False


class FakeConn:
    def __init__(self, log):
        self.log = log

    def transaction(self):
        return FakeTransaction(self.log)

    async def execute(self, *args, **kwargs):
        return None


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeSendResult:
    channel = "clients_wa"
    response = {"data": {"id": "msg-1"}}


def make_entry(event_key="order_confirm_request"):
    return NotificationOutboxEntry(
        id=77,
        event_key=event_key,
        recipient_kind="clients",
        client_id=1268,
        template="Подтвердите заказ на {{date}}",
        payload={"date": "08.09.2026"},
        locale="ru-RU",
        scheduled_at=None,
        attempts=1,
        client_phone="79990000000",
        client_name="Анна",
        client_preferred_channel=None,
        client_user_id_wa=None,
        client_user_id_tg=None,
        client_user_id_max=None,
        client_requires_connection=False,
        notifications_enabled=True,
    )


class BeforeSendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = []
        self.pool = FakePool(FakeConn(self.log))
        self.entry = make_entry()

    def _worker(self, **kwargs):
        return NotificationWorker(self.pool, rules=None, **kwargs)

    async def test_letter_is_sent_when_nothing_objects(self):
        async def allow(entry):
            return PRE_SEND_OK

        worker = self._worker(before_send=allow)
        with mock.patch("notifications.worker.send_with_rules") as send, \
                mock.patch("notifications.worker.mark_outbox_sent") as sent:
            send.return_value = FakeSendResult()
            await worker._handle_entry(self.entry)
        self.assertTrue(send.called)
        self.assertTrue(sent.called)

    async def test_cancelled_letter_never_reaches_the_client(self):
        """Заказа больше нет — вопрос не уходит, письмо отменяется с причиной."""
        async def refuse(entry):
            return PreSendVerdict("cancel", "заказа больше нет в CRM")

        worker = self._worker(before_send=refuse)
        with mock.patch("notifications.worker.send_with_rules") as send, \
                mock.patch("notifications.worker.cancel_outbox_entry") as cancel:
            await worker._handle_entry(self.entry)
        self.assertFalse(send.called)
        self.assertTrue(cancel.called)
        self.assertEqual(cancel.call_args.args[2], "заказа больше нет в CRM")

    async def test_unreachable_crm_only_postpones_the_letter(self):
        """CRM не ответила — это не «заказа нет». Письмо ждёт следующей попытки,
        иначе сетевая заминка навсегда съедала бы живые вопросы."""
        async def postpone(entry):
            return PreSendVerdict("retry", "amoCRM не ответила")

        worker = self._worker(before_send=postpone)
        with mock.patch("notifications.worker.send_with_rules") as send, \
                mock.patch("notifications.worker.cancel_outbox_entry") as cancel, \
                mock.patch("notifications.worker.mark_outbox_failure") as failure:
            await worker._handle_entry(self.entry)
        self.assertFalse(send.called)
        self.assertFalse(cancel.called)
        self.assertTrue(failure.called)

    async def test_postponed_letter_waits_longer_than_a_usual_retry(self):
        """Попыток всего пять. Тратить их за полчаса на одну и ту же недоступную
        CRM бессмысленно: живой вопрос сгорел бы, не дождавшись её возвращения."""
        async def postpone(entry):
            return PreSendVerdict("retry", "amoCRM не ответила")

        worker = self._worker(before_send=postpone, precheck_retry_minutes=15)
        with mock.patch("notifications.worker.send_with_rules"), \
                mock.patch("notifications.worker.mark_outbox_failure") as failure:
            await worker._handle_entry(self.entry)
        self.assertEqual(failure.call_args.kwargs["retry_delay_minutes"], 15)

    async def test_worker_without_hooks_works_as_before(self):
        worker = self._worker()
        with mock.patch("notifications.worker.send_with_rules") as send, \
                mock.patch("notifications.worker.mark_outbox_sent") as sent:
            send.return_value = FakeSendResult()
            await worker._handle_entry(self.entry)
        self.assertTrue(send.called)
        self.assertTrue(sent.called)


class AfterSendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = []
        self.pool = FakePool(FakeConn(self.log))
        self.entry = make_entry()

    async def test_fact_of_sending_is_recorded_in_the_same_transaction(self):
        seen = []

        async def after(conn, entry):
            self.log.append("after_send")
            seen.append(entry)

        worker = NotificationWorker(self.pool, rules=None, after_send=after)
        with mock.patch("notifications.worker.send_with_rules") as send, \
                mock.patch("notifications.worker.mark_outbox_sent") as sent:
            send.return_value = FakeSendResult()
            sent.side_effect = lambda *a, **kw: self.log.append("mark_sent")
            await worker._handle_entry(self.entry)
        self.assertEqual(seen, [self.entry])
        self.assertEqual(self.log, ["tx:begin", "mark_sent", "after_send", "tx:commit"])

    async def test_nothing_is_recorded_when_sending_failed(self):
        called = []

        async def after(conn, entry):
            called.append(entry)

        worker = NotificationWorker(self.pool, rules=None, after_send=after)
        with mock.patch("notifications.worker.send_with_rules") as send, \
                mock.patch("notifications.worker.mark_outbox_failure"):
            send.side_effect = RuntimeError("wahelp упал")
            await worker._handle_entry(self.entry)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()


class CancellingALetterByItsNumber(unittest.TestCase):
    """Отмена письма, номер которого пришёл из чужой таблицы.

    Ожидание подтверждения помнит номер письма-вопроса сутками. За эти сутки
    письмо могло уйти клиенту, и пометить ушедшее «отменено» — значит соврать
    в суточной сводке владельца. Поэтому отмена по номеру трогает только те
    письма, что ещё лежат в очереди.
    """

    def setUp(self):
        self.queries: list[str] = []

    def _conn(self, result):
        queries = self.queries

        class Conn:
            async def fetchval(self, query, *args):
                queries.append(query)
                return result

        return Conn()

    def test_only_pending_letters_are_cancelled(self):
        import asyncio

        from notifications.outbox import cancel_pending_outbox

        done = asyncio.run(cancel_pending_outbox(self._conn(17), 17, "заказ отменён"))
        self.assertTrue(done)
        self.assertIn("status='pending'", self.queries[0].replace(" ", ""))

    def test_letter_already_sent_reports_nothing_done(self):
        import asyncio

        from notifications.outbox import cancel_pending_outbox

        done = asyncio.run(cancel_pending_outbox(self._conn(None), 17, "заказ отменён"))
        self.assertFalse(done)

    def test_worker_cancellation_still_touches_any_status(self):
        """Работник очереди отменяет письмо, которое сам держит в руках."""
        import asyncio

        from notifications.outbox import cancel_outbox_entry

        entry = mock.Mock(id=17)
        asyncio.run(cancel_outbox_entry(self._conn(17), entry, "заказа больше нет"))
        self.assertNotIn("status='pending'", self.queries[0].replace(" ", ""))


class QuietHoursTests(unittest.IsolatedAsyncioTestCase):
    """Ночная тишина у работника очереди (ТЗ docs/plans/2026-10-13-quiet-hours.md).

    С 20:00 до 10:00 по Москве письмо клиенту или лиду не уходит и не тратит
    попытку: оно возвращается в очередь к 10:00. Ответ на сообщение клиента
    (`"reply": true` в правилах) уходит сразу. Мастерам и владельцу — как раньше.
    Проверка — после дешёвых проверок и до `before_send`: ночью незачем ходить
    в CRM. Часы подменяются.
    """

    NIGHT = datetime(2026, 10, 13, 23, 30, tzinfo=MSK)
    DAY = datetime(2026, 10, 13, 14, 0, tzinfo=MSK)
    MORNING = datetime(2026, 10, 14, 10, 0, tzinfo=MSK)

    def setUp(self):
        self.log = []
        self.pool = FakePool(FakeConn(self.log))
        self.checked = []

    async def _before_send(self, entry):
        self.checked.append(entry.id)
        return PRE_SEND_OK

    def _entry(self, event_key="order_completed_summary", recipient_kind="client", **kw):
        return dataclasses.replace(make_entry(event_key), recipient_kind=recipient_kind, **kw)

    async def _handle(self, entry, *, now, enabled=True):
        worker = NotificationWorker(self.pool, rules=RULES, before_send=self._before_send,
                                    client_quiet_hours=enabled)
        with mock.patch("notifications.worker._now", return_value=now), \
                mock.patch("notifications.worker.send_with_rules") as send, \
                mock.patch("notifications.worker.mark_outbox_sent"), \
                mock.patch("notifications.worker.cancel_outbox_entry") as cancel, \
                mock.patch("notifications.worker.defer_outbox_entry") as defer:
            send.return_value = FakeSendResult()
            await worker._handle_entry(entry)
        return send, defer, cancel

    async def test_client_letter_at_night_waits_until_ten_without_asking_crm(self):
        send, defer, _ = await self._handle(self._entry(), now=self.NIGHT)
        self.assertFalse(send.called)
        self.assertTrue(defer.called)
        self.assertEqual(defer.call_args.args[1:], (77, self.MORNING))
        self.assertEqual(self.checked, [])

    async def test_reply_to_the_client_goes_at_night(self):
        for key in ("order_confirmed_thanks", "order_rating_response_high_client"):
            with self.subTest(event=key):
                send, defer, _ = await self._handle(self._entry(key), now=self.NIGHT)
                self.assertTrue(send.called)
                self.assertFalse(defer.called)

    async def test_daytime_letter_goes_as_before(self):
        send, defer, _ = await self._handle(self._entry(), now=self.DAY)
        self.assertTrue(send.called)
        self.assertFalse(defer.called)
        self.assertEqual(self.checked, [77])

    async def test_switch_off_letter_goes_at_night(self):
        send, defer, _ = await self._handle(self._entry(), now=self.NIGHT, enabled=False)
        self.assertTrue(send.called)
        self.assertFalse(defer.called)

    async def test_worker_without_the_flag_keeps_quiet_hours_off(self):
        worker = NotificationWorker(self.pool, rules=RULES)
        with mock.patch("notifications.worker._now", return_value=self.NIGHT), \
                mock.patch("notifications.worker.send_with_rules") as send, \
                mock.patch("notifications.worker.mark_outbox_sent"), \
                mock.patch("notifications.worker.defer_outbox_entry") as defer:
            send.return_value = FakeSendResult()
            await worker._handle_entry(self._entry())
        self.assertTrue(send.called)
        self.assertFalse(defer.called)

    async def test_lead_letter_at_night_waits(self):
        send, defer, _ = await self._handle(self._entry(recipient_kind="lead"), now=self.NIGHT)
        self.assertFalse(send.called)
        self.assertTrue(defer.called)

    async def test_master_letter_at_night_goes_as_before(self):
        send, defer, _ = await self._handle(
            self._entry("order_changed_notify_master", recipient_kind="master"),
            now=self.NIGHT)
        self.assertTrue(send.called)
        self.assertFalse(defer.called)

    async def test_event_missing_from_rules_is_not_a_reply(self):
        send, defer, _ = await self._handle(self._entry("no_such_event"), now=self.NIGHT)
        self.assertFalse(send.called)
        self.assertTrue(defer.called)

    async def test_cheap_checks_still_cancel_at_night(self):
        """Нет телефона — письмо отменяется в любое время, а не ждёт утра."""
        send, defer, cancel = await self._handle(
            self._entry(client_phone=None), now=self.NIGHT)
        self.assertFalse(send.called)
        self.assertFalse(defer.called)
        self.assertTrue(cancel.called)
