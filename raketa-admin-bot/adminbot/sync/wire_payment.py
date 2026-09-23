"""Доводка сделки после поступления оплаты по счёту (задача 11, ТЗ 2026-09-22).

Отдельный цикл, а не часть наблюдателя (`sync/watcher.py`): тот доводит заказы
до «done» и после этого к ним не возвращается — «Заказ выполнен» с неоплаченным
счётом уже done, и `PgOrderSource` больше его не отдаёт. Этот цикл, наоборот,
интересуется только такими «done»-связками: источник сам находит их по
`public.orders` (способ оплаты, `awaiting_wire_payment`) и связкам, ещё не
доведённым (`payment_synced_at IS NULL`), и отдаёт готовые пары (заказ, связка) —
устройство то же, что у `sync/address_reminder.py`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Optional, Protocol

import asyncpg

from adminbot import db
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import AmoLink, Order
from adminbot.sync.engine import WirePaymentResult

log = logging.getLogger(__name__)

# Операция не срочная — владелец не ждёт её секундами, а амо не стоит дёргать
# лишний раз (та же логика, что у напоминания про адрес).
DEFAULT_POLL_INTERVAL_SEC = 900


class WirePaymentSource(Protocol):
    """Откуда цикл берёт заказы, готовые к доводке."""

    async def due(self) -> list[tuple[Order, AmoLink]]: ...


class WirePaymentSync:
    """Раз в `poll_interval_sec` — доводит все «due» сделки, отчитывается о каждой."""

    def __init__(
        self,
        *,
        source: WirePaymentSource,
        engine: Any,
        on_synced: Optional[Callable[[Order, AmoLink, WirePaymentResult], Awaitable[Any]]] = None,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(MOSCOW_TZ),
    ) -> None:
        self.source = source
        self.engine = engine
        # Отчёт владельцу — только когда сделка реально доведена до конца
        # (стадия переведена в «выполнено и оплата получена»): пример письма
        # в ТЗ ровно про этот случай, а «сделка уже финальная» — тихая правка.
        self.on_synced = on_synced
        self.poll_interval_sec = poll_interval_sec
        self.sleep = sleep
        self.now = now

    async def tick(self) -> int:
        """Один проход. Возвращает, сколько сделок доведено."""
        due = await self.source.due()
        synced = 0
        for order, link in due:
            try:
                result = await self.engine.process_payment(order, link)
            except Exception:                          # noqa: BLE001 — один сбой не должен стопорить остальных
                log.exception("%s №%s: доводка оплаты по счёту не удалась",
                             order.label, order.order_id)
                continue
            synced += 1
            if result is not None and result.stage_moved and self.on_synced is not None:
                await self._report(order, link, result)
        return synced

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except Exception:                          # noqa: BLE001
                log.exception("Доводка оплаты по счёту: проход не удался")
            await self.sleep(self.poll_interval_sec)

    # --- внутреннее ---

    async def _report(self, order: Order, link: AmoLink, result: WirePaymentResult) -> None:
        """Сообщение владельцу не должно ронять проход: Telegram бывает недоступен."""
        try:
            await self.on_synced(order, link, result)
        except Exception:                              # noqa: BLE001
            log.exception("%s №%s: отчёт о доводке оплаты не ушёл", order.label, order.order_id)


class PgWirePaymentSource:
    """Боевой источник: заказы по счёту, уже оплаченные, с недоведённой связкой.

    Два узких запроса (`db.py`), join — в Python: сперва номера заказов,
    подходящих по способу оплаты и `awaiting_wire_payment` (`bot_pool`), потом
    связки `amo_links` с этими номерами, ещё не доведённые (`own_pool`).
    Полные `Order` для отчёта и для `Engine.update_lead` берём тем же
    `fetch_orders_by_ids`, что и наблюдатель.
    """

    def __init__(self, bot_pool: asyncpg.Pool, own_pool: asyncpg.Pool, backlog_from: date) -> None:
        self.bot_pool = bot_pool
        self.own_pool = own_pool
        self.backlog_from = backlog_from

    async def due(self) -> list[tuple[Order, AmoLink]]:
        wire_ids = await db.fetch_wire_paid_order_ids(self.bot_pool, self.backlog_from)
        if not wire_ids:
            return []
        links = await db.fetch_links_needing_wire_payment_sync(self.own_pool, wire_ids)
        if not links:
            return []
        orders = await db.fetch_orders_by_ids(self.bot_pool, [link.order_id for link in links])
        by_id = {order.order_id: order for order in orders}
        return [(by_id[link.order_id], link) for link in links if link.order_id in by_id]
