"""Движок: полный путь заказа от решения матчера до проведённой сделки.

Ни живой amoCRM, ни Postgres: и то, и другое подменено двойниками из tests/fakes.py.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import Order
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


def make_order(order_id=596, amount="5950", rating=None, master=("Дмитрий Козлов", "79306858534")):
    return Order(
        order_id=order_id,
        phone10="9601861067",
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
