"""Правила матчера, уточнённые владельцем 2026-08-25 (после первого экзамена).

Разбор случаев и решения — в `docs/plans/history-exam-result.md`.
Границы окон взяты из измерений по 90 дням истории, а не назначены на глаз.
"""

from datetime import date, timedelta
from decimal import Decimal

from adminbot.sync.matcher import STALE_LEAD_DAYS, Decision, LeadInfo, match

REAL, PRIM = 4482787, 4482751
SUCCESS, CLOSED = 142, 143
# этапы первичной воронки
UNSORTED_PRIM, NEW_LEAD, DIALOG = 41463532, 41463535, 41463544
# этапы воронки реализации
CREATED, CONFIRMED, DONE = 41463832, 41463838, 41463964
CARPETS = 4645519

# «Специалист» в амо: enum-значения действующих мастеров
KOZLOV, POLOZOV, SKOROPASHKINA = 951507, 951505, 952251

ORDER_DAY = date(2026, 8, 20)


def L(id, pipeline, status, *, order_date=None, closed_date=None, created_date=None,
      specialists=(), price=None):
    return LeadInfo(
        lead_id=id, pipeline_id=pipeline, status_id=status,
        order_date=order_date, closed_date=closed_date,
        created_date=created_date or ORDER_DAY - timedelta(days=1),   # по умолчанию свежая
        specialist_ids=tuple(specialists), price=price,
    )


# --- «Специалист»: уборка и химчистка одному клиенту в один день (заказ №575) ---

def test_specialist_separates_cleaning_from_furniture():
    """Две открытые сделки на одну дату: уборка у Ольги, химчистка у Дмитрия."""
    d = match(
        order_date=ORDER_DAY,
        candidates=[
            L(31527193, REAL, CREATED, order_date=ORDER_DAY, specialists=[SKOROPASHKINA]),
            L(31527209, REAL, CONFIRMED, order_date=ORDER_DAY, specialists=[KOZLOV]),
        ],
        master_specialist_ids=(KOZLOV,),
    )
    assert d == Decision(kind="use_realization", lead_id=31527209)


def test_specialist_ignored_when_master_unknown():
    """Мастера заказа нет в списке специалистов амо — признак просто не применяется."""
    d = match(
        order_date=ORDER_DAY,
        candidates=[
            L(1, REAL, CREATED, order_date=ORDER_DAY, specialists=[SKOROPASHKINA]),
            L(2, REAL, CONFIRMED, order_date=ORDER_DAY, specialists=[KOZLOV]),
        ],
    )
    assert d.kind == "ask_owner" and set(d.options) == {1, 2}


def test_no_specialist_match_means_the_hint_is_useless():
    """Нашего мастера нет ни в одной сделке — признак не сужает выбор, спрашиваем.

    Отбросить сделку по чужому мастеру нельзя: поле показывает планируемого
    мастера, а не поехавшего (см. заказы №418 и №445).
    """
    d = match(
        order_date=ORDER_DAY,
        candidates=[
            L(1, REAL, CREATED, order_date=ORDER_DAY, specialists=[SKOROPASHKINA]),
            L(2, REAL, CONFIRMED, order_date=ORDER_DAY, specialists=[]),
        ],
        master_specialist_ids=(POLOZOV,),
    )
    assert d.kind == "ask_owner" and set(d.options) == {1, 2}


def test_specialist_is_who_was_planned_not_who_went():
    """Заказы №418 и №445: в сделке один мастер, выполнил другой — сделка верная.

    Поле «Специалист» заполняют при планировании и не переписывают при замене.
    Поэтому чужой мастер НЕ отбрасывает сделку.
    """
    d = match(
        order_date=ORDER_DAY,
        candidates=[L(31271495, REAL, CREATED, order_date=ORDER_DAY, specialists=[KOZLOV])],
        master_specialist_ids=(POLOZOV,),
    )
    assert d == Decision(kind="use_realization", lead_id=31271495)


def test_matching_specialist_still_wins_when_there_is_a_choice():
    """Совпадение по мастеру — сильный довод: из нескольких берём сделку нашего."""
    d = match(
        order_date=ORDER_DAY,
        candidates=[
            L(1, REAL, CREATED, order_date=ORDER_DAY, specialists=[POLOZOV]),
            L(2, REAL, CREATED, order_date=ORDER_DAY, specialists=[]),
            L(3, REAL, CREATED, order_date=ORDER_DAY, specialists=[KOZLOV]),
        ],
        master_specialist_ids=(KOZLOV,),
    )
    assert d == Decision(kind="use_realization", lead_id=3)


def test_leftover_deal_of_another_master_is_caught_by_age():
    """Заказ №548: сделка Полозова с прошлого раза, 16 дней — это хвост.

    Отсеивается не по мастеру (поле ненадёжно), а по возрасту сделки.
    """
    d = match(
        order_date=ORDER_DAY,
        candidates=[L(31448601, REAL, CREATED,
                      created_date=ORDER_DAY - timedelta(days=16), specialists=[POLOZOV])],
        master_specialist_ids=(KOZLOV,),
    )
    assert d.kind == "ask_owner_stale" and d.options == (31448601,)


# --- сделка, закрытая до выполнения заказа, не может быть нашей (заказ №570) ---

def test_deal_closed_before_the_job_is_not_ours():
    """Заказ 17.08, сделка закрыта 04.08 — работа тогда ещё не была сделана."""
    d = match(order_date=date(2026, 8, 17), candidates=[
        L(31489155, REAL, SUCCESS, order_date=date(2026, 8, 3),
          closed_date=date(2026, 8, 4), created_date=date(2026, 8, 3))])
    assert d.kind == "create_new"


def test_deal_closed_on_the_order_day_is_ours():
    """Владелец успел провести сделку в тот же день — это наш заказ."""
    d = match(order_date=ORDER_DAY, candidates=[
        L(1, REAL, SUCCESS, order_date=ORDER_DAY, closed_date=ORDER_DAY)])
    assert d == Decision(kind="already_done", lead_id=1)


def test_deal_closed_a_day_before_is_still_ours():
    """Мастер закрыл заказ в боте на следующий день после проведения сделки."""
    d = match(order_date=ORDER_DAY, candidates=[
        L(1, REAL, SUCCESS, order_date=ORDER_DAY, closed_date=ORDER_DAY - timedelta(days=1))])
    assert d == Decision(kind="already_done", lead_id=1)


def test_two_deals_same_master_still_ask():
    """Один мастер, два заказа одного клиента — признак не помогает, спрашиваем."""
    d = match(
        order_date=ORDER_DAY,
        candidates=[
            L(1, REAL, CREATED, order_date=ORDER_DAY, specialists=[KOZLOV]),
            L(2, REAL, CREATED, order_date=ORDER_DAY, specialists=[KOZLOV]),
        ],
        master_specialist_ids=(KOZLOV,),
    )
    assert d.kind == "ask_owner" and set(d.options) == {1, 2}


# --- первичная воронка: «Новый лид» важнее «Неразобранного» (заказ №583) ---

def test_new_lead_wins_over_unsorted():
    """Клиент звонил дважды: первый раз не дозвонился, второй — трубку подняли."""
    d = match(order_date=ORDER_DAY, candidates=[
        L(31547905, PRIM, UNSORTED_PRIM),
        L(31548023, PRIM, NEW_LEAD)])
    assert d.kind == "use_primary" and d.lead_id == 31548023
    assert d.duplicates == (31547905,)      # второму лиду робот напишет комментарий


def test_unsorted_used_when_nothing_else():
    """Оператор перезвонил и принял заказ, сделка так и осталась в «Неразобранном»."""
    d = match(order_date=ORDER_DAY, candidates=[L(500, PRIM, UNSORTED_PRIM)])
    assert d == Decision(kind="use_primary", lead_id=500)


def test_later_primary_stage_also_wins_over_unsorted():
    d = match(order_date=ORDER_DAY, candidates=[
        L(600, PRIM, UNSORTED_PRIM), L(601, PRIM, DIALOG)])
    assert d.lead_id == 601 and d.duplicates == (600,)


def test_two_unsorted_leads_ask_owner():
    d = match(order_date=ORDER_DAY, candidates=[
        L(700, PRIM, UNSORTED_PRIM), L(701, PRIM, UNSORTED_PRIM)])
    assert d.kind == "ask_owner" and set(d.options) == {700, 701}


# --- старые хвосты: спросить владельца двумя кнопками ---

def test_stale_open_deal_asks_with_two_buttons():
    """Открытая сделка старше 30 дней — это хвост, а не наш заказ (заказ №508)."""
    d = match(order_date=ORDER_DAY, candidates=[
        L(30893137, REAL, CREATED, created_date=ORDER_DAY - timedelta(days=80))])
    assert d.kind == "ask_owner_stale"
    assert d.options == (30893137,)


def test_two_stale_open_deals_ask_once():
    d = match(order_date=ORDER_DAY, candidates=[
        L(30469023, REAL, DONE, created_date=date(2025, 9, 15)),
        L(30893137, REAL, CREATED, created_date=date(2025, 12, 18))])
    assert d.kind == "ask_owner_stale" and set(d.options) == {30469023, 30893137}


def test_fresh_deal_wins_over_stale_tail():
    """Свежая сделка есть — хвосты не мешают и вопроса не будет."""
    d = match(order_date=ORDER_DAY, candidates=[
        L(30893137, REAL, CREATED, created_date=date(2025, 12, 18)),
        L(31600000, REAL, CREATED, created_date=ORDER_DAY - timedelta(days=1))])
    assert d == Decision(kind="use_realization", lead_id=31600000)


def test_completed_deal_beats_stale_tails():
    """Заказ уже проведён руками — хвосты не должны уводить в вопрос (заказ №508)."""
    d = match(order_date=date(2026, 7, 20), candidates=[
        L(30469023, REAL, DONE, created_date=date(2025, 9, 15)),
        L(30893137, REAL, CREATED, created_date=date(2025, 12, 18)),
        L(31442723, REAL, SUCCESS, order_date=date(2026, 7, 20),
          closed_date=date(2026, 7, 28), created_date=date(2026, 7, 20))])
    assert d == Decision(kind="already_done", lead_id=31442723)


def test_stale_boundary_is_two_weeks():
    """Граница из истории: 98,6% верных сделок заведены не раньше чем за 14 дней."""
    assert STALE_LEAD_DAYS == 14

    edge = match(order_date=ORDER_DAY, candidates=[
        L(1, REAL, CREATED, created_date=ORDER_DAY - timedelta(days=14))])
    assert edge.kind == "use_realization"        # ровно две недели — ещё живая

    over = match(order_date=ORDER_DAY, candidates=[
        L(2, REAL, CREATED, created_date=ORDER_DAY - timedelta(days=15))])
    assert over.kind == "ask_owner_stale"


def test_booking_made_weeks_ahead_is_not_a_tail():
    """Заказы №451, №523, №574: клиент записался за три недели вперёд.

    Сделке 20 дней, но её «Дата и время заказа» — ровно день работы.
    Возраст тут ни при чём: это живая запись, а не забытый хвост.
    """
    d = match(order_date=ORDER_DAY, candidates=[
        L(31472345, REAL, CREATED, order_date=ORDER_DAY,
          created_date=ORDER_DAY - timedelta(days=20))])
    assert d == Decision(kind="use_realization", lead_id=31472345)


def test_old_deal_with_a_different_date_is_still_a_tail():
    """Заказ №548: сделке 16 дней и её дата заказа — от прошлой работы."""
    d = match(order_date=date(2026, 8, 7), candidates=[
        L(31448601, REAL, CREATED, order_date=date(2026, 7, 22),
          created_date=date(2026, 7, 22))])
    assert d.kind == "ask_owner_stale"


def test_stale_primary_lead_also_asks():
    d = match(order_date=ORDER_DAY, candidates=[
        L(800, PRIM, NEW_LEAD, created_date=date(2024, 1, 10))])
    assert d.kind == "ask_owner_stale" and d.options == (800,)


# --- дата создания сделки как признак ---

def test_already_done_recognized_by_creation_date():
    """Заказ разбирали пачкой: «Дата заказа» пустая, закрыли через две недели.

    Ни дата заказа, ни дата закрытия не совпадают — опознаём по дате создания.
    """
    d = match(order_date=ORDER_DAY, candidates=[
        L(31281427, REAL, SUCCESS, closed_date=ORDER_DAY + timedelta(days=14),
          created_date=ORDER_DAY - timedelta(days=1))])
    assert d == Decision(kind="already_done", lead_id=31281427)


def test_old_completed_deal_is_not_recognized_as_this_order():
    """Прошлогодняя проведённая сделка — не наш заказ, заводим новую."""
    d = match(order_date=ORDER_DAY, candidates=[
        L(1, REAL, SUCCESS, closed_date=date(2025, 3, 5), created_date=date(2025, 3, 1))])
    assert d.kind == "create_new"


def test_creation_date_alone_does_not_break_a_tie():
    """Две открытые сделки одного мастера — дата создания слишком слабый довод."""
    d = match(
        order_date=ORDER_DAY,
        candidates=[
            L(1, REAL, CREATED, created_date=ORDER_DAY - timedelta(days=10), specialists=[KOZLOV]),
            L(2, REAL, CREATED, created_date=ORDER_DAY - timedelta(days=1), specialists=[KOZLOV]),
        ],
        master_specialist_ids=(KOZLOV,),
    )
    assert d.kind == "ask_owner" and set(d.options) == {1, 2}


def test_deal_created_after_order_is_still_a_candidate():
    """Каждая шестая сделка в истории создана ПОЗЖЕ заказа — отбрасывать нельзя."""
    d = match(order_date=ORDER_DAY, candidates=[
        L(1, REAL, CREATED, created_date=ORDER_DAY + timedelta(days=8))])
    assert d == Decision(kind="use_realization", lead_id=1)


# --- заведомо чужая сделка: другой мастер И сумма в разы больше ---

def test_foreign_looking_deal_on_the_same_day_asks_the_owner():
    """Заказ №561: работа Дмитрия на 9980 ₽, а сделка — общий заказ офиса на 34 980 ₽.

    Взять её нельзя (затрём сумму), завести новую — тоже (у владельца одна сделка
    на весь объект). Отличить от параллельной работы другого мастера нельзя — спрашиваем.
    """
    d = match(
        order_date=ORDER_DAY,
        candidates=[L(31515875, REAL, CREATED, order_date=ORDER_DAY,
                      specialists=[POLOZOV, SKOROPASHKINA], price=Decimal("34980"))],
        master_specialist_ids=(KOZLOV,),
        order_amount=Decimal("9980"),
    )
    assert d.kind == "ask_owner_unrelated" and d.options == (31515875,)


def test_foreign_deal_on_another_date_does_not_block_new_one():
    """Чужая сделка не на дату заказа вопросов не вызывает — заводим свою."""
    d = match(
        order_date=ORDER_DAY,
        candidates=[L(1, REAL, CREATED, order_date=ORDER_DAY - timedelta(days=9),
                      specialists=[SKOROPASHKINA], price=Decimal("17550"))],
        master_specialist_ids=(POLOZOV,),
        order_amount=Decimal("4500"),
    )
    assert d.kind == "create_new"


def test_other_master_but_matching_amount_is_ours():
    """Заказ №418: мастера подменили, но сумма совпадает — сделка наша."""
    d = match(
        order_date=ORDER_DAY,
        candidates=[L(31271495, REAL, CREATED, order_date=ORDER_DAY,
                      specialists=[KOZLOV], price=Decimal("3000"))],
        master_specialist_ids=(POLOZOV,),
        order_amount=Decimal("3000"),
    )
    assert d == Decision(kind="use_realization", lead_id=31271495)


def test_upsell_does_not_make_the_deal_foreign():
    """Заказ №445: в сделке план 5200 ₽, по факту чек 5500 ₽ — расхождение обычное."""
    d = match(
        order_date=ORDER_DAY,
        candidates=[L(1, REAL, CREATED, order_date=ORDER_DAY,
                      specialists=[POLOZOV], price=Decimal("5200"))],
        master_specialist_ids=(KOZLOV,),
        order_amount=Decimal("5500"),
    )
    assert d.kind == "use_realization"


def test_placeholder_price_is_not_evidence():
    """В сделках встречается цена 1 ₽ как заглушка — это не улика против."""
    d = match(
        order_date=ORDER_DAY,
        candidates=[L(1, REAL, CREATED, order_date=ORDER_DAY,
                      specialists=[SKOROPASHKINA], price=Decimal("1"))],
        master_specialist_ids=(POLOZOV,),
        order_amount=Decimal("4500"),
    )
    assert d.kind == "use_realization"


def test_our_master_is_never_foreign_however_odd_the_amount():
    d = match(
        order_date=ORDER_DAY,
        candidates=[L(1, REAL, CREATED, order_date=ORDER_DAY,
                      specialists=[KOZLOV], price=Decimal("50000"))],
        master_specialist_ids=(KOZLOV,),
        order_amount=Decimal("4500"),
    )
    assert d.kind == "use_realization"


# --- одна сделка не закрывает два заказа ---

def test_deal_taken_by_another_order_is_not_offered_again():
    """Заказы №339 и №346: один клиент, два заказа подряд, две сделки.

    Второй заказ не должен хватать сделку, закреплённую за первым.
    """
    candidates = [
        L(31157731, REAL, CREATED, order_date=ORDER_DAY),
        L(31166307, REAL, CREATED, order_date=ORDER_DAY),
    ]
    first = match(order_date=ORDER_DAY, candidates=candidates)
    assert first.kind == "ask_owner"          # без подсказок выбор неоднозначен

    second = match(order_date=ORDER_DAY, candidates=candidates,
                   taken_lead_ids={31157731})
    assert second == Decision(kind="use_realization", lead_id=31166307)


def test_all_deals_taken_means_create_new():
    """Заказов у клиента больше, чем сделок, — недостающую заводим."""
    d = match(order_date=ORDER_DAY,
              candidates=[L(1, REAL, CREATED, order_date=ORDER_DAY)],
              taken_lead_ids={1})
    assert d.kind == "create_new"


def test_taken_deal_does_not_block_already_done():
    """Проведённая сделка тоже закрепляется за своим заказом."""
    candidates = [
        L(1, REAL, SUCCESS, order_date=ORDER_DAY, closed_date=ORDER_DAY),
        L(2, REAL, SUCCESS, order_date=ORDER_DAY, closed_date=ORDER_DAY),
    ]
    assert match(order_date=ORDER_DAY, candidates=candidates,
                 taken_lead_ids={1}) == Decision(kind="already_done", lead_id=2)


# --- ковры ---

def test_carpet_deal_does_not_block_new_deal():
    """Ковровая сделка — параллельный процесс с партнёром: не кандидат и не помеха."""
    d = match(order_date=ORDER_DAY, candidates=[L(1, CARPETS, 42638827)])
    assert d.kind == "create_new"          # не ask_owner_stale и не use_realization


# --- дата закрытия против заявленной даты заказа (заказ №587, найдено 2026-08-25) ---

def test_deal_of_an_earlier_order_closed_today_is_not_this_order():
    """Настоящий случай: заказ №587 (25.08, 5 400 ₽) и сделка заказа №566.

    Владелец разбирает CRM пачками, поэтому сделка от 15.08 была закрыта 25.08 —
    в день нового заказа. По дате закрытия она подходит, но в её поле «Дата и
    время заказа» стоит 15.08: это работа другого дня. Молча привязывать заказ
    к ней нельзя — иначе заказ №587 останется вообще без сделки.
    """
    order_day = date(2026, 8, 25)
    lead = L(31511203, REAL, SUCCESS, order_date=date(2026, 8, 15),
             closed_date=order_day, created_date=date(2026, 8, 15),
             specialists=(POLOZOV,), price=Decimal("9050"))

    d = match(order_date=order_day, candidates=[lead],
              master_specialist_ids=(POLOZOV,), order_amount=Decimal("5400"))

    assert d.kind != "already_done"
    assert d.kind == "ask_owner_stale" and d.options == (31511203,)


def test_closing_date_still_works_when_order_date_is_empty():
    """Поле даты заказа заполняют не всегда: тогда дата закрытия — законный признак."""
    lead = L(1, REAL, SUCCESS, order_date=None, closed_date=ORDER_DAY,
             created_date=ORDER_DAY - timedelta(days=1))

    d = match(order_date=ORDER_DAY, candidates=[lead])

    assert d == Decision(kind="already_done", lead_id=1)


def test_closing_date_works_when_order_date_agrees():
    """Дата заказа в сделке совпадает — обычное «вы уже провели сами»."""
    lead = L(1, REAL, SUCCESS, order_date=ORDER_DAY, closed_date=ORDER_DAY)

    assert match(order_date=ORDER_DAY, candidates=[lead]).kind == "already_done"
