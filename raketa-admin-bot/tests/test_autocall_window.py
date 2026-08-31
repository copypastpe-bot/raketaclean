"""Окно звонков 10:00–20:00 МСК: заявка ночью ждёт утра, повторы тоже."""

from datetime import datetime, timedelta, timezone

import pytest

from adminbot.amo.fields import MOSCOW_TZ
from adminbot.autocall.window import next_call_moment, validate_window


def msk(*args):
    return datetime(*args, tzinfo=MOSCOW_TZ)


# --- next_call_moment: базовые случаи из плана ---

def test_daytime_calls_now():
    now = msk(2026, 8, 31, 14, 30)
    assert next_call_moment(now, now=now) == now


def test_late_evening_waits_for_tomorrow_morning():
    now = msk(2026, 8, 31, 21, 30)
    assert next_call_moment(now, now=now) == msk(2026, 9, 1, 10, 0)


def test_night_waits_for_today_morning():
    now = msk(2026, 8, 31, 2, 0)
    assert next_call_moment(now, now=now) == msk(2026, 8, 31, 10, 0)


def test_one_minute_before_open_waits_for_today_morning():
    now = msk(2026, 8, 31, 9, 59)
    assert next_call_moment(now, now=now) == msk(2026, 8, 31, 10, 0)


def test_exactly_at_open_calls_now():
    # Граница from_hour ВХОДИТ в окно: ровно в 10:00:00 звонить уже можно
    # (симметрия с закрытием — граница to_hour в окно не входит).
    now = msk(2026, 8, 31, 10, 0)
    assert next_call_moment(now, now=now) == now


def test_exactly_at_close_waits_for_tomorrow():
    # Граница to_hour НЕ входит в окно: ровно в 20:00 звонить уже поздно.
    now = msk(2026, 8, 31, 20, 0)
    assert next_call_moment(now, now=now) == msk(2026, 9, 1, 10, 0)


def test_retry_near_close_respects_window():
    # Повтор, назначенный в 19:58 + 10 минут: желаемый момент 20:08 → завтра.
    now = msk(2026, 8, 31, 19, 58)
    retry_at = now + timedelta(minutes=10)
    assert next_call_moment(retry_at, now=now) == msk(2026, 9, 1, 10, 0)


# --- желаемый момент в прошлом (робот был выключен) ---

def test_desired_moment_in_past_counts_from_now():
    created = msk(2026, 8, 30, 15, 0)          # вчера днём
    now = msk(2026, 8, 31, 12, 0)              # робот включился сегодня в окне
    assert next_call_moment(created, now=now) == now


def test_desired_moment_in_past_and_now_outside_window():
    created = msk(2026, 8, 30, 15, 0)          # вчера днём
    now = msk(2026, 8, 31, 21, 0)              # робот включился вечером
    assert next_call_moment(created, now=now) == msk(2026, 9, 1, 10, 0)


# --- часовые пояса ---

def test_utc_input_is_converted_to_moscow():
    # 18:30 UTC = 21:30 МСК → завтра в 10:00 МСК.
    now_utc = datetime(2026, 8, 31, 18, 30, tzinfo=timezone.utc)
    result = next_call_moment(now_utc, now=now_utc)
    assert result == msk(2026, 9, 1, 10, 0)
    assert result.tzinfo is MOSCOW_TZ


def test_result_is_aware_in_moscow_tz():
    now = datetime(2026, 8, 31, 11, 0, tzinfo=timezone.utc)  # 14:00 МСК, в окне
    result = next_call_moment(now, now=now)
    assert result.tzinfo is MOSCOW_TZ
    assert result == now


def test_naive_created_at_rejected():
    now = msk(2026, 8, 31, 14, 0)
    with pytest.raises(ValueError):
        next_call_moment(datetime(2026, 8, 31, 14, 0), now=now)


def test_naive_now_rejected():
    created = msk(2026, 8, 31, 14, 0)
    with pytest.raises(ValueError):
        next_call_moment(created, now=datetime(2026, 8, 31, 14, 0))


# --- своё окно через параметры ---

def test_custom_window_hours_are_respected():
    now = msk(2026, 8, 31, 11, 30)
    assert next_call_moment(now, now=now, from_hour=12, to_hour=18) == \
        msk(2026, 8, 31, 12, 0)
    at_close = msk(2026, 8, 31, 18, 0)
    assert next_call_moment(at_close, now=at_close, from_hour=12, to_hour=18) == \
        msk(2026, 9, 1, 12, 0)


# --- validate_window ---

def test_validate_window_accepts_default():
    validate_window(10, 20)  # не кидает


def test_validate_window_rejects_hour_above_23():
    with pytest.raises(RuntimeError):
        validate_window(10, 24)


def test_validate_window_rejects_negative_hour():
    with pytest.raises(RuntimeError):
        validate_window(-1, 20)


def test_validate_window_rejects_from_not_before_to():
    with pytest.raises(RuntimeError):
        validate_window(20, 10)
    with pytest.raises(RuntimeError):
        validate_window(10, 10)
