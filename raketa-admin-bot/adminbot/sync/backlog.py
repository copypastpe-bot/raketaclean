"""Хвост непроведённых заказов: сначала показать план, потом провести.

Владелец не должен подписываться под тем, чего не видел. Поэтому работа
с хвостом идёт в два шага:

1) предпросмотр — робот читает amoCRM и считает, что сделал бы с каждым заказом.
   Ни CRM, ни собственное состояние робота при этом не меняются: черновик
   считается на отдельном клиенте-репетиции и на хранилище в памяти;
2) «Поехали» — те же заказы проходят ещё раз, но уже боевым движком, который
   пишет и в CRM, и в базу.

Два разных движка, а не переключаемый флаг у одного: иначе наблюдатель, работающий
в этот момент параллельно, случайно провёл бы в бою совсем другие заказы.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from adminbot.models import AmoLink, Order
from adminbot.phone import for_owner
from adminbot.sync.store import LinkStore, MemoryLinkStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannedOrder:
    """Один заказ хвоста и то, что робот с ним сделает (или уже сделал)."""

    order_id: int
    title: str                                       # человеческая шапка заказа
    status: str
    path: Optional[str]
    actions: tuple[tuple[str, Optional[int]], ...] = ()   # (действие, id объекта в амо)


class BacklogRunner:
    def __init__(
        self,
        *,
        fetch_orders: Callable[[], Awaitable[list[Order]]],
        rehearsal_engine: Callable[[LinkStore], Any],
        live_engine: Any,
    ) -> None:
        self.fetch_orders = fetch_orders
        self.rehearsal_engine = rehearsal_engine
        self.live_engine = live_engine
        # Черновик последнего предпросмотра — полезен в тестах и при разборе жалоб.
        self.last_rehearsal_store: Optional[MemoryLinkStore] = None

    async def preview(self) -> list[PlannedOrder]:
        """Посчитать план по хвосту, ничего не меняя."""
        orders = await self.fetch_orders()
        store = MemoryLinkStore()
        self.last_rehearsal_store = store
        engine = self.rehearsal_engine(store)

        plan: list[PlannedOrder] = []
        for order in orders:
            link = await engine.process_order(order)
            plan.append(_planned(order, link, store.actions))
        return plan

    async def run_live(self) -> list[PlannedOrder]:
        """Провести хвост по-настоящему. Вызывается только по кнопке владельца."""
        orders = await self.fetch_orders()
        if orders:
            log.warning("Боевой прогон хвоста: заказов %s", len(orders))

        done: list[PlannedOrder] = []
        for order in orders:
            link = await self.live_engine.process_order(order)
            done.append(_planned(order, link, getattr(self.live_engine.store, "actions", [])))
        return done


def _planned(order: Order, link: Optional[AmoLink], actions: list[dict]) -> PlannedOrder:
    own = tuple((row["action"], row.get("amo_id")) for row in actions
                if row["order_id"] == order.order_id)
    return PlannedOrder(
        order_id=order.order_id,
        title=order_title(order),
        status=link.status if link else "error",
        path=link.path if link else None,
        actions=own,
    )


def order_title(order: Order) -> str:
    """Шапка работы для владельца: с телефоном и датой, чтобы не искать в CRM."""
    parts = [f"{order.label} №{order.order_id}"]
    if order.client_name:
        parts.append(order.client_name)
    parts.append(for_owner(order.phone10))
    parts.append(f"{money(order.amount_total)} ₽")
    parts.append(f"заказ {order.created_at:%d.%m.%Y %H:%M}")
    return " · ".join(parts)


def money(amount) -> str:
    """5950 → «5 950»: как в чеке, а не как в базе."""
    return f"{int(amount):,}".replace(",", " ")
