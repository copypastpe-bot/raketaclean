"""Карточки владельцу: вопрос с кнопками, вечерняя сводка, предпросмотр хвоста.

Здесь проверяется то, что владелец увидит своими глазами: понятный текст,
замаскированные телефоны (правило проекта по персональным данным) и кнопки,
по нажатию которых робот поймёт, что именно выбрали.
"""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import Order
from adminbot.sync.backlog import PlannedOrder
from adminbot.sync.reconcile import DailySummary, SummaryRow
from adminbot.tg.cards import (
    BACKLOG_GO, BACKLOG_HOLD, parse_choice, preview_card, question_card, summary_text,
)

ORDER_MOMENT = datetime(2026, 8, 24, 17, 53, tzinfo=MOSCOW_TZ)


def make_order(order_id=596):
    return Order(order_id=order_id, phone10="9601861067", created_at=ORDER_MOMENT,
                 amount_total=Decimal("5950"), client_name="Ирина",
                 masters=[("Дмитрий Козлов", "79306858534")])


def buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


# --- карточка-вопрос ---

def test_question_card_shows_order_and_choices():
    question = {"reason": "ask_owner", "options": [
        {"lead_id": 41400001, "pipeline_id": ids.PIPELINE_REALIZATION,
         "date": "2026-07-22", "price": 5000, "name": "Ниж! Матрас, Юлия"},
        {"lead_id": 41400002, "pipeline_id": ids.PIPELINE_REALIZATION,
         "date": "2026-07-24", "price": 6000, "name": None},
    ]}

    text, keyboard = question_card(make_order(), question)

    assert "Заказ №596" in text
    assert "Ирина" in text
    assert "5 950" in text                      # чек с разделителем разрядов
    assert "24.08.2026" in text                 # дата заказа с годом
    assert "какая из них" in text.lower()       # причина вопроса словами

    labels = [button.text for button in buttons(keyboard)]
    assert labels[:2] == ["Сделка 22.07 · 5 000 ₽", "Сделка 24.07 · 6 000 ₽"]
    assert "➕ Создать новую" in labels
    assert "✋ Сам разберусь" in labels

    data = [button.callback_data for button in buttons(keyboard)]
    assert data[:2] == ["amosync:596:41400001", "amosync:596:41400002"]
    assert "amosync:596:new" in data and "amosync:596:manual" in data


def test_question_card_shows_the_full_phone_to_the_owner():
    """Бот личный: владельцу нужен номер целиком, чтобы позвонить не заходя в CRM.

    В журналах сервера телефон по-прежнему маскируется — там читатель не один.
    """
    text, _ = question_card(make_order(), {"reason": "ask_owner", "options": []})

    assert "+79601861067" in text


def test_question_card_for_stale_leads_offers_a_new_deal():
    question = {"reason": "ask_owner_stale", "options": [
        {"lead_id": 41400003, "pipeline_id": ids.PIPELINE_PRIMARY,
         "date": "2026-05-14", "price": 3000, "name": "Лен! Ковролин"},
    ]}

    text, keyboard = question_card(make_order(), question)

    assert "свежих сделок нет" in text.lower()
    assert "amosync:596:new" in [button.callback_data for button in buttons(keyboard)]


def test_question_card_when_salesbot_is_silent_offers_a_retry():
    text, keyboard = question_card(make_order(), {"reason": "сейлзбот не создал автосделку"})

    assert "автосделку" in text
    assert "amosync:596:retry" in [button.callback_data for button in buttons(keyboard)]


def test_question_card_without_a_known_reason_still_works():
    text, keyboard = question_card(make_order(), None)

    assert "Заказ №596" in text
    assert buttons(keyboard)                    # кнопки есть всегда: тупика быть не должно


# --- разбор нажатия ---

def test_parse_choice_understands_every_button():
    assert parse_choice("amosync:596:41400001") == (596, "lead", 41400001)
    assert parse_choice("amosync:596:new") == (596, "new", None)
    assert parse_choice("amosync:596:manual") == (596, "manual", None)
    assert parse_choice("amosync:596:retry") == (596, "retry", None)


def test_parse_choice_rejects_junk():
    for data in ("", "amosync", "amosync:596", "другое:596:new", "amosync:абв:new", None):
        assert parse_choice(data) is None


# --- вечерняя сводка ---

def summary_with(**fields):
    return DailySummary(**fields)


def test_summary_lists_what_the_robot_did():
    summary = summary_with(
        processed=(SummaryRow(581, "done", "A", 41400001),
                   SummaryRow(585, "done", "B", 41400002)),
        created=(SummaryRow(590, "done", "C", 41400003),),
        already_done=(SummaryRow(591, "done", "done", 41400004),),
        total_orders=4,
    )

    text = summary_text(summary)

    assert "Проведено: 2" in text
    assert "№581" in text and "#41400001" in text
    assert "Создано новых сделок: 1" in text
    assert "провели сами" in text.lower()


def test_summary_puts_owner_business_first():
    """Главное для владельца — что требует его внимания, а не что прошло гладко."""
    summary = summary_with(
        processed=(SummaryRow(581, "done", "A", 41400001),),
        waiting_owner=(SummaryRow(596, "waiting_owner"),),
        stuck=(SummaryRow(593, "error", "A", 41400005, "AmoError: 502"),),
        missed=(SummaryRow(598, "missed", phone10="9601861067"),),
        total_orders=4,
    )

    text = summary_text(summary)

    assert text.index("№596") < text.index("№581")     # вопросы выше отчёта об успехах
    assert "AmoError: 502" in text
    assert "№598" in text
    assert "+79601861067" in text                      # телефон прямо в сводке


def test_summary_of_a_quiet_day():
    text = summary_text(summary_with(processed=(SummaryRow(581, "done", "A", 1),), total_orders=1))

    assert "разбираться не с чем" in text.lower()


# --- предпросмотр хвоста ---

def test_preview_lists_planned_actions_and_asks_for_a_go():
    plan = [
        PlannedOrder(order_id=582, title="Заказ №582 · Ирина …1067 · 5 950 ₽ · 24.08",
                     status="in_progress", path="A",
                     actions=(("update_lead", 41400001), ("move_lead", 41400001),
                              ("complete_task", 5001))),
        PlannedOrder(order_id=583, title="Заказ №583 · Юлия …6642 · 3 300 ₽ · 23.08",
                     status="waiting_owner", path=None, actions=(("ask_owner", None),)),
    ]

    text, keyboard = preview_card(plan)

    assert "2 заказ" in text
    assert "заполню сделку #41400001" in text
    assert "закрою задачу" in text
    assert "спрошу вас" in text
    assert "ничего не изменил" in text.lower()          # это ещё репетиция

    data = [button.callback_data for button in buttons(keyboard)]
    assert data == [BACKLOG_GO, BACKLOG_HOLD]


def test_preview_of_an_empty_backlog_has_no_go_button():
    text, keyboard = preview_card([])

    assert "нечего" in text.lower()
    assert keyboard is None


# --- ковры от партнёра ---

def carpet_row(partner_id=44426, **overrides):
    from decimal import Decimal as D
    from adminbot.carpets.report import CarpetRow
    values = dict(partner_id=partner_id, phone10="9601945325",
                  client_name="Толстая Светлана", district="Советский",
                  amount=D("3995"), return_date=date(2026, 8, 23),
                  added_date=date(2026, 8, 12))
    values.update(overrides)
    return CarpetRow(**values)


def test_carpet_question_card_shows_the_order_and_choices():
    from adminbot.tg.cards import carpet_question_card

    question = {"reason": "какая сделка про этот заказ", "options": [
        {"lead_id": 31516051, "pipeline_id": ids.PIPELINE_CARPETS,
         "date": "2026-08-12", "price": 1, "name": None},
    ]}

    text, keyboard = carpet_question_card(carpet_row(), question)

    assert "Ковры" in text
    assert "44426" in text                          # номер заказа партнёра
    assert "3 995" in text
    assert "+79601945325" in text                   # владельцу — номер целиком
    assert "23.08" in text                          # когда сдали ковры

    data = [button.callback_data for row_ in keyboard.inline_keyboard for button in row_]
    assert "carpet:44426:31516051" in data
    assert "carpet:44426:new" in data and "carpet:44426:manual" in data


def test_carpet_refusal_card_says_it_is_a_refusal():
    from adminbot.tg.cards import carpet_question_card

    text, _ = carpet_question_card(
        carpet_row(is_refusal=True, refusal_reason="Не взяли трубку"), None)

    assert "отказ" in text.lower()
    assert "Не взяли трубку" in text


def test_parse_carpet_choice():
    from adminbot.tg.cards import parse_carpet_choice

    assert parse_carpet_choice("carpet:44426:31516051") == (44426, "lead", 31516051)
    assert parse_carpet_choice("carpet:44426:new") == (44426, "new", None)
    assert parse_carpet_choice("carpet:44426:manual") == (44426, "manual", None)
    assert parse_carpet_choice("amosync:596:new") is None       # чужая кнопка
    assert parse_carpet_choice("мусор") is None


def test_carpet_report_text_sums_up_a_letter():
    from adminbot.carpets.watcher import CarpetTickReport
    from adminbot.tg.cards import carpet_report_text

    text = carpet_report_text("отчёт с 17.08 по 23.08", CarpetTickReport(
        letters=1, processed=5,
        by_status={"done": 3, "waiting_owner": 1, "waiting_salesbot": 1}))

    assert "отчёт с 17.08 по 23.08" in text
    assert "Проведено: 3" in text
    assert "ждут вашего ответа: 1" in text.lower()
    assert "1" in text


def test_carpet_report_says_when_all_is_clean():
    from adminbot.carpets.watcher import CarpetTickReport
    from adminbot.tg.cards import carpet_report_text

    text = carpet_report_text("свод за август", CarpetTickReport(
        letters=1, processed=4, by_status={"done": 4}))

    assert "разбираться не с чем" in text.lower()


def test_order_done_message_for_the_week_of_watching():
    """О каждом проведённом заказе — сообщение со ссылкой (решение 2026-08-27)."""
    from adminbot.tg.cards import order_done_text
    from adminbot.models import AmoLink
    from tests.test_watcher import make_order

    order = make_order(591)
    link = AmoLink(order_id=591, phone10=order.phone10, status="done", path="A",
                   real_lead_id=31570695)

    text = order_done_text(order, link, base_url="https://raketacleancrm.amocrm.ru")

    assert "Заказ №591" in text
    assert "+7" in text                                  # телефон целиком
    assert "https://raketacleancrm.amocrm.ru/leads/detail/31570695" in text
    assert "взял" in text.lower()


def test_order_done_message_tells_a_new_deal_from_an_old_one():
    from adminbot.tg.cards import order_done_text
    from adminbot.models import AmoLink
    from tests.test_watcher import make_order

    created = AmoLink(order_id=592, phone10="9601861067", status="done", path="C",
                      real_lead_id=41400009)
    by_owner = AmoLink(order_id=593, phone10="9601861067", status="done", path="done",
                       real_lead_id=41400010)

    assert "создал" in order_done_text(make_order(592), created,
                                       base_url="https://x").lower()
    assert "вы" in order_done_text(make_order(593), by_owner,
                                   base_url="https://x").lower()


def test_order_line_prints_moscow_time_not_utc():
    """В базе бота время лежит в UTC; владельцу нужно московское (задача 2, 16.09)."""
    utc_order = replace(make_order(),
                         created_at=datetime(2026, 9, 16, 10, 30, tzinfo=timezone.utc))

    text, _ = question_card(utc_order, None)

    assert "16.09.2026 13:30" in text
    assert "10:30" not in text
