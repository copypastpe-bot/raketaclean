"""Тексты цикла «Повторный заказ» (ТЗ 2026-09-28, задача 3).

Только строители текста: вход — данные, выход — строка. Ни сети, ни базы.
Телефонов в этом цикле нет вовсе — робот ведёт разговор в сделке, письма
владельцу тоже без них.

Работа в текстах названа по виду (ТЗ 2026-09-30, задача 4): «заказ №N» —
химчистка, «уборка №N» — уборка. «Повторный заказ» — имя задачи в CRM, не работы,
оно не меняется.
"""

from __future__ import annotations

from typing import Sequence

from adminbot.amo.fields import MOSCOW_TZ
from adminbot.feedback.models import KIND_CLEANING, KIND_ORDER, RatedOrder
from adminbot.tg.cards import mark_rehearsal

# Имя работы по виду: именительный, винительный, дательный падеж.
_WORK_NAMES = {
    KIND_ORDER: ("заказ", "заказ", "заказу"),
    KIND_CLEANING: ("уборка", "уборку", "уборке"),
}


def work_title(kind: str) -> str:
    """«Заказ» / «Уборка» — с заглавной, для начала строки журнала."""
    return _WORK_NAMES[kind][0].capitalize()


def _nominative(kind: str) -> str:
    return _WORK_NAMES[kind][0]


def _accusative(kind: str) -> str:
    return _WORK_NAMES[kind][1]


def _dative(kind: str) -> str:
    return _WORK_NAMES[kind][2]

# Третья строка комментария — по положению задачи «Повторный заказ» на момент
# постановки «Связаться» (принято координатором при написании ТЗ 28.09).
_FEEDBACK_TASK_LINES = {
    "closed_now": "Поставил задачу «Связаться», «Повторный заказ» закрыл.",
    "closed_by_hand": "Поставил задачу «Связаться»; «Повторный заказ» уже был закрыт.",
    "not_yet": "Поставил задачу «Связаться»; «Повторный заказ» закрою, когда он появится.",
}


def _replied_when(order: RatedOrder) -> str:
    return order.replied_at.astimezone(MOSCOW_TZ).strftime("%d.%m %H:%M")


def _answer(order: RatedOrder) -> str:
    return (order.comment or "").strip() or str(order.score)


def _deal_url(base_url: str, lead_id: int) -> str:
    return f"{base_url.rstrip('/')}/leads/detail/{lead_id}"


def note_text(order: RatedOrder, *, feedback_task: str) -> str:
    """Комментарий в сделку при оценке 1–4 (решение владельца 9)."""
    return "\n".join([
        f"🤖 Клиент оценил {_accusative(order.kind)} №{order.order_id} на {order.score} в боте "
        f"({_replied_when(order)}).",
        f"Ответ клиента: «{_answer(order)}».",
        _FEEDBACK_TASK_LINES[feedback_task],
    ])


def contact_task_text(order: RatedOrder) -> str:
    """Текст задачи «Связаться» (решение владельца 9)."""
    return (f"Клиент оценил {_accusative(order.kind)} №{order.order_id} на {order.score} "
            "— узнать, что не так.")


def feedback_result_text(score: int, *, kind: str = KIND_ORDER) -> str:
    """Текст, которым закрывается «Повторный заказ» (решение владельца 9)."""
    work = _accusative(kind)
    if score == 5:
        return f"🤖 Клиент оценил {work} в боте на 5."
    return f"🤖 Клиент оценил {work} в боте на {score}, поставлена задача «Связаться»."


def rehearsal_text(order: RatedOrder, actions: Sequence[str], *, base_url: str) -> str:
    """Репетиция: что робот сделал бы по заказу, в CRM не написав ничего."""
    lines = [f"Репетиция: {_nominative(order.kind)} №{order.order_id}, оценка {order.score}."]
    lines += [f"— {action}" for action in actions]
    lines.append(_deal_url(base_url, order.lead_id))
    return mark_rehearsal("\n".join(lines), True)


def failure_text(order: RatedOrder, error: str, *, base_url: str) -> str:
    """Три сбоя подряд по заказу — владелец разбирается руками."""
    lines = [
        f"⚠️ Не смог обработать оценку {order.score} по {_dative(order.kind)} "
        f"№{order.order_id}, сделай руками.",
        f"Ошибка: {error}",
        _deal_url(base_url, order.lead_id),
    ]
    return "\n".join(lines)
