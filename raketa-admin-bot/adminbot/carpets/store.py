"""Что робот помнит о строках отчёта партнёра.

Ключ — номер заказа в CRM партнёра (колонка «#»). Он сквозной и не меняется,
поэтому месячный свод, где те же заказы приходят второй раз, ничего не испортит:
робот увидит, что строка уже разобрана, и пройдёт мимо.

Устройство то же, что у хранилища уборки: узкий набор операций, боевая версия
поверх Postgres и двойник в памяти для репетиций и тестов.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from adminbot import db
from adminbot.models import CarpetLink


class CarpetStore(Protocol):
    """Что движку нужно от хранилища — и ничего сверх того."""

    async def get(self, partner_id: int) -> Optional[CarpetLink]: ...

    async def create(self, partner_id: int, phone10: Optional[str],
                     source_file: Optional[str] = None) -> CarpetLink: ...

    async def update(self, partner_id: int, **fields: Any) -> Optional[CarpetLink]: ...

    async def mark_step(self, partner_id: int, step: str) -> None: ...

    async def log(self, partner_id: int, action: str, *, dry_run: bool,
                  entity: Optional[str] = None, amo_id: Optional[int] = None,
                  payload: Optional[Any] = None) -> None: ...

    async def taken_leads(self, phone10: str, exclude_partner_id: int) -> set[int]: ...


class MemoryCarpetStore:
    """Хранилище в памяти: для репетиций и тестов. В базу не пишет ничего."""

    def __init__(self, now: Optional[Any] = None) -> None:
        self.links: dict[int, CarpetLink] = {}
        self.actions: list[dict] = []
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def get(self, partner_id: int) -> Optional[CarpetLink]:
        return self.links.get(partner_id)

    async def create(self, partner_id: int, phone10: Optional[str],
                     source_file: Optional[str] = None) -> CarpetLink:
        link = self.links.get(partner_id)
        if link is None:
            link = CarpetLink(partner_id=partner_id, phone10=phone10 or "", status="new",
                              source_file=source_file, checklist={},
                              created_at=self._now(), updated_at=self._now())
            self.links[partner_id] = link
        return link

    async def update(self, partner_id: int, **fields: Any) -> Optional[CarpetLink]:
        link = self.links.get(partner_id)
        if link is None:
            return None
        self.links[partner_id] = replace(link, **fields, updated_at=self._now())
        return self.links[partner_id]

    async def mark_step(self, partner_id: int, step: str) -> None:
        link = self.links[partner_id]
        checklist = dict(link.checklist)
        checklist.setdefault(step, self._now().isoformat())
        self.links[partner_id] = replace(link, checklist=checklist, updated_at=self._now())

    async def log(self, partner_id: int, action: str, *, dry_run: bool,
                  entity: Optional[str] = None, amo_id: Optional[int] = None,
                  payload: Optional[Any] = None) -> None:
        self.actions.append({"partner_id": partner_id, "action": action, "dry_run": dry_run,
                             "entity": entity, "amo_id": amo_id, "payload": payload})

    async def taken_leads(self, phone10: str, exclude_partner_id: int) -> set[int]:
        return {link.lead_id for link in self.links.values()
                if link.phone10 == phone10 and link.partner_id != exclude_partner_id
                and link.lead_id}

    def actions_of(self, action: str) -> list[dict]:
        return [row for row in self.actions if row["action"] == action]


class PgCarpetStore:
    """Боевое хранилище ковровых привязок: схема `adminbot`."""

    def __init__(self, pool) -> None:
        self._pool = pool

    async def get(self, partner_id: int) -> Optional[CarpetLink]:
        return await db.get_carpet_link(self._pool, partner_id)

    async def create(self, partner_id: int, phone10: Optional[str],
                     source_file: Optional[str] = None) -> CarpetLink:
        return await db.create_carpet_link(self._pool, partner_id, phone10, source_file)

    async def update(self, partner_id: int, **fields: Any) -> Optional[CarpetLink]:
        return await db.update_carpet_link(self._pool, partner_id, **fields)

    async def mark_step(self, partner_id: int, step: str) -> None:
        await db.mark_carpet_step(self._pool, partner_id, step)

    async def log(self, partner_id: int, action: str, *, dry_run: bool,
                  entity: Optional[str] = None, amo_id: Optional[int] = None,
                  payload: Optional[Any] = None) -> None:
        await db.log_carpet_action(self._pool, partner_id=partner_id, action=action,
                                   dry_run=dry_run, amo_entity=entity, amo_id=amo_id,
                                   payload=payload)

    async def taken_leads(self, phone10: str, exclude_partner_id: int) -> set[int]:
        return await db.fetch_carpet_taken_leads(self._pool, phone10, exclude_partner_id)
