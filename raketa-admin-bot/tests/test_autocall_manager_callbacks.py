"""Нажатия кнопки «Я уже перезвонил»: таблица рабочего бота, админ-бот только читает.

ТЗ docs/plans/2026-10-23-autocall-manager-button.md, задача 2. Нажатие получает
рабочий бот и пишет строку в `public.autocall_manager_callbacks`; админ-бот перед
повтором спрашивает «по сделке нажимали?». Таблица создаётся здесь из миграции
рабочего бота `app/migrations/0019_autocall_manager_callbacks.sql` в корне
монорепо, а не из копии, — разойдётся договорённость, упадёт тест (как в
test_promo_callback_store.py). Pg-проверки пропускаются без TEST_DB_DSN.
"""

import os
from pathlib import Path

import pytest

from adminbot import db
from adminbot.autocall.store import MemoryManagerCallbacks, PgManagerCallbackSource

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")

ROOT = Path(__file__).resolve().parent.parent
BOT_MIGRATION = ROOT.parent / "app" / "migrations" / "0019_autocall_manager_callbacks.sql"


async def test_memory_callbacks_answer_only_for_pressed_lead():
    """Репетиция и тесты: нажатия в памяти, в базу ничего."""
    callbacks = MemoryManagerCallbacks()
    assert await callbacks.pressed(31750907) is False

    callbacks.press(31750907)

    assert await callbacks.pressed(31750907) is True
    assert await callbacks.pressed(31746499) is False


@pytest.fixture
async def bot_pool():
    if not TEST_DB_DSN:
        pytest.skip("TEST_DB_DSN не задан — нужен Postgres")
    pool = await db.create_pool(TEST_DB_DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS public.autocall_manager_callbacks")
        await conn.execute("SET search_path TO public")
        await conn.execute(BOT_MIGRATION.read_text())
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS public.autocall_manager_callbacks")
        await pool.close()


async def test_pg_source_reads_worker_bot_table(bot_pool):
    """Строка рабочего бота по сделке — «нажимали»; по другой сделке — нет."""
    source = PgManagerCallbackSource(bot_pool)
    assert await source.pressed(31750907) is False

    async with bot_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO public.autocall_manager_callbacks (lead_id, tg_user_id) "
            "VALUES ($1, $2)", 31750907, 555000111)

    assert await source.pressed(31750907) is True
    assert await source.pressed(31746499) is False
