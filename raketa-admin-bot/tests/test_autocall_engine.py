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
import pytest

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.autocall.chain import Outcome
from adminbot.autocall.engine import CALL_OUTCOME_TIMEOUT_SEC, AutocallEngine
from adminbot.autocall.pbx import MemoryPbx
from adminbot.autocall.store import MemoryAutocallStore
from adminbot.models import AutocallLead
from tests.fakes import FakeAmo

PHONE = "9601861067"
MANAGER_DIAL = "9161234567"          # рабочий телефон менеджера
MANAGER_DIAL_PERSONAL = "9169876543"  # личный: туда идёт повтор, если рабочий молчал


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


class FlakyStore:
    """Оборачивает MemoryAutocallStore и один раз роняет ИМЕННО запись намерения.

    Условие срабатывания — update(status="calling", attempts_total=2): это и
    есть «запись намерения второй попытки» из регрессии ревью. Остальные
    вызовы (log_action, due, любые другие update) идут в настоящее хранилище
    без изменений — фейк точечный, а не общая порча стораджа.
    """

    def __init__(self, inner: MemoryAutocallStore) -> None:
        self._inner = inner
        self.failed_once = False

    async def get(self, lead_id):
        return await self._inner.get(lead_id)

    async def create(self, lead_id, *, phone10=None, **fields):
        return await self._inner.create(lead_id, phone10=phone10, **fields)

    async def update(self, lead_id, **fields):
        if (not self.failed_once and fields.get("status") == "calling"
                and fields.get("attempts_total") == 2):
            self.failed_once = True
            raise OSError("сеть моргнула на записи намерения второй попытки")
        return await self._inner.update(lead_id, **fields)

    async def due(self, now):
        return await self._inner.due(now)

    async def cursor(self):
        return await self._inner.cursor()

    async def save_cursor(self, created_from):
        await self._inner.save_cursor(created_from)

    async def log_action(self, lead_id, action, *, dry_run, payload=None):
        await self._inner.log_action(lead_id, action, dry_run=dry_run, payload=payload)

    async def actions_for(self, lead_id):
        return await self._inner.actions_for(lead_id)


def make_engine(*, store, pbx, amo, dry_run=False, notify_manager=None,
                notify_owner_rehearsal=None, notify_owner_connected=None,
                manager_dials=(MANAGER_DIAL,)):
    return AutocallEngine(
        pbx=pbx, amo=amo, store=store, manager_dials=manager_dials,
        notify_manager=notify_manager, notify_owner_rehearsal=notify_owner_rehearsal,
        notify_owner_connected=notify_owner_connected,
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


# --- 1б. Защита: без телефона АТС не трогаем ---

async def test_queued_chain_without_phone_is_closed_without_touching_pbx():
    """Что бы ни лежало в хранилище (ручная правка, сбой между шагами создания
    записи), звонить без номера движок не должен: цепочка закрывается сразу,
    АТС не получает пустой client_phone."""
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    lead_id = 302
    await store.create(lead_id, phone10=None)
    now = msk(2026, 8, 31, 14, 0)

    link = await store.get(lead_id)
    await engine.process_due(link, now)

    closed = await store.get(lead_id)
    assert closed.status == "gave_up"
    assert closed.last_error == "звонок без телефона невозможен"
    assert pbx.calls == []

    logged = store.actions_of("no_phone")
    assert logged and logged[0]["lead_id"] == lead_id


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


# --- 5б. Второй телефон менеджера (решение владельца 2026-09-02) ---
#
# У админа два телефона — рабочий и личный. Правило владельца: не взял
# рабочий, значит повтор через 5 минут идёт на личный. Само правило повтора
# (решение №3) при этом не меняется, меняется только НОМЕР.


async def _first_attempt_missed_by_manager(store, pbx, amo, engine, lead_id, now):
    """Первая попытка, менеджер не взял → цепочка вернулась в очередь."""
    await store.create(lead_id, phone10=PHONE)
    link = await store.get(lead_id)
    await engine.process_due(link, now)
    calling = await store.get(lead_id)
    pbx.set_outcome(calling.call_id, Outcome.MANAGER_NO_ANSWER)
    link2 = await store.get(lead_id)
    await engine.process_due(link2, now + timedelta(seconds=1))
    return await store.get(lead_id)


async def test_retry_after_manager_miss_goes_to_his_second_phone():
    """Рабочий не ответил — повтор набирает личный телефон менеджера."""
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo,
                         manager_dials=(MANAGER_DIAL, MANAGER_DIAL_PERSONAL))
    now = msk(2026, 9, 2, 14, 0)

    retried = await _first_attempt_missed_by_manager(store, pbx, amo, engine, 401, now)
    link = await store.get(401)
    await engine.process_due(link, retried.next_action_at)

    assert [dial for dial, _ in pbx.calls] == [MANAGER_DIAL, MANAGER_DIAL_PERSONAL]


async def test_retry_after_client_miss_stays_on_first_phone():
    """Клиент не подошёл — менеджер ни при чём, звоним на тот же рабочий."""
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo,
                         manager_dials=(MANAGER_DIAL, MANAGER_DIAL_PERSONAL))
    now = msk(2026, 9, 2, 14, 0)
    await store.create(402, phone10=PHONE)

    link = await store.get(402)
    await engine.process_due(link, now)
    calling = await store.get(402)
    pbx.set_outcome(calling.call_id, Outcome.CLIENT_NO_ANSWER)
    link2 = await store.get(402)
    await engine.process_due(link2, now + timedelta(seconds=1))

    retried = await store.get(402)
    link3 = await store.get(402)
    await engine.process_due(link3, retried.next_action_at)

    assert [dial for dial, _ in pbx.calls] == [MANAGER_DIAL, MANAGER_DIAL]


async def test_single_phone_setting_keeps_working():
    """Номер задан один — повтор идёт на него же, без падений."""
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo, manager_dials=(MANAGER_DIAL,))
    now = msk(2026, 9, 2, 14, 0)

    retried = await _first_attempt_missed_by_manager(store, pbx, amo, engine, 403, now)
    link = await store.get(403)
    await engine.process_due(link, retried.next_action_at)

    assert [dial for dial, _ in pbx.calls] == [MANAGER_DIAL, MANAGER_DIAL]


async def test_rehearsal_works_without_manager_phones():
    """Репетиция живёт и без телефонов: она никому не звонит.

    Служба на сервере крутит автозвонок в репетиции, и настройки АТС там
    могут быть не заполнены. Падение на старте из-за пустого списка
    телефонов остановило бы работающую функцию — этого допустить нельзя.
    """
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo, dry_run=True, manager_dials=())
    await store.create(404, phone10=PHONE)

    link = await store.get(404)
    await engine.process_due(link, msk(2026, 9, 2, 14, 0))

    assert pbx.calls == []
    assert (await store.get(404)).status == "done"


async def test_live_call_without_manager_phones_fails_loudly():
    """А в бою пустой список — ошибка с понятным текстом, а не молчание."""
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo, dry_run=False, manager_dials=())
    await store.create(405, phone10=PHONE)

    link = await store.get(405)
    with pytest.raises(ValueError, match="телефон"):
        await engine.process_due(link, msk(2026, 9, 2, 14, 0))


# --- 5в. След робота в сделке (решение владельца 2026-09-02) ---
#
# Звонок роботом не попадает в карточку сделки: связка АТС с амо знает только
# внутренний номер 100, а робот звонит менеджеру на мобильный. Проверено на
# живой заявке — в АТС вызов есть, в амо нет. Поэтому след оставляет робот.


def _notes(amo):
    return [text for name, (_lead, text) in
            ((n, a) for n, a in amo.calls if n == "add_note")]


async def test_finished_chain_leaves_note_in_deal():
    """Соединились — в сделке появляется одно примечание об этом."""
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    now = msk(2026, 9, 2, 18, 8)
    await store.create(501, phone10=PHONE)

    link = await store.get(501)
    await engine.process_due(link, now)
    calling = await store.get(501)
    assert _notes(amo) == []          # пока звонок идёт, писать нечего

    pbx.set_outcome(calling.call_id, Outcome.CONNECTED)
    link2 = await store.get(501)
    await engine.process_due(link2, now + timedelta(seconds=1))

    notes = _notes(amo)
    assert len(notes) == 1
    assert "соединил" in notes[0].lower()


async def test_retry_does_not_leave_note():
    """Цепочка ещё жива (повтор через 5 минут) — примечание преждевременно."""
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    now = msk(2026, 9, 2, 18, 8)

    await _first_attempt_missed_by_manager(store, pbx, amo, engine, 502, now)

    assert _notes(amo) == []


async def test_gave_up_chain_leaves_note_about_manager():
    """Менеджер не ответил дважды — в сделке видно, почему робот встал."""
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    now = msk(2026, 9, 2, 18, 8)

    retried = await _first_attempt_missed_by_manager(store, pbx, amo, engine, 503, now)
    link = await store.get(503)
    await engine.process_due(link, retried.next_action_at)
    calling2 = await store.get(503)
    pbx.set_outcome(calling2.call_id, Outcome.MANAGER_NO_ANSWER)
    link2 = await store.get(503)
    await engine.process_due(link2, retried.next_action_at + timedelta(seconds=1))

    assert (await store.get(503)).status == "gave_up"
    notes = _notes(amo)
    assert len(notes) == 1
    assert "менеджер" in notes[0].lower()


async def test_note_failure_does_not_break_the_chain():
    """CRM не приняла примечание — цепочка всё равно закрыта правильно.

    Примечание — след для человека, а не часть решения: потерять его
    неприятно, но остановить из-за него уже завершённую цепочку хуже.
    """
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    now = msk(2026, 9, 2, 18, 8)
    await store.create(504, phone10=PHONE)

    link = await store.get(504)
    await engine.process_due(link, now)
    calling = await store.get(504)
    pbx.set_outcome(calling.call_id, Outcome.CONNECTED)
    amo.fail_on = "add_note"
    link2 = await store.get(504)
    await engine.process_due(link2, now + timedelta(seconds=1))

    assert (await store.get(504)).status == "done"


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


# --- Регрессия ревью: Retry обязан сбросить called_at/call_id ---

async def test_retry_clears_called_at_and_call_id_so_recovery_never_reasks_old_outcome():
    """Баг из ревью: Retry не сбрасывал called_at/call_id → сбой ровно на записи
    намерения ВТОРОЙ попытки оставлял строку в "error" со СТАРЫМ call_id;
    _recover_error принимал его за «попытка ещё идёт», переспрашивал АТС по
    этому call_id и засчитывал уже разобранный исход менеджера второй раз —
    без единого нового звонка (manager_failures доезжал до 2 и до
    "manager_unreachable" за один реальный звонок).

    С фиксом после Retry called_at/call_id всегда None, поэтому такое же
    падение на записи намерения второй попытки восстанавливается как "queued"
    и реально перезванивает, а не пересчитывает прошлый исход.
    """
    inner = MemoryAutocallStore()
    store = FlakyStore(inner)
    pbx = MemoryPbx()
    amo = FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    lead_id = 311
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 14, 0)

    # Первая попытка: реальный звонок, менеджер не берёт → Retry в queued.
    link = await store.get(lead_id)
    await engine.process_due(link, now)
    calling = await store.get(lead_id)
    call_id_1 = calling.call_id
    pbx.set_outcome(call_id_1, Outcome.MANAGER_NO_ANSWER)
    now2 = now + timedelta(seconds=1)
    link2 = await store.get(lead_id)
    await engine.process_due(link2, now2)

    retried = await store.get(lead_id)
    assert retried.status == "queued"
    assert retried.manager_failures == 1
    assert retried.called_at is None            # фикс: попытка не «висит» после Retry
    assert retried.call_id is None

    # Вторая попытка: запись намерения падает — цепочка уходит в error,
    # а не в повторный разбор старого исхода.
    now3 = retried.next_action_at
    link3 = await store.get(lead_id)
    await engine.process_due(link3, now3)

    failed = await store.get(lead_id)
    assert failed.status == "error"
    assert failed.attempts_total == 1            # вторая попытка не засчиталась
    assert failed.manager_failures == 1           # старый исход НЕ пересчитан
    assert len(pbx.calls) == 1                    # второго реального звонка ещё не было

    # Восстановление: запись намерения проходит нормально, звонок — реальный второй.
    now4 = now3 + timedelta(seconds=1)
    link4 = await store.get(lead_id)
    await engine.process_due(link4, now4)

    recovered = await store.get(lead_id)
    assert recovered.status == "calling"
    assert recovered.attempts_total == 2
    assert recovered.manager_failures == 1        # НЕ 2 — старый исход не переспрошен
    assert recovered.call_id != call_id_1
    assert len(pbx.calls) == 2                    # ровно один новый реальный звонок


# --- Восстановление error без called_at ---

async def test_error_without_called_at_recovers_as_queued():
    """Сбой ДО первой команды АТС (called_at ещё не проставлен) — восстановление
    ведётся как "queued": окно, затем звонок с нуля, а не переспрос несуществующего
    исхода несуществующего звонка.
    """
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    lead_id = 312
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 14, 0)
    # Сбой смоделирован напрямую: цепочка в error, called_at ещё ни разу не
    # проставлялся (сбой случился раньше самой команды АТС).
    await store.update(lead_id, status="error", last_error="сеть моргнула",
                       next_action_at=now - timedelta(seconds=1))

    link = await store.get(lead_id)
    await engine.process_due(link, now)

    recovered = await store.get(lead_id)
    assert recovered.status == "calling"
    assert recovered.attempts_total == 1
    assert recovered.call_id == "fake-1"
    assert recovered.last_error is None
    assert pbx.calls == [(MANAGER_DIAL, PHONE)]


# --- Минор: симметричный warning, если запись пропала из хранилища ---

async def test_recover_error_warns_when_lead_missing_from_store(caplog):
    """Симметрия с другими аномалиями (финальный/незнакомый статус — тоже log.warning):

    если store.update(last_error=None) не нашёл запись, движок не падает и
    не выдумывает состояние — предупреждает и по-прежнему пытается довести
    цепочку по данным из переданного `link`.
    """
    store = MemoryAutocallStore()               # запись НЕ создаём — её нет в хранилище
    pbx = MemoryPbx()
    amo = FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    ghost = AutocallLead(lead_id=999, status="error", phone10=PHONE,
                         attempts_total=0, called_at=None)

    with caplog.at_level("WARNING"):
        await engine.process_due(ghost, msk(2026, 8, 31, 14, 0))

    assert any("не удалось снять last_error" in record.getMessage()
              for record in caplog.records)


# --- Задача 11: отчёт владельцу о соединении (неделя наблюдения, дизайн §6.5) ---

async def test_connected_outcome_notifies_owner():
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()
    notified = []

    async def notify_owner_connected(lead_id):
        notified.append(lead_id)

    engine = make_engine(store=store, pbx=pbx, amo=amo,
                         notify_owner_connected=notify_owner_connected)
    lead_id = 313
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 14, 0)

    link = await store.get(lead_id)
    await engine.process_due(link, now)
    calling = await store.get(lead_id)
    pbx.set_outcome(calling.call_id, Outcome.CONNECTED)
    now2 = now + timedelta(seconds=1)
    link2 = await store.get(lead_id)
    await engine.process_due(link2, now2)

    done = await store.get(lead_id)
    assert done.status == "done"
    assert notified == [lead_id]

    notify_action = next(row for row in await store.actions_for(lead_id)
                         if row["action"] == "notify")
    assert notify_action["payload"] == {"kind": "connected"}


async def test_failed_owner_connected_notification_does_not_break_chain():
    """Недоставленный отчёт о соединении не должен отменять сам факт done:
    цепочка уже завершилась, дело лишь в журнале (см. _notify_manager)."""
    store = MemoryAutocallStore()
    pbx = MemoryPbx()
    amo = FakeAmo()

    async def broken_notify(lead_id):
        raise RuntimeError("Telegram недоступен")

    engine = make_engine(store=store, pbx=pbx, amo=amo,
                         notify_owner_connected=broken_notify)
    lead_id = 314
    await store.create(lead_id, phone10=PHONE)
    now = msk(2026, 8, 31, 14, 0)

    link = await store.get(lead_id)
    await engine.process_due(link, now)
    calling = await store.get(lead_id)
    pbx.set_outcome(calling.call_id, Outcome.CONNECTED)
    now2 = now + timedelta(seconds=1)
    link2 = await store.get(lead_id)
    await engine.process_due(link2, now2)

    done = await store.get(lead_id)
    assert done.status == "done"                     # соединение не отменилось

    failed = next(row for row in await store.actions_for(lead_id)
                 if row["action"] == "notify_failed")
    assert failed["payload"]["kind"] == "connected"


# --- Кнопка «Я уже перезвонил» (ТЗ 2026-10-23, задача 2) ---
#
# Выключатель AUTOCALL_MANAGER_BUTTON. Недозвон, за которым следует повтор, —
# менеджеру одно сообщение с кнопкой; дальше то же сообщение правится: новый
# повтор — новое время, итог — без кнопки. Перед повтором робот смотрит, не
# нажал ли менеджер кнопку (строку пишет рабочий бот): нажал — цепочка
# закрывается статусом cancelled, звонка нет, в сделке примечание.

from dataclasses import replace as dc_replace  # noqa: E402

from adminbot.autocall.engine import ManagerCard  # noqa: E402
from adminbot.autocall.store import MemoryManagerCallbacks  # noqa: E402

MANAGER_CALLED_NOTE = "Автозвонок: менеджер перезвонил клиенту сам (кнопка). Робот остановился."


class FakeManagerChat:
    """Личка менеджера в рабочем боте: что отправлено и что поправлено."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, ManagerCard, int]] = []
        self.edited: list[tuple[int, int, ManagerCard]] = []
        self.fail_edit = False
        self.fail_send = False
        self.edit_error: Exception | None = None   # точный ответ Telegram на правку
        self._next_id = 9000

    async def send(self, lead_id: int, card: ManagerCard) -> int:
        if self.fail_send:
            raise RuntimeError("Telegram недоступен")
        self._next_id += 1
        self.sent.append((lead_id, card, self._next_id))
        return self._next_id

    async def edit(self, message_id: int, lead_id: int, card: ManagerCard) -> None:
        if self.fail_edit:
            raise RuntimeError("Bad Request: message to edit not found")
        if self.edit_error is not None:
            raise self.edit_error
        self.edited.append((message_id, lead_id, card))


class BrokenCallbacks:
    """Таблица нажатий не отвечает (нет миграции рабочего бота, сеть)."""

    async def pressed(self, lead_id: int) -> bool:
        raise RuntimeError('relation "public.autocall_manager_callbacks" does not exist')


def make_button_engine(*, store, pbx, amo, chat, callbacks, manager_button=True,
                       notify_manager=None, notify_owner_connected=None):
    return AutocallEngine(
        pbx=pbx, amo=amo, store=store, manager_dials=(MANAGER_DIAL,),
        notify_manager=notify_manager, notify_owner_connected=notify_owner_connected,
        manager_button=manager_button, manager_chat=chat, manager_callbacks=callbacks,
        dry_run=False,
    )


async def _attempt(store, pbx, engine, lead_id, now, outcome):
    """Одна попытка целиком: команда АТС в `now`, исход — через секунду."""
    await engine.process_due(await store.get(lead_id), now)
    calling = await store.get(lead_id)
    assert calling.status == "calling"
    pbx.set_outcome(calling.call_id, outcome)
    await engine.process_due(await store.get(lead_id), now + timedelta(seconds=1))
    return await store.get(lead_id)


def _button_setup(**kwargs):
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    chat, callbacks, notified = FakeManagerChat(), MemoryManagerCallbacks(), []

    async def notify_manager(lead_id, kind):
        notified.append((lead_id, kind))

    engine = make_button_engine(store=store, pbx=pbx, amo=amo, chat=chat,
                                callbacks=callbacks, notify_manager=notify_manager, **kwargs)
    return store, pbx, amo, chat, callbacks, notified, engine


async def test_button_manager_miss_sends_message_with_button_and_remembers_it():
    store, pbx, amo, chat, _callbacks, notified, engine = _button_setup()
    await store.create(701, phone10=PHONE)
    now = msk(2026, 10, 23, 10, 0)

    retried = await _attempt(store, pbx, engine, 701, now, Outcome.MANAGER_NO_ANSWER)

    assert retried.status == "queued"
    assert chat.sent == [(701, ManagerCard(
        kind="retry_manager", button=True,
        retry_at=now + timedelta(seconds=1) + timedelta(minutes=5),
        now=now + timedelta(seconds=1)), 9001)]
    assert chat.edited == []
    assert retried.tg_message_id == 9001
    assert notified == []


async def test_button_second_miss_edits_the_same_message_and_skips_retry_10():
    """Второй недозвон — то же сообщение с новым временем; прежнее отдельное
    «повтор через 10 минут» не шлётся."""
    store, pbx, amo, chat, _callbacks, notified, engine = _button_setup()
    await store.create(702, phone10=PHONE)
    now = msk(2026, 10, 23, 10, 0)
    first = await _attempt(store, pbx, engine, 702, now, Outcome.MANAGER_NO_ANSWER)

    now2 = first.next_action_at
    second = await _attempt(store, pbx, engine, 702, now2, Outcome.CLIENT_NO_ANSWER)

    assert second.status == "queued"
    assert len(chat.sent) == 1
    assert chat.edited == [(9001, 702, ManagerCard(
        kind="retry_client", button=True,
        retry_at=now2 + timedelta(seconds=1) + timedelta(minutes=10),
        now=now2 + timedelta(seconds=1)))]
    assert second.tg_message_id == 9001
    assert notified == []                       # client_retry_10 не ушёл


async def test_button_pressed_before_retry_cancels_without_a_call():
    store, pbx, amo, chat, callbacks, notified, engine = _button_setup()
    await store.create(703, phone10=PHONE)
    now = msk(2026, 10, 23, 10, 0)
    first = await _attempt(store, pbx, engine, 703, now, Outcome.MANAGER_NO_ANSWER)

    callbacks.press(703)                         # рабочий бот записал нажатие
    await engine.process_due(await store.get(703), first.next_action_at)

    final = await store.get(703)
    assert final.status == "cancelled"
    assert final.next_action_at is None
    assert final.attempts_total == 1
    assert len(pbx.calls) == 1                   # второго звонка нет
    cancelled = [row for row in store.actions if row["action"] == "cancelled"]
    assert [row["payload"] for row in cancelled] == [{"reason": "manager_called"}]
    assert _notes(amo) == [MANAGER_CALLED_NOTE]
    assert chat.edited == [(9001, 703, ManagerCard(kind="manager_called", button=False))]
    assert notified == []

    # Закрытая цепочка больше не трогается.
    await engine.process_due(final, first.next_action_at + timedelta(minutes=1))
    assert len(pbx.calls) == 1


async def test_button_first_attempt_is_not_checked():
    """Проверка нажатия — только перед повтором: первую попытку ничто не держит."""
    store, pbx, amo, chat, callbacks, notified, engine = _button_setup()
    await store.create(704, phone10=PHONE)
    callbacks.press(704)

    await engine.process_due(await store.get(704), msk(2026, 10, 23, 10, 0))

    assert (await store.get(704)).status == "calling"
    assert len(pbx.calls) == 1


async def test_button_switch_off_ignores_press_and_sends_nothing_new():
    store, pbx, amo, chat, callbacks, notified, engine = _button_setup(manager_button=False)
    await store.create(705, phone10=PHONE)
    now = msk(2026, 10, 23, 10, 0)
    first = await _attempt(store, pbx, engine, 705, now, Outcome.CLIENT_NO_ANSWER)
    callbacks.press(705)

    await engine.process_due(await store.get(705), first.next_action_at)

    assert (await store.get(705)).status == "calling"
    assert len(pbx.calls) == 2                   # нажатие ничего не остановило
    assert chat.sent == [] and chat.edited == []
    assert notified == [(705, "client_retry_10")]
    assert first.tg_message_id is None


def _comparable(lead):
    return dc_replace(lead, created_at=None, updated_at=None)


async def _run_full_chain(engine, store, pbx):
    """Менеджер не взял → клиент не взял → клиент не взял: сделка уходит на этап."""
    await store.create(706, phone10=PHONE)
    now = msk(2026, 10, 23, 19, 50)
    first = await _attempt(store, pbx, engine, 706, now, Outcome.MANAGER_NO_ANSWER)
    second = await _attempt(store, pbx, engine, 706, first.next_action_at,
                            Outcome.CLIENT_NO_ANSWER)                    # повтор — завтра
    await _attempt(store, pbx, engine, 706, second.next_action_at, Outcome.CLIENT_NO_ANSWER)
    return _comparable(await store.get(706))


async def test_button_switch_off_behaves_exactly_as_before():
    """Выключатель выключен — поведение ровно прежнее, даже если источник
    нажатий и личка менеджера переданы, а кнопка «нажата»: те же звонки, те же
    сообщения, тот же журнал, те же поля строки, те же действия в амо."""
    def setup():
        store, pbx, amo, notified = MemoryAutocallStore(), MemoryPbx(), FakeAmo(), []

        async def notify_manager(lead_id, kind):
            notified.append((lead_id, kind))
        return store, pbx, amo, notified, notify_manager

    store_a, pbx_a, amo_a, notified_a, notify_a = setup()
    before = make_engine(store=store_a, pbx=pbx_a, amo=amo_a, notify_manager=notify_a)
    lead_a = await _run_full_chain(before, store_a, pbx_a)

    store_b, pbx_b, amo_b, notified_b, notify_b = setup()
    chat, callbacks = FakeManagerChat(), MemoryManagerCallbacks()
    callbacks.press(706)
    switched_off = make_button_engine(store=store_b, pbx=pbx_b, amo=amo_b, chat=chat,
                                      callbacks=callbacks, manager_button=False,
                                      notify_manager=notify_b)
    lead_b = await _run_full_chain(switched_off, store_b, pbx_b)

    assert lead_a.status == "no_contact"
    assert lead_b == lead_a
    assert pbx_b.calls == pbx_a.calls
    assert notified_b == notified_a == [(706, "client_retry_10"), (706, "no_contact_final")]
    assert store_b.actions == store_a.actions
    assert amo_b.calls == amo_a.calls
    assert chat.sent == [] and chat.edited == []


async def test_button_connected_edits_message_to_final_without_button():
    store, pbx, amo, chat, _callbacks, notified, engine = _button_setup()
    await store.create(707, phone10=PHONE)
    now = msk(2026, 10, 23, 10, 0)
    first = await _attempt(store, pbx, engine, 707, now, Outcome.MANAGER_NO_ANSWER)

    second_at = first.next_action_at
    done = await _attempt(store, pbx, engine, 707, second_at, Outcome.CONNECTED)

    assert done.status == "done"
    assert len(chat.sent) == 1
    assert chat.edited == [(9001, 707, ManagerCard(kind="done", button=False, at=second_at))]
    assert notified == []


async def test_button_gave_up_edits_message_instead_of_sending_new():
    store, pbx, amo, chat, _callbacks, notified, engine = _button_setup()
    await store.create(708, phone10=PHONE)
    now = msk(2026, 10, 23, 10, 0)
    first = await _attempt(store, pbx, engine, 708, now, Outcome.MANAGER_NO_ANSWER)

    final = await _attempt(store, pbx, engine, 708, first.next_action_at,
                           Outcome.MANAGER_NO_ANSWER)

    assert final.status == "gave_up"
    assert chat.edited == [(9001, 708, ManagerCard(kind="manager_unreachable", button=False))]
    assert len(chat.sent) == 1
    assert notified == []                        # отдельного сообщения нет
    assert len(_notes(amo)) == 1                 # примечание — как и раньше


async def test_button_final_without_message_goes_the_old_way():
    """Сообщения с кнопкой по цепочке нет (выключатель включили посреди цепочки,
    отправка не удалась) — итог прежним отдельным сообщением."""
    store, pbx, amo, chat, _callbacks, notified, engine = _button_setup()
    await store.create(709, phone10=PHONE)
    call_id = await pbx.call_now(to_dial=MANAGER_DIAL, client_phone=PHONE)
    pbx.set_outcome(call_id, Outcome.CLIENT_NO_ANSWER)
    await store.update(709, status="calling", called_at=msk(2026, 10, 23, 10, 10),
                       attempts_total=2, client_failures=1, call_id=call_id)

    await engine.process_due(await store.get(709), msk(2026, 10, 23, 10, 11))

    assert (await store.get(709)).status == "no_contact"
    assert notified == [(709, "no_contact_final")]
    assert chat.sent == [] and chat.edited == []


async def test_button_edit_failure_sends_new_message_and_remembers_it():
    store, pbx, amo, chat, _callbacks, notified, engine = _button_setup()
    await store.create(710, phone10=PHONE)
    now = msk(2026, 10, 23, 10, 0)
    first = await _attempt(store, pbx, engine, 710, now, Outcome.MANAGER_NO_ANSWER)
    chat.fail_edit = True                        # менеджер удалил сообщение

    second = await _attempt(store, pbx, engine, 710, first.next_action_at,
                            Outcome.CLIENT_NO_ANSWER)

    assert [message_id for _lead, _card, message_id in chat.sent] == [9001, 9002]
    assert chat.sent[1][1].kind == "retry_client"
    assert chat.sent[1][1].button is True
    assert second.tg_message_id == 9002
    assert [row["action"] for row in store.actions].count("card_edit_failed") == 1

    final = await _attempt(store, pbx, engine, 710, second.next_action_at,
                           Outcome.CLIENT_NO_ANSWER)
    assert final.status == "no_contact"
    assert chat.sent[2][1] == ManagerCard(kind="no_contact_final", button=False)
    assert final.tg_message_id == 9003
    assert notified == []


async def test_button_send_failure_does_not_break_the_chain():
    store, pbx, amo, chat, _callbacks, notified, engine = _button_setup()
    chat.fail_send = True
    await store.create(711, phone10=PHONE)

    retried = await _attempt(store, pbx, engine, 711, msk(2026, 10, 23, 10, 0),
                             Outcome.MANAGER_NO_ANSWER)

    assert retried.status == "queued"
    assert retried.tg_message_id is None
    failed = [row for row in store.actions if row["action"] == "notify_failed"]
    assert failed and failed[0]["payload"]["kind"] == "retry_manager"


async def test_button_check_failure_calls_as_before():
    """Таблица нажатий не ответила — робот звонит, как раньше, и пишет в журнал:
    сбой проверки не должен держать звонок клиенту."""
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    chat = FakeManagerChat()
    engine = make_button_engine(store=store, pbx=pbx, amo=amo, chat=chat,
                                callbacks=BrokenCallbacks())
    await store.create(712, phone10=PHONE)
    first = await _attempt(store, pbx, engine, 712, msk(2026, 10, 23, 10, 0),
                           Outcome.MANAGER_NO_ANSWER)

    await engine.process_due(await store.get(712), first.next_action_at)

    assert (await store.get(712)).status == "calling"
    assert len(pbx.calls) == 2
    # Две отметки: после исхода первой попытки (правка П1) и перед повтором.
    assert [row["action"] for row in store.actions].count("button_check_failed") == 2


async def test_last_called_at_survives_retry():
    """called_at обнуляется при возврате в очередь, last_called_at — нет:
    перед повтором робот знает, когда звонил в прошлый раз."""
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    engine = make_engine(store=store, pbx=pbx, amo=amo)
    await store.create(713, phone10=PHONE)
    now = msk(2026, 10, 23, 10, 0)

    retried = await _attempt(store, pbx, engine, 713, now, Outcome.MANAGER_NO_ANSWER)

    assert retried.called_at is None
    assert retried.last_called_at == now

    await engine.process_due(retried, retried.next_action_at)
    assert (await store.get(713)).last_called_at == retried.next_action_at


# --- Нажатие во время идущего звонка (правка П1 по ревью Задачи 2) ---
#
# Менеджер нажал кнопку, пока робот набирал номер: рабочий бот уже написал
# «Принято» и убрал кнопку. Исход звонка ведёт к повтору — но повтора не будет,
# поэтому цепочка закрывается сразу, без сообщения «Повтор в …».

async def _press_during_second_call(store, pbx, engine, callbacks, lead_id, first,
                                    outcome):
    """Вторая попытка: робот набирает, менеджер жмёт кнопку, приходит исход."""
    await engine.process_due(await store.get(lead_id), first.next_action_at)
    calling = await store.get(lead_id)
    assert calling.status == "calling"
    callbacks.press(lead_id)
    pbx.set_outcome(calling.call_id, outcome)
    await engine.process_due(calling, first.next_action_at + timedelta(seconds=1))
    return await store.get(lead_id)


@pytest.mark.parametrize("first_outcome, second_outcome", [
    (Outcome.CLIENT_NO_ANSWER, Outcome.MANAGER_NO_ANSWER),   # во второй раз не взял менеджер
    (Outcome.MANAGER_NO_ANSWER, Outcome.CLIENT_NO_ANSWER),   # во второй раз не взял клиент
], ids=["manager_no_answer", "client_no_answer"])
async def test_button_pressed_during_call_closes_chain_instead_of_retry(
        first_outcome, second_outcome):
    store, pbx, amo, chat, callbacks, notified, engine = _button_setup()
    await store.create(720, phone10=PHONE)
    first = await _attempt(store, pbx, engine, 720, msk(2026, 10, 23, 10, 0), first_outcome)
    assert first.tg_message_id == 9001

    final = await _press_during_second_call(store, pbx, engine, callbacks, 720, first,
                                            second_outcome)

    assert final.status == "cancelled"
    assert final.next_action_at is None
    assert final.attempts_total == 2
    assert (final.manager_failures, final.client_failures) == (1, 1)
    assert final.called_at is None and final.call_id is None
    assert len(chat.sent) == 1
    # Сообщения «Повтор в …» нет — сразу итог.
    assert chat.edited == [(9001, 720, ManagerCard(kind="manager_called", button=False))]
    assert final.tg_message_id == 9001
    assert notified == []                        # и client_retry_10 не ушёл
    assert _notes(amo) == [MANAGER_CALLED_NOTE]
    cancelled = [row for row in store.actions if row["action"] == "cancelled"]
    assert [row["payload"] for row in cancelled] == [{"reason": "manager_called"}]

    # Звонков больше нет.
    await engine.process_due(final, first.next_action_at + timedelta(minutes=30))
    assert len(pbx.calls) == 2


async def test_button_pressed_during_call_connected_is_done_as_before():
    """Нажатие во время звонка, а звонок соединил — итог «соединил», как раньше."""
    store, pbx, amo, chat, callbacks, notified, engine = _button_setup()
    await store.create(721, phone10=PHONE)
    first = await _attempt(store, pbx, engine, 721, msk(2026, 10, 23, 10, 0),
                           Outcome.MANAGER_NO_ANSWER)

    final = await _press_during_second_call(store, pbx, engine, callbacks, 721, first,
                                            Outcome.CONNECTED)

    assert final.status == "done"
    assert chat.edited == [(9001, 721, ManagerCard(kind="done", button=False,
                                                   at=first.next_action_at))]
    assert [row for row in store.actions if row["action"] == "cancelled"] == []
    assert len(_notes(amo)) == 1 and _notes(amo) != [MANAGER_CALLED_NOTE]


async def test_button_pressed_during_call_switch_off_retries_as_before():
    store, pbx, amo, chat, callbacks, notified, engine = _button_setup(manager_button=False)
    await store.create(722, phone10=PHONE)
    first = await _attempt(store, pbx, engine, 722, msk(2026, 10, 23, 10, 0),
                           Outcome.MANAGER_NO_ANSWER)

    final = await _press_during_second_call(store, pbx, engine, callbacks, 722, first,
                                            Outcome.CLIENT_NO_ANSWER)

    assert final.status == "queued"
    assert notified == [(722, "client_retry_10")]
    assert chat.sent == [] and chat.edited == []
    assert [row for row in store.actions if row["action"] == "cancelled"] == []


async def test_button_check_failure_after_call_shows_retry_as_before():
    """Проверка нажатия после исхода упала — сообщение о повторе и повтор, как
    раньше, отметка в журнале."""
    store, pbx, amo = MemoryAutocallStore(), MemoryPbx(), FakeAmo()
    chat = FakeManagerChat()
    engine = make_button_engine(store=store, pbx=pbx, amo=amo, chat=chat,
                                callbacks=BrokenCallbacks())
    await store.create(723, phone10=PHONE)

    retried = await _attempt(store, pbx, engine, 723, msk(2026, 10, 23, 10, 0),
                             Outcome.MANAGER_NO_ANSWER)

    assert retried.status == "queued"
    assert retried.next_action_at is not None
    assert [card.kind for _lead, card, _id in chat.sent] == ["retry_manager"]
    assert [row["action"] for row in store.actions].count("button_check_failed") == 1


# --- «message is not modified» — правка удалась (правка П3 по ревью Задачи 2) ---
#
# Telegram отвечает ошибкой, если текст и кнопка не изменились (бывает при
# повторе шага после сбоя записи в базу). Сообщение уже такое, как нужно, —
# нового не слать.

def _not_modified_error():
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText
    return TelegramBadRequest(
        method=EditMessageText(text="x", chat_id=1, message_id=9001),
        message="Bad Request: message is not modified: specified new message content "
                "and reply markup are exactly the same as a current content and reply "
                "markup of the message")


async def test_button_edit_not_modified_is_a_successful_edit():
    store, pbx, amo, chat, _callbacks, notified, engine = _button_setup()
    await store.create(724, phone10=PHONE)
    first = await _attempt(store, pbx, engine, 724, msk(2026, 10, 23, 10, 0),
                           Outcome.MANAGER_NO_ANSWER)
    chat.edit_error = _not_modified_error()

    second = await _attempt(store, pbx, engine, 724, first.next_action_at,
                            Outcome.CLIENT_NO_ANSWER)

    assert second.status == "queued"
    assert len(chat.sent) == 1                   # нового сообщения нет
    assert second.tg_message_id == 9001
    assert "card_edit_failed" not in [row["action"] for row in store.actions]


async def test_button_cancel_edit_not_modified_sends_nothing_new():
    store, pbx, amo, chat, callbacks, notified, engine = _button_setup()
    await store.create(725, phone10=PHONE)
    first = await _attempt(store, pbx, engine, 725, msk(2026, 10, 23, 10, 0),
                           Outcome.MANAGER_NO_ANSWER)
    chat.edit_error = _not_modified_error()
    callbacks.press(725)

    await engine.process_due(await store.get(725), first.next_action_at)

    final = await store.get(725)
    assert final.status == "cancelled"
    assert len(chat.sent) == 1
    assert final.tg_message_id == 9001
    assert "card_edit_failed" not in [row["action"] for row in store.actions]
