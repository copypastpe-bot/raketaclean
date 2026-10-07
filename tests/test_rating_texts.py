"""Тексты оценки и подпись «Мы всегда на связи» — решение владельца 2026-10-07.

Просьба об оценке начинается с самой просьбы: в уведомлении видна первая
строка, а приветствие её занимало. Ответ на пятёрку ведёт в одну точку —
форму отзыва на Яндексе, поэтому подписи с мессенджерами в нём нет. Подпись
в остальных сервисных сообщениях отделена двумя пустыми строками.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from crm import wahelp_dispatcher as wd

RULES_PATH = Path(__file__).resolve().parents[1] / "docs" / "notification_rules.json"

RATING_REQUEST = ("raketaclean.ru: как вам наша работа? Оцените от 1 до 5 — "
                  "просто отправьте цифру в ответ 🙂")
HIGH_RATING_REPLY = (
    "Спасибо, очень рады! 🤩\n"
    "\n"
    "Оставите пару слов на Яндексе? Это минута, а нам очень поможет: "
    "новые клиенты выбирают по отзывам.\n"
    "👉 https://yandex.ru/maps/org/raketaclean_ru/14124463093/reviews/?add-review=true\n"
    "\n"
    "Достаточно написать, какая была услуга и понравился ли результат."
)

FOOTER_KEYS = (
    "order_completed_summary",
    "cleaning_order_completed_summary",
    "order_rating_reminder",
    "cleaning_order_rating_reminder",
    "order_rating_response_mid_client",
    "order_rating_response_low_client",
)


def _rules() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _event(key: str) -> dict:
    return next(item for item in _rules()["events"] if item["key"] == key)


class RatingTextsTests(unittest.TestCase):
    def test_rating_request_starts_with_the_request(self):
        for key in ("order_rating_reminder", "cleaning_order_rating_reminder"):
            self.assertEqual(_event(key)["template"], RATING_REQUEST, key)

    def test_high_rating_reply_leads_to_yandex_form(self):
        self.assertEqual(_event("order_rating_response_high_client")["template"],
                         HIGH_RATING_REPLY)

    def test_chain_copy_matches_the_event(self):
        """Цепочка в файле правил — описание, но расходиться с событием не должна."""
        chain = next(item for item in _rules()["chains"]
                     if item["key"] == "post_completion_rating_flow")
        branch = next(item for item in chain["branches"] if item["when"] == "rating == 5")
        self.assertEqual(branch["actions"][0]["template"], HIGH_RATING_REPLY)


class ContactFooterTests(unittest.TestCase):
    def test_footer_separated_by_two_blank_lines(self):
        for key in FOOTER_KEYS:
            self.assertEqual(
                wd._with_contact_footer("Текст", wd.WHATSAPP_CHANNEL, key),
                f"Текст\n\n\n{wd.WA_CONTACT_FOOTER}", key)

    def test_fallback_text_uses_the_same_gap(self):
        with mock.patch.object(wd, "WA_TG_FALLBACK_TEXT", "Пишите в Telegram"):
            self.assertEqual(
                wd._with_contact_footer("Текст", wd.TELEGRAM_CHANNEL, "order_rating_reminder"),
                "Текст\n\n\nПишите в Telegram")

    def test_high_rating_reply_has_no_footer(self):
        self.assertEqual(
            wd._with_contact_footer("Текст", wd.MAX_CHANNEL,
                                    "order_rating_response_high_client"),
            "Текст")


if __name__ == "__main__":
    unittest.main()
