"""Цикл доводки сделки после оплаты по счёту (задача 11, ТЗ 2026-09-22).

Источник — двойник, отдаёт готовые пары (заказ, связка); движок настоящий
(`Engine`) поверх `FakeAmo`/`FakeStore`, чтобы «в амо ноль записей» в
репетиции проверялось на деле, а не пересказом.
"""

from datetime import datetime
from decimal import Decimal

import asyncio

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import AmoLink, Order
from adminbot.sync.engine import Engine
from adminbot.sync.specialists import SpecialistIndex
from adminbot.sync.wire_payment import WirePaymentSync
from tests.fakes import FakeAmo, FakeStore

ORDER_MOMENT = datetime(2026, 9, 10, 12, 0, tzinfo=MOSCOW_TZ)
SPECIALISTS = SpecialistIndex.from_enums([])


def make_order(order_id=588, amount="5500"):
    return Order(order_id=order_id, phone10="9601861067", created_at=ORDER_MOMENT,
                amount_total=Decimal(amount), masters=[], client_name="Ирина",
                payment_method="Расчётный", awaiting_wire_payment=False)


def make_link(order_id=588, real_lead_id=41463832_20, **extra):
    return AmoLink(order_id=order_id, phone10="9601861067", status="done", path="A",
                   real_lead_id=real_lead_id, payment_pending=True, **extra)


def make_engine(amo, store, *, dry_run=False):
    return Engine(amo=amo, store=store, specialists=SPECIALISTS, dry_run=dry_run)


class FakeSource:
    """Пары (заказ, связка), готовые к доводке — ровно то, что дали."""

    def __init__(self, due):
        self._due = list(due)

    async def due(self):
        return list(self._due)


async def test_tick_completes_the_deal_and_reports_it():
    amo, store = FakeAmo(), FakeStore()
    lead_id = 41463832_21
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE, price=1)
    link = make_link(real_lead_id=lead_id)
    store.links[588] = link
    order = make_order()
    reported = []

    async def on_synced(order, link, result):
        reported.append((order.order_id, result.stage_moved, result.tasks_closed))

    sync = WirePaymentSync(source=FakeSource([(order, link)]),
                           engine=make_engine(amo, store), on_synced=on_synced)
    n = await sync.tick()

    assert n == 1
    assert amo.leads[lead_id]["status_id"] == ids.STATUS_SUCCESS
    assert reported == [(588, True, 0)]
    assert store.links[588].payment_synced_at is not None


async def test_tick_reports_a_partial_fix_in_the_final_branch():
    """Сделка уже финальная, цена-заглушка и открытая задача — доводим и
    отчитываемся (ревью 23.09): владелец просил «выполнить цепочку и кинуть
    отчёт», а не только про случай полного перевода стадии."""
    amo, store = FakeAmo(), FakeStore()
    lead_id = 41463832_22
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS, price=1)
    amo.add_task(lead_id, 1, 2270740)
    link = make_link(real_lead_id=lead_id)
    store.links[588] = link
    order = make_order()
    reported = []

    async def on_synced(order, link, result):
        reported.append((order.order_id, result.stage_moved, result.tasks_closed))

    sync = WirePaymentSync(source=FakeSource([(order, link)]),
                           engine=make_engine(amo, store), on_synced=on_synced)
    n = await sync.tick()

    assert n == 1
    assert reported == [(588, False, 1)]         # отчитались, но без «стадия переведена»
    assert amo.leads[lead_id]["price"] == 5500
    assert amo.calls_of("complete_task") == [1]


async def test_tick_does_not_report_when_nothing_changed():
    """Финальная стадия, сумма уже верная, задач нет — менять нечего, молчим."""
    amo, store = FakeAmo(), FakeStore()
    lead_id = 41463832_23
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS, price=5500)
    link = make_link(real_lead_id=lead_id)
    store.links[588] = link
    order = make_order()
    reported = []

    async def on_synced(order, link, result):
        reported.append(order.order_id)

    sync = WirePaymentSync(source=FakeSource([(order, link)]),
                           engine=make_engine(amo, store), on_synced=on_synced)
    n = await sync.tick()

    assert n == 1                                # обработали (payment_synced_at стоит)
    assert reported == []                        # но не отчитались
    assert amo.calls_of("update_lead") == []
    assert store.links[588].payment_synced_at is not None


async def test_rehearsal_writes_nothing_to_amo_but_still_reports():
    """Репетиция: свой dry_run, интенты в амо не выполняются, отчёт всё равно уходит."""
    amo, store = FakeAmo(dry_run=True), FakeStore()
    lead_id = 41463832_23
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE, price=1)
    link = make_link(real_lead_id=lead_id)
    store.links[588] = link
    order = make_order()
    reported = []

    async def on_synced(order, link, result):
        reported.append(result.stage_moved)

    sync = WirePaymentSync(source=FakeSource([(order, link)]),
                           engine=make_engine(amo, store, dry_run=True), on_synced=on_synced)
    await sync.tick()

    assert amo.leads[lead_id]["status_id"] == ids.REAL_STAGE_DONE   # в амо не записано
    assert amo.leads[lead_id]["price"] == 1
    assert reported == [True]                    # хук вызывается — пометку ставит cards.py


async def test_one_broken_item_does_not_stop_the_rest():
    amo, store = FakeAmo(), FakeStore()
    good_lead, bad_lead = 41463832_24, 41463832_25
    amo.add_lead(good_lead, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE, price=1)
    amo.add_lead(bad_lead, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE, price=1)
    bad_link = make_link(order_id=589, real_lead_id=bad_lead)
    good_link = make_link(order_id=588, real_lead_id=good_lead)
    store.links[589] = bad_link
    store.links[588] = good_link
    reported = []

    async def on_synced(order, link, result):
        reported.append(order.order_id)

    amo.fail_on = "update_lead"
    amo.fail_lead_id = bad_lead
    sync = WirePaymentSync(
        source=FakeSource([(make_order(order_id=589), bad_link), (make_order(), good_link)]),
        engine=make_engine(amo, store), on_synced=on_synced)
    n = await sync.tick()

    assert n == 1
    assert reported == [588]
    assert store.links[589].payment_synced_at is None    # упавшая — не отмечена, попробуем снова
    assert store.links[588].payment_synced_at is not None


async def test_run_forever_sleeps_between_ticks_and_stops():
    stop = asyncio.Event()
    naps = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            stop.set()

    sync = WirePaymentSync(source=FakeSource([]), engine=make_engine(FakeAmo(), FakeStore()),
                           poll_interval_sec=900, sleep=fake_sleep)
    await sync.run_forever(stop)

    assert naps == [900, 900]


async def test_run_forever_survives_a_broken_tick():
    stop = asyncio.Event()
    ticks = []

    class BrokenSource:
        async def due(self):
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("Postgres недоступен")
            return []

    async def fake_sleep(seconds):
        if len(ticks) >= 2:
            stop.set()

    sync = WirePaymentSync(source=BrokenSource(), engine=make_engine(FakeAmo(), FakeStore()),
                           sleep=fake_sleep)
    await sync.run_forever(stop)

    assert len(ticks) == 2

async def test_rehearsal_handles_each_order_once_per_process():
    """Задача 20 брифа: репетиция не пишет отметку в базу, и источник отдаёт те же
    заказы каждый проход. Владелец получал одинаковые отчёты каждые 15 минут.
    Связки в хранилище движка нет — как в бою: она из базы, а хранилище в памяти."""
    amo, store = FakeAmo(dry_run=True), FakeStore()
    lead_id = 41463832_26
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE, price=1)
    link = make_link(real_lead_id=lead_id)
    order = make_order()
    reported = []

    async def on_synced(order, link, result):
        reported.append(order.order_id)

    sync = WirePaymentSync(source=FakeSource([(order, link)]),
                           engine=make_engine(amo, store, dry_run=True),
                           on_synced=on_synced, dry_run=True)
    first = await sync.tick()
    second = await sync.tick()

    assert (first, second) == (1, 0)
    assert reported == [588]                     # второй раз молчим до перезапуска

async def test_live_mode_does_not_keep_handled_orders_in_memory():
    """В бою отметка в базе, и повторный заказ от источника — повод доводить снова."""
    amo, store = FakeAmo(), FakeStore()
    lead_id = 41463832_27
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE, price=1)
    link = make_link(real_lead_id=lead_id)
    order = make_order()

    sync = WirePaymentSync(source=FakeSource([(order, link)]),
                           engine=make_engine(amo, store), dry_run=False)
    first = await sync.tick()
    second = await sync.tick()

    assert (first, second) == (1, 1)
