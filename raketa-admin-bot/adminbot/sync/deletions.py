"""Источник неразобранных удалений: заказ снят в рабочем боте, связка про это
ещё не в курсе.

У удаления два разных следа (факт 1, ТЗ 2026-09-17 «удаление заказа освобождает
сделку»): заказ химчистки при удалении пропадает из БД физически, и рабочий
бот кладёт след в свой регистр `public.deleted_orders`; уборка остаётся строкой
`public.cleaning_orders` с заполненным `deleted_at`. Разбор (задача 5, отдельным
этапом) для обоих один и тот же — не важно, каким путём заказ пропал, поэтому
источник сводит оба следа к одному списку `DeletionRecord`.

Разобранные записи не возвращаются повторно — критерий не сам факт удаления,
а отметка `adminbot.order_deletions_seen` (задача 3), ключ (`kind`, `order_id`):
номера заказов и уборок пересекаются, поэтому голого `order_id` недостаточно.
"""

from __future__ import annotations

import asyncpg

from adminbot import db
from adminbot.models import DeletionRecord


async def fetch_pending_deletions(own_pool: asyncpg.Pool) -> list[DeletionRecord]:
    """Все неразобранные удаления — заказы и уборки вместе, старые первыми.

    Сортировка по времени удаления, а не по виду работы: решение владельца 4
    (ТЗ 2026-09-17) разбирает удаления первым шагом прохода, и внутри этого шага
    порядок должен быть предсказуемым, а не «сначала все заказы, потом уборки».
    """
    orders = await db.fetch_pending_order_deletions(own_pool)
    cleanings = await db.fetch_pending_cleaning_deletions(own_pool)
    return sorted(orders + cleanings, key=lambda r: (r.deleted_at, r.kind, r.order_id))
