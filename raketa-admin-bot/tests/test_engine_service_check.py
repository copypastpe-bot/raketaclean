"""Движок и сверка «Услуги» (п.8, решения владельца 2026-09-30).

Выключатель `service_check` (AMO_SERVICE_CHECK) по умолчанию выключен: движок
ведёт себя как раньше. Включён — группа заказа уходит в матчер.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import Order
from adminbot.sync.engine import Engine
from adminbot.sync.specialists import SpecialistIndex
from adminbot.tg.cards import question_card
from tests.fakes import FakeAmo, FakeStore

KOZLOV_ENUM, OLGA_ENUM = 951507, 952251
SPECIALISTS = SpecialistIndex.from_enums([
    {"id": KOZLOV_ENUM, "value": "Дмитрий Козлов +79306858534"},
    {"id": OLGA_ENUM, "value": "Ольга Скоропашкина 89081572721"},
])
ORDER_MOMENT = datetime(2026, 9, 29, 17, 0, tzinfo=MOSCOW_TZ)
SOFA_LEAD = 31685437


def cleaning_order():
    """Уборка №9 Дарьи: вид работы задан источником, «Специалист» — Ольга."""
    return Order(order_id=9, phone10="9000000009", created_at=ORDER_MOMENT,
                 amount_total=Decimal("12600"), masters=[("Бригадир", "79000000001")],
                 client_name="Дарья", address="ул. Ленина, 5", kind="cleaning",
                 service_kind="cleaning", specialist_enums=(OLGA_ENUM,))


def furniture_order():
    return Order(order_id=700, phone10="9000000010", created_at=ORDER_MOMENT,
                 amount_total=Decimal("5950"), masters=[("Дмитрий Козлов", "79306858534")],
                 client_name="Ирина", address="ул. Ленина, 5")


def make_engine(amo, store, *, service_check):
    return Engine(amo=amo, store=store, specialists=SPECIALISTS, dry_run=False,
                  service_check=service_check,
                  now=lambda: ORDER_MOMENT + timedelta(minutes=1))


def add_open_deal(amo, lead_id, service_enum):
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 created_at=int(ORDER_MOMENT.timestamp()) - 86400,
                 custom_fields_values=[{"field_id": ids.FIELD_SERVICE,
                                        "values": [{"enum_id": service_enum}]}])


async def test_darya_cleaning_goes_to_new_deal_and_sofa_is_untouched():
    amo, store = FakeAmo(), FakeStore()
    add_open_deal(amo, SOFA_LEAD, ids.SERVICE_ENUM_FURNITURE)

    link = await make_engine(amo, store, service_check=True).process_order(cleaning_order())

    assert link.path == "C"
    assert amo.leads[SOFA_LEAD]["status_id"] == ids.REAL_STAGE_CREATED
    assert all(lead_id != SOFA_LEAD for lead_id, _ in amo.calls_of("update_lead"))


async def test_switch_off_keeps_old_behaviour():
    amo, store = FakeAmo(), FakeStore()
    add_open_deal(amo, SOFA_LEAD, ids.SERVICE_ENUM_FURNITURE)

    link = await make_engine(amo, store, service_check=False).process_order(cleaning_order())

    assert link.path == "A" and link.real_lead_id == SOFA_LEAD


async def test_other_service_deal_becomes_owner_question():
    amo, store = FakeAmo(), FakeStore()
    add_open_deal(amo, 555, ids.SERVICE_ENUM_OTHER)

    link = await make_engine(amo, store, service_check=True).process_order(furniture_order())

    assert link.status == "waiting_owner"
    assert link.question["reason"] == "ask_owner_other"
    assert [option["lead_id"] for option in link.question["options"]] == [555]
    assert amo.calls_of("update_lead") == []


def test_other_service_card_explains_the_question():
    question = {"reason": "ask_owner_other",
                "options": [{"lead_id": 555, "label": "Сделка 555"}]}

    text, keyboard = question_card(furniture_order(), question)

    assert "«Другое»" in text
    buttons = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "➕ Создать новую" in buttons and "✋ Сам разберусь" in buttons
