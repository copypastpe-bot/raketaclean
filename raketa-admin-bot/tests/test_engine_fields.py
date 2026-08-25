"""Поля сделки и правила оплаты — уточнения владельца после первой боевой сделки.

Разбор заказа №585 показал, что робот заполняет не всё: не было специалиста,
варианта и даты оплаты, типа клиента, а имя контакта осталось «Входящий 79…».
Плюс появилось правило про неоплаченный счёт.
"""

from datetime import timedelta
from decimal import Decimal

from adminbot.amo import ids
from adminbot.models import Order
from tests.fakes import FakeAmo, FakeStore
from tests.test_engine import ORDER_MOMENT, make_engine, make_order, open_realization_lead


def fields_of(amo) -> dict[int, dict]:
    """Поля, отправленные в первом заполнении сделки, по id поля."""
    _, payload = amo.calls_of("update_lead")[0]
    return {field["field_id"]: field for field in payload["custom_fields"]}


def wire_order(paid: bool, **extra) -> Order:
    return Order(
        order_id=700, phone10="9601861067", created_at=ORDER_MOMENT,
        amount_total=Decimal("12000"), masters=[("Дмитрий Козлов", "79306858534")],
        client_name="ООО Ромашка", address="ул. Ленина, 5",
        payment_method="р/с", awaiting_wire_payment=not paid, **extra,
    )


# --- новые поля ---

async def test_specialist_is_filled_from_order_master():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)

    await make_engine(amo, store).process_order(make_order())

    specialist = fields_of(amo)[ids.FIELD_SPECIALIST]
    assert specialist["values"] == [{"enum_id": 951507}]      # Дмитрий Козлов


async def test_specialist_is_not_overwritten():
    """В сделке может стоять мастер с этапа планирования — не трогаем."""
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo, custom_fields_values=[
        {"field_id": ids.FIELD_SPECIALIST, "values": [{"value": "Никита", "enum_id": 951505}]}])

    await make_engine(amo, store).process_order(make_order())

    assert ids.FIELD_SPECIALIST not in fields_of(amo)


async def test_payment_type_and_date_are_filled():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)
    order = make_order()
    order = Order(**{**order.__dict__, "payment_method": "Карта Дима"})

    await make_engine(amo, store).process_order(order)

    fields = fields_of(amo)
    assert fields[ids.FIELD_PAYMENT_TYPE]["values"] == [{"enum_id": ids.PAYMENT_ENUM_CARD}]
    assert ids.FIELD_PAYMENT_DATE in fields                  # дата оплаты = дате заказа


async def test_cash_payment_maps_to_cash_enum():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)
    order = Order(**{**make_order().__dict__, "payment_method": "Наличные"})

    await make_engine(amo, store).process_order(order)

    assert fields_of(amo)[ids.FIELD_PAYMENT_TYPE]["values"] == [{"enum_id": ids.PAYMENT_ENUM_CASH}]


async def test_client_type_is_person_by_default():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)

    await make_engine(amo, store).process_order(make_order())

    assert fields_of(amo)[ids.FIELD_CLIENT_TYPE]["values"] == [{"enum_id": ids.CLIENT_TYPE_PERSON}]


async def test_unknown_payment_method_is_not_invented():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)
    order = Order(**{**make_order().__dict__, "payment_method": "Криптой занёс"})

    await make_engine(amo, store).process_order(order)

    assert ids.FIELD_PAYMENT_TYPE not in fields_of(amo)


# --- источник сделки ---

async def test_unsorted_lead_without_source_gets_word_of_mouth():
    """Лид застрял в «Неразобранном» без источника — значит клиент пришёл по сарафану."""
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.STATUS_UNSORTED_PRIMARY,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)

    await make_engine(amo, store).process_order(make_order())

    source = fields_of(amo)[ids.FIELD_SOURCE]
    assert source["values"] == [{"enum_id": ids.SOURCE_ENUM_WORD_OF_MOUTH}]


async def test_existing_source_is_never_overwritten():
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.STATUS_UNSORTED_PRIMARY,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600,
                 custom_fields_values=[{"field_id": ids.FIELD_SOURCE,
                                        "values": [{"value": "Авито", "enum_id": 8047}]}])

    await make_engine(amo, store).process_order(make_order())

    assert ids.FIELD_SOURCE not in fields_of(amo)


async def test_repeat_client_gets_repeat_order_source():
    """У клиента уже были заказы в боте — источник «Повторный заказ», а не сарафан."""
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.STATUS_UNSORTED_PRIMARY,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)
    order = Order(**{**make_order().__dict__, "is_repeat_client": True})

    await make_engine(amo, store).process_order(order)

    assert fields_of(amo)[ids.FIELD_SOURCE]["values"] == [{"enum_id": ids.SOURCE_ENUM_REPEAT}]


async def test_robot_created_deal_gets_a_source_too():
    """Сделку робот заводит сам — следа звонка или заявки нет, значит сарафан."""
    amo, store = FakeAmo(), FakeStore()          # ни сделок, ни контактов

    await make_engine(amo, store).process_order(make_order())

    created = amo.calls_of("create_lead")[0]
    sources = [f for f in created["custom_fields"] if f["field_id"] == ids.FIELD_SOURCE]
    assert sources[0]["values"] == [{"enum_id": ids.SOURCE_ENUM_WORD_OF_MOUTH}]


async def test_source_is_not_touched_outside_unsorted():
    """Лид уже разобран оператором — источник его забота, не робота."""
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)

    await make_engine(amo, store).process_order(make_order())

    assert ids.FIELD_SOURCE not in fields_of(amo)


# --- расчёт по счёту ---

async def test_unpaid_wire_stops_at_order_done_stage():
    """Работа сделана, деньги по счёту не пришли: останавливаемся на «Заказ выполнен».

    Тогда сейлзбот поставит задачу получить оплату, и владелец её отследит.
    """
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)

    link = await make_engine(amo, store).process_order(wire_order(paid=False))

    assert link.status == "done"
    assert amo.leads[lead_id]["status_id"] == ids.REAL_STAGE_DONE      # НЕ финальный этап
    fields = fields_of(amo)
    assert ids.FIELD_PAYMENT_DATE not in fields                        # денег ещё нет
    assert fields[ids.FIELD_CLIENT_TYPE]["values"] == [{"enum_id": ids.CLIENT_TYPE_COMPANY}]
    assert fields[ids.FIELD_PAYMENT_TYPE]["values"] == [{"enum_id": ids.PAYMENT_ENUM_WIRE}]


async def test_paid_wire_goes_all_the_way():
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)

    await make_engine(amo, store).process_order(wire_order(paid=True))

    assert amo.leads[lead_id]["status_id"] == ids.STATUS_SUCCESS
    assert ids.FIELD_PAYMENT_DATE in fields_of(amo)                    # оплата поступила


async def test_note_explains_why_the_deal_is_not_closed():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)

    await make_engine(amo, store).process_order(wire_order(paid=False))

    text = amo.calls_of("add_note")[0][1]
    assert "не поступила" in text


# --- имя контакта ---

async def test_autogenerated_contact_name_is_replaced():
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo, _embedded={"contacts": [{"id": 111}]})
    amo.contacts.append({"id": 111, "name": "Входящий 79605379757 (79302303307 - Сайт"})

    await make_engine(amo, store).process_order(make_order())

    assert amo.calls_of("update_contact") == [(111, "Ирина")]
    assert amo.contacts[0]["name"] == "Ирина"


async def test_human_written_contact_name_is_kept():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo, _embedded={"contacts": [{"id": 111}]})
    amo.contacts.append({"id": 111, "name": "Ирина Петровна Соколова"})

    await make_engine(amo, store).process_order(make_order())

    assert not amo.calls_of("update_contact")          # вписанное вручную не трогаем


async def test_bare_phone_as_name_is_replaced():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo, _embedded={"contacts": [{"id": 111}]})
    amo.contacts.append({"id": 111, "name": "+7 960 537-97-57"})

    await make_engine(amo, store).process_order(make_order())

    assert amo.calls_of("update_contact") == [(111, "Ирина")]


# --- отметка робота ---

async def test_robot_leaves_a_note_in_the_deal():
    """Владелец должен видеть в сделке, что её провёл робот и по каким данным."""
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)

    await make_engine(amo, store).process_order(make_order())

    lead, text = amo.calls_of("add_note")[0]
    assert lead == lead_id
    assert "роботом amo_sync" in text
    assert "№596" in text and "5950" in text and "Дмитрий Козлов" in text


async def test_tasks_are_closed_before_the_stage_changes():
    """Переход в финал порождает задачи сейлзбота — закрываем ДО перехода."""
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)
    amo.add_task(lead_id, 1, 2270740)

    await make_engine(amo, store).process_order(make_order())

    order_of_calls = [name for name, _ in amo.calls]
    assert order_of_calls.index("complete_task") < order_of_calls.index("move_lead")


# --- «Услуга» настраивается, а не зашита в код ---

async def test_service_comes_from_the_configured_master_map():
    """Новый мастер добавляется настройкой: в коде для этого править нечего."""
    from adminbot.sync.engine import Engine, service_enums
    from tests.test_engine import SPECIALISTS

    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)
    engine = Engine(amo=amo, store=store, specialists=SPECIALISTS, dry_run=False,
                    service_by_master=service_enums({"пётр": "cleaning"}))

    await engine.process_order(make_order(master=("Пётр Новиков", "79001234567")))

    service = fields_of(amo)[ids.FIELD_SERVICE]
    assert service["values"][0]["enum_id"] == ids.SERVICE_ENUM_CLEANING


async def test_unknown_master_leaves_service_empty():
    """Мастера нет в настройке — поле не выдумываем."""
    from adminbot.sync.engine import Engine
    from tests.test_engine import SPECIALISTS

    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)

    await Engine(amo=amo, store=store, specialists=SPECIALISTS, dry_run=False,
                 service_by_master={}).process_order(
        make_order(master=("Кто-то Незнакомый", None)))

    assert ids.FIELD_SERVICE not in fields_of(amo)
