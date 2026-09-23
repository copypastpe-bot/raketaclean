"""Напоминание владельцу: номер и имя записи не сходятся с контактом сделки.

Устройство то же, что у напоминания про адрес (`sync/address_reminder.py`,
образец задачи 7 ТЗ 2026-09-16): период считается не самим циклом, а по
отметке в базе (`contact_reminder_sent_at`), потолок в `cap` напоминаний —
дальше робот замолкает сам. Отдельный модуль, а не общее ядро (решение
координатора 22.09: вынос дороже двух часов при таком размере задачи) —
долг записан в ТЗ и отчёте задачи 5.

Перед каждым повтором цикл перечитывает контакт дочки в amoCRM заново
(`_fetch_contact` + `contact_mismatch_text` из `gcal/engine.py`) — владелец
мог поправить контакт прямо в CRM, минуя кнопку «Я разобрался», и тогда
дальнейшие напоминания про уже исправленное расхождение были бы лишними.
Сошлось — снимаем `contact_mismatch` и молчим; запись перестаёт попадать
в `due()` сама, без отдельного флага «мute» (тот резервируется под решение
владельца «Я разобрался»).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional, Protocol

from adminbot import db
from adminbot.amo.fields import MOSCOW_TZ, lead_contact_ids
from adminbot.gcal.engine import contact_mismatch_text, contact_resolved
from adminbot.models import CalendarLink

log = logging.getLogger(__name__)

# То же решение владельца, что и у напоминания про адрес: раз в сутки, потолок 7.
DEFAULT_CAP = 7
DEFAULT_POLL_INTERVAL_SEC = 3600


async def fetch_deal_contact(amo: Any, link: CalendarLink) -> Optional[dict]:
    """Контакт дочки (а если её ещё нет — лида воронки 1) заново из CRM.

    Тот же приём, что у `adminbot.amo.fields.fetch_lead_address`: связке не
    верим на слово, сделка могла измениться с прошлого раза.
    """
    lead_id = link.real_lead_id or link.primary_lead_id
    if lead_id is None:
        return None
    lead = await amo.get_lead(lead_id)
    contact_ids = lead_contact_ids(lead)
    if not contact_ids:
        return None
    return await amo.get_contact(contact_ids[0])


class ReminderSource(Protocol):
    """Откуда цикл берёт записи, которым пора напомнить."""

    async def due(self) -> list[CalendarLink]: ...


class ContactReminder:
    """Раз в сутки, пока не сойдётся или не скажут «разобрался» — не больше `cap` раз."""

    def __init__(
        self,
        *,
        source: ReminderSource,
        store: Any,
        amo: Any,
        on_reminder: Callable[[CalendarLink, int], Awaitable[Any]],
        cap: int = DEFAULT_CAP,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(MOSCOW_TZ),
    ) -> None:
        self.source = source
        self.store = store
        self.amo = amo
        self.on_reminder = on_reminder
        self.cap = cap
        self.poll_interval_sec = poll_interval_sec
        self.sleep = sleep
        self.now = now

    async def tick(self) -> int:
        """Один проход. Возвращает, скольким записям напомнили."""
        due = await self.source.due()
        sent = 0
        for link in due:
            try:
                if await self._remind(link):
                    sent += 1
            except Exception:                          # noqa: BLE001 — один сбой не должен стопорить остальных
                log.exception("Календарь, запись %s: напоминание про контакт не отправлено",
                             link.event_id)
        return sent

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except Exception:                          # noqa: BLE001
                log.exception("Напоминания про контакт: проход не удался")
            await self.sleep(self.poll_interval_sec)

    # --- внутреннее ---

    async def _remind(self, link: CalendarLink) -> bool:
        """Одна запись. Возвращает True, если напоминание отправлено.

        Перед отправкой перечитываем контакт дочки в CRM заново — та же
        причина, что у напоминания про адрес: владелец мог поправить контакт
        прямо в сделке, не нажимая кнопку. `contact_resolved` отличает «точно
        сошлось» (молчим и снимаем расхождение) от «сверить сейчас нечем»
        (дочки ещё нет, контакт не читается) — во втором случае это не повод
        замолчать, напоминание уходит с прежним текстом.
        """
        contact = await fetch_deal_contact(self.amo, link)
        if contact_resolved(link.client_name, link.phone10, contact):
            await self.store.update(link.event_id, contact_mismatch=None)
            log.info("Календарь, запись %s: контакт сошёлся при перепроверке — "
                     "напоминание не шлю, замолкаю", link.event_id)
            return False

        mismatch = contact_mismatch_text(link.client_name, link.phone10, contact) \
            or link.contact_mismatch
        if mismatch != link.contact_mismatch:
            await self.store.update(link.event_id, contact_mismatch=mismatch)
            link = await self.store.get(link.event_id) or link

        count = (link.contact_reminder_count or 0) + 1
        await self.on_reminder(link, count)
        now = self.now()

        if count >= self.cap:
            await self.store.log(link.event_id, "contact_reminder_capped", dry_run=False,
                                 payload={"count": count})
            await self.store.update(link.event_id, contact_reminder_count=count,
                                    contact_reminder_sent_at=now, contact_reminder_muted=True)
            log.info("Календарь, запись %s: напоминаний про контакт было %s — замолкаю сам",
                     link.event_id, count)
        else:
            await self.store.update(link.event_id, contact_reminder_count=count,
                                    contact_reminder_sent_at=now)
        return True


class PgContactReminderSource:
    """Боевой источник: записи календаря с расхождением контакта."""

    def __init__(self, own_pool: Any, *, cap: int = DEFAULT_CAP) -> None:
        self._pool = own_pool
        self.cap = cap

    async def due(self) -> list[CalendarLink]:
        return await db.fetch_calendar_links_needing_contact_reminder(self._pool, cap=self.cap)
