"""Напоминание владельцу: сделка заведена с нуля, а адреса в ней всё ещё нет.

Отдельный цикл, а не часть наблюдателя (`sync/watcher.py`): тот доводит заказы
до статуса «done» и после этого к ним не возвращается — «done» заказов в его
источнике работы просто больше нет (задача 4 из ТЗ 2026-09-16 живёт в рабочем
боте и здесь ни при чём). Этот цикл, наоборот, интересуется только «done»
заказами пути C (создали сделку с нуля), у которых `deal_address` пусто.

Устройство то же, что у почты владельца (`tg/outbox.py`): период считается не
самим циклом, а по отметке в базе (`address_reminder_sent_at`), поэтому частота
опроса (`poll_interval_sec`) не обязана совпадать с сутками — она просто не
должна быть реже. Кнопки и текст карточки — дело `tg/cards.py`, доставка —
дело `OwnerMail`; этот модуль только решает, кому пора напомнить и сколько
раз уже напомнили.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional, Protocol

from adminbot import db
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import AmoLink

log = logging.getLogger(__name__)

# Решение владельца 16.09: раз в сутки, не больше семи раз — дальше робот
# замолкает сам. Опрос очереди чаще суток не нужен, поэтому час — за глаза.
DEFAULT_CAP = 7
DEFAULT_POLL_INTERVAL_SEC = 3600


class ReminderSource(Protocol):
    """Откуда цикл берёт связки, которым пора напомнить."""

    async def due(self) -> list[AmoLink]: ...


class AddressReminder:
    """Раз в сутки, пока не заполнят или не скажут «не напоминать» — не больше `cap` раз."""

    def __init__(
        self,
        *,
        source: ReminderSource,
        store: Any,
        on_reminder: Callable[[AmoLink, int], Awaitable[Any]],
        cap: int = DEFAULT_CAP,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(MOSCOW_TZ),
    ) -> None:
        self.source = source
        self.store = store
        self.on_reminder = on_reminder
        self.cap = cap
        self.poll_interval_sec = poll_interval_sec
        self.sleep = sleep
        self.now = now

    async def tick(self) -> int:
        """Один проход. Возвращает, скольким связкам напомнили."""
        due = await self.source.due()
        sent = 0
        for link in due:
            try:
                await self._remind(link)
                sent += 1
            except Exception:                          # noqa: BLE001 — один сбой не должен стопорить остальных
                log.exception("Заказ №%s: напоминание про адрес не отправлено", link.order_id)
        return sent

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except Exception:                          # noqa: BLE001
                log.exception("Напоминания про адрес: проход не удался")
            await self.sleep(self.poll_interval_sec)

    # --- внутреннее ---

    async def _remind(self, link: AmoLink) -> None:
        count = (link.address_reminder_count or 0) + 1
        await self.on_reminder(link, count)
        now = self.now()

        if count >= self.cap:
            # Потолок достигнут этим самым напоминанием — молчим сами и
            # оставляем в журнале, чтобы случай можно было найти потом.
            await self.store.log(link.order_id, "address_reminder_capped", dry_run=False,
                                 payload={"count": count})
            await self.store.update(link.order_id, address_reminder_count=count,
                                    address_reminder_sent_at=now, address_reminder_muted=True)
            log.info("Заказ №%s: напоминаний про адрес было %s — замолкаю сам",
                     link.order_id, count)
        else:
            await self.store.update(link.order_id, address_reminder_count=count,
                                    address_reminder_sent_at=now)


class PgReminderSource:
    """Боевой источник: связки химчистки или уборок — задаётся таблицей."""

    def __init__(self, pool: Any, *, table: str, cap: int = DEFAULT_CAP) -> None:
        self._pool = pool
        self._table = table
        self.cap = cap

    async def due(self) -> list[AmoLink]:
        return await db.fetch_links_needing_address_reminder(
            self._pool, cap=self.cap, table=self._table)
