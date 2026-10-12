"""Наблюдатель почты: письмо → разобранные строки → сделки.

Проверяем поведение цикла, а не разбор файла (он проверен отдельно): когда
письмо считается разобранным, что происходит при сбое, и как незавершённые
строки доводятся до конца после того, как письмо уже прочитано.
"""

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from adminbot.carpets.report import CarpetRow
from adminbot.carpets.watcher import ACTIVE_STATUSES, CarpetLetterSummary, CarpetWatcher
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
        # Поля, которые строка получит на ближайшем process_row: так двойник «проходит
        # шаг» или «доводит строку» во время прохода, а не до него.
        self.next_run: dict[int, dict] = {}
        self.store = FakeStore()

    async def process_row(self, row, source_file=None):
        self.processed.append(row.partner_id)
        if row.partner_id in self.raise_on:
            raise RuntimeError("амо не отвечает")
        # Настоящий движок берёт состояние из хранилища: если строку уже трогали,
        # у неё, например, записан id отправленной карточки.
        known = self.store.links.get(row.partner_id)
        if known is not None:
            if row.partner_id in self.next_run:
                known = replace(known, **self.next_run.pop(row.partner_id))
                self.store.links[row.partner_id] = known
            return known
        link = self.results.get(row.partner_id) or CarpetLink(
            partner_id=row.partner_id, phone10=row.phone10 or "", status="done")
        self.store.links[row.partner_id] = link
        return link


class FakeStore:
    def __init__(self):
        self.links: dict[int, CarpetLink] = {}
        self.remembered: list[tuple] = []
        self.held: dict[str, tuple] = {}
        self.released: list[str] = []
        self.pending: list[tuple[CarpetRow, CarpetLink]] = []
        self.logged: list[tuple] = []
        self.reports: dict[str, dict] = {}    # итог письма: uid → заказы и отметка

    async def get(self, partner_id):
        return self.links.get(partner_id)

    async def update(self, partner_id, **fields):
        from dataclasses import replace
        self.links[partner_id] = replace(self.links[partner_id], **fields)
        return self.links[partner_id]

    async def update_and_log(self, partner_id, *, action, dry_run, payload=None, **fields):
        self.logged.append((partner_id, action, payload))
        return await self.update(partner_id, **fields)

    async def letter_state(self, uid):
        # Порядок проверок — как в MemoryCarpetStore.letter_state (задача 4, 16.09):
        # released → held → processed → None. Результат тот же в любом порядке,
        # но разный порядок в двойнике и в бою путает при чтении теста.
        if uid in self.released:
            return "released"
        if uid in self.held:
            return "held"
        return "processed" if uid in [item[0] for item in self.remembered] else None

    async def remember_letter(self, uid, subject, files, rows_total) -> None:
        self.held.pop(uid, None)
        if uid in self.released:
            self.released.remove(uid)
        self.remembered.append((uid, subject, files, rows_total))

    async def hold_letter(self, uid, subject, files, rows_total, reason) -> None:
        self.held[uid] = (subject, list(files), rows_total, reason)

    def release(self, uid) -> None:
        """Владелец снял отложение: письмо разрешено провести."""
        self.held.pop(uid, None)
        self.released.append(uid)

    async def pending_rows(self):
        # Как боевое хранилище: строка — в её нынешнем виде и только незавершённая.
        rows = []
        for row_, link in self.pending:
            current = self.links.get(row_.partner_id, link)
            if current.status in ACTIVE_STATUSES:
                rows.append((row_, current))
        return rows

    # Итог письма — та же логика, что у MemoryCarpetStore (задача 1).

    async def track_letter(self, uid, subject, partner_ids, refused_ids) -> None:
        self.reports.setdefault(uid, {"uid": uid, "subject": subject,
                                      "partner_ids": list(partner_ids),
                                      "refused_ids": list(refused_ids), "reported": False})

    async def letters_to_report(self):
        return [{key: value for key, value in item.items() if key != "reported"}
                for item in self.reports.values() if not item["reported"]]

    async def mark_letter_reported(self, uid) -> None:
        if uid in self.reports:
            self.reports[uid]["reported"] = True

    async def links_by_ids(self, partner_ids):
        return [self.links[pid] for pid in partner_ids if pid in self.links]


class Clock:
    def __init__(self):
        self.moment = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.moment

    def advance(self, **delta):
        self.moment += timedelta(**delta)


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


async def test_broken_attachment_holds_its_letter_but_not_the_others():
    """Неразобранный файл откладывает своё письмо — соседнее разбирается как обычно."""
    mailbox, engine = FakeMailBox([letter(uid="1"), letter(uid="2")]), FakeEngine()
    held = []

    def parse(data):
        if not parse.first_done:
            parse.first_done = True
            raise ValueError("В отчёте партнёра нет колонок: Телефон")
        return [row(44535)]
    parse.first_done = False

    async def on_held(letter_, reason, rows_total, refused_total):
        held.append((letter_.uid, reason))

    watcher = CarpetWatcher(engine=engine, mailbox=mailbox, store=engine.store,
                            parse=parse, on_held=on_held)
    report = await watcher.tick()

    assert engine.processed == [44535]                 # второе письмо разобрано
    assert mailbox.seen == ["2"]
    assert report.held == 1
    assert [uid for uid, _ in held] == ["1"]
    assert "нет колонок" in held[0][1] and "Договоры (11).xlsx" in held[0][1]
    assert "1" in engine.store.held                    # первое письмо ждёт владельца


async def test_broken_attachment_holds_the_whole_letter():
    """Сбойный файл больше не пропускает вперёд остальные вложения того же письма.

    Решение владельца 15.09.2026: неразобранный файл требует взгляда человека,
    а не тихих повторов каждый час.
    """
    two_files = Letter(uid="9", subject="свод за месяц", sender="raketa@raketaclean.ru",
                       attachments={"битый.xlsx": REPORT, "целый.xlsx": REPORT})
    mailbox, engine = FakeMailBox([two_files]), FakeEngine()

    def parse(data):
        if not parse.first_done:
            parse.first_done = True
            raise ValueError("В отчёте партнёра нет колонок: Телефон")
        return [row(44535)]
    parse.first_done = False

    watcher = CarpetWatcher(engine=engine, mailbox=mailbox, store=engine.store, parse=parse)
    report = await watcher.tick()

    assert engine.processed == []                      # второй файл тоже не проводим
    assert mailbox.seen == [] and engine.store.remembered == []
    assert report.held == 1


# --- слишком большое письмо ---

async def test_letter_over_the_limit_is_held():
    """Архив за два года вместо недельного отчёта: не проводим ни одной строки."""
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()
    held = []

    async def on_held(letter_, reason, rows_total, refused_total):
        held.append((letter_.uid, reason, rows_total, refused_total))

    watcher = make_watcher(mailbox, engine, max_rows=2, on_held=on_held,
                           rows=[row(1), row(2), row(3, is_refusal=True)])
    report = await watcher.tick()

    assert engine.processed == []                      # в амо не ушло ничего
    assert mailbox.seen == []                          # письмо осталось в папке
    assert engine.store.remembered == []
    assert held == [("1", "строк 3, порог 2", 3, 1)]
    assert report.held == 1 and report.processed == 0


async def test_held_letter_is_skipped_on_the_next_tick():
    """Владельцу говорим один раз: второй проход проходит мимо молча."""
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()
    held = []

    async def on_held(letter_, reason, rows_total, refused_total):
        held.append(letter_.uid)

    watcher = make_watcher(mailbox, engine, max_rows=1, on_held=on_held,
                           rows=[row(1), row(2)])
    await watcher.tick()
    report = await watcher.tick()

    assert held == ["1"]                               # без повторного сообщения
    assert engine.processed == [] and mailbox.seen == []
    assert report.held == 0


async def test_letter_exactly_at_the_limit_is_processed():
    """Порог — это «больше нельзя», ровно на пороге письмо проводится."""
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()

    watcher = make_watcher(mailbox, engine, max_rows=2, rows=[row(1), row(2)])
    report = await watcher.tick()

    assert engine.processed == [1, 2]
    assert mailbox.seen == ["1"] and report.held == 0


async def test_released_letter_is_processed_on_the_next_tick():
    """Владелец снял отложение — письмо разбирается как обычное."""
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()
    watcher = make_watcher(mailbox, engine, max_rows=1, rows=[row(1), row(2)])
    await watcher.tick()

    engine.store.release("1")
    report = await watcher.tick()

    assert engine.processed == [1, 2]
    assert mailbox.seen == ["1"]
    assert engine.store.remembered[0][0] == "1"
    assert report.held == 0


async def test_rehearsal_holds_the_letter_too():
    """В репетиции письмо тоже откладывается — и тоже остаётся непрочитанным."""
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()

    watcher = make_watcher(mailbox, engine, max_rows=1, dry_run=True,
                           rows=[row(1), row(2)])
    await watcher.tick()

    assert engine.processed == []
    assert mailbox.seen == []
    assert "1" in engine.store.held


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

async def test_report_waits_until_every_row_is_settled():
    """Итог не уходит, пока сейлзбот не завёл сделку (письмо 11.10: «0 из 2»)."""
    mailbox = FakeMailBox([letter(subject="отчёт с 05.10 по 11.10")])
    engine = FakeEngine({
        45199: CarpetLink(partner_id=45199, phone10="1", status="done"),
        45257: CarpetLink(partner_id=45257, phone10="2", status="waiting_salesbot"),
    })
    reports = []

    async def on_report(summary):
        reports.append(summary)

    watcher = make_watcher(mailbox, engine, on_report=on_report,
                           rows=[row(45199), row(45257)])
    await watcher.tick()
    assert reports == []                                   # сейлзбот ещё думает

    engine.next_run[45257] = {"status": "done"}           # сейлзбот завёл, робот довёл
    engine.store.pending = [(row(45257), engine.store.links[45257])]
    await watcher.tick()
    await watcher.tick()                                   # и второй раз не повторяет

    assert reports == [CarpetLetterSummary(uid="1", subject="отчёт с 05.10 по 11.10",
                                           delivered=2)]


async def test_report_does_not_wait_for_owner_answers():
    """Карточка без ответа — не повод держать итог (решение 2), отказ — своей строкой."""
    mailbox = FakeMailBox([letter()])
    engine = FakeEngine({
        44426: CarpetLink(partner_id=44426, phone10="1", status="done"),
        44535: CarpetLink(partner_id=44535, phone10="2", status="waiting_owner"),
        43986: CarpetLink(partner_id=43986, phone10="3", status="done"),
    })
    reports = []

    async def on_report(summary):
        reports.append(summary)

    rows = [row(44426), row(44535), row(43986, is_refusal=True)]
    await make_watcher(mailbox, engine, on_report=on_report, rows=rows).tick()

    assert reports == [CarpetLetterSummary(uid="1", subject="отчёт с 17.08 по 23.08",
                                           delivered=1, refused=1, waiting_owner=1)]


async def test_stuck_row_is_handed_to_owner_after_three_hours():
    clock = Clock()
    mailbox = FakeMailBox([letter()])
    engine = FakeEngine({44426: CarpetLink(partner_id=44426, phone10="9601945325",
                                           status="error", last_error="AmoError: 500")})
    reports, manual = [], []

    async def on_report(summary):
        reports.append(summary)

    async def on_manual(row_, link):
        manual.append((row_.partner_id, link.status))

    watcher = make_watcher(mailbox, engine, on_report=on_report, on_manual=on_manual,
                           now=clock)
    await watcher.tick()
    assert engine.store.links[44426].stuck_since == clock.moment

    engine.store.pending = [(row(), engine.store.links[44426])]
    clock.advance(hours=2, minutes=59)
    await watcher.tick()
    assert manual == [] and reports == []

    clock.advance(minutes=1)
    await watcher.tick()
    await watcher.tick()                                   # повторно не пишет

    assert manual == [(44426, "manual")]
    assert engine.store.links[44426].status == "manual"
    assert engine.store.logged[0][:2] == (44426, "manual_handoff")
    assert reports == [CarpetLetterSummary(uid="1", subject="отчёт с 17.08 по 23.08",
                                           manual=1)]


async def test_passed_step_restarts_the_clock():
    """Любой пройденный шаг — отсчёт заново (решение 5)."""
    clock = Clock()
    engine = FakeEngine({44426: CarpetLink(partner_id=44426, phone10="1", status="error",
                                           checklist={"fill_carpet_lead": "t"})})
    watcher = make_watcher(FakeMailBox([letter()]), engine, now=clock)
    await watcher.tick()

    clock.advance(hours=2)
    engine.next_run[44426] = {"checklist": {"fill_carpet_lead": "t",
                                            "move_carpet_delivered": "t"}}
    engine.store.pending = [(row(), engine.store.links[44426])]
    await watcher.tick()
    assert engine.store.links[44426].stuck_since == clock.moment

    clock.advance(hours=2)
    await watcher.tick()
    assert engine.store.links[44426].status == "error"     # с перезапуска прошло два часа


async def test_choosing_a_deal_counts_as_a_step():
    """Выбор сделки — тоже пройденный шаг: отсчёт заново (решение 5)."""
    clock = Clock()
    engine = FakeEngine({44426: CarpetLink(partner_id=44426, phone10="1", status="error")})
    watcher = make_watcher(FakeMailBox([letter()]), engine, now=clock)
    await watcher.tick()

    clock.advance(hours=2, minutes=50)
    engine.next_run[44426] = {"lead_id": 31723663}       # сделка выбрана, следующий шаг упал
    engine.store.pending = [(row(), engine.store.links[44426])]
    await watcher.tick()
    assert engine.store.links[44426].stuck_since == clock.moment

    clock.advance(minutes=10)
    await watcher.tick()
    assert engine.store.links[44426].status == "error"   # с выбора сделки прошло 10 минут


async def test_recovered_row_forgets_the_clock():
    clock = Clock()
    engine = FakeEngine({44426: CarpetLink(partner_id=44426, phone10="1", status="error")})
    watcher = make_watcher(FakeMailBox([letter()]), engine, now=clock)
    await watcher.tick()

    engine.next_run[44426] = {"status": "done"}           # амо ожила, строка доведена
    engine.store.pending = [(row(), engine.store.links[44426])]
    await watcher.tick()

    assert engine.store.links[44426].stuck_since is None


async def test_crashing_row_is_handed_over_and_the_letter_completes():
    """Исключение не-амо: раньше письмо крутилось молча вечно, теперь — три часа."""
    clock = Clock()
    mailbox = FakeMailBox([letter()])
    engine = FakeEngine()
    engine.store.links[44426] = CarpetLink(partner_id=44426, phone10="1",
                                           status="in_progress")
    engine.raise_on = {44426}
    reports, manual = [], []

    async def on_report(summary):
        reports.append(summary)

    async def on_manual(row_, link):
        manual.append(row_.partner_id)

    watcher = make_watcher(mailbox, engine, on_report=on_report, on_manual=on_manual,
                           now=clock)
    first = await watcher.tick()
    assert first.failures and mailbox.seen == []
    assert engine.store.links[44426].last_error == "RuntimeError: амо не отвечает"

    clock.advance(hours=3)
    await watcher.tick()

    assert manual == [44426]
    assert mailbox.seen == ["1"]                           # письмо больше не держит
    assert reports == [CarpetLetterSummary(uid="1", subject="отчёт с 17.08 по 23.08",
                                           manual=1)]


async def test_row_with_a_fate_is_not_touched_by_a_crash():
    """Сбой прохода по строке с судьбой: ни второго «сделайте руками», ни новой ошибки."""
    clock = Clock()
    engine = FakeEngine()
    engine.store.links[44426] = CarpetLink(partner_id=44426, phone10="1", status="manual",
                                           stuck_since=clock.moment - timedelta(hours=5))
    engine.store.links[44427] = CarpetLink(partner_id=44427, phone10="2", status="done")
    engine.raise_on = {44426, 44427}
    manual = []

    async def on_manual(row_, link):
        manual.append(row_.partner_id)

    watcher = make_watcher(FakeMailBox([letter()]), engine, on_manual=on_manual, now=clock,
                           rows=[row(44426), row(44427)])
    await watcher.tick()

    assert manual == [] and engine.store.logged == []
    assert engine.store.links[44427].stuck_since is None
    assert engine.store.links[44427].last_error is None


async def test_rehearsal_reports_once_and_leaves_the_letter_unread():
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()
    reports = []

    async def on_report(summary):
        reports.append(summary)

    watcher = make_watcher(mailbox, engine, on_report=on_report, dry_run=True)
    await watcher.tick()
    await watcher.tick()

    assert len(reports) == 1
    assert mailbox.seen == [] and engine.store.remembered == []


async def test_concurrent_ticks_send_one_report():
    """/carpets во время фонового прохода не удваивает итог."""
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()
    reports = []

    async def on_report(summary):
        reports.append(summary)

    watcher = make_watcher(mailbox, engine, on_report=on_report)
    await asyncio.gather(watcher.tick(), watcher.tick())

    assert len(reports) == 1


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


async def test_rehearsal_leaves_the_letter_unread():
    """Репетиция не должна «съедать» отчёт: пометка уходит в почту по-настоящему."""
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()

    await make_watcher(mailbox, engine, dry_run=True).tick()

    assert engine.processed == [44426]                 # решения приняты
    assert mailbox.seen == []                          # но письмо осталось в работе
    assert engine.store.remembered == []
