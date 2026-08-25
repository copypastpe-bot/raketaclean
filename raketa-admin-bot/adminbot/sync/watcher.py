"""Наблюдатель: постоянный цикл, который доводит заказы до проведённых сделок.

Как это выглядит со стороны владельца: мастер закрыл заказ в рабочем боте —
через минуту сделка в amoCRM оформлена. Никаких кнопок нажимать не нужно.

Устройство цикла:
1) выключатель — если функция на паузе, тик не трогает ни базу, ни амо;
2) источник работы — заказы, до которых робот ещё не добрался или не довёл;
3) движок — по одному заказу за раз, каждый в своей «песочнице»: сбой одного
   не отменяет остальные;
4) вопрос владельцу — если движок упёрся в неоднозначность, карточка уходит
   в Telegram РОВНО один раз (признак — записанный id сообщения).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable, Optional, Protocol

import asyncpg

from adminbot import db
from adminbot.models import AmoLink, Order

log = logging.getLogger(__name__)

# Статусы привязки, по которым работа ещё не закончена и заказ надо трогать снова.
# `waiting_owner` здесь тоже есть: движок по нему ничего не делает, но наблюдателю
# он нужен, чтобы дослать карточку-вопрос, если Telegram в прошлый раз не ответил.
ACTIVE_STATUSES: tuple[str, ...] = (
    "new", "in_progress", "waiting_salesbot", "error", "waiting_owner",
)


class OrderSource(Protocol):
    """Откуда наблюдатель берёт заказы «на сейчас»."""

    async def pending(self) -> list[Order]: ...


@dataclass(frozen=True)
class TickReport:
    """Итог одного прохода — для журнала и команды /status."""

    paused: bool = False
    scanned: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    questions: tuple[int, ...] = ()                  # заказы, по которым ушёл вопрос
    failures: tuple[tuple[int, str], ...] = ()       # (номер заказа, что случилось)


class Watcher:
    def __init__(
        self,
        *,
        engine: Any,
        source: OrderSource,
        is_enabled: Optional[Callable[[], Any]] = None,
        poll_interval_sec: int = 60,
        on_question: Optional[Callable[[Order, AmoLink], Awaitable[Optional[int]]]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.engine = engine
        self.source = source
        # Выключатель задаёт тот, кто собирает сервис: настройка окружения плюс
        # пауза владельца. Не задан — считаем, что раз объект создан, работать можно.
        self.is_enabled = is_enabled
        self.poll_interval_sec = poll_interval_sec
        self.on_question = on_question
        self.sleep = sleep

    async def tick(self) -> TickReport:
        """Один проход: взять заказы и продвинуть каждый настолько, насколько можно."""
        if not await self._enabled():
            return TickReport(paused=True)

        orders = await self.source.pending()
        statuses: Counter[str] = Counter()
        questions: list[int] = []
        failures: list[tuple[int, str]] = []

        for order in orders:
            try:
                link = await self.engine.process_order(order)
            except Exception as exc:                  # noqa: BLE001 — цикл не должен падать
                log.exception("Заказ №%s: проход прерван", order.order_id)
                failures.append((order.order_id, f"{type(exc).__name__}: {exc}"))
                continue

            if link is None:
                continue
            statuses[link.status] += 1
            if await self._maybe_ask(order, link):
                questions.append(order.order_id)

        return TickReport(scanned=len(orders), by_status=dict(statuses),
                          questions=tuple(questions), failures=tuple(failures))

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        """Вечный цикл. Останавливается по событию `stop` (используется при выключении)."""
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except Exception:                          # noqa: BLE001
                # База недоступна, сеть моргнула — переживём до следующего тика.
                log.exception("Тик наблюдателя не удался")
            await self.sleep(self.poll_interval_sec)

    # --- внутреннее ---

    async def _enabled(self) -> bool:
        if self.is_enabled is None:
            return True
        result = self.is_enabled()
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)

    async def _maybe_ask(self, order: Order, link: AmoLink) -> bool:
        """Отправить карточку-вопрос, если она ещё не отправлена. True — отправили."""
        if link.status != "waiting_owner" or self.on_question is None:
            return False
        if link.question_msg_id:
            return False                               # владелец уже спрошен

        message_id = await self.on_question(order, link)
        if not message_id:
            log.warning("Заказ №%s: карточку-вопрос отправить не удалось", order.order_id)
            return False
        await self.engine.store.update(order.order_id, question_msg_id=int(message_id))
        return True


class PgOrderSource:
    """Боевой источник работы: два узких запроса вместо перебора всей истории.

    Первый — новые заказы (в них робот ещё не заглядывал), второй — заказы
    с незакрытой привязкой. Поэтому объём работы за тик зависит от того, сколько
    осталось незавершённого, а не от того, как давно начался хвост.
    """

    def __init__(self, bot_pool: asyncpg.Pool, own_pool: asyncpg.Pool, backlog_from: date) -> None:
        self.bot_pool = bot_pool
        self.own_pool = own_pool
        self.backlog_from = backlog_from

    async def pending(self) -> list[Order]:
        fresh = await db.fetch_unprocessed_orders(self.bot_pool, self.own_pool, self.backlog_from)
        active_ids = await db.fetch_link_ids_by_status(self.own_pool, ACTIVE_STATUSES)
        unfinished = await db.fetch_orders_by_ids(self.bot_pool, active_ids)

        by_id = {order.order_id: order for order in unfinished}
        by_id.update({order.order_id: order for order in fresh})
        return [by_id[order_id] for order_id in sorted(by_id)]
