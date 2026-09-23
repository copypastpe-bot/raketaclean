"""Напоминание про расхождение «номер + имя»: раз в сутки, не больше `cap` раз.

Источник записей и отправка карточки — двойники: здесь проверяется устройство
самого цикла — счётчик, отметка времени последнего напоминания, потолок с
автоматическим «молчу сам» и перепроверка контакта дочки в amoCRM перед
отправкой (задача 5, ТЗ 2026-09-22). Тот же приём, что у `test_address_reminder.py`.
"""

import asyncio
from datetime import datetime

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.gcal.contact_reminder import ContactReminder
from adminbot.gcal.store import MemoryCalendarStore
from adminbot.models import CalendarLink
from tests.fakes import FakeAmo

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=MOSCOW_TZ)


def make_link(event_id="evt-1", count=0, mismatch="В записи: Наталья, …1933. "
                                                    "В CRM: Ирина, …4455.", **extra):
    fields = dict(event_id=event_id, kind="order", status="in_progress",
                  phone10="9601861933", client_name="Наталья",
                  real_lead_id=41400001, contact_mismatch=mismatch,
                  contact_reminder_count=count)
    fields.update(extra)
    return CalendarLink(**fields)


def make_store(*links):
    store = MemoryCalendarStore(now=lambda: NOW)
    for link in links:
        store.links[link.event_id] = link
    return store


class FakeSource:
    """Записи, которым пора напомнить — отдаёт ровно тот список, что дали."""

    def __init__(self, links):
        self._links = list(links)

    async def due(self):
        return list(self._links)


def make_amo_with_contact(name, phone10, lead_id=41400001):
    """Контакт дочки в CRM — с именем и телефоном, которые сверит цикл."""
    amo = FakeAmo()
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, 1)
    amo.leads[lead_id]["_embedded"] = {"contacts": [{"id": 900001}]}
    amo.contacts.append({
        "id": 900001, "name": name,
        "custom_fields_values": [{
            "field_code": "PHONE", "values": [{"value": phone10}],
        }],
    })
    return amo


async def test_tick_sends_a_reminder_and_bumps_the_counter():
    link = make_link(count=2)
    store = make_store(link)
    amo = make_amo_with_contact("Ирина", "9604554455")   # расхождение остаётся
    sent = []

    async def on_reminder(link, count):
        sent.append((link.event_id, count))

    reminder = ContactReminder(source=FakeSource([link]), store=store, amo=amo,
                               on_reminder=on_reminder, now=lambda: NOW)
    n = await reminder.tick()

    assert n == 1
    assert sent == [("evt-1", 3)]
    updated = store.links["evt-1"]
    assert updated.contact_reminder_count == 3
    assert updated.contact_reminder_sent_at == NOW
    assert updated.contact_reminder_muted is False


async def test_reaching_the_cap_mutes_and_logs_it():
    link = make_link(count=6)
    store = make_store(link)
    amo = make_amo_with_contact("Ирина", "9604554455")

    async def on_reminder(link, count):
        return None

    reminder = ContactReminder(source=FakeSource([link]), store=store, amo=amo,
                               on_reminder=on_reminder, cap=7, now=lambda: NOW)
    await reminder.tick()

    updated = store.links["evt-1"]
    assert updated.contact_reminder_count == 7
    assert updated.contact_reminder_muted is True
    capped = store.actions_of("contact_reminder_capped")
    assert len(capped) == 1 and capped[0]["payload"] == {"count": 7}


async def test_one_broken_reminder_does_not_stop_the_rest():
    bad_link = make_link(event_id="evt-bad")
    ok_link = make_link(event_id="evt-ok")
    store = make_store(bad_link, ok_link)
    amo = make_amo_with_contact("Ирина", "9604554455")
    sent = []

    async def on_reminder(link, count):
        if link.event_id == "evt-bad":
            raise RuntimeError("телеграм недоступен")
        sent.append(link.event_id)

    reminder = ContactReminder(source=FakeSource([bad_link, ok_link]), store=store,
                               amo=amo, on_reminder=on_reminder, now=lambda: NOW)
    n = await reminder.tick()

    assert n == 1
    assert sent == ["evt-ok"]
    assert store.links["evt-bad"].contact_reminder_count == 0
    assert store.links["evt-bad"].contact_reminder_sent_at is None


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

    reminder = ContactReminder(source=FakeSource([]), store=store, amo=FakeAmo(),
                               on_reminder=on_reminder, poll_interval_sec=3600,
                               sleep=fake_sleep)
    await reminder.run_forever(stop)

    assert naps == [3600, 3600]


# --- перепроверка контакта в amoCRM перед отправкой ---

async def test_contact_fixed_in_amo_is_cleared_and_reminder_is_not_sent():
    """Владелец поправил контакт в сделке напрямую, не нажимая «Я разобрался»."""
    link = make_link(count=2)
    store = make_store(link)
    amo = make_amo_with_contact("Наталья", "9601861933")   # теперь сходится
    sent = []

    async def on_reminder(link, count):
        sent.append(link.event_id)

    reminder = ContactReminder(source=FakeSource([link]), store=store, amo=amo,
                               on_reminder=on_reminder, now=lambda: NOW)
    n = await reminder.tick()

    assert n == 0
    assert sent == []
    updated = store.links["evt-1"]
    assert updated.contact_mismatch is None       # расхождение снято
    assert updated.contact_reminder_count == 2     # счётчик не растёт
    assert updated.contact_reminder_sent_at is None


async def test_still_mismatched_sends_reminder_as_before():
    link = make_link(count=1)
    store = make_store(link)
    amo = make_amo_with_contact("Ирина", "9604554455")
    sent = []

    async def on_reminder(link, count):
        sent.append((link.event_id, count))

    reminder = ContactReminder(source=FakeSource([link]), store=store, amo=amo,
                               on_reminder=on_reminder, now=lambda: NOW)
    n = await reminder.tick()

    assert n == 1
    assert sent == [("evt-1", 2)]
    updated = store.links["evt-1"]
    assert updated.contact_mismatch is not None
    assert updated.contact_reminder_count == 2
    assert updated.contact_reminder_sent_at == NOW


async def test_no_deal_contact_still_reminds_with_the_stored_text():
    """Дочки ещё нет — сверить сейчас нечем, это не повод замолчать (не «сошлось»)."""
    link = make_link(count=1, real_lead_id=None, primary_lead_id=None)
    store = make_store(link)
    amo = FakeAmo()
    sent = []

    async def on_reminder(link, count):
        sent.append((link.event_id, count))

    reminder = ContactReminder(source=FakeSource([link]), store=store, amo=amo,
                               on_reminder=on_reminder, now=lambda: NOW)
    n = await reminder.tick()

    assert n == 1
    assert sent == [("evt-1", 2)]
    updated = store.links["evt-1"]
    assert updated.contact_mismatch == link.contact_mismatch   # текст не потерян
    assert updated.contact_reminder_count == 2
    assert updated.contact_reminder_sent_at == NOW
