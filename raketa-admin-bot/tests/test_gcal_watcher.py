"""Наблюдатель календаря: один проход обмена.

Самое важное здесь — три правила, нарушение любого из которых робот не заметит,
а владелец заметит поздно:

1. **При первом включении уже лежащие записи в работу не берутся** (решение 8).
   Иначе робот разом заведёт сделки по всем будущим заказам, которые владелец
   давно ведёт сам.
2. **Закладка сохраняется только после успешного прохода.** Сохранить её раньше —
   значит потерять изменения, которые не успели обработаться.
3. **Одна плохая запись не роняет проход.** Остальные записи должны быть
   обработаны, а сбойная — вернуться в следующий раз.
"""

from datetime import date

import pytest

from adminbot.gcal.client import SyncBatch
from adminbot.gcal.store import MemoryCalendarStore
from adminbot.gcal.watcher import CalendarWatcher

SYNC_FROM = date(2026, 8, 26)

ORDER = {"id": "evt-1", "summary": "Сов! Матрас, Юлия", "status": "confirmed",
         "description": "Матрас/2\n89605379757 Юлия",
         "location": "Панина д 7к2",
         "start": {"dateTime": "2026-08-27T14:30:00+03:00"}}
BLOCK = {"id": "evt-2", "summary": "⛔️Дима", "status": "confirmed",
         "start": {"date": "2026-08-28"}}
DELETED = {"id": "evt-1", "status": "cancelled"}


class FakeCalendar:
    """Google в памяти: отдаёт заготовленные пачки изменений."""

    def __init__(self, *batches):
        self.batches = list(batches)
        self.calls: list[tuple] = []

    async def fetch(self, *, sync_token, sync_from):
        self.calls.append((sync_token, sync_from))
        return self.batches.pop(0) if self.batches else SyncBatch((), sync_token)


class FakeEngine:
    def __init__(self):
        self.seen: list = []
        self.fail_on: str | None = None

    async def process(self, event):
        if event.event_id == self.fail_on:
            raise RuntimeError("поддельный сбой")
        self.seen.append(event)
        return await _link(event)


async def _link(event):
    from adminbot.models import CalendarLink

    return CalendarLink(event_id=event.event_id, kind=event.kind.value, status="done")


def build(calendar, *, engine=None, store=None, **kwargs) -> CalendarWatcher:
    return CalendarWatcher(calendar=calendar, engine=engine or FakeEngine(),
                           store=store or MemoryCalendarStore(),
                           sync_from=SYNC_FROM, **kwargs)


async def test_first_exchange_only_remembers_what_is_already_there():
    """Записи, лежавшие в календаре до включения, робот не трогает (решение 8)."""
    store = MemoryCalendarStore()
    engine = FakeEngine()
    watcher = build(FakeCalendar(SyncBatch((ORDER, BLOCK), "TOKEN-1")),
                    engine=engine, store=store)

    report = await watcher.tick()

    assert engine.seen == []                       # ни одной сделки не заведено
    assert report.known == 2
    assert (await store.get("evt-1")).status == "skipped"
    assert await store.cursor() == ("TOKEN-1", SYNC_FROM)


async def test_new_records_after_the_first_exchange_go_to_work():
    store = MemoryCalendarStore()
    engine = FakeEngine()
    calendar = FakeCalendar(SyncBatch((), "TOKEN-1"), SyncBatch((ORDER,), "TOKEN-2"))
    watcher = build(calendar, engine=engine, store=store)

    await watcher.tick()
    report = await watcher.tick()

    assert [event.event_id for event in engine.seen] == ["evt-1"]
    assert calendar.calls[1][0] == "TOKEN-1"        # второй заход — с закладкой
    assert report.processed == 1
    assert await store.cursor() == ("TOKEN-2", SYNC_FROM)


async def test_deletion_reaches_the_engine():
    """Удалённая запись — единственный признак отмены, потерять её нельзя."""
    store = MemoryCalendarStore()
    engine = FakeEngine()
    calendar = FakeCalendar(SyncBatch((), "T1"), SyncBatch((DELETED,), "T2"))
    watcher = build(calendar, engine=engine, store=store)

    await watcher.tick()
    await watcher.tick()

    assert [event.kind.value for event in engine.seen] == ["cancelled"]


async def test_one_bad_record_does_not_stop_the_pass():
    store = MemoryCalendarStore()
    engine = FakeEngine()
    engine.fail_on = "evt-1"
    other = {**ORDER, "id": "evt-9"}
    calendar = FakeCalendar(SyncBatch((), "T1"), SyncBatch((ORDER, other), "T2"))
    watcher = build(calendar, engine=engine, store=store)

    await watcher.tick()
    report = await watcher.tick()

    assert [event.event_id for event in engine.seen] == ["evt-9"]
    assert report.failures and report.failures[0][0] == "evt-1"
    assert await store.cursor() == ("T2", SYNC_FROM)     # проход всё же состоялся


async def test_google_failure_keeps_the_bookmark():
    """Обмен не удался — закладку не двигаем, иначе потеряем изменения."""
    class BrokenCalendar:
        async def fetch(self, *, sync_token, sync_from):
            raise RuntimeError("Google недоступен")

    store = MemoryCalendarStore()
    await store.save_cursor("TOKEN-1", sync_from=SYNC_FROM)
    watcher = build(BrokenCalendar(), store=store)

    with pytest.raises(RuntimeError):
        await watcher.tick()

    assert await store.cursor() == ("TOKEN-1", SYNC_FROM)


async def test_unfinished_records_are_picked_up_again():
    """Ожидание автосделки продолжается на следующем проходе, а не теряется."""
    store = MemoryCalendarStore()
    engine = FakeEngine()
    calendar = FakeCalendar(SyncBatch((), "T1"), SyncBatch((), "T2"))
    watcher = build(calendar, engine=engine, store=store)
    await watcher.tick()

    from adminbot.gcal.event import parse_event
    await store.create("evt-1", kind="order", phone10="9605379757",
                       event_data=parse_event(ORDER).to_dict())
    await store.update("evt-1", status="waiting_salesbot")

    await watcher.tick()      # обмен пустой — запись пришла из хранилища

    assert [event.event_id for event in engine.seen] == ["evt-1"]
    assert engine.seen[0].phone10 == "9605379757"   # разбор восстановлен целиком


async def test_switch_off_means_no_requests():
    calendar = FakeCalendar(SyncBatch((ORDER,), "T1"))
    watcher = build(calendar, is_enabled=lambda: False)

    report = await watcher.tick()

    assert report.paused is True
    assert calendar.calls == []


async def test_question_card_is_sent_once():
    """Карточка уходит один раз: повтор бы дублировал вопрос каждую минуту."""
    store = MemoryCalendarStore()
    sent = []

    class AskingEngine(FakeEngine):
        async def process(self, event):
            from adminbot.models import CalendarLink
            link = await store.create(event.event_id, kind=event.kind.value)
            return await store.update(event.event_id, status="waiting_owner",
                                      question={"reason": "ask_owner"}) or link

    async def on_question(link):
        sent.append(link.event_id)
        return 777

    calendar = FakeCalendar(SyncBatch((), "T1"), SyncBatch((ORDER,), "T2"))
    watcher = build(calendar, engine=AskingEngine(), store=store, on_question=on_question)

    await watcher.tick()
    await watcher.tick()
    await watcher.tick()

    assert sent == ["evt-1"]
    assert (await store.get("evt-1")).question_msg_id == 777
