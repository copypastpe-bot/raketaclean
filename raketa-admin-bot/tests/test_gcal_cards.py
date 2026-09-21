"""Карточки календаря и ответы владельца.

Две вещи проверяются особо:

- **телефон и дата показываются целиком** — бот личный, и владельцу нужно
  позвонить клиенту, не открывая CRM (его решение 2026-08-26);
- **ответ владельца не идёт в amoCRM напрямую**: он записывается рядом с
  записью, а работу доделает обычный проход. Поэтому нажатие не потеряется,
  даже если робота перезапустят сразу после него.
"""

from datetime import date

import pytest

from adminbot.gcal.store import MemoryCalendarStore
from adminbot.tg.calendar_cards import (
    boat_card, calendar_question_card, cancellation_card, parse_calendar_choice,
)
from adminbot.tg.bot import CalendarAnswers


def a_link(**overrides):
    from adminbot.models import CalendarLink

    fields = dict(event_id="evt-1", kind="order", status="waiting_owner",
                  phone10="9605379757", order_date=date(2026, 8, 27),
                  client_name="Юлия", district="советский", services=("mattress",),
                  real_lead_id=41400001,
                  question={"reason": "заказ отменён — закрыть сделку?",
                            "lead_id": 41400001})
    fields.update(overrides)
    return CalendarLink(**fields)


# --- карточки ---

def test_cancellation_card_says_what_happened():
    text, keyboard = cancellation_card(a_link())

    assert "отмен" in text.lower()
    assert "+79605379757" in text                      # телефон целиком
    assert "27.08" in text                             # и дата заказа
    assert "Юлия" in text
    assert "матрас" in text.lower()
    assert "#41400001" in text
    buttons = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("Закрыть" in b for b in buttons)
    assert any("Оставить" in b for b in buttons)


def test_boat_card_offers_to_create():
    link = a_link(kind="boat", client_name="Толстой", phone10=None, real_lead_id=None,
                  question={"reason": "теплоход — завести сделку?",
                            "boat": "Толстой", "when": "12.09 09:00"})

    text, keyboard = boat_card(link)

    assert "Толстой" in text
    assert "12.09" in text
    buttons = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("Завести" in b for b in buttons)
    assert any("Пропустить" in b for b in buttons)


def test_question_card_lists_candidate_deals():
    link = a_link(question={"reason": "ask_owner", "options": [
        {"lead_id": 41400001, "pipeline_id": 4482787, "date": "2026-08-27"},
        {"lead_id": 41400002, "pipeline_id": 4482751, "date": None},
    ]})

    text, keyboard = calendar_question_card(link)

    assert "несколько" in text.lower()
    buttons = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "Сделка #41400001 · 27.08" in buttons     # дата помогает выбрать
    assert "Сделка #41400002" in buttons
    assert any("Сам разберусь" in b for b in buttons)


def test_an_old_deal_shows_its_year_on_the_button():
    """Сделка не этого года — на кнопке год обязателен.

    2026-09-02 владелец увидел «Сделка #29174771 · 11.09» и принял её за свежую,
    хотя это сентябрь 2024-го.
    """
    link = a_link(question={"reason": "ask_owner_stale", "options": [
        {"lead_id": 29174771, "pipeline_id": 4482787, "date": "2024-09-11"},
    ]})

    _text, keyboard = calendar_question_card(link)

    buttons = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "Сделка #29174771 · 11.09.2024" in buttons


def test_choice_is_short_enough_for_telegram():
    """В кнопке нельзя везти длинный id записи Google — упрёмся в лимит.

    Поэтому запись находится по сообщению, на кнопку которого нажали.
    """
    _, keyboard = cancellation_card(a_link())

    for row in keyboard.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


def test_junk_callbacks_are_ignored():
    assert parse_calendar_choice(None) is None
    assert parse_calendar_choice("amosync:1:new") is None
    assert parse_calendar_choice("gcal:close") == "close"


# --- ответы владельца ---

@pytest.fixture
async def answered():
    store = MemoryCalendarStore()
    await store.create("evt-1", kind="order", phone10="9605379757")
    await store.update("evt-1", status="waiting_owner", real_lead_id=41400001,
                       question_msg_id=555,
                       question={"reason": "заказ отменён — закрыть сделку?"})
    answers = CalendarAnswers(owner_tg_id=190933209, store=store)
    return store, answers


class FakeCallback:
    def __init__(self, data, *, user_id=190933209, message_id=555):
        self.data = data
        self.from_user = type("User", (), {"id": user_id})()
        self.answered = None
        self.edited = None
        outer = self

        class Message:
            def __init__(self):
                self.message_id = message_id

            async def edit_text(self, text):
                outer.edited = text

        self.message = Message()

    async def answer(self, text=None):
        self.answered = text or ""


async def test_owner_confirms_closing(answered):
    store, answers = answered

    await answers.on_choice(FakeCallback("gcal:close"))

    link = await store.get("evt-1")
    assert link.status == "closing"                    # закроет обычный проход
    assert link.question is None


async def test_owner_keeps_the_deal(answered):
    """«Оставить как есть» — сначала пометка сделки, отмена записи потом.

    Кнопка ставит `marking`, а галочку «заказ ведёт владелец» и итоговый
    `cancelled` доделывает проход движка: решение владельца должно пережить
    перезапуск робота, как и закрытие сделки.
    """
    store, answers = answered

    await answers.on_choice(FakeCallback("gcal:keep"))

    link = await store.get("evt-1")
    assert link.status == "marking"
    assert "остав" in (link.skip_reason or "")


async def test_stranger_gets_nothing(answered):
    """Бот личный: чужие нажатия игнорируются молча."""
    store, answers = answered

    callback = FakeCallback("gcal:close", user_id=1)
    await answers.on_choice(callback)

    assert callback.answered is None
    assert (await store.get("evt-1")).status == "waiting_owner"


async def test_boat_is_created_only_after_the_button(answered):
    store, answers = answered
    await store.update("evt-1", kind="boat", status="waiting_owner",
                       question={"reason": "теплоход — завести сделку?"})

    await answers.on_choice(FakeCallback("gcal:boat_create"))

    link = await store.get("evt-1")
    assert link.status == "new"
    assert link.path == "BOAT"


# --- тексты для владельца ---

def test_calendar_summary_speaks_plainly():
    """В сводке нет внутренних слов робота: только то, что владельцу решать."""
    from adminbot.gcal.watcher import CalendarTickReport
    from adminbot.tg.calendar_cards import calendar_summary_text

    report = CalendarTickReport(changes=7, processed=5,
                                by_status={"done": 3, "waiting_salesbot": 1,
                                           "skipped": 1, "waiting_owner": 1},
                                questions=("evt-1",),
                                unknown_districts=("печер", "цветы"))

    text = calendar_summary_text(report, counts={"done": 12, "cancelled": 1})

    assert "календар" in text.lower()
    assert "waiting_salesbot" not in text
    assert "Печер" in text or "печер" in text        # непонятные приставки названы
    assert "3" in text


def test_quiet_day_says_so():
    from adminbot.gcal.watcher import CalendarTickReport
    from adminbot.tg.calendar_cards import calendar_summary_text

    text = calendar_summary_text(CalendarTickReport(), counts={})

    assert "новых записей" in text.lower() or "ничего" in text.lower()


def test_status_shows_mode_and_last_exchange():
    from adminbot.gcal.watcher import CalendarTickReport
    from adminbot.tg.calendar_cards import calendar_status_text

    text = calendar_status_text(enabled=True, dry_run=True,
                                report=CalendarTickReport(changes=2, processed=2))

    assert "репетиция" in text.lower()
    assert "выключен" not in text.lower()

    off = calendar_status_text(enabled=False, dry_run=False, report=None)
    assert "выключен" in off.lower()


def test_status_lists_every_calendar_when_there_are_several():
    """Владелец должен видеть, что календарь бригадира опрашивается, а не молчит."""
    from adminbot.gcal.watcher import CalendarTickReport
    from adminbot.tg.calendar_cards import calendar_status_text

    text = calendar_status_text(
        enabled=True, dry_run=False,
        report=CalendarTickReport(changes=3, processed=3,
                                  by_calendar={"raketaclean52@gmail.com": 2,
                                               "brigade@group.calendar.google.com": 1}))

    assert "raketaclean52@gmail.com" in text
    assert "brigade@group.calendar.google.com" in text


def test_status_names_a_calendar_google_did_not_answer():
    """Молчащий календарь — не «всё хорошо, изменений нет»: это надо сказать."""
    from adminbot.gcal.watcher import CalendarTickReport
    from adminbot.tg.calendar_cards import calendar_status_text

    text = calendar_status_text(
        enabled=True, dry_run=False,
        report=CalendarTickReport(
            changes=1, processed=1,
            by_calendar={"brigade@group.calendar.google.com": 1},
            calendars_failed=(("raketaclean52@gmail.com", "GCalError: 503"),)))

    assert "raketaclean52@gmail.com" in text
    assert "не ответил" in text.lower() or "не удался" in text.lower()


def test_status_stays_short_with_a_single_calendar():
    """Пока календарь один, лишних строк в /status быть не должно."""
    from adminbot.gcal.watcher import CalendarTickReport
    from adminbot.tg.calendar_cards import calendar_status_text

    text = calendar_status_text(
        enabled=True, dry_run=False,
        report=CalendarTickReport(changes=1, processed=1,
                                  by_calendar={"raketaclean52@gmail.com": 1}))

    assert "raketaclean52@gmail.com" not in text


# --- отчёт репетиции ---

def test_rehearsal_card_tells_what_would_happen():
    """В репетиции робот обязан рассказывать даже об уверенных решениях.

    Иначе прогон бесполезен: владелец видит только вопросы, а как раз вопросов
    робот в хорошем случае и не задаёт.
    """
    from adminbot.tg.calendar_cards import rehearsal_text

    link = a_link(status="done", question=None, path="C", district="борский",
                  client_name="Светлана", order_date=date(2026, 8, 31),
                  services=("furniture",))
    actions = [
        {"action": "create_contact", "amo_id": None},
        {"action": "create_lead", "amo_id": 41400009},
        {"action": "move_lead", "amo_id": 41400009},
    ]

    text = rehearsal_text(link, actions)

    assert "репетиц" in text.lower()
    assert "Светлана" in text and "31.08" in text
    assert "+79605379757" in text                 # телефон целиком — бот личный
    assert "мебель" in text.lower() and "Борский" in text
    assert "создам контакт" in text.lower()
    assert "создам сделку" in text.lower()
    assert "в CRM ничего не менял" in text or "не менял" in text


def test_rehearsal_card_is_honest_about_doing_nothing():
    """Запись пропущена — так и говорим, без выдуманной работы."""
    from adminbot.tg.calendar_cards import rehearsal_text

    link = a_link(status="skipped", question=None,
                  skip_reason="перемыв по гарантии — сделка не нужна")

    text = rehearsal_text(link, [])

    assert "перемыв" in text.lower()
    assert "создам" not in text.lower()


# --- сообщения о сделанной работе (неделя наблюдения) ---

def test_done_message_says_what_happened_and_gives_the_link():
    """Владелец проверяет работу по горячим следам: что сделал и куда смотреть."""
    from adminbot.tg.calendar_cards import done_text

    link = a_link(status="done", question=None, path="B", real_lead_id=31570695,
                  client_name="Милена", order_date=date(2026, 8, 28),
                  services=("mattress",), district="нижегородский")
    actions = [{"action": "update_lead", "amo_id": 31570689},
               {"action": "move_lead", "amo_id": 31570689},
               {"action": "update_lead", "amo_id": 31570695}]

    text = done_text(link, actions, base_url="https://raketacleancrm.amocrm.ru")

    assert "Календарь" in text
    assert "Милена" in text and "28.08" in text
    assert "матрас" in text.lower()
    assert "https://raketacleancrm.amocrm.ru/leads/detail/31570695" in text
    assert "взял" in text.lower()                    # существующую сделку, не создал


def test_done_message_distinguishes_a_new_deal():
    from adminbot.tg.calendar_cards import done_text

    link = a_link(status="done", question=None, path="C", real_lead_id=41400009)
    actions = [{"action": "create_contact", "amo_id": None},
               {"action": "create_lead", "amo_id": 41400007}]

    text = done_text(link, actions, base_url="https://raketacleancrm.amocrm.ru")

    assert "создал" in text.lower()
    assert "новый контакт" in text.lower()           # клиента в CRM не было


def test_done_message_warns_about_a_forgotten_deal():
    """Робот завёл новую сделку, но у клиента висит незакрытая старая.

    Он про неё молчать не должен: раньше такая сделка стоила владельцу вопроса,
    теперь — одной строки в отчёте, по которой её можно найти и закрыть.
    """
    from adminbot.tg.calendar_cards import done_text

    link = a_link(status="done", question=None, path="C", real_lead_id=41400009,
                  client_name="Елизавета")
    actions = [{"action": "note_forgotten",
                "payload": {"lead_ids": [29174771], "dates": {"29174771": "2024-09-11"}}},
               {"action": "create_lead", "amo_id": 41400009}]

    text = done_text(link, actions, base_url="https://raketacleancrm.amocrm.ru")

    assert "29174771" in text
    assert "11.09.2024" in text                       # видно, насколько она старая
    assert "закрыть" in text.lower()                  # что с ней делать


def test_done_message_stays_short_without_forgotten_deals():
    """Обычный случай — никаких лишних строк про CRM."""
    from adminbot.tg.calendar_cards import done_text

    link = a_link(status="done", question=None, path="C", real_lead_id=41400009)
    text = done_text(link, [{"action": "create_lead", "amo_id": 41400009}],
                     base_url="https://raketacleancrm.amocrm.ru")

    assert "незакрыт" not in text.lower()


def test_changed_record_message_lists_what_was_updated():
    """Запись поправили после проведения — говорим, что именно подтянули."""
    from adminbot.tg.calendar_cards import updated_text

    link = a_link(status="done", question=None, real_lead_id=31570695,
                  client_name="Милена")

    text = updated_text(link, ("адрес", "комментарий"),
                        base_url="https://raketacleancrm.amocrm.ru")

    assert "изменил" in text.lower() or "поправил" in text.lower()
    assert "адрес" in text and "комментарий" in text
    assert "/leads/detail/31570695" in text


def test_closed_deal_question_offers_to_confirm_not_to_work():
    """По закрытой сделке робот предлагает признать её, а не работать с ней."""
    link = a_link(question={"reason": "ask_owner_closed", "options": [
        {"lead_id": 31570357, "pipeline_id": 4482787, "date": "2026-08-27"}]})

    text, keyboard = calendar_question_card(link)

    assert "закрыт" in text.lower() and "сами" in text.lower()
    buttons = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("Это она" in b for b in buttons)
    assert any("Создать новую" in b for b in buttons)


async def test_owner_confirms_the_closed_deal(answered):
    store, answers = answered
    await store.update("evt-1", status="waiting_owner", real_lead_id=None,
                       question={"reason": "ask_owner_closed",
                                 "options": [{"lead_id": 31570357,
                                              "pipeline_id": 4482787}]})

    await answers.on_choice(FakeCallback("gcal:linked_31570357"))

    link = await store.get("evt-1")
    assert link.status == "done"
    assert link.real_lead_id == 31570357
    assert link.path == "done"                   # работать по ней робот не будет


# --- решения владельца пишутся в журнал (задача 8, ТЗ 2026-09-21) ---

async def test_owner_takes_it_over_is_logged_as_manual(answered):
    store, answers = answered

    await answers.on_choice(FakeCallback("gcal:manual"))

    assert store.actions_of("answer_owner") == [
        {"event_id": "evt-1", "action": "answer_owner", "dry_run": False,
         "entity": None, "amo_id": None, "payload": {"choice": "manual"}}]


async def test_owner_keeps_the_deal_is_logged_with_a_different_choice(answered):
    """«Оставить как есть» тоже попадает в журнал — но не как «manual»."""
    store, answers = answered

    await answers.on_choice(FakeCallback("gcal:keep"))

    assert store.actions_of("answer_owner") == [
        {"event_id": "evt-1", "action": "answer_owner", "dry_run": False,
         "entity": None, "amo_id": None, "payload": {"choice": "keep"}}]


async def test_confirming_the_closed_deal_logs_its_lead_id(answered):
    store, answers = answered
    await store.update("evt-1", status="waiting_owner", real_lead_id=None,
                       question={"reason": "ask_owner_closed",
                                 "options": [{"lead_id": 31570357,
                                              "pipeline_id": 4482787}]})

    await answers.on_choice(FakeCallback("gcal:linked_31570357"))

    assert store.actions_of("answer_owner") == [
        {"event_id": "evt-1", "action": "answer_owner", "dry_run": False,
         "entity": "lead", "amo_id": 31570357, "payload": {"choice": "linked_31570357"}}]


async def test_stranger_writes_nothing_to_the_journal(answered):
    store, answers = answered

    await answers.on_choice(FakeCallback("gcal:manual", user_id=1))

    assert store.actions == []


async def test_owner_choice_does_not_duplicate_the_calendar_journal_entry():
    """Кнопка пишет `answer_owner`, движок отдельно — `update_lead»: проверяем,
    что решение владельца не задваивается и не путается с записью движка."""
    from adminbot.amo import ids
    from adminbot.gcal.engine import CalendarEngine
    from adminbot.gcal.event import EventKind, ParsedEvent
    from tests.fakes import FakeAmo

    amo = FakeAmo()
    amo.add_lead(41400001, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)
    store = MemoryCalendarStore()
    await store.create("evt-1", kind="order", phone10="9605379757")
    await store.update("evt-1", status="waiting_owner", real_lead_id=41400001,
                       question_msg_id=555,
                       question={"reason": "заказ отменён — закрыть сделку?"})
    answers = CalendarAnswers(owner_tg_id=190933209, store=store)

    await answers.on_choice(FakeCallback("gcal:manual"))

    engine = CalendarEngine(amo=amo, store=store, dry_run=False)
    await engine.process(ParsedEvent(event_id="evt-1", kind=EventKind.CANCELLED))

    assert len(store.actions_of("answer_owner")) == 1
    assert len(store.actions_of("update_lead")) == 1


# --- пометка репетиции (задача 3, 16.09) ---

def test_calendar_question_cards_mark_rehearsal():
    """В репетиции CRM не тронута — карточки-вопросы должны отличаться от боевых."""
    link = a_link()

    live_text, _ = cancellation_card(link)
    rehearsal, _ = cancellation_card(link, dry_run=True)
    assert not live_text.startswith("🎭")
    assert rehearsal.startswith("🎭 РЕПЕТИЦИЯ")

    boat_link = a_link(kind="boat", client_name="Толстой", phone10=None, real_lead_id=None,
                       question={"reason": "теплоход — завести сделку?",
                                 "boat": "Толстой", "when": "12.09 09:00"})
    rehearsal_boat, _ = boat_card(boat_link, dry_run=True)
    assert rehearsal_boat.startswith("🎭 РЕПЕТИЦИЯ")

    question_link = a_link(question={"reason": "ask_owner", "options": []})
    rehearsal_question, _ = calendar_question_card(question_link, dry_run=True)
    assert rehearsal_question.startswith("🎭 РЕПЕТИЦИЯ")


def test_updated_record_message_marks_rehearsal():
    from adminbot.tg.calendar_cards import updated_text

    link = a_link(status="done", question=None, real_lead_id=31570695, client_name="Милена")

    live_text = updated_text(link, ("адрес",), base_url="https://x")
    rehearsal = updated_text(link, ("адрес",), base_url="https://x", dry_run=True)

    assert not live_text.startswith("🎭")
    assert rehearsal.startswith("🎭 РЕПЕТИЦИЯ")
