"""Кнопка «Я уже перезвонил» робота автозвонка (bot.py, `autocall_called_cb`).

ТЗ docs/plans/2026-10-23-autocall-manager-button.md, задача 1 (2026-10-23).

Робот автозвонка (админ-бот) не дозвонился — менеджеру в личку рабочего бота
приходит сообщение с кнопкой «Я уже перезвонил» (шлёт админ-бот ключом рабочего
бота, нажатие приходит сюда). Рабочий бот смотрит статус цепочки в
`adminbot.autocall_leads`: живая (`queued`, `calling`, `error`) — строка в
`autocall_manager_callbacks` (по ней админ-бот перед повтором остановится),
к сообщению дописывается «✅ Принято…», кнопка убирается. Цепочки нет или она
закончилась — без записи, кнопка убирается, ответ «Робот по этой заявке уже
не звонит».

На настоящем Postgres (DSN в `TEST_DB_DSN`, без него класс пропускается):
своя схема в `search_path` пула, `autocall_manager_callbacks` — ровно тем
файлом миграции, что уйдёт на прод; таблица цепочек — минимальная копия в той
же схеме, обработчик смотрит в неё через подменённый `AUTOCALL_LEADS_SOURCE`.
Telegram подменён.
"""

import os
import unittest
from pathlib import Path
from unittest import mock

import asyncpg

import bot

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "app" / "migrations"
SCHEMA = "autocall_manager_callbacks_test"

LEAD_ID = 31750907
MANAGER_ID = 555
CHAT_ID = 555
MESSAGE_ID = 900
OLD_TEXT = (
    "Робот не дозвонился до вас по заявке. Повтор в 10:12.\n"
    "Телефон клиента: +7 916 111-22-33\n"
    "https://example.amocrm.ru/leads/detail/31750907"
)
ACCEPTED_LINE = "✅ Принято: робот по этой заявке больше не позвонит."


def _migration(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


class FakeQuery:
    def __init__(self, lead_id, *, user_id=MANAGER_ID, text=OLD_TEXT):
        self.from_user = mock.Mock(id=user_id, full_name="Менеджер", username=None)
        self.data = f"{bot.AUTOCALL_CALLED_CB_PREFIX}{lead_id}"
        self.message = mock.Mock(chat=mock.Mock(id=CHAT_ID), message_id=MESSAGE_ID, text=text)
        self.answer = mock.AsyncMock()


class PrefixTests(unittest.TestCase):
    def test_prefix_is_literal(self):
        # Тот же префикс собирает кнопку в админ-боте — менять только вместе.
        self.assertEqual(bot.AUTOCALL_CALLED_CB_PREFIX, "autocall_called:")

    def test_leads_source_is_adminbot_table(self):
        self.assertEqual(bot.AUTOCALL_LEADS_SOURCE, "adminbot.autocall_leads")


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class AutocallCalledButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            await admin.execute(f"CREATE SCHEMA {SCHEMA}")
            await admin.execute(f"SET search_path TO {SCHEMA}")
            # Только то, что читает обработчик, — не копия adminbot.autocall_leads.
            await admin.execute(
                "CREATE TABLE autocall_leads (lead_id bigint PRIMARY KEY, status text NOT NULL)"
            )
            await admin.execute(_migration("0019_autocall_manager_callbacks.sql"))
        finally:
            await admin.close()
        self.pool = await asyncpg.create_pool(
            TEST_DB_DSN, min_size=1, max_size=2, server_settings={"search_path": SCHEMA}
        )
        for target, value in (
            ("pool", self.pool),
            ("AUTOCALL_LEADS_SOURCE", f"{SCHEMA}.autocall_leads"),
        ):
            patcher = mock.patch.object(bot, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.edit_message_text = self._patch_bot("edit_message_text")
        self.edit_message_reply_markup = self._patch_bot("edit_message_reply_markup")

    async def asyncTearDown(self):
        await self.pool.close()
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        finally:
            await admin.close()

    def _patch_bot(self, name):
        patcher = mock.patch.object(bot.bot, name, mock.AsyncMock())
        self.addCleanup(patcher.stop)
        return patcher.start()

    async def _chain(self, status, lead_id=LEAD_ID):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO autocall_leads (lead_id, status) VALUES ($1, $2)", lead_id, status
            )

    async def _callbacks(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT lead_id, tg_user_id, pressed_at FROM autocall_manager_callbacks ORDER BY lead_id"
            )

    async def _press(self, lead_id=LEAD_ID, **kwargs):
        query = FakeQuery(lead_id, **kwargs)
        await bot.autocall_called_cb(query)
        return query

    def _assert_button_dropped_only(self):
        self.edit_message_text.assert_not_awaited()
        call = self.edit_message_reply_markup.await_args
        self.assertIsNotNone(call)
        self.assertEqual((call.kwargs["chat_id"], call.kwargs["message_id"]), (CHAT_ID, MESSAGE_ID))
        self.assertEqual(call.kwargs["reply_markup"].inline_keyboard, [])

    async def test_live_chain_records_press_and_edits_message(self):
        for status in ("queued", "calling", "error"):
            with self.subTest(status=status):
                lead_id = LEAD_ID + len(status)
                await self._chain(status, lead_id)
                self.edit_message_text.reset_mock()

                query = await self._press(lead_id)

                query.answer.assert_awaited_once_with("Принято")
                rows = [r for r in await self._callbacks() if r["lead_id"] == lead_id]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["tg_user_id"], MANAGER_ID)
                self.assertIsNotNone(rows[0]["pressed_at"])
                call = self.edit_message_text.await_args
                self.assertEqual((call.kwargs["chat_id"], call.kwargs["message_id"]), (CHAT_ID, MESSAGE_ID))
                self.assertEqual(call.kwargs["text"], f"{OLD_TEXT}\n\n{ACCEPTED_LINE}")
                self.assertIsNone(call.kwargs["parse_mode"])
                self.assertEqual(call.kwargs["reply_markup"].inline_keyboard, [])

    async def test_live_chain_with_empty_text_only_drops_button(self):
        # Telegram прислал недоступное сообщение без текста — текст не трогаем,
        # только убираем кнопку; нажатие всё равно записывается.
        await self._chain("queued")

        query = await self._press(text=None)

        query.answer.assert_awaited_once_with("Принято")
        rows = await self._callbacks()
        self.assertEqual([r["lead_id"] for r in rows], [LEAD_ID])
        self._assert_button_dropped_only()

    async def test_second_press_keeps_one_row_without_error(self):
        await self._chain("queued")
        await self._press(user_id=MANAGER_ID)

        query = await self._press(user_id=777)

        query.answer.assert_awaited_once_with("Принято")
        rows = await self._callbacks()
        self.assertEqual(len(rows), 1)
        # Строки не меняются: остаётся первое нажатие.
        self.assertEqual(rows[0]["tg_user_id"], MANAGER_ID)

    async def test_final_chain_is_not_recorded(self):
        for status in ("done", "no_contact", "gave_up", "cancelled"):
            with self.subTest(status=status):
                lead_id = LEAD_ID + len(status)
                await self._chain(status, lead_id)
                self.edit_message_reply_markup.reset_mock()
                self.edit_message_text.reset_mock()

                query = await self._press(lead_id)

                query.answer.assert_awaited_once_with("Робот по этой заявке уже не звонит")
                self._assert_button_dropped_only()
        self.assertEqual(await self._callbacks(), [])

    async def test_no_chain_is_not_recorded(self):
        query = await self._press(LEAD_ID)

        query.answer.assert_awaited_once_with("Робот по этой заявке уже не звонит")
        self._assert_button_dropped_only()
        self.assertEqual(await self._callbacks(), [])

    async def test_failed_edit_still_records_press(self):
        await self._chain("calling")
        self.edit_message_text.side_effect = RuntimeError("message to edit not found")

        query = await self._press()

        query.answer.assert_awaited_once_with("Принято")
        rows = await self._callbacks()
        self.assertEqual([r["lead_id"] for r in rows], [LEAD_ID])

    async def test_migration_is_safe_to_repeat(self):
        await self._chain("queued")
        await self._press()
        admin = await asyncpg.connect(TEST_DB_DSN)
        try:
            await admin.execute(f"SET search_path TO {SCHEMA}")
            await admin.execute(_migration("0019_autocall_manager_callbacks.sql"))
        finally:
            await admin.close()
        self.assertEqual(len(await self._callbacks()), 1)
