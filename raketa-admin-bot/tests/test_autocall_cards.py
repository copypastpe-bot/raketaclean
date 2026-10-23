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
    connected_text, manager_text, no_phone_text, rehearsal_text, stuck_alert_text,
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
        "Не дозвонились до менеджера по заявке. Робот больше не "
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
        "Репетиция: позвонил бы сейчас менеджеру по заявке.\n"
        "Телефон клиента: +79001234567\n"
        f"{DEAL_URL}"
    )


def test_rehearsal_text_has_full_phone():
    """Полный телефон и здесь — владелец в репетиции проверяет заявку сам."""
    text = rehearsal_text(LEAD_ID, PHONE10, base_url=BASE_URL)

    assert "+79001234567" in text
    assert "…" not in text  # не маска


def test_rehearsal_text_without_phone_skips_the_line():
    """phone10=None (записи уже нет или телефон не сохранился) — строку
    телефона пропускаем, а не печатаем «телефон не распознан» на ровном
    месте (ревью Задачи 11)."""
    text = rehearsal_text(LEAD_ID, None, base_url=BASE_URL)

    assert text == (
        "Репетиция: позвонил бы сейчас менеджеру по заявке.\n"
        f"{DEAL_URL}"
    )
    assert "Телефон клиента" not in text
    assert "None" not in text


# --- connected_text ---

def test_connected_text_exact():
    text = connected_text(LEAD_ID, PHONE10, base_url=BASE_URL)

    assert text == (
        "Автозвонок: соединил менеджера с клиентом по заявке.\n"
        "Телефон клиента: +79001234567\n"
        f"{DEAL_URL}"
    )


def test_connected_text_has_full_phone():
    text = connected_text(LEAD_ID, PHONE10, base_url=BASE_URL)

    assert "+79001234567" in text
    assert "…" not in text  # не маска


def test_connected_text_without_phone_skips_the_line():
    """Тот же случай, что и у rehearsal_text: None не печатает заглушку."""
    text = connected_text(LEAD_ID, None, base_url=BASE_URL)

    assert text == (
        "Автозвонок: соединил менеджера с клиентом по заявке.\n"
        f"{DEAL_URL}"
    )
    assert "Телефон клиента" not in text
    assert "None" not in text


# --- no_phone_text ---

def test_no_phone_text_exact():
    text = no_phone_text(LEAD_ID, base_url=BASE_URL)

    assert text == (
        "Заявка без телефона — робот звонить не может, посмотрите сделку.\n"
        f"{DEAL_URL}"
    )


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


# --- сообщение менеджеру с кнопкой «Я уже перезвонил» (ТЗ 2026-10-23, задача 2) ---
#
# Одно сообщение на цепочку, дальше правится: пока впереди повтор — время
# повтора и кнопка, робот закончил — итог без кнопки. Кнопку обрабатывает
# рабочий бот (префикс AUTOCALL_CALLED_CB_PREFIX в корневом bot.py).

from datetime import datetime, timezone  # noqa: E402

from adminbot.amo.fields import MOSCOW_TZ  # noqa: E402
from adminbot.autocall.chain import CARD_KINDS  # noqa: E402
from adminbot.tg.autocall_cards import (  # noqa: E402
    AUTOCALL_CALLED_CB_PREFIX, called_keyboard, manager_card_text,
)

PHONE_LINE = "Телефон клиента: +79001234567"


def _msk(*args):
    return datetime(*args, tzinfo=MOSCOW_TZ)


def test_called_button_prefix_is_verbatim():
    """Префикс живёт в двух кодовых базах — у рабочего бота такой же
    (`AUTOCALL_CALLED_CB_PREFIX` в корневом bot.py). Разойдутся — нажатие
    никто не поймает."""
    assert AUTOCALL_CALLED_CB_PREFIX == "autocall_called:"


def test_called_keyboard_has_one_button_with_lead_id():
    keyboard = called_keyboard(LEAD_ID)

    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert len(buttons) == 1
    assert buttons[0].text == "Я уже перезвонил"
    assert buttons[0].callback_data == f"autocall_called:{LEAD_ID}"
    assert len(buttons[0].callback_data.encode()) <= 64     # предел Telegram


def test_retry_card_manager_missed_same_day():
    """Время повтора — московское; день тот же — без даты."""
    text = manager_card_text(
        "retry_manager", LEAD_ID, base_url=BASE_URL, phone10=PHONE10,
        retry_at=datetime(2026, 10, 23, 7, 12, tzinfo=timezone.utc),   # 10:12 МСК
        now=datetime(2026, 10, 23, 7, 7, tzinfo=timezone.utc))

    assert text == (
        "Робот не дозвонился до вас по заявке. Повтор в 10:12.\n"
        f"{PHONE_LINE}\n{DEAL_URL}"
    )


def test_retry_card_client_missed_same_day():
    text = manager_card_text(
        "retry_client", LEAD_ID, base_url=BASE_URL, phone10=PHONE10,
        retry_at=_msk(2026, 10, 23, 10, 22), now=_msk(2026, 10, 23, 10, 12))

    assert text == f"Клиент не взял трубку. Повтор в 10:22.\n{PHONE_LINE}\n{DEAL_URL}"


def test_retry_card_next_day_names_the_date():
    """Окно 10–20 перенесло повтор на завтра — дата в тексте обязательна."""
    text = manager_card_text(
        "retry_manager", LEAD_ID, base_url=BASE_URL, phone10=PHONE10,
        retry_at=_msk(2026, 10, 24, 10, 0), now=_msk(2026, 10, 23, 19, 58))

    assert text.splitlines()[0] == (
        "Робот не дозвонился до вас по заявке. Повтор 24.10 в 10:00.")


def test_final_card_connected():
    text = manager_card_text("done", LEAD_ID, base_url=BASE_URL, phone10=PHONE10,
                             at=_msk(2026, 10, 23, 10, 12))

    assert text == f"Робот соединил вас с клиентом в 10:12.\n{PHONE_LINE}\n{DEAL_URL}"


def test_final_card_manager_called():
    text = manager_card_text("manager_called", LEAD_ID, base_url=BASE_URL, phone10=PHONE10)

    assert text == (
        f"Вы перезвонили клиенту сами — робот остановился.\n{PHONE_LINE}\n{DEAL_URL}")


def test_final_card_client_called():
    text = manager_card_text("client_called", LEAD_ID, base_url=BASE_URL,
                             at=_msk(2026, 10, 23, 10, 9))

    assert text == (
        f"С клиентом уже был звонок через АТС в 10:09 — робот остановился.\n{DEAL_URL}")


@pytest.mark.parametrize("kind", ["no_contact_final", "manager_unreachable",
                                  "attempts_exhausted"])
def test_final_card_keeps_current_texts(kind):
    """Нынешние итоги не меняются (раздел «Чего не делать» ТЗ)."""
    assert manager_card_text(kind, LEAD_ID, base_url=BASE_URL, phone10=PHONE10) == \
        manager_text(kind, LEAD_ID, base_url=BASE_URL, phone10=PHONE10)


def test_every_card_kind_builds_text():
    moment = _msk(2026, 10, 23, 10, 12)
    for kind in CARD_KINDS:
        text = manager_card_text(kind, LEAD_ID, base_url=BASE_URL,
                                 retry_at=moment, at=moment, now=moment)
        assert DEAL_URL in text
        assert "None" not in text


@pytest.mark.parametrize("kind", ["client_retry_10", "не_бывает_такого"])
def test_card_unknown_kind_raises(kind):
    """client_retry_10 — отдельное прежнее сообщение, а не вид сообщения с кнопкой."""
    with pytest.raises(ValueError):
        manager_card_text(kind, LEAD_ID, base_url=BASE_URL)


@pytest.mark.parametrize("kind,missing", [
    ("retry_manager", "retry_at"), ("retry_client", "retry_at"),
    ("done", "at"), ("client_called", "at"),
])
def test_card_without_its_moment_raises(kind, missing):
    """Обещать «повтор в …» или «в 10:12» без времени нельзя — ошибка, а не пустота."""
    with pytest.raises(ValueError):
        manager_card_text(kind, LEAD_ID, base_url=BASE_URL)
