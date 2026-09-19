"""Сторож (задача 7 ТЗ 2026-09-18): пульс трёх ботов, опрос amoCRM, база,
прокси, рассыльщик клиентам. Инциденты (задача 8) сюда не входят — здесь
только то, что `Watchdog.check_once()` решает «сломано/работает».

Нужен настоящий Postgres — используется фикстура `watchdog_pool`
(conftest.py): схема `notify` плюс урезанная копия таблиц рабочего бота
(fixtures/bot_schema_min.sql), которые сторож только читает.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Optional

from notifyd.watchdog import (
    ADMIN_BOT_SERVICE_KEY,
    CLIENT_BOT_SERVICE_KEY,
    KEY_ADMIN_HEARTBEAT,
    KEY_AMOCRM_POLL,
    KEY_CLIENT_HEARTBEAT,
    KEY_DATABASE,
    KEY_DISPATCH,
    KEY_PROXY,
    KEY_WORKER_HEARTBEAT,
    NOTIFY_HEARTBEATS_TABLE,
    PUBLIC_HEARTBEATS_TABLE,
    WORKER_BOT_SERVICE_KEY,
    Watchdog,
)

from conftest import NOW


def _watchdog(pool, *, proxy_probe=None, **kwargs) -> Watchdog:
    async def _default_proxy_probe() -> bool:
        return True

    return Watchdog(pool=pool, proxy_probe=proxy_probe or _default_proxy_probe,
                    enabled=True, now=lambda: NOW, **kwargs)


def _result(results, key: str):
    return next(r for r in results if r.key == key)


async def _set_heartbeat(pool, *, table: str, service_key: str, last_seen_at,
                         display_name: str = "тест", status: str = "ok") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO {table} (service_key, display_name, status, last_seen_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (service_key) DO UPDATE
                SET display_name = EXCLUDED.display_name,
                    status = EXCLUDED.status,
                    last_seen_at = EXCLUDED.last_seen_at
            """,
            service_key, display_name, status, last_seen_at,
        )


async def _set_amocrm_poll(pool, *, updated_at, stream: str = "lead_events") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.amocrm_api_state (stream, cursor_created_at, updated_at)
            VALUES ($1, 0, $2)
            ON CONFLICT (stream) DO UPDATE SET updated_at = EXCLUDED.updated_at
            """,
            stream, updated_at,
        )


async def _insert_dispatch_row(pool, *, status: str, scheduled_at,
                               sent_at: Optional[object] = None) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO public.notification_outbox
                (event_key, recipient_kind, template, payload, status, scheduled_at, sent_at)
            VALUES ('test.event', 'client', 'test', '{}'::jsonb, $1, $2, $3)
            RETURNING id
            """,
            status, scheduled_at, sent_at,
        )


# --------------------------------------------------------------------------
# Пульс трёх ботов
# --------------------------------------------------------------------------

async def test_missing_heartbeats_are_all_broken(watchdog_pool):
    wd = _watchdog(watchdog_pool)
    results = await wd.check_once()
    for key in (KEY_WORKER_HEARTBEAT, KEY_CLIENT_HEARTBEAT, KEY_ADMIN_HEARTBEAT):
        result = _result(results, key)
        assert result.ok is False
        assert "нет вовсе" in result.detail


async def test_fresh_heartbeats_are_ok(watchdog_pool):
    await _set_heartbeat(watchdog_pool, table=PUBLIC_HEARTBEATS_TABLE,
                        service_key=WORKER_BOT_SERVICE_KEY, last_seen_at=NOW)
    await _set_heartbeat(watchdog_pool, table=PUBLIC_HEARTBEATS_TABLE,
                        service_key=CLIENT_BOT_SERVICE_KEY, last_seen_at=NOW)
    await _set_heartbeat(watchdog_pool, table=NOTIFY_HEARTBEATS_TABLE,
                        service_key=ADMIN_BOT_SERVICE_KEY, last_seen_at=NOW)

    wd = _watchdog(watchdog_pool)
    results = await wd.check_once()
    for key in (KEY_WORKER_HEARTBEAT, KEY_CLIENT_HEARTBEAT, KEY_ADMIN_HEARTBEAT):
        assert _result(results, key).ok is True


async def test_stale_heartbeat_is_broken(watchdog_pool):
    await _set_heartbeat(watchdog_pool, table=PUBLIC_HEARTBEATS_TABLE,
                        service_key=WORKER_BOT_SERVICE_KEY,
                        last_seen_at=NOW - timedelta(minutes=10))

    wd = _watchdog(watchdog_pool, heartbeat_max_age_sec=300)
    result = _result(await wd.check_once(), KEY_WORKER_HEARTBEAT)
    assert result.ok is False
    assert "устарел" in result.detail


async def test_admin_heartbeat_ignores_a_same_named_row_in_public(watchdog_pool):
    """Админ-боту нельзя писать в public (хард-правило) — строка там
    с тем же service_key не должна засчитаться за пульс из notify."""
    await _set_heartbeat(watchdog_pool, table=PUBLIC_HEARTBEATS_TABLE,
                        service_key=ADMIN_BOT_SERVICE_KEY, last_seen_at=NOW)

    wd = _watchdog(watchdog_pool)
    result = _result(await wd.check_once(), KEY_ADMIN_HEARTBEAT)
    assert result.ok is False


# --------------------------------------------------------------------------
# Опрос amoCRM (закрывает дыру факта 6 ТЗ)
# --------------------------------------------------------------------------

async def test_amocrm_never_polled_is_broken(watchdog_pool):
    wd = _watchdog(watchdog_pool)
    result = _result(await wd.check_once(), KEY_AMOCRM_POLL)
    assert result.ok is False


async def test_amocrm_recent_poll_is_ok(watchdog_pool):
    await _set_amocrm_poll(watchdog_pool, updated_at=NOW - timedelta(seconds=30))
    wd = _watchdog(watchdog_pool, amocrm_max_age_sec=300)
    result = _result(await wd.check_once(), KEY_AMOCRM_POLL)
    assert result.ok is True


async def test_amocrm_stale_poll_is_broken(watchdog_pool):
    await _set_amocrm_poll(watchdog_pool, updated_at=NOW - timedelta(minutes=20))
    wd = _watchdog(watchdog_pool, amocrm_max_age_sec=300)
    result = _result(await wd.check_once(), KEY_AMOCRM_POLL)
    assert result.ok is False
    assert "стоит" in result.detail


# --------------------------------------------------------------------------
# База и прокси
# --------------------------------------------------------------------------

async def test_database_check_ok_on_live_pool(watchdog_pool):
    wd = _watchdog(watchdog_pool)
    result = _result(await wd.check_once(), KEY_DATABASE)
    assert result.ok is True


async def test_proxy_check_reflects_probe_result(watchdog_pool):
    async def broken_probe() -> bool:
        return False

    wd = _watchdog(watchdog_pool, proxy_probe=broken_probe)
    result = _result(await wd.check_once(), KEY_PROXY)
    assert result.ok is False


async def test_proxy_probe_exception_does_not_hide_other_checks(watchdog_pool):
    async def exploding_probe() -> bool:
        raise RuntimeError("сеть недоступна")

    wd = _watchdog(watchdog_pool, proxy_probe=exploding_probe)
    results = await wd.check_once()

    proxy_result = _result(results, KEY_PROXY)
    assert proxy_result.ok is False
    assert "сеть недоступна" in proxy_result.detail
    # Падение одной проверки не должно унести с собой остальные результаты.
    assert _result(results, KEY_DATABASE).ok is True
    assert len(results) == 7


# --------------------------------------------------------------------------
# Рассыльщик клиентам: возраст последней отправки и рост очереди
# --------------------------------------------------------------------------

async def test_dispatch_empty_queue_is_ok(watchdog_pool):
    wd = _watchdog(watchdog_pool)
    result = _result(await wd.check_once(), KEY_DISPATCH)
    assert result.ok is True


async def test_dispatch_stalled_with_pending_is_broken(watchdog_pool):
    await _insert_dispatch_row(watchdog_pool, status="pending",
                               scheduled_at=NOW - timedelta(hours=1))

    wd = _watchdog(watchdog_pool, dispatch_max_age_sec=1800)
    result = _result(await wd.check_once(), KEY_DISPATCH)
    assert result.ok is False
    assert "отправок ещё не было" in result.detail


async def test_dispatch_recent_send_with_pending_is_ok(watchdog_pool):
    await _insert_dispatch_row(watchdog_pool, status="pending",
                               scheduled_at=NOW - timedelta(minutes=5))
    await _insert_dispatch_row(watchdog_pool, status="sent",
                               scheduled_at=NOW - timedelta(minutes=20),
                               sent_at=NOW - timedelta(minutes=10))

    wd = _watchdog(watchdog_pool, dispatch_max_age_sec=1800)
    result = _result(await wd.check_once(), KEY_DISPATCH)
    assert result.ok is True


async def test_dispatch_growing_queue_is_broken_on_second_pass(watchdog_pool):
    await _insert_dispatch_row(watchdog_pool, status="pending",
                               scheduled_at=NOW - timedelta(minutes=1))
    await _insert_dispatch_row(watchdog_pool, status="sent",
                               scheduled_at=NOW - timedelta(minutes=20),
                               sent_at=NOW - timedelta(minutes=1))

    wd = _watchdog(watchdog_pool, dispatch_max_age_sec=1800)
    first = _result(await wd.check_once(), KEY_DISPATCH)
    assert first.ok is True                     # один в очереди, слали недавно — норма

    await _insert_dispatch_row(watchdog_pool, status="pending",
                               scheduled_at=NOW - timedelta(minutes=1))
    second = _result(await wd.check_once(), KEY_DISPATCH)
    assert second.ok is False
    assert "растёт" in second.detail


# --------------------------------------------------------------------------
# Сквозные проверки интерфейса и выключателя
# --------------------------------------------------------------------------

async def test_check_once_covers_all_seven_keys(watchdog_pool):
    wd = _watchdog(watchdog_pool)
    results = await wd.check_once()

    assert {r.key for r in results} == {
        KEY_WORKER_HEARTBEAT, KEY_CLIENT_HEARTBEAT, KEY_ADMIN_HEARTBEAT,
        KEY_AMOCRM_POLL, KEY_DATABASE, KEY_PROXY, KEY_DISPATCH,
    }
    assert all(r.level == "red" for r in results)


async def test_disabled_watchdog_never_checks(watchdog_pool):
    """Выключатель в положении «выключено» — сторож поднимается и молчит."""
    calls = {"n": 0}
    stop = asyncio.Event()

    async def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        stop.set()

    async def probe_should_not_be_called() -> bool:
        raise AssertionError("проверки не должно быть при выключенном стороже")

    wd = Watchdog(pool=watchdog_pool, proxy_probe=probe_should_not_be_called,
                  enabled=False, now=lambda: NOW, sleep=fake_sleep)
    await wd.run_forever(stop)

    assert calls["n"] == 1


async def test_enabled_watchdog_runs_a_pass_then_stops(watchdog_pool):
    stop = asyncio.Event()
    calls = {"n": 0}

    async def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        stop.set()

    wd = _watchdog(watchdog_pool, sleep=fake_sleep)
    await wd.run_forever(stop)

    assert calls["n"] == 1


# --------------------------------------------------------------------------
# Один проход в минуту (замечание 2 ревью 18.09)
# --------------------------------------------------------------------------

async def test_manual_check_does_not_disturb_queue_growth(watchdog_pool):
    """Команда «что сейчас сломано» зовёт те же проверки, но замер очереди
    оставляет циклу: иначе рост между проходами теряется и «рассыльщик жив,
    но захлёбывается» маскируется."""
    await _insert_dispatch_row(watchdog_pool, status="pending",
                               scheduled_at=NOW - timedelta(minutes=1))
    await _insert_dispatch_row(watchdog_pool, status="sent",
                               scheduled_at=NOW - timedelta(minutes=20),
                               sent_at=NOW - timedelta(minutes=1))

    wd = _watchdog(watchdog_pool, dispatch_max_age_sec=1800)
    assert _result(await wd.check_once(), KEY_DISPATCH).ok is True

    await _insert_dispatch_row(watchdog_pool, status="pending",
                               scheduled_at=NOW - timedelta(minutes=1))
    await wd.check_once(remember=False)          # владелец нажал «что сломано»
    await wd.check_once(remember=False)          # и ещё раз

    second = _result(await wd.check_once(), KEY_DISPATCH)
    assert second.ok is False
    assert "растёт" in second.detail


async def test_cycle_hands_results_to_incidents(watchdog_pool):
    """Проверки идут одним проходом: цикл сам отдаёт результат дальше,
    второго независимого вызывающего у check_once нет."""
    stop = asyncio.Event()
    seen: list = []

    async def fake_sleep(_seconds: float) -> None:
        stop.set()

    async def on_results(results) -> None:
        seen.append(list(results))

    wd = _watchdog(watchdog_pool, sleep=fake_sleep)
    await wd.run_forever(stop, on_results=on_results)

    assert len(seen) == 1
    assert {r.key for r in seen[0]} == {
        KEY_WORKER_HEARTBEAT, KEY_CLIENT_HEARTBEAT, KEY_ADMIN_HEARTBEAT,
        KEY_AMOCRM_POLL, KEY_DATABASE, KEY_PROXY, KEY_DISPATCH,
    }


async def test_disabled_watchdog_does_not_feed_incidents(watchdog_pool):
    """Сторож выключен — проверок нет, значит и инцидентам ничего не уходит."""
    stop = asyncio.Event()
    seen: list = []

    async def fake_sleep(_seconds: float) -> None:
        stop.set()

    async def on_results(results) -> None:
        seen.append(list(results))

    wd = _watchdog(watchdog_pool, sleep=fake_sleep)
    wd.enabled = False
    await wd.run_forever(stop, on_results=on_results)

    assert seen == []
