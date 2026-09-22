"""Общий счётчик ожидания (`adminbot/sync/waiting.py`): секунды с отметки шага.

Задача 2 ТЗ `docs/plans/2026-09-22-order-chain.md`: таймер ожидания автосделки
должен считать от отметки шага `move_primary_success`, а не от `updated_at`,
который сам шаг `wait_salesbot` двигает каждый проход.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from adminbot.sync.waiting import waited_since

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=MSK)


def test_no_mark_returns_none():
    assert waited_since({}, "move_primary_success", NOW) is None
    assert waited_since(None, "move_primary_success", NOW) is None


def test_counts_from_iso_string_with_zone():
    """Так кладёт база: `checklist[шаг] = to_jsonb(now())` (`db.py:1076`)."""
    stamp = (NOW - timedelta(minutes=20)).isoformat()
    waited = waited_since({"move_primary_success": stamp}, "move_primary_success", NOW)
    assert waited == 1200.0


def test_counts_from_datetime_object():
    """На случай, если где-то положат datetime напрямую, а не строку."""
    moment = NOW - timedelta(minutes=20)
    waited = waited_since({"move_primary_success": moment}, "move_primary_success", NOW)
    assert waited == 1200.0


def test_naive_stamp_is_treated_as_moscow_time():
    stamp = (NOW - timedelta(minutes=20)).replace(tzinfo=None).isoformat()
    waited = waited_since({"move_primary_success": stamp}, "move_primary_success", NOW)
    assert waited == 1200.0


def test_other_steps_do_not_affect_the_target_step():
    checklist = {"fill_primary": (NOW - timedelta(minutes=5)).isoformat()}
    assert waited_since(checklist, "move_primary_success", NOW) is None
