"""Задачи «Повторный заказ», по которым клиент так и не поставил оценку.

Амо ставит «Повторный заказ» (тип 2270746) при переводе сделки в «Успешно
реализовано», робот закрывает её, когда клиент оценил заказ в боте (ТЗ
2026-09-28). Без оценки задача висит вечно. Решение владельца 2026-10-29:
задачи, срок которых истёк больше недели назад, закрыть.

С роботом это не сталкивается: поставит клиент оценку 1–4 позже — робот всё
равно напишет комментарий и поставит «Связаться» (решение 6 от 28.09).

Здесь отбор; закрывает `scripts/close_repeat_tasks.py`.
"""

from __future__ import annotations

from typing import Any, Sequence

from adminbot.amo import ids

REPEAT_TASK_DAYS = 7


async def fetch_open_repeat_tasks(amo: Any) -> list[dict]:
    """Все открытые «Повторные заказы» аккаунта. Фильтра по сроку у амо нет."""
    params: list[tuple[str, Any]] = [
        ("filter[task_type][]", ids.TASK_TYPE_FEEDBACK),
        ("filter[is_completed]", 0),
        ("order[complete_till]", "asc"),
    ]
    return await amo.get_all("/api/v4/tasks", "tasks", params=params)


def select_overdue(tasks: Sequence[dict], now_ts: int, *,
                   older_than_days: int = REPEAT_TASK_DAYS) -> list[dict]:
    """Задачи, срок которых истёк больше N дней назад, от самых старых.

    Перепроверяем тип и «не выполнена» сами, а не верим фильтру на слово:
    закрыть чужую задачу хуже, чем оставить свою. Нет срока — не трогаем.
    """
    border = now_ts - older_than_days * 86400
    picked: list[tuple[int, dict]] = []
    for task in tasks:
        if task.get("is_completed"):
            continue
        if int(task.get("task_type_id") or 0) != ids.TASK_TYPE_FEEDBACK:
            continue
        try:
            till = int(task.get("complete_till"))
        except (TypeError, ValueError):
            continue
        if till < border:
            picked.append((till, task))
    return [task for _till, task in sorted(picked, key=lambda pair: pair[0])]
