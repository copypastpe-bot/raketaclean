"""Postgres для откликов на промо: заявки рабочего бота и своё состояние.

Заявки (`public.promo_callbacks`) — только чтение, как всё в схеме `public`
(хард-правило проекта). Своё состояние — `adminbot.promo_callback_state` и
`adminbot.promo_callback_cursor` (миграция 018), у каждого режима свои строки.
SQL живёт здесь, а не в `db.py`, — тем же приёмом, что `sync/deletions.py`.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

import asyncpg

from adminbot.promo_callback.sync import PromoCallback, PromoCallbackState

_CALLBACK_COLUMNS = "id, source, client_id, lead_id, phone, name, response_text, created_at"

# Поля строки состояния, которые цикл вправе менять. Имя поля попадает в текст
# запроса, поэтому — только из этого списка.
_UPDATABLE_FIELDS = frozenset({"status", "contact_id", "lead_id", "attempts", "last_error"})


def _callback_from_row(row: asyncpg.Record) -> PromoCallback:
    return PromoCallback(id=row["id"], source=row["source"], phone=row["phone"],
                         name=row["name"], response_text=row["response_text"],
                         created_at=row["created_at"], client_id=row["client_id"],
                         lead_id=row["lead_id"])


def _state_from_row(row: asyncpg.Record) -> PromoCallbackState:
    return PromoCallbackState(callback_id=row["callback_id"], mode=row["mode"],
                              status=row["status"], contact_id=row["contact_id"],
                              lead_id=row["lead_id"], attempts=row["attempts"],
                              last_error=row["last_error"])


class PgPromoCallbackSource:
    """Заявки рабочего бота. Читает через `bot_pool` — как `PgWirePaymentSource`."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def max_id(self) -> int:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval("SELECT coalesce(max(id), 0) FROM public.promo_callbacks")
        return int(value)

    async def after(self, last_id: int, limit: int) -> list[PromoCallback]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_CALLBACK_COLUMNS} FROM public.promo_callbacks "
                "WHERE id > $1 ORDER BY id LIMIT $2", last_id, limit)
        return [_callback_from_row(row) for row in rows]

    async def by_ids(self, callback_ids: Sequence[int]) -> list[PromoCallback]:
        if not callback_ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_CALLBACK_COLUMNS} FROM public.promo_callbacks "
                "WHERE id = ANY($1::bigint[]) ORDER BY id", list(callback_ids))
        return [_callback_from_row(row) for row in rows]


class PgPromoCallbackStore:
    """Своё состояние в схеме `adminbot`. Пишет через `own_pool`."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def cursor(self, mode: str) -> Optional[int]:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT last_id FROM adminbot.promo_callback_cursor WHERE mode = $1", mode)
        return None if value is None else int(value)

    async def save_cursor(self, mode: str, last_id: int) -> None:
        """Закладка только вперёд: назад её не двигает даже запоздавший проход."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO adminbot.promo_callback_cursor (mode, last_id) VALUES ($1, $2)
                ON CONFLICT (mode) DO UPDATE
                    SET last_id = GREATEST(adminbot.promo_callback_cursor.last_id,
                                           EXCLUDED.last_id),
                        updated_at = now()
                """, mode, last_id)

    async def register(self, mode: str, callback_ids: Sequence[int]) -> None:
        """Завести строки состояния; уже заведённые не трогаем."""
        if not callback_ids:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO adminbot.promo_callback_state (callback_id, mode)
                SELECT unnest($1::bigint[]), $2
                ON CONFLICT (callback_id, mode) DO NOTHING
                """, list(callback_ids), mode)

    async def pending(self, mode: str) -> list[PromoCallbackState]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT callback_id, mode, status, contact_id, lead_id, attempts, last_error
                FROM adminbot.promo_callback_state
                WHERE mode = $1 AND status IN ('new', 'lead_created')
                ORDER BY callback_id
                """, mode)
        return [_state_from_row(row) for row in rows]

    async def get(self, callback_id: int, mode: str) -> Optional[PromoCallbackState]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT callback_id, mode, status, contact_id, lead_id, attempts, last_error
                FROM adminbot.promo_callback_state WHERE callback_id = $1 AND mode = $2
                """, callback_id, mode)
        return None if row is None else _state_from_row(row)

    async def update(self, callback_id: int, mode: str, **fields: Any) -> None:
        unknown = set(fields) - _UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Недопустимые поля заявки на звонок: {sorted(unknown)}")
        if not fields:
            return
        names = list(fields)
        assignments = ", ".join(f"{name} = ${index}" for index, name in enumerate(names, start=3))
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE adminbot.promo_callback_state SET {assignments}, updated_at = now() "
                "WHERE callback_id = $1 AND mode = $2",
                callback_id, mode, *(fields[name] for name in names))
