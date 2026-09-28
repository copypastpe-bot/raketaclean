"""Письма владельцу по откликам на промо (ТЗ 2026-09-23, задача 5; «завёл сделку» — 28.09).

Только строители текста: вход — данные, выход — строка. Телефон — целиком
(`phone.for_owner`): бот личный, владельцу номер нужен, чтобы позвонить самому
(решение владельца 2026-08-26). Маска — только в логах.
"""

from __future__ import annotations

from typing import Any, Optional

from adminbot.amo.fields import MOSCOW_TZ
from adminbot.phone import for_owner
from adminbot.promo_callback.sync import deal_name
from adminbot.tg.cards import mark_rehearsal


def _deal_url(base_url: str, lead_id: int) -> str:
    return f"{base_url.rstrip('/')}/leads/detail/{lead_id}"


def rehearsal_text(callback: Any, contact_id: Optional[int]) -> str:
    """Репетиция: что робот сделал бы по заявке, в CRM не написав ничего."""
    contact = (f"контакт найден №{contact_id}" if contact_id is not None
               else "контакт будет заведён")
    return (f"Репетиция: завёл бы сделку «{deal_name(callback)}», {contact}, "
            "поставил бы в автозвонок.")


def done_text(callback: Any, lead_id: int, *, base_url: str) -> str:
    """Бой: сделка по отклику заведена и примечание записано (решение владельца 28.09).

    Одно сообщение на заявку, по образцу отчёта автозвонка: что сделал робот,
    кто ответил, ссылка на сделку. Звонит дальше автозвонок, владельцу —
    только знать.
    """
    name = (getattr(callback, "name", None) or "").strip() or "без имени"
    who = "Клиент" if callback.source == "client" else "Лид"
    answer = (getattr(callback, "response_text", None) or "").strip() or "1"
    created_at = getattr(callback, "created_at", None)
    when = (" " + created_at.astimezone(MOSCOW_TZ).strftime("%d.%m %H:%M")
            if created_at is not None else "")
    return "\n".join([
        "Отклик на промо: завёл сделку, автозвонок подхватит.",
        f"{name} · {for_owner(callback.phone)}",
        f"{who} ответил «{answer}»{when}",
        _deal_url(base_url, lead_id),
    ])


def failure_text(callback: Any, *, error: str, lead_id: Optional[int] = None,
                 base_url: str = "", dry_run: bool = False) -> str:
    """Сделку по отклику завести не вышло — владелец звонит сам.

    Если сделка уже заведена, а сорвалось только примечание, звонить руками не
    нужно: сделка с тегом стоит в «Новом лиде», автозвонок её возьмёт.
    """
    name = (getattr(callback, "name", None) or "").strip() or "без имени"
    if lead_id is None:
        head = "⚠️ Не смог завести сделку по отклику на промо, позвоните руками."
    else:
        head = ("⚠️ Сделку по отклику на промо завёл, но примечание не записалось — "
                "автозвонок её возьмёт.")
    lines = [head, f"{name} · {for_owner(callback.phone)}", f"Ошибка: {error}"]
    if lead_id is not None and base_url:
        lines += ["", _deal_url(base_url, lead_id)]
    return mark_rehearsal("\n".join(lines), dry_run)
