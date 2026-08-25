"""Тесты логики экзамена: как определяется «факт» и как оценивается решение матчера."""

from datetime import date, datetime, timezone
from decimal import Decimal

from adminbot.models import Order
from adminbot.sync.matcher import Decision, LeadInfo
from scripts.history_exam import (
    VERDICT_AUTO, VERDICT_PENDING, VERDICT_WRONG,
    classify, find_fact_lead, to_lead_info,
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
        amount_total=Decimal("5950"), master_names=[],
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


# --- оценка решения матчера ---

def test_hit_counts_as_auto():
    verdict, _ = classify(order(), Decision(kind="already_done", lead_id=77), 77, [])
    assert verdict == VERDICT_AUTO


def test_miss_counts_as_wrong():
    verdict, note = classify(order(), Decision(kind="use_realization", lead_id=78), 77, [])
    assert verdict == VERDICT_WRONG and "78" in note and "77" in note


def test_creating_duplicate_is_the_worst_case():
    verdict, note = classify(order(), Decision(kind="create_new"), 77, [])
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


def test_to_lead_info_reads_pipeline_and_both_dates():
    info = to_lead_info(lead(11, status=CREATED, order_ts=TS_2025_08_12, closed_at=TS_2025_08_12))
    assert info == LeadInfo(lead_id=11, pipeline_id=REAL, status_id=CREATED,
                            order_date=date(2025, 8, 12), closed_date=date(2025, 8, 12), name=None)
