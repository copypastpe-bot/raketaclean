"""Наблюдатель календаря: обмен с Google → разбор → сделки в amoCRM.

Три правила, которые определяют устройство прохода:

1. **Первый обмен только запоминает.** В календаре уже лежат будущие заказы —
   постоянные клиенты записываются за три недели. Робот берёт в работу только то,
   что появилось после включения (решение владельца 8), иначе разом заведёт
   сделки по всем заказам, которые владелец давно ведёт сам.
2. **Закладка сохраняется в конце прохода.** Сохранить её раньше — потерять
   изменения, которые не успели обработаться: следующий обмен их уже не покажет.
3. **Одна плохая запись не роняет проход.** Остальные обрабатываются, сбойная
   остаётся в работе и вернётся в следующий раз.

Незавершённые записи (ожидание автосделки, ошибки, неотправленные карточки)
берутся не из календаря, а из своего хранилища: обмен приносит только изменения,
а ждать автосделку робот может дольше, чем живёт одна пачка изменений.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable, Optional

from adminbot.gcal.event import EventKind, ParsedEvent, parse_event

log = logging.getLogger(__name__)

# Пока есть незаконченная работа (ждём автосделку), следующий проход делаем скоро.
QUICK_RETRY_SEC = 60


@dataclass(frozen=True)
class CalendarTickReport:
    """Итог одного прохода — для журнала, вечерней сводки и команды /status."""

    paused: bool = False
    changes: int = 0                                  # сколько записей принёс обмен
    known: int = 0                                    # запомнено при первом включении
    processed: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    questions: tuple[str, ...] = ()                   # записи, по которым ушёл вопрос
    unknown_districts: tuple[str, ...] = ()           # приставки, которых робот не знает
    failures: tuple[tuple[str, str], ...] = ()
    full_resync: bool = False


class CalendarWatcher:
    def __init__(
        self,
        *,
        calendar: Any,
        engine: Any,
        store: Any,
        sync_from: date,
        is_enabled: Optional[Callable[[], Any]] = None,
        poll_interval_sec: int = 300,
        on_question: Optional[Callable[[Any], Awaitable[Optional[int]]]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.calendar = calendar
        self.engine = engine
        self.store = store
        self.sync_from = sync_from
        self.is_enabled = is_enabled
        self.poll_interval_sec = poll_interval_sec
        self.on_question = on_question
        self.sleep = sleep
        self.last_report: Optional[CalendarTickReport] = None

    async def tick(self) -> CalendarTickReport:
        """Один проход: забрать изменения и доделать незавершённое."""
        if not await self._enabled():
            return self._remember(CalendarTickReport(paused=True))

        sync_token, saved_from = await self.store.cursor()
        sync_from = saved_from or self.sync_from
        first_run = sync_token is None and saved_from is None

        batch = await self.calendar.fetch(sync_token=sync_token, sync_from=sync_from)

        statuses: Counter[str] = Counter()
        questions: list[str] = []
        failures: list[tuple[str, str]] = []
        unknown: list[str] = []
        known = 0

        for raw in batch.events:
            parsed = parse_event(raw)
            if parsed.unknown_district and parsed.unknown_district not in unknown:
                unknown.append(parsed.unknown_district)

            if first_run:
                await self._remember_known(parsed)
                known += 1
                continue

            await self._handle(parsed, statuses, questions, failures)

        # Незавершённое из прошлых проходов: ожидание автосделки, ошибки, вопросы.
        for link in await self.store.pending():
            if not link.event_data:
                continue                              # разбора нет — продолжать нечем
            await self._handle(ParsedEvent.from_dict(link.event_data),
                               statuses, questions, failures)

        # Закладка — только теперь, когда пачка разобрана.
        await self.store.save_cursor(batch.sync_token or sync_token, sync_from=sync_from)

        return self._remember(CalendarTickReport(
            changes=len(batch.events), known=known, processed=sum(statuses.values()),
            by_status=dict(statuses), questions=tuple(questions),
            unknown_districts=tuple(unknown), failures=tuple(failures),
            full_resync=batch.full_resync))

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except Exception:                          # noqa: BLE001
                log.exception("Проход по календарю не удался")
            await self.sleep(self._next_delay())

    def _next_delay(self) -> float:
        """Обычно ждём положенное, но не тогда, когда работа не закончена."""
        counts = dict(getattr(self.last_report, "by_status", {}) or {})
        unfinished = sum(counts.get(status, 0) for status in
                         ("waiting_salesbot", "in_progress", "new", "error", "closing"))
        return QUICK_RETRY_SEC if unfinished else self.poll_interval_sec

    # --- внутреннее ---

    async def _remember_known(self, parsed: ParsedEvent) -> None:
        """Запись была в календаре до включения — только помним о ней.

        Помним затем, чтобы её последующее удаление не выглядело загадкой
        и чтобы правка такой записи не завела сделку задним числом.
        """
        link = await self.store.get(parsed.event_id)
        if link is not None:
            return
        await self.store.create(parsed.event_id, kind=parsed.kind.value,
                                phone10=parsed.phone10, order_date=parsed.order_date,
                                event_data=parsed.to_dict())
        await self.store.update(parsed.event_id, status="skipped",
                                skip_reason="была в календаре до включения")

    async def _handle(self, parsed: ParsedEvent, statuses: Counter,
                      questions: list, failures: list) -> None:
        try:
            link = await self.engine.process(parsed)
        except Exception as exc:                       # noqa: BLE001 — запись не роняет проход
            log.exception("Календарь, запись %s: проход прерван", parsed.event_id)
            failures.append((parsed.event_id, f"{type(exc).__name__}: {exc}"))
            return

        if link is None:
            return
        statuses[link.status] += 1
        await self._save_event_data(parsed, link)
        if await self._maybe_ask(link):
            questions.append(parsed.event_id)

    async def _save_event_data(self, parsed: ParsedEvent, link: Any) -> None:
        """Держать разбор рядом с записью: им продолжают незаконченную цепочку."""
        if parsed.kind is EventKind.CANCELLED:
            return
        if getattr(link, "event_data", None) == parsed.to_dict():
            return
        await self.store.update(parsed.event_id, event_data=parsed.to_dict())

    async def _maybe_ask(self, link: Any) -> bool:
        """Карточка уходит один раз: повтор дублировал бы вопрос каждый проход."""
        if link.status != "waiting_owner" or self.on_question is None:
            return False
        if link.question_msg_id:
            return False

        message_id = await self.on_question(link)
        if not message_id:
            log.warning("Календарь, запись %s: карточку отправить не удалось",
                        link.event_id)
            return False
        await self.store.update(link.event_id, question_msg_id=int(message_id))
        return True

    def _remember(self, report: CalendarTickReport) -> CalendarTickReport:
        self.last_report = report
        return report

    async def _enabled(self) -> bool:
        if self.is_enabled is None:
            return True
        result = self.is_enabled()
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)
