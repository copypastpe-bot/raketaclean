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
from adminbot.models import AmoLink, Order
from adminbot.sync.backlog import PlannedOrder
from adminbot.sync.reconcile import DailySummary, SummaryRow
from adminbot.tg.cards import (
    ADDR_PREFIX, BACKLOG_GO, BACKLOG_HOLD, CLEANING_ADDR_PREFIX, address_missing_card,
    parse_address_choice, parse_choice, preview_card, question_card, summary_text,
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


# --- карточка «сделка без адреса» (ТЗ 2026-09-16, задача 7) ---

def make_link(order_id=596, phone10="9601861067", real_lead_id=41400001, primary_lead_id=None):
    return AmoLink(order_id=order_id, phone10=phone10, status="done", path="C",
                   real_lead_id=real_lead_id, primary_lead_id=primary_lead_id)


def test_address_missing_card_shows_order_phone_and_deal_link():
    text, keyboard = address_missing_card(make_link(), base_url="https://x.amocrm.ru",
                                          reminder_no=1)

    assert "Заказ №596" in text
    assert "+79601861067" in text
    assert "https://x.amocrm.ru/leads/detail/41400001" in text
    assert "Напоминание 1 из 7." in text

    labels = [button.text for button in buttons(keyboard)]
    assert labels == ["✅ Я заполнил", "🔕 Не напоминать"]
    data = [button.callback_data for button in buttons(keyboard)]
    assert data == [f"{ADDR_PREFIX}:596:filled", f"{ADDR_PREFIX}:596:mute"]


def test_address_missing_card_uses_the_primary_lead_when_no_realization_yet():
    link = make_link(real_lead_id=None, primary_lead_id=700)

    text, _ = address_missing_card(link, base_url="https://x.amocrm.ru", reminder_no=3, cap=7)

    assert "leads/detail/700" in text
    assert "Напоминание 3 из 7." in text


def test_address_missing_card_for_cleaning_uses_its_own_prefix_and_label():
    """Своя приставка кнопок — иначе ответ по уборке ушёл бы в заказ с тем же номером."""
    text, keyboard = address_missing_card(make_link(order_id=5), label="Уборка",
                                          prefix=CLEANING_ADDR_PREFIX,
                                          base_url="https://x.amocrm.ru", reminder_no=1)

    assert "Уборка №5" in text
    data = [button.callback_data for button in buttons(keyboard)]
    assert data == [f"{CLEANING_ADDR_PREFIX}:5:filled", f"{CLEANING_ADDR_PREFIX}:5:mute"]


def test_parse_address_choice_understands_both_buttons():
    assert parse_address_choice(f"{ADDR_PREFIX}:596:filled") == (596, "filled")
    assert parse_address_choice(f"{ADDR_PREFIX}:596:mute") == (596, "mute")
    assert parse_address_choice(f"{CLEANING_ADDR_PREFIX}:5:filled", CLEANING_ADDR_PREFIX) == (5, "filled")


def test_parse_address_choice_rejects_junk():
    for data in ("", "addr", "addr:596", "addr:596:lead_1", "другое:596:filled",
                 "addr:абв:filled", None):
        assert parse_address_choice(data) is None


# --- вечерняя сводка (ТЗ 2026-09-21-evening-summary-rework.md, задачи 3 и 4) ---

def summary_with(**fields):
    return DailySummary(**fields)


def test_summary_text_matches_the_approved_layout_for_a_quiet_day():
    """Тихий день: макет утверждён дословно — четыре числа, «Событий не было»
    по уборкам, «Хвостов нет» вместо списка."""
    summary = summary_with(processed_today=4, handed_to_owner=1, waiting_address=2,
                           cleaning=summary_with())

    text = summary_text(summary, calendar_created=3)

    assert text == (
        "📊 Вечерняя сверка\n\n"
        "Завёл из календаря: 3\n"
        "Провёл из бота: 4\n"
        "Передано администратору: 1\n"
        "Ждут адрес: 2\n\n"
        "🧹 Уборки\n"
        "Событий не было.\n\n"
        "Хвостов нет — разбираться не с чем."
    )


def test_summary_text_matches_the_approved_layout_with_tails():
    """День с хвостами: тот же верх, а вместо последней строки — блок «Хвосты»
    с четырьмя категориями (та же строка данных, что и в утверждённом макете)."""
    summary = summary_with(
        processed_today=4, handed_to_owner=1, waiting_address=2,
        waiting_owner=(
            SummaryRow(601, "waiting_owner", phone10="9519069162",
                      order_date=datetime(2026, 8, 31), lead_id=31570357),
            SummaryRow(605, "waiting_owner", phone10="9081559394",
                      order_date=datetime(2026, 9, 1), lead_id=31585279),
        ),
        failed=(
            SummaryRow(612, "error", phone10="9877568979",
                      order_date=datetime(2026, 9, 4), lead_id=31587009,
                      detail="амо ответила 504"),
        ),
        stale=(
            SummaryRow(613, "in_progress", phone10="9101451011",
                      order_date=datetime(2026, 9, 4), lead_id=31602355),
        ),
        cleaning=summary_with(),
    )

    text = summary_text(summary, calendar_created=3)

    assert text == (
        "📊 Вечерняя сверка\n\n"
        "Завёл из календаря: 3\n"
        "Провёл из бота: 4\n"
        "Передано администратору: 1\n"
        "Ждут адрес: 2\n\n"
        "🧹 Уборки\n"
        "Событий не было.\n\n"
        "⚠️ Хвосты\n\n"
        "❓ Ждут вашего ответа: 2\n"
        "   • №601 · +79519069162 · 31.08 · #31570357\n"
        "   • №605 · +79081559394 · 01.09 · #31585279\n\n"
        "⛔ Сбой робота: 1\n"
        "   • №612 · +79877568979 · 04.09 · #31587009\n"
        "     амо ответила 504\n\n"
        "⏳ Зависли дольше часа: 1\n"
        "   • №613 · +79101451011 · 04.09 · #31602355\n\n"
        "🕳 Не разобрано: 0"
    )


def test_summary_text_shows_cleaning_numbers_when_cleaning_moved_too():
    """Раздел уборок строится тем же кодом, что и заказы: формат одинаковый,
    данные свои (решение владельца) — не «Событий не было», раз было движение."""
    cleaning = summary_with(processed_today=2, waiting_address=1)
    summary = summary_with(processed_today=5, handed_to_owner=1, cleaning=cleaning)

    text = summary_text(summary)

    assert "🧹 Уборки\nЗавёл из календаря: 0\nПровёл из бота: 2\n" \
           "Передано администратору: 0\nЖдут адрес: 1" in text
    assert text.count("Провёл из бота:") == 2                # у каждого потока своё число


def test_summary_text_omits_cleaning_section_when_the_feature_is_off():
    """cleaning=None — функция уборок выключена, раздела в сообщении нет вовсе
    (отличать от cleaning с нулевыми полями, который печатает «Событий не было»)."""
    text = summary_text(summary_with(processed_today=1))

    assert "Уборки" not in text


def test_tails_merge_orders_and_cleaning_into_one_list():
    """Хвосты — одна картина дня и для заказов, и для уборок (то же правило,
    что уже держит is_quiet: оба потока считаются вместе)."""
    summary = summary_with(
        failed=(SummaryRow(700, "error", detail="боевая ошибка"),),
        cleaning=summary_with(failed=(SummaryRow(5, "error", detail="ошибка уборки"),)),
    )

    text = summary_text(summary)

    assert "⛔ Сбой робота: 2" in text
    assert "№700" in text and "№5" in text


def test_tails_block_caps_a_category_at_ten_and_counts_the_rest():
    rows = tuple(SummaryRow(600 + i, "waiting_owner") for i in range(12))
    summary = summary_with(waiting_owner=rows)

    text = summary_text(summary)

    assert text.count("• №6") == 10
    assert "…и ещё 2" in text


# --- ссылки на сделки в хвостах (задача 5, ТЗ 2026-09-21-evening-summary-rework.md) ---

def test_tail_row_lead_number_becomes_a_link_when_base_url_is_known():
    """Вид строки не меняется — меняется только то, что номер сделки становится
    кликабельным (уточнение координатора). Текст ссылки — `#<номер сделки>`."""
    summary = summary_with(waiting_owner=(
        SummaryRow(601, "waiting_owner", phone10="9519069162",
                  order_date=datetime(2026, 8, 31), lead_id=31570357),
    ))

    text = summary_text(summary, base_url="https://example.amocrm.ru")

    assert ('   • №601 · +79519069162 · 31.08 · '
           '<a href="https://example.amocrm.ru/leads/detail/31570357">#31570357</a>'
           in text)


def test_tail_row_stays_plain_without_a_base_url():
    """Без адреса CRM строка выглядит ровно как раньше — задача 5 ничего не ломает."""
    summary = summary_with(waiting_owner=(
        SummaryRow(601, "waiting_owner", phone10="9519069162",
                  order_date=datetime(2026, 8, 31), lead_id=31570357),
    ))

    text = summary_text(summary)

    assert "   • №601 · +79519069162 · 31.08 · #31570357" in text
    assert "<a href" not in text


def test_tail_detail_is_escaped_for_html_so_a_broken_pass_still_sends():
    """Разметка HTML включена для всего сообщения — текст ошибки экранируется,
    иначе символ вроде `<` сломал бы разбор сообщения в Telegram."""
    summary = summary_with(failed=(
        SummaryRow(612, "error", lead_id=31587009, detail="таймаут <5 сек>, амо & прокси"),
    ))

    text = summary_text(summary, base_url="https://example.amocrm.ru")

    assert "таймаут &lt;5 сек&gt;, амо &amp; прокси" in text


# --- сбой календарного прохода (задача 9, ТЗ 2026-09-21-evening-summary-rework.md) ---

def test_calendar_pass_failure_shows_up_as_a_tail_line():
    """Решение владельца 21.09: сбой календарного прохода — строка в хвостах,
    а не только запись в журнале сервера, которую он не видит."""
    summary = summary_with()

    text = summary_text(summary, calendar_failed=True)

    assert "⛔ Сбой: календарный проход не отработал" in text
    assert "Хвостов нет — разбираться не с чем." not in text    # день не тихий


def test_calendar_pass_success_shows_no_failure_line():
    summary = summary_with()

    text = summary_text(summary, calendar_created=3, calendar_failed=False)

    assert "Сбой: календарный проход" not in text


def test_calendar_disabled_is_not_a_failure():
    """Календарь выключен вовсе (умолчание calendar_failed=False) — не сбой,
    строки нет, а тихий день остаётся тихим."""
    summary = summary_with()

    text = summary_text(summary)

    assert "Сбой: календарный проход" not in text
    assert "Хвостов нет — разбираться не с чем." in text


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


def test_carpet_held_text_explains_why_and_what_to_do():
    """Письмо отложено: владельцу нужны причина и обе команды (задача 4, 16.09)."""
    from adminbot.tg.cards import carpet_held_text

    text = carpet_held_text("отчёт за август", "строк 120, порог 100", 120, 5, "uid-42")

    assert "отчёт за август" in text
    assert "строк 120, порог 100" in text
    assert "115 выполненных" in text                # 120 - 5 отказов
    assert "5 отказ" in text
    assert "--carpets-release=uid-42" in text
    assert "robot_amo" in text


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


# --- пометка репетиции (задача 3, 16.09) ---

def test_question_card_marks_rehearsal():
    """В репетиции CRM не тронута — карточка должна отличаться от боевой."""
    live_text, _ = question_card(make_order(), None)
    rehearsal, _ = question_card(make_order(), None, dry_run=True)

    assert not live_text.startswith("🎭")
    assert rehearsal.startswith("🎭 РЕПЕТИЦИЯ")
    assert rehearsal != live_text


def test_order_done_message_marks_rehearsal():
    from adminbot.models import AmoLink
    from adminbot.tg.cards import order_done_text

    link = AmoLink(order_id=596, phone10="9601861067", status="done", path="A",
                   real_lead_id=31570695)

    live_text = order_done_text(make_order(), link, base_url="https://x")
    rehearsal = order_done_text(make_order(), link, base_url="https://x", dry_run=True)

    assert not live_text.startswith("🎭")
    assert rehearsal.startswith("🎭 РЕПЕТИЦИЯ")


def test_carpet_cards_mark_rehearsal():
    from adminbot.carpets.watcher import CarpetTickReport
    from adminbot.tg.cards import carpet_held_text, carpet_question_card, carpet_report_text

    question_text, _ = carpet_question_card(carpet_row(), None, dry_run=True)
    assert question_text.startswith("🎭 РЕПЕТИЦИЯ")

    report = carpet_report_text("тема", CarpetTickReport(letters=1, processed=1),
                                dry_run=True)
    assert report.startswith("🎭 РЕПЕТИЦИЯ")

    held = carpet_held_text("тема", "порог превышен", 5, 1, "uid-1", dry_run=True)
    assert held.startswith("🎭 РЕПЕТИЦИЯ")

    # По умолчанию (боевой режим) пометки нет ни у одной карточки.
    live_question, _ = carpet_question_card(carpet_row(), None)
    assert not live_question.startswith("🎭")
