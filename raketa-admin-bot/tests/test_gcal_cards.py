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
    store, answers = answered

    await answers.on_choice(FakeCallback("gcal:keep"))

    link = await store.get("evt-1")
    assert link.status == "cancelled"
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
