"""Поиск сделки клиента в amoCRM по телефону (`notifications/order_address.py`).

С 16.09.2026 модуль сам за адресом в amoCRM не ходит (задача 3 ТЗ «адреса
до конца») — тесты на `fetch_deal_address`/`resolve_order_address` отсюда
убраны вместе с функциями. Осталось правило, которое мастер не увидит, а
владелец заметит поздно, если оно нарушится:

**Чужую сделку не берём.** amoCRM ищет контакт подстрокой по всем полям,
а у постоянного клиента открытых сделок бывает несколько (старая «ждёт
оплаты» и сегодняшняя). Контакт сверяем по окончанию номера, сделку
выбираем по близости даты работы к моменту проведения.
"""

import unittest
from datetime import datetime, timedelta, timezone

from notifications.order_address import (
    MAX_DEAL_DISTANCE,
    address_of_deal,
    contact_lead_ids,
    contact_matches_phone,
    phone_query,
    pick_open_deal,
)

MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 9, 6, 15, 0, tzinfo=MSK)

REALIZATION = 4482787
OTHER_PIPELINE = 1111111
STAGE_CONFIRMED = 41463838
STAGE_WON = 142
STAGE_LOST = 143

FIELD_ADDRESS = 18639
FIELD_ORDER_AT = 18701


def _deal(deal_id, *, pipeline=REALIZATION, status=STAGE_CONFIRMED,
          address=None, order_at=None, created_at=1_725_000_000):
    fields = []
    if address is not None:
        fields.append({"field_id": FIELD_ADDRESS, "values": [{"value": address}]})
    if order_at is not None:
        raw = order_at if isinstance(order_at, str) else str(int(order_at.timestamp()))
        fields.append({"field_id": FIELD_ORDER_AT, "values": [{"value": raw}]})
    return {
        "id": deal_id,
        "pipeline_id": pipeline,
        "status_id": status,
        "created_at": created_at,
        "custom_fields_values": fields,
    }


def _contact(contact_id, *phones, leads=()):
    fields = []
    if phones:
        fields.append({"field_code": "PHONE",
                       "values": [{"value": phone} for phone in phones]})
    return {
        "id": contact_id,
        "custom_fields_values": fields,
        "_embedded": {"leads": [{"id": lead_id} for lead_id in leads]},
    }


class PhoneQueryTests(unittest.TestCase):
    """amoCRM ищет по десяти цифрам без кода страны — иначе «+7» и «8» дают
    два разных запроса на один и тот же номер."""

    def test_plus_seven_becomes_ten_digits(self):
        self.assertEqual(phone_query("+79001234567"), "9001234567")

    def test_leading_eight_becomes_ten_digits(self):
        self.assertEqual(phone_query("89001234567"), "9001234567")

    def test_ten_digits_stay_as_is(self):
        self.assertEqual(phone_query("9001234567"), "9001234567")

    def test_formatting_is_ignored(self):
        self.assertEqual(phone_query("8 (900) 123-45-67"), "9001234567")

    def test_unusable_phone_gives_nothing(self):
        for phone in (None, "", "   ", "1234567", "+19001234567", "123456789012"):
            self.assertIsNone(phone_query(phone), phone)


class ContactMatchesPhoneTests(unittest.TestCase):
    """Поиск amoCRM — подстрочный по всем полям, а не по телефону. Контакт,
    у которого номер не тот, — чужой, каким бы путём он ни нашёлся."""

    def test_matches_by_phone_ending(self):
        contact = _contact(10, "+7 900 123-45-67")
        self.assertTrue(contact_matches_phone(contact, "9001234567"))

    def test_other_number_is_rejected(self):
        contact = _contact(10, "+79007654321")
        self.assertFalse(contact_matches_phone(contact, "9001234567"))

    def test_contact_without_phone_is_rejected(self):
        self.assertFalse(contact_matches_phone(_contact(10), "9001234567"))

    def test_any_of_several_phones_is_enough(self):
        contact = _contact(10, "+79007654321", "8-900-123-45-67")
        self.assertTrue(contact_matches_phone(contact, "9001234567"))


class ContactLeadIdsTests(unittest.TestCase):
    def test_collects_ids_in_order(self):
        self.assertEqual(contact_lead_ids(_contact(10, leads=(5, 3, 9))), [5, 3, 9])

    def test_empty_embedded_gives_nothing(self):
        self.assertEqual(contact_lead_ids({"id": 10}), [])
        self.assertEqual(contact_lead_ids({"id": 10, "_embedded": {}}), [])

    def test_garbage_entries_are_skipped(self):
        contact = {"id": 10, "_embedded": {"leads": [
            {"id": 5}, {"name": "без id"}, {"id": "abc"}, {"id": "9"},
        ]}}
        self.assertEqual(contact_lead_ids(contact), [5, 9])


class PickOpenDealTests(unittest.TestCase):
    """Сделка выбирается по дате работы, а не по свежести: у постоянного
    клиента «самая новая» на второй заказ подряд указала бы не на ту."""

    def test_other_pipeline_is_skipped(self):
        deals = [_deal(1, pipeline=OTHER_PIPELINE, order_at=NOW)]
        self.assertIsNone(pick_open_deal(deals, now=NOW))

    def test_closed_deals_are_skipped(self):
        deals = [_deal(1, status=STAGE_WON, order_at=NOW),
                 _deal(2, status=STAGE_LOST, order_at=NOW)]
        self.assertIsNone(pick_open_deal(deals, now=NOW))

    def test_nearest_date_wins_over_old_unpaid_deal(self):
        """Старая сделка «Заказ выполнен, ждёт оплаты» тоже открыта —
        но ездили не по ней."""
        old = _deal(1, order_at=NOW - timedelta(days=30), created_at=100)
        today = _deal(2, order_at=NOW - timedelta(hours=1), created_at=50)
        self.assertEqual(pick_open_deal([old, today], now=NOW)["id"], 2)

    def test_future_deal_loses_to_todays(self):
        """Заказ на следующую неделю уже заведён — сегодняшний ближе."""
        today = _deal(1, order_at=NOW - timedelta(hours=2))
        next_week = _deal(2, order_at=NOW + timedelta(days=7))
        self.assertEqual(pick_open_deal([next_week, today], now=NOW)["id"], 1)

    def test_equal_distance_prefers_past(self):
        future = _deal(1, order_at=NOW + timedelta(hours=2))
        past = _deal(2, order_at=NOW - timedelta(hours=2))
        self.assertEqual(pick_open_deal([future, past], now=NOW)["id"], 2)

    def test_dated_deal_beats_undated(self):
        undated = _deal(1, created_at=999)
        dated = _deal(2, order_at=NOW + timedelta(days=1), created_at=1)
        self.assertEqual(pick_open_deal([undated, dated], now=NOW)["id"], 2)

    def test_among_undated_newest_wins(self):
        older = _deal(1, created_at=100)
        newer = _deal(2, created_at=200)
        self.assertEqual(pick_open_deal([older, newer], now=NOW)["id"], 2)

    def test_broken_date_does_not_break_choice(self):
        """Поле правили руками: сделка считается сделкой без даты, проход не падает."""
        broken = _deal(1, order_at="abc", created_at=500)
        dated = _deal(2, order_at=NOW, created_at=1)
        self.assertEqual(pick_open_deal([broken, dated], now=NOW)["id"], 2)
        self.assertEqual(pick_open_deal([broken], now=NOW)["id"], 1)

    def test_far_dated_deal_is_not_taken(self):
        """Сделка на следующий месяц — другой заказ по другому адресу.
        Неверный адрес хуже отсутствующего: «нет адреса» честно откатится
        на карточку клиента, а неверный заметят поздно."""
        deals = [_deal(1, order_at=NOW + timedelta(days=30), address="Другой адрес")]
        self.assertIsNone(pick_open_deal(deals, now=NOW))

    def test_todays_undated_deal_beats_far_dated(self):
        """Сегодняшнюю сделку завели без даты, а на следующий месяц у клиента
        уже открыта другая: ехали сегодня — и берём сегодняшнюю."""
        undated_today = _deal(1, created_at=1_725_000_000)
        far = _deal(2, order_at=NOW + timedelta(days=30), created_at=1)
        self.assertEqual(pick_open_deal([far, undated_today], now=NOW)["id"], 1)

    def test_month_old_dated_deal_loses_to_undated(self):
        """Сделка месячной давности к сегодняшнему выезду отношения не имеет,
        даже если у сегодняшней даты работы нет вовсе."""
        old = _deal(1, order_at=NOW - timedelta(days=30), created_at=1)
        undated = _deal(2, created_at=1_725_000_000)
        self.assertEqual(pick_open_deal([old, undated], now=NOW)["id"], 2)

    def test_window_edge_is_still_taken(self):
        """Граница окна принадлежит окну, секунда за ней — уже нет."""
        edge = _deal(1, order_at=NOW - timedelta(seconds=MAX_DEAL_DISTANCE))
        self.assertEqual(pick_open_deal([edge], now=NOW)["id"], 1)
        beyond = _deal(2, order_at=NOW - timedelta(seconds=MAX_DEAL_DISTANCE + 1))
        self.assertIsNone(pick_open_deal([beyond], now=NOW))

    def test_naive_now_is_refused(self):
        """Без часового пояса сдвиг на три часа меняет выбор между сделками
        одного дня — молча ошибиться здесь дороже, чем упасть."""
        with self.assertRaises(ValueError):
            pick_open_deal([_deal(1, order_at=NOW)], now=NOW.replace(tzinfo=None))

    def test_broken_pipeline_does_not_break_choice(self):
        """Мусор в поле воронки проход не роняет: сделка просто не подходит."""
        broken = {"id": 1, "pipeline_id": "abc", "status_id": STAGE_CONFIRMED,
                  "created_at": 500, "custom_fields_values": []}
        self.assertIsNone(pick_open_deal([broken], now=NOW))

    def test_nothing_suitable_gives_none(self):
        self.assertIsNone(pick_open_deal([], now=NOW))
        self.assertIsNone(pick_open_deal([_deal(1, pipeline=OTHER_PIPELINE)], now=NOW))


class DealAddressTests(unittest.TestCase):
    def test_address_is_stripped(self):
        deal = _deal(1, address="  Менделеева д 15а, кв 99  ")
        self.assertEqual(address_of_deal(deal), "Менделеева д 15а, кв 99")

    def test_empty_field_gives_none(self):
        self.assertIsNone(address_of_deal(_deal(1, address="")))
        self.assertIsNone(address_of_deal(_deal(1)))

    def test_missing_deal_gives_none(self):
        self.assertIsNone(address_of_deal(None))


if __name__ == "__main__":
    unittest.main()
