"""Напоминание про сделку без адреса: раз в сутки, не больше `cap` раз.

Источник связок и отправка карточки — двойники: здесь проверяется устройство
самого цикла — счётчик, отметка времени последнего напоминания, потолок
с автоматическим «молчу сам» (ТЗ 2026-09-16, задача 7) и перепроверка сделки
в amoCRM перед отправкой (та же ТЗ, задача 10).
"""

import asyncio
from datetime import datetime

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import AmoLink
from adminbot.sync.address_reminder import AddressReminder
from tests.fakes import FakeAmo, FakeStore

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=MOSCOW_TZ)


def make_link(order_id=596, count=0, **extra):
    return AmoLink(order_id=order_id, phone10="9601861067", status="done", path="C",
                   real_lead_id=41400001, address_reminder_count=count, **extra)


def make_store(*links):
    store = FakeStore()
    for link in links:
        store.links[link.order_id] = link
    return store


class FakeSource:
    """Связки, которым пора напомнить — отдаёт ровно тот список, что дали."""

    def __init__(self, links):
        self._links = list(links)

    async def due(self):
        return list(self._links)


async def test_tick_sends_a_reminder_and_bumps_the_counter():
    link = make_link(count=2)
    store = make_store(link)
    sent = []

    async def on_reminder(link, count):
        sent.append((link.order_id, count))

    reminder = AddressReminder(source=FakeSource([link]), store=store, amo=FakeAmo(),
                               on_reminder=on_reminder, now=lambda: NOW)
    n = await reminder.tick()

    assert n == 1
    assert sent == [(596, 3)]                      # было напомнено 2 раза, это — третий
    updated = store.links[596]
    assert updated.address_reminder_count == 3
    assert updated.address_reminder_sent_at == NOW
    assert updated.address_reminder_muted is False


async def test_reaching_the_cap_mutes_and_logs_it():
    link = make_link(count=6)
    store = make_store(link)

    async def on_reminder(link, count):
        return None

    reminder = AddressReminder(source=FakeSource([link]), store=store, amo=FakeAmo(),
                               on_reminder=on_reminder, cap=7, now=lambda: NOW)
    await reminder.tick()

    updated = store.links[596]
    assert updated.address_reminder_count == 7
    assert updated.address_reminder_muted is True    # потолок — молчим сами дальше
    capped = store.actions_of("address_reminder_capped")
    assert len(capped) == 1 and capped[0]["payload"] == {"count": 7}


async def test_one_broken_reminder_does_not_stop_the_rest():
    """Сбой на одной связке (например, Telegram упал) не должен ронять весь проход."""
    bad_link = make_link(order_id=596)
    ok_link = make_link(order_id=597)
    store = make_store(bad_link, ok_link)
    sent = []

    async def on_reminder(link, count):
        if link.order_id == 596:
            raise RuntimeError("телеграм недоступен")
        sent.append(link.order_id)

    reminder = AddressReminder(source=FakeSource([bad_link, ok_link]), store=store,
                               amo=FakeAmo(), on_reminder=on_reminder, now=lambda: NOW)
    n = await reminder.tick()

    assert n == 1
    assert sent == [597]
    # упавшей связке отметка не поставлена — на следующем проходе она снова «due»
    assert store.links[596].address_reminder_count == 0
    assert store.links[596].address_reminder_sent_at is None


async def test_run_forever_sleeps_between_ticks_and_stops():
    store = make_store()
    stop = asyncio.Event()
    naps = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            stop.set()

    async def on_reminder(link, count):
        return None

    reminder = AddressReminder(source=FakeSource([]), store=store, amo=FakeAmo(),
                               on_reminder=on_reminder, poll_interval_sec=3600,
                               sleep=fake_sleep)
    await reminder.run_forever(stop)

    assert naps == [3600, 3600]


async def test_run_forever_survives_a_broken_tick():
    """Упавший тик (например, база недоступна) не убивает цикл."""
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

    async def on_reminder(link, count):
        return None

    reminder = AddressReminder(source=BrokenSource(), store=make_store(), amo=FakeAmo(),
                               on_reminder=on_reminder, sleep=fake_sleep)
    await reminder.run_forever(stop)

    assert len(ticks) == 2


# --- перепроверка сделки в amoCRM перед отправкой (ТЗ 2026-09-16, задача 10) ---

def make_amo_with_address(address, lead_id=41400001):
    amo = FakeAmo()
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, 1,
                 custom_fields_values=[
                     {"field_id": ids.FIELD_ADDRESS, "values": [{"value": address}]}])
    return amo


async def test_address_found_in_amo_is_saved_and_reminder_is_not_sent():
    """Владелец вписал адрес прямо в сделку, не нажимая «Я заполнил»."""
    link = make_link(count=2)
    store = make_store(link)
    amo = make_amo_with_address("ул. Мира, 10")
    sent = []

    async def on_reminder(link, count):
        sent.append(link.order_id)

    reminder = AddressReminder(source=FakeSource([link]), store=store, amo=amo,
                               on_reminder=on_reminder, now=lambda: NOW)
    n = await reminder.tick()

    assert n == 0
    assert sent == []                                   # напоминание не ушло
    updated = store.links[596]
    assert updated.deal_address == "ул. Мира, 10"        # адрес забрали в связку
    assert updated.address_reminder_count == 2           # счётчик не растёт
    assert updated.address_reminder_sent_at is None       # отметка не ставилась


async def test_address_still_empty_in_amo_sends_reminder_as_before():
    link = make_link(count=1)
    store = make_store(link)
    amo = FakeAmo()
    amo.add_lead(link.real_lead_id, ids.PIPELINE_REALIZATION, 1)   # сделка есть, адреса нет
    sent = []

    async def on_reminder(link, count):
        sent.append((link.order_id, count))

    reminder = AddressReminder(source=FakeSource([link]), store=store, amo=amo,
                               on_reminder=on_reminder, now=lambda: NOW)
    n = await reminder.tick()

    assert n == 1
    assert sent == [(596, 2)]
    updated = store.links[596]
    assert updated.deal_address is None
    assert updated.address_reminder_count == 2
    assert updated.address_reminder_sent_at == NOW


async def test_amo_not_responding_skips_the_tick_without_spending_an_attempt():
    """Молчание CRM не должно тратить попытки — пробуем на следующем проходе."""
    link = make_link(count=1)
    store = make_store(link)
    amo = FakeAmo()
    amo.fail_on = "get_lead"
    sent = []

    async def on_reminder(link, count):
        sent.append(link.order_id)

    reminder = AddressReminder(source=FakeSource([link]), store=store, amo=amo,
                               on_reminder=on_reminder, now=lambda: NOW)
    n = await reminder.tick()

    assert n == 0
    assert sent == []
    updated = store.links[596]
    assert updated.deal_address is None
    assert updated.address_reminder_count == 1           # не увеличился
    assert updated.address_reminder_sent_at is None
