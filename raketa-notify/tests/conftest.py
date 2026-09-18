"""Общие фикстуры тестов службы notify.

Тесты идут на живой базе — TEST_DB_DSN обязателен, заглушки вместо Postgres
не годятся (правило этого ТЗ: прошлый раз отсутствие живой базы в тестах
пропустило дефект в бой). Схема `notify` пересоздаётся перед каждым тестом
из migrations/001_notify_schema.sql — тот же приём, что в
raketa-admin-bot/tests/test_autocall_store.py.

Роли `notify`, `bot`, `adminbot` должны существовать в кластере Postgres
заранее (миграция сама проверяет роль `notify` и упадёт без неё) — их
заводит владелец/админ руками, тесты за паролями не ходят и ролей не создают.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from notifyd import db

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "migrations").glob("*.sql"))

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def pool():
    """Пул к тестовой базе со свежей схемой `notify` на каждый тест."""
    if not TEST_DB_DSN:
        pytest.skip("TEST_DB_DSN не задан — нужен Postgres")

    admin_pool = await db.create_pool(TEST_DB_DSN, min_size=1, max_size=5)
    async with admin_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS notify CASCADE")
        for migration in MIGRATIONS:
            await conn.execute(migration.read_text())
    try:
        yield admin_pool
    finally:
        await admin_pool.close()


async def insert_outbox(pool: Any, *, kind: str, text: str = "текст события",
                        ref: Optional[str] = None, source: str = "worker",
                        dry_run: bool = False, status: str = "pending",
                        attempts: int = 0, next_try_at: Optional[datetime] = None,
                        expires_at: Optional[datetime] = None,
                        reply_markup: Optional[dict] = None) -> int:
    """Положить строку прямо в notify.outbox, минуя клиента ботов (задача 2,
    другая территория этого ТЗ) — здесь проверяется только почтальон."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO notify.outbox
                (kind, text, reply_markup, ref, source, dry_run, status, attempts,
                 next_try_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id
            """,
            kind, text, reply_markup, ref, source, dry_run, status, attempts,
            next_try_at or NOW, expires_at or (NOW + timedelta(hours=1)),
        )


async def fetch_outbox(pool: Any, outbox_id: int) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM notify.outbox WHERE id = $1", outbox_id)
    assert row is not None, f"событие {outbox_id} исчезло из ящика"
    return dict(row)


class FakeSender:
    """Отправитель для тестов: ничего не шлёт по-настоящему, только запоминает."""

    def __init__(self, *, always_fail: bool = False) -> None:
        self.sent: list[tuple[Any, str, Optional[dict]]] = []
        self.always_fail = always_fail
        self._next_id = 1000

    async def send(self, chat_id: Any, text: str,
                   reply_markup: Optional[dict] = None) -> Optional[int]:
        if self.always_fail:
            raise RuntimeError("фальшивая сеть недоступна")
        self.sent.append((chat_id, text, reply_markup))
        self._next_id += 1
        return self._next_id


class FakeJournalSource:
    """Источник журнала для тестов переходника (задача 5): читать
    настоящий journald на macOS нельзя (ограничение среды в ТЗ), поэтому
    тест кладёт строки сам через `.push(...)`, а `read_new()` отдаёт их
    один раз и очищает — как реальный источник отдаёт только новое."""

    def __init__(self, unit: str) -> None:
        self.unit = unit
        self._pending: list[Any] = []

    def push(self, message: str, *, timestamp: Optional[datetime] = None) -> None:
        from notifyd.journal_source import JournalEntry

        self._pending.append(JournalEntry(unit=self.unit, timestamp=timestamp or NOW,
                                          message=message))

    async def read_new(self) -> list[Any]:
        entries, self._pending = self._pending, []
        return entries
