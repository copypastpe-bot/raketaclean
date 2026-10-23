"""Примечание в сделку по итогу цепочки автозвонка (adminbot.autocall.notes).

Проверяется, что каждый финал цепочки получает свой текст, что время звонка
печатается по Москве и пропускается, когда его нет, и что незнакомый финал
падает ошибкой, а не пишет в CRM пустоту.
"""

from datetime import datetime, timezone

import pytest

from adminbot.amo.fields import MOSCOW_TZ
from adminbot.autocall.notes import deal_note_text

# Время команды АТС в примечании печатается по Москве.
CALLED_AT = datetime(2026, 9, 2, 18, 8, tzinfo=MOSCOW_TZ)

# --- deal_note_text: след робота в самой сделке ---
#
# Связка АТС с амо знает только внутренний номер 100, а робот теперь звонит
# менеджеру на мобильный — поэтому звонок в карточку сделки не попадает
# (проверено на живой заявке 2026-09-02: в АТС вызов есть, в амо нет).
# Робот пишет след сам: одно примечание на завершённую цепочку.

def test_deal_note_connected_names_time_and_where_the_record_is():
    """Соединились: в примечании время звонка и куда идти за записью."""
    text = deal_note_text("done", called_at=CALLED_AT)

    assert "соединил" in text.lower()
    assert "18:08" in text
    assert "АТС" in text


def test_deal_note_client_no_contact():
    """Клиент дважды не ответил — сделка ушла на этап, в примечании причина."""
    text = deal_note_text("no_contact", called_at=CALLED_AT)

    assert "клиент" in text.lower()


def test_deal_note_manager_unreachable():
    """Менеджер не взял ни один телефон — робот остановился."""
    text = deal_note_text("gave_up", called_at=CALLED_AT, reason="manager_unreachable")

    assert "менеджер" in text.lower()


def test_deal_note_attempts_exhausted():
    """Предохранитель: попытки кончились."""
    text = deal_note_text("gave_up", called_at=CALLED_AT, reason="attempts_exhausted")

    assert "попытк" in text.lower()


def test_deal_note_without_call_time_says_nothing_about_it():
    """Времени звонка может не быть (сбой между шагами) — строку просто пропускаем."""
    text = deal_note_text("done", called_at=None)

    assert "None" not in text


def test_deal_note_cancelled_by_manager_button_is_verbatim():
    """ТЗ 2026-10-23: менеджер нажал «Я уже перезвонил» — робот остановился."""
    text = deal_note_text("cancelled", reason="manager_called")

    assert text == ("Автозвонок: менеджер перезвонил клиенту сам (кнопка). "
                    "Робот остановился.")


def test_deal_note_cancelled_by_pbx_history_names_moscow_time():
    """ТЗ 2026-10-23: в истории АТС уже был звонок с клиентом — время по Москве."""
    contact_at = datetime(2026, 10, 23, 7, 9, tzinfo=timezone.utc)   # 10:09 по Москве
    text = deal_note_text("cancelled", reason="client_called", contact_at=contact_at)

    assert text == ("Автозвонок: с клиентом уже был звонок через АТС (10:09 МСК) — "
                    "клиент перезвонил сам или звонили из офиса. Робот остановился.")


def test_deal_note_client_called_needs_contact_time():
    """Без времени звонка примечание «был звонок через АТС» писать нельзя."""
    with pytest.raises(ValueError):
        deal_note_text("cancelled", reason="client_called")


def test_deal_note_unknown_status_raises():
    """Незнакомый финал — ошибка здесь, а не молчаливая пустота в CRM."""
    with pytest.raises(ValueError):
        deal_note_text("calling", called_at=CALLED_AT)
