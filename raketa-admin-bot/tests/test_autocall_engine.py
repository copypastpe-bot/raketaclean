"""Движок автозвонка: связка машины попыток (chain.py) с АТС и амо.

Фейки — MemoryAutocallStore, MemoryPbx (свои, автозвонка) и FakeAmo из
tests/fakes.py (move_lead уже есть там для календаря — переиспользуем).
Каждый сценарий здесь — прямая проверка правил process_due из плана
Задачи 8: окно звонков, намерение до звонка, таймаут исхода, предохранитель
уведомлений, репетиция без следов.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import aiohttp

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.autocall.chain import Outcome
from adminbot.autocall.engine import CALL_OUTCOME_TIMEOUT_SEC, AutocallEngine
from adminbot.autocall.pbx import MemoryPbx
from adminbot.autocall.store import MemoryAutocallStore
from tests.fakes import FakeAmo

PHONE = "9601861067"
MANAGER_DIAL = "9161234567"


def msk(*args):
    return datetime(*args, tzinfo=MOSCOW_TZ)


class BrokenPbx:
    """АТС, которая падает прямо в call_now — для проверки «намерение до звонка»."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def call_now(self, *, to_dial: str, client_phone: str) -> str:
        self.calls.append((to_dial, client_phone))
        raise aiohttp.ClientError("АТС недоступна")

    async def call_outcome(self, call_id: str, *, called_at):
        return None  # не должен вызываться в этом сценарии: call_id так и не появился


def make_engine(*, store, pbx, amo, dry_run=False, notify_manager=None,
                notify_owner_rehearsal=None):
    return AutocallEngine(
        pbx=pbx, amo=amo, store=store, manager_dial=MANAGER_DIAL,
        notify_manager=notify_manager, notify_owner_rehearsal=notify_owner_rehearsal,
        dry_run=dry_run,
    )


# --- 1. Счастливый путь ---

async def test_happy_path_connects_and_closes_chain():
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    lead_id = 301
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 14, 0)

    link = await store.get(lead_id)
    await engine.process_due(link, now)

    calling = await store.get(lead_id)
    assert calling.status == "calling"
    assert calling.attempts_total == 1
    assert calling.call_id == "fake-1"
    assert pbx.calls == [(MANAGER_DIAL, PHONE)]

    pbx.set_outcome(calling.call_id, Outcome.CONNECTED)
    now2 = now + timedelta(seconds=1)
    link2 = await store.get(lead_id)
    await engine.process_due(link2, now2)

    done = await store.get(lead_id)
    assert done.status == "done"
    assert done.next_action_at is None

    actions = [row["action"] for row in await store.actions_for(lead_id)]
    assert "call_started" in actions
    assert "outcome" in actions


# --- 2. Намерение фиксируется до звонка ---

async def test_falling_between_intent_and_call_does_not_double_count_attempt():
    """АТС падает в call_now: намерение (calling, attempts_total+1) уже записано.

    Починка АТС здесь не нужна: цепочка в "calling" без call_id больше НЕ зовёт
    call_now — она ждёт исход или таймаут, как и любая другая попытка "в полёте".
    """
    store = MemoryAutocallStore()
    pbx = BrokenPbx()
    amo = FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    lead_id = 302
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 14, 0)

    link = await store.get(lead_id)
    await engine.process_due(link, now)

    failed = await store.get(lead_id)
    assert failed.status == "error"
    assert failed.attempts_total == 1              # намерение уже засчитано
    assert failed.called_at == now
    assert failed.call_id is None
    assert failed.last_error is not None
    assert pbx.calls == [(MANAGER_DIAL, PHONE)]     # call_now вызван ровно один раз

    # Раньше таймаута: error → ведём себя как calling → исхода нет → ждём.
    soon = now + timedelta(seconds=60)
    link2 = await store.get(lead_id)
    await engine.process_due(link2, soon)
    waiting = await store.get(lead_id)
    assert waiting.last_error is None               # ошибка снята при восстановлении
    assert waiting.attempts_total == 1               # попытка так и не задвоилась
    assert pbx.calls == [(MANAGER_DIAL, PHONE)]      # второго звонка не было

    # После таймаута — исход UNKNOWN, дальше по правилам менеджера (повтор через 5 минут).
    later = now + timedelta(seconds=CALL_OUTCOME_TIMEOUT_SEC + 1)
    link3 = await store.get(lead_id)
    await engine.process_due(link3, later)
    after_timeout = await store.get(lead_id)
    assert after_timeout.status == "queued"
    assert after_timeout.manager_failures == 1
    assert after_timeout.attempts_total == 1         # одна попытка на весь сценарий
    outcome_action = next(row for row in await store.actions_for(lead_id)
                          if row["action"] == "outcome")
    assert outcome_action["payload"] == {"outcome": "unknown"}


# --- 3. Ночная заявка ждёт окна ---

async def test_night_lead_waits_for_window_without_calling():
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    lead_id = 303
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 21, 30)

    link = await store.get(lead_id)
    await engine.process_due(link, now)

    updated = await store.get(lead_id)
    assert updated.status == "queued"
    assert updated.next_action_at == msk(2026, 9, 1, 10, 0)
    assert updated.attempts_total == 0
    assert pbx.calls == []


# --- 4. Повтор уважает окно ---

async def test_retry_after_client_no_answer_respects_window():
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    lead_id = 304
    await store.create(lead_id, phone10=PHONE)

    call_id = await pbx.call_now(to_dial=MANAGER_DIAL, client_phone=PHONE)
    pbx.set_outcome(call_id, Outcome.CLIENT_NO_ANSWER)
    called_at = msk(2026, 8, 31, 19, 55)
    await store.update(lead_id, status="calling", called_at=called_at,
                       attempts_total=1, call_id=call_id)

    now = msk(2026, 8, 31, 19, 58)
    link = await store.get(lead_id)
    await engine.process_due(link, now)

    updated = await store.get(lead_id)
    assert updated.status == "queued"
    assert updated.client_failures == 1
    assert updated.next_action_at == msk(2026, 9, 1, 10, 0)   # 20:08 упёрся в закрытие окна


# --- 5. Менеджер дважды не взял ---

async def test_manager_unreachable_twice_gives_up_without_touching_amo():
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()
    notified = []

    async def notify_manager(lead_id, kind):
        notified.append((lead_id, kind))

    engine = make_engine(store=store, pbx=pbx, amo=amo, notify_manager=notify_manager)
    lead_id = 305
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 14, 0)

    link = await store.get(lead_id)
    await engine.process_due(link, now)
    calling = await store.get(lead_id)
    pbx.set_outcome(calling.call_id, Outcome.MANAGER_NO_ANSWER)
    now2 = now + timedelta(seconds=1)
    link2 = await store.get(lead_id)
    await engine.process_due(link2, now2)

    retried = await store.get(lead_id)
    assert retried.status == "queued"
    assert retried.manager_failures == 1
    assert retried.next_action_at == now2 + timedelta(minutes=5)

    now3 = retried.next_action_at
    link3 = await store.get(lead_id)
    await engine.process_due(link3, now3)
    calling2 = await store.get(lead_id)
    assert calling2.attempts_total == 2
    assert calling2.call_id != calling.call_id
    pbx.set_outcome(calling2.call_id, Outcome.MANAGER_NO_ANSWER)
    now4 = now3 + timedelta(seconds=1)
    link4 = await store.get(lead_id)
    await engine.process_due(link4, now4)

    final = await store.get(lead_id)
    assert final.status == "gave_up"
    assert final.manager_failures == 2
    assert final.next_action_at is None
    assert notified == [(lead_id, "manager_unreachable")]
    assert amo.calls_of("move_lead") == []


# --- 6. Клиент дважды не взял ---

async def test_client_no_answer_twice_moves_lead_no_contact():
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()
    notified = []

    async def notify_manager(lead_id, kind):
        notified.append((lead_id, kind))

    engine = make_engine(store=store, pbx=pbx, amo=amo, notify_manager=notify_manager)
    lead_id = 306
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 14, 0)

    link = await store.get(lead_id)
    await engine.process_due(link, now)
    calling = await store.get(lead_id)
    pbx.set_outcome(calling.call_id, Outcome.CLIENT_NO_ANSWER)
    now2 = now + timedelta(seconds=1)
    link2 = await store.get(lead_id)
    await engine.process_due(link2, now2)

    retried = await store.get(lead_id)
    assert retried.status == "queued"
    assert retried.client_failures == 1
    assert retried.next_action_at == now2 + timedelta(minutes=10)

    now3 = retried.next_action_at
    link3 = await store.get(lead_id)
    await engine.process_due(link3, now3)
    calling2 = await store.get(lead_id)
    pbx.set_outcome(calling2.call_id, Outcome.CLIENT_NO_ANSWER)
    now4 = now3 + timedelta(seconds=1)
    link4 = await store.get(lead_id)
    await engine.process_due(link4, now4)

    final = await store.get(lead_id)
    assert final.status == "no_contact"
    assert final.client_failures == 2
    assert notified == [(lead_id, "client_retry_10"), (lead_id, "no_contact_final")]
    assert amo.calls_of("move_lead") == [
        (lead_id, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NO_CONTACT),
    ]


# --- 7. Репетиция не оставляет следов ---

async def test_rehearsal_calls_owner_once_and_touches_nothing_else():
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()
    rehearsed = []

    async def notify_owner_rehearsal(lead_id, phone10):
        rehearsed.append((lead_id, phone10))

    engine = make_engine(store=store, pbx=pbx, amo=amo, dry_run=True,
                         notify_owner_rehearsal=notify_owner_rehearsal)
    lead_id = 307
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 14, 0)

    link = await store.get(lead_id)
    await engine.process_due(link, now)

    updated = await store.get(lead_id)
    assert updated.status == "done"
    assert pbx.calls == []
    assert amo.calls == []
    assert rehearsed == [(lead_id, PHONE)]

    would_call = next(row for row in await store.actions_for(lead_id)
                      if row["action"] == "would_call")
    assert would_call["dry_run"] is True
    assert would_call["payload"] == {"phone10": PHONE}


# --- 8. Упавшее уведомление менеджера не роняет цепочку ---

async def test_failed_manager_notification_does_not_break_chain():
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()

    async def broken_notify(lead_id, kind):
        raise RuntimeError("Telegram недоступен")

    engine = make_engine(store=store, pbx=pbx, amo=amo, notify_manager=broken_notify)
    lead_id = 308
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 14, 0)

    link = await store.get(lead_id)
    await engine.process_due(link, now)
    calling = await store.get(lead_id)
    pbx.set_outcome(calling.call_id, Outcome.CLIENT_NO_ANSWER)
    now2 = now + timedelta(seconds=1)
    link2 = await store.get(lead_id)
    await engine.process_due(link2, now2)

    updated = await store.get(lead_id)
    assert updated.status == "queued"                # цепочка не упала
    assert updated.client_failures == 1

    failed = next(row for row in await store.actions_for(lead_id)
                 if row["action"] == "notify_failed")
    assert failed["payload"]["kind"] == "client_retry_10"


# --- 9. Ожидание исхода и таймаут ---

async def test_calling_without_outcome_waits_then_times_out_to_unknown():
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    lead_id = 309
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 14, 0)

    link = await store.get(lead_id)
    await engine.process_due(link, now)
    calling = await store.get(lead_id)
    # исход не программируем: MemoryPbx.call_outcome вернёт None

    soon = now + timedelta(seconds=CALL_OUTCOME_TIMEOUT_SEC - 1)
    link2 = await store.get(lead_id)
    await engine.process_due(link2, soon)
    unchanged = await store.get(lead_id)
    assert unchanged == calling                       # ничего не изменилось
    assert all(row["action"] != "outcome" for row in await store.actions_for(lead_id))

    later = now + timedelta(seconds=CALL_OUTCOME_TIMEOUT_SEC + 1)
    link3 = await store.get(lead_id)
    await engine.process_due(link3, later)
    timed_out = await store.get(lead_id)
    assert timed_out.status == "queued"
    assert timed_out.manager_failures == 1
    outcome_action = next(row for row in await store.actions_for(lead_id)
                          if row["action"] == "outcome")
    assert outcome_action["payload"] == {"outcome": "unknown"}


# --- 10. Финальный статус на входе — тихий выход ---

async def test_final_status_input_is_a_silent_noop():
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    lead_id = 310
    await store.create(lead_id, phone10=PHONE)
    await store.update(lead_id, status="done")
    before = await store.get(lead_id)

    await engine.process_due(before, msk(2026, 8, 31, 14, 0))

    after = await store.get(lead_id)
    assert after == before
    assert pbx.calls == []
    assert amo.calls == []
    assert await store.actions_for(lead_id) == []
