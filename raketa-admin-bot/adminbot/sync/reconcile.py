"""Вечерняя сверка: в 21:00 МСК робот пересчитывает день и отчитывается.

Зачем она нужна, если есть наблюдатель. Наблюдатель работает «здесь и сейчас»
и может чего-то не увидеть: сервис перезапускали, амо лежала, заказ появился
задним числом. Сверка раз в сутки смотрит на весь хвост целиком и отвечает на
единственный вопрос владельца: «всё ли разобрано, и если нет — что осталось».

Форматированием текста этот модуль не занимается: он собирает объект-сводку,
а как её показать в Telegram — дело задачи 12 (`adminbot/tg/cards.py`).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Optional, Protocol

import asyncpg

from adminbot import db
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import AmoLink

log = logging.getLogger(__name__)

# Сколько заказ может «висеть» в работе, прежде чем это станет поводом для отчёта.
STALE_AFTER_SEC = 3600

# Окно суток для «Провёл из бота» (задача 2, ТЗ 2026-09-21): параметром, а не
# константой в теле запроса, — чтобы тест мог его подменить, не подделывая часы.
TOUCHED_WINDOW_HOURS = 24

# Пути, по которым робот довёл существующую сделку, и путь «создали с нуля».
_PROCESSED_PATHS = ("A", "B")
_CREATED_PATH = "C"
_OWNER_PATH = "done"          # владелец провёл сделку сам, робот только привязал заказ


@dataclass(frozen=True)
class OrderBrief:
    """Заказ в том виде, в каком он нужен сводке: номер, телефон, когда был."""

    order_id: int
    phone10: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Snapshot:
    """Срез состояния за период: что робот записал и какие заказы вообще есть."""

    links: tuple[AmoLink, ...]
    orders: tuple[OrderBrief, ...] = ()
    # Номера работ, упомянутых в журнале действий за окно суток (задача 2, ТЗ
    # 2026-09-21): уже свёрнуто в множество, несколько строк журнала на одно
    # проведение работы здесь не считаются отдельно. Поля связок для этого не
    # годятся — `updated_at` двигает любая правка, а не только завершение работы.
    touched_order_ids: frozenset[int] = frozenset()
    # Сколько раз владелец нажал «Сам разберусь» за окно суток (задача 8, ТЗ
    # 2026-09-21-evening-summary-rework.md) — уже готовое число из журнала
    # действий (`db.count_owner_handled`), а не множество: связка не различает
    # это нажатие и автоматическое already_done, свернуть их в build_summary
    # не из чего.
    handled_count: int = 0

    @property
    def order_ids(self) -> tuple[int, ...]:
        return tuple(order.order_id for order in self.orders)


@dataclass(frozen=True)
class SummaryRow:
    """Одна строка сводки — заказ и то, чем он закончился.

    Телефон и дата заказа здесь не для красоты: владелец читает сводку в телефоне
    и должен видеть, о ком речь, не открывая CRM.
    """

    order_id: int
    status: str
    path: Optional[str] = None
    lead_id: Optional[int] = None
    detail: Optional[str] = None      # текст ошибки или причина ожидания
    phone10: Optional[str] = None
    order_date: Optional[datetime] = None


@dataclass(frozen=True)
class DailySummary:
    """Итог дня для владельца."""

    processed: tuple[SummaryRow, ...] = ()      # робот довёл сделку до конца
    created: tuple[SummaryRow, ...] = ()        # робот создал сделку с нуля
    already_done: tuple[SummaryRow, ...] = ()   # было проведено владельцем вручную
    waiting_owner: tuple[SummaryRow, ...] = ()  # ждут решения владельца
    failed: tuple[SummaryRow, ...] = ()         # сбой робота (статус error)
    stale: tuple[SummaryRow, ...] = ()          # зависли дольше stale_after_sec
    in_flight: tuple[int, ...] = ()             # в работе прямо сейчас — это норма
    missed: tuple[SummaryRow, ...] = ()         # заказы, до которых робот не добрался
    total_orders: int = 0
    # Четыре числа вечерней сводки (задача 2, ТЗ 2026-09-21-evening-summary-rework.md).
    # «Завёл из календаря» сюда не входит: это не про amo_links/cleaning_links,
    # а про отдельную таблицу adminbot.gcal_events — собирается отдельно
    # (db.count_calendar_created) и подмешивается снаружи, не этой функцией.
    processed_today: int = 0    # «Провёл из бота»: путь A/B/C (не различаем — решение владельца), довели за окно суток
    # «Передано администратору» (задача 8): только amosync/amoclean — свой кусок
    # числа из этого потока, calendar-контур считается отдельно
    # (db.count_calendar_owner_handled) и суммируется снаружи, тем же приёмом,
    # что и «Завёл из календаря».
    handed_to_owner: int = 0
    waiting_address: int = 0    # «Ждут адрес»: путь C без адреса, накопленным итогом
    # Уборки клининг-контура за тот же день. Отдельная сводка, но внутри той же:
    # владелец должен получить одну картину дня, а не два сообщения подряд.
    # None — функция уборок выключена, и раздела в сообщении нет вовсе.
    cleaning: Optional["DailySummary"] = None

    @property
    def is_quiet(self) -> bool:
        """День без хвостов: владельцу делать нечего."""
        return not (self.waiting_owner or self.failed or self.stale or self.missed)


class SummarySource(Protocol):
    """Откуда сверка берёт срез состояния."""

    async def collect(self) -> Snapshot: ...


def next_run_at(now: datetime, hour_msk: int = 21) -> datetime:
    """Когда ближайшая сверка. Считаем по московскому времени, а не по времени сервера."""
    now_msk = now.astimezone(MOSCOW_TZ)
    target = now_msk.replace(hour=hour_msk, minute=0, second=0, microsecond=0)
    if target <= now_msk:
        target += timedelta(days=1)
    return target


def build_summary(snapshot: Snapshot, *, now: datetime,
                  stale_after_sec: int = STALE_AFTER_SEC) -> DailySummary:
    """Разложить срез состояния по понятным владельцу корзинам."""
    processed: list[SummaryRow] = []
    created: list[SummaryRow] = []
    already_done: list[SummaryRow] = []
    waiting_owner: list[SummaryRow] = []
    failed: list[SummaryRow] = []
    stale: list[SummaryRow] = []
    in_flight: list[int] = []

    by_id = {order.order_id: order for order in snapshot.orders}
    for link in snapshot.links:
        row = _row(link, by_id.get(link.order_id))
        if link.status == "done":
            if link.path == _OWNER_PATH:
                already_done.append(row)
            elif link.path == _CREATED_PATH:
                created.append(row)
            else:
                processed.append(row)
        elif link.status == "waiting_owner":
            waiting_owner.append(row)
        elif link.status == "error":
            failed.append(row)
        elif _is_stale(link, now, stale_after_sec):
            stale.append(row)
        else:
            in_flight.append(link.order_id)

    linked_ids = {link.order_id for link in snapshot.links}
    missed = tuple(_row_from_order(order) for order in snapshot.orders
                   if order.order_id not in linked_ids)

    # «Провёл из бота»: пути A, B и C здесь не различаются (решение владельца —
    # создание сделки с нуля не показываем отдельной строкой, но саму работу
    # робота в счётчик берём). Считаем только то, что журнал действий
    # подтверждает за окно суток.
    processed_today = sum(1 for row in processed + created
                          if row.order_id in snapshot.touched_order_ids)

    # «Ждут адрес»: весь накопленный хвост, без окна суток — то же условие,
    # по которому робот шлёт напоминания (sync/address_reminder.py,
    # db.fetch_links_needing_address_reminder): путь C, статус done, адреса нет.
    waiting_address = sum(
        1 for link in snapshot.links
        if link.status == "done" and link.path == _CREATED_PATH and link.deal_address is None
    )

    return DailySummary(
        processed=tuple(processed),
        created=tuple(created),
        already_done=tuple(already_done),
        waiting_owner=tuple(waiting_owner),
        failed=tuple(failed),
        stale=tuple(stale),
        in_flight=tuple(sorted(in_flight)),
        missed=missed,
        total_orders=len(set(snapshot.order_ids) | linked_ids),
        processed_today=processed_today,
        handed_to_owner=snapshot.handled_count,
        waiting_address=waiting_address,
    )


class Reconciler:
    """Ежевечерний обход: догнать пропущенное, потом отчитаться."""

    def __init__(
        self,
        *,
        watcher: Any,
        source: SummarySource,
        on_summary: Callable[[DailySummary], Awaitable[None]],
        calendar_watcher: Optional[Any] = None,
        on_calendar: Optional[Callable[[Any], Awaitable[None]]] = None,
        cleaning_watcher: Optional[Any] = None,
        cleaning_source: Optional[SummarySource] = None,
        hour_msk: int = 21,
        stale_after_sec: int = STALE_AFTER_SEC,
        now: Callable[[], datetime] = lambda: datetime.now(MOSCOW_TZ),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.watcher = watcher
        self.source = source
        self.on_summary = on_summary
        self.calendar_watcher = calendar_watcher
        self.on_calendar = on_calendar
        # Уборки идут в том же вечернем сообщении, что и заказы: одна картина дня.
        self.cleaning_watcher = cleaning_watcher
        self.cleaning_source = cleaning_source
        self.hour_msk = hour_msk
        self.stale_after_sec = stale_after_sec
        self.now = now
        self.sleep = sleep

    async def run_once(self) -> DailySummary:
        """Догоняющий проход, затем сводка владельцу.

        Порядок важен: если сначала посчитать, а потом дообработать, сводка
        сообщит о пропусках, которых через секунду уже не будет.
        """
        await self.watcher.tick()
        snapshot = await self.source.collect()
        summary = build_summary(snapshot, now=self.now(), stale_after_sec=self.stale_after_sec)
        summary = replace(summary, cleaning=await self._cleaning_summary())
        await self.on_summary(summary)
        await self._report_calendar()
        return summary

    async def _cleaning_summary(self) -> Optional[DailySummary]:
        """Уборки за тот же день. Функция выключена — раздела нет вовсе.

        Сбой здесь не должен съесть сводку по заказам: она — главное сообщение
        вечера, а уборки к ней добавляются.
        """
        if self.cleaning_source is None:
            return None
        try:
            if self.cleaning_watcher is not None:
                await self.cleaning_watcher.tick()
            return build_summary(await self.cleaning_source.collect(), now=self.now(),
                                 stale_after_sec=self.stale_after_sec)
        except Exception:                              # noqa: BLE001
            log.exception("Вечерний проход по уборкам не удался")
            return None

    async def _report_calendar(self) -> None:
        """Отдельная строка про календарь — если функция вообще включена.

        Сбой здесь не должен съесть вечернюю сводку по заказам: она уже ушла,
        и календарь — дополнение к ней, а не её часть.
        """
        if self.calendar_watcher is None or self.on_calendar is None:
            return
        try:
            report = await self.calendar_watcher.tick()
            if not report.paused:
                await self.on_calendar(report)
        except Exception:                              # noqa: BLE001
            log.exception("Вечерний проход по календарю не удался")

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            delay = (next_run_at(self.now(), self.hour_msk) - self.now()).total_seconds()
            await self.sleep(max(delay, 0))
            try:
                await self.run_once()
            except Exception:                          # noqa: BLE001
                log.exception("Вечерняя сверка не удалась")


class PgSummarySource:
    """Боевой срез: заказы бота с начала хвоста и всё, что робот по ним записал.

    `touched_window_hours`/`now` — окно суток для «Провёл из бота» (задача 2):
    свои, а не общие с `Reconciler`, чтобы источник среза был самодостаточен
    и тест мог подменить и час, и окно, не трогая ничего вокруг.
    """

    def __init__(self, bot_pool: asyncpg.Pool, own_pool: asyncpg.Pool, backlog_from: date, *,
                 touched_window_hours: int = TOUCHED_WINDOW_HOURS,
                 now: Callable[[], datetime] = lambda: datetime.now(MOSCOW_TZ)) -> None:
        self.bot_pool = bot_pool
        self.own_pool = own_pool
        self.backlog_from = backlog_from
        self.touched_window_hours = touched_window_hours
        self.now = now

    async def collect(self) -> Snapshot:
        orders = await db.fetch_orders_since(self.bot_pool, self.backlog_from)
        links = await db.fetch_links_for_orders(
            self.own_pool, [order.order_id for order in orders])
        since = self.now() - timedelta(hours=self.touched_window_hours)
        touched = await db.fetch_touched_order_ids(self.own_pool, since)
        # «Передано администратору» (задача 8): то же окно суток, что и у
        # «Провёл из бота» — владелец 21.09 решил считать одним приёмом.
        handled = await db.count_owner_handled(self.own_pool, since)
        return Snapshot(
            links=tuple(links),
            orders=tuple(OrderBrief(order.order_id, order.phone10, order.created_at)
                         for order in orders),
            touched_order_ids=touched,
            handled_count=handled,
        )


class PgCleaningSummarySource:
    """Тот же срез, но по уборкам: своя таблица бота и своя таблица связок."""

    def __init__(self, bot_pool: asyncpg.Pool, own_pool: asyncpg.Pool, backlog_from: date, *,
                 touched_window_hours: int = TOUCHED_WINDOW_HOURS,
                 now: Callable[[], datetime] = lambda: datetime.now(MOSCOW_TZ)) -> None:
        self.bot_pool = bot_pool
        self.own_pool = own_pool
        self.backlog_from = backlog_from
        self.touched_window_hours = touched_window_hours
        self.now = now

    async def collect(self) -> Snapshot:
        orders = await db.fetch_cleaning_orders_since(self.bot_pool, self.backlog_from)
        links = await db.fetch_links_for_orders(
            self.own_pool, [order.order_id for order in orders],
            table=db.CLEANING_LINKS_TABLE)
        since = self.now() - timedelta(hours=self.touched_window_hours)
        touched = await db.fetch_touched_order_ids(
            self.own_pool, since, table=db.CLEANING_ACTIONS_TABLE)
        handled = await db.count_owner_handled(
            self.own_pool, since, table=db.CLEANING_ACTIONS_TABLE)
        return Snapshot(
            links=tuple(links),
            orders=tuple(OrderBrief(order.order_id, order.phone10, order.created_at)
                         for order in orders),
            touched_order_ids=touched,
            handled_count=handled,
        )


def _row(link: AmoLink, order: Optional[OrderBrief] = None) -> SummaryRow:
    return SummaryRow(
        order_id=link.order_id,
        status=link.status,
        path=link.path,
        lead_id=link.real_lead_id or link.primary_lead_id,
        detail=link.last_error or _WAIT_REASONS.get(link.status),
        phone10=(order.phone10 if order else None) or link.phone10 or None,
        order_date=order.created_at if order else None,
    )


def _row_from_order(order: OrderBrief) -> SummaryRow:
    """Заказ, до которого робот не добрался: привязки нет, данные есть."""
    return SummaryRow(order_id=order.order_id, status="missed",
                      phone10=order.phone10, order_date=order.created_at)


_WAIT_REASONS = {
    "waiting_salesbot": "сейлзбот не создал автосделку",
    "in_progress": "работа не доведена до конца",
    "new": "заказ не начат",
}


def _is_stale(link: AmoLink, now: datetime, stale_after_sec: int) -> bool:
    """Заказ висит в работе дольше положенного."""
    updated = link.updated_at
    if updated is None:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=MOSCOW_TZ)
    return (now - updated).total_seconds() > stale_after_sec
