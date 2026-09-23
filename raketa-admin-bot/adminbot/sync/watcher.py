"""Наблюдатель: постоянный цикл, который доводит заказы до проведённых сделок.

Как это выглядит со стороны владельца: мастер закрыл заказ в рабочем боте —
через минуту сделка в amoCRM оформлена. Никаких кнопок нажимать не нужно.

Устройство цикла:
1) выключатель — если функция на паузе, тик не трогает ни базу, ни амо;
2) разбор удалений (необязательный шаг, ТЗ 2026-09-17, задача 5) — если задан,
   выполняется ДО выборки заказов: удалённый заказ должен освободить свою
   сделку раньше, чем робот увидит переоформленный заказ на том же месте;
3) источник работы — заказы, до которых робот ещё не добрался или не довёл;
4) движок — по одному заказу за раз, каждый в своей «песочнице»: сбой одного
   не отменяет остальные;
5) вопрос владельцу — если движок упёрся в неоднозначность, карточка уходит
   в Telegram РОВНО один раз (признак — записанный id сообщения).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Optional, Protocol

import asyncpg

from adminbot import db
from adminbot.amo.fields import MOSCOW_TZ
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
        on_done: Optional[Callable[[Order, AmoLink], Awaitable[None]]] = None,
        handle_deletions: Optional[Callable[[], Awaitable[Any]]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(MOSCOW_TZ),
    ) -> None:
        self.engine = engine
        self.source = source
        # Выключатель задаёт тот, кто собирает сервис: настройка окружения плюс
        # пауза владельца. Не задан — считаем, что раз объект создан, работать можно.
        self.is_enabled = is_enabled
        self.poll_interval_sec = poll_interval_sec
        self.on_question = on_question
        # Неделя наблюдения (решение владельца 2026-08-27): о каждом проведённом
        # заказе робот пишет владельцу сразу, со ссылкой на сделку. Отключается
        # снятием обработчика.
        self.on_done = on_done
        # Разбор удалённых заказов (ТЗ 2026-09-17 «удаление заказа освобождает
        # сделку», задача 5): необязательный шаг ПЕРЕД выборкой заказов, а не
        # свой цикл со своим таймером (решение владельца 4). Только так закрывается
        # гонка «удаление раньше нового заказа»: разбор обязан снять связку до
        # того, как `source.pending()` увидит переоформленный заказ. Не задан —
        # `tick()` ведёт себя ровно как раньше (существующие вызовы не меняются).
        self.handle_deletions = handle_deletions
        self.sleep = sleep
        self.now = now
        # Последний проход — чтобы владелец мог спросить /status и увидеть,
        # что робот действительно смотрит в базу, а не молча стоит.
        self.last_tick_at: Optional[datetime] = None
        self.last_report: Optional[TickReport] = None

    async def tick(self) -> TickReport:
        """Один проход: взять заказы и продвинуть каждый настолько, насколько можно."""
        if not await self._enabled():
            return self._remember(TickReport(paused=True))

        if self.handle_deletions is not None:
            await self._run_deletions()

        orders = await self.source.pending()
        statuses: Counter[str] = Counter()
        questions: list[int] = []
        failures: list[tuple[int, str]] = []

        for order in orders:
            try:
                link = await self.engine.process_order(order)
            except Exception as exc:                  # noqa: BLE001 — цикл не должен падать
                log.exception("%s №%s: проход прерван", order.label, order.order_id)
                failures.append((order.order_id, f"{type(exc).__name__}: {exc}"))
                continue

            if link is None:
                continue
            statuses[link.status] += 1
            if link.status == "done" and order.calendar_event_id:
                await self._link_calendar_order(order)
            if await self._maybe_ask(order, link):
                questions.append(order.order_id)
            elif link.status == "done" and self.on_done is not None:
                await self._report_done(order, link)

        return self._remember(TickReport(
            scanned=len(orders), by_status=dict(statuses),
            questions=tuple(questions), failures=tuple(failures)))

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

    def _remember(self, report: TickReport) -> TickReport:
        self.last_tick_at = self.now()
        self.last_report = report
        return report

    async def _enabled(self) -> bool:
        if self.is_enabled is None:
            return True
        result = self.is_enabled()
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)

    async def _run_deletions(self) -> None:
        """Сбой разбора удалений не должен останавливать доводку заказов."""
        try:
            await self.handle_deletions()
        except Exception:                                  # noqa: BLE001
            log.exception("Разбор удалённых заказов не удался")

    async def _report_done(self, order: Order, link: AmoLink) -> None:
        """Сообщение владельцу не должно ронять проход: Telegram бывает недоступен."""
        try:
            await self.on_done(order, link)
        except Exception:                              # noqa: BLE001
            log.exception("%s №%s: сообщение о работе не ушло", order.label, order.order_id)

    async def _link_calendar_order(self, order: Order) -> None:
        """После проведения — вписать номер заказа обратно в запись календаря
        (задача 10, ТЗ 2026-09-22): круг замкнут в обе стороны, рабочий бот по
        своему представлению `adminbot.calendar_jobs` увидит, что запись уже
        занята заказом. Сбой не должен ронять тик — то же правило, что у `_report_done`.
        """
        try:
            calendar_link = await self.engine.store.get_calendar_link(order.calendar_event_id)
            if calendar_link is not None and calendar_link.order_id is None:
                await self.engine.store.update_calendar_link(
                    order.calendar_event_id, order_id=order.order_id)
        except Exception:                              # noqa: BLE001
            log.exception("%s №%s: не удалось записать номер заказа в запись календаря",
                          order.label, order.order_id)

    async def _maybe_ask(self, order: Order, link: AmoLink) -> bool:
        """Отправить карточку-вопрос, если она ещё не отправлена. True — отправили."""
        if link.status != "waiting_owner" or self.on_question is None:
            return False
        if link.question_msg_id:
            return False                               # владелец уже спрошен

        message_id = await self.on_question(order, link)
        if not message_id:
            log.warning("%s №%s: карточку-вопрос отправить не удалось",
                        order.label, order.order_id)
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


class PgCleaningSource:
    """Тот же источник, но по уборкам клининг-контура.

    Читает `public.cleaning_orders` и свою таблицу связок: номера уборок и заказов
    химчистки пересекаются, и общая очередь путала бы их между собой.
    """

    def __init__(self, bot_pool: asyncpg.Pool, own_pool: asyncpg.Pool, backlog_from: date) -> None:
        self.bot_pool = bot_pool
        self.own_pool = own_pool
        self.backlog_from = backlog_from

    async def pending(self) -> list[Order]:
        fresh = await db.fetch_unprocessed_cleaning_orders(
            self.bot_pool, self.own_pool, self.backlog_from)
        active_ids = await db.fetch_link_ids_by_status(
            self.own_pool, ACTIVE_STATUSES, table=db.CLEANING_LINKS_TABLE)
        unfinished = await db.fetch_cleaning_orders_by_ids(self.bot_pool, active_ids)

        by_id = {order.order_id: order for order in unfinished}
        by_id.update({order.order_id: order for order in fresh})
        return [by_id[order_id] for order_id in sorted(by_id)]
