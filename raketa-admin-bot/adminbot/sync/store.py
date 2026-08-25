"""Хранилище состояния заказов для движка.

Движок не знает про Postgres: он работает через этот узкий набор операций.
В бою — PgLinkStore поверх схемы `adminbot`, в тестах — двойник в памяти.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

import asyncpg

from adminbot import db
from adminbot.models import AmoLink


class LinkStore(Protocol):
    """Что движку нужно от хранилища — и ничего сверх того."""

    async def get(self, order_id: int) -> Optional[AmoLink]: ...

    async def create(self, order_id: int, phone10: Optional[str]) -> AmoLink: ...

    async def update(self, order_id: int, **fields: Any) -> Optional[AmoLink]: ...

    async def mark_step(self, order_id: int, step: str) -> None: ...

    async def log(self, order_id: int, action: str, *, dry_run: bool,
                  entity: Optional[str] = None, amo_id: Optional[int] = None,
                  payload: Optional[Any] = None) -> None: ...

    async def taken_leads(self, phone10: str, exclude_order_id: int) -> set[int]: ...


class PgLinkStore:
    """Боевое хранилище: схема `adminbot` того же Postgres, что и у бота."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get(self, order_id: int) -> Optional[AmoLink]:
        return await db.get_link(self._pool, order_id)

    async def create(self, order_id: int, phone10: Optional[str]) -> AmoLink:
        return await db.create_link(self._pool, order_id, phone10)

    async def update(self, order_id: int, **fields: Any) -> Optional[AmoLink]:
        return await db.update_link(self._pool, order_id, **fields)

    async def mark_step(self, order_id: int, step: str) -> None:
        await db.mark_checklist_step(self._pool, order_id, step)

    async def log(self, order_id: int, action: str, *, dry_run: bool,
                  entity: Optional[str] = None, amo_id: Optional[int] = None,
                  payload: Optional[Any] = None) -> None:
        await db.log_action(self._pool, order_id=order_id, action=action, dry_run=dry_run,
                            amo_entity=entity, amo_id=amo_id, payload=payload)

    async def taken_leads(self, phone10: str, exclude_order_id: int) -> set[int]:
        return await db.fetch_taken_lead_ids(self._pool, phone10, exclude_order_id)
