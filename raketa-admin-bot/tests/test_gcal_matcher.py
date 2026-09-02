"""Какой сделке в амо соответствует запись календаря.

Разница с матчером заказов из бота принципиальная: там заказ уже выполнен и
робот смотрит назад (не проведена ли сделка руками), здесь заказ только
предстоит — робот смотрит вперёд. Поэтому закрытая сделка тут вообще не
кандидат: это прошлая работа того же клиента.

Второе отличие: у записи календаря нет ни суммы чека, ни мастера. Значит
отсеять чужую сделку по цене и специалисту нельзя, и единственный надёжный
признак — дата. Где дата не решает, робот спрашивает владельца.
"""

from datetime import date

from adminbot.amo import ids
from adminbot.gcal.matcher import match_event
from adminbot.sync.matcher import LeadInfo

ORDER_DAY = date(2026, 8, 27)


def realization(lead_id: int, status=ids.REAL_STAGE_CREATED, **kwargs) -> LeadInfo:
    return LeadInfo(lead_id=lead_id, pipeline_id=ids.PIPELINE_REALIZATION,
                    status_id=status, **kwargs)


def primary(lead_id: int, status=ids.PRIM_STAGE_DIALOG, **kwargs) -> LeadInfo:
    return LeadInfo(lead_id=lead_id, pipeline_id=ids.PIPELINE_PRIMARY,
                    status_id=status, **kwargs)


def test_open_realization_deal_is_taken():
    """Сейлзбот уже завёл сделку — робот её дозаполняет, этап не двигает."""
    decision = match_event(order_date=ORDER_DAY,
                           candidates=[realization(1, order_date=ORDER_DAY)])

    assert decision.kind == "use_realization"
    assert decision.lead_id == 1


def test_primary_lead_starts_the_chain():
    decision = match_event(order_date=ORDER_DAY, candidates=[primary(2)])

    assert decision.kind == "use_primary"
    assert decision.lead_id == 2


def test_nothing_found_means_new_deal():
    decision = match_event(order_date=ORDER_DAY, candidates=[])

    assert decision.kind == "create_new"


def test_closed_deal_is_never_taken_into_work():
    """Заказ ещё не выполнен, а сделка закрыта — работать с ней робот не станет.

    Он либо спросит владельца (если закрыта ровно на дату записи), либо заведёт
    новую, но дозаполнять и двигать закрытую не будет никогда.
    """
    closed = realization(3, status=ids.STATUS_SUCCESS, order_date=ORDER_DAY,
                         closed_date=ORDER_DAY)

    decision = match_event(order_date=ORDER_DAY, candidates=[closed])

    assert decision.kind not in ("use_realization", "use_primary")


def test_date_picks_between_two_open_deals():
    """У клиента две работы подряд — каждой своя сделка, различает дата."""
    decision = match_event(
        order_date=ORDER_DAY,
        candidates=[realization(1, order_date=date(2026, 8, 30)),
                    realization(2, order_date=ORDER_DAY)])

    assert decision.kind == "use_realization"
    assert decision.lead_id == 2


def test_two_deals_without_dates_are_a_question():
    """Дата ничего не говорит — спрашиваем владельца, а не гадаем."""
    decision = match_event(order_date=ORDER_DAY,
                           candidates=[realization(1), realization(2)])

    assert decision.kind == "ask_owner"
    assert decision.options == (1, 2)


def test_deal_taken_by_another_calendar_event_is_skipped():
    """Одна сделка не может обслуживать две записи календаря."""
    decision = match_event(
        order_date=ORDER_DAY,
        candidates=[realization(1, order_date=ORDER_DAY)],
        taken_lead_ids={1})

    assert decision.kind == "create_new"


def test_carpet_and_archived_pipelines_are_ignored():
    """Ковры идут через партнёра, архив — прошлая жизнь CRM."""
    carpet = LeadInfo(lead_id=9, pipeline_id=ids.PIPELINE_CARPETS,
                      status_id=ids.CARPET_STAGE_IN_WORK, order_date=ORDER_DAY)

    decision = match_event(order_date=ORDER_DAY, candidates=[carpet])

    assert decision.kind == "create_new"


def test_regular_client_books_three_weeks_ahead():
    """Лид завели 6 августа, работа 27-го — это норма, а не забытый хвост.

    Для заказов из бота такой лид считался бы протухшим (граница 14 дней), но
    там дело было после работы. Запись календаря наоборот появляется заранее.
    """
    decision = match_event(order_date=ORDER_DAY,
                           candidates=[primary(2, created_date=date(2026, 8, 6))])

    assert decision.kind == "use_primary"
    assert decision.lead_id == 2


def test_stale_lead_of_recent_months_is_a_question_not_a_new_deal():
    """Хвост трёхмесячной давности — молча заводить поверх него вторую нельзя.

    Такой лид ещё может оказаться про этот самый заказ: клиент обращался
    весной, договорились, потом перенесли. Решает владелец.
    """
    decision = match_event(order_date=ORDER_DAY,
                           candidates=[primary(2, created_date=date(2026, 6, 1))])

    assert decision.kind == "ask_owner_stale"
    assert decision.options == (2,)


def test_forgotten_lead_does_not_stop_a_new_deal():
    """Хвост старше полугода — забытая сделка, а не работа. Заводим новую.

    Решение владельца 2026-09-02. Повод: по записи на 06.09.2026 робот спросил
    владельца из-за сделки от 11.09.2024, висевшей на этапе «мастер назначен».
    В CRM таких 60 из 93 незакрытых — вопросы приходили бы постоянно.
    """
    decision = match_event(order_date=ORDER_DAY,
                           candidates=[realization(9, created_date=date(2024, 9, 11))])

    assert decision.kind == "create_new"
    assert decision.forgotten == (9,)          # владельцу скажем, что хвост висит


def test_a_fresh_tail_outweighs_a_forgotten_one():
    """Есть и свежий хвост, и древний — спрашиваем, но кнопкой только свежий.

    Древние в вариантах бесполезны: привязывать сегодняшний заказ к сделке
    двухлетней давности владелец не станет, а лишняя кнопка путает.
    """
    decision = match_event(
        order_date=ORDER_DAY,
        candidates=[primary(2, created_date=date(2026, 6, 1)),
                    realization(9, created_date=date(2024, 9, 11))])

    assert decision.kind == "ask_owner_stale"
    assert decision.options == (2,)


def test_a_tail_without_dates_is_still_a_question():
    """Возраст неизвестен — считаем хвост живым и спрашиваем.

    Сомнение решается в пользу вопроса: заводить вторую сделку по живому
    заказу дороже, чем лишний раз спросить.
    """
    decision = match_event(order_date=ORDER_DAY, candidates=[primary(2)])

    assert decision.kind == "use_primary"      # без дат лид считается своевременным

    decision = match_event(
        order_date=ORDER_DAY,
        candidates=[primary(2, created_date=date(2026, 6, 1)), primary(3)])

    assert decision.kind in ("use_primary", "ask_owner_stale")


def test_handled_lead_wins_over_unsorted():
    """Клиент мог обратиться дважды: сначала пропущенный звонок, потом разговор."""
    decision = match_event(
        order_date=ORDER_DAY,
        candidates=[primary(1, status=ids.STATUS_UNSORTED_PRIMARY),
                    primary(2, status=ids.PRIM_STAGE_DIALOG)])

    assert decision.kind == "use_primary"
    assert decision.lead_id == 2
    assert decision.duplicates == (1,)         # второму лиду робот оставит комментарий


def test_realization_wins_over_primary():
    """Сделка реализации уже создана сейлзботом — лид первичной вторичен."""
    decision = match_event(
        order_date=ORDER_DAY,
        candidates=[primary(1), realization(2, order_date=ORDER_DAY)])

    assert decision.kind == "use_realization"
    assert decision.lead_id == 2


def test_closed_deal_on_the_same_day_is_a_question():
    """Владелец мог провести этот заказ сам до того, как записал его в календарь.

    Решение владельца 2026-08-27. Раньше робот молча заводил вторую сделку:
    закрытые он считает прошлой работой клиента. Но закрытая ровно на дату
    записи — скорее тот же заказ, и решать это должен владелец.
    """
    closed = realization(5, status=ids.STATUS_SUCCESS, order_date=ORDER_DAY,
                         closed_date=ORDER_DAY)

    decision = match_event(order_date=ORDER_DAY, candidates=[closed])

    assert decision.kind == "ask_owner_closed"
    assert decision.options == (5,)


def test_old_closed_deal_is_still_just_history():
    """Закрытая сделка с другой датой — прошлый заказ, вопрос задавать не о чем."""
    closed = realization(5, status=ids.STATUS_SUCCESS, order_date=date(2026, 5, 10),
                         closed_date=date(2026, 5, 12))

    decision = match_event(order_date=ORDER_DAY, candidates=[closed])

    assert decision.kind == "create_new"


def test_open_deal_wins_over_a_closed_one():
    """Есть открытая — работаем с ней, закрытая не мешает."""
    decision = match_event(
        order_date=ORDER_DAY,
        candidates=[realization(5, status=ids.STATUS_SUCCESS, order_date=ORDER_DAY,
                                closed_date=ORDER_DAY),
                    realization(6, order_date=ORDER_DAY)])

    assert decision.kind == "use_realization"
    assert decision.lead_id == 6
