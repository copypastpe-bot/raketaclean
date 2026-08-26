"""Какой сделке amoCRM соответствует строка отчёта партнёра.

Случаи взяты из боевой разведки 2026-08-26 по двум свежим отчётам: из пяти
строк три нашли открытую ковровую сделку, одна — только лид в первичной,
одна не нашла ничего свежего. Правила должны отрабатывать именно это.

Принцип тот же, что и в уборке: лучше спросить владельца, чем уверенно
ошибиться. Ковры сложнее — заказ диктуется партнёру голосом, и связь между
его базой и CRM слабее.
"""

from datetime import date
from decimal import Decimal

from adminbot.amo import ids
from adminbot.carpets.matcher import CarpetDecision, CarpetLead, match_carpet
from adminbot.carpets.report import CarpetRow

PRIM, REAL, CARPETS, LEGACY = 4482751, 4482787, 4645519, 5215336
HANDED_OVER, IN_WORK, UNSORTED_CARPET = 42638827, 71292730, 42638824
UNSORTED_PRIM, NEW_LEAD = 41463532, 41463535
DELIVERED, REFUSED = 142, 143


def row(partner_id=44426, added=date(2026, 8, 12), returned=date(2026, 8, 23),
        amount="3995", phone="9601945325") -> CarpetRow:
    return CarpetRow(partner_id=partner_id, phone10=phone, amount=Decimal(amount),
                     added_date=added, return_date=returned,
                     pickup_date=date(2026, 8, 16), district="Советский")


def lead(lead_id, pipeline, status, *, created=date(2026, 8, 12), order_date=None,
         price=None) -> CarpetLead:
    return CarpetLead(lead_id=lead_id, pipeline_id=pipeline, status_id=status,
                      created_date=created, order_date=order_date,
                      price=None if price is None else Decimal(price))


# --- прямое попадание: открытая ковровая сделка ---

def test_single_open_carpet_lead_is_taken():
    """Заказ №44426: одна сделка «Передано в работу (ВПО)» — её и проводим."""
    decision = match_carpet(row(), [lead(31516051, CARPETS, HANDED_OVER,
                                         order_date=date(2026, 8, 16))])

    assert decision == CarpetDecision(kind="use_carpet", lead_id=31516051)


def test_old_carpet_deals_do_not_hide_the_fresh_one():
    """Заказ №44418: рядом со свежей висят ковровые 2024 и 2025 годов."""
    decision = match_carpet(row(partner_id=44418, phone="9200008625"), [
        lead(29458331, CARPETS, DELIVERED, created=date(2024, 11, 26)),
        lead(30220181, CARPETS, DELIVERED, created=date(2025, 7, 15)),
        lead(31516071, CARPETS, HANDED_OVER, created=date(2026, 8, 12)),
    ])

    assert decision.kind == "use_carpet" and decision.lead_id == 31516071


def test_cleaning_deal_of_the_same_client_is_ignored():
    """Заказ №44352: у клиента в тот же день и уборка, и ковры. Берём ковры."""
    decision = match_carpet(row(partner_id=44352, phone="9200009033"), [
        lead(31507673, REAL, DELIVERED, created=date(2026, 8, 9)),
        lead(31507675, CARPETS, HANDED_OVER, created=date(2026, 8, 9)),
    ])

    assert decision.kind == "use_carpet" and decision.lead_id == 31507675


def test_deal_in_work_stage_also_counts_as_open():
    decision = match_carpet(row(), [lead(1, CARPETS, IN_WORK)])

    assert decision.kind == "use_carpet"


# --- уже проведено владельцем ---

def test_carpet_deal_delivered_around_the_return_date_is_already_done():
    """Владелец успел провести сделку сам — только привязываем, не трогаем."""
    decision = match_carpet(row(), [lead(2, CARPETS, DELIVERED,
                                         created=date(2026, 8, 12),
                                         order_date=date(2026, 8, 23))])

    assert decision == CarpetDecision(kind="already_done", lead_id=2)


def test_old_delivered_carpet_deal_is_not_this_order():
    """Ковровая сделка годичной давности к этому заказу отношения не имеет."""
    decision = match_carpet(row(), [lead(3, CARPETS, DELIVERED,
                                         created=date(2025, 7, 15),
                                         order_date=date(2025, 7, 15))])

    assert decision.kind != "already_done"


# --- несколько открытых: спрашиваем ---

def test_two_open_carpet_deals_ask_the_owner():
    decision = match_carpet(row(), [
        lead(10, CARPETS, HANDED_OVER, created=date(2026, 8, 12)),
        lead(11, CARPETS, HANDED_OVER, created=date(2026, 8, 12)),
    ])

    assert decision.kind == "ask_owner" and set(decision.options) == {10, 11}


def test_date_separates_two_open_carpet_deals():
    """Одна заведена под этот заказ, другая — месяц назад: выбираем по дате."""
    decision = match_carpet(row(), [
        lead(12, CARPETS, HANDED_OVER, created=date(2026, 7, 5)),
        lead(13, CARPETS, HANDED_OVER, created=date(2026, 8, 12)),
    ])

    assert decision.kind == "use_carpet" and decision.lead_id == 13


# --- ковровой нет, но есть лид в первичной ---

def test_fresh_primary_lead_is_used_when_carpet_deal_is_missing():
    """Заказ №44535: ковровой нет, но в первичной висит свежий лид."""
    decision = match_carpet(row(partner_id=44535, added=date(2026, 8, 17),
                                returned=date(2026, 8, 22), phone="9202994600"), [
        lead(31532745, PRIM, UNSORTED_PRIM, created=date(2026, 8, 17)),
    ])

    assert decision == CarpetDecision(kind="use_primary", lead_id=31532745)


def test_old_primary_lead_is_not_used():
    decision = match_carpet(row(), [lead(20, PRIM, NEW_LEAD, created=date(2026, 5, 1))])

    assert decision.kind == "create_new"


# --- ничего подходящего ---

def test_nothing_fresh_means_new_chain():
    """Заказ №44345: ковровая от 04.07 закрыта, свежего ничего нет."""
    decision = match_carpet(row(partner_id=44345, added=date(2026, 8, 8),
                                returned=date(2026, 8, 13), phone="9108970195"), [
        lead(31381221, CARPETS, REFUSED, created=date(2026, 7, 4)),
        lead(31503937, PRIM, REFUSED, created=date(2026, 8, 6)),
    ])

    assert decision.kind == "create_new"


def test_no_deals_at_all_means_new_chain():
    assert match_carpet(row(), []).kind == "create_new"


def test_legacy_carpet_pipeline_is_ignored():
    """«Воронка ковров реализация» — рудимент, туда ничего не пишем."""
    decision = match_carpet(row(), [lead(30, LEGACY, HANDED_OVER)])

    assert decision.kind == "create_new"


# --- занятые сделки ---

def test_deal_taken_by_another_partner_order_is_not_reused():
    """У клиента два заказа подряд — каждой работе своя сделка."""
    decision = match_carpet(row(), [lead(40, CARPETS, HANDED_OVER)], taken_lead_ids={40})

    assert decision.kind == "create_new"
