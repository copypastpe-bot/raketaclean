"""Тесты матчера. Каждый хитрый случай — из разведки `tgbot-v1/recon/06-matching-metrics.md`."""

from datetime import date

from adminbot.sync.matcher import match, Decision, LeadInfo


def L(id, pipeline, status, order_date=None):
    return LeadInfo(lead_id=id, pipeline_id=pipeline, status_id=status, order_date=order_date)


REAL, PRIM, SUCCESS, CLOSED = 4482787, 4482751, 142, 143
CREATED = 41463832
PRIM_NEW = 41463535          # «Новый лид» первичной воронки
CARPETS, CARPETS_LEGACY = 4645519, 5215336


def test_path_a_single_open_realization():
    d = match(order_date=date(2026, 8, 20), candidates=[L(1, REAL, CREATED)])
    assert d == Decision(kind="use_realization", lead_id=1)


def test_path_b_only_primary_lead():
    d = match(order_date=date(2026, 8, 20), candidates=[L(2, PRIM, PRIM_NEW)])
    assert d == Decision(kind="use_primary", lead_id=2)


def test_path_c_no_candidates():
    assert match(order_date=date(2026, 8, 20), candidates=[]).kind == "create_new"


def test_pair_primary_plus_realization_same_client_is_not_ambiguous():
    # пара «первичная(успех) + автосделка» — это ОДИН заказ; приоритет реализации
    d = match(order_date=date(2026, 7, 23), candidates=[
        L(10, PRIM, SUCCESS, date(2026, 7, 23)), L(11, REAL, CREATED, date(2026, 7, 23))])
    assert d == Decision(kind="use_realization", lead_id=11)


def test_stale_dates_resolved_by_open_stage():
    # кейс «Лен!Ковролин, Анна»: даты 2024 года, но открытая сделка одна
    d = match(order_date=date(2026, 6, 16), candidates=[
        L(20, REAL, SUCCESS, date(2024, 5, 14)), L(21, REAL, CREATED, date(2026, 6, 8))])
    assert d == Decision(kind="use_realization", lead_id=21)


def test_two_open_same_date_is_ambiguous():
    # кейс «Ниж! Матрас, Юлия» с ДВУМЯ открытыми: вопрос владельцу
    d = match(order_date=date(2026, 7, 13), candidates=[
        L(30, REAL, CREATED, date(2026, 7, 13)), L(31, REAL, CREATED, date(2026, 7, 13))])
    assert d.kind == "ask_owner" and set(d.options) == {30, 31}


def test_already_completed_deal_binds_without_touching():
    # вы провели руками раньше робота (дизайн §5.1)
    d = match(order_date=date(2026, 8, 20), candidates=[L(40, REAL, SUCCESS, date(2026, 8, 20))])
    assert d == Decision(kind="already_done", lead_id=40)


def test_carpets_pipeline_ignored():
    # заказ из бота — никогда не ковры (решение владельца №7-контекст)
    d = match(order_date=date(2026, 8, 20), candidates=[L(50, CARPETS, 42638827)])
    assert d.kind == "create_new"


# --- дополнительные случаи разведки ---

def test_legacy_carpets_and_archived_pipelines_ignored():
    d = match(order_date=date(2026, 8, 20), candidates=[
        L(60, CARPETS_LEGACY, 1), L(61, 4482808, 2), L(62, 4781380, 3), L(63, 7554370, 4)])
    assert d.kind == "create_new"


def test_date_picks_one_of_several_open_realization():
    # 19% случаев по метрике M5: несколько открытых, дата ±2 дня решает
    d = match(order_date=date(2026, 8, 2), candidates=[
        L(70, REAL, CREATED, date(2026, 7, 19)),
        L(71, REAL, CREATED, date(2026, 8, 1)),
        L(72, REAL, CREATED, date(2026, 8, 10))])
    assert d == Decision(kind="use_realization", lead_id=71)


def test_several_open_none_matching_date_asks_owner():
    # кейс №540: серия заказов постоянного клиента, дата не решает → не выдумываем
    d = match(order_date=date(2026, 8, 2), candidates=[
        L(80, REAL, CREATED, date(2026, 7, 19)), L(81, REAL, CREATED, date(2026, 8, 10))])
    assert d.kind == "ask_owner" and set(d.options) == {80, 81}


def test_open_realization_wins_over_open_primary():
    d = match(order_date=date(2026, 8, 20), candidates=[
        L(90, PRIM, PRIM_NEW, date(2026, 8, 20)), L(91, REAL, CREATED, date(2026, 8, 20))])
    assert d == Decision(kind="use_realization", lead_id=91)


def test_closed_deals_are_not_candidates():
    # «Закрыто и не реализовано» — не наш заказ, заводим новую сделку
    d = match(order_date=date(2026, 8, 20), candidates=[
        L(100, REAL, CLOSED, date(2026, 8, 20)), L(101, PRIM, CLOSED, date(2026, 8, 20))])
    assert d.kind == "create_new"


def test_old_completed_deals_do_not_block_new_order():
    # постоянный клиент: прошлые заказы проведены, новый заказ — новая сделка
    d = match(order_date=date(2026, 8, 20), candidates=[
        L(110, REAL, SUCCESS, date(2025, 3, 1)), L(111, REAL, SUCCESS, date(2024, 11, 5))])
    assert d.kind == "create_new"


def test_completed_realization_wins_over_open_primary():
    # заказ проведён руками, а старый лид в первичной так и висит открытым
    d = match(order_date=date(2026, 8, 20), candidates=[
        L(120, PRIM, PRIM_NEW, date(2026, 8, 20)), L(121, REAL, SUCCESS, date(2026, 8, 20))])
    assert d == Decision(kind="already_done", lead_id=121)


def test_several_open_primary_leads_resolved_by_date():
    d = match(order_date=date(2026, 8, 20), candidates=[
        L(130, PRIM, PRIM_NEW, date(2026, 8, 19)), L(131, PRIM, PRIM_NEW, date(2026, 5, 1))])
    assert d == Decision(kind="use_primary", lead_id=130)


def test_several_open_primary_leads_without_dates_ask_owner():
    d = match(order_date=date(2026, 8, 20), candidates=[
        L(140, PRIM, PRIM_NEW), L(141, PRIM, PRIM_NEW)])
    assert d.kind == "ask_owner" and set(d.options) == {140, 141}


def test_date_window_is_two_days_inclusive():
    assert match(order_date=date(2026, 8, 20), candidates=[
        L(150, REAL, CREATED, date(2026, 8, 18)), L(151, REAL, CREATED, date(2026, 8, 14)),
    ]) == Decision(kind="use_realization", lead_id=150)
    assert match(order_date=date(2026, 8, 20), candidates=[
        L(152, REAL, CREATED, date(2026, 8, 22)), L(153, REAL, CREATED, date(2026, 8, 26)),
    ]) == Decision(kind="use_realization", lead_id=152)


def test_options_are_sorted_for_stable_cards():
    d = match(order_date=date(2026, 8, 20), candidates=[
        L(161, REAL, CREATED), L(160, REAL, CREATED)])
    assert d.options == (160, 161)      # порядок кнопок в карточке не скачет
