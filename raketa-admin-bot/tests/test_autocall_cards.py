"""Тексты сообщений автозвонка: менеджеру и владельцу.

Проверяется:

- **полный телефон в сообщении** — не последние 4 цифры, а номер целиком: бот
  личный, а получателю (менеджеру/владельцу) нужно дозвониться клиенту, не
  открывая CRM (решение владельца 2026-08-26, adminbot.phone.for_owner);
- phone10=None не печатает строку "None";
- незнакомый kind у manager_text — ValueError (движок не должен уметь
  отправить менеджеру неизвестный черновик текста).

Телефон в тестах вымышленный: +7 900 123-45-67 → phone10="9001234567".
"""

import pytest

from adminbot.autocall.chain import NOTIFY_KINDS
from adminbot.tg.autocall_cards import (
    connected_text, manager_text, rehearsal_text, stuck_alert_text,
)

BASE_URL = "https://example.amocrm.ru"
LEAD_ID = 41400777
PHONE10 = "9001234567"
DEAL_URL = f"{BASE_URL}/leads/detail/{LEAD_ID}"


# --- manager_text ---

def test_manager_text_client_retry_10():
    text = manager_text("client_retry_10", LEAD_ID, base_url=BASE_URL)

    assert text == f"Попытка звонка не удалась, повтор через 10 минут.\n{DEAL_URL}"


def test_manager_text_no_contact_final():
    text = manager_text("no_contact_final", LEAD_ID, base_url=BASE_URL)

    assert text == (
        "Клиент дважды не ответил. Сделка перенесена в «Не было 1-го "
        f"касания» — дальше вручную.\n{DEAL_URL}"
    )


def test_manager_text_manager_unreachable():
    text = manager_text("manager_unreachable", LEAD_ID, base_url=BASE_URL)

    assert text == (
        "Не дозвонились до менеджера по заявке с сайта. Робот больше не "
        f"звонит по ней.\n{DEAL_URL}"
    )


def test_manager_text_attempts_exhausted():
    text = manager_text("attempts_exhausted", LEAD_ID, base_url=BASE_URL)

    assert text == f"Попытки звонка по заявке исчерпаны (4). Робот остановился.\n{DEAL_URL}"


def test_manager_text_covers_every_notify_kind():
    """Каждый kind из chain.NOTIFY_KINDS должен уметь построить текст."""
    for kind in NOTIFY_KINDS:
        text = manager_text(kind, LEAD_ID, base_url=BASE_URL)
        assert DEAL_URL in text


def test_manager_text_unknown_kind_raises():
    with pytest.raises(ValueError):
        manager_text("не_бывает_такого", LEAD_ID, base_url=BASE_URL)


def test_manager_text_includes_full_phone_when_given():
    """Полный телефон, не маска: получателю нужно самому дозвониться клиенту
    (решение владельца 2026-08-26 — не восстанавливать маскировку здесь)."""
    text = manager_text("client_retry_10", LEAD_ID, base_url=BASE_URL, phone10=PHONE10)

    assert "+79001234567" in text
    assert "Телефон клиента: +79001234567" in text


def test_manager_text_without_phone_does_not_print_none():
    text = manager_text("client_retry_10", LEAD_ID, base_url=BASE_URL, phone10=None)

    assert "None" not in text
    assert "Телефон клиента" not in text


def test_manager_text_default_phone_is_none():
    text = manager_text("client_retry_10", LEAD_ID, base_url=BASE_URL)

    assert "None" not in text
    assert "Телефон клиента" not in text


# --- rehearsal_text ---

def test_rehearsal_text_exact():
    text = rehearsal_text(LEAD_ID, PHONE10, base_url=BASE_URL)

    assert text == (
        "Репетиция: позвонил бы сейчас менеджеру по заявке с сайта.\n"
        "Телефон клиента: +79001234567\n"
        f"{DEAL_URL}"
    )


def test_rehearsal_text_has_full_phone():
    """Полный телефон и здесь — владелец в репетиции проверяет заявку сам."""
    text = rehearsal_text(LEAD_ID, PHONE10, base_url=BASE_URL)

    assert "+79001234567" in text
    assert "…" not in text  # не маска


# --- connected_text ---

def test_connected_text_exact():
    text = connected_text(LEAD_ID, PHONE10, base_url=BASE_URL)

    assert text == (
        "Автозвонок: соединил менеджера с клиентом по заявке с сайта.\n"
        "Телефон клиента: +79001234567\n"
        f"{DEAL_URL}"
    )


def test_connected_text_has_full_phone():
    text = connected_text(LEAD_ID, PHONE10, base_url=BASE_URL)

    assert "+79001234567" in text
    assert "…" not in text  # не маска


# --- stuck_alert_text ---

def test_stuck_alert_text_exact():
    assert stuck_alert_text(30) == (
        "АТС не отвечает уже 30 мин — автозвонки стоят. "
        "Робот продолжает попытки."
    )


def test_stuck_alert_text_no_link():
    """Тревога про транспорт целиком, не про конкретную сделку — ссылки нет."""
    text = stuck_alert_text(45)

    assert "http" not in text
    assert "45" in text
