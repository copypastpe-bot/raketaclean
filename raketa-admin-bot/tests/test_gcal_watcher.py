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

    def __init__(self, *batches, calendar_id="main@gmail.com"):
        self.calendar_id = calendar_id
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


MAIN = "main@gmail.com"
BRIGADE = "brigade@group.calendar.google.com"


def build(*calendars, engine=None, store=None, **kwargs) -> CalendarWatcher:
    return CalendarWatcher(calendars=calendars, engine=engine or FakeEngine(),
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
    assert await store.cursor(MAIN) == ("TOKEN-1", SYNC_FROM)


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
    assert await store.cursor(MAIN) == ("TOKEN-2", SYNC_FROM)


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
    assert await store.cursor(MAIN) == ("T2", SYNC_FROM)  # проход всё же состоялся


class BrokenCalendar:
    """Google, который не отвечает по этому календарю."""

    def __init__(self, calendar_id=MAIN):
        self.calendar_id = calendar_id

    async def fetch(self, *, sync_token, sync_from):
        raise RuntimeError("Google недоступен")


async def test_google_failure_keeps_the_bookmark():
    """Обмен не удался — закладку не двигаем, иначе потеряем изменения."""
    store = MemoryCalendarStore()
    await store.save_cursor(MAIN, "TOKEN-1", sync_from=SYNC_FROM)
    watcher = build(BrokenCalendar(), store=store)

    report = await watcher.tick()

    assert await store.cursor(MAIN) == ("TOKEN-1", SYNC_FROM)
    assert [name for name, _ in report.calendars_failed] == [MAIN]
    assert report.failures == ()          # сбой календаря — не сбойная запись


# --- когда календарей несколько (решение владельца 2026-09-01) ---


async def test_both_calendars_are_exchanged_in_one_pass():
    """Мебель ведут мастера в основном календаре, уборки — бригадир в своём."""
    store = MemoryCalendarStore()
    engine = FakeEngine()
    cleaning = {**ORDER, "id": "evt-clean", "summary": "Сор! Уборка, Лариса"}
    main = FakeCalendar(SyncBatch((), "M1"), SyncBatch((ORDER,), "M2"))
    brigade = FakeCalendar(SyncBatch((), "B1"), SyncBatch((cleaning,), "B2"),
                           calendar_id=BRIGADE)
    watcher = build(main, brigade, engine=engine, store=store)

    await watcher.tick()                                # первый обмен — только помним
    report = await watcher.tick()

    assert sorted(event.event_id for event in engine.seen) == ["evt-1", "evt-clean"]
    assert report.processed == 2
    assert await store.cursor(MAIN) == ("M2", SYNC_FROM)
    assert await store.cursor(BRIGADE) == ("B2", SYNC_FROM)


async def test_a_broken_calendar_does_not_stop_the_other():
    """Сбой Google по одному календарю не должен останавливать работу по другому."""
    store = MemoryCalendarStore()
    engine = FakeEngine()
    brigade = FakeCalendar(SyncBatch((), "B1"), SyncBatch((ORDER,), "B2"),
                           calendar_id=BRIGADE)
    watcher = build(BrokenCalendar(), brigade, engine=engine, store=store)

    await watcher.tick()
    report = await watcher.tick()

    assert [event.event_id for event in engine.seen] == ["evt-1"]   # второй отработал
    assert await store.cursor(MAIN) == (None, None)                 # первый — нет
    assert await store.cursor(BRIGADE) == ("B2", SYNC_FROM)
    assert [name for name, _ in report.calendars_failed] == [MAIN]


async def test_unfinished_work_is_handled_once_not_once_per_calendar():
    """Незавершённая запись доделывается один раз за проход, сколько бы ни было
    календарей: иначе робот дважды сходил бы за одной сделкой в amoCRM."""
    store = MemoryCalendarStore()
    engine = FakeEngine()
    main = FakeCalendar(SyncBatch((), "M1"))
    brigade = FakeCalendar(SyncBatch((), "B1"), calendar_id=BRIGADE)
    watcher = build(main, brigade, engine=engine, store=store)
    await watcher.tick()

    await store.create("evt-waiting", kind="order", phone10="9601861067",
                       event_data={"event_id": "evt-waiting", "kind": "order"})
    await store.update("evt-waiting", status="waiting_salesbot")
    await watcher.tick()

    assert [event.event_id for event in engine.seen] == ["evt-waiting"]


async def test_only_the_first_calendar_inherits_the_old_bookmark():
    """Закладку «до нескольких календарей» забирает основной календарь.

    Отдать её второму — значит объявить его записи давно прочитанными и
    пропустить всё, что бригадир завёл до включения.
    """
    asked: list[tuple[str, bool]] = []

    class SpyStore(MemoryCalendarStore):
        async def cursor(self, calendar_id, *, inherit_legacy=False):
            asked.append((calendar_id, inherit_legacy))
            return await super().cursor(calendar_id, inherit_legacy=inherit_legacy)

    store = SpyStore()
    watcher = build(FakeCalendar(SyncBatch((), "M1")),
                    FakeCalendar(SyncBatch((), "B1"), calendar_id=BRIGADE),
                    store=store)

    await watcher.tick()

    assert asked == [(MAIN, True), (BRIGADE, False)]


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


async def test_known_district_without_a_field_in_amo_is_reported():
    """Балахнинский робот понимает, но в списке амо его нет — это не «непонятная»
    приставка, а недостающее значение в CRM. Владелец должен видеть разницу."""
    store = MemoryCalendarStore()
    balakhna = {**ORDER, "id": "evt-bal", "summary": "Бал! Диван, Ирина"}
    calendar = FakeCalendar(SyncBatch((), "T1"), SyncBatch((balakhna,), "T2"))
    watcher = build(calendar, store=store)

    await watcher.tick()
    report = await watcher.tick()

    assert report.districts_missing == ("балахнинский",)
    assert report.unknown_districts == ()


async def test_rehearsal_reports_every_record_to_the_owner():
    """В репетиции владелец узнаёт и об уверенных решениях, а не только о вопросах."""
    store = MemoryCalendarStore()
    sent: list = []

    async def on_rehearsal(link, actions) -> None:
        sent.append((link.event_id, [a["action"] for a in actions]))

    class WorkingEngine(FakeEngine):
        async def process(self, event):
            link = await store.create(event.event_id, kind=event.kind.value)
            await store.log(event.event_id, "create_lead", dry_run=True, amo_id=41400009)
            self.seen.append(event)
            return await store.update(event.event_id, status="done") or link

    calendar = FakeCalendar(SyncBatch((), "T1"), SyncBatch((ORDER,), "T2"))
    watcher = build(calendar, engine=WorkingEngine(), store=store,
                    dry_run=True, on_rehearsal=on_rehearsal)

    await watcher.tick()
    await watcher.tick()

    assert sent == [("evt-1", ["create_lead"])]


async def test_live_mode_stays_quiet():
    """В бою робот работает молча: отчёт по каждому заказу — это спам."""
    store = MemoryCalendarStore()
    sent: list = []

    async def on_rehearsal(link, actions) -> None:
        sent.append(link.event_id)

    calendar = FakeCalendar(SyncBatch((), "T1"), SyncBatch((ORDER,), "T2"))
    watcher = build(calendar, store=store, dry_run=False, on_rehearsal=on_rehearsal)

    await watcher.tick()
    await watcher.tick()

    assert sent == []


# --- отчёт о сделанной работе (неделя наблюдения) ---


class FinishingEngine(FakeEngine):
    """Работа по записи заканчивается за один проход: сделка реализации уже была."""

    def __init__(self, store):
        super().__init__()
        self.store = store

    async def process(self, event):
        link = await self.store.create(event.event_id, kind=event.kind.value)
        self.seen.append(event)
        return await self.store.update(event.event_id, status="done",
                                       real_lead_id=31600001) or link


class SlowSalesbot(FakeEngine):
    """Два прохода ждём автосделку, на третьем она появляется.

    Ровно то, что происходит вживую: робот заводит лид и передаёт его в работу,
    а сделку реализации сейлзбот создаёт спустя минуту-другую.
    """

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.attempts = 0

    async def process(self, event):
        link = await self.store.create(event.event_id, kind=event.kind.value)
        self.attempts += 1
        if self.attempts == 1:
            await self.store.log(event.event_id, "create_lead", dry_run=False,
                                 amo_id=41400009)
        if self.attempts < 3:
            return await self.store.update(event.event_id, status="waiting_salesbot",
                                           primary_lead_id=41400009) or link
        return await self.store.update(event.event_id, status="done",
                                       real_lead_id=31600001) or link


async def test_one_record_is_handled_once_per_pass():
    """Запись, пришедшая с изменениями и лежащая в незавершённых, — одна работа.

    Незавершённые робот добирает из своего хранилища, потому что ждать автосделку
    он может дольше, чем живёт одна пачка изменений. Но запись, которая только что
    пришла из календаря, попадает в оба списка сразу — и до этой проверки движок
    ходил по ней в amoCRM дважды за проход.
    """
    store = MemoryCalendarStore()
    engine = SlowSalesbot(store)

    calendar = FakeCalendar(SyncBatch((), "T1"), SyncBatch((ORDER,), "T2"))
    watcher = build(calendar, engine=engine, store=store, dry_run=False)

    await watcher.tick()
    await watcher.tick()

    assert engine.attempts == 1


async def test_finished_work_is_reported_once():
    """Отчёт уходит один раз — когда работа по записи закончена целиком.

    Пока робот ждёт автосделку сейлзбота, он молчит: ожидание — это середина
    работы, а не сделанное дело. Раньше отчёт уходил на каждом проходе, и по
    одной записи владелец получал три почти одинаковых сообщения, причём в двух
    первых ссылка вела на лид, а не на сделку.
    """
    store = MemoryCalendarStore()
    sent: list = []

    async def on_done(link, actions):
        sent.append((link.event_id, link.real_lead_id))
        return 555

    calendar = FakeCalendar(SyncBatch((), "T1"), SyncBatch((ORDER,), "T2"))
    watcher = build(calendar, engine=SlowSalesbot(store), store=store,
                    dry_run=False, on_done=on_done)

    await watcher.tick()                           # включение: запоминаем календарь
    await watcher.tick()                           # завёл лид, ждёт автосделку
    assert sent == []                              # ожидание — не повод писать
    await watcher.tick()                           # всё ещё ждёт
    assert sent == []
    await watcher.tick()                           # автосделка появилась — готово

    assert sent == [("evt-1", 31600001)]           # одно сообщение, ссылка на сделку
    assert (await store.get("evt-1")).done_msg_id == 555


async def test_a_finished_record_is_not_reported_again():
    """Запись правят в календаре и после проведения — отчёт не повторяется.

    Изменение, до сделки не дошедшее (скажем, поправили имя клиента), заново
    прогоняет запись через движок. Без отметки «уже отчитался» владелец получил
    бы второе сообщение о работе, сделанной вчера.
    """
    store = MemoryCalendarStore()
    sent: list = []

    async def on_done(link, actions):
        sent.append(link.event_id)
        return 777

    calendar = FakeCalendar(SyncBatch((), "T1"), SyncBatch((ORDER,), "T2"),
                            SyncBatch((ORDER,), "T3"))
    watcher = build(calendar, engine=FinishingEngine(store), store=store,
                    dry_run=False, on_done=on_done)

    await watcher.tick()
    await watcher.tick()
    await watcher.tick()

    assert sent == ["evt-1"]


async def test_the_mark_is_set_only_after_telegram_took_the_message():
    """Телеграм не ответил — отметку не ставим, иначе отчёт пропадёт навсегда."""
    store = MemoryCalendarStore()
    answers = [None, 999]
    sent: list = []

    async def on_done(link, actions):
        sent.append(link.event_id)
        return answers.pop(0)

    calendar = FakeCalendar(SyncBatch((), "T1"), SyncBatch((ORDER,), "T2"),
                            SyncBatch((ORDER,), "T3"))
    watcher = build(calendar, engine=FinishingEngine(store), store=store,
                    dry_run=False, on_done=on_done)

    await watcher.tick()
    await watcher.tick()                           # Telegram промолчал
    assert (await store.get("evt-1")).done_msg_id is None
    await watcher.tick()                           # повторная попытка

    assert sent == ["evt-1", "evt-1"]
    assert (await store.get("evt-1")).done_msg_id == 999
