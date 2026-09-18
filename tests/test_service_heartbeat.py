"""Пульс рабочего бота (`write_own_heartbeat`, bot.py) — ТЗ 2026-09-18,
задача 6: раз в минуту отмечается «я жив» в `service_heartbeats`, той же
таблице, которую уже читает `check_client_bot_health` для клиентского бота
(факт 7 ТЗ) и которую читает сторож `raketa-notify/notifyd/watchdog.py`
(задача 7, свои тесты — raketa-notify/tests/test_watchdog.py).

Нужен настоящий Postgres — TEST_DB_DSN, без него тесты пропускаются:
    export TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/raketaclean_test
"""

import asyncio
import os
import unittest

import asyncpg

import bot

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class WriteOwnHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(dsn=TEST_DB_DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as conn:
            await bot.ensure_service_heartbeat_schema(conn)
        self._orig_pool = bot.pool
        bot.pool = self.pool

    async def asyncTearDown(self):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM service_heartbeats WHERE service_key = $1",
                bot.SERVICE_HEARTBEAT_SERVICE_KEY,
            )
        bot.pool = self._orig_pool
        await self.pool.close()

    async def test_writes_a_fresh_row_on_first_call(self):
        await bot.write_own_heartbeat()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT display_name, status, last_seen_at, last_ok_at "
                "FROM service_heartbeats WHERE service_key = $1",
                bot.SERVICE_HEARTBEAT_SERVICE_KEY,
            )
        self.assertIsNotNone(row)
        self.assertEqual(row["display_name"], bot.SERVICE_HEARTBEAT_DISPLAY_NAME)
        self.assertEqual(row["status"], "ok")
        self.assertIsNotNone(row["last_seen_at"])
        self.assertEqual(row["last_seen_at"], row["last_ok_at"])

    async def test_second_call_updates_the_same_row_not_a_new_one(self):
        await bot.write_own_heartbeat()
        async with self.pool.acquire() as conn:
            first_seen = await conn.fetchval(
                "SELECT last_seen_at FROM service_heartbeats WHERE service_key = $1",
                bot.SERVICE_HEARTBEAT_SERVICE_KEY,
            )

        await asyncio.sleep(0.01)  # гарантируем другую метку времени
        await bot.write_own_heartbeat()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT last_seen_at FROM service_heartbeats WHERE service_key = $1",
                bot.SERVICE_HEARTBEAT_SERVICE_KEY,
            )
        self.assertEqual(len(rows), 1)  # не завелась вторая строка на тот же ключ
        self.assertGreater(rows[0]["last_seen_at"], first_seen)

    async def test_noop_when_pool_is_not_ready(self):
        """Пул ещё не поднят при старте — функция не должна падать (тот же
        приём защиты, что у check_client_bot_health)."""
        bot.pool = None
        await bot.write_own_heartbeat()
