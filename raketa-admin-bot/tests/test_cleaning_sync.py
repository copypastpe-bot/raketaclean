"""Уборки клининг-контура: тот же движок, но своя подпись, услуга и оплата.

Что здесь проверяется — ровно то, чем уборка отличается от заказа химчистки:

- «Услуга» и «Специалист» заданы решением владельца, а не выведены из бригадира;
- свои способы оплаты, включая подарочный сертификат и «Расчётный»;
- у уборки нет «ждём оплату по счёту»: деньги вносят при проведении, поэтому
  сделка идёт до «Успешно реализовано» даже по безналу;
- подпись «Уборка №N» и свои кнопки: ответ по уборке не должен уйти в заказ;
- репетиция не пишет ни в амо, ни в базу.

Ни живой amoCRM, ни Postgres: и то, и другое подменено двойниками.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import Order
from adminbot.sync.engine import Engine
from adminbot.sync.specialists import SpecialistIndex
from adminbot.sync.store import MemoryLinkStore
from tests.fakes import FakeAmo, FakeStore

# Справочник «Специалистов» из боевой амо. Бригадира Ларисы в нём нет вовсе:
# у уборки «Специалистом» всегда стоит Ольга (решение владельца 2026-09-10).
OLGA_ENUM = ids.SPECIALIST_ENUM_CLEANING
SPECIALISTS = SpecialistIndex.from_enums([
    {"id": 951507, "value": "Дмитрий Козлов +79306858534"},
    {"id": OLGA_ENUM, "value": "Ольга Скоропашкина 89081572721"},
])

CLEANING_MOMENT = datetime(2026, 9, 10, 16, 20, tzinfo=MOSCOW_TZ)


def make_cleaning(order_id=5, amount="9000", payment="Наличные",
                  foreman=("Лариса Иванова", "79200000001")):
    """Уборка в том виде, в каком её отдаёт `db._cleaning_order_from_row`."""
    return Order(
        order_id=order_id,
        kind="cleaning",
        phone10="9601861067",
        created_at=CLEANING_MOMENT,
        amount_total=Decimal(amount),
        masters=[foreman] if foreman else [],
        rating_score=None,
        client_name="Ирина",
        address="Менделеева 15, кв 3",
        payment_method=payment,
        awaiting_wire_payment=False,
        service_kind="cleaning",
        specialist_enums=(OLGA_ENUM,),
    )


def make_engine(amo, store, *, dry_run=False):
    return Engine(amo=amo, store=store, specialists=SPECIALISTS, dry_run=dry_run,
                  now=lambda: CLEANING_MOMENT + timedelta(minutes=1))


def open_realization_lead(amo, lead_id=41463832_00, **extra):
    amo.add_lead(lead_id, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED,
                 created_at=int(CLEANING_MOMENT.timestamp()) - 86400, **extra)
    return lead_id


def fields_of(amo, field_id, call=0):
    _, payload = amo.calls_of("update_lead")[call]
    return [field for field in payload["custom_fields"] if field["field_id"] == field_id]


# --- «Услуга» и «Специалист» задаются источником, а не бригадиром ---

async def test_cleaning_sets_olga_and_cleaning_service_whoever_the_foreman_is():
    """Бригадира в справочнике амо нет — и это ничему не мешает."""
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)

    link = await make_engine(amo, store).process_order(make_cleaning())

    assert link.status == "done"
    assert fields_of(amo, ids.FIELD_SERVICE)[0]["values"] == [
        {"enum_id": ids.SERVICE_ENUM_CLEANING}]
    assert fields_of(amo, ids.FIELD_SPECIALIST)[0]["values"] == [{"enum_id": OLGA_ENUM}]


async def test_cleaning_deal_address_is_saved_to_the_link():
    """Задача 1 (ТЗ 2026-09-16): у уборки адрес сделки уходит в связку так же, как у заказа."""
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo, custom_fields_values=[
        {"field_id": ids.FIELD_ADDRESS, "values": [{"value": "Менделеева 15, кв 3"}]}])

    link = await make_engine(amo, store).process_order(make_cleaning())

    assert link.deal_address == "Менделеева 15, кв 3"


async def test_cleaning_already_done_reads_address_without_writing():
    """Задача 9 (ТЗ 2026-09-16): дыра из задачи 1 — путь done не заходил в
    _fill_lead — воспроизводится и у уборки. Чинится тем же местом в движке,
    отдельного кода для уборок нет.
    """
    amo, store = FakeAmo(), FakeStore()
    amo.add_lead(41463832_00, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS,
                 created_at=int(CLEANING_MOMENT.timestamp()) - 3600,
                 closed_at=int(CLEANING_MOMENT.timestamp()),
                 custom_fields_values=[
                     {"field_id": ids.FIELD_ADDRESS, "values": [{"value": "Менделеева 15, кв 3"}]}])

    link = await make_engine(amo, store).process_order(make_cleaning())

    assert link.status == "done" and link.path == "done" and link.real_lead_id == 41463832_00
    assert link.deal_address == "Менделеева 15, кв 3"
    assert not amo.calls_of("update_lead") and not amo.calls_of("move_lead")


async def test_overrides_beat_the_lookup_by_master_name():
    """Бригадира зовут Дмитрий — но это всё равно уборка, а не чистка мебели.

    Иначе робот вывел бы «Чистку мебели» по имени мастера и поставил бы
    Специалистом Козлова: значения из справочника подходят по имени и телефону.
    """
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)

    await make_engine(amo, store).process_order(
        make_cleaning(foreman=("Дмитрий Козлов", "79306858534")))

    assert fields_of(amo, ids.FIELD_SERVICE)[0]["values"] == [
        {"enum_id": ids.SERVICE_ENUM_CLEANING}]
    assert fields_of(amo, ids.FIELD_SPECIALIST)[0]["values"] == [{"enum_id": OLGA_ENUM}]


async def test_foreman_goes_into_the_note_not_into_the_specialist_field():
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)

    await make_engine(amo, store).process_order(make_cleaning())

    notes = [text for target, text in amo.calls_of("add_note") if target == lead_id] \
        or [text for _, text in amo.calls_of("add_note")]
    assert any("Лариса Иванова" in text for text in notes)
    assert any("Уборка №5" in text for text in notes)


# --- оплата ---

async def test_wire_cleaning_is_a_company_and_still_goes_all_the_way():
    """«Расчётный» = безнал и юрлицо. Но ждать оплату у уборки нечего.

    У химчистки безнал означает «счёт выставлен, деньги придут потом», и сделка
    останавливается на «Заказ выполнен». В клининге оплату вносят при проведении
    заказа, поэтому сделка доходит до «Успешно реализовано».
    """
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)

    await make_engine(amo, store).process_order(make_cleaning(payment="Расчётный"))

    assert fields_of(amo, ids.FIELD_PAYMENT_TYPE)[0]["values"] == [
        {"enum_id": ids.PAYMENT_ENUM_WIRE}]
    assert fields_of(amo, ids.FIELD_CLIENT_TYPE)[0]["values"] == [
        {"enum_id": ids.CLIENT_TYPE_COMPANY}]
    assert fields_of(amo, ids.FIELD_PAYMENT_DATE)                  # дата оплаты проставлена
    assert amo.leads[lead_id]["status_id"] == ids.STATUS_SUCCESS   # доведена до конца


async def test_gift_certificate_has_its_own_payment_value():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)

    await make_engine(amo, store).process_order(
        make_cleaning(payment="Подарочный сертификат"))

    assert fields_of(amo, ids.FIELD_PAYMENT_TYPE)[0]["values"] == [
        {"enum_id": ids.PAYMENT_ENUM_CERTIFICATE}]


async def test_card_payment_of_a_cleaning_is_a_transfer_to_the_card():
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)

    await make_engine(amo, store).process_order(make_cleaning(payment="Карта"))

    assert fields_of(amo, ids.FIELD_PAYMENT_TYPE)[0]["values"] == [
        {"enum_id": ids.PAYMENT_ENUM_CARD}]


async def test_wire_spelled_without_yo_is_still_a_wire():
    """«Расчетный» и «Расчётный» — один и тот же способ оплаты."""
    amo, store = FakeAmo(), FakeStore()
    open_realization_lead(amo)

    await make_engine(amo, store).process_order(make_cleaning(payment="Расчетный"))

    assert fields_of(amo, ids.FIELD_CLIENT_TYPE)[0]["values"] == [
        {"enum_id": ids.CLIENT_TYPE_COMPANY}]


# --- «Получить ОС»: оценки у клининга нет (решение владельца №9) ---

async def test_feedback_task_stays_open_for_a_cleaning():
    amo, store = FakeAmo(), FakeStore()
    lead_id = open_realization_lead(amo)
    amo.add_task(lead_id, 1, 2270740)                     # «Назначь мастера»
    amo.add_task(lead_id, 2, ids.TASK_TYPE_FEEDBACK)      # «Получить ОС»

    await make_engine(amo, store).process_order(make_cleaning())

    assert amo.calls_of("complete_task") == [1]


# --- новая сделка с нуля называется уборкой ---

async def test_a_new_lead_is_named_after_the_cleaning():
    amo, store = FakeAmo(), FakeStore()          # сделок у клиента нет вовсе

    await make_engine(amo, store).process_order(make_cleaning())

    assert amo.calls_of("create_lead")[0]["name"] == "Уборка №5"


# --- репетиция ---

async def test_rehearsal_writes_nowhere():
    """Репетиция считает решения, но не трогает ни CRM, ни базу.

    Хранилище — в памяти процесса (как его собирает main.py), клиент амо —
    репетиционный. После прохода в CRM не должно измениться ничего.
    """
    amo, store = FakeAmo(dry_run=True), MemoryLinkStore()
    lead_id = open_realization_lead(amo)
    before = dict(amo.leads[lead_id])

    link = await make_engine(amo, store, dry_run=True).process_order(make_cleaning())

    assert link.status == "done"                       # решение принято
    assert amo.leads[lead_id] == before                # а в CRM ничего не изменилось
    assert all(row["dry_run"] for row in store.actions)   # весь журнал — репетиционный
    assert store.links[5].real_lead_id == lead_id      # состояние осталось в памяти


# --- карточки владельцу ---

def test_card_is_signed_as_a_cleaning_and_keeps_its_own_buttons():
    """Ответ по «Уборке №5» не должен уйти в «Заказ №5»: номера пересекаются."""
    from adminbot.tg.cards import CHOICE_PREFIX, CLEANING_CHOICE_PREFIX, question_card

    text, keyboard = question_card(make_cleaning(), {
        "reason": "ask_owner",
        "options": [{"lead_id": 41400001, "pipeline_id": ids.PIPELINE_REALIZATION,
                     "date": "2026-09-10", "price": 9000, "name": None}],
    })
    data = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "Уборка №5" in text and "Заказ №5" not in text
    assert all(item.startswith(f"{CLEANING_CHOICE_PREFIX}:") for item in data)
    assert not any(item.startswith(f"{CHOICE_PREFIX}:") for item in data)


def test_a_choice_is_only_read_by_its_own_stream():
    from adminbot.tg.cards import CHOICE_PREFIX, CLEANING_CHOICE_PREFIX, parse_choice

    assert parse_choice("amoclean:5:41400001", CLEANING_CHOICE_PREFIX) == (5, "lead", 41400001)
    assert parse_choice("amoclean:5:new", CLEANING_CHOICE_PREFIX) == (5, "new", None)
    # чужая приставка — молчание, а не действие по чужой работе
    assert parse_choice("amoclean:5:new", CHOICE_PREFIX) is None
    assert parse_choice("amosync:5:new", CLEANING_CHOICE_PREFIX) is None


def test_done_message_says_it_was_a_cleaning():
    from adminbot.models import AmoLink
    from adminbot.tg.cards import order_done_text

    link = AmoLink(order_id=5, phone10="9601861067", status="done", path="A",
                   real_lead_id=31570695)

    text = order_done_text(make_cleaning(), link, base_url="https://x")

    assert text.startswith("✅ Уборка из бота")
    assert "Уборка №5" in text


def test_backlog_title_says_it_was_a_cleaning():
    from adminbot.sync.backlog import order_title

    assert order_title(make_cleaning()).startswith("Уборка №5")


# --- одна картина дня: заказы и уборки в общем сообщении ---

def test_evening_summary_keeps_both_streams_in_one_message():
    """Обновлено под ТЗ 2026-09-21-evening-summary-rework.md (задача 4): сводка
    больше не перечисляет проведённые заказы построчно — только числом
    (`processed_today`); хвост уборки при этом виден в общей картине дня."""
    from adminbot.sync.reconcile import DailySummary, SummaryRow
    from adminbot.tg.cards import summary_text

    summary = DailySummary(
        processed_today=1,
        cleaning=DailySummary(
            processed_today=1,
            waiting_owner=(SummaryRow(7, "waiting_owner"),),
        ),
    )

    text = summary_text(summary)

    assert text.count("📊 Вечерняя сверка") == 1        # одно сообщение, а не два
    assert "🧹 Уборки" in text
    assert "№7" in text                                  # хвост уборки — в общей картине
    assert "разбираться не с чем" not in text.lower()   # уборка ждёт ответа владельца


def test_a_quiet_day_stays_quiet_with_cleanings_too():
    from adminbot.sync.reconcile import DailySummary
    from adminbot.tg.cards import summary_text

    summary = DailySummary(
        processed_today=1,
        cleaning=DailySummary(processed_today=1),
    )

    assert "разбираться не с чем" in summary_text(summary).lower()


async def test_reconciler_puts_cleanings_into_the_same_summary():
    """Сверка проходит по уборкам сама и кладёт их в ту же сводку."""
    from adminbot.models import AmoLink
    from adminbot.sync.reconcile import OrderBrief, Reconciler, Snapshot

    class FakeWatcher:
        def __init__(self):
            self.ticks = 0

        async def tick(self):
            self.ticks += 1

    class FakeSource:
        def __init__(self, snapshot):
            self.snapshot = snapshot

        async def collect(self):
            return self.snapshot

    def snapshot(order_id):
        return Snapshot(
            links=(AmoLink(order_id=order_id, phone10="9601861067", status="done",
                           path="A", real_lead_id=41400001,
                           created_at=CLEANING_MOMENT, updated_at=CLEANING_MOMENT),),
            orders=(OrderBrief(order_id, "9601861067", CLEANING_MOMENT),),
        )

    sent = []
    cleaning_watcher = FakeWatcher()
    reconciler = Reconciler(
        watcher=FakeWatcher(), source=FakeSource(snapshot(581)),
        on_summary=lambda summary, **_numbers: _remember(sent, summary),
        cleaning_watcher=cleaning_watcher, cleaning_source=FakeSource(snapshot(5)),
        now=lambda: CLEANING_MOMENT,
    )

    summary = await reconciler.run_once()

    assert cleaning_watcher.ticks == 1                       # догоняющий проход по уборкам
    assert [row.order_id for row in summary.cleaning.processed] == [5]
    assert len(sent) == 1                                    # владельцу — одно сообщение


async def _remember(bucket, summary):
    bucket.append(summary)


# --- выключатели ---

async def test_status_names_the_cleaning_switch():
    from tests.test_tg_guard import FakeMessage, make_commands

    message = FakeMessage()
    await make_commands(cleaning_enabled=True, cleaning_dry_run=True).status(message)
    assert "Уборки: репетиция" in message.replies[0]

    message = FakeMessage()
    await make_commands(cleaning_enabled=True, cleaning_dry_run=False).status(message)
    assert "Уборки: БОЕВОЙ" in message.replies[0]

    message = FakeMessage()
    await make_commands().status(message)
    assert "Уборки: выключены" in message.replies[0]


def test_cleaning_switches_are_off_by_default(monkeypatch):
    """Включает владелец, а не выкладка кода."""
    from adminbot.config import Settings

    for name in ("CLEANING_SYNC_ENABLED", "CLEANING_SYNC_DRY_RUN", "CLEANING_BACKLOG_FROM"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (("ADMINBOT_TG_TOKEN", "1:x"), ("ADMINBOT_OWNER_TG_ID", "7"),
                        ("BOT_DB_DSN", "postgresql:///x"),
                        ("AMO_BASE_URL", "https://x"), ("AMO_TOKEN", "t")):
        monkeypatch.setenv(name, value)

    settings = Settings.from_env()

    assert settings.cleaning_sync_enabled is False
    assert settings.cleaning_sync_dry_run is True
    assert settings.cleaning_backlog_from is None      # без значения хвост не трогаем
