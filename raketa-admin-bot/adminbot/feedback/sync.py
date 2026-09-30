"""Цикл «оценка клиента → задачи в CRM» (ТЗ 2026-09-28, задача 3).

Рабочий бот запрашивает у клиента оценку заказа и пишет ответ в
`public.orders` (`rating_score`, `rating_comment`, `rating_replied_at`) —
это остаётся как есть, не трогаем. amoCRM сама ставит задачу «Повторный
заказ» (id 2270746), когда сделка реализации переходит в «Успешно
реализовано»; этот цикл читает оценённые заказы (`adminbot.feedback.store`)
и по каждому решает, что сделать в CRM:

- оценка 5 → закрыть «Повторный заказ»;
- оценка 1–4 → комментарий в сделку, задача «Связаться» на ответственного
  (срок — сутки от постановки) и закрытие «Повторного заказа»;
- «Повторный заказ» ещё не появилась (амо не успела) → ждать, но не дольше
  30 дней после ответа клиента;
- «Повторный заказ» уже закрыта руками: при 5 — ничего, при 1–4 — всё равно
  комментарий и «Связаться» (решение владельца 6);
- старая оценка (до включения режима), у которой задачи уже нет или она
  закрыта, — не трогаем вовсе (решение владельца 8).

Устройство — как у `promo_callback/sync.py`: источник, цикл, отчёт, своё
состояние в схеме `adminbot` (миграция 019), у репетиции и у боя свои строки
и своя закладка `started_at`. Строка состояния — по ключу `(kind, order_id)`
(миграция 020): заказ №12 и уборка №12 — разные работы. В отличие от промо-отклика, «Связаться» может
понадобиться раньше, чем «Повторный заказ» вообще появится в CRM, поэтому
статусов у заказа не два, а несколько (см. `feedback/models.py`).

Репетиция читает CRM по-настоящему (клиент репетиции с `dry_run=True`
ничего не пишет, но методы вызываются те же) и вместо `done`/`contact_set`
ставит `dry_run` с одним письмом `on_rehearsal`, если по заказу нашлось хоть
одно действие для записи; ожидание и `skipped` — без писем (принято
координатором при написании ТЗ).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence

from adminbot.amo import ids
from adminbot.feedback.models import (
    MODE_LIVE,
    MODE_REHEARSAL,
    OPEN_STATUSES,
    STATUS_CONTACT_SET,
    STATUS_DONE,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_SKIPPED,
    FeedbackKey,
    FeedbackState,
    RatedOrder,
)
from adminbot.tg.feedback_cards import contact_task_text, feedback_result_text, note_text

log = logging.getLogger(__name__)

# Проход раз в 15 минут — оценка не срочная, амо не стоит дёргать чаще
# (принято координатором, п.1 брифа задачи).
DEFAULT_POLL_INTERVAL_SEC = 900
# Сколько проходов подряд может упасть один заказ, прежде чем робот сдастся
# и попросит владельца сделать руками.
MAX_ATTEMPTS = 3
# Не больше 20 заказов за проход — первый проход разбирает накопленное
# частями, не нагружая CRM (принято координатором).
BATCH_LIMIT = 20
# «Повторный заказ» появляется в CRM не сразу (оплата по счёту — с задержкой);
# ждём до 30 дней после ответа клиента, потом отступаемся (решение владельца).
FEEDBACK_TASK_WAIT = timedelta(days=30)
# Срок задачи «Связаться» — плюс сутки от момента постановки (решение 5).
CONTACT_DEADLINE = timedelta(hours=24)

# Статусы, которыми `tick()` считает заказ доведённым до конца прохода.
_FINAL_STATUSES = frozenset((STATUS_DONE, STATUS_SKIPPED, STATUS_DRY_RUN))


class FeedbackSource(Protocol):
    """Откуда цикл берёт оценённые заказы (own_pool + bot_pool, только чтение)."""

    async def rated_orders(self) -> list[RatedOrder]: ...


class FeedbackStore(Protocol):
    """Что робот помнит о заказах (схема `adminbot`). Всё — в пределах режима."""

    async def started_at(self, mode: str) -> Optional[datetime]: ...

    async def save_started_at(self, mode: str, when: datetime) -> None: ...

    async def states(self, mode: str) -> dict[FeedbackKey, FeedbackState]: ...

    async def register(self, mode: str, keys: Sequence[FeedbackKey]) -> None: ...

    async def update(self, kind: str, order_id: int, mode: str, **fields: Any) -> None: ...


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


class FeedbackSync:
    """Раз в `poll_interval_sec` — оценённые заказы без строки или ещё открытые."""

    def __init__(
        self,
        *,
        source: FeedbackSource,
        store: FeedbackStore,
        amo: Any,
        dry_run: bool = True,
        on_rehearsal: Optional[Callable[[RatedOrder, list[str]], Awaitable[Any]]] = None,
        on_failure: Optional[Callable[[RatedOrder, str], Awaitable[Any]]] = None,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        now: Callable[[], datetime] = _default_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.source = source
        self.store = store
        # Репетиция получает клиента репетиции: читает CRM по-настоящему,
        # писать не может (`_perform` в амо-клиенте с dry_run=True возвращает
        # неисполненный Intent). Сам цикл вызывает те же методы в обоих режимах.
        self.amo = amo
        self.dry_run = dry_run
        self.mode = MODE_REHEARSAL if dry_run else MODE_LIVE
        self.on_rehearsal = on_rehearsal
        self.on_failure = on_failure
        self.poll_interval_sec = poll_interval_sec
        self.now = now
        self.sleep = sleep

    async def tick(self) -> int:
        """Один проход. Возвращает, сколько заказов доведено до конечного статуса."""
        started = await self.store.started_at(self.mode)
        moment = self.now()
        if started is None:
            # Первый проход режима: закладка — этот самый момент. Старые
            # оценки (до неё) берём в работу только если задача ещё открыта
            # (решение владельца 8) — это решает `_handle` через `is_new`.
            started = moment
            await self.store.save_started_at(self.mode, started)

        orders = await self.source.rated_orders()
        states = await self.store.states(self.mode)

        candidates = [
            order for order in orders
            if order.key not in states or states[order.key].status in OPEN_STATUSES
        ]
        new_keys = [order.key for order in candidates if order.key not in states]
        if new_keys:
            await self.store.register(self.mode, new_keys)

        candidates.sort(key=lambda order: order.replied_at)
        batch = candidates[:BATCH_LIMIT]

        done = 0
        for order in batch:
            state = states.get(order.key) or FeedbackState(order_id=order.order_id,
                                                            mode=self.mode, kind=order.kind)
            status = await self._handle(order, state, started, moment)
            if status in _FINAL_STATUSES:
                done += 1
        return done

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except Exception:                                # noqa: BLE001
                log.exception("Повторный заказ: проход не удался")
            await self.sleep(self.poll_interval_sec)

    # --- один заказ ---

    async def _handle(self, order: RatedOrder, state: FeedbackState, started: datetime,
                      moment: datetime) -> Optional[str]:
        """Довести заказ до статуса. None — сбой (следующий проход повторит)."""
        try:
            tasks = await self.amo.get_lead_tasks_of_type(order.lead_id, ids.TASK_TYPE_FEEDBACK)
        except Exception as exc:                             # noqa: BLE001 — сбой CRM
            await self._fail(order, state, exc)
            return None

        open_ = [task for task in tasks if not task.get("is_completed")]
        closed_by_hand = bool(tasks) and not open_
        is_new = order.replied_at >= started
        expired = moment > order.replied_at + FEEDBACK_TASK_WAIT

        try:
            if not is_new and not open_:
                # Старая оценка без открытой задачи — не трогаем совсем
                # (решение владельца 8): ни CRM, ни письма.
                await self._reset_and_save(state, status=STATUS_SKIPPED)
                status = STATUS_SKIPPED
            else:
                status = await self._process(order, state, open_, closed_by_hand, expired,
                                             moment)
        except Exception as exc:                             # noqa: BLE001 — сбой CRM/базы
            await self._fail(order, state, exc)
            return None

        log.info("Заказ №%s (сделка %s), оценка %s: %s",
                order.order_id, order.lead_id, order.score, status)
        return status

    async def _process(self, order: RatedOrder, state: FeedbackState, open_: list[dict],
                       closed_by_hand: bool, expired: bool, moment: datetime) -> str:
        """Собственно решения 5 и 6. В репетиции те же вызовы идут в клиент репетиции."""
        if order.score == 5:
            if open_:
                for task in open_:
                    await self.amo.complete_task(task["id"], feedback_result_text(5))
                return await self._finish(order, state, STATUS_DONE,
                                          ['закрыл бы «Повторный заказ»'])
            if closed_by_hand or expired:
                # Закрыта руками — при 5 ничего не делаем (решение 6). Истёк
                # срок ожидания — отступаемся. Ни то ни другое не требует
                # записи в CRM, поэтому статус общий для обоих режимов.
                await self._reset_and_save(state, status=STATUS_SKIPPED)
                return STATUS_SKIPPED
            # Задачи ещё нет и срок не истёк — ждём, статус не меняем.
            await self._reset_attempts_only(state)
            return state.status

        # Оценка 1–4: «Связаться» и комментарий ставятся сразу, не дожидаясь
        # «Повторного заказа» (решение владельца 5), даже если он уже закрыт
        # руками (решение 6).
        current = state
        actions: list[str] = []
        if current.contact_task_id is None:
            lead = await self.amo.get_lead(order.lead_id)
            responsible = (lead or {}).get("responsible_user_id")
            deadline = int((moment + CONTACT_DEADLINE).timestamp())
            intent = await self.amo.create_task(
                order.lead_id, task_type_id=ids.TASK_TYPE_CONTACT,
                text=contact_task_text(order), complete_till=deadline,
                responsible_user_id=responsible)
            if self.dry_run:
                contact_task_id = 0
            elif intent.entity_id is not None:
                contact_task_id = intent.entity_id
            else:
                # Амо поставила задачу, но номер в ответе не пришёл. -1 — признак
                # «поставлена, номер неизвестен»: None здесь повторил бы постановку
                # каждый проход (спам задачами) — замечание ревью, задача 3.
                contact_task_id = -1
                log.warning("Заказ №%s (сделка %s): «Связаться» поставлена, но amoCRM "
                           "не вернула номер задачи", order.order_id, order.lead_id)
            current = await self._save(current, contact_task_id=contact_task_id)
            actions.append('поставил бы «Связаться»')
        if not current.note_added:
            position = ("closed_now" if open_ else
                        "closed_by_hand" if closed_by_hand else "not_yet")
            await self.amo.add_note(order.lead_id, note_text(order, feedback_task=position))
            current = await self._save(current, note_added=True)
            actions.append('написал бы комментарий')

        if open_:
            for task in open_:
                await self.amo.complete_task(task["id"], feedback_result_text(order.score))
            actions.append('закрыл бы «Повторный заказ»')
            return await self._finish(order, current, STATUS_DONE, actions)
        if closed_by_hand or expired:
            # Закрыта руками — закрывать нечего, но контакт с клиентом уже
            # налажен. Истёк срок — отступаемся, закрывать уже некого.
            return await self._finish(order, current, STATUS_DONE, actions)
        return await self._finish(order, current, STATUS_CONTACT_SET, actions)

    async def _finish(self, order: RatedOrder, state: FeedbackState, live_status: str,
                      actions: list[str]) -> str:
        """Итог разбора одного заказа: бой — статус как решили; репетиция —
        `dry_run` с письмом, если было хоть одно действие, иначе без записей.
        """
        if not self.dry_run:
            await self._reset_and_save(state, status=live_status)
            return live_status
        if not actions:
            await self._reset_attempts_only(state)
            return state.status
        await self._report_rehearsal(order, actions)
        await self._reset_and_save(state, status=STATUS_DRY_RUN)
        return STATUS_DRY_RUN

    async def _save(self, state: FeedbackState, **fields: Any) -> FeedbackState:
        await self.store.update(state.kind, state.order_id, state.mode, **fields)
        return replace(state, **fields)

    async def _reset_and_save(self, state: FeedbackState, **fields: Any) -> FeedbackState:
        """Успех обнуляет счётчик сбоев (принято координатором по разделу 8 брифа)."""
        if state.attempts or state.last_error:
            fields = {**fields, "attempts": 0, "last_error": None}
        return await self._save(state, **fields)

    async def _reset_attempts_only(self, state: FeedbackState) -> None:
        if state.attempts or state.last_error:
            await self._save(state, attempts=0, last_error=None)

    async def _fail(self, order: RatedOrder, state: FeedbackState, exc: Exception) -> None:
        """Сбой заказа: следующий проход повторит; третий подряд — `failed` и письмо."""
        attempts = state.attempts + 1
        error = str(exc) or type(exc).__name__
        log.warning("Заказ №%s (сделка %s): попытка %s/%s не удалась: %s",
                    order.order_id, order.lead_id, attempts, MAX_ATTEMPTS, error)
        if attempts < MAX_ATTEMPTS:
            await self._save_quietly(state, attempts=attempts, last_error=error)
            return
        await self._save_quietly(state, attempts=attempts, last_error=error,
                                 status=STATUS_FAILED)
        await self._report_failure(order, error)

    async def _save_quietly(self, state: FeedbackState, **fields: Any) -> None:
        """Отметка сбоя сама не должна ронять проход: база бывает недоступна."""
        try:
            await self.store.update(state.kind, state.order_id, state.mode, **fields)
        except Exception:                                    # noqa: BLE001
            log.exception("Заказ №%s: не смог записать отметку о сбое", state.order_id)

    async def _report_rehearsal(self, order: RatedOrder, actions: list[str]) -> None:
        if self.on_rehearsal is None:
            return
        try:
            await self.on_rehearsal(order, actions)
        except Exception:                                    # noqa: BLE001
            log.exception("Заказ №%s: отчёт репетиции не ушёл", order.order_id)

    async def _report_failure(self, order: RatedOrder, error: str) -> None:
        if self.on_failure is None:
            return
        try:
            await self.on_failure(order, error)
        except Exception:                                    # noqa: BLE001
            log.exception("Заказ №%s: письмо о сбое не ушло", order.order_id)
