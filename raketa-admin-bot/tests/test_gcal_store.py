"""Память робота о записях календаря.

Ключ — идентификатор записи в Google: он вечный и не меняется при правках.
Благодаря этому повторный обмен (а он бывает при каждом протухании закладки)
не заводит вторую сделку по той же записи.

Отдельно проверяется закладка обмена: в репетиции она НЕ должна попадать в базу.
Иначе боевой запуск начнёт с «изменений нет» и пропустит всё, что было, — ровно
та ошибка, что случилась с письмами партнёра 2026-08-26.
"""

from datetime import date

import pytest

from adminbot.gcal.store import MemoryCalendarStore


@pytest.fixture
def store():
    return MemoryCalendarStore()


async def test_event_is_remembered_once(store):
    first = await store.create("evt-1", kind="order", phone10="9601861067",
                               order_date=date(2026, 8, 27))
    again = await store.create("evt-1", kind="order", phone10="9601861067",
                               order_date=date(2026, 8, 27))

    assert first.event_id == again.event_id == "evt-1"
    assert again.status == "new"
    assert len(store.links) == 1                 # повторный обмен не задвоил запись


async def test_progress_is_saved_step_by_step(store):
    await store.create("evt-1", kind="order", phone10="9601861067")

    await store.update("evt-1", status="in_progress", primary_lead_id=555)
    await store.mark_step("evt-1", "fill_primary")
    link = await store.get("evt-1")

    assert link.status == "in_progress"
    assert link.primary_lead_id == 555
    assert "fill_primary" in link.checklist      # шаг зафиксирован, повтор его не сделает


async def test_lead_taken_by_another_calendar_event_is_not_reused(store):
    """Одна сделка не может обслуживать две разные записи календаря."""
    await store.create("evt-1", kind="order", phone10="9601861067")
    await store.update("evt-1", real_lead_id=777)
    await store.create("evt-2", kind="order", phone10="9601861067")

    taken = await store.taken_leads("9601861067", exclude_event_id="evt-2")

    assert taken == {777}
    assert await store.taken_leads("9601861067", exclude_event_id="evt-1") == set()


async def test_unfinished_events_come_back(store):
    """Ожидание автосделки переживает перезапуск: запись возьмут из хранилища."""
    await store.create("evt-1", kind="order", phone10="9601861067")
    await store.update("evt-1", status="waiting_salesbot")
    await store.create("evt-2", kind="order", phone10="9605379757")
    await store.update("evt-2", status="done")

    pending = await store.pending()

    assert [link.event_id for link in pending] == ["evt-1"]


async def test_bookmark_is_kept(store):
    assert await store.cursor() == (None, None)

    await store.save_cursor("TOKEN-1", sync_from=date(2026, 8, 27))

    assert await store.cursor() == ("TOKEN-1", date(2026, 8, 27))


async def test_actions_are_logged_with_rehearsal_flag(store):
    await store.create("evt-1", kind="order", phone10="9601861067")

    await store.log("evt-1", "update_lead", dry_run=True, entity="lead", amo_id=555,
                    payload={"price": 0})

    assert store.actions[0]["dry_run"] is True
    assert store.actions[0]["action"] == "update_lead"


async def test_skipped_events_keep_their_reason(store):
    """Пропущенное объясняется: вечером владелец увидит, чего робот не тронул."""
    await store.create("evt-3", kind="skip", phone10=None)
    await store.update("evt-3", status="skipped", skip_reason="телефон не найден")

    link = await store.get("evt-3")

    assert link.status == "skipped"
    assert link.skip_reason == "телефон не найден"
    assert link not in await store.pending()     # пропущенное больше не трогаем
