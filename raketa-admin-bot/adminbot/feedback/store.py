"""Postgres для цикла «Повторный заказ»: оценённые заказы и своё состояние.

Оценка — из связки сделки реализации (`adminbot.amo_links`, `own_pool`) и оценки
клиента (`public.orders`, `bot_pool`, только чтение — хард-правило проекта).
Своё состояние — `adminbot.feedback_state` и `adminbot.feedback_cursor`
(миграция 019), у каждого режима свои строки, тем же приёмом, что
`promo_callback/store.py`. Строка состояния — по ключу `(kind, order_id)`
в пределах режима (миграция 020): заказ №12 и уборка №12 — разные строки.

ТЗ docs/plans/2026-09-28-feedback-tasks.md, задача 2;
ТЗ docs/plans/2026-09-30-cleaning-ratings.md, задача 3.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

import asyncpg

from adminbot.feedback.models import FeedbackKey, FeedbackState, RatedOrder

_UPDATABLE_FIELDS = frozenset({"status", "contact_task_id", "note_added", "attempts", "last_error"})


def _state_from_row(row: asyncpg.Record) -> FeedbackState:
    return FeedbackState(order_id=row["order_id"], mode=row["mode"], status=row["status"],
                         contact_task_id=row["contact_task_id"], note_added=row["note_added"],
                         attempts=row["attempts"], last_error=row["last_error"],
                         kind=row["kind"])


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

    async def states(self, mode: str) -> dict[FeedbackKey, FeedbackState]:
        """Строки режима по ключу `(kind, order_id)`."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT kind, order_id, mode, status, contact_task_id, note_added, attempts, "
                "last_error FROM adminbot.feedback_state WHERE mode = $1",
                mode,
            )
        return {(row["kind"], row["order_id"]): _state_from_row(row) for row in rows}

    async def register(self, mode: str, keys: Sequence[FeedbackKey]) -> None:
        """Завести строки состояния по ключам `(kind, order_id)`; уже заведённые не трогаем."""
        if not keys:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO adminbot.feedback_state (kind, order_id, mode) "
                "SELECT kind, order_id, $3 FROM unnest($1::text[], $2::bigint[]) "
                "AS new_rows(kind, order_id) "
                "ON CONFLICT (kind, order_id, mode) DO NOTHING",
                [kind for kind, _ in keys], [order_id for _, order_id in keys], mode,
            )

    async def update(self, kind: str, order_id: int, mode: str, **fields: Any) -> None:
        unknown = set(fields) - _UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Недопустимые поля состояния отзыва: {sorted(unknown)}")
        if not fields:
            return
        names = list(fields)
        assignments = ", ".join(f"{name} = ${index}" for index, name in enumerate(names, start=4))
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE adminbot.feedback_state SET {assignments}, updated_at = now() "
                "WHERE kind = $1 AND order_id = $2 AND mode = $3",
                kind, order_id, mode, *(fields[name] for name in names),
            )
