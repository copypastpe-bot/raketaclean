"""Пульс админ-бота (`adminbot/heartbeat.py`, HeartbeatWriter) — ТЗ
2026-09-18, задача 6: раз в минуту отмечается «я жив» в
`notify.service_heartbeats` (не в `public.service_heartbeats` — туда
этому боту писать нельзя, хард-правило проекта).

Нужен настоящий Postgres (DSN в TEST_DB_DSN, как в test_notify_bus.py):
    export TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/adminbot_test

Миграции берутся ровно те же, что применяет служба-почтальон (задачи 1 и 7,
`raketa-notify/migrations/001_notify_schema.sql` и `002_watchdog_schema.sql`
в корне монорепо), а не переписанная копия. Схема `notify` не пересоздаётся
с нуля (обе миграции идемпотентны): в этой же ветке возможна параллельная
работа над каталогом `raketa-notify/`, и DROP SCHEMA здесь мог бы забрать
данные у чужой проверки — тот же приём, что в test_notify_bus.py.
"""

import asyncio
import os
from datetime import timedelta
from pathlib import Path

import pytest

from adminbot import db, heartbeat

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DB_DSN, reason="TEST_DB_DSN не задан — нужен Postgres")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "raketa-notify" / "migrations"
MIGRATION_SQL = [p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS_DIR.glob("*.sql"))]


@pytest.fixture
async def notify_pool():
    pool = await db.create_pool(TEST_DB_DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        for sql in MIGRATION_SQL:
            await conn.execute(sql)
    yield pool
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM notify.service_heartbeats WHERE service_key = $1",
            heartbeat.SERVICE_KEY,
        )
    await pool.close()


async def test_write_once_inserts_a_fresh_row(notify_pool):
    writer = heartbeat.HeartbeatWriter(pool=notify_pool)
    await writer.write_once()

    async with notify_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT display_name, status, last_seen_at, last_ok_at "
            "FROM notify.service_heartbeats WHERE service_key = $1",
            heartbeat.SERVICE_KEY,
        )
    assert row is not None
    assert row["display_name"] == heartbeat.DISPLAY_NAME
    assert row["status"] == "ok"
    assert row["last_seen_at"] == row["last_ok_at"]


async def test_second_call_updates_the_same_row_not_a_new_one(notify_pool):
    writer = heartbeat.HeartbeatWriter(pool=notify_pool)
    await writer.write_once()
    async with notify_pool.acquire() as conn:
        first_seen = await conn.fetchval(
            "SELECT last_seen_at FROM notify.service_heartbeats WHERE service_key = $1",
            heartbeat.SERVICE_KEY,
        )

    later = first_seen + timedelta(seconds=5)
    writer2 = heartbeat.HeartbeatWriter(pool=notify_pool, now=lambda: later)
    await writer2.write_once()

    async with notify_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT last_seen_at FROM notify.service_heartbeats WHERE service_key = $1",
            heartbeat.SERVICE_KEY,
        )
    assert len(rows) == 1               # не завелась вторая строка на тот же ключ
    assert rows[0]["last_seen_at"] == later


async def test_run_forever_writes_at_least_once_then_stops(notify_pool):
    stop = asyncio.Event()
    calls = {"n": 0}

    async def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        stop.set()

    writer = heartbeat.HeartbeatWriter(pool=notify_pool, sleep=fake_sleep)
    await writer.run_forever(stop)

    assert calls["n"] == 1
    async with notify_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM notify.service_heartbeats WHERE service_key = $1",
            heartbeat.SERVICE_KEY,
        )
    assert row is not None
