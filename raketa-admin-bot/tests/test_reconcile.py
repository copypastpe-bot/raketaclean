"""Вечерняя сверка: в 21:00 МСК робот пересчитывает день и отчитывается.

Смысл сверки — поймать то, что цикл наблюдателя мог пропустить: заказ, до
которого робот не добрался, зависшую сделку, ошибку. Здесь проверяем расчёт
времени запуска, состав сводки и порядок действий (сначала догоняющий проход,
потом отчёт владельцу).
"""

import asyncio
from datetime import datetime, timedelta

from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import AmoLink
from adminbot.sync.reconcile import (
    DailySummary, OrderBrief, Reconciler, Snapshot, build_summary, next_run_at,
)

NOW = datetime(2026, 8, 25, 21, 0, tzinfo=MOSCOW_TZ)


def link(order_id, status, *, path=None, real=None, primary=None, minutes_ago=5, error=None,
        deal_address=None):
    return AmoLink(order_id=order_id, phone10="9601861067", status=status, path=path,
                   real_lead_id=real, primary_lead_id=primary, last_error=error,
                   deal_address=deal_address,
                   created_at=NOW - timedelta(minutes=minutes_ago),
                   updated_at=NOW - timedelta(minutes=minutes_ago))


# --- когда просыпаться ---

def test_next_run_is_today_when_evening_has_not_come():
    now = datetime(2026, 8, 25, 15, 30, tzinfo=MOSCOW_TZ)
    assert next_run_at(now, 21) == datetime(2026, 8, 25, 21, 0, tzinfo=MOSCOW_TZ)


def test_next_run_moves_to_tomorrow_after_the_hour():
    now = datetime(2026, 8, 25, 21, 0, 1, tzinfo=MOSCOW_TZ)
    assert next_run_at(now, 21) == datetime(2026, 8, 26, 21, 0, tzinfo=MOSCOW_TZ)


def test_next_run_counts_moscow_time_not_server_time():
    """Сервер живёт в UTC, а владелец — в Москве. Считаем по Москве."""
    from datetime import timezone

    now_utc = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)      # 18:00 МСК
    assert next_run_at(now_utc, 21) == datetime(2026, 8, 25, 21, 0, tzinfo=MOSCOW_TZ)


# --- что попадает в сводку ---

def test_summary_splits_orders_by_outcome():
    snapshot = Snapshot(
        links=(
            link(596, "done", path="A", real=41400001),      # довели существующую сделку
            link(597, "done", path="B", real=41400002),      # через лид первичной воронки
            link(598, "done", path="C", real=41400003),      # создали с нуля
            link(599, "done", path="done", real=41400004),   # владелец провёл сам, робот привязал
        ),
        orders=(OrderBrief(596, '9601945325', NOW), OrderBrief(597, '9601945325', NOW), OrderBrief(598, '9601945325', NOW), OrderBrief(599, '9601945325', NOW)),
    )

    summary = build_summary(snapshot, now=NOW)

    assert [row.order_id for row in summary.processed] == [596, 597]
    assert [row.order_id for row in summary.created] == [598]
    assert [row.order_id for row in summary.already_done] == [599]
    assert summary.processed[0].lead_id == 41400001
    assert summary.is_quiet is True                          # разбираться владельцу не с чем


def test_summary_splits_failed_and_stale_and_collects_missed():
    snapshot = Snapshot(
        links=(
            link(600, "waiting_owner", path=None),
            link(601, "error", path="A", real=41400005, error="AmoError: 502"),
            link(602, "waiting_salesbot", path="B", primary=41400006, minutes_ago=90),
            link(603, "waiting_salesbot", path="B", primary=41400007, minutes_ago=5),
            link(604, "in_progress", path="A", real=41400008, minutes_ago=120),
        ),
        orders=(OrderBrief(600, '9601945325', NOW), OrderBrief(601, '9601945325', NOW), OrderBrief(602, '9601945325', NOW), OrderBrief(603, '9601945325', NOW), OrderBrief(604, '9601945325', NOW), OrderBrief(605, '9601945325', NOW)),            # 605 привязки не имеет вовсе
    )

    summary = build_summary(snapshot, now=NOW, stale_after_sec=3600)

    assert [row.order_id for row in summary.waiting_owner] == [600]
    # сбой робота — отдельно от зависших дольше часа
    assert [row.order_id for row in summary.failed] == [601]
    assert summary.failed[0].detail == "AmoError: 502"
    assert [row.order_id for row in summary.stale] == [602, 604]
    assert [row.order_id for row in summary.missed] == [605]
    assert summary.missed[0].phone10        # телефон для владельца на месте
    assert summary.in_flight == (603,)                       # свежее ожидание — это норма
    assert summary.is_quiet is False


def test_summary_of_an_empty_day():
    summary = build_summary(Snapshot(links=(), orders=()), now=NOW)

    assert summary.total_orders == 0
    assert summary.is_quiet is True
    assert isinstance(summary, DailySummary)


# --- четыре числа (задача 2, ТЗ 2026-09-21-evening-summary-rework.md) ---
#
# «Провёл из бота» и «Ждут адрес» считаются по-разному: первое — только то, что
# журнал действий подтверждает за окно суток (`Snapshot.touched_order_ids`),
# второе — весь накопленный хвост без окна, прямо по полям связки.

def test_order_processed_today_counts_toward_processed_today():
    snapshot = Snapshot(
        links=(link(596, "done", path="A", real=41400001),),
        orders=(OrderBrief(596, '9601945325', NOW),),
        touched_order_ids=frozenset({596}),
    )

    summary = build_summary(snapshot, now=NOW)

    assert summary.processed_today == 1


def test_order_finished_long_ago_and_untouched_today_counts_nowhere():
    old = link(597, "done", path="A", real=41400002, minutes_ago=60 * 24 * 30)

    summary = build_summary(Snapshot(links=(old,), orders=(OrderBrief(597, '9601945325', NOW),)),
                            now=NOW)

    assert summary.processed_today == 0
    assert summary.waiting_address == 0


def test_scratch_lead_processed_today_counts_too():
    """Пути A, B и C не различаются (решение владельца) — считаем и путь C."""
    scratch_lead = link(598, "done", path="C", real=41400003, deal_address="ул. Ленина, 5")
    snapshot = Snapshot(
        links=(scratch_lead,),
        orders=(OrderBrief(598, '9601945325', NOW),),
        touched_order_ids=frozenset({598}),
    )

    summary = build_summary(snapshot, now=NOW)

    assert summary.processed_today == 1


def test_order_finished_long_ago_touched_today_only_by_address_reminder():
    """Ловушка ТЗ: тронут сегодня только напоминанием про адрес — не «провёл».

    Седьмое (замолкающее) напоминание пишет строку в журнал, но
    `db.fetch_touched_order_ids` её отфильтровывает у самого источника —
    поэтому здесь заказ уже не значится тронутым (пустое множество имитирует
    реальный результат такого запроса), хотя связка «done»/путь C дожила
    до сих пор и адреса так и не получила.
    """
    scratch_lead = link(598, "done", path="C", real=41400003,
                        minutes_ago=60 * 24 * 30, deal_address=None)
    snapshot = Snapshot(
        links=(scratch_lead,),
        orders=(OrderBrief(598, '9601945325', NOW),),
        touched_order_ids=frozenset(),
    )

    summary = build_summary(snapshot, now=NOW)

    assert summary.processed_today == 0          # тронуто не было — только помолчали
    assert summary.waiting_address == 1           # а адреса всё ещё нет


def test_order_linked_long_ago_and_finished_today_counts_as_processed():
    old_link = link(599, "done", path="B", primary=41400004, minutes_ago=60 * 24 * 30)
    snapshot = Snapshot(
        links=(old_link,), orders=(OrderBrief(599, '9601945325', NOW),),
        touched_order_ids=frozenset({599}),
    )

    summary = build_summary(snapshot, now=NOW)

    assert summary.processed_today == 1


def test_waiting_address_does_not_depend_on_the_day_window():
    three_weeks_old = link(600, "done", path="C", real=41400005,
                           minutes_ago=60 * 24 * 21, deal_address=None)
    snapshot = Snapshot(links=(three_weeks_old,), orders=(OrderBrief(600, '9601945325', NOW),),
                        touched_order_ids=frozenset())          # ничего не тронуто сегодня

    summary = build_summary(snapshot, now=NOW)

    assert summary.waiting_address == 1


def test_handled_count_passes_through_to_handed_to_owner():
    """Задача 8: «Передано администратору» считается по журналу вне build_summary
    (там нет чем отличить «Сам разберусь» от автоматического already_done — оба
    пишут связку одинаково), поэтому число приходит уже готовым в срезе."""
    snapshot = Snapshot(links=(), orders=(), handled_count=3)

    summary = build_summary(snapshot, now=NOW)

    assert summary.handed_to_owner == 3


def test_handled_count_defaults_to_zero_on_a_quiet_day():
    summary = build_summary(Snapshot(links=(), orders=()), now=NOW)

    assert summary.handed_to_owner == 0


def test_waiting_address_ignores_filled_addresses_and_other_paths():
    filled = link(601, "done", path="C", real=41400006, deal_address="ул. Ленина, 5")
    owner_kept = link(602, "done", path="done", real=41400007, deal_address=None)
    in_progress = link(603, "in_progress", path="C", real=41400008, deal_address=None)
    snapshot = Snapshot(
        links=(filled, owner_kept, in_progress),
        orders=(OrderBrief(601, '9601945325', NOW), OrderBrief(602, '9601945325', NOW),
               OrderBrief(603, '9601945325', NOW)),
    )

    summary = build_summary(snapshot, now=NOW)

    assert summary.waiting_address == 0


# --- порядок действий вечером ---

class FakeWatcher:
    def __init__(self):
        self.ticks = 0

    async def tick(self):
        self.ticks += 1


class FakeSource:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def collect(self):
        return self.snapshot


async def test_run_once_scans_first_then_reports():
    """Сначала догоняющий проход, потом отчёт: иначе сводка врёт о пропусках."""
    order = 596
    watcher = FakeWatcher()
    source = FakeSource(Snapshot(links=(link(order, "done", path="A", real=1),),
                                 orders=(OrderBrief(order, '9601945325', NOW),)))
    sent = []

    async def on_summary(summary, **_calendar_numbers):
        sent.append(summary)

    reconciler = Reconciler(watcher=watcher, source=source, on_summary=on_summary,
                            now=lambda: NOW)
    summary = await reconciler.run_once()

    assert watcher.ticks == 1
    assert sent == [summary]
    assert [row.order_id for row in summary.processed] == [order]


async def test_run_forever_waits_until_the_appointed_hour():
    watcher = FakeWatcher()
    source = FakeSource(Snapshot(links=(), orders=()))
    stop = asyncio.Event()
    naps = []
    moment = datetime(2026, 8, 25, 18, 0, tzinfo=MOSCOW_TZ)          # за три часа до сверки

    async def fake_sleep(seconds):
        naps.append(seconds)
        stop.set()

    async def on_summary(summary, **_calendar_numbers):
        pass

    reconciler = Reconciler(watcher=watcher, source=source, on_summary=on_summary,
                            hour_msk=21, now=lambda: moment, sleep=fake_sleep)
    await reconciler.run_forever(stop)

    assert naps == [3 * 3600]
    assert watcher.ticks == 1                                # проснулись и сделали сверку


async def test_evening_check_covers_the_calendar():
    """В 21:00 числа календаря приходят внутри той же сводки — одной картиной
    дня, без второго сообщения «📅 Календарь» (задача 6, ТЗ
    2026-09-21-evening-summary-rework.md)."""
    ticks = {"count": 0}

    async def calendar_counts():
        ticks["count"] += 1
        return 3, 1

    sent: list = []

    async def on_summary(summary, **numbers):
        sent.append((summary, numbers))

    reconciler = Reconciler(
        watcher=_SilentWatcher(), source=_EmptySource(),
        on_summary=on_summary, calendar_counts=calendar_counts,
    )

    await reconciler.run_once()

    assert ticks["count"] == 1
    assert len(sent) == 1                       # одно сообщение, не два
    _, numbers = sent[0]
    assert numbers == {"calendar_created": 3, "calendar_handled": 1,
                       "calendar_failed": False}


async def test_calendar_failure_does_not_eat_the_summary():
    """Сбой календарного прохода не должен лишить владельца сводки по заказам:
    числа календаря уходят нулями, а сводка всё равно уходит одним сообщением.
    `calendar_failed=True` (задача 9, ТЗ 2026-09-21-evening-summary-rework.md) —
    признак сбоя доезжает до сводки, а не тонет в `except`."""
    async def broken_calendar_counts():
        raise RuntimeError("Google недоступен")

    sent: list = []

    async def on_summary(summary, **numbers):
        sent.append((summary, numbers))

    reconciler = Reconciler(
        watcher=_SilentWatcher(), source=_EmptySource(),
        on_summary=on_summary, calendar_counts=broken_calendar_counts,
    )

    await reconciler.run_once()                 # не падает

    assert len(sent) == 1                       # сводка по заказам всё равно ушла
    _, numbers = sent[0]
    assert numbers == {"calendar_created": 0, "calendar_handled": 0,
                       "calendar_failed": True}


async def test_calendar_disabled_is_not_a_failure():
    """Календарь выключен вовсе (`calendar_counts=None`, значение по умолчанию)
    — это не сбой прохода, числа нулевые и `calendar_failed` остаётся False
    (задача 9: три разных случая, ноль — не всегда сбой)."""
    sent: list = []

    async def on_summary(summary, **numbers):
        sent.append((summary, numbers))

    reconciler = Reconciler(watcher=_SilentWatcher(), source=_EmptySource(),
                            on_summary=on_summary)

    await reconciler.run_once()

    _, numbers = sent[0]
    assert numbers == {"calendar_created": 0, "calendar_handled": 0,
                       "calendar_failed": False}


class _SilentWatcher:
    async def tick(self):
        return None


class _EmptySource:
    async def collect(self):
        from adminbot.sync.reconcile import Snapshot

        return Snapshot(orders=(), links=())


def _collect(box: list):
    async def send(item, **_kwargs) -> None:
        box.append(item)

    return send
