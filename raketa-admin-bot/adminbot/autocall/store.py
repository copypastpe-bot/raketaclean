"""Что робот помнит о цепочках автозвонка.

Ключ — идентификатор сделки амо. Наблюдатель видит одну и ту же заявку при
каждом опросе, поэтому повторный проход не заводит вторую цепочку: робот
продолжает с того места, где остановился.

Здесь же живёт курсор опроса амо (с какого момента создания читать сделки).
У него особое правило: **в репетиции курсор нельзя писать в базу**. Иначе
боевой запуск начнёт не со своего момента включения, а с того, что робот уже
посмотрел вхолостую, — та же ошибка, что с письмами партнёра 2026-08-26.
Поэтому в репетиции работает хранилище в памяти, и курсор живёт до перезапуска.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from adminbot import db
from adminbot.autocall.chain import ACTIVE_STATUSES  # словарь статусов живёт в chain.py
from adminbot.models import AutocallLead


def _reject_unknown_fields(fields: dict) -> None:
    """Память отвергает те же поля, что база: источник один — db.py.

    На памяти держится репетиция; будь она добрее боевого хранилища,
    ошибка в имени поля всплыла бы только при боевом запуске.
    """
    unknown = set(fields) - db._UPDATABLE_AUTOCALL_FIELDS
    if unknown:
        raise ValueError(f"Недопустимые поля цепочки автозвонка: {sorted(unknown)}")


class AutocallStore(Protocol):
    """Что движку и наблюдателю нужно от хранилища — и ничего сверх того."""

    async def get(self, lead_id: int) -> Optional[AutocallLead]: ...

    async def create(self, lead_id: int, *, phone10: Optional[str],
                     **fields: Any) -> AutocallLead: ...

    async def update(self, lead_id: int, **fields: Any) -> Optional[AutocallLead]: ...

    async def due(self, now: datetime) -> list[AutocallLead]: ...

    async def cursor(self) -> Optional[datetime]: ...

    async def save_cursor(self, created_from: datetime) -> None: ...

    async def log_action(self, lead_id: int, action: str, *, dry_run: bool,
                         payload: Optional[Any] = None) -> None: ...

    async def actions_for(self, lead_id: int) -> list[dict]: ...


class MemoryAutocallStore:
    """Хранилище в памяти: для репетиций и тестов. В базу не пишет ничего.

    Это принцип проекта «репетиция не оставляет следов»: общее с боевым режимом
    состояние (особенно курсор) украло бы у него работу.
    """

    def __init__(self, now: Optional[Any] = None) -> None:
        self.leads: dict[int, AutocallLead] = {}
        self.actions: list[dict] = []
        self._cursor: Optional[datetime] = None
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def get(self, lead_id: int) -> Optional[AutocallLead]:
        return self.leads.get(lead_id)

    async def create(self, lead_id: int, *, phone10: Optional[str] = None,
                     **fields: Any) -> AutocallLead:
        """Завести цепочку — при желании сразу с готовым статусом.

        Наблюдатель заводит заявку без телефона сразу финалом (status="gave_up")
        одной командой, а не create → update: так сбой хранилища между шагами
        не оставляет "queued"-запись без номера, которую due() отдавал бы
        движку вечно. "queued" по умолчанию, но fields может его переопределить —
        отсюда словарь, а не keyword status="queued" вперемешку с **fields
        (то же имя дважды было бы TypeError).
        """
        _reject_unknown_fields(fields)
        lead = self.leads.get(lead_id)
        if lead is None:
            lead = AutocallLead(lead_id=lead_id, phone10=phone10,
                                created_at=self._now(), updated_at=self._now(),
                                **{"status": "queued", **fields})
            self.leads[lead_id] = lead
        return lead

    async def update(self, lead_id: int, **fields: Any) -> Optional[AutocallLead]:
        _reject_unknown_fields(fields)
        lead = self.leads.get(lead_id)
        if lead is None:
            return None
        self.leads[lead_id] = replace(lead, **fields, updated_at=self._now())
        return self.leads[lead_id]

    async def due(self, now: datetime) -> list[AutocallLead]:
        """Незаконченные цепочки, у которых срок подошёл. Просроченные — первыми.

        Без срока (next_action_at пуст) — действовать сразу: цепочку только завели.
        """
        ready = [lead for lead in self.leads.values()
                 if lead.status in ACTIVE_STATUSES
                 and (lead.next_action_at is None or lead.next_action_at <= now)]
        return sorted(ready, key=lambda lead: (lead.next_action_at is not None,
                                               lead.next_action_at or now, lead.lead_id))

    async def cursor(self) -> Optional[datetime]:
        return self._cursor

    async def save_cursor(self, created_from: datetime) -> None:
        self._cursor = created_from

    async def log_action(self, lead_id: int, action: str, *, dry_run: bool,
                         payload: Optional[Any] = None) -> None:
        self.actions.append({"lead_id": lead_id, "action": action,
                             "dry_run": dry_run, "payload": payload})

    async def actions_for(self, lead_id: int) -> list[dict]:
        """Что робот делал по заявке — свежее первым, как в базе."""
        return [row for row in reversed(self.actions) if row["lead_id"] == lead_id]

    def actions_of(self, action: str) -> list[dict]:
        return [row for row in self.actions if row["action"] == action]


class PgAutocallStore:
    """Боевое хранилище цепочек автозвонка: схема `adminbot`."""

    def __init__(self, pool) -> None:
        self._pool = pool

    async def get(self, lead_id: int) -> Optional[AutocallLead]:
        return await db.get_autocall_lead(self._pool, lead_id)

    async def create(self, lead_id: int, *, phone10: Optional[str] = None,
                     **fields: Any) -> AutocallLead:
        return await db.create_autocall_lead(self._pool, lead_id, phone10=phone10, **fields)

    async def update(self, lead_id: int, **fields: Any) -> Optional[AutocallLead]:
        return await db.update_autocall_lead(self._pool, lead_id, **fields)

    async def due(self, now: datetime) -> list[AutocallLead]:
        return await db.fetch_due_autocall_leads(self._pool, ACTIVE_STATUSES, now)

    async def cursor(self) -> Optional[datetime]:
        return await db.get_autocall_cursor(self._pool)

    async def save_cursor(self, created_from: datetime) -> None:
        await db.save_autocall_cursor(self._pool, created_from)

    async def log_action(self, lead_id: int, action: str, *, dry_run: bool,
                         payload: Optional[Any] = None) -> None:
        await db.log_autocall_action(self._pool, lead_id=lead_id, action=action,
                                     dry_run=dry_run, payload=payload)

    async def actions_for(self, lead_id: int) -> list[dict]:
        return await db.fetch_autocall_actions(self._pool, lead_id)
