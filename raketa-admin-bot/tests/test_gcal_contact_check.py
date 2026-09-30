"""Сверка «номер + имя» записи с контактом сделки (задача 5, ТЗ 2026-09-22).

Три точки сверки в движке: путь В (`_step_ensure_contact`, контакт найден, а
не создан), путь Б (`_step_check_contact`, контакт лида воронки 1), `_refresh`
(смена номера у записи с известной сделкой). Путь А не сверяется никогда, а
выключатель `contact_check` гасит все три точки разом.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from adminbot.amo import ids
from adminbot.gcal.engine import CalendarEngine, contact_mismatch_text, contact_resolved
from adminbot.gcal.event import EventKind, ParsedEvent
from adminbot.gcal.store import MemoryCalendarStore
from tests.fakes import FakeAmo

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=MSK)
ORDER_DAY = date(2026, 8, 27)


def an_order(**overrides) -> ParsedEvent:
    fields = dict(
        event_id="evt-1", kind=EventKind.ORDER, order_date=ORDER_DAY,
        phones=("9605379757",), client_name="Юлия", services=("mattress",),
        summary="Сов! Матрас, Юлия",
    )
    fields.update(overrides)
    return ParsedEvent(**fields)


def contact_with_phone(contact_id: int, name: str, phone10: str) -> dict:
    return {"id": contact_id, "name": name,
            "custom_fields_values": [
                {"field_code": "PHONE", "values": [{"value": phone10}]}]}


@pytest.fixture
def amo():
    return FakeAmo()


def build(amo, *, contact_check: bool = True, store=None) -> CalendarEngine:
    return CalendarEngine(amo=amo, store=store or MemoryCalendarStore(now=lambda: NOW),
                          dry_run=False, contact_check=contact_check, now=lambda: NOW)


# --- сверка сама по себе (чистая функция) ---

def test_naташа_and_наталья_are_not_a_mismatch():
    contact = contact_with_phone(1, "Наталья", "9601861933")
    assert contact_mismatch_text("Наташа", "9601861933", contact) is None
    assert contact_resolved("Наташа", "9601861933", contact) is True


def test_ирина_and_наталья_is_a_mismatch():
    contact = contact_with_phone(1, "Наталья", "9601861933")
    text = contact_mismatch_text("Ирина", "9601861933", contact)
    assert text is not None
    assert "Ирина" in text and "Наталья" in text
    assert contact_resolved("Ирина", "9601861933", contact) is False


def test_full_name_in_crm_matches_the_first_name_in_the_record():
    """ФИО в CRM, имя в календаре — одно лицо (жалоба владельца 28.09:
    «Ирина» против «Корнилова Ирина Ивановна» при том же номере)."""
    contact = contact_with_phone(1, "Корнилова Ирина Ивановна", "9601860783")
    assert contact_mismatch_text("Ирина", "9601860783", contact) is None
    assert contact_resolved("Ирина", "9601860783", contact) is True


def test_full_name_checks_the_name_not_the_surname():
    """В ФИО имя — второе слово: «Иван» против «Иванов Олег Петрович» — расхождение."""
    contact = contact_with_phone(1, "Иванов Олег Петрович", "9601861933")
    assert contact_mismatch_text("Иван", "9601861933", contact) is not None


def test_full_name_checks_the_name_not_the_patronymic():
    contact = contact_with_phone(1, "Петров Сергей Иванович", "9601861933")
    assert contact_mismatch_text("Иван", "9601861933", contact) is not None


def test_two_words_in_crm_are_checked_both_ways():
    """«Серова Оксана» и «Оксана Серова» — порядок в CRM бывает любым."""
    for name in ("Серова Оксана", "Оксана Серова"):
        contact = contact_with_phone(1, name, "9601861933")
        assert contact_mismatch_text("Оксана", "9601861933", contact) is None, name


def test_full_name_with_another_phone_is_still_a_mismatch():
    """Имя сошлось, номер нет — карточка нужна (карточка «Ксения» 28.09)."""
    contact = contact_with_phone(1, "Буракова Ксения Олеговна", "9601866530")
    assert contact_mismatch_text("Ксения", "9601868094", contact) is not None


def test_phone_not_in_contact_is_a_mismatch():
    contact = contact_with_phone(1, "Наталья", "9004445566")
    text = contact_mismatch_text("Наталья", "9601861933", contact)
    assert text is not None
    assert contact_resolved("Наталья", "9601861933", contact) is False


def test_mismatch_text_shows_both_phones_in_full():
    """Сообщения владельцу — с полным номером (решение 2026-08-26); 30.09 — и в строке
    сравнения: оба номера целиком, чтобы сравнить глазами и позвонить по любому."""
    contact = contact_with_phone(1, "Наталья", "9004445566")
    text = contact_mismatch_text("Наталья", "9601861933", contact)
    assert "+79601861933" in text and "+79004445566" in text


def test_mismatch_card_shows_record_phone_once():
    from types import SimpleNamespace
    from adminbot.tg.calendar_cards import contact_mismatch_card
    contact = contact_with_phone(1, "Наталья", "9004445566")
    link = SimpleNamespace(
        event_data={"summary": "Диван Наталья"}, order_date=date(2026, 9, 30),
        phone10="9601861933", contact_mismatch=contact_mismatch_text("Наталья", "9601861933", contact),
        real_lead_id=None, primary_lead_id=None, event_id="ev1")

    text, _ = contact_mismatch_card(link, reminder_no=1)

    assert text.count("+79601861933") == 1
    assert "+79004445566" in text


def test_no_record_name_is_not_checked():
    contact = contact_with_phone(1, "Ирина", "9004445566")
    assert contact_mismatch_text(None, "9601861933", contact) is None
    assert contact_resolved(None, "9601861933", contact) is False


def test_autogenerated_contact_name_is_not_checked():
    contact = contact_with_phone(1, "Входящий звонок", "9004445566")
    assert contact_mismatch_text("Наталья", "9601861933", contact) is None
    assert contact_resolved("Наталья", "9601861933", contact) is False


# --- путь В: контакт НАШЁЛСЯ (не создан) ---

async def test_path_c_found_contact_mismatch_is_remembered(amo):
    amo.contacts.append(contact_with_phone(555, "Ирина", "9004445566"))
    engine = build(amo)

    link = await engine.process(an_order())

    assert amo.calls_of("create_contact") == []          # контакт не создавали
    assert link.contact_mismatch is not None
    assert "Юлия" in link.contact_mismatch and "Ирина" in link.contact_mismatch
    assert link.status == "waiting_salesbot"              # цепочка пошла дальше


async def test_path_c_found_contact_matching_is_not_a_mismatch(amo):
    amo.contacts.append(contact_with_phone(555, "Юлия", "9605379757"))
    engine = build(amo)

    link = await engine.process(an_order())

    assert link.contact_mismatch is None


async def test_path_c_newly_created_contact_is_not_checked(amo):
    """Свежесозданный контакт по построению совпадает с записью — сверять нечего."""
    engine = build(amo)

    link = await engine.process(an_order())

    assert amo.calls_of("create_contact")
    assert link.contact_mismatch is None


# --- путь Б: контакт лида воронки 1 ---

async def test_path_b_primary_lead_contact_mismatch_is_remembered(amo):
    amo.add_lead(41400002, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_DIALOG)
    amo.leads[41400002]["_embedded"] = {"contacts": [{"id": 555}]}
    amo.contacts.append(contact_with_phone(555, "Ирина", "9004445566"))
    engine = build(amo)

    link = await engine.process(an_order())

    assert link.contact_mismatch is not None
    assert link.status == "waiting_salesbot"               # цепочка пошла дальше


async def test_path_b_primary_lead_contact_matching_is_not_a_mismatch(amo):
    amo.add_lead(41400002, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_DIALOG)
    amo.leads[41400002]["_embedded"] = {"contacts": [{"id": 555}]}
    amo.contacts.append(contact_with_phone(555, "Юлия", "9605379757"))
    engine = build(amo)

    link = await engine.process(an_order())

    assert link.contact_mismatch is None


# --- путь А: не сверяем никогда ---

async def _owner_chose_realization(store, order, lead_id: int) -> None:
    """Путь А: сделку второй воронки выбрал владелец кнопкой.

    Сам матчер вторую воронку с 2026-09-30 не смотрит (решение владельца),
    поэтому в путь А запись попадает только так.
    """
    await store.create(order.event_id, kind=order.kind.value, phone10=order.phone10)
    await store.update(order.event_id, status="new", path="A", real_lead_id=lead_id,
                       event_data=order.to_dict())


async def test_path_a_existing_realization_deal_is_never_checked(amo):
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    amo.leads[41400001]["_embedded"] = {"contacts": [{"id": 555}]}
    amo.contacts.append(contact_with_phone(555, "Ирина", "9004445566"))   # разошлось бы
    store = MemoryCalendarStore(now=lambda: NOW)
    await _owner_chose_realization(store, an_order(), 41400001)
    engine = build(amo, store=store)

    link = await engine.process(an_order())

    assert link.status == "done"
    assert link.contact_mismatch is None


# --- _refresh: смена номера у записи с известной сделкой ---

async def test_refresh_phone_change_with_known_deal_is_checked(amo):
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    amo.leads[41400001]["_embedded"] = {"contacts": [{"id": 555}]}
    amo.contacts.append(contact_with_phone(555, "Юлия", "9605379757"))
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    await engine.process(an_order())                       # заводим сделку как обычно

    changed = await engine.process(an_order(phones=("9009991122",)))  # чужой номер

    assert changed.contact_mismatch is not None
    assert changed.status == "done"                          # цепочка не встала


async def test_refresh_phone_change_matching_the_deal_is_not_a_mismatch(amo):
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    amo.leads[41400001]["_embedded"] = {"contacts": [{"id": 555}]}
    amo.contacts.append(contact_with_phone(555, "Юлия", "9009991122"))
    store = MemoryCalendarStore(now=lambda: NOW)
    await _owner_chose_realization(store, an_order(), 41400001)
    engine = build(amo, store=store)
    await engine.process(an_order())

    changed = await engine.process(an_order(phones=("9009991122",)))

    assert changed.contact_mismatch is None


async def test_refresh_amo_failure_on_contact_check_does_not_drop_the_edit(amo):
    """Ревью 23.09: сбой амо на сверке не должен ронять весь `_refresh`.

    Легитимная правка записи (здесь — новый телефон) обязана примениться,
    даже если сверка контакта не задалась; расхождение при этом не пишем —
    попробуем на следующем обмене.
    """
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    amo.leads[41400001]["_embedded"] = {"contacts": [{"id": 555}]}
    amo.contacts.append(contact_with_phone(555, "Юлия", "9605379757"))
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    await engine.process(an_order())                       # заводим сделку как обычно

    amo.fail_on = "get_contact"
    changed = await engine.process(an_order(phones=("9009991122",)))   # чужой номер

    assert changed.phone10 == "9009991122"                  # телефон всё равно записан
    assert changed.contact_mismatch is None                 # а сверка — нет, амо упала
    assert changed.last_error is None                        # исключение наружу не ушло


# --- выключатель ---

async def test_contact_check_disabled_does_nothing_anywhere(amo):
    amo.contacts.append(contact_with_phone(555, "Ирина", "9004445566"))
    amo.add_lead(41400002, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_DIALOG)
    engine = build(amo, contact_check=False)

    link = await engine.process(an_order())

    assert link.contact_mismatch is None
