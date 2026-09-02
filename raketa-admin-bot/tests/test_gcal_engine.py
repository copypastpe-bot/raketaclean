"""Движок календаря: от записи в блокноте владельца до заполненной сделки.

Что здесь важнее всего проверить:

- робот **не трогает то, что уже заполнено** — правки владельца в CRM важнее
  догадок робота (общее правило проекта);
- робот **не двигает сделку дальше «Заказ оформлен»** и не закрывает автозадачу
  «Назначь мастера»: мастера в записи календаря нет, и говорить в CRM, что он
  назначен, робот не вправе (решение владельца 13);
- **бюджет не трогается** — сумма чека известна только боту;
- в репетиции в амо не уходит ни одного запроса.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from adminbot.amo import ids
from adminbot.gcal.engine import CalendarEngine
from adminbot.gcal.event import EventKind, ParsedEvent
from adminbot.gcal.store import MemoryCalendarStore
from tests.fakes import FakeAmo

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=MSK)
ORDER_DAY = date(2026, 8, 27)


def an_order(**overrides) -> ParsedEvent:
    """Обычная запись: «Сов! Матрас, Юлия» на завтра."""
    fields = dict(
        event_id="evt-1", kind=EventKind.ORDER, order_date=ORDER_DAY,
        start_at=datetime(2026, 8, 27, 14, 30, tzinfo=MSK),
        phones=("9605379757",), client_name="Юлия", district="советский",
        services=("mattress",), address="Панина д 7к2, кв 132",
        comment="Матрас/2\nСушка/2\nаллергик астматик спит",
        summary="Сов! Матрас, Юлия",
    )
    fields.update(overrides)
    return ParsedEvent(**fields)


def build(amo, *, dry_run=False, store=None) -> CalendarEngine:
    # Часы у движка и хранилища общие: движок считает по ним, сколько уже ждёт
    # автосделку, и разное время сломало бы этот счёт.
    return CalendarEngine(amo=amo, store=store or MemoryCalendarStore(now=lambda: NOW),
                          dry_run=dry_run, now=lambda: NOW)


def sent_fields(amo, call: int = 0) -> dict[int, dict]:
    """Поля, которые робот отправил в амо при заполнении сделки, по id поля."""
    _, payload = amo.calls_of("update_lead")[call]
    return {field["field_id"]: field for field in payload["custom_fields"]}


def only_value(field: dict):
    return field["values"][0].get("value", field["values"][0].get("enum_id"))


def stamp_of(moment: datetime) -> int:
    """Как амо хранит «Дату и время заказа» — unix-время."""
    return int(moment.timestamp())


def day_of(field: dict) -> date:
    """День из поля «Дата и время заказа» по Москве — им и меряется перенос."""
    return datetime.fromtimestamp(only_value(field), tz=timezone.utc).astimezone(MSK).date()


@pytest.fixture
def amo():
    return FakeAmo()


async def test_existing_realization_deal_is_filled_not_moved(amo):
    """Сделка уже есть — робот дозаполняет её и оставляет на месте (решение 13)."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    engine = build(amo)

    link = await engine.process(an_order())

    fields = sent_fields(amo)
    assert only_value(fields[ids.FIELD_ADDRESS]) == "Панина д 7к2, кв 132"
    assert "аллергик астматик спит" in only_value(fields[ids.FIELD_COMMENT])
    assert only_value(fields[ids.FIELD_DISTRICT]) == ids.DISTRICT_ENUMS["советский"]
    assert only_value(fields[ids.FIELD_SERVICE]) == 128971           # «Чистка матрасов»
    assert amo.calls_of("move_lead") == []                           # этап не двигали
    assert "price" not in amo.calls_of("update_lead")[0][1]          # бюджет не наш
    assert ids.FIELD_SPECIALIST not in fields                        # мастера не знаем
    assert link.status == "done"
    assert link.real_lead_id == 41400001


async def test_filled_fields_are_never_overwritten(amo):
    """Заполненные поля робот не трогает. Кроме адреса и комментария.

    Оба стали исключением по решению владельца 2026-08-27: амо подставляет
    в сделку адрес из карточки клиента, а в комментарии лида стоит огрызок
    от заявки («Хим чи») — состав заказа и адрес живут в календаре.
    """
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 custom_fields_values=[
                     {"field_id": ids.FIELD_PAYMENT_TYPE,
                      "values": [{"value": "Наличка", "enum_id": ids.PAYMENT_ENUM_CASH}]},
                     {"field_id": ids.FIELD_SERVICE,
                      "values": [{"value": "Уборка", "enum_id": ids.SERVICE_ENUM_CLEANING}]},
                 ])
    engine = build(amo)

    await engine.process(an_order())

    fields = sent_fields(amo)
    assert ids.FIELD_SERVICE not in fields          # услуга не переписана
    assert ids.FIELD_ADDRESS in fields              # адрес обновлён из записи
    assert ids.FIELD_COMMENT in fields              # и комментарий тоже


async def test_autotasks_stay_open(amo):
    """«Назначь мастера» — работа владельца, робот её не закрывает (решение 13)."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    amo.add_task(41400001, task_id=7, task_type_id=2270740)
    engine = build(amo)

    await engine.process(an_order())

    assert amo.calls_of("complete_task") == []


async def test_primary_lead_is_handed_over_and_waits_for_the_salesbot(amo):
    """Лид первичной: заполнить → «Передано в работу» → ждать автосделку."""
    amo.add_lead(41400002, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_DIALOG)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)

    link = await engine.process(an_order())

    assert amo.calls_of("move_lead") == [(41400002, ids.PIPELINE_PRIMARY, ids.STATUS_SUCCESS)]
    assert link.status == "waiting_salesbot"
    assert link.primary_lead_id == 41400002

    # Сейлзбот создал автосделку — следующий проход её подхватывает и дозаполняет.
    amo.add_lead(41400003, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    link = await engine.process(an_order())

    assert link.status == "done"
    assert link.real_lead_id == 41400003
    assert ids.FIELD_COMMENT in sent_fields(amo, call=-1)   # автосделку дозаполнили


async def test_no_deal_at_all_starts_the_chain(amo):
    """Сделки нет: находим или заводим контакт, создаём лид, передаём в работу."""
    engine = build(amo)

    link = await engine.process(an_order())

    created = amo.calls_of("create_contact")
    assert created and created[0] == ("Юлия", "+79605379757")
    assert amo.calls_of("create_lead")
    assert link.status == "waiting_salesbot"


async def test_silent_kinds_are_skipped_without_touching_amo(amo):
    """Выходной мастера, перемыв и запись без телефона — молча мимо."""
    engine = build(amo)

    for kind, event_id in ((EventKind.BLOCK, "b"), (EventKind.REWASH, "r"),
                           (EventKind.SKIP, "s"), (EventKind.UNSETTLED, "u")):
        link = await engine.process(ParsedEvent(event_id=event_id, kind=kind,
                                                order_date=ORDER_DAY))
        assert link.status == "skipped", kind
        assert link.skip_reason

    assert amo.calls == []


async def test_unsettled_event_returns_to_work_when_the_mark_is_removed(amo):
    """Владелец снял «⁉️» — робот берёт запись в работу (решение 9)."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)

    await engine.process(ParsedEvent(event_id="evt-1", kind=EventKind.UNSETTLED,
                                     order_date=ORDER_DAY))
    link = await engine.process(an_order())

    assert link.status == "done"


async def test_rehearsal_writes_nothing_to_amo(amo):
    """Репетиция: решения принимаем, в CRM не пишем ничего."""
    amo.dry_run = True
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, dry_run=True, store=store)

    await engine.process(an_order())

    assert amo.leads[41400001].get("custom_fields_values") is None   # сделка не изменилась
    assert store.actions_of("update_lead")                           # но решение записано
    assert store.actions_of("update_lead")[0]["dry_run"] is True


async def test_progress_is_not_repeated_after_a_restart(amo):
    """Повторный проход по той же записи не делает работу дважды."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)

    await engine.process(an_order())
    calls_after_first = len(amo.calls)
    await engine.process(an_order())

    assert len(amo.calls) == calls_after_first


async def test_missing_salesbot_deal_asks_the_owner(amo):
    """Автосделки нет сорок минут — это уже не задержка, а повод спросить."""
    amo.add_lead(41400002, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_DIALOG)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    await engine.process(an_order())

    engine.now = lambda: NOW + timedelta(minutes=45)
    link = await engine.process(an_order())

    assert link.status == "waiting_owner"
    assert link.question["reason"] == "сейлзбот не создал автосделку"


async def test_amo_failure_leaves_the_event_for_the_next_pass(amo):
    """Амо упала посреди цепочки — запись остаётся в работе, а не теряется."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    amo.fail_on = "update_lead"
    engine = build(amo)

    link = await engine.process(an_order())

    assert link.status == "error"
    assert "AmoError" in link.last_error


async def test_several_services_are_all_written(amo):
    """«Уборка + Мебель» — в амо уходят оба значения: поле это позволяет."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    engine = build(amo)

    await engine.process(an_order(services=("cleaning", "furniture")))

    values = [item["enum_id"] for item in sent_fields(amo)[ids.FIELD_SERVICE]["values"]]
    assert values == [ids.SERVICE_ENUM_CLEANING, ids.SERVICE_ENUM_FURNITURE]


async def test_unknown_district_leaves_the_field_empty(amo):
    """Приставка не расшифрована — поле пустое, догадок нет (решение 12)."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    engine = build(amo)

    await engine.process(an_order(district=None))

    assert ids.FIELD_DISTRICT not in sent_fields(amo)


# --- то, что владелец подтвердил кнопкой ---

async def test_confirmed_cancellation_closes_the_deal(amo):
    """Владелец нажал «Закрыть сделку» — закрывает её обычный проход, не Telegram."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    await store.create("evt-1", kind="order", phone10="9605379757")
    await store.update("evt-1", status="closing", real_lead_id=41400001)

    link = await engine.process(ParsedEvent(event_id="evt-1", kind=EventKind.CANCELLED))

    assert amo.calls_of("move_lead") == [
        (41400001, ids.PIPELINE_REALIZATION, ids.STATUS_CLOSED)]
    assert link.status == "cancelled"


async def test_boat_deal_is_created_after_confirmation(amo):
    """Теплоход: сделка на юрлицо, без телефона и суммы (решение 7)."""
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    boat = ParsedEvent(event_id="evt-boat", kind=EventKind.BOAT,
                       order_date=date(2026, 9, 12), client_name="Толстой",
                       summary="Толстой с 9:00")

    asked = await engine.process(boat)
    assert asked.status == "waiting_owner"             # сам не заводит

    await store.update("evt-boat", status="new", path="BOAT", question=None)
    link = await engine.process(boat)

    created = amo.calls_of("create_lead")[0]
    assert "Толстой" in created["name"]
    assert created["pipeline_id"] == ids.PIPELINE_PRIMARY
    fields = {field["field_id"]: field for field in created["custom_fields"]}
    assert fields[ids.FIELD_CLIENT_TYPE]["values"] == [{"enum_id": ids.CLIENT_TYPE_COMPANY}]
    assert fields[ids.FIELD_DISTRICT]["values"] == [
        {"enum_id": ids.DISTRICT_ENUMS["юридическое лицо"]}]
    assert "price" not in created                      # сумму ставит владелец
    assert link.status == "done"


async def test_deal_closed_by_owner_before_the_answer(amo):
    """Пока карточка висела, сделку закрыли руками — второй раз не трогаем."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    await store.create("evt-1", kind="order", phone10="9605379757")
    await store.update("evt-1", real_lead_id=41400001)

    link = await engine.process(ParsedEvent(event_id="evt-1", kind=EventKind.CANCELLED))

    assert link.status == "cancelled"
    assert amo.calls_of("move_lead") == []


async def test_editing_an_old_record_does_not_wake_it_up(amo):
    """Правка записи, лежавшей в календаре до включения, не заводит сделку.

    Иначе решение владельца «старое веду сам» нарушалось бы при первой же
    правке: робот получил бы изменение и принял запись за новую работу.
    """
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    await store.create("evt-1", kind="order", phone10="9605379757")
    await store.update("evt-1", status="skipped",
                       skip_reason="была в календаре до включения")

    link = await engine.process(an_order(comment="Дописал состав"))

    assert link.status == "skipped"
    assert amo.calls == []


async def test_removing_the_unsettled_mark_still_wakes_the_record(amo):
    """А снятие пометки «⁉️» по-прежнему возвращает запись в работу (решение 9)."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    await store.create("evt-1", kind="unsettled", phone10=None)
    await store.update("evt-1", status="skipped",
                       skip_reason="дата не подтверждена — ждём, пока снимут пометку")

    link = await engine.process(an_order())

    assert link.status == "done"


async def test_salesbot_deal_taken_by_another_record_is_not_stolen(amo):
    """Две записи одного клиента подряд — у каждой своя автосделка.

    После «Передано в работу» робот ищет появившуюся автосделку по телефону.
    Если не проверить, не занята ли найденная сделка другой записью календаря,
    второй заказ прицепится к сделке первого: в CRM данные разъедутся, а при
    отмене робот предложит закрыть чужую сделку.
    """
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)

    # Первая запись прошла цепочку и заняла автосделку 41400010.
    await store.create("evt-first", kind="order", phone10="9605379757")
    await store.update("evt-first", status="done", real_lead_id=41400010)
    amo.add_lead(41400010, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)

    # Вторая запись того же клиента: лид создан, ждём её собственную автосделку.
    await store.create("evt-second", kind="order", phone10="9605379757")
    await store.update("evt-second", status="waiting_salesbot", path="C",
                       primary_lead_id=41400011)
    second = an_order(event_id="evt-second", order_date=date(2026, 8, 28))

    link = await engine.process(second)

    assert link.real_lead_id != 41400010          # чужую сделку не забрали
    assert link.status == "waiting_salesbot"      # ждём свою

    # Сейлзбот создал автосделку для второго лида — её и берём.
    amo.add_lead(41400012, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    link = await engine.process(second)

    assert link.real_lead_id == 41400012


async def test_finished_record_keeps_its_details_fresh(amo):
    """Владелец поправил телефон в проведённой записи — память робота обновляется.

    Работать по такой записи уже нечего, но её сведения нужны позже: по телефону
    робот проверяет, не занята ли сделка другой записью, а в карточке отмены
    показывает владельцу, кому звонить.
    """
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    await store.create("evt-1", kind="order", phone10="9605379757")
    await store.update("evt-1", status="done", real_lead_id=41400001)

    link = await engine.process(an_order(phones=("9151231544",), client_name="Алена"))

    assert link.status == "done"                  # цепочку не начинаем заново
    assert link.phone10 == "9151231544"           # но телефон теперь верный
    assert link.client_name == "Алена"
    assert amo.calls == []                        # в CRM не полезли


async def test_address_from_the_calendar_wins(amo):
    """Адрес заказа берётся из записи, даже если в сделке уже что-то стоит.

    Решение владельца 2026-08-27: в сделку амо подставляет адрес из карточки
    клиента — адрес прошлого заказа. Куда ехать мастеру сегодня, написано
    в календаре, и это главнее.
    """
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 custom_fields_values=[
                     {"field_id": ids.FIELD_ADDRESS,
                      "values": [{"value": "Бор ул. Луначарского 208"}]}])
    engine = build(amo)

    await engine.process(an_order(address="Перекопская, д. 10, п7, э3, кв. 247"))

    fields = sent_fields(amo)
    assert only_value(fields[ids.FIELD_ADDRESS]) == "Перекопская, д. 10, п7, э3, кв. 247"


async def test_address_is_not_written_to_the_client_card(amo):
    """В карточку клиента адрес не уходит: заказ бывает не по адресу клиента."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    amo.contacts.append({"id": 555, "name": "Входящий 79605379757"})
    engine = build(amo)

    await engine.process(an_order())

    for _contact_id, payload in amo.calls_of("update_contact"):
        assert "адрес" not in str(payload).lower()


async def test_comment_from_the_calendar_wins(amo):
    """Состав заказа из записи важнее огрызка, пришедшего с заявкой.

    Живой случай 2026-08-27: в лиде стояло «Хим чи», а в записи — «детский
    матрас с сушкой 2000р / что-то еще / минимальна 3000р». В сделку попал
    огрызок, потому что поле не было пустым.
    """
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 custom_fields_values=[
                     {"field_id": ids.FIELD_COMMENT, "values": [{"value": "Хим чи"}]}])
    engine = build(amo)

    await engine.process(an_order(comment="детский матрас с сушкой 2000р\nминимальна 3000р"))

    assert "детский матрас" in only_value(sent_fields(amo)[ids.FIELD_COMMENT])


async def test_forgotten_deal_does_not_stop_the_work_but_is_reported(amo):
    """Висит незакрытая сделка двухлетней давности — заводим новую и говорим о ней.

    Живой случай 2026-09-02: сделка от 11.09.2024 на этапе «мастер назначен»
    заставила робота спросить владельца по записи на 06.09.2026. Владельцу такие
    вопросы не нужны, но и молчать о мусоре в CRM нельзя — он копится.
    """
    amo.add_lead(29174771, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CONFIRMED,
                 created_at=int(datetime(2024, 9, 11, tzinfo=MSK).timestamp()))
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)

    link = await engine.process(an_order())

    assert link.status != "waiting_owner"                # вопроса владельцу нет
    assert amo.calls_of("create_lead")                   # новая сделка заведена
    told = [row for row in store.actions if row["action"] == "note_forgotten"]
    assert told and told[0]["payload"]["lead_ids"] == [29174771]


async def test_order_date_is_rewritten_when_the_day_moved(amo):
    """Заказ перенесли на другой день — в сделке должен стоять новый день.

    Живой случай 2026-09-02: клиент отменила уборку, а потом подтвердила на
    другое число. Сделка оставалась открытой со старой датой, робот запись
    к ней привязал, комментарий обновил, а дату не тронул — поле было занято.
    Владелец отменил прежнее решение 5: календарь — источник правды о дне
    работы, как адрес и комментарий.
    """
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 custom_fields_values=[
                     {"field_id": ids.FIELD_ORDER_DATETIME,
                      "values": [{"value": stamp_of(datetime(2026, 8, 20, 12, 0, tzinfo=MSK))}]}])
    engine = build(amo)

    await engine.process(an_order())

    assert day_of(sent_fields(amo)[ids.FIELD_ORDER_DATETIME]) == ORDER_DAY


async def test_owner_time_inside_the_same_day_is_kept(amo):
    """День тот же, время в сделке своё — не трогаем и в амо не идём.

    В записи календаря стоит начало работы, а в сделке владелец мог поставить
    удобное ему время. Спорить не о чем: переносом считается смена дня.
    """
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 custom_fields_values=[
                     {"field_id": ids.FIELD_ORDER_DATETIME,
                      "values": [{"value": stamp_of(datetime(2026, 8, 27, 9, 0, tzinfo=MSK))}]}])
    engine = build(amo)

    await engine.process(an_order())

    assert ids.FIELD_ORDER_DATETIME not in sent_fields(amo)


async def test_moved_record_carries_the_new_date_into_the_deal(amo):
    """Запись перетащили на другой день — правка доезжает до сделки.

    Перенос в календаре — это правка записи, а не новая запись. Раньше дата
    в список изменяемых полей не входила вовсе: робот молча оставлял в сделке
    старый день и владельцу об этом не говорил.
    """
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    await engine.process(an_order())
    moved_day = ORDER_DAY + timedelta(days=3)

    await engine.process(an_order(
        order_date=moved_day,
        start_at=datetime(moved_day.year, moved_day.month, moved_day.day, 14, 30, tzinfo=MSK)))

    assert day_of(sent_fields(amo, call=-1)[ids.FIELD_ORDER_DATETIME]) == moved_day
    assert engine.last_edits == ("дата",)       # владельцу скажут, что именно поменялось


async def test_same_comment_is_not_rewritten(amo):
    """Тот же текст второй раз не отправляем: лишний запрос в амо ни к чему."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 custom_fields_values=[
                     {"field_id": ids.FIELD_COMMENT,
                      "values": [{"value": "Матрас/2\nСушка/2\nаллергик астматик спит"}]},
                     {"field_id": ids.FIELD_ADDRESS,
                      "values": [{"value": "Панина д 7к2, кв 132"}]}])
    engine = build(amo)

    await engine.process(an_order())

    fields = sent_fields(amo)
    assert ids.FIELD_COMMENT not in fields
    assert ids.FIELD_ADDRESS not in fields


async def test_edited_record_updates_the_deal(amo):
    """Владелец дописал состав и поменял адрес — робот подтянул это в сделку.

    Решение владельца 2026-08-27: раз адрес и комментарий живут в календаре,
    они должны быть верны и после правки, а не только в момент заведения.
    День работы при этом не менялся — дату в амо второй раз не отправляем.
    """
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    await engine.process(an_order())
    calls_before = len(amo.calls_of("update_lead"))

    changed = await engine.process(an_order(
        address="Другая улица, д 1", comment="Диван + два кресла, сушка"))

    fields = sent_fields(amo, call=-1)
    assert len(amo.calls_of("update_lead")) == calls_before + 1
    assert only_value(fields[ids.FIELD_ADDRESS]) == "Другая улица, д 1"
    assert "два кресла" in only_value(fields[ids.FIELD_COMMENT])
    assert ids.FIELD_ORDER_DATETIME not in fields     # дату не правим
    assert changed.status == "done"


async def test_untouched_record_is_not_rewritten(amo):
    """Запись пришла без изменений — в амо не идём вовсе."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    store = MemoryCalendarStore(now=lambda: NOW)
    engine = build(amo, store=store)
    await engine.process(an_order())
    calls_before = len(amo.calls)

    await engine.process(an_order())

    assert len(amo.calls) == calls_before


# --- «Источник сделки» (правило владельца 2026-08-27) ---

async def test_source_is_filled_in_an_existing_lead_when_empty(amo):
    """Лид есть, источник в нём не указан — ставим «Сарафанное радио»."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    engine = build(amo)

    await engine.process(an_order())

    fields = sent_fields(amo)
    assert fields[ids.FIELD_SOURCE]["values"] == [
        {"enum_id": ids.SOURCE_ENUM_WORD_OF_MOUTH}]


async def test_source_in_the_lead_is_never_overwritten(amo):
    """Источник в лиде указан — он и остаётся: там правда о том, откуда клиент."""
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 custom_fields_values=[
                     {"field_id": ids.FIELD_SOURCE,
                      "values": [{"value": "Авито", "enum_id": 235983}]}])
    engine = build(amo)

    await engine.process(an_order())

    assert ids.FIELD_SOURCE not in sent_fields(amo)


async def test_new_client_gets_word_of_mouth(amo):
    """Ни лида, ни контакта — клиент пришёл впервые, это сарафан."""
    engine = build(amo)

    await engine.process(an_order())

    created = amo.calls_of("create_lead")[0]
    fields = {field["field_id"]: field for field in created["custom_fields"]}
    assert fields[ids.FIELD_SOURCE]["values"] == [
        {"enum_id": ids.SOURCE_ENUM_WORD_OF_MOUTH}]


async def test_known_contact_without_a_lead_is_a_repeat_order(amo):
    """Лида нет, но контакт в CRM есть — значит клиент возвращается."""
    amo.contacts.append({"id": 555, "name": "Юлия"})
    engine = build(amo)

    await engine.process(an_order())

    created = amo.calls_of("create_lead")[0]
    fields = {field["field_id"]: field for field in created["custom_fields"]}
    assert fields[ids.FIELD_SOURCE]["values"] == [{"enum_id": ids.SOURCE_ENUM_REPEAT}]
