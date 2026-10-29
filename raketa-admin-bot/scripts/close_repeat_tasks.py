"""Закрыть задачи «Повторный заказ» с истёкшим больше недели назад сроком.

Решение владельца 2026-10-29. Что отбирается — `adminbot/amo/repeat_tasks.py`.
По умолчанию — только показать.

Запуск на сервере:

    sudo raketa-admin-bot-update --close-repeat-tasks         показать, что закроется
    sudo raketa-admin-bot-update --close-repeat-tasks-live    закрыть по-настоящему

Предел за один запуск — 300 задач, поднимается ключом
`--close-repeat-tasks-limit=N`: старые задачи могли копиться годами, и такую
кучу владелец должен увидеть до того, как она закроется.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from adminbot.amo.client import AmoClient, AmoError
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.amo.repeat_tasks import (
    REPEAT_TASK_DAYS, fetch_open_repeat_tasks, select_overdue)
from adminbot.config import Settings

SAFETY_LIMIT = 300

RESULT = "🤖 Закрыто при чистке CRM {today}: оценки клиента не было."


async def main() -> int:
    live = os.environ.get("CLOSE_REPEAT_TASKS_LIVE", "").strip() in ("1", "true")
    limit = int(os.environ.get("CLOSE_REPEAT_TASKS_LIMIT", "") or SAFETY_LIMIT)
    settings = Settings.from_env()
    amo = AmoClient(base_url=settings.amo_base_url, token=settings.amo_token,
                    dry_run=not live)
    now = datetime.now(timezone.utc)
    today = now.astimezone(MOSCOW_TZ).date()
    print("Режим: " + ("БОЕВОЙ — задачи будут закрыты" if live
                       else "ПРОСМОТР — в CRM ничего не меняется"))
    print(f"Порог: срок истёк больше {REPEAT_TASK_DAYS} дней назад\n")

    try:
        tasks = await fetch_open_repeat_tasks(amo)
        overdue = select_overdue(tasks, int(now.timestamp()))
        print(f"Открытых «Повторных заказов»: {len(tasks)}, из них со сроком "
              f"старше {REPEAT_TASK_DAYS} дней: {len(overdue)}\n")
        if not overdue:
            return 0

        _print_plan(overdue, base_url=settings.amo_base_url)

        if len(overdue) > limit:
            print(f"\nОстановился: под правило попало {len(overdue)} задач, "
                  f"а предел — {limit}. Ничего не закрыто. Если это ожидаемо, "
                  f"поднимите предел ключом --close-repeat-tasks-limit=N.")
            return 2

        if not live:
            print("\nЭто был просмотр. Закрыть по-настоящему: "
                  "sudo raketa-admin-bot-update --close-repeat-tasks-live")
            return 0

        closed, failed = await _close(amo, overdue, f"{today:%d.%m}")
        print(f"\nЗакрыто: {closed}. Не удалось: {failed}.")
        return 0 if not failed else 1
    finally:
        await amo.close()


def _deadline(task: dict) -> str:
    till = datetime.fromtimestamp(int(task["complete_till"]), tz=MOSCOW_TZ)
    return f"{till:%d.%m.%Y}"


def _print_plan(tasks: list[dict], *, base_url: str) -> None:
    by_month = Counter(_deadline(task)[3:] for task in tasks)
    print("По месяцу срока:")
    for month, count in sorted(by_month.items(),
                               key=lambda pair: (pair[0][3:], pair[0][:2])):
        print(f"  {month}: {count}")

    print("\nСписок (от самых старых):")
    for task in tasks:
        entity = (f"{base_url}/leads/detail/{task.get('entity_id')}"
                  if task.get("entity_type") == "leads" and task.get("entity_id")
                  else f"{task.get('entity_type') or '—'} #{task.get('entity_id') or '—'}")
        print(f"  задача #{task.get('id')} · срок {_deadline(task)} · {entity}")


async def _close(amo: Any, tasks: list[dict], today: str) -> tuple[int, int]:
    """Закрыть отобранные задачи. Одна ошибка не останавливает остальные."""
    closed = 0
    failed = 0
    for task in tasks:
        task_id = int(task.get("id") or 0)
        try:
            await amo.complete_task(task_id, RESULT.format(today=today))
            closed += 1
            print(f"  закрыта задача #{task_id}")
        except AmoError as exc:
            failed += 1
            print(f"  НЕ закрыта задача #{task_id}: {exc}")
    return closed, failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
