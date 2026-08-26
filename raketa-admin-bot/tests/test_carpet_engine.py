"""Проведение ковровой сделки по строке отчёта партнёра.

Ни живой amoCRM, ни базы: клиент и хранилище подменены двойниками. Проверяем
то, что владелец увидит в карточке сделки, и то, чего в ней быть не должно —
перезаписанных руками полей и выдуманных данных.
"""

from datetime import date, datetime
from decimal import Decimal

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.carpets.engine import CarpetEngine
from adminbot.carpets.report import CarpetRow
from adminbot.carpets.store import MemoryCarpetStore
from tests.fakes import FakeAmo

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=MOSCOW_TZ)


def row(partner_id=44426, **overrides) -> CarpetRow:
    values = dict(
        partner_id=partner_id, phone10="9601945325", client_name="Толстая Светлана",
        address="Ивлеева 18-101 п6 э1", district="Советский",
        amount=Decimal("3995"), price=Decimal("3995"), payment_method="Наличные",
        pickup_date=date(2026, 8, 16), return_date=date(2026, 8, 23),
        added_date=date(2026, 8, 12), status="Ковры сданы клиенту",
    )
    values.update(overrides)
    return CarpetRow(**values)


def refusal_row(**overrides) -> CarpetRow:
    return row(partner_id=43986, amount=Decimal(0), price=Decimal(0),
               payment_method=None, pickup_date=None, return_date=None,
               status="Забор отказ", refusal_reason="Не взяли трубку",
               is_refusal=True, **overrides)


def open_carpet_lead(amo: FakeAmo, lead_id=31516051, **extra) -> int:
    amo.add_lead(lead_id, ids.PIPELINE_CARPETS, ids.CARPET_STAGE_HANDED_OVER,
                 created_at=int(datetime(2026, 8, 12, tzinfo=MOSCOW_TZ).timestamp()), **extra)
    return lead_id


def make_engine(amo, store, *, dry_run=False) -> CarpetEngine:
    return CarpetEngine(amo=amo, store=store, dry_run=dry_run, now=lambda: NOW)


def fields_of(amo: FakeAmo) -> dict[int, dict]:
    """Поля из первого заполнения сделки, по id поля."""
    _, payload = amo.calls_of("update_lead")[0]
    return {field["field_id"]: field for field in payload["custom_fields"]}


# --- выполненный заказ ---

async def test_completed_order_is_filled_and_delivered():
    amo, store = FakeAmo(), MemoryCarpetStore()
    lead_id = open_carpet_lead(amo)

    link = await make_engine(amo, store).process_row(row())

    assert link.status == "done" and link.lead_id == lead_id

    _, payload = amo.calls_of("update_lead")[0]
    assert payload["price"] == Decimal("3995")          # «Взято денег у клиента»

    fields = fields_of(amo)
    assert fields[ids.FIELD_CARPET_PICKUP]["values"][0]["value"]   # дата забора проставлена
    assert fields[ids.FIELD_CARPET_RETURN]["values"][0]["value"]   # дата возврата тоже
    assert fields[ids.FIELD_DISTRICT]["values"][0]["enum_id"] == 946953   # Советский
    assert fields[ids.FIELD_PAYMENT_TYPE]["values"][0]["enum_id"] == ids.PAYMENT_ENUM_CASH

    assert amo.calls_of("move_lead") == [(lead_id, ids.PIPELINE_CARPETS,
                                          ids.CARPET_STAGE_DELIVERED)]


async def test_robot_leaves_a_note_about_itself():
    amo, store = FakeAmo(), MemoryCarpetStore()
    open_carpet_lead(amo)

    await make_engine(amo, store).process_row(row())

    _, text = amo.calls_of("add_note")[0]
    assert "44426" in text                              # номер заказа партнёра
    assert "3995" in text
    assert "робот" in text.lower()


async def test_card_payment_maps_to_transfer():
    amo, store = FakeAmo(), MemoryCarpetStore()
    open_carpet_lead(amo)

    await make_engine(amo, store).process_row(row(payment_method="Карта"))

    assert fields_of(amo)[ids.FIELD_PAYMENT_TYPE]["values"][0]["enum_id"] == ids.PAYMENT_ENUM_CARD


async def test_unknown_district_is_left_empty():
    """«Опалиха» в списке амо нет — поле не выдумываем."""
    amo, store = FakeAmo(), MemoryCarpetStore()
    open_carpet_lead(amo)

    await make_engine(amo, store).process_row(row(district="Опалиха"))

    assert ids.FIELD_DISTRICT not in fields_of(amo)


async def test_filled_fields_are_not_overwritten():
    """Адрес, услугу и специалиста владелец мог поправить руками."""
    amo, store = FakeAmo(), MemoryCarpetStore()
    open_carpet_lead(amo, custom_fields_values=[
        {"field_id": ids.FIELD_ADDRESS, "values": [{"value": "свой адрес"}]},
        {"field_id": ids.FIELD_SERVICE, "values": [{"enum_id": 128971}]},
        {"field_id": ids.FIELD_SPECIALIST, "values": [{"enum_id": 951505}]},
    ])

    await make_engine(amo, store).process_row(row())

    sent = fields_of(amo)
    assert ids.FIELD_ADDRESS not in sent
    assert ids.FIELD_SERVICE not in sent
    assert ids.FIELD_SPECIALIST not in sent


async def test_empty_fields_are_filled_with_carpet_defaults():
    amo, store = FakeAmo(), MemoryCarpetStore()
    open_carpet_lead(amo)

    await make_engine(amo, store).process_row(row())

    sent = fields_of(amo)
    assert sent[ids.FIELD_SERVICE]["values"][0]["enum_id"] == ids.SERVICE_ENUM_CARPETS
    assert sent[ids.FIELD_SPECIALIST]["values"][0]["enum_id"] == ids.SPECIALIST_ENUM_CARPETS
    assert sent[ids.FIELD_ADDRESS]["values"][0]["value"] == "Ивлеева 18-101 п6 э1"


# --- отказ ---

async def test_refusal_closes_the_deal_with_a_reason():
    amo, store = FakeAmo(), MemoryCarpetStore()
    lead_id = open_carpet_lead(amo)

    link = await make_engine(amo, store).process_row(refusal_row())

    assert link.status == "done"
    assert amo.calls_of("move_lead") == [(lead_id, ids.PIPELINE_CARPETS,
                                          ids.CARPET_STAGE_REFUSED)]
    _, text = amo.calls_of("add_note")[0]
    assert "Не взяли трубку" in text
    assert amo.calls_of("update_lead") == []            # бюджет и поля не трогаем


async def test_refusal_without_a_deal_is_just_recorded():
    """Сделки нет, забирать было нечего — новую заводить незачем."""
    amo, store = FakeAmo(), MemoryCarpetStore()

    link = await make_engine(amo, store).process_row(refusal_row())

    assert link.status == "done"
    assert amo.calls_of("move_lead") == []


# --- уже проведено владельцем ---

async def test_already_delivered_deal_is_only_linked():
    amo, store = FakeAmo(), MemoryCarpetStore()
    amo.add_lead(31516051, ids.PIPELINE_CARPETS, ids.CARPET_STAGE_DELIVERED,
                 created_at=int(datetime(2026, 8, 12, tzinfo=MOSCOW_TZ).timestamp()),
                 custom_fields_values=[{"field_id": ids.FIELD_ORDER_DATETIME,
                                        "values": [{"value": int(datetime(2026, 8, 23, tzinfo=MOSCOW_TZ).timestamp())}]}])

    link = await make_engine(amo, store).process_row(row())

    assert link.status == "done" and link.lead_id == 31516051
    assert amo.calls_of("update_lead") == [] and amo.calls_of("move_lead") == []


# --- повторная обработка ---

async def test_second_run_of_the_same_row_does_nothing():
    """Месячный свод содержит те же заказы: второй раз в CRM лезть нельзя."""
    amo, store = FakeAmo(), MemoryCarpetStore()
    open_carpet_lead(amo)
    engine = make_engine(amo, store)

    await engine.process_row(row())
    calls_after_first = len(amo.calls)
    await engine.process_row(row())

    assert len(amo.calls) == calls_after_first


# --- вопросы владельцу ---

async def test_two_open_deals_wait_for_the_owner():
    amo, store = FakeAmo(), MemoryCarpetStore()
    open_carpet_lead(amo, 31516051)
    open_carpet_lead(amo, 31516052)

    link = await make_engine(amo, store).process_row(row())

    assert link.status == "waiting_owner"
    # В вопросе лежат сами варианты: владельцу на кнопке нужны дата и сумма сделки.
    assert [option["lead_id"] for option in link.question["options"]] == [31516051, 31516052]
    assert amo.calls_of("move_lead") == []


async def test_row_without_phone_waits_for_the_owner():
    amo, store = FakeAmo(), MemoryCarpetStore()

    link = await make_engine(amo, store).process_row(row(phone10=None))

    assert link.status == "waiting_owner"
    assert not amo.calls                                # в амо даже не ходили


# --- репетиция ---

async def test_rehearsal_changes_nothing_in_crm():
    amo, store = FakeAmo(dry_run=True), MemoryCarpetStore()
    lead_id = open_carpet_lead(amo)

    await make_engine(amo, store, dry_run=True).process_row(row())

    assert amo.leads[lead_id]["status_id"] == ids.CARPET_STAGE_HANDED_OVER
    assert store.actions                                # но план записан в журнал
