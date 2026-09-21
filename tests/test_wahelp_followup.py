"""Досылка в WhatsApp тем, кто не прочитал сообщение в первом канале.

Механизм появился 17.11.2025 (коммит «маршрутизация ТГ/ВА») и не работал
ни одного дня: вызов `send_text_to_phone` в `crm/wahelp_dispatcher.py` стоял,
а в импорт модуля функция не попала. Каждое срабатывание падало с
`NameError`, ошибка гасилась в `except Exception` и уходила только в журнал
сервера, поэтому снаружи всё выглядело исправным — обычные сообщения идут
другим путём и работают.

Поймано службой оповещений 21.09, в первые минуты репетиции: 16 клиентов за
сутки, 401 случай за то время, что хранил журнал. Этот тест держит связку
«досылка доходит до отправки» на месте.
"""

from __future__ import annotations

import unittest
from unittest import mock

from crm import wahelp_dispatcher


class FollowupSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_followup_reaches_the_whatsapp_sender(self):
        """Задача досылки доходит до отправки, а не падает на пути к ней."""
        sent: dict[str, object] = {}

        async def fake_send(channel_kind, *, phone, name, text):
            sent.update(channel=channel_kind, phone=phone, name=name, text=text)
            return {"ok": True}

        with mock.patch.object(wahelp_dispatcher, "send_text_to_phone", fake_send), \
             mock.patch.object(wahelp_dispatcher, "FOLLOWUP_DELAY_SECONDS", 0), \
             mock.patch.object(wahelp_dispatcher, "WA_FOLLOWUP_DISABLED", False):
            await wahelp_dispatcher.schedule_followup_for_client(
                client_id=777,
                phone="+70000000000",
                name="Тест",
                text="Напоминание о заказе",
            )
            task = wahelp_dispatcher._followup_tasks.get(777)
            self.assertIsNotNone(task, "задача досылки не заведена")
            await task

        self.assertEqual(sent.get("text"), "Напоминание о заказе")
        self.assertEqual(sent.get("phone"), "+70000000000")
        self.assertEqual(sent.get("channel"), wahelp_dispatcher.WHATSAPP_CHANNEL)

    async def test_followup_is_skipped_when_switched_off(self):
        """Выключатель `WA_FOLLOWUP_DISABLED` гасит досылку целиком."""
        with mock.patch.object(wahelp_dispatcher, "WA_FOLLOWUP_DISABLED", True):
            await wahelp_dispatcher.schedule_followup_for_client(
                client_id=778, phone="+70000000000", name="Тест", text="Не должно уйти")

        self.assertIsNone(wahelp_dispatcher._followup_tasks.get(778))
