"""Наблюдатель автозвонка: один проход опроса амо.

Три правила, которые здесь проверяются (см. докстрину watcher.py):

1. Первый проход только ставит закладку — лежащие на этапе заявки не читаем.
2. Закладка двигается вперёд только по факту пришедших сделок; PK хранилища
   (lead_id) гасит повтор той же сделки на границе закладки.
3. Одна сделка (без телефона, упавшее уведомление, сбойная цепочка у движка)
   не должна ронять весь проход.

Движок здесь — фейк-регистратор, а не настоящий AutocallEngine: у настоящего
движка есть собственное окно звонков, завязанное на реальные часы
(datetime.now внутри tick недоступен тестам для подмены), и он уже проверен
своим набором тестов (test_autocall_engine.py). Здесь важно только то, какую
цепочку и когда наблюдатель ей отдаёт — это и проверяет регистратор.
Телефоны — вымышленные десятизначные номера.
"""

from __future__ import annotations

from datetime import datetime, timezone

from adminbot.autocall.store import MemoryAutocallStore
from adminbot.autocall.watcher import AutocallWatcher

SITE_TAG = "Заявка с сайта"


def site_lead(lead_id: int, created_at: int, *, contact_id: int | None = None,
              tag: str | None = SITE_TAG) -> dict:
    """Сделка амо на этапе «Новый лид» — ровно то, что отдаёт find_leads_created_since."""
    embedded: dict = {}
    if tag:
        embedded["tags"] = [{"name": tag}]
    if contact_id is not None:
        embedded["contacts"] = [{"id": contact_id, "is_main": True}]
    return {"id": lead_id, "created_at": created_at, "_embedded": embedded}


def contact_with_phone(contact_id: int, phone: str) -> dict:
    return {"id": contact_id,
            "custom_fields_values": [{"field_code": "PHONE", "values": [{"value": phone}]}]}


class FakeSiteAmo:
    """amoCRM в памяти: минимум, который нужен наблюдателю.

    В отличие от боевого клиента, ответ не фильтрует по created_from_ts сам —
    тест решает, какие сделки «уже пришли» на момент опроса, как FakeCalendar
    в тестах календаря отдаёт заготовленные пачки, а не считает синтоксен.
    """

    def __init__(self, leads: list[dict] | None = None, contacts: list[dict] | None = None):
        self.leads: list[dict] = list(leads or [])
        self.contacts: dict[int, dict] = {c["id"]: c for c in (contacts or [])}
        self.calls: list[tuple[int, int, int]] = []

    async def find_leads_created_since(self, pipeline_id, status_id, created_from_ts):
        self.calls.append((pipeline_id, status_id, created_from_ts))
        return list(self.leads)

    async def get_contact(self, contact_id):
        return self.contacts.get(contact_id)


class FakeEngine:
    """Движок-регистратор: запоминает, по каким цепочкам его вызвали.

    fail_on — набор lead_id, по которым process_due должен упасть: так
    проверяется, что сбойная цепочка не мешает соседней.
    """

    def __init__(self):
        self.seen: list[tuple[int, datetime]] = []
        self.fail_on: set[int] = set()

    async def process_due(self, link, now):
        if link.lead_id in self.fail_on:
            raise RuntimeError("поддельный сбой движка")
        self.seen.append((link.lead_id, now))


def build(amo, *, store=None, engine=None, **kwargs) -> AutocallWatcher:
    return AutocallWatcher(amo=amo, engine=engine or FakeEngine(),
                           store=store or MemoryAutocallStore(), **kwargs)


async def test_first_pass_only_sets_bookmark_and_reads_nothing():
    """Заявки, лежащие на этапе до включения, — дело владельца, не робота."""
    amo = FakeSiteAmo(leads=[site_lead(1001, 1_900_000_000, contact_id=501)])
    store = MemoryAutocallStore()
    watcher = build(amo, store=store)

    report = await watcher.tick()

    assert report.first_run is True
    assert report.seen == 0
    assert amo.calls == []                          # find_leads_created_since не вызван
    assert await store.cursor() is not None
    assert await store.get(1001) is None


async def test_deal_without_site_tag_is_ignored():
    amo = FakeSiteAmo()
    store = MemoryAutocallStore()
    watcher = build(amo, store=store)
    await watcher.tick()                             # первый проход — только закладка

    amo.leads = [site_lead(2001, 1_900_000_100, contact_id=None, tag=None)]
    report = await watcher.tick()

    assert report.seen == 1
    assert report.new_chains == 0
    assert await store.get(2001) is None


async def test_new_site_lead_with_phone_creates_chain_and_hands_it_to_engine_same_pass():
    amo = FakeSiteAmo(contacts=[contact_with_phone(501, "9991110001")])
    store = MemoryAutocallStore()
    engine = FakeEngine()
    watcher = build(amo, store=store, engine=engine)
    await watcher.tick()

    amo.leads = [site_lead(3001, 1_900_000_200, contact_id=501)]
    report = await watcher.tick()

    assert report.new_chains == 1
    assert report.processed == 1
    lead = await store.get(3001)
    assert lead.phone10 == "9991110001"
    assert lead.status == "queued"
    assert lead.next_action_at is None               # due() отдал её немедленно
    assert [lead_id for lead_id, _ in engine.seen] == [3001]


async def test_repeated_deal_at_bookmark_boundary_does_not_create_second_chain():
    """Граница закладки читается включительно — амо может отдать ту же сделку снова."""
    amo = FakeSiteAmo(contacts=[contact_with_phone(502, "9991110002")])
    store = MemoryAutocallStore()
    watcher = build(amo, store=store)
    await watcher.tick()

    amo.leads = [site_lead(3002, 1_900_000_300, contact_id=502)]
    first = await watcher.tick()                      # заявка впервые видна
    second = await watcher.tick()                     # та же заявка снова (граница)

    assert first.new_chains == 1
    assert second.new_chains == 0
    assert len(store.leads) == 1


async def test_no_phone_gives_up_and_notifies_owner():
    amo = FakeSiteAmo()                                # контактов нет вовсе
    store = MemoryAutocallStore()
    notified: list[int] = []

    async def on_no_phone(lead_id: int) -> None:
        notified.append(lead_id)

    watcher = build(amo, store=store, on_no_phone=on_no_phone)
    await watcher.tick()

    amo.leads = [site_lead(4001, 1_900_000_400, contact_id=None)]
    report = await watcher.tick()

    lead = await store.get(4001)
    assert lead.status == "gave_up"
    assert lead.phone10 is None
    assert lead.last_error == "телефон из сделки не извлёкся"
    assert report.no_phone == 1
    assert notified == [4001]
    logged = store.actions_of("no_phone")
    assert logged and logged[0]["lead_id"] == 4001


async def test_failing_on_no_phone_notification_does_not_stop_the_pass():
    amo = FakeSiteAmo()
    store = MemoryAutocallStore()

    async def broken_on_no_phone(lead_id: int) -> None:
        raise RuntimeError("Telegram недоступен")

    watcher = build(amo, store=store, on_no_phone=broken_on_no_phone)
    await watcher.tick()

    amo.leads = [site_lead(4002, 1_900_000_500, contact_id=None)]
    report = await watcher.tick()                      # не должно упасть

    assert report.no_phone == 1
    assert (await store.get(4002)).status == "gave_up"


async def test_bookmark_moves_to_max_created_at_and_stays_on_empty_response():
    amo = FakeSiteAmo(contacts=[contact_with_phone(503, "9991110003"),
                                contact_with_phone(504, "9991110004")])
    store = MemoryAutocallStore()
    watcher = build(amo, store=store)
    await watcher.tick()

    amo.leads = [site_lead(5001, 1_900_000_600, contact_id=503),
                site_lead(5002, 1_900_000_900, contact_id=504)]
    await watcher.tick()

    expected = datetime.fromtimestamp(1_900_000_900, tz=timezone.utc)
    assert await store.cursor() == expected

    amo.leads = []                                      # пустой ответ
    await watcher.tick()

    assert await store.cursor() == expected             # закладка не сдвинулась


async def test_engine_failure_on_one_chain_does_not_stop_the_other():
    amo = FakeSiteAmo(contacts=[contact_with_phone(505, "9991110005"),
                                contact_with_phone(506, "9991110006")])
    store = MemoryAutocallStore()
    engine = FakeEngine()
    watcher = build(amo, store=store, engine=engine)
    await watcher.tick()

    amo.leads = [site_lead(6001, 1_900_001_000, contact_id=505),
                site_lead(6002, 1_900_001_100, contact_id=506)]
    engine.fail_on = {6001}
    report = await watcher.tick()

    assert report.processed == 2
    assert report.failures and report.failures[0][0] == 6001
    assert [lead_id for lead_id, _ in engine.seen] == [6002]


class PartialFailStore:
    """Оборачивает MemoryAutocallStore и роняет update — сценарий ревью:
    хранилище падает между шагами create → update → log_action. С атомарным
    create (готовый статус уже в первой команде) ветка "нет телефона"
    update больше не вызывает вовсе, поэтому даже сломанный update не мешает
    цепочке закрыться, а не зависнуть "queued"-зомби без номера.
    """

    def __init__(self, inner: MemoryAutocallStore) -> None:
        self._inner = inner

    async def get(self, lead_id):
        return await self._inner.get(lead_id)

    async def create(self, lead_id, *, phone10=None, **fields):
        return await self._inner.create(lead_id, phone10=phone10, **fields)

    async def update(self, lead_id, **fields):
        raise OSError("хранилище недоступно на update")

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


async def test_no_phone_finish_survives_a_broken_update():
    """Регрессия ревью: раньше create → update оставлял "queued"-зомби
    без телефона, если update падал между шагами. Атомарный create закрывает
    цепочку сразу, и сломанный update этой ветки вообще не касается."""
    inner = MemoryAutocallStore()
    store = PartialFailStore(inner)
    amo = FakeSiteAmo()
    watcher = build(amo, store=store)
    await watcher.tick()

    amo.leads = [site_lead(4003, 1_900_000_700, contact_id=None)]
    report = await watcher.tick()                  # update сломан, но не нужен

    assert report.no_phone == 1
    lead = await inner.get(4003)
    assert lead.status == "gave_up"                # не "queued"-зомби
    assert lead.phone10 is None


async def test_switch_off_means_no_requests():
    amo = FakeSiteAmo(leads=[site_lead(7001, 1_900_002_000, contact_id=None)])
    watcher = build(amo, is_enabled=lambda: False)

    report = await watcher.tick()

    assert report.paused is True
    assert amo.calls == []
