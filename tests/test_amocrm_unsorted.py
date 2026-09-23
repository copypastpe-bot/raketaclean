"""Карточки «Неразобранного»: вид, сборка, текст, окно отправки.

ТЗ docs/plans/2026-09-23-amo-unsorted-cards.md, задача 1.
"""

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from notifications.amocrm_unsorted import (
    KIND_CALL,
    KIND_MESSAGE,
    MOSCOW_TZ,
    OPEN_LEAD_BUTTON_TEXT,
    UnsortedCard,
    build_unsorted_card,
    done_button_text,
    first_send_at,
    next_reminder_at,
    render_card_text,
    unsorted_kind,
)

API_BASE = "https://example.amocrm.ru"
MSK = ZoneInfo("Europe/Moscow")


def _msk(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def _ts(dt):
    return int(dt.timestamp())


def _call_item(**metadata):
    meta = {"called_at": _ts(_msk(2026, 9, 23, 14, 5)), "duration": 0, "from": "+79991234567"}
    meta.update(metadata)
    return {
        "uid": "u" * 60,
        "category": "sip",
        "pipeline_id": 55,
        "created_at": _ts(_msk(2026, 9, 23, 14, 6)),
        "_embedded": {"leads": [{"id": 123}], "contacts": [{"id": 10}]},
        "metadata": meta,
    }


def _chat_item(**metadata):
    meta = {
        "received_at": _ts(_msk(2026, 9, 23, 18, 42)),
        "from": "Сергей",
        "service": "wahelp.whatbot",
        "source_name": "telegram",
        "client": {"name": "Сергей", "avatar": "https://example.com/a.png"},
    }
    meta.update(metadata)
    return {
        "uid": "c" * 60,
        "category": "chats",
        "pipeline_id": 55,
        "created_at": _ts(_msk(2026, 9, 23, 18, 43)),
        "_embedded": {"leads": [{"id": 456}], "contacts": [{"id": 20}]},
        "metadata": meta,
    }


CONTACT_WITH_PHONE = {
    "id": 10,
    "name": "Иван",
    "custom_fields_values": [
        {"field_code": "PHONE", "values": [{"value": "+79991234567"}]},
    ],
}
CONTACT_NO_PHONE = {"id": 20, "name": "Сергей", "custom_fields_values": []}


class UnsortedKindTests(unittest.TestCase):
    def test_sip_is_call(self):
        self.assertEqual(unsorted_kind({"category": "sip"}), KIND_CALL)

    def test_chats_is_message(self):
        self.assertEqual(unsorted_kind({"category": "chats"}), KIND_MESSAGE)

    def test_other_category_is_none(self):
        self.assertIsNone(unsorted_kind({"category": "forms"}))
        self.assertIsNone(unsorted_kind({"category": "mail"}))

    def test_missing_category_is_none(self):
        self.assertIsNone(unsorted_kind({}))
        self.assertIsNone(unsorted_kind({"category": None}))


class BuildCardTests(unittest.TestCase):
    def test_call_card_from_item_and_contact(self):
        card = build_unsorted_card(_call_item(), api_base=API_BASE, contact=CONTACT_WITH_PHONE)

        self.assertIsInstance(card, UnsortedCard)
        self.assertEqual(card.uid, "u" * 60)
        self.assertEqual(card.kind, KIND_CALL)
        self.assertEqual(card.lead_id, 123)
        self.assertEqual(card.contact_name, "Иван")
        self.assertEqual(card.phone, "+79991234567")
        self.assertEqual(card.event_at, _msk(2026, 9, 23, 14, 5))
        self.assertEqual(card.link, "https://example.amocrm.ru/leads/detail/123")

    def test_message_card_takes_name_from_metadata_client(self):
        card = build_unsorted_card(_chat_item(), api_base=API_BASE, contact=CONTACT_NO_PHONE)

        self.assertEqual(card.kind, KIND_MESSAGE)
        self.assertEqual(card.lead_id, 456)
        self.assertEqual(card.contact_name, "Сергей")
        self.assertIsNone(card.phone)
        self.assertEqual(card.event_at, _msk(2026, 9, 23, 18, 42))

    def test_phone_from_metadata_when_contact_missing(self):
        card = build_unsorted_card(_call_item(), api_base=API_BASE, contact=None)

        self.assertEqual(card.phone, "+79991234567")
        self.assertIsNone(card.contact_name)

    def test_name_from_metadata_from_when_it_is_not_a_phone(self):
        item = _chat_item(client={}, **{"from": "Мария"})

        card = build_unsorted_card(item, api_base=API_BASE, contact=None)

        self.assertEqual(card.contact_name, "Мария")
        self.assertIsNone(card.phone)

    def test_event_time_falls_back_to_item_created_at(self):
        item = _call_item()
        del item["metadata"]["called_at"]

        card = build_unsorted_card(item, api_base=API_BASE, contact=CONTACT_WITH_PHONE)

        self.assertEqual(card.event_at, _msk(2026, 9, 23, 14, 6))

    def test_event_time_falls_back_to_now_without_any_time(self):
        item = _chat_item()
        del item["metadata"]["received_at"]
        del item["created_at"]
        now = _msk(2026, 9, 23, 19, 0)

        card = build_unsorted_card(item, api_base=API_BASE, contact=None, now=now)

        self.assertEqual(card.event_at, now)

    def test_no_lead_means_no_link(self):
        item = _call_item()
        item["_embedded"]["leads"] = []

        card = build_unsorted_card(item, api_base=API_BASE, contact=CONTACT_WITH_PHONE)

        self.assertIsNone(card.lead_id)
        self.assertIsNone(card.link)

    def test_unknown_kind_raises(self):
        item = _call_item()
        item["category"] = "forms"

        with self.assertRaises(ValueError):
            build_unsorted_card(item, api_base=API_BASE, contact=None)


class RenderCardTests(unittest.TestCase):
    def test_call_card_text(self):
        card = build_unsorted_card(_call_item(), api_base=API_BASE, contact=CONTACT_WITH_PHONE)

        self.assertEqual(
            render_card_text(card),
            "Пропущенный звонок\n"
            "Клиент: Иван\n"
            "Номер: +79991234567\n"
            "Время звонка: 23.09 14:05\n"
            "Ссылка: https://example.amocrm.ru/leads/detail/123",
        )

    def test_message_card_text_without_phone(self):
        card = build_unsorted_card(_chat_item(), api_base=API_BASE, contact=CONTACT_NO_PHONE)

        self.assertEqual(
            render_card_text(card),
            "Новое сообщение\n"
            "Клиент: Сергей\n"
            "Номер:\n"
            "Время сообщения: 23.09 18:42\n"
            "Ссылка: https://example.amocrm.ru/leads/detail/456",
        )

    def test_message_card_text_with_phone(self):
        contact = dict(CONTACT_WITH_PHONE, id=20, name="Сергей")
        card = build_unsorted_card(_chat_item(), api_base=API_BASE, contact=contact)

        self.assertIn("\nНомер: +79991234567\n", render_card_text(card))

    def test_closed_call_card_adds_mark(self):
        card = build_unsorted_card(_call_item(), api_base=API_BASE, contact=CONTACT_WITH_PHONE)

        self.assertEqual(
            render_card_text(card, closed=True),
            render_card_text(card) + "\n✅ Перезвонил",
        )

    def test_closed_message_card_adds_mark(self):
        card = build_unsorted_card(_chat_item(), api_base=API_BASE, contact=CONTACT_NO_PHONE)

        self.assertEqual(
            render_card_text(card, closed=True),
            render_card_text(card) + "\n✅ Ответил",
        )

    def test_time_is_moscow_even_if_event_at_is_utc(self):
        card = UnsortedCard(
            uid="x",
            kind=KIND_CALL,
            lead_id=None,
            contact_name=None,
            phone=None,
            event_at=datetime(2026, 9, 23, 6, 30, tzinfo=timezone.utc),
            link=None,
        )

        self.assertEqual(
            render_card_text(card),
            "Пропущенный звонок\n"
            "Клиент:\n"
            "Номер:\n"
            "Время звонка: 23.09 09:30\n"
            "Ссылка:",
        )

    def test_button_texts(self):
        self.assertEqual(OPEN_LEAD_BUTTON_TEXT, "Перейти в сделку")
        self.assertEqual(done_button_text(KIND_CALL), "Я перезвонил")
        self.assertEqual(done_button_text(KIND_MESSAGE), "Ответил")


class SendWindowTests(unittest.TestCase):
    def test_before_window_goes_to_nine_same_day(self):
        self.assertEqual(first_send_at(_msk(2026, 9, 23, 8, 59)), _msk(2026, 9, 23, 9, 0))

    def test_nine_sharp_is_in_window(self):
        self.assertEqual(first_send_at(_msk(2026, 9, 23, 9, 0)), _msk(2026, 9, 23, 9, 0))

    def test_inside_window_is_now(self):
        self.assertEqual(first_send_at(_msk(2026, 9, 23, 19, 30)), _msk(2026, 9, 23, 19, 30))

    def test_twenty_sharp_goes_to_nine_next_day(self):
        self.assertEqual(first_send_at(_msk(2026, 9, 23, 20, 0)), _msk(2026, 9, 24, 9, 0))

    def test_late_evening_goes_to_nine_next_day(self):
        self.assertEqual(first_send_at(_msk(2026, 9, 23, 23, 0)), _msk(2026, 9, 24, 9, 0))

    def test_after_midnight_goes_to_nine_same_day(self):
        self.assertEqual(first_send_at(_msk(2026, 9, 24, 0, 30)), _msk(2026, 9, 24, 9, 0))

    def test_month_end_rolls_over(self):
        self.assertEqual(first_send_at(_msk(2026, 9, 30, 21, 0)), _msk(2026, 10, 1, 9, 0))

    def test_utc_input_is_judged_by_moscow_clock(self):
        # 05:59 UTC = 08:59 МСК
        now = datetime(2026, 9, 23, 5, 59, tzinfo=timezone.utc)

        self.assertEqual(first_send_at(now), _msk(2026, 9, 23, 9, 0))

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValueError):
            first_send_at(datetime(2026, 9, 23, 12, 0))

    def test_result_is_moscow_time(self):
        result = first_send_at(datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc))

        self.assertEqual(result.utcoffset(), timedelta(hours=3))


class ReminderTests(unittest.TestCase):
    def test_reminder_in_an_hour(self):
        self.assertEqual(next_reminder_at(_msk(2026, 9, 23, 18, 30)), _msk(2026, 9, 23, 19, 30))

    def test_reminder_just_before_window_end(self):
        self.assertEqual(next_reminder_at(_msk(2026, 9, 23, 18, 59)), _msk(2026, 9, 23, 19, 59))

    def test_reminder_past_window_goes_to_nine_next_day(self):
        self.assertEqual(next_reminder_at(_msk(2026, 9, 23, 19, 30)), _msk(2026, 9, 24, 9, 0))

    def test_reminder_on_twenty_sharp_goes_to_nine_next_day(self):
        self.assertEqual(next_reminder_at(_msk(2026, 9, 23, 19, 0)), _msk(2026, 9, 24, 9, 0))

    def test_reminder_from_nine_is_ten(self):
        self.assertEqual(next_reminder_at(_msk(2026, 9, 24, 9, 0)), _msk(2026, 9, 24, 10, 0))

    def test_constants_live_in_module(self):
        self.assertEqual(str(MOSCOW_TZ), "Europe/Moscow")


if __name__ == "__main__":
    unittest.main()
