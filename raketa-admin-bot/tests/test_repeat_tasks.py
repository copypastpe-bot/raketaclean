"""Чистка задач «Повторный заказ»: закрываются только просроченные больше недели.

Перепроверка типа и «не выполнена» — на случай, если фильтр амо пропустит
лишнее: закрыть чужую задачу хуже, чем оставить свою.
"""

from datetime import datetime, timezone

from adminbot.amo import ids
from adminbot.amo.repeat_tasks import fetch_open_repeat_tasks, select_overdue

NOW = int(datetime(2026, 10, 29, 12, tzinfo=timezone.utc).timestamp())
DAY = 86400


def a_task(task_id: int, days_overdue: float, **extra) -> dict:
    task = {"id": task_id, "task_type_id": ids.TASK_TYPE_FEEDBACK, "is_completed": False,
            "complete_till": int(NOW - days_overdue * DAY), "entity_type": "leads",
            "entity_id": 1000 + task_id}
    task.update(extra)
    return task


def test_only_older_than_a_week():
    tasks = [a_task(1, 8), a_task(2, 6.9), a_task(3, -1), a_task(4, 7.01)]
    assert [t["id"] for t in select_overdue(tasks, NOW)] == [1, 4]


def test_oldest_first():
    tasks = [a_task(1, 10), a_task(2, 300), a_task(3, 30)]
    assert [t["id"] for t in select_overdue(tasks, NOW)] == [2, 3, 1]


def test_other_type_completed_or_no_deadline_untouched():
    tasks = [a_task(1, 30, task_type_id=2270716),
             a_task(2, 30, is_completed=True),
             a_task(3, 30, complete_till=None),
             a_task(4, 30)]
    assert [t["id"] for t in select_overdue(tasks, NOW)] == [4]


async def test_asks_open_tasks_of_the_type():
    class Fake:
        calls = []

        async def get_all(self, path, key, params=None):
            self.calls.append((path, key, list(params)))
            return []

    amo = Fake()
    await fetch_open_repeat_tasks(amo)
    path, key, params = amo.calls[0]
    assert (path, key) == ("/api/v4/tasks", "tasks")
    assert ("filter[task_type][]", ids.TASK_TYPE_FEEDBACK) in params
    assert ("filter[is_completed]", 0) in params
