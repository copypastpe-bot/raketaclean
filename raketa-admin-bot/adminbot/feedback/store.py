"""Postgres для цикла «Повторный заказ»: оценённые заказы и своё состояние.

Оценка — из связки сделки реализации (`adminbot.amo_links`, `own_pool`) и оценки
клиента (`public.orders`, `bot_pool`, только чтение — хард-правило проекта).
Своё состояние — `adminbot.feedback_state` и `adminbot.feedback_cursor`
(миграция 019), у каждого режима свои строки, тем же приёмом, что
`promo_callback/store.py`.

ТЗ docs/plans/2026-09-28-feedback-tasks.md, задача 2.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

import asyncpg

from adminbot.feedback.models import FeedbackState, RatedOrder

_UPDATABLE_FIELDS = frozenset({"status", "contact_task_id", "note_added", "attempts", "last_error"})


def _state_from_row(row: asyncpg.Record) -> FeedbackState:
    return FeedbackState(order_id=row["order_id"], mode=row["mode"], status=row["status"],
                         contact_task_id=row["contact_task_id"], note_added=row["note_added"],
                         attempts=row["attempts"], last_error=row["last_error"])


class PgFeedbackSource:
    """Оценённые заказы: сделка реализации (own_pool) + оценка клиента (bot_pool)."""

    def __init__(self, bot_pool: asyncpg.Pool, own_pool: asyncpg.Pool) -> None:
        self.bot_pool = bot_pool
        self.own_pool = own_pool

    async def rated_orders(self) -> list[RatedOrder]:
        async with self.own_pool.acquire() as conn:
            links = await conn.fetch(
                "SELECT order_id, real_lead_id FROM adminbot.amo_links "
                "WHERE status = 'done' AND real_lead_id IS NOT NULL"
            )
        if not links:
            return []
        lead_by_order = {row["order_id"]: row["real_lead_id"] for row in links}
        async with self.bot_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, rating_score, rating_comment, rating_replied_at FROM public.orders "
                "WHERE id = ANY($1::bigint[]) AND rating_score IS NOT NULL "
                "AND rating_replied_at IS NOT NULL",
                list(lead_by_order),
            )
        return [
            RatedOrder(order_id=row["id"], lead_id=lead_by_order[row["id"]],
                      score=row["rating_score"], comment=row["rating_comment"],
                      replied_at=row["rating_replied_at"])
            for row in rows
        ]


class PgFeedbackStore:
    """Своё состояние цикла в схеме adminbot. Пишет через own_pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def started_at(self, mode: str) -> Optional[datetime]:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT started_at FROM adminbot.feedback_cursor WHERE mode = $1", mode)

    async def save_started_at(self, mode: str, when: datetime) -> None:
        """Закладка первого прохода режима — ставится только один раз."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO adminbot.feedback_cursor (mode, started_at) VALUES ($1, $2) "
                "ON CONFLICT (mode) DO NOTHING",
                mode, when,
            )

    async def states(self, mode: str) -> dict[int, FeedbackState]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT order_id, mode, status, contact_task_id, note_added, attempts, "
                "last_error FROM adminbot.feedback_state WHERE mode = $1",
                mode,
            )
        return {row["order_id"]: _state_from_row(row) for row in rows}

    async def register(self, mode: str, order_ids: Sequence[int]) -> None:
        """Завести строки состояния; уже заведённые не трогаем."""
        if not order_ids:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO adminbot.feedback_state (order_id, mode) "
                "SELECT unnest($1::bigint[]), $2 "
                "ON CONFLICT (order_id, mode) DO NOTHING",
                list(order_ids), mode,
            )

    async def update(self, order_id: int, mode: str, **fields: Any) -> None:
        unknown = set(fields) - _UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Недопустимые поля состояния отзыва: {sorted(unknown)}")
        if not fields:
            return
        names = list(fields)
        assignments = ", ".join(f"{name} = ${index}" for index, name in enumerate(names, start=3))
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE adminbot.feedback_state SET {assignments}, updated_at = now() "
                "WHERE order_id = $1 AND mode = $2",
                order_id, mode, *(fields[name] for name in names),
            )
