"""Тесты двойника FakeAmo (tests/fakes.py): задачи сделки по типу и постановка задачи.

Задача 1 ТЗ 2026-09-28 — опора для цикла «Повторный заказ» (задача 3), который
будет использовать этот двойник в тестах движка. Здесь проверяется сам двойник,
без движка.
"""

import pytest

from adminbot.amo import ids
from adminbot.amo.client import AmoError
from tests.fakes import FakeAmo


async def test_get_lead_tasks_of_type_includes_closed_and_excludes_other_type():
    amo = FakeAmo()
    amo.add_task(500, 1, ids.TASK_TYPE_FEEDBACK)
    amo.add_task(500, 2, ids.TASK_TYPE_CONTACT)          # другой тип — не должен попасть
    await amo.complete_task(1)                            # закрываем задачу 1

    tasks = await amo.get_lead_tasks_of_type(500, ids.TASK_TYPE_FEEDBACK)

    assert [t["id"] for t in tasks] == [1]
    assert tasks[0]["is_completed"] is True                # закрытая видна с этим флагом
    assert ("get_lead_tasks_of_type", (500, ids.TASK_TYPE_FEEDBACK)) in amo.calls


async def test_get_lead_tasks_of_type_empty_for_unknown_lead():
    amo = FakeAmo()
    assert await amo.get_lead_tasks_of_type(999, ids.TASK_TYPE_FEEDBACK) == []


async def test_create_task_records_call_and_adds_task():
    amo = FakeAmo()

    intent = await amo.create_task(500, task_type_id=ids.TASK_TYPE_CONTACT,
                                   text="Позвонить", complete_till=1756500000,
                                   responsible_user_id=951507)

    assert intent.entity_id is not None
    assert intent.performed is True
    stored = amo.tasks[500]
    assert len(stored) == 1
    assert stored[0]["id"] == intent.entity_id
    assert stored[0]["task_type_id"] == ids.TASK_TYPE_CONTACT
    assert stored[0]["is_completed"] is False
    calls = amo.calls_of("create_task")
    assert calls and calls[0]["task_type_id"] == ids.TASK_TYPE_CONTACT
    assert calls[0]["responsible_user_id"] == 951507


async def test_create_task_dry_run_does_not_add_task():
    amo = FakeAmo(dry_run=True)

    intent = await amo.create_task(500, task_type_id=ids.TASK_TYPE_CONTACT,
                                   text="Позвонить", complete_till=1756500000)

    assert intent.performed is False
    assert intent.entity_id is None
    assert amo.tasks.get(500) is None                     # репетиция ничего не заводит


async def test_create_task_respects_fail_on():
    amo = FakeAmo()
    amo.fail_on = "create_task"

    with pytest.raises(AmoError):
        await amo.create_task(500, task_type_id=ids.TASK_TYPE_CONTACT,
                              text="Позвонить", complete_till=1756500000)
