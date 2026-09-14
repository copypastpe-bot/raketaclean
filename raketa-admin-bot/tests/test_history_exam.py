"""Тесты логики экзамена: как определяется «факт» и как оценивается решение матчера."""

from datetime import date, datetime, timezone
from decimal import Decimal

from adminbot.models import Order
from adminbot.sync.matcher import Decision, LeadInfo
from scripts.history_exam import (
    SOURCE_AMBIGUOUS, VERDICT_AMBIGUOUS, VERDICT_AUTO, VERDICT_PENDING, VERDICT_WRONG,
    classify, find_fact_lead, to_lead_info, to_lead_info_as_of,
)

REAL, PRIM, SUCCESS, CREATED = 4482787, 4482751, 142, 41463832
# 1755000000 = 12.08.2025 15:40 по Москве
TS_2025_08_12 = 1755000000


def lead(id, pipeline=REAL, status=SUCCESS, *, order_ts=None, closed_at=None):
    payload = {"id": id, "pipeline_id": pipeline, "status_id": status, "custom_fields_values": []}
    if order_ts:
        payload["custom_fields_values"] = [{"field_id": 18701, "values": [{"value": order_ts}]}]
    if closed_at:
        payload["closed_at"] = closed_at
    return payload


def order(order_id=596, day=date(2025, 8, 12)):
    return Order(
        order_id=order_id, phone10="9601861067",
        created_at=datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc),
        amount_total=Decimal("5950"), masters=[],
    )


# --- определение факта ---

def test_fact_found_by_order_date_field():
    leads = [lead(1, order_ts=TS_2025_08_12)]
    assert find_fact_lead(date(2025, 8, 12), leads) == (1, "по полю «Дата и время заказа»")


def test_fact_falls_back_to_closed_at_when_date_field_empty():
    # 22% сделок без «Даты и времени заказа» — иначе факт бы потерялся
    leads = [lead(2, closed_at=TS_2025_08_12)]
    assert find_fact_lead(date(2025, 8, 12), leads) == (2, "по дате закрытия сделки")


def test_open_deals_are_never_the_fact():
    assert find_fact_lead(date(2025, 8, 12), [lead(3, status=CREATED, order_ts=TS_2025_08_12)]) == (None, "")


def test_primary_pipeline_success_is_not_the_fact():
    # «Передано в работу» в первичной воронке — это ещё не проведённый заказ
    assert find_fact_lead(date(2025, 8, 12), [lead(4, pipeline=PRIM, order_ts=TS_2025_08_12)]) == (None, "")


def test_distant_completed_deal_is_not_the_fact():
    assert find_fact_lead(date(2026, 1, 1), [lead(5, order_ts=TS_2025_08_12, closed_at=TS_2025_08_12)]) == (None, "")


def test_closest_completed_deal_wins():
    leads = [lead(6, closed_at=TS_2025_08_12), lead(7, closed_at=TS_2025_08_12 + 2 * 86400)]
    assert find_fact_lead(date(2025, 8, 12), leads)[0] == 6


def test_tie_is_resolved_by_order_amount():
    """Заказ №520: уборка и химчистка по одному адресу в один день.

    Обе сделки проведены и одинаково близки по дате — разводим по сумме чека.
    Сумму матчер не смотрит, поэтому проверка остаётся независимой.
    """
    cleaning = lead(31442847, order_ts=TS_2025_08_12)
    cleaning["price"] = 20500
    chemistry = lead(31450633, order_ts=TS_2025_08_12)
    chemistry["price"] = 3300

    found, source = find_fact_lead(date(2025, 8, 12), [cleaning, chemistry], Decimal("3300"))
    assert found == 31450633 and "сумме чека" in source


def test_tie_without_matching_amount_is_ambiguous():
    """Сумма не развела — честно признаём, что сверять не с чем."""
    first, second = lead(1, order_ts=TS_2025_08_12), lead(2, order_ts=TS_2025_08_12)
    first["price"] = second["price"] = 5000

    found, source = find_fact_lead(date(2025, 8, 12), [first, second], Decimal("3300"))
    assert found is None and source == SOURCE_AMBIGUOUS

    verdict, _ = classify(order(), Decision(kind="use_realization", lead_id=1), None, [], source)
    assert verdict == VERDICT_AMBIGUOUS       # не ошибка и не зачёт


# --- оценка решения матчера ---

def test_hit_counts_as_auto():
    verdict, _ = classify(order(), Decision(kind="already_done", lead_id=77), 77, [])
    assert verdict == VERDICT_AUTO


def test_miss_counts_as_wrong():
    verdict, note = classify(order(), Decision(kind="use_realization", lead_id=78), 77, [])
    assert verdict == VERDICT_WRONG and "78" in note and "77" in note


def test_creating_duplicate_is_the_worst_case():
    """Сделка уже существовала и была проведена, а робот всё равно завёл бы новую."""
    existing = [LeadInfo(lead_id=77, pipeline_id=REAL, status_id=SUCCESS)]
    verdict, note = classify(order(), Decision(kind="create_new"), 77, existing)
    assert verdict == VERDICT_WRONG and "дубль" in note


def test_create_new_without_fact_is_correct():
    verdict, _ = classify(order(), Decision(kind="create_new"), None, [])
    assert verdict == VERDICT_AUTO


def test_open_deal_without_fact_is_pending_not_error():
    # свежий заказ: сделка открыта, владелец ещё не провёл — робот сработал бы верно
    candidates = [LeadInfo(lead_id=80, pipeline_id=REAL, status_id=CREATED)]
    verdict, _ = classify(order(), Decision(kind="use_realization", lead_id=80), None, candidates)
    assert verdict == VERDICT_PENDING


def test_path_b_without_fact_is_correct():
    verdict, _ = classify(order(), Decision(kind="use_primary", lead_id=90), None, [])
    assert verdict == VERDICT_AUTO


def test_path_b_is_correct_when_the_deal_did_not_exist_yet():
    """Заказ №426: сделку реализации сейлзбот создаст ПОСЛЕ перевода лида в работу."""
    existing = [LeadInfo(lead_id=31300017, pipeline_id=PRIM, status_id=41463535)]
    verdict, note = classify(
        order(), Decision(kind="use_primary", lead_id=31300017), 31311313, existing)
    assert verdict == VERDICT_AUTO and "31311313" in note


def test_create_new_is_correct_when_nothing_existed_yet():
    """Заказ №429: в момент заказа в CRM не было ничего, сделки завели назавтра."""
    verdict, _ = classify(order(), Decision(kind="create_new"), 31312227, [])
    assert verdict == VERDICT_AUTO


def test_taking_a_deal_that_is_not_the_eventual_one_is_still_wrong():
    """Взял существовавшую сделку, а провели другую — это настоящая ошибка."""
    existing = [LeadInfo(lead_id=100, pipeline_id=REAL, status_id=CREATED)]
    verdict, _ = classify(
        order(), Decision(kind="use_realization", lead_id=100), 200, existing)
    assert verdict == VERDICT_WRONG


# --- восстановление состояния CRM на момент заказа ---

MOMENT = datetime(2025, 8, 12, 18, 0, tzinfo=timezone.utc)      # момент заказа
BEFORE = int(datetime(2025, 8, 10, 12, 0, tzinfo=timezone.utc).timestamp())
AFTER = int(datetime(2025, 8, 20, 12, 0, tzinfo=timezone.utc).timestamp())


def test_lead_created_later_did_not_exist_yet():
    later = lead(1, status=CREATED)
    later["created_at"] = AFTER
    assert to_lead_info_as_of(later, MOMENT) is None


def test_lead_closed_later_was_open_at_that_moment():
    """Сделка сейчас проведена, но на момент заказа была ещё в работе."""
    payload = lead(2, status=SUCCESS, closed_at=AFTER)
    payload["created_at"] = BEFORE

    info = to_lead_info_as_of(payload, MOMENT)

    assert info.is_open                       # тогда она была открыта
    assert info.status_id == 41463832         # точный этап неизвестен — подставной
    assert info.closed_date is None


def test_lead_closed_earlier_stays_closed():
    payload = lead(3, status=SUCCESS, closed_at=BEFORE)
    payload["created_at"] = BEFORE

    info = to_lead_info_as_of(payload, MOMENT)

    assert not info.is_open and info.is_success
    assert info.closed_date == date(2025, 8, 10)


def test_open_lead_keeps_its_stage():
    payload = lead(4, status=CREATED)
    payload["created_at"] = BEFORE
    assert to_lead_info_as_of(payload, MOMENT).status_id == CREATED


def test_primary_lead_open_at_that_moment_gets_new_lead_stage():
    payload = lead(5, pipeline=PRIM, status=SUCCESS, closed_at=AFTER)
    payload["created_at"] = BEFORE
    assert to_lead_info_as_of(payload, MOMENT).status_id == 41463535    # «Новый лид»


def test_to_lead_info_reads_pipeline_and_both_dates():
    info = to_lead_info(lead(11, status=CREATED, order_ts=TS_2025_08_12, closed_at=TS_2025_08_12))
    assert info == LeadInfo(lead_id=11, pipeline_id=REAL, status_id=CREATED,
                            order_date=date(2025, 8, 12), closed_date=date(2025, 8, 12), name=None)
