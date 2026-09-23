"""Отклик на промо → контакт → сделка с тегом (ТЗ 2026-09-23, задачи 5 и 6).

Живая CRM и Postgres здесь не участвуют: источник и хранилище — в памяти,
амо — двойник из `tests/fakes.py`. Запросы к базе проверяет
`test_promo_callback_store.py`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional

from adminbot.amo import ids
from adminbot.promo_callback.sync import (
    MODE_LIVE, MODE_REHEARSAL, PromoCallback, PromoCallbackState, PromoCallbackSync,
    deal_name, note_text)
from adminbot.tg.promo_cards import failure_text, rehearsal_text
from tests.fakes import FakeAmo

AMO_URL = "https://example.amocrm.ru"
CREATED = datetime(2026, 9, 23, 9, 5, tzinfo=timezone.utc)          # 12:05 МСК


def _callback(callback_id: int, *, phone: str = "+79601861067", name: Optional[str] = "Ирина",
              source: str = "client", text: Optional[str] = "1") -> PromoCallback:
    return PromoCallback(id=callback_id, source=source, phone=phone, name=name,
                         response_text=text, created_at=CREATED,
                         client_id=100 if source == "client" else None,
                         lead_id=None if source == "client" else 500)


def _contact(contact_id: int, *phones: str) -> dict:
    return {"id": contact_id, "name": "Контакт",
            "custom_fields_values": [{"field_code": "PHONE",
                                      "values": [{"value": phone} for phone in phones]}]}


class MemorySource:
    def __init__(self, *callbacks: PromoCallback) -> None:
        self.rows = {callback.id: callback for callback in callbacks}

    def add(self, callback: PromoCallback) -> None:
        self.rows[callback.id] = callback

    async def max_id(self) -> int:
        return max(self.rows, default=0)

    async def after(self, last_id: int, limit: int) -> list[PromoCallback]:
        return [self.rows[key] for key in sorted(self.rows) if key > last_id][:limit]

    async def by_ids(self, callback_ids) -> list[PromoCallback]:
        return [self.rows[key] for key in sorted(callback_ids) if key in self.rows]


class MemoryStore:
    def __init__(self) -> None:
        self.cursors: dict[str, int] = {}
        self.states: dict[tuple[int, str], PromoCallbackState] = {}

    async def cursor(self, mode: str) -> Optional[int]:
        return self.cursors.get(mode)

    async def save_cursor(self, mode: str, last_id: int) -> None:
        self.cursors[mode] = max(self.cursors.get(mode, last_id), last_id)

    async def register(self, mode: str, callback_ids) -> None:
        for callback_id in callback_ids:
            self.states.setdefault((callback_id, mode),
                                   PromoCallbackState(callback_id=callback_id, mode=mode))

    async def pending(self, mode: str) -> list[PromoCallbackState]:
        return [state for (_, state_mode), state in sorted(self.states.items())
                if state_mode == mode and state.status in ("new", "lead_created")]

    async def update(self, callback_id: int, mode: str, **fields: Any) -> None:
        key = (callback_id, mode)
        self.states[key] = replace(self.states[key], **fields)

    def state(self, callback_id: int, mode: str = MODE_LIVE) -> PromoCallbackState:
        return self.states[(callback_id, mode)]


class Letters:
    def __init__(self) -> None:
        self.rehearsals: list[tuple[PromoCallback, Optional[int]]] = []
        self.failures: list[tuple[PromoCallback, Optional[int], str]] = []

    async def rehearsal(self, callback, contact_id) -> None:
        self.rehearsals.append((callback, contact_id))

    async def failure(self, callback, lead_id, error) -> None:
        self.failures.append((callback, lead_id, error))


def _sync(source, store, amo, letters, *, dry_run: bool = False) -> PromoCallbackSync:
    return PromoCallbackSync(source=source, store=store, amo=amo, dry_run=dry_run,
                             on_rehearsal=letters.rehearsal, on_failure=letters.failure)


async def _armed(source, store, amo, letters, *, dry_run: bool = False) -> PromoCallbackSync:
    """Цикл после первого прохода: закладка уже стоит."""
    sync = _sync(source, store, amo, letters, dry_run=dry_run)
    await sync.tick()
    return sync


# --- первый проход: закладка ---

async def test_first_pass_sets_bookmark_and_does_nothing():
    source = MemorySource(_callback(7), _callback(9))
    store, amo, letters = MemoryStore(), FakeAmo(), Letters()

    assert await _sync(source, store, amo, letters).tick() == 0

    assert store.cursors == {MODE_LIVE: 9}             # встал на текущий максимум
    assert store.states == {}
    assert amo.calls == []
    assert letters.rehearsals == letters.failures == []


async def test_first_pass_on_empty_table_sets_zero_and_later_takes_new():
    source, store, amo, letters = MemorySource(), MemoryStore(), FakeAmo(), Letters()
    sync = await _armed(source, store, amo, letters)
    assert store.cursors == {MODE_LIVE: 0}

    source.add(_callback(1))
    assert await sync.tick() == 1
    assert store.state(1).status == "queued"


async def test_only_callbacks_after_bookmark_are_taken():
    source = MemorySource(_callback(7))
    store, amo, letters = MemoryStore(), FakeAmo(), Letters()
    amo.contacts = [_contact(111, "+79601861067")]
    sync = await _armed(source, store, amo, letters)

    source.add(_callback(8))
    await sync.tick()

    assert (7, MODE_LIVE) not in store.states          # до включения — не трогаем
    assert store.state(8).status == "queued"
    assert store.cursors[MODE_LIVE] == 8


# --- бой: контакт, сделка с тегом, примечание ---

async def test_found_contact_gets_lead_with_tag_and_note():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo()
    amo.contacts = [_contact(111, "8 (960) 186-10-67")]
    sync = await _armed(source, store, amo, letters)
    source.add(_callback(1))

    assert await sync.tick() == 1

    assert amo.calls_of("find_contacts_by_phone") == ["9601861067"]
    assert amo.calls_of("create_contact") == []
    [lead] = amo.calls_of("create_lead")
    assert lead == {"name": "Отклик на промо — Ирина", "pipeline_id": ids.PIPELINE_PRIMARY,
                    "status_id": ids.PRIM_STAGE_NEW_LEAD, "contact_id": 111,
                    "tags": ["Отклик на промо"]}
    state = store.state(1)
    assert state.status == "queued"
    assert state.contact_id == 111 and state.lead_id is not None
    assert amo.calls_of("add_note") == [
        (state.lead_id, "🤖 Клиент ответил «1» на промо 23.09 12:05. Клиент из бота.")]
    assert letters.failures == [] and letters.rehearsals == []     # в бою по успеху молчим


async def test_missing_contact_is_created_with_full_number():
    source, store, amo, letters = MemorySource(), MemoryStore(), FakeAmo(), Letters()
    sync = await _armed(source, store, amo, letters)
    source.add(_callback(1, phone="89601861067", name="Юлия", source="lead"))

    await sync.tick()

    assert amo.calls_of("create_contact") == [("Юлия", "+79601861067")]
    contact_id = store.state(1).contact_id
    assert contact_id is not None
    assert amo.calls_of("create_lead")[0]["contact_id"] == contact_id
    assert amo.calls_of("add_note")[0][1].endswith("Лид из бота.")


async def test_contact_found_by_substring_but_other_number_is_not_taken():
    """Амо ищет подстрокой: контакт с другим номером не наш — заводим новый."""
    source, store, amo, letters = MemorySource(), MemoryStore(), FakeAmo(), Letters()
    amo.contacts = [_contact(222, "+79601861068")]
    sync = await _armed(source, store, amo, letters)
    source.add(_callback(1))

    await sync.tick()

    assert len(amo.calls_of("create_contact")) == 1
    assert amo.calls_of("create_lead")[0]["contact_id"] != 222


async def test_retry_after_failure_past_lead_creation_does_not_create_second_lead():
    source, store, amo, letters = MemorySource(), MemoryStore(), FakeAmo(), Letters()
    sync = await _armed(source, store, amo, letters)
    source.add(_callback(1))

    amo.fail_on = "add_note"
    assert await sync.tick() == 0
    state = store.state(1)
    assert state.status == "lead_created" and state.lead_id is not None
    assert state.attempts == 1 and "add_note" in state.last_error

    amo.fail_on = None
    assert await sync.tick() == 1

    assert len(amo.calls_of("create_lead")) == 1                   # вторую не завёл
    assert len(amo.calls_of("create_contact")) == 1                # и второй контакт тоже
    assert amo.calls_of("add_note")[-1][0] == state.lead_id
    assert store.state(1).status == "queued"
    assert letters.failures == []


async def test_retry_after_contact_creation_does_not_create_second_contact():
    source, store, amo, letters = MemorySource(), MemoryStore(), FakeAmo(), Letters()
    sync = await _armed(source, store, amo, letters)
    source.add(_callback(1))

    amo.fail_on = "create_lead"
    await sync.tick()
    assert store.state(1).contact_id is not None and store.state(1).lead_id is None

    amo.fail_on = None
    await sync.tick()

    assert len(amo.calls_of("create_contact")) == 1
    assert store.state(1).status == "queued"


async def test_three_failures_mark_failed_and_write_to_owner_once():
    source, store, amo, letters = MemorySource(), MemoryStore(), FakeAmo(), Letters()
    sync = await _armed(source, store, amo, letters)
    source.add(_callback(1))
    amo.fail_on = "find_contacts_by_phone"

    for _ in range(3):
        await sync.tick()

    state = store.state(1)
    assert state.status == "failed" and state.attempts == 3
    [(callback, lead_id, error)] = letters.failures
    assert callback.id == 1 and lead_id is None and "find_contacts_by_phone" in error

    amo.fail_on = None
    await sync.tick()                                              # больше не берём
    assert len(letters.failures) == 1
    assert amo.calls == []


async def test_two_failures_then_success_does_not_write_to_owner():
    source, store, amo, letters = MemorySource(), MemoryStore(), FakeAmo(), Letters()
    sync = await _armed(source, store, amo, letters)
    source.add(_callback(1))

    amo.fail_on = "create_lead"
    await sync.tick()
    await sync.tick()
    amo.fail_on = None
    await sync.tick()

    assert store.state(1).status == "queued"
    assert letters.failures == []


async def test_unrecognized_phone_fails_at_once_without_crm():
    source, store, amo, letters = MemorySource(), MemoryStore(), FakeAmo(), Letters()
    sync = await _armed(source, store, amo, letters)
    source.add(_callback(1, phone="12345"))

    await sync.tick()

    assert store.state(1).status == "failed"
    assert amo.calls == []
    [(_, lead_id, error)] = letters.failures
    assert lead_id is None and "не распознан" in error


# --- репетиция ---

async def test_rehearsal_reads_crm_writes_nothing_and_reports():
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    amo = FakeAmo(dry_run=True)
    amo.contacts = [_contact(111, "+79601861067")]
    sync = await _armed(source, store, amo, letters, dry_run=True)
    source.add(_callback(1))

    assert await sync.tick() == 1

    assert [name for name, _ in amo.calls] == ["find_contacts_by_phone"]   # только чтение
    assert store.state(1, MODE_REHEARSAL).status == "dry_run"
    [(callback, contact_id)] = letters.rehearsals
    assert callback.id == 1 and contact_id == 111

    await sync.tick()                                              # отчёт один раз
    assert len(letters.rehearsals) == 1


async def test_rehearsal_and_live_keep_separate_bookmarks_and_rows():
    """Репетиция не отнимает работу у боя: своя закладка, свои строки."""
    source, store, letters = MemorySource(), MemoryStore(), Letters()
    rehearsal = await _armed(source, store, FakeAmo(dry_run=True), letters, dry_run=True)
    source.add(_callback(1))
    await rehearsal.tick()
    assert store.cursors == {MODE_REHEARSAL: 1}

    live_amo = FakeAmo()
    live = _sync(source, store, live_amo, letters)
    await live.tick()                                              # первый проход боя
    assert store.cursors[MODE_LIVE] == 1                           # встал на максимум
    assert (1, MODE_LIVE) not in store.states
    source.add(_callback(2))
    await live.tick()

    assert store.state(2).status == "queued"
    assert (2, MODE_REHEARSAL) not in store.states


# --- тексты ---

def test_deal_name_and_note_fall_back_when_fields_are_empty():
    callback = _callback(1, name="  ", text=None)

    assert deal_name(callback) == "Отклик на промо — Клиент"
    assert note_text(callback) == "🤖 Клиент ответил «1» на промо 23.09 12:05. Клиент из бота."


def test_rehearsal_text_names_the_deal_and_the_contact():
    assert rehearsal_text(_callback(1), 111) == (
        "Репетиция: завёл бы сделку «Отклик на промо — Ирина», контакт найден №111, "
        "поставил бы в автозвонок.")
    assert "контакт будет заведён" in rehearsal_text(_callback(1), None)


def test_failure_text_has_name_full_phone_and_error():
    text = failure_text(_callback(1), error="amoCRM 500: сбой")

    assert text.splitlines() == [
        "⚠️ Не смог завести сделку по отклику на промо, позвоните руками.",
        "Ирина · +79601861067",
        "Ошибка: amoCRM 500: сбой",
    ]


def test_failure_text_when_lead_exists_says_autocall_will_take_it():
    text = failure_text(_callback(1), error="сбой", lead_id=777, base_url=AMO_URL,
                        dry_run=True)

    assert text.startswith("🎭 РЕПЕТИЦИЯ · ⚠️ Сделку по отклику на промо завёл")
    assert text.endswith(f"{AMO_URL}/leads/detail/777")
