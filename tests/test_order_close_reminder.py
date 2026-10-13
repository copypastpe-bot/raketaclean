"""Сторож проведения заказов: время, окно, текст, названия услуг.

ТЗ docs/plans/2026-10-13-order-close-reminder.md, задача 1 (2026-10-13).

Чистые функции `notifications/order_close_reminder.py`, без базы и Telegram:
первое напоминание — не раньше следующего дня после записи в 10:00 МСК;
повтор — через 30 минут; окно [10:00, 20:00) МСК, всё вне окна — на ближайшие
10:00; «Напомнить завтра» — 10:00 следующего дня; текст — по решению
владельца 4 (без телефона клиента и без фразы про календарь).
"""

import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from notifications import order_close_reminder as ocr

MSK = ZoneInfo("Europe/Moscow")


def _msk(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


class WindowTests(unittest.TestCase):
    def test_window_bounds(self):
        cases = (
            (_msk(2026, 10, 5, 9, 59), False),
            (_msk(2026, 10, 5, 10, 0), True),
            (_msk(2026, 10, 5, 19, 59), True),
            (_msk(2026, 10, 5, 20, 0), False),
            (_msk(2026, 10, 5, 0, 0), False),
        )
        for moment, expected in cases:
            with self.subTest(moment=moment):
                self.assertIs(ocr.in_window(moment), expected)

    def test_window_uses_moscow_time_for_utc_input(self):
        # 07:00 UTC = 10:00 МСК.
        self.assertTrue(ocr.in_window(datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)))
        self.assertFalse(ocr.in_window(datetime(2026, 10, 5, 6, 59, tzinfo=timezone.utc)))

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            ocr.in_window(datetime(2026, 10, 5, 12, 0))
        with self.assertRaises(ValueError):
            ocr.first_send_at(date(2026, 10, 4), datetime(2026, 10, 5, 12, 0))
        with self.assertRaises(ValueError):
            ocr.next_reminder_at(datetime(2026, 10, 5, 12, 0))
        with self.assertRaises(ValueError):
            ocr.snooze_until(datetime(2026, 10, 5, 12, 0))


class FirstSendTests(unittest.TestCase):
    ORDER_DATE = date(2026, 10, 4)

    def test_never_earlier_than_next_day_ten(self):
        cases = (
            # В сам день записи — ещё рано: ждём завтра 10:00.
            (_msk(2026, 10, 4, 15, 0), _msk(2026, 10, 5, 10, 0)),
            (_msk(2026, 10, 4, 23, 30), _msk(2026, 10, 5, 10, 0)),
            # На следующий день до 10:00 — ровно 10:00.
            (_msk(2026, 10, 5, 0, 0), _msk(2026, 10, 5, 10, 0)),
            (_msk(2026, 10, 5, 9, 59), _msk(2026, 10, 5, 10, 0)),
            # Ровно 10:00 и позже в окне — сразу.
            (_msk(2026, 10, 5, 10, 0), _msk(2026, 10, 5, 10, 0)),
            (_msk(2026, 10, 5, 14, 17), _msk(2026, 10, 5, 14, 17)),
            (_msk(2026, 10, 5, 19, 59), _msk(2026, 10, 5, 19, 59)),
            # 20:00 и позже — ночью молчит, ближайшие 10:00.
            (_msk(2026, 10, 5, 20, 0), _msk(2026, 10, 6, 10, 0)),
            (_msk(2026, 10, 5, 23, 0), _msk(2026, 10, 6, 10, 0)),
            # Запись давняя (запуск сторожа на 14 дней назад) — сразу, в окне.
            (_msk(2026, 10, 13, 12, 0), _msk(2026, 10, 13, 12, 0)),
            (_msk(2026, 10, 13, 8, 0), _msk(2026, 10, 13, 10, 0)),
        )
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(ocr.first_send_at(self.ORDER_DATE, now), expected)

    def test_result_is_moscow_time(self):
        result = ocr.first_send_at(self.ORDER_DATE, datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(result, _msk(2026, 10, 5, 12, 0))
        self.assertEqual(result.tzinfo, MSK)


class NextReminderTests(unittest.TestCase):
    def test_thirty_minutes_inside_window(self):
        cases = (
            (_msk(2026, 10, 5, 10, 0), _msk(2026, 10, 5, 10, 30)),
            (_msk(2026, 10, 5, 19, 29), _msk(2026, 10, 5, 19, 59)),
            # Повтор попал на 20:00 или позже — ближайшие 10:00.
            (_msk(2026, 10, 5, 19, 30), _msk(2026, 10, 6, 10, 0)),
            (_msk(2026, 10, 5, 19, 59), _msk(2026, 10, 6, 10, 0)),
            # Отправка вне окна (не должно быть, но) — всё равно в окно.
            (_msk(2026, 10, 5, 9, 0), _msk(2026, 10, 5, 10, 0)),
        )
        for sent_at, expected in cases:
            with self.subTest(sent_at=sent_at):
                self.assertEqual(ocr.next_reminder_at(sent_at), expected)


class SnoozeTests(unittest.TestCase):
    def test_tomorrow_ten_by_moscow_calendar_day(self):
        cases = (
            (_msk(2026, 10, 5, 10, 0), _msk(2026, 10, 6, 10, 0)),
            (_msk(2026, 10, 5, 19, 59), _msk(2026, 10, 6, 10, 0)),
            # После 20:00 — тоже завтра 10:00.
            (_msk(2026, 10, 5, 20, 0), _msk(2026, 10, 6, 10, 0)),
            (_msk(2026, 10, 5, 23, 59), _msk(2026, 10, 6, 10, 0)),
            # До 10:00 — 10:00 следующего календарного дня, не сегодняшние 10:00.
            (_msk(2026, 10, 6, 0, 30), _msk(2026, 10, 7, 10, 0)),
            (_msk(2026, 10, 6, 9, 59), _msk(2026, 10, 7, 10, 0)),
        )
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(ocr.snooze_until(now), expected)

    def test_utc_input_counts_moscow_day(self):
        # 22:30 UTC 5-го = 01:30 МСК 6-го → завтра по Москве = 7-е.
        now = datetime(2026, 10, 5, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(ocr.snooze_until(now), _msk(2026, 10, 7, 10, 0))


class ServiceNamesTests(unittest.TestCase):
    def test_known_codes_in_record_order_first_capitalized(self):
        self.assertEqual(ocr.service_names(["carpeting", "furniture"]), "Ковры, мебель")
        self.assertEqual(ocr.service_names(["furniture", "carpeting"]), "Мебель, ковры")
        self.assertEqual(
            ocr.service_names(
                ["mattress", "cleaning", "rug_home", "windows", "furniture", "carpeting"]
            ),
            "Матрас, клининг, ковёр на дому, окна, мебель, ковры",
        )

    def test_unknown_code_as_is(self):
        self.assertEqual(ocr.service_names(["furniture", "sofa_x"]), "Мебель, sofa_x")
        self.assertEqual(ocr.service_names(["sofa_x", "furniture"]), "Sofa_x, мебель")

    def test_empty(self):
        self.assertEqual(ocr.service_names(None), "")
        self.assertEqual(ocr.service_names([]), "")
        self.assertEqual(ocr.service_names(["", "  "]), "")


class RenderTextTests(unittest.TestCase):
    def test_owner_sample(self):
        text = ocr.render_text(
            order_date=date(2026, 10, 4),
            client_name="Валерий",
            address="Афонино, Восточная, 342",
            services=["carpeting", "furniture"],
        )
        self.assertEqual(
            text,
            "⚠️ Заказ не проведён в боте\n"
            "04.10 · Валерий · Афонино, Восточная, 342\n"
            "Ковры, мебель\n"
            "\n"
            "Проведите заказ в боте.",
        )

    def test_empty_name_and_address_are_skipped(self):
        text = ocr.render_text(
            order_date=date(2026, 9, 29),
            client_name="  ",
            address=None,
            services=["windows"],
        )
        self.assertEqual(
            text,
            "⚠️ Заказ не проведён в боте\n29.09\nОкна\n\nПроведите заказ в боте.",
        )

    def test_no_services_no_empty_line(self):
        text = ocr.render_text(
            order_date=date(2026, 10, 4),
            client_name="Лора",
            address="",
            services=None,
        )
        self.assertEqual(
            text,
            "⚠️ Заказ не проведён в боте\n04.10 · Лора\n\nПроведите заказ в боте.",
        )

    def test_html_characters_kept_as_is_for_plain_text(self):
        # Отправляется без parse_mode: <, >, & остаются как в записи.
        text = ocr.render_text(
            order_date=date(2026, 10, 4),
            client_name="Кафе <Хюгге> & Ко",
            address="Мира, 1",
            services=["cleaning"],
        )
        self.assertIn("04.10 · Кафе <Хюгге> & Ко · Мира, 1", text)

    def test_no_phone_and_no_calendar_phrase(self):
        text = ocr.render_text(
            order_date=date(2026, 10, 4),
            client_name="Валерий",
            address="Афонино",
            services=["carpeting"],
        )
        self.assertNotIn("календар", text.lower())

    def test_button_and_marks(self):
        self.assertEqual(ocr.SNOOZE_BUTTON_TEXT, "Напомнить завтра")
        self.assertEqual(ocr.DONE_MARK, "✅ Заказ проведён")
        self.assertEqual(ocr.SNOOZED_MARK, "Отложено до завтра")


if __name__ == "__main__":
    unittest.main()
