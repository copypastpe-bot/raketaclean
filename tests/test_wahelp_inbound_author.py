"""Чьё сообщение пришло из WAHelp (bot.py, `handle_wahelp_inbound`).

Инцидент 2026-10-21: менеджер ответил клиентке из amoCRM web или из Telegram
на телефоне «Да, счет и акт оставили». WAHelp прислал это сообщение в тот же
вебхук с меткой не «from_operator», робот принял его за ответ клиентки на
вопрос о заказе и написал ей «Спасибо. Специалист позвонит за 30 минут до
приезда» — уже после выполненной работы.

Решение владельца 2026-10-23: клиентом считаются только метки «from_client»
и «from» (так они названы в `scripts/export_wahelp_chats.py`), всё прочее
робот пропускает мимо. Метку пропущенного пишет в журнал без текста и
телефона — кроме своих «from_operator» и пустой: это наши письма и их
статусы, они только засорили бы журнал.

Ожидание подтверждения кончается в момент начала работы: робот передаёт
правилу время работы, и позднее «да» ничего не подтверждает.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import bot

CLIENT_PHONE = "+7 916 111-22-33"
CLIENT_DIGITS = "79161112233"
STAFF_TEXT = "Да, счет и акт оставили👌🏼"


def _payload(text: str, destination: str) -> dict:
    return {"data": {"destination": destination, "message": text,
                     "user": {"phone": CLIENT_PHONE}}}


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


class InboundAuthorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pending = {
            "lead_id": 501, "deal_id": 502, "client_id": 7, "status": "owner_notified",
            "order_at": datetime.now(timezone.utc) + timedelta(hours=12),
            "phone_digits": CLIENT_DIGITS, "notified_at": datetime.now(timezone.utc),
            "asked_sent_at": datetime.now(timezone.utc) - timedelta(hours=2),
            "full_name": "Влада", "phone": CLIENT_PHONE,
        }
        self.pending_for = mock.AsyncMock(return_value=self.pending)
        self.accept = mock.AsyncMock()
        self.call_owner = mock.AsyncMock()
        patches = [
            mock.patch.object(bot, "pool", _FakePool()),
            mock.patch.object(bot, "CLIENT_MESSAGING_ENABLED", True),
            mock.patch.object(bot, "_confirmation_pending_for", self.pending_for),
            mock.patch.object(bot, "_confirmation_accept", self.accept),
            mock.patch.object(bot, "_confirmation_call_owner", self.call_owner),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_staff_message_is_not_a_client_answer(self):
        for destination in ("from_amocrm", "from_messenger", "from_manager",
                            "from_bot", "from_operator", "", "something_new"):
            with self.subTest(destination=destination):
                for m in (self.pending_for, self.accept, self.call_owner):
                    m.reset_mock()

                handled = await bot.handle_wahelp_inbound(_payload(STAFF_TEXT, destination))

                self.assertFalse(handled)
                self.pending_for.assert_not_awaited()
                self.accept.assert_not_awaited()
                self.call_owner.assert_not_awaited()

    async def test_client_labels_still_reach_the_robot(self):
        for destination in ("from_client", "from", "FROM_CLIENT"):
            with self.subTest(destination=destination):
                self.accept.reset_mock()

                handled = await bot.handle_wahelp_inbound(_payload("Да", destination))

                self.assertTrue(handled)
                self.accept.assert_awaited_once()

    async def test_skipped_label_goes_to_log_without_text_and_phone(self):
        with self.assertLogs(bot.logger, level="INFO") as logs:
            await bot.handle_wahelp_inbound(_payload(STAFF_TEXT, "from_amocrm"))

        line = "\n".join(logs.output)
        self.assertIn("from_amocrm", line)
        self.assertNotIn("счет", line)
        self.assertNotIn(CLIENT_DIGITS[-4:], line)

    async def test_own_messages_are_not_logged(self):
        for destination in ("from_operator", ""):
            with self.subTest(destination=destination):
                with mock.patch.object(bot.logger, "info") as info:
                    await bot.handle_wahelp_inbound(_payload(STAFF_TEXT, destination))

                info.assert_not_called()

    async def test_yes_after_work_started_is_ignored(self):
        self.pending["order_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)

        await bot.handle_wahelp_inbound(_payload("Да", "from_client"))

        self.accept.assert_not_awaited()
        self.call_owner.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
