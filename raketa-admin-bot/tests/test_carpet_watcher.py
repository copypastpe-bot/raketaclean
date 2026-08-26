"""Наблюдатель почты: письмо → разобранные строки → сделки.

Проверяем поведение цикла, а не разбор файла (он проверен отдельно): когда
письмо считается разобранным, что происходит при сбое, и как незавершённые
строки доводятся до конца после того, как письмо уже прочитано.
"""

import asyncio
from datetime import date
from decimal import Decimal

from adminbot.carpets.report import CarpetRow
from adminbot.carpets.watcher import CarpetWatcher
from adminbot.mail import Letter
from adminbot.models import CarpetLink

REPORT = b"PK\x03\x04report"


def row(partner_id=44426, **overrides) -> CarpetRow:
    values = dict(partner_id=partner_id, phone10="9601945325", amount=Decimal("3995"),
                  return_date=date(2026, 8, 23), added_date=date(2026, 8, 12))
    values.update(overrides)
    return CarpetRow(**values)


class FakeMailBox:
    def __init__(self, letters):
        self.letters = list(letters)
        self.seen: list[str] = []
        self.fetches = 0

    async def fetch_new(self):
        self.fetches += 1
        return list(self.letters)

    async def mark_seen(self, uid):
        self.seen.append(uid)


class FakeEngine:
    """Движок-двойник: возвращает заранее назначенный итог по каждой строке."""

    def __init__(self, results=None):
        self.results = results or {}
        self.processed: list[int] = []
        self.raise_on: set[int] = set()
        self.store = FakeStore()

    async def process_row(self, row, source_file=None):
        self.processed.append(row.partner_id)
        if row.partner_id in self.raise_on:
            raise RuntimeError("амо не отвечает")
        # Настоящий движок берёт состояние из хранилища: если строку уже трогали,
        # у неё, например, записан id отправленной карточки.
        known = self.store.links.get(row.partner_id)
        if known is not None:
            return known
        link = self.results.get(row.partner_id) or CarpetLink(
            partner_id=row.partner_id, phone10=row.phone10 or "", status="done")
        self.store.links[row.partner_id] = link
        return link


class FakeStore:
    def __init__(self):
        self.links: dict[int, CarpetLink] = {}
        self.remembered: list[tuple] = []
        self.pending: list[tuple[CarpetRow, CarpetLink]] = []

    async def update(self, partner_id, **fields):
        from dataclasses import replace
        self.links[partner_id] = replace(self.links[partner_id], **fields)
        return self.links[partner_id]

    async def letter_processed(self, uid) -> bool:
        return uid in [item[0] for item in self.remembered]

    async def remember_letter(self, uid, subject, files, rows_total) -> None:
        self.remembered.append((uid, subject, files, rows_total))

    async def pending_rows(self):
        return list(self.pending)


def letter(uid="1", subject="отчёт с 17.08 по 23.08") -> Letter:
    return Letter(uid=uid, subject=subject, sender="raketa@raketaclean.ru",
                  attachments={"Договоры (11).xlsx": REPORT})


def make_watcher(mailbox, engine, *, rows=None, **kwargs) -> CarpetWatcher:
    return CarpetWatcher(engine=engine, mailbox=mailbox, store=engine.store,
                         parse=lambda data: list(rows if rows is not None else [row()]),
                         **kwargs)


# --- обычный проход ---

async def test_letter_rows_are_processed_and_letter_marked_read():
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()

    report = await make_watcher(mailbox, engine).tick()

    assert engine.processed == [44426]
    assert report.processed == 1 and report.letters == 1
    assert mailbox.seen == ["1"]                       # письмо разобрано
    assert engine.store.remembered[0][0] == "1"        # и записано в базу


async def test_already_processed_letter_is_skipped():
    """Пометка в почте могла не поставиться — вторая страховка в базе."""
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()
    watcher = make_watcher(mailbox, engine)
    await engine.store.remember_letter("1", "отчёт", ["Договоры (11).xlsx"], 1)

    report = await watcher.tick()

    assert engine.processed == []
    assert mailbox.seen == ["1"]                       # но прочитанным пометим


async def test_paused_watcher_touches_nothing():
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()

    report = await make_watcher(mailbox, engine, is_enabled=lambda: False).tick()

    assert report.paused is True
    assert mailbox.fetches == 0 and engine.processed == []


# --- сбои ---

async def test_letter_with_a_failed_row_is_not_marked_read():
    """Если строка не разобралась, письмо остаётся в работе — разберём позже."""
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()
    engine.raise_on = {44426}

    report = await make_watcher(mailbox, engine).tick()

    assert report.failures and report.failures[0][0] == 44426
    assert mailbox.seen == []                          # письмо не «съедено»
    assert engine.store.remembered == []


async def test_broken_attachment_does_not_stop_the_others():
    mailbox, engine = FakeMailBox([letter(uid="1"), letter(uid="2")]), FakeEngine()

    def parse(data):
        if not parse.first_done:
            parse.first_done = True
            raise ValueError("В отчёте партнёра нет колонок: Телефон")
        return [row(44535)]
    parse.first_done = False

    watcher = CarpetWatcher(engine=engine, mailbox=mailbox, store=engine.store, parse=parse)
    report = await watcher.tick()

    assert engine.processed == [44535]                 # второе письмо разобрано
    assert mailbox.seen == ["2"]
    assert any("нет колонок" in text for _, text in report.failures)


# --- незавершённые строки ---

async def test_pending_rows_are_finished_on_the_next_tick():
    """Заказ ждал автосделку сейлзбота — письма уже нет, строка взята из базы."""
    mailbox, engine = FakeMailBox([]), FakeEngine()
    waiting = CarpetLink(partner_id=44535, phone10="9202994600", status="waiting_salesbot")
    engine.store.pending = [(row(44535, phone10="9202994600"), waiting)]

    report = await make_watcher(mailbox, engine).tick()

    assert engine.processed == [44535]
    assert report.processed == 1


# --- вопросы владельцу ---

async def test_question_is_asked_once():
    mailbox, engine = FakeMailBox([letter()]), FakeEngine(
        {44426: CarpetLink(partner_id=44426, phone10="9601945325", status="waiting_owner")})
    sent = []

    async def on_question(row, link):
        sent.append(row.partner_id)
        return 777

    watcher = make_watcher(mailbox, engine, on_question=on_question)
    await watcher.tick()

    assert sent == [44426]
    assert engine.store.links[44426].question_msg_id == 777

    # второй проход по той же строке карточку не повторяет
    engine.store.pending = [(row(), engine.store.links[44426])]
    mailbox.letters = []
    await watcher.tick()
    assert sent == [44426]


# --- отчёт владельцу ---

async def test_report_counts_outcomes():
    mailbox = FakeMailBox([letter()])
    engine = FakeEngine({
        44426: CarpetLink(partner_id=44426, phone10="1", status="done"),
        44535: CarpetLink(partner_id=44535, phone10="2", status="waiting_owner"),
        44345: CarpetLink(partner_id=44345, phone10="3", status="waiting_salesbot"),
    })
    reports = []

    async def on_report(letter_, report):
        reports.append((letter_.subject, report))

    watcher = make_watcher(mailbox, engine, on_report=on_report,
                           rows=[row(44426), row(44535), row(44345)])
    await watcher.tick()

    subject, report = reports[0]
    assert subject == "отчёт с 17.08 по 23.08"
    assert report.by_status == {"done": 1, "waiting_owner": 1, "waiting_salesbot": 1}


# --- цикл ---

async def test_run_forever_sleeps_between_ticks():
    mailbox, engine = FakeMailBox([]), FakeEngine()
    stop = asyncio.Event()
    naps = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            stop.set()

    watcher = make_watcher(mailbox, engine, poll_interval_sec=3600, sleep=fake_sleep)
    await watcher.run_forever(stop)

    assert naps == [3600, 3600]                        # письма приходят раз в неделю


async def test_unfinished_work_shortens_the_pause():
    """Ждём автосделку сейлзбота — час простоя здесь был бы нелепым."""
    mailbox, engine = FakeMailBox([]), FakeEngine()
    waiting = CarpetLink(partner_id=44535, phone10="9202994600", status="waiting_salesbot")
    engine.store.pending = [(row(44535, phone10="9202994600"), waiting)]
    engine.results = {44535: waiting}
    stop = asyncio.Event()
    naps = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        stop.set()

    watcher = make_watcher(mailbox, engine, poll_interval_sec=3600, sleep=fake_sleep)
    await watcher.run_forever(stop)

    assert naps == [60]


async def test_quiet_watcher_sleeps_the_full_hour():
    mailbox, engine = FakeMailBox([]), FakeEngine()
    stop = asyncio.Event()
    naps = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        stop.set()

    watcher = make_watcher(mailbox, engine, poll_interval_sec=3600, sleep=fake_sleep)
    await watcher.run_forever(stop)

    assert naps == [3600]
