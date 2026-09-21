"""Хранилище состояния заказов для движка.

Движок не знает про Postgres: он работает через этот узкий набор операций.
В бою — PgLinkStore поверх схемы `adminbot`, в тестах — двойник в памяти.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
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

    async def update_and_log(self, order_id: int, *, action: str, dry_run: bool,
                             entity: Optional[str] = None, amo_id: Optional[int] = None,
                             payload: Optional[Any] = None,
                             **fields: Any) -> Optional[AmoLink]: ...

    async def taken_leads(self, phone10: str, exclude_order_id: int) -> set[int]: ...


class MemoryLinkStore:
    """Хранилище в памяти: для репетиции и разовых прогонов.

    Ничего не пишет ни в какую базу — состояние живёт только на время запуска.
    """

    def __init__(self, now: Optional[Any] = None) -> None:
        self.links: dict[int, AmoLink] = {}
        self.actions: list[dict] = []
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def get(self, order_id: int) -> Optional[AmoLink]:
        return self.links.get(order_id)

    async def create(self, order_id: int, phone10: Optional[str]) -> AmoLink:
        link = self.links.get(order_id)
        if link is None:
            link = AmoLink(order_id=order_id, phone10=phone10 or "", status="new",
                           checklist={}, created_at=self._now(), updated_at=self._now())
            self.links[order_id] = link
        return link

    async def update(self, order_id: int, **fields: Any) -> Optional[AmoLink]:
        link = self.links.get(order_id)
        if link is None:
            return None
        self.links[order_id] = replace(link, **fields, updated_at=self._now())
        return self.links[order_id]

    async def mark_step(self, order_id: int, step: str) -> None:
        link = self.links[order_id]
        checklist = dict(link.checklist)
        checklist.setdefault(step, self._now().isoformat())
        self.links[order_id] = replace(link, checklist=checklist, updated_at=self._now())

    async def log(self, order_id: int, action: str, *, dry_run: bool,
                  entity: Optional[str] = None, amo_id: Optional[int] = None,
                  payload: Optional[Any] = None) -> None:
        self.actions.append({"order_id": order_id, "action": action, "dry_run": dry_run,
                             "entity": entity, "amo_id": amo_id, "payload": payload})

    async def update_and_log(self, order_id: int, *, action: str, dry_run: bool,
                             entity: Optional[str] = None, amo_id: Optional[int] = None,
                             payload: Optional[Any] = None, **fields: Any) -> Optional[AmoLink]:
        """Обновить связку и записать решение в журнал — одним вызовом.

        В памяти настоящей транзакции нет, но порядок соблюдён: если работы
        уже нет и обновление не удалось, строки в журнале тоже не будет
        (задача 8, ТЗ 2026-09-21-evening-summary-rework.md).
        """
        updated = await self.update(order_id, **fields)
        if updated is not None:
            await self.log(order_id, action, dry_run=dry_run, entity=entity,
                           amo_id=amo_id, payload=payload)
        return updated

    async def taken_leads(self, phone10: str, exclude_order_id: int) -> set[int]:
        taken: set[int] = set()
        for link in self.links.values():
            if link.phone10 != phone10 or link.order_id == exclude_order_id:
                continue
            taken.update(value for value in (link.primary_lead_id, link.real_lead_id) if value)
        return taken


class PgLinkStore:
    """Боевое хранилище: схема `adminbot` того же Postgres, что и у бота.

    Таблица задаётся при сборке: у заказов химчистки и у уборок они разные,
    потому что номера работ в базе бота пересекаются. Движок про это не знает —
    он получает хранилище и работает с ним одинаково.
    """

    def __init__(self, pool: asyncpg.Pool, *, links_table: str = db.LINKS_TABLE,
                 actions_table: str = db.ACTIONS_TABLE) -> None:
        self._pool = pool
        self._links = links_table
        self._actions = actions_table

    async def get(self, order_id: int) -> Optional[AmoLink]:
        return await db.get_link(self._pool, order_id, table=self._links)

    async def create(self, order_id: int, phone10: Optional[str]) -> AmoLink:
        return await db.create_link(self._pool, order_id, phone10, table=self._links)

    async def update(self, order_id: int, **fields: Any) -> Optional[AmoLink]:
        return await db.update_link(self._pool, order_id, table=self._links, **fields)

    async def mark_step(self, order_id: int, step: str) -> None:
        await db.mark_checklist_step(self._pool, order_id, step, table=self._links)

    async def log(self, order_id: int, action: str, *, dry_run: bool,
                  entity: Optional[str] = None, amo_id: Optional[int] = None,
                  payload: Optional[Any] = None) -> None:
        await db.log_action(self._pool, order_id=order_id, action=action, dry_run=dry_run,
                            amo_entity=entity, amo_id=amo_id, payload=payload,
                            table=self._actions)

    async def update_and_log(self, order_id: int, *, action: str, dry_run: bool,
                             entity: Optional[str] = None, amo_id: Optional[int] = None,
                             payload: Optional[Any] = None, **fields: Any) -> Optional[AmoLink]:
        return await db.update_link_and_log(
            self._pool, order_id, table=self._links, actions_table=self._actions,
            action=action, dry_run=dry_run, entity=entity, amo_id=amo_id,
            payload=payload, **fields)

    async def taken_leads(self, phone10: str, exclude_order_id: int) -> set[int]:
        return await db.fetch_taken_lead_ids(self._pool, phone10, exclude_order_id,
                                             table=self._links)


class PgCleaningLinkStore(PgLinkStore):
    """То же хранилище, но для уборок клининг-контура."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__(pool, links_table=db.CLEANING_LINKS_TABLE,
                         actions_table=db.CLEANING_ACTIONS_TABLE)
