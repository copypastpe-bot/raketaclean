"""Что робот помнит о записях календаря.

Ключ — идентификатор записи в Google. Он вечный и переживает правки, поэтому
повторный обмен ничего не портит: робот видит, что запись уже в работе, и идёт
дальше с того места, где остановился.

Здесь же живёт закладка обмена. У неё особое правило: **в репетиции закладку
нельзя писать в базу**. Если записать, боевой запуск получит от Google «с
прошлого раза изменений нет» и пропустит всё, что робот посмотрел вхолостую, —
ровно та ошибка, которая случилась с письмами партнёра 2026-08-26.
Поэтому в репетиции работает хранилище в памяти, и закладка живёт до перезапуска.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any, Optional, Protocol

from adminbot import db
from adminbot.models import CalendarLink

# Статусы, по которым работа ещё не закончена: их наблюдатель добирает из базы.
# `waiting_owner` здесь тоже есть — движок по нему ничего не делает, но карточку
# может понадобиться дослать, если Telegram в прошлый раз не ответил.
ACTIVE_STATUSES: tuple[str, ...] = (
    "new", "in_progress", "waiting_salesbot", "waiting_owner", "closing", "error",
)


class CalendarStore(Protocol):
    """Что движку и наблюдателю нужно от хранилища — и ничего сверх того."""

    async def get(self, event_id: str) -> Optional[CalendarLink]: ...

    async def create(self, event_id: str, *, kind: str, phone10: Optional[str],
                     **fields: Any) -> CalendarLink: ...

    async def update(self, event_id: str, **fields: Any) -> Optional[CalendarLink]: ...

    async def mark_step(self, event_id: str, step: str) -> None: ...

    async def log(self, event_id: str, action: str, *, dry_run: bool,
                  entity: Optional[str] = None, amo_id: Optional[int] = None,
                  payload: Optional[Any] = None) -> None: ...

    async def taken_leads(self, phone10: str, exclude_event_id: str) -> set[int]: ...

    async def pending(self) -> list[CalendarLink]: ...

    async def find_by_question_msg(self, message_id: Optional[int]) -> Optional[CalendarLink]: ...

    async def forget(self, event_id: str) -> None: ...

    async def actions_for(self, event_id: str) -> list[dict]: ...

    async def cursor(self, calendar_id: str, *, inherit_legacy: bool = False,
                     ) -> tuple[Optional[str], Optional[date]]: ...

    async def save_cursor(self, calendar_id: str, sync_token: Optional[str],
                          *, sync_from: date) -> None: ...


class MemoryCalendarStore:
    """Хранилище в памяти: для репетиций и тестов. В базу не пишет ничего."""

    def __init__(self, now: Optional[Any] = None) -> None:
        self.links: dict[str, CalendarLink] = {}
        self.actions: list[dict] = []
        # Закладка на каждый календарь: у мебельного и уборочного свои пачки
        # изменений, общая закладка стирала бы одну другой.
        self._cursors: dict[str, tuple[Optional[str], Optional[date]]] = {}
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def get(self, event_id: str) -> Optional[CalendarLink]:
        return self.links.get(event_id)

    async def create(self, event_id: str, *, kind: str, phone10: Optional[str] = None,
                     **fields: Any) -> CalendarLink:
        link = self.links.get(event_id)
        if link is None:
            link = CalendarLink(event_id=event_id, kind=kind, status="new",
                                phone10=phone10, checklist={},
                                created_at=self._now(), updated_at=self._now(), **fields)
            self.links[event_id] = link
        return link

    async def update(self, event_id: str, **fields: Any) -> Optional[CalendarLink]:
        link = self.links.get(event_id)
        if link is None:
            return None
        self.links[event_id] = replace(link, **fields, updated_at=self._now())
        return self.links[event_id]

    async def mark_step(self, event_id: str, step: str) -> None:
        link = self.links[event_id]
        checklist = dict(link.checklist)
        checklist.setdefault(step, self._now().isoformat())
        self.links[event_id] = replace(link, checklist=checklist, updated_at=self._now())

    async def log(self, event_id: str, action: str, *, dry_run: bool,
                  entity: Optional[str] = None, amo_id: Optional[int] = None,
                  payload: Optional[Any] = None) -> None:
        self.actions.append({"event_id": event_id, "action": action, "dry_run": dry_run,
                             "entity": entity, "amo_id": amo_id, "payload": payload})

    async def taken_leads(self, phone10: str, exclude_event_id: str) -> set[int]:
        """Сделки, занятые ДРУГИМИ записями календаря.

        Заказы бота здесь намеренно не учитываются: заказ из бота и запись
        календаря — обычно один и тот же заказ, и привязка к одной сделке
        как раз правильна.
        """
        taken: set[int] = set()
        for link in self.links.values():
            if link.phone10 != phone10 or link.event_id == exclude_event_id:
                continue
            taken.update(lead for lead in (link.real_lead_id, link.primary_lead_id) if lead)
        return taken

    async def pending(self) -> list[CalendarLink]:
        return [link for link in self.links.values() if link.status in ACTIVE_STATUSES]

    async def find_by_question_msg(self, message_id: Optional[int]) -> Optional[CalendarLink]:
        """Запись, по которой владельцу отправлена именно эта карточка."""
        if message_id is None:
            return None
        return next((link for link in self.links.values()
                     if link.question_msg_id == int(message_id)), None)

    async def forget(self, event_id: str) -> None:
        """Забыть запись целиком — чтобы провести её заново с чистого листа.

        Нужно только для ручного разбора последствий: обычный ход работы
        полагается на чек-лист, который как раз не даёт делать одно дважды.
        """
        self.links.pop(event_id, None)

    async def cursor(self, calendar_id: str, *, inherit_legacy: bool = False,
                     ) -> tuple[Optional[str], Optional[date]]:
        return self._cursors.get(calendar_id, (None, None))

    async def save_cursor(self, calendar_id: str, sync_token: Optional[str],
                          *, sync_from: date) -> None:
        self._cursors[calendar_id] = (sync_token, sync_from)

    async def actions_for(self, event_id: str) -> list[dict]:
        return [row for row in self.actions if row.get("event_id") == event_id]

    def actions_of(self, action: str) -> list[dict]:
        return [row for row in self.actions if row["action"] == action]


class PgCalendarStore:
    """Боевое хранилище записей календаря: схема `adminbot`."""

    def __init__(self, pool) -> None:
        self._pool = pool

    async def get(self, event_id: str) -> Optional[CalendarLink]:
        return await db.get_calendar_link(self._pool, event_id)

    async def create(self, event_id: str, *, kind: str, phone10: Optional[str] = None,
                     **fields: Any) -> CalendarLink:
        return await db.create_calendar_link(self._pool, event_id, kind=kind,
                                             phone10=phone10, **fields)

    async def update(self, event_id: str, **fields: Any) -> Optional[CalendarLink]:
        return await db.update_calendar_link(self._pool, event_id, **fields)

    async def mark_step(self, event_id: str, step: str) -> None:
        await db.mark_calendar_step(self._pool, event_id, step)

    async def log(self, event_id: str, action: str, *, dry_run: bool,
                  entity: Optional[str] = None, amo_id: Optional[int] = None,
                  payload: Optional[Any] = None) -> None:
        await db.log_calendar_action(self._pool, event_id=event_id, action=action,
                                     dry_run=dry_run, amo_entity=entity, amo_id=amo_id,
                                     payload=payload)

    async def taken_leads(self, phone10: str, exclude_event_id: str) -> set[int]:
        return await db.fetch_calendar_taken_leads(self._pool, phone10, exclude_event_id)

    async def pending(self) -> list[CalendarLink]:
        return await db.fetch_pending_calendar_links(self._pool, ACTIVE_STATUSES)

    async def find_by_question_msg(self, message_id: Optional[int]) -> Optional[CalendarLink]:
        if message_id is None:
            return None
        return await db.find_calendar_link_by_question_msg(self._pool, int(message_id))

    async def forget(self, event_id: str) -> None:
        await db.delete_calendar_link(self._pool, event_id)

    async def actions_for(self, event_id: str) -> list[dict]:
        return await db.fetch_calendar_actions(self._pool, event_id)

    async def cursor(self, calendar_id: str, *, inherit_legacy: bool = False,
                     ) -> tuple[Optional[str], Optional[date]]:
        return await db.get_calendar_cursor(self._pool, calendar_id,
                                            inherit_legacy=inherit_legacy)

    async def save_cursor(self, calendar_id: str, sync_token: Optional[str],
                          *, sync_from: date) -> None:
        await db.save_calendar_cursor(self._pool, calendar_id, sync_token, sync_from)
