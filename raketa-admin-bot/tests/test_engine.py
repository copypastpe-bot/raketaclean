"""Движок: полный путь заказа от решения матчера до проведённой сделки.

Ни живой amoCRM, ни Postgres: и то, и другое подменено двойниками из tests/fakes.py.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from adminbot.amo import ids
from adminbot.amo.client import AmoError
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import AmoLink, Order
from adminbot.sync.engine import Engine
from adminbot.sync.specialists import SpecialistIndex
from tests.fakes import FakeAmo, FakeStore

KOZLOV_ENUM, POLOZOV_ENUM, OLGA_ENUM = 951507, 951505, 952251
SPECIALISTS = SpecialistIndex.from_enums([
    {"id": KOZLOV_ENUM, "value": "Дмитрий Козлов +79306858534"},
    {"id": POLOZOV_ENUM, "value": "Никита Полозов +79101251720"},
    {"id": OLGA_ENUM, "value": "Ольга Скоропашкина 89081572721"},
])

ORDER_MOMENT = datetime(2026, 8, 24, 17, 53, tzinfo=MOSCOW_TZ)


def make_order(order_id=596, amount="5950", rating=None, master=("Дмитрий Козлов", "79306858534"),
              phone10="9601861067"):
    return Order(
        order_id=order_id,
        phone10=phone10,
        created_at=ORDER_MOMENT,
        amount_total=Decimal(amount),
        masters=[master] if master else [],
        rating_score=rating,
        client_name="Ирина",
        address="ул. Ленина, 5",
    )


def make_engine(amo, store, *, dry_run=False, now=None, child_by_note=False):
    return Engine(amo=amo, store=store, specialists=SPECIALISTS, dry_run=dry_run,
                  child_by_note=child_by_note,
                  now=now or (lambda: ORDER_MOMENT + timedelta(minutes=1)))


def open_realization_lead(amo, lead_id=41463832_00, **extra):
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 created_at=int(ORDER_MOMENT.timestamp()) - 86400, **extra)
    return lead_id


# --- путь А: автосделка есть, довести до конца ---

async def test_path_a_completes_the_deal():
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)
    amo.add_task(lead_id, 1, 2270740)                     # «Назначь мастера»
    amo.add_task(lead_id, 2, ids.TASK_TYPE_FEEDBACK)      # «Получить ОС»

    link = await make_engine(amo, store).process_order(make_order())

    assert link.status == "done" and link.path == "A" and link.real_lead_id == lead_id
    assert amo.leads[lead_id]["status_id"] == ids.STATUS_SUCCESS     # проведена
    assert amo.leads[lead_id]["price"] == 5950                       # бюджет = сумма чека
    assert amo.calls_of("complete_task") == [1]      # автозадача закрыта
    # «Получить ОС» не закрыта: клиент не поставил оценку (решение владельца №9)
    assert 2 not in amo.calls_of("complete_task")


async def test_feedback_task_closed_when_client_rated():
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)
    amo.add_task(lead_id, 2, ids.TASK_TYPE_FEEDBACK)

    await make_engine(amo, store).process_order(make_order(rating=5))

    assert amo.calls_of("complete_task") == [2]


async def test_service_is_set_by_master_when_empty():
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)

    await make_engine(amo, store).process_order(make_order())

    _, payload = amo.calls_of("update_lead")[0]
    services = [field for field in payload["custom_fields"]
                if field["field_id"] == ids.FIELD_SERVICE]
    assert services == [{"field_id": ids.FIELD_SERVICE,
                         "values": [{"enum_id": ids.SERVICE_ENUM_FURNITURE}]}]


async def test_service_is_not_touched_when_already_filled():
    """Решение владельца №7: заполненную «Услугу» не трогаем."""
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo, custom_fields_values=[
        {"field_id": ids.FIELD_SERVICE, "values": [{"value": "Уборка", "enum_id": 772345}]}])

    await make_engine(amo, store).process_order(make_order())

    _, payload = amo.calls_of("update_lead")[0]
    assert all(field["field_id"] != ids.FIELD_SERVICE for field in payload["custom_fields"])


async def test_cleaning_master_gets_cleaning_service():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)

    await make_engine(amo, store).process_order(
        make_order(master=("Ольга Скоропашкина", "89081572721")))

    _, payload = amo.calls_of("update_lead")[0]
    services = [field for field in payload["custom_fields"]
                if field["field_id"] == ids.FIELD_SERVICE]
    assert services[0]["values"] == [{"enum_id": ids.SERVICE_ENUM_CLEANING}]


# --- заказ уже проведён руками ---

async def test_already_done_binds_without_touching():
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(500, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600,
                 closed_at=int(ORDER_MOMENT.timestamp()))

    link = await make_engine(amo, store).process_order(make_order())

    assert link.status == "done" and link.path == "done" and link.real_lead_id == 500
    assert not amo.calls_of("update_lead") and not amo.calls_of("move_lead")


async def test_already_done_reads_address_without_writing():
    """Задача 9 (ТЗ 2026-09-16): дыра из задачи 1 — путь done не заходил в _fill_lead,
    поэтому deal_address оставался пустым, даже когда в сделке адрес уже был.
    """
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(500, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600,
                 closed_at=int(ORDER_MOMENT.timestamp()),
                 custom_fields_values=[
                     {"field_id": ids.FIELD_ADDRESS, "values": [{"value": "ул. Мира, 10"}]}])

    link = await make_engine(amo, store).process_order(make_order())

    assert link.deal_address == "ул. Мира, 10"
    assert not amo.calls_of("update_lead") and not amo.calls_of("move_lead")


async def test_already_done_leaves_address_empty_when_deal_has_none():
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(500, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600,
                 closed_at=int(ORDER_MOMENT.timestamp()))

    link = await make_engine(amo, store).process_order(make_order())

    assert link.deal_address is None


# --- путь Г: вопрос владельцу ---

async def test_ambiguous_case_waits_for_owner():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo, lead_id=601)
    open_realization_lead(amo, lead_id=602)

    link = await make_engine(amo, store).process_order(make_order())

    assert link.status == "waiting_owner"
    assert not amo.calls_of("update_lead")           # ничего не трогали
    question = store.actions_of("ask_owner")[0]
    assert set(question["payload"]["options"]) == {601, 602}


# --- режим репетиции ---

async def test_dry_run_changes_nothing_but_records_everything():
    amo, store = FakeAmo(dry_run=True), FakeStore()
    lead_id = open_realization_lead(amo)

    link = await make_engine(amo, store, dry_run=True).process_order(make_order())

    assert link.status == "done"                      # цепочка пройдена целиком
    # но в амо ничего не изменилось: этап прежний, бюджет не проставлен
    assert amo.leads[lead_id]["status_id"] == ids.REAL_STAGE_CREATED
    assert "price" not in amo.leads[lead_id]
    assert all(row["dry_run"] for row in store.actions if row["action"] != "ask_owner")
    assert store.actions_of("update_lead")            # намерения записаны в журнал


# --- сбои и продолжение с места остановки ---

async def test_amo_failure_saves_progress_and_marks_error():
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)
    amo.fail_on = "move_lead"

    link = await make_engine(amo, store).process_order(make_order())

    assert link.status == "error" and "move_lead" in (link.last_error or "")
    assert "fill_realization" in link.checklist       # первый шаг сохранён
    assert "move_realization_done" not in link.checklist


async def test_next_tick_continues_from_where_it_stopped():
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)
    amo.fail_on = "move_lead"
    order = make_order()

    await make_engine(amo, store).process_order(order)   # упали на смене этапа
    amo.fail_on = None
    link = await make_engine(amo, store).process_order(order)   # следующий тик

    assert link.status == "done"
    assert len(amo.calls_of("update_lead")) == 1        # заполнение НЕ повторилось
    assert amo.leads[lead_id]["status_id"] == ids.STATUS_SUCCESS


async def test_finished_order_is_not_processed_again():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)
    order = make_order()
    engine = make_engine(amo, store)

    await engine.process_order(order)
    calls_before = len(amo.calls)
    await engine.process_order(order)

    assert len(amo.calls) == calls_before             # повторный тик ничего не делает


# --- путь Б: ожидание автосделки сейлзбота ---

async def test_path_b_waits_for_salesbot():
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)

    link = await make_engine(amo, store).process_order(make_order())

    assert link.status == "waiting_salesbot" and link.path == "B"
    assert link.primary_lead_id == 700
    assert amo.leads[700]["status_id"] == ids.STATUS_SUCCESS     # «Передано в работу»


async def test_path_b_continues_when_salesbot_created_the_deal():
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)
    order = make_order()

    await make_engine(amo, store).process_order(order)
    # сейлзбот создал автосделку
    amo.add_lead(701, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 created_at=int(ORDER_MOMENT.timestamp()))
    link = await make_engine(amo, store).process_order(order)

    assert link.status == "done" and link.real_lead_id == 701
    assert amo.leads[701]["status_id"] == ids.STATUS_SUCCESS


async def test_path_b_waits_out_a_slow_salesbot():
    """Амо подтормаживает: через 20 минут ещё ждём, а не дёргаем владельца.

    Владелец видел задержки автосделки до 20 минут (2026-08-26), поэтому порог
    поднят до 40. Лишний вопрос обходится дороже лишнего ожидания.
    """
    amo = FakeAmo()
    store = FakeStore(now=lambda: ORDER_MOMENT + timedelta(minutes=1))
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)
    order = make_order()

    await make_engine(amo, store).process_order(order)
    still_waiting = make_engine(amo, store, now=lambda: ORDER_MOMENT + timedelta(minutes=20))
    link = await still_waiting.process_order(order)

    assert link.status == "waiting_salesbot"


async def test_path_b_asks_owner_when_salesbot_is_silent_too_long():
    """Дизайн §5.3: автосделки нет и через сорок минут — карточка-вопрос владельцу."""
    amo = FakeAmo()
    # Часы у хранилища и у движка общие: иначе «сколько уже ждём» не посчитать.
    store = FakeStore(now=lambda: ORDER_MOMENT + timedelta(minutes=1))
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)
    order = make_order()

    await make_engine(amo, store).process_order(order)
    late = make_engine(amo, store, now=lambda: ORDER_MOMENT + timedelta(minutes=45))
    link = await late.process_order(order)

    assert link.status == "waiting_owner"
    assert store.actions_of("salesbot_timeout")


async def test_wait_salesbot_timer_counts_from_move_primary_success_not_updated_at():
    """Дефект 22.09 (задача 2 ТЗ `order-chain`): `_run_checklist` перед каждым
    `StepResult(wait=True)` зовёт `store.update(status="waiting_salesbot")` —
    это двигает `updated_at`, даже когда статус не поменялся. Считать ожидание
    от него нельзя: `updated_at` обнуляется на каждом опросе, и вопрос
    владельцу не пришёл бы никогда. Отсчёт — от отметки шага
    `move_primary_success` в чек-листе.
    """
    amo, store = FakeAmo(), FakeStore()
    moved_at = ORDER_MOMENT - timedelta(minutes=20)
    order = make_order()
    store.links[order.order_id] = AmoLink(
        order_id=order.order_id, phone10=order.phone10, status="waiting_salesbot",
        path="B", primary_lead_id=700,
        checklist={"fill_primary": moved_at.isoformat(),
                  "move_primary_success": moved_at.isoformat()},
        created_at=moved_at, updated_at=ORDER_MOMENT - timedelta(minutes=1),
    )
    engine = make_engine(amo, store, now=lambda: ORDER_MOMENT)
    engine.salesbot_wait_sec = 600

    link = await engine.process_order(order)

    assert link.status == "waiting_owner"
    assert store.actions_of("salesbot_timeout")


async def test_child_by_note_picks_the_linked_deal_over_a_free_one():
    """Дефект 20.09 (Гагарина/Малая Ямская): при AMO_CHILD_BY_NOTE робот не
    забирает чужую свободную сделку воронки 2 — дочка берётся строго
    по служебному примечанию сейлзбота у лида воронки 1.
    """
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)
    order = make_order()

    await make_engine(amo, store, child_by_note=True).process_order(order)

    # Другая свободная открытая сделка того же клиента — случай Натальи 20.09.
    amo.add_lead(701, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 created_at=int(ORDER_MOMENT.timestamp()))
    # Настоящая дочка — по примечанию у лида первичной (700).
    amo.add_lead(702, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 created_at=int(ORDER_MOMENT.timestamp()))
    amo.add_child_note(700, 702)

    link = await make_engine(amo, store, child_by_note=True).process_order(order)

    assert link.real_lead_id == 702
    assert link.status == "done"
    logged = store.actions_of("child_by_note")
    assert logged and logged[0]["payload"] == {"parent": 700, "child": 702}


async def test_child_by_note_waits_when_there_is_no_note_yet():
    """Примечания ещё нет — робот ждёт, а не хватает первую попавшуюся сделку."""
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)
    order = make_order()

    await make_engine(amo, store, child_by_note=True).process_order(order)
    amo.add_lead(701, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 created_at=int(ORDER_MOMENT.timestamp()))

    link = await make_engine(amo, store, child_by_note=True).process_order(order)

    assert link.status == "waiting_salesbot"
    assert link.real_lead_id is None


async def test_child_by_note_stops_when_the_linked_deal_is_already_closed():
    """Ревью 22.09: дочку из примечания привязываем всегда — это факт цепочки —
    но если она уже закрыта руками (успех или отказ), дальше в неё не пишем.
    """
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)
    order = make_order()

    await make_engine(amo, store, child_by_note=True).process_order(order)

    amo.add_lead(702, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS,
                 created_at=int(ORDER_MOMENT.timestamp()))
    amo.add_child_note(700, 702)
    writes_before = len(amo.calls_of("update_lead")) + len(amo.calls_of("move_lead"))

    link = await make_engine(amo, store, child_by_note=True).process_order(order)

    assert link.real_lead_id == 702                # привязали — это факт цепочки
    assert link.status == "done"
    assert link.last_error == "сделка реализации уже закрыта в CRM, не трогал"
    # После привязки в амо ни одной новой записи: дальше шаги не выполнялись.
    assert len(amo.calls_of("update_lead")) + len(amo.calls_of("move_lead")) == writes_before
    logged = store.actions_of("child_closed")
    assert logged and logged[0]["payload"] == {"child": 702, "status_id": ids.STATUS_SUCCESS}


async def test_child_by_note_treats_a_deleted_deal_as_closed():
    """Примечание есть, а сделки уже нет (get_lead → None) — тоже не трогаем."""
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(700, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)
    order = make_order()

    await make_engine(amo, store, child_by_note=True).process_order(order)
    amo.add_child_note(700, 703)                    # дочки в amo.leads нет вовсе

    link = await make_engine(amo, store, child_by_note=True).process_order(order)

    assert link.real_lead_id == 703
    assert link.status == "done"
    logged = store.actions_of("child_closed")
    assert logged and logged[0]["payload"] == {"child": 703, "status_id": None}


async def test_duplicate_leads_get_a_comment():
    """Клиент звонил дважды: второму лиду робот пишет комментарий (заказ №583)."""
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(800, ids.PIPELINE_PRIMARY, ids.STATUS_UNSORTED_PRIMARY,
                 created_at=int(ORDER_MOMENT.timestamp()) - 7200)
    amo.add_lead(801, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD,
                 created_at=int(ORDER_MOMENT.timestamp()) - 3600)

    link = await make_engine(amo, store).process_order(make_order())

    assert link.primary_lead_id == 801
    noted = amo.calls_of("add_note")
    assert len(noted) == 1 and noted[0][0] == 800


# --- путь В: сделки нет вовсе ---

async def test_path_c_creates_contact_and_lead():
    amo, store = FakeAmo(), FakeStore()          # ни сделок, ни контактов

    link = await make_engine(amo, store).process_order(make_order())

    assert link.path == "C"
    assert amo.calls_of("create_contact")        # контакт заведён
    created = amo.calls_of("create_lead")[0]
    assert created["pipeline_id"] == ids.PIPELINE_PRIMARY
    assert created["status_id"] == ids.PRIM_STAGE_NEW_LEAD
    assert created["price"] == Decimal("5950")
    assert link.status == "waiting_salesbot"     # ждём автосделку сейлзбота


async def test_path_c_reuses_existing_contact():
    amo, store = FakeAmo(), FakeStore()
    amo.contacts.append({"id": 111, "name": "Ирина"})

    await make_engine(amo, store).process_order(make_order())

    assert not amo.calls_of("create_contact")    # контакт нашёлся, второй не нужен
    assert amo.calls_of("create_lead")[0]["contact_id"] == 111


# --- одна сделка не закрывает два заказа ---

async def test_deal_taken_by_another_order_is_not_reused():
    amo, store = FakeAmo(), FakeStore()
    first_lead = open_realization_lead(amo, lead_id=901)
    second_lead = open_realization_lead(amo, lead_id=902)
    engine = make_engine(amo, store)

    first = await engine.process_order(make_order(order_id=1))
    second = await engine.process_order(make_order(order_id=2))

    assert first.status == "waiting_owner"       # два кандидата — вопрос владельцу
    # после ответа владельца первый заказ занял сделку
    await store.update(1, status="done", path="A", real_lead_id=901)
    third = await engine.process_order(make_order(order_id=3))
    assert third.real_lead_id == 902             # остаётся только вторая


async def test_ask_owner_options_carry_the_deal_address():
    """Задача 7 (ТЗ 2026-09-22): карточка-вопрос различает варианты адресом —
    `LeadInfo.address` должен долетать до `question["options"]`.
    """
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo, lead_id=901, custom_fields_values=[
        {"field_id": ids.FIELD_ADDRESS, "values": [{"value": "ул. Мира, 10"}]}])
    open_realization_lead(amo, lead_id=902)

    link = await make_engine(amo, store).process_order(make_order())

    assert link.status == "waiting_owner"
    addresses = {option["lead_id"]: option["address"] for option in link.question["options"]}
    assert addresses == {901: "ул. Мира, 10", 902: None}


async def test_deal_taken_by_another_order_is_not_reused_across_different_phones():
    """Задача 6, ТЗ 2026-09-22: один и тот же человек с двумя номерами —
    «занято» считается по номеру сделки, а не по телефону заказа. Раньше
    заказ со вторым номером не видел первую связку занятой и забирал чужую
    сделку.
    """
    amo, store = FakeAmo(), FakeStore()
    first_lead = open_realization_lead(amo, lead_id=901)
    second_lead = open_realization_lead(amo, lead_id=902)
    engine = make_engine(amo, store)

    first = await engine.process_order(make_order(order_id=1, phone10="9601861067"))
    assert first.status == "waiting_owner"       # два кандидата — вопрос владельцу
    await store.update(1, status="done", path="A", real_lead_id=901)

    # второй заказ того же человека, но с другим номером телефона
    second = await engine.process_order(make_order(order_id=2, phone10="9219998877"))
    assert second.real_lead_id == 902             # сделка 901 занята первым заказом


# --- телефон не распознан ---

async def test_order_without_phone_waits_for_owner():
    amo, store = FakeAmo(), FakeStore()
    order = Order(order_id=1, phone10=None, created_at=ORDER_MOMENT,
                  amount_total=Decimal("1000"), masters=[])

    link = await make_engine(amo, store).process_order(order)

    assert link.status == "waiting_owner"
    assert not amo.calls                          # в амо даже не ходили


# --- независимость движков друг от друга ---

async def test_engines_do_not_share_scratch_state():
    """Каждый движок считает сам за себя.

    В сервисе их несколько сразу: наблюдатель, репетиция предпросмотра и боевой
    прогон хвоста. Если бы черновые заметки (лиды-дубли, найденный контакт) были
    общими, боевой движок дописывал бы в CRM по чужому расчёту.
    """
    first = make_engine(FakeAmo(), FakeStore())
    second = make_engine(FakeAmo(), FakeStore())

    first._duplicates[596] = (41400001,)
    first._contacts[596] = 55

    assert second._duplicates == {}
    assert second._contacts == {}


# --- доводка сделки после оплаты по счёту (задача 11, ТЗ 2026-09-22) ---

def make_wire_order(order_id=700, amount="5500", awaiting=False):
    return Order(order_id=order_id, phone10="9601861067", created_at=ORDER_MOMENT,
                amount_total=Decimal(amount), masters=[], client_name="Ирина",
                payment_method="Расчётный", awaiting_wire_payment=awaiting)


async def test_move_realization_done_marks_payment_pending_when_wire_awaiting():
    """Остановка на «Заказ выполнен» из-за неоплаченного счёта помечается
    payment_pending — доводка (process_payment) потом находит её по этому флагу."""
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)

    link = await make_engine(amo, store).process_order(make_wire_order(awaiting=True))

    assert link.status == "done"
    assert amo.leads[lead_id]["status_id"] == ids.REAL_STAGE_DONE   # не STATUS_SUCCESS
    assert store.links[700].payment_pending is True


async def test_move_realization_done_leaves_payment_pending_unset_for_cash():
    """Оплата не по счёту — флаг не трогаем вовсе (остаётся None)."""
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)
    order = Order(order_id=701, phone10="9601861067", created_at=ORDER_MOMENT,
                 amount_total=Decimal("3000"), masters=[], payment_method="Наличные")

    await make_engine(amo, store).process_order(order)

    assert store.links[701].payment_pending is None


async def test_process_payment_completes_the_deal_when_stage_is_done():
    amo, store = FakeAmo(), FakeStore()
    lead_id = 41463832_10
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE, price=1)
    amo.add_task(lead_id, 1, 2270740)                     # автозадача, закрываем
    link = AmoLink(order_id=700, phone10="9601861067", status="done", path="A",
                  real_lead_id=lead_id, payment_pending=True)
    store.links[700] = link
    order = make_wire_order()

    result = await make_engine(amo, store).process_payment(order, link)

    assert amo.leads[lead_id]["status_id"] == ids.STATUS_SUCCESS
    assert amo.leads[lead_id]["price"] == 5500
    assert amo.calls_of("complete_task") == [1]
    note_text = amo.calls_of("add_note")[0][1]
    assert "Оплата по счёту получена: 5500 ₽" in note_text
    assert "выполнено и оплата получена" in note_text
    assert result.stage_moved is True
    assert result.tasks_closed == 1
    assert result.changed is True
    updated = store.links[700]
    assert updated.payment_synced_at is not None
    action = store.actions_of("wire_payment_synced")[0]
    assert action["payload"] == {"lead_id": lead_id, "stage": ids.REAL_STAGE_DONE,
                                 "tasks_closed": 1, "stage_moved": True, "changed": True}


async def test_process_payment_fixes_placeholder_price_and_closes_tasks_in_final_stage():
    """Сделка уже финальная (вопреки ожиданию) с ценой-заглушкой и открытой
    задачей — задача 11 доводится: сумма и задача, стадию не трогаем, примечания
    нет (оно только про перевод стадии — ревью 23.09)."""
    amo, store = FakeAmo(), FakeStore()
    lead_id = 41463832_11
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS, price=1)
    amo.add_task(lead_id, 1, 2270740)
    link = AmoLink(order_id=701, phone10="9601861067", status="done", path="A",
                  real_lead_id=lead_id)
    store.links[701] = link
    order = make_wire_order(order_id=701)

    result = await make_engine(amo, store).process_payment(order, link)

    assert amo.leads[lead_id]["price"] == 5500
    assert amo.leads[lead_id]["status_id"] == ids.STATUS_SUCCESS     # стадия не тронута
    assert amo.calls_of("move_lead") == []
    assert amo.calls_of("complete_task") == [1]                      # задачу закрыли
    assert amo.calls_of("add_note") == []                             # примечания нет
    assert result.stage_moved is False
    assert result.tasks_closed == 1
    assert result.changed is True                     # сумма и задача — есть о чём отчитаться
    assert store.links[701].payment_synced_at is not None


async def test_process_payment_does_nothing_when_final_stage_price_looks_right():
    amo, store = FakeAmo(), FakeStore()
    lead_id = 41463832_12
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS, price=5500)
    link = AmoLink(order_id=702, phone10="9601861067", status="done", path="A",
                  real_lead_id=lead_id)
    store.links[702] = link
    order = make_wire_order(order_id=702)

    result = await make_engine(amo, store).process_payment(order, link)

    assert amo.calls_of("update_lead") == []
    assert amo.calls_of("add_note") == []
    assert amo.calls_of("move_lead") == []
    assert amo.calls_of("complete_task") == []
    assert result.stage_moved is False
    assert result.tasks_closed == 0
    assert result.changed is False                    # менять было нечего — молчим
    assert store.links[702].payment_synced_at is not None            # второй раз не возьмут


async def test_process_payment_journal_dry_run_follows_the_engine_flag():
    """Своя репетиция (WIRE_PAYMENT_DRY_RUN), не общий amo_sync_dry_run."""
    amo, store = FakeAmo(dry_run=True), FakeStore()
    lead_id = 41463832_13
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE, price=1)
    link = AmoLink(order_id=703, phone10="9601861067", status="done", path="A",
                  real_lead_id=lead_id, payment_pending=True)
    store.links[703] = link
    order = make_wire_order(order_id=703)

    await make_engine(amo, store, dry_run=True).process_payment(order, link)

    assert amo.leads[lead_id]["status_id"] == ids.REAL_STAGE_DONE    # в амо не записано
    action = store.actions_of("wire_payment_synced")[0]
    assert action["dry_run"] is True


async def test_process_payment_retries_closing_tasks_after_a_previous_failure():
    """Ревью 23.09: сбой между move_lead и close_tasks не должен оставлять
    задачу открытой навсегда. Первый проход — move_lead прошёл, close_tasks
    упал: payment_synced_at не ставится. Второй проход видит сделку уже
    финальной и всё равно закрывает задачу — эта ветка их больше не пропускает."""
    amo, store = FakeAmo(), FakeStore()
    lead_id = 41463832_14
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE, price=1)
    amo.add_task(lead_id, 1, 2270740)
    link = AmoLink(order_id=704, phone10="9601861067", status="done", path="A",
                  real_lead_id=lead_id, payment_pending=True)
    store.links[704] = link
    order = make_wire_order(order_id=704)
    engine = make_engine(amo, store)

    amo.fail_on = "complete_task"
    with pytest.raises(AmoError):
        await engine.process_payment(order, link)

    assert amo.leads[lead_id]["status_id"] == ids.STATUS_SUCCESS     # move_lead успел пройти
    assert store.links[704].payment_synced_at is None                 # не отмечено — попробуем снова

    amo.fail_on = None
    link = store.links[704]                                           # перечитали обновлённую связку
    result = await engine.process_payment(order, link)

    assert amo.calls_of("complete_task") == [1]           # задача закрыта на повторном проходе
    assert result.tasks_closed == 1
    assert result.stage_moved is False        # стадия уже была финальной ко второму проходу
    assert result.changed is True
    assert store.links[704].payment_synced_at is not None
