"""Маскировка ПД в переходнике журнала (задача 5 ТЗ 2026-09-18).

Чистые функции — без базы, без asyncio.
"""

from __future__ import annotations

from notifyd.redact import mask_addresses, mask_phones, redact


# --------------------------------------------------------------------------
# Телефон
# --------------------------------------------------------------------------

def test_phone_plus7_is_masked_to_last4():
    text = mask_phones("клиент +79991234567 просит перезвонить")
    assert "9991234567" not in text
    assert "…4567" in text


def test_phone_bare_8_is_masked():
    text = mask_phones("контакт 89991234567 не отвечает")
    assert "89991234567" not in text
    assert "…4567" in text


def test_phone_without_country_code_is_masked():
    """9XXXXXXXXX (10 цифр, без 7/8) — тот же формат, что понимает
    bot.py:normalize_phone_for_db."""
    text = mask_phones("номер 9991234567 записан неверно")
    assert "9991234567" not in text
    assert "…4567" in text


def test_phone_with_separators_is_masked():
    text = mask_phones("звонили с +7 (999) 123-45-67")
    assert "999" not in text
    assert "…4567" in text


def test_webhook_payload_dump_phone_is_masked():
    """Подтверждённая находка разведки: notifications/outbox.py:626 кладёт
    в warning весь payload вебхука целиком — там может быть номер
    получателя. Здесь — как это выглядит после маскировки."""
    payload_like = ("Webhook payload missing message id. event=status "
                    "payload={'to': '79991234567', 'status': 'read'}")
    text = mask_phones(payload_like)
    assert "79991234567" not in text
    assert "…4567" in text


def test_short_numeric_id_is_not_touched():
    """ID сделки/заказа — не телефон, маскировать не нужно (и не должно:
    длина не совпадает ни с одной из двух форм)."""
    text = mask_phones("Сделка 82193718 не разобрана: timeout")
    assert "82193718" in text


def test_money_amount_with_dot_is_not_touched():
    text = mask_phones("сумма чека 79991234.50 руб.")
    assert "79991234.50" in text


def test_already_masked_phone_is_left_alone():
    text = mask_phones("клиент …4567 не отвечает")
    assert text == "клиент …4567 не отвечает"


# --------------------------------------------------------------------------
# Адрес
# --------------------------------------------------------------------------

def test_street_name_is_masked_marker_kept():
    text = mask_addresses("адрес заказа: ул. Ленина, зона уборки центр")
    assert "Ленина" not in text
    assert "ул. …" in text


def test_house_and_apartment_numbers_are_masked_marker_kept():
    text = mask_addresses("уточнили: д. 12, кв. 45")
    assert "12" not in text
    assert "45" not in text
    assert "д. …" in text
    assert "кв. …" in text


def test_text_without_address_markers_is_not_touched():
    text = mask_addresses("сделка 123 не разобрана: timeout")
    assert text == "сделка 123 не разобрана: timeout"


# --------------------------------------------------------------------------
# Адрес — расширенные формы (задача 2 ТЗ 2026-09-19, пункт 5 ревью)
# --------------------------------------------------------------------------

def test_house_number_after_street_with_comma_is_masked():
    """«ул. Ленина, 5» — номер дома шёл открытым (нет слова «д.»)."""
    text = mask_addresses("адрес: ул. Ленина, 5")
    assert "Ленина" not in text
    assert "5" not in text
    assert "ул. … …" in text


def test_house_number_after_street_without_comma_is_masked():
    """«ул. Ленина 5» — та же дыра без запятой."""
    text = mask_addresses("адрес: ул. Ленина 5")
    assert "Ленина" not in text
    assert "5" not in text
    assert "ул. … …" in text


def test_prospekt_short_form_pr_t_is_masked():
    """«пр-т» не было в списке маркеров — не маскировалось вовсе."""
    text = mask_addresses("едем на пр-т Мира 12")
    assert "Мира" not in text
    assert "12" not in text
    assert "пр-т" in text


def test_prospekt_short_form_with_dot_is_masked():
    text = mask_addresses("едем на пр. Победы 100")
    assert "Победы" not in text
    assert "100" not in text
    assert "пр." in text


def test_prospekt_full_word_is_still_masked():
    text = mask_addresses("едем на проспект Победы 100")
    assert "Победы" not in text
    assert "100" not in text
    assert "проспект" in text


def test_stroenie_number_is_masked_not_only_word():
    """«строение 5» — терялось только слово, номер оставался."""
    text = mask_addresses("уточнили строение 5 у диспетчера")
    assert "5" not in text
    assert "строение …" in text


def test_glued_house_marker_d5_is_masked():
    """«д5» слитно — не ловилось вовсе."""
    text = mask_addresses("уточнили: д5, всё верно")
    assert "д5" not in text
    assert "5" not in text
    assert "д…" in text


def test_glued_apartment_marker_kv12_is_masked():
    text = mask_addresses("отметили кв12 в заявке")
    assert "кв12" not in text
    assert "12" not in text
    assert "кв…" in text


def test_order_number_is_not_touched_by_address_mask():
    text = mask_addresses("заказ 581 не разобран")
    assert text == "заказ 581 не разобран"


def test_money_amount_is_not_touched_by_address_mask():
    text = mask_addresses("к оплате 1200 руб")
    assert text == "к оплате 1200 руб"


def test_time_is_not_touched_by_address_mask():
    text = mask_addresses("приедем к 12:30")
    assert text == "приедем к 12:30"


# --------------------------------------------------------------------------
# Комбинированная функция (то, чем пользуется адаптер)
# --------------------------------------------------------------------------

def test_redact_masks_both_phone_and_address_in_one_pass():
    text = redact("клиент +79991234567, ул. Мира, д. 5, кв. 10 просит перенести уборку")
    assert "9991234567" not in text
    assert "Мира" not in text
    assert "д. 5" not in text
    assert "кв. 10" not in text
    assert "…4567" in text
    assert "ул. …" in text
    assert "д. …" in text
    assert "кв. …" in text


def test_house_building_number_is_masked_in_full_word_form():
    """«корпус N» — та же природа дефекта, что у «строение N»: короткая форма
    маркера съедала хвост слова, а номер оставался открытым (найдено 19.09)."""
    assert mask_addresses("корпус 3") == "корпус …"
    assert mask_addresses("корп. 3") == "корп. …"
    assert mask_addresses("корп 3") == "корп …"
