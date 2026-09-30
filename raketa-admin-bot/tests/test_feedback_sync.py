"""Оценка клиента → задачи в CRM (ТЗ 2026-09-28, задача 3).

Источник и хранилище — в памяти, CRM — двойник `FakeAmo` из `tests/fakes.py`,
как в `test_promo_callback.py`. Запросы к базе (реальный источник/хранилище)
проверяет `test_feedback_store.py`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from adminbot.amo import ids
from adminbot.amo.client import Intent
from adminbot.feedback.models import (
    KIND_CLEANING,
    KIND_ORDER,
    MODE_LIVE,
    MODE_REHEARSAL,
    OPEN_STATUSES,
    STATUS_CONTACT_SET,
    STATUS_DONE,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_NEW,
    STATUS_SKIPPED,
    FeedbackState,
    RatedOrder,
)
from adminbot.feedback.sync import FeedbackSync
from adminbot.tg.feedback_cards import (
    contact_task_text,
    failure_text,
    feedback_result_text,
    note_text,
    rehearsal_text,
)
from tests.fakes import FakeAmo

# 04.09.2026 12:46 МСК — момент из утверждённого примера текстов (решение 9).
REPLIED = datetime(2026, 9, 4, 9, 46, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)                # чуть позже ответа


class MemorySource:
    def __init__(self, *orders: RatedOrder) -> None:
        self.rows = {(order.kind, order.order_id): order for order in orders}

    def add(self, order: RatedOrder) -> None:
        self.rows[(order.kind, order.order_id)] = order

    async def rated_orders(self) -> list[RatedOrder]:
        return list(self.rows.values())


class MemoryStore:
    def __init__(self) -> None:
        self.started: dict[str, datetime] = {}
        self.rows: dict[tuple[str, int, str], FeedbackState] = {}

    async def started_at(self, mode: str) -> Optional[datetime]:
        return self.started.get(mode)

    async def save_started_at(self, mode: str, when: datetime) -> None:
        self.started.setdefault(mode, when)

    async def states(self, mode: str) -> dict[tuple[str, int], FeedbackState]:
        return {(kind, order_id): state
                for (kind, order_id, state_mode), state in self.rows.items()
                if state_mode == mode}

    async def register(self, mode: str, keys) -> None:
        for kind, order_id in keys:
            self.rows.setdefault((kind, order_id, mode),
                                 FeedbackState(order_id=order_id, mode=mode, kind=kind))

    async def update(self, kind: str, order_id: int, mode: str, **fields: Any) -> None:
        key = (kind, order_id, mode)
        self.rows[key] = replace(self.rows[key], **fields)

    def state(self, order_id: int, mode: str = MODE_LIVE, kind: str = KIND_ORDER) -> FeedbackState:
        return self.rows[(kind, order_id, mode)]


class Letters:
    def __init__(self) -> None:
        self.rehearsals: list[tuple[RatedOrder, list[str]]] = []
        self.failures: list[tuple[RatedOrder, str]] = []

    async def rehearsal(self, order, actions) -> None:
        self.rehearsals.append((order, list(actions)))

    async def failure(self, order, error) -> None:
        self.failures.append((order, error))


class Clock:
    """Часы движка: тесты двигают `value`, чтобы проверить сроки (24 ч, 30 дней)."""

    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


def _order(order_id: int, *, lead_id: Optional[int] = None, score: int = 5,
          comment: Optional[str] = None, replied_at: datetime = NOW,
          kind: str = KIND_ORDER) -> RatedOrder:
    return RatedOrder(order_id=order_id, lead_id=lead_id or (80000 + order_id), score=score,
                      comment=comment, replied_at=replied_at, kind=kind)


def _sync(source, store, amo, letters, *, dry_run: bool = False,
          now: Optional[Clock] = None) -> FeedbackSync:
    return FeedbackSync(source=source, store=store, amo=amo, dry_run=dry_run,
                        on_rehearsal=letters.rehearsal, on_failure=letters.failure,
                        now=now or Clock(NOW))


async def _armed(source, store, amo, letters, *, dry_run: bool = False,
                 now: Optional[Clock] = None) -> FeedbackSync:
    """Цикл после первого прохода: закладка уже стоит (started_at = NOW)."""
    sync = _sync(source, store, amo, letters, dry_run=dry_run, now=now)
    await sync.tick()
    return sync


def _lead(amo: FakeAmo, lead_id: int, *, responsible_user_id: int = 555) -> None:
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE,
                responsible_user_id=responsible_user_id)


# --- первый проход: закладка ---

async def test_first_tick_sets_started_at():
    source, store, amo, letters = MemorySource(), MemoryStore(), FakeAmo(), Letters()
    clock = Clock(NOW)
    sync = _sync(source, store, amo, letters, now=clock)

    await sync.tick()

    assert store.started[MODE_LIVE] == NOW
    assert amo.calls == []


# --- оценка 5 ---

async def test_score5_open_task_is_closed_with_result_text_and_done():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_id = 41000
    amo.add_task(lead_id, task_id=1, task_type_id=ids.TASK_TYPE_FEEDBACK)
    sync = await _armed(source, store, amo, letters)
    order = _order(601, lead_id=lead_id, score=5, replied_at=REPLIED - timedelta(minutes=5))
    source.add(order)

    assert await sync.tick() == 1

    assert amo.calls_of("complete_task") == [1]
    assert amo.task_results[1] == "🤖 Клиент оценил заказ в боте на 5."
    assert store.state(601).status == STATUS_DONE
    assert amo.calls_of("create_task") == []
    assert amo.calls_of("add_note") == []


async def test_score5_closed_by_hand_is_skipped_without_writes():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_id = 41001
    amo.add_task(lead_id, task_id=2, task_type_id=ids.TASK_TYPE_FEEDBACK)
    amo.task_results[2] = "закрыл руками"
    sync = await _armed(source, store, amo, letters)
    source.add(_order(602, lead_id=lead_id, score=5))

    assert await sync.tick() == 1

    assert store.state(602).status == STATUS_SKIPPED
    assert amo.calls_of("complete_task") == []
    assert amo.calls_of("create_task") == []
    assert amo.calls_of("add_note") == []


async def test_score5_without_task_waits_then_skips_after_30_days():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_id = 41002
    clock = Clock(NOW)
    sync = await _armed(source, store, amo, letters, now=clock)
    order = _order(603, lead_id=lead_id, score=5)
    source.add(order)

    assert await sync.tick() == 0
    assert store.state(603).status == STATUS_NEW
    assert amo.calls_of("complete_task") == []

    clock.value = order.replied_at + timedelta(days=31)
    assert await sync.tick() == 1
    assert store.state(603).status == STATUS_SKIPPED


# --- оценка 1–4 ---

async def test_score_low_open_task_sets_contact_note_and_closes():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_id = 41003
    _lead(amo, lead_id, responsible_user_id=777)
    amo.add_task(lead_id, task_id=3, task_type_id=ids.TASK_TYPE_FEEDBACK)
    sync = await _armed(source, store, amo, letters)
    order = _order(605, lead_id=lead_id, score=3, comment="3", replied_at=REPLIED)
    source.add(order)

    assert await sync.tick() == 1

    [task] = amo.calls_of("create_task")
    assert task["task_type_id"] == ids.TASK_TYPE_CONTACT
    assert task["text"] == "Клиент оценил заказ №605 на 3 — узнать, что не так."
    assert task["responsible_user_id"] == 777
    assert task["complete_till"] == int((NOW + timedelta(hours=24)).timestamp())

    [(note_lead_id, note)] = amo.calls_of("add_note")
    assert note_lead_id == lead_id
    assert note == (
        "🤖 Клиент оценил заказ №605 на 3 в боте (04.09 12:46).\n"
        "Ответ клиента: «3».\n"
        "Поставил задачу «Связаться», «Повторный заказ» закрыл."
    )

    assert amo.calls_of("complete_task") == [3]
    assert amo.task_results[3] == "🤖 Клиент оценил заказ в боте на 3, поставлена задача «Связаться»."
    state = store.state(605)
    assert state.status == STATUS_DONE
    assert state.contact_task_id is not None and state.note_added is True


async def test_score_low_closed_by_hand_still_sets_contact_and_note():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_id = 41004
    _lead(amo, lead_id)
    amo.add_task(lead_id, task_id=4, task_type_id=ids.TASK_TYPE_FEEDBACK)
    amo.task_results[4] = "закрыл руками"
    sync = await _armed(source, store, amo, letters)
    order = _order(606, lead_id=lead_id, score=2, comment="2")
    source.add(order)

    assert await sync.tick() == 1

    assert len(amo.calls_of("create_task")) == 1
    [(_, note)] = amo.calls_of("add_note")
    assert "уже был закрыт" in note
    assert amo.calls_of("complete_task") == []          # нечего закрывать — уже закрыта
    assert store.state(606).status == STATUS_DONE


async def test_score_low_without_task_sets_contact_set_and_waits():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_id = 41005
    _lead(amo, lead_id)
    sync = await _armed(source, store, amo, letters)
    order = _order(607, lead_id=lead_id, score=1, comment="1")
    source.add(order)

    assert await sync.tick() == 0

    [(_, note)] = amo.calls_of("add_note")
    assert "закрою, когда он появится" in note
    state = store.state(607)
    assert state.status == STATUS_CONTACT_SET
    assert state.contact_task_id is not None and state.note_added is True


async def test_next_tick_closes_once_task_appears_without_second_contact_or_note():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_id = 41006
    _lead(amo, lead_id)
    sync = await _armed(source, store, amo, letters)
    order = _order(608, lead_id=lead_id, score=4, comment="4")
    source.add(order)
    await sync.tick()                                    # contact_set, задачи ещё нет
    assert len(amo.calls_of("create_task")) == 1
    assert len(amo.calls_of("add_note")) == 1

    task_id = store.state(608).contact_task_id
    amo.add_task(lead_id, task_id=99, task_type_id=ids.TASK_TYPE_FEEDBACK)
    assert await sync.tick() == 1

    assert len(amo.calls_of("create_task")) == 1          # вторую не поставил
    assert len(amo.calls_of("add_note")) == 1              # второго комментария нет
    assert amo.calls_of("complete_task") == [99]
    assert store.state(608).status == STATUS_DONE
    assert store.state(608).contact_task_id == task_id


async def test_failure_after_contact_task_retry_does_not_create_second():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_id = 41007
    _lead(amo, lead_id)
    sync = await _armed(source, store, amo, letters)
    order = _order(609, lead_id=lead_id, score=3, comment="3")
    source.add(order)

    amo.fail_on = "add_note"
    assert await sync.tick() == 0
    state = store.state(609)
    assert state.contact_task_id is not None and state.attempts == 1
    assert len(amo.calls_of("create_task")) == 1

    amo.fail_on = None
    assert await sync.tick() == 0                          # задачи всё ещё нет — contact_set
    assert len(amo.calls_of("create_task")) == 1           # вторую не поставил
    assert store.state(609).status == STATUS_CONTACT_SET
    assert store.state(609).attempts == 0                   # успех обнулил счётчик


async def test_score_low_missing_entity_id_in_response_uses_sentinel_and_no_retry():
    """Ревью, замечание 1: амо поставила задачу, но номер в ответе не пришёл
    (entity_id=None) — раньше это писалось как contact_task_id=None, и на
    следующем проходе «Связаться» ставилась заново (спам задачами)."""
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_id = 41013
    _lead(amo, lead_id)
    created_calls: list[dict] = []

    async def create_task_no_entity_id(lead_id_arg, *, task_type_id, text, complete_till,
                                       responsible_user_id=None):
        created_calls.append({"task_type_id": task_type_id, "text": text,
                              "complete_till": complete_till,
                              "responsible_user_id": responsible_user_id})
        return Intent(action="create_task", entity="task", entity_id=None,
                     payload={}, performed=True)

    amo.create_task = create_task_no_entity_id
    sync = await _armed(source, store, amo, letters)
    order = _order(618, lead_id=lead_id, score=3, comment="3")
    source.add(order)

    assert await sync.tick() == 0                        # задачи «Повторный заказ» нет
    assert len(created_calls) == 1
    assert store.state(618).contact_task_id == -1
    assert store.state(618).status == STATUS_CONTACT_SET

    assert await sync.tick() == 0                          # второй проход
    assert len(created_calls) == 1                          # вторую задачу не поставил


# --- старые оценки (решение 8) ---

async def test_old_rating_with_open_task_is_processed_without_open_task_is_skipped():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_with_task, lead_without_task = 41008, 41009
    amo.add_task(lead_with_task, task_id=5, task_type_id=ids.TASK_TYPE_FEEDBACK)
    clock = Clock(NOW)
    sync = _sync(source, store, amo, letters, now=clock)
    # уже есть оценённые заказы к моменту первого прохода — оценка старше started_at.
    old = NOW - timedelta(days=5)
    source.add(_order(610, lead_id=lead_with_task, score=5, replied_at=old))
    source.add(_order(611, lead_id=lead_without_task, score=5, replied_at=old))

    await sync.tick()                                       # started_at = NOW

    assert amo.calls_of("get_lead_tasks_of_type") == [
        (lead_with_task, ids.TASK_TYPE_FEEDBACK), (lead_without_task, ids.TASK_TYPE_FEEDBACK)]
    assert store.state(610).status == STATUS_DONE              # с открытой — обработан
    assert store.state(611).status == STATUS_SKIPPED           # без открытой задачи — не трогаем


# --- сбои ---

async def test_three_failures_mark_failed_and_report_once():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    sync = await _armed(source, store, amo, letters)
    order = _order(612, score=5)
    source.add(order)
    amo.fail_on = "get_lead_tasks_of_type"

    for _ in range(3):
        await sync.tick()

    state = store.state(612)
    assert state.status == STATUS_FAILED and state.attempts == 3
    [(failed_order, error)] = letters.failures
    assert failed_order.order_id == 612 and "get_lead_tasks_of_type" in error

    amo.fail_on = None
    await sync.tick()                                        # больше не берём
    assert len(letters.failures) == 1


# --- репетиция ---

async def test_rehearsal_writes_nothing_reports_once_and_sets_dry_run():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo(dry_run=True)
    lead_id = 41010
    _lead(amo, lead_id, responsible_user_id=42)
    sync = await _armed(source, store, amo, letters, dry_run=True)
    order = _order(613, lead_id=lead_id, score=3, comment="3")
    source.add(order)

    assert await sync.tick() == 1

    assert amo.leads[lead_id]["responsible_user_id"] == 42     # запись не изменилась
    assert amo.tasks.get(lead_id, []) == []                    # задача не заведена
    [(reported_order, actions)] = letters.rehearsals
    assert reported_order.order_id == 613
    assert actions == ['поставил бы «Связаться»', 'написал бы комментарий']
    state = store.state(613, MODE_REHEARSAL)
    assert state.status == STATUS_DRY_RUN and state.contact_task_id == 0

    await sync.tick()
    assert len(letters.rehearsals) == 1                         # отчёт один раз


async def test_rehearsal_waiting_and_skipped_send_no_letters():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo(dry_run=True)
    lead_a, lead_b = 41011, 41012
    amo.add_task(lead_b, task_id=6, task_type_id=ids.TASK_TYPE_FEEDBACK)
    amo.task_results[6] = "закрыл руками"
    clock = Clock(NOW)
    sync = _sync(source, store, amo, letters, dry_run=True, now=clock)
    source.add(_order(614, lead_id=lead_a, score=5))            # задачи нет — ждём
    source.add(_order(615, lead_id=lead_b, score=5))            # закрыта руками — skipped

    await sync.tick()

    assert letters.rehearsals == []
    assert store.state(614, MODE_REHEARSAL).status == STATUS_NEW
    assert store.state(615, MODE_REHEARSAL).status == STATUS_SKIPPED


async def test_rehearsal_and_live_have_separate_queues():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    order = _order(616, score=5)

    rehearsal_amo = FakeAmo(dry_run=True)
    rehearsal_amo.add_task(order.lead_id, task_id=7, task_type_id=ids.TASK_TYPE_FEEDBACK)
    rehearsal = await _armed(source, store, rehearsal_amo, letters, dry_run=True)
    source.add(order)
    await rehearsal.tick()
    assert store.state(616, MODE_REHEARSAL).status == STATUS_DRY_RUN

    live_amo = FakeAmo()
    live_amo.add_task(order.lead_id, task_id=8, task_type_id=ids.TASK_TYPE_FEEDBACK)
    live = _sync(source, store, live_amo, letters)
    await live.tick()                                          # свой проход, свой заказ

    # Бой обработал тот же заказ независимо — своя задача, свой итог.
    assert store.state(616, MODE_LIVE).status == STATUS_DONE
    assert live_amo.calls_of("complete_task") == [8]
    assert rehearsal_amo.calls_of("complete_task") == [7]       # репетиция своего не трогала
    assert store.state(616, MODE_REHEARSAL).status == STATUS_DRY_RUN   # репетиция не изменилась


# --- заказ и уборка с одним номером (миграция 020) ---

async def test_order_and_cleaning_with_same_number_have_separate_states():
    """Заказ №12 доведён до конца — уборка №12 всё равно своя работа, не «уже сделано»."""
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    order_lead, cleaning_lead = 43000, 43001
    amo.add_task(order_lead, task_id=21, task_type_id=ids.TASK_TYPE_FEEDBACK)
    amo.add_task(cleaning_lead, task_id=22, task_type_id=ids.TASK_TYPE_FEEDBACK)
    sync = await _armed(source, store, amo, letters)

    source.add(_order(12, lead_id=order_lead, score=5, kind=KIND_ORDER))
    assert await sync.tick() == 1
    assert store.state(12, kind=KIND_ORDER).status == STATUS_DONE

    source.add(_order(12, lead_id=cleaning_lead, score=5, kind=KIND_CLEANING))
    assert await sync.tick() == 1

    assert amo.calls_of("complete_task") == [21, 22]
    cleaning = store.state(12, kind=KIND_CLEANING)
    assert (cleaning.kind, cleaning.status) == (KIND_CLEANING, STATUS_DONE)
    assert store.state(12, kind=KIND_ORDER).status == STATUS_DONE


# --- уборка: тексты по виду работы (ТЗ 2026-09-30, задача 4) ---

async def test_cleaning_score_low_texts_name_the_cleaning():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_id = 43100
    _lead(amo, lead_id, responsible_user_id=777)
    amo.add_task(lead_id, task_id=31, task_type_id=ids.TASK_TYPE_FEEDBACK)
    sync = await _armed(source, store, amo, letters)
    source.add(_order(12, lead_id=lead_id, score=4, comment="4", replied_at=REPLIED,
                      kind=KIND_CLEANING))

    assert await sync.tick() == 1

    [task] = amo.calls_of("create_task")
    assert task["text"] == "Клиент оценил уборку №12 на 4 — узнать, что не так."
    [(_lead_id, note)] = amo.calls_of("add_note")
    assert note == (
        "🤖 Клиент оценил уборку №12 на 4 в боте (04.09 12:46).\n"
        "Ответ клиента: «4».\n"
        "Поставил задачу «Связаться», «Повторный заказ» закрыл."
    )
    assert amo.task_results[31] == (
        "🤖 Клиент оценил уборку в боте на 4, поставлена задача «Связаться».")
    assert store.state(12, kind=KIND_CLEANING).status == STATUS_DONE


async def test_cleaning_score5_closes_task_with_cleaning_text():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    lead_id = 43101
    amo.add_task(lead_id, task_id=32, task_type_id=ids.TASK_TYPE_FEEDBACK)
    sync = await _armed(source, store, amo, letters)
    source.add(_order(13, lead_id=lead_id, score=5, kind=KIND_CLEANING))

    assert await sync.tick() == 1

    assert amo.task_results[32] == "🤖 Клиент оценил уборку в боте на 5."


async def test_journal_names_the_work_by_kind(caplog):
    caplog.set_level(logging.INFO, logger="adminbot.feedback.sync")
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    amo.add_task(43200, task_id=41, task_type_id=ids.TASK_TYPE_FEEDBACK)
    amo.add_task(43201, task_id=42, task_type_id=ids.TASK_TYPE_FEEDBACK)
    sync = await _armed(source, store, amo, letters)
    source.add(_order(12, lead_id=43200, score=5, kind=KIND_ORDER))
    source.add(_order(12, lead_id=43201, score=5, kind=KIND_CLEANING))

    await sync.tick()

    messages = [record.getMessage() for record in caplog.records]
    assert "Заказ №12 (сделка 43200), оценка 5: done" in messages
    assert "Уборка №12 (сделка 43201), оценка 5: done" in messages


async def test_journal_failure_names_the_cleaning(caplog):
    caplog.set_level(logging.INFO, logger="adminbot.feedback.sync")
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    sync = await _armed(source, store, amo, letters)
    source.add(_order(14, lead_id=43300, score=3, kind=KIND_CLEANING))
    amo.fail_on = "get_lead_tasks_of_type"

    await sync.tick()

    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("Уборка №14 (сделка 43300): попытка 1/3 не удалась")
               for message in messages)
    assert not any(message.startswith("Заказ №14") for message in messages)


# --- лимит за проход ---

async def test_batch_limit_is_20_orders_oldest_first():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    clock = Clock(NOW)
    sync = _sync(source, store, amo, letters, now=clock)
    await sync.tick()                                          # закладка

    for i in range(25):
        lead_id = 42000 + i
        amo.add_task(lead_id, task_id=1000 + i, task_type_id=ids.TASK_TYPE_FEEDBACK)
        source.add(_order(700 + i, lead_id=lead_id, score=5,
                          replied_at=NOW - timedelta(days=25 - i)))   # старые первыми

    assert await sync.tick() == 20
    registered = {order_id for (_kind, order_id, mode) in store.rows if mode == MODE_LIVE}
    assert len(registered) == 25                                     # все запомнены
    closed_task_ids = sorted(amo.calls_of("complete_task"))
    assert closed_task_ids == list(range(1000, 1020))                 # только 20 старейших
    assert store.state(700).status == STATUS_DONE                    # самый старый — обработан
    assert store.state(724).status == STATUS_NEW                     # самый новый — ждёт своего часа


# --- тексты ---

def test_note_text_variants():
    order = RatedOrder(order_id=605, lead_id=41000, score=3, comment="3", replied_at=REPLIED)

    assert note_text(order, feedback_task="closed_now") == (
        "🤖 Клиент оценил заказ №605 на 3 в боте (04.09 12:46).\n"
        "Ответ клиента: «3».\n"
        "Поставил задачу «Связаться», «Повторный заказ» закрыл."
    )
    assert "уже был закрыт" in note_text(order, feedback_task="closed_by_hand")
    assert "закрою, когда он появится" in note_text(order, feedback_task="not_yet")


def test_note_text_falls_back_to_score_when_comment_is_empty():
    order = RatedOrder(order_id=605, lead_id=41000, score=3, comment="  ", replied_at=REPLIED)

    assert "Ответ клиента: «3»." in note_text(order, feedback_task="closed_now")


def test_contact_task_text():
    order = RatedOrder(order_id=605, lead_id=41000, score=3, comment="3", replied_at=REPLIED)
    assert contact_task_text(order) == "Клиент оценил заказ №605 на 3 — узнать, что не так."


def test_feedback_result_text():
    assert feedback_result_text(5) == "🤖 Клиент оценил заказ в боте на 5."
    assert feedback_result_text(3) == (
        "🤖 Клиент оценил заказ в боте на 3, поставлена задача «Связаться».")


def test_rehearsal_text_has_prefix_actions_and_link():
    order = RatedOrder(order_id=605, lead_id=777, score=3, comment="3", replied_at=REPLIED)
    text = rehearsal_text(order, ['поставил бы «Связаться»'],
                          base_url="https://example.amocrm.ru")

    assert text.startswith("🎭 РЕПЕТИЦИЯ · ")
    assert "поставил бы «Связаться»" in text
    assert text.endswith("https://example.amocrm.ru/leads/detail/777")


def test_failure_text_matches_exact_lines():
    order = RatedOrder(order_id=605, lead_id=777, score=3, comment="3", replied_at=REPLIED)
    text = failure_text(order, "amoCRM 500: сбой", base_url="https://example.amocrm.ru")

    assert text.splitlines() == [
        "⚠️ Не смог обработать оценку 3 по заказу №605, сделай руками.",
        "Ошибка: amoCRM 500: сбой",
        "https://example.amocrm.ru/leads/detail/777",
    ]


def _cleaning(score: int = 4) -> RatedOrder:
    return RatedOrder(order_id=12, lead_id=777, score=score, comment=str(score),
                      replied_at=REPLIED, kind=KIND_CLEANING)


def test_cleaning_note_text():
    assert note_text(_cleaning(), feedback_task="not_yet") == (
        "🤖 Клиент оценил уборку №12 на 4 в боте (04.09 12:46).\n"
        "Ответ клиента: «4».\n"
        "Поставил задачу «Связаться»; «Повторный заказ» закрою, когда он появится."
    )


def test_cleaning_contact_task_text():
    assert contact_task_text(_cleaning()) == "Клиент оценил уборку №12 на 4 — узнать, что не так."


def test_cleaning_feedback_result_text():
    assert feedback_result_text(5, kind=KIND_CLEANING) == "🤖 Клиент оценил уборку в боте на 5."
    assert feedback_result_text(4, kind=KIND_CLEANING) == (
        "🤖 Клиент оценил уборку в боте на 4, поставлена задача «Связаться».")
    assert feedback_result_text(5, kind=KIND_ORDER) == "🤖 Клиент оценил заказ в боте на 5."


def test_cleaning_rehearsal_text():
    text = rehearsal_text(_cleaning(), ['поставил бы «Связаться»'],
                          base_url="https://example.amocrm.ru")

    assert text.splitlines()[0].endswith("Репетиция: уборка №12, оценка 4.")


def test_order_rehearsal_text_names_the_order():
    order = RatedOrder(order_id=605, lead_id=777, score=3, comment="3", replied_at=REPLIED)
    text = rehearsal_text(order, [], base_url="https://example.amocrm.ru")

    assert text.splitlines()[0].endswith("Репетиция: заказ №605, оценка 3.")


def test_cleaning_failure_text():
    text = failure_text(_cleaning(3), "amoCRM 500: сбой", base_url="https://example.amocrm.ru")

    assert text.splitlines()[0] == "⚠️ Не смог обработать оценку 3 по уборке №12, сделай руками."
