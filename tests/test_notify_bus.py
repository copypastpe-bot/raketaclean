"""Клиент «положить событие» (`notifications/notify_bus.py`, `put_event`).

ТЗ `docs/plans/2026-09-18-notifications-bus.md`, задача 2: одна вставка
в `notify.outbox`, без похода в сеть; там, где событие рождается внутри
транзакции — в ней же; неудачная вставка не роняет вызывающий код.
Урок ревью 17.09 (замечание 1 в tests/test_pending_order_reports.py,
повторено в test_deleted_orders.py): такие вещи без настоящего Postgres
не ловятся — здесь живая база, DSN в TEST_DB_DSN, без него тесты
пропускаются:
    export TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/raketaclean_test

Миграция берётся ровно та же, что применяет служба-почтальон (задача 1,
`raketa-notify/migrations/001_notify_schema.sql`), а не переписанная
копия — так тест ловит и её собственные дефекты. Схема `notify` при этом
не пересоздаётся с нуля (миграция идемпотентна, повторный прогон не
ломает): в этой же ветке параллельно работает другой исполнитель над
каталогом `raketa-notify/`, и DROP SCHEMA здесь мог бы забрать данные у
его собственной проверки. Поэтому все строки этого файла помечены
префиксом `kind` и убираются точечно в tearDown, а не через пересоздание
схемы.
"""

import json
import os
import unittest
from pathlib import Path

import asyncpg

from notifications import notify_bus

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent
    / "raketa-notify" / "migrations" / "001_notify_schema.sql"
).read_text(encoding="utf-8")

_MARK = "test_notify_bus_"  # префикс kind для всех строк этого файла


@unittest.skipUnless(TEST_DB_DSN, "TEST_DB_DSN не задан — нужен настоящий Postgres")
class PutEventRealSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(dsn=TEST_DB_DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as conn:
            await conn.execute(MIGRATION_SQL)

    async def asyncTearDown(self):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM notify.outbox WHERE kind LIKE $1", f"{_MARK}%")
        await self.pool.close()

    async def test_insert_lands_row_with_expected_fields(self):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                event_id = await notify_bus.put_event(
                    conn,
                    kind=f"{_MARK}order_done",
                    text="Заказ №123 закрыт",
                    ref=123,
                    reply_markup={"inline_keyboard": [[{"text": "ок", "callback_data": "ok"}]]},
                )
        self.assertIsNotNone(event_id)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT kind, text, reply_markup, ref, source, dry_run, status, expires_at "
                "FROM notify.outbox WHERE id=$1",
                event_id,
            )
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], f"{_MARK}order_done")
        self.assertEqual(row["text"], "Заказ №123 закрыт")
        self.assertEqual(row["ref"], "123")
        self.assertEqual(row["source"], "worker")
        self.assertFalse(row["dry_run"])
        self.assertEqual(row["status"], "pending")
        self.assertIsNotNone(row["expires_at"])
        self.assertEqual(
            json.loads(row["reply_markup"]),
            {"inline_keyboard": [[{"text": "ок", "callback_data": "ok"}]]},
        )

    async def test_rollback_leaves_no_row(self):
        kind = f"{_MARK}rollback"
        with self.assertRaises(RuntimeError):
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    event_id = await notify_bus.put_event(conn, kind=kind, text="х")
                    self.assertIsNotNone(event_id)
                    raise RuntimeError("имитация отката бизнес-операции")

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM notify.outbox WHERE kind=$1", kind)
        self.assertIsNone(row)

    async def test_failed_insert_does_not_crash_or_abort_outer_transaction(self):
        good_kind_before = f"{_MARK}before"
        good_kind_after = f"{_MARK}after"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                before_id = await notify_bus.put_event(conn, kind=good_kind_before, text="до")
                self.assertIsNotNone(before_id)

                # kind=None ломает NOT NULL — событие "родилось" некорректным,
                # но это не повод ронять транзакцию вокруг (put_event гасит
                # исключение сама и продолжает работу вызывающего кода).
                failed_id = await notify_bus.put_event(conn, kind=None, text="сломано")
                self.assertIsNone(failed_id)

                after_id = await notify_bus.put_event(conn, kind=good_kind_after, text="после")
                self.assertIsNotNone(after_id)
            # Если бы savepoint внутри put_event не сработал, conn остался бы
            # в aborted-состоянии, и выход из внешней транзакции здесь упал бы.

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT kind FROM notify.outbox WHERE kind IN ($1, $2)",
                good_kind_before, good_kind_after,
            )
        self.assertEqual({r["kind"] for r in rows}, {good_kind_before, good_kind_after})


if __name__ == "__main__":
    unittest.main()
