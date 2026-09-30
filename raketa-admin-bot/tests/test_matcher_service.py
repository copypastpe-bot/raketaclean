"""Сверка «Услуги» сделки с видом заказа (п.8, решения владельца 2026-09-30).

Повод — уборка №9 Дарьи («Генералка», 12 600) ушла в сделку дивана: единственная
открытая сделка клиента бралась без сверки услуги.

Четыре группы услуг: химчистка на дому, уборка, ковры «Кристалл», «Другое».
- услуга из группы заказа (хоть одна) — сделка подходит, выбор как раньше;
- все услуги из чужой группы — сделки для заказа нет, ищем дальше;
- «Другое» — робот сам не берёт, спрашивает владельца;
- услуги нет или она неизвестна (удалена из справочника) — как раньше.
"""

from datetime import date, timedelta

import pytest

from adminbot.amo import ids
from adminbot.sync.matcher import Decision, LeadInfo, match

REAL, PRIM = ids.PIPELINE_REALIZATION, ids.PIPELINE_PRIMARY
CREATED = ids.REAL_STAGE_CREATED
NEW_LEAD = 41463535
SUCCESS = ids.STATUS_SUCCESS

HOME, CLEANING = ids.SERVICE_GROUP_HOME, ids.SERVICE_GROUP_CLEANING
FURNITURE, UBORKA, WINDOWS = ids.SERVICE_ENUM_FURNITURE, ids.SERVICE_ENUM_CLEANING, ids.SERVICE_ENUM_WINDOWS
OTHER, RUG_PICKUP, FACADES = ids.SERVICE_ENUM_OTHER, ids.SERVICE_ENUM_RUG_PICKUP, ids.SERVICE_ENUM_FACADES
SOFA_CORNER_DELETED = 878949          # «Диван угловой до 220» — удалён из справочника

ORDER_DAY = date(2026, 9, 29)


def L(id, pipeline=REAL, status=CREATED, *, services=(), order_date=ORDER_DAY, closed_date=None):
    return LeadInfo(
        lead_id=id, pipeline_id=pipeline, status_id=status,
        order_date=order_date, closed_date=closed_date,
        created_date=ORDER_DAY - timedelta(days=1),
        service_ids=tuple(services),
    )


def test_service_groups_follow_owner_decision():
    """Живой справочник «Услуги» 2026-09-30, разложенный владельцем по группам."""
    assert ids.SERVICE_GROUPS == {
        933165: HOME,        # Чистка мебели
        128971: HOME,        # Чистка матрасов
        128981: HOME,        # Чистка ковролина
        878959: HOME,        # чистка ковра на дому
        878955: HOME,        # Диван премиум
        772345: CLEANING,    # Уборка
        933169: CLEANING,    # Мойка окон
        947369: ids.SERVICE_GROUP_CARPETS,   # Ковры КРИСТАЛ
        128983: ids.SERVICE_GROUP_OTHER,     # Другое
        878957: ids.SERVICE_GROUP_OTHER,     # Ковер вывоз (своими силами)
        933167: ids.SERVICE_GROUP_OTHER,     # Фасады/Вывески
    }


def test_darya_cleaning_order_does_not_take_furniture_deal():
    """Уборка, единственная открытая сделка — «Чистка мебели»: заводим новую, диван не трогаем."""
    d = match(order_date=ORDER_DAY, candidates=[L(31685437, services=[FURNITURE])],
              order_group=CLEANING)
    assert d == Decision(kind="create_new")


@pytest.mark.parametrize("service", [UBORKA, WINDOWS])
def test_furniture_order_does_not_take_cleaning_deal(service):
    d = match(order_date=ORDER_DAY, candidates=[L(1, services=[service])], order_group=HOME)
    assert d == Decision(kind="create_new")


def test_carpets_service_is_alien_to_both():
    d = match(order_date=ORDER_DAY, candidates=[L(1, services=[ids.SERVICE_ENUM_CARPETS])],
              order_group=HOME)
    assert d == Decision(kind="create_new")


def test_deal_with_services_of_both_groups_fits():
    d = match(order_date=ORDER_DAY, candidates=[L(1, services=[FURNITURE, UBORKA])],
              order_group=CLEANING)
    assert d == Decision(kind="use_realization", lead_id=1)


@pytest.mark.parametrize("services", [(), (SOFA_CORNER_DELETED,)])
def test_empty_or_unknown_service_keeps_old_behaviour(services):
    d = match(order_date=ORDER_DAY, candidates=[L(1, services=services)], order_group=CLEANING)
    assert d == Decision(kind="use_realization", lead_id=1)


def test_alien_deal_no_longer_makes_choice_ambiguous():
    """Раньше две открытые сделки на одну дату — вопрос владельцу; чужая теперь не в счёт."""
    d = match(order_date=ORDER_DAY,
              candidates=[L(1, services=[FURNITURE]), L(2, services=[UBORKA])],
              order_group=CLEANING)
    assert d == Decision(kind="use_realization", lead_id=2)


def test_completed_alien_deal_is_not_taken_as_already_done():
    """Закрытая сделка дивана на ту же дату — не «проведено руками» для уборки."""
    d = match(order_date=ORDER_DAY,
              candidates=[L(1, status=SUCCESS, services=[FURNITURE], closed_date=ORDER_DAY)],
              order_group=CLEANING)
    assert d == Decision(kind="create_new")


def test_alien_primary_lead_is_skipped():
    d = match(order_date=ORDER_DAY, candidates=[L(1, PRIM, NEW_LEAD, services=[FURNITURE])],
              order_group=CLEANING)
    assert d == Decision(kind="create_new")


@pytest.mark.parametrize("service", [OTHER, RUG_PICKUP, FACADES])
def test_other_service_deal_asks_owner(service):
    d = match(order_date=ORDER_DAY, candidates=[L(7, services=[service])], order_group=HOME)
    assert d == Decision(kind="ask_owner_other", options=(7,))


def test_completed_other_service_deal_asks_owner_too():
    d = match(order_date=ORDER_DAY,
              candidates=[L(7, status=SUCCESS, services=[OTHER], closed_date=ORDER_DAY)],
              order_group=CLEANING)
    assert d == Decision(kind="ask_owner_other", options=(7,))


def test_other_plus_own_group_fits():
    d = match(order_date=ORDER_DAY, candidates=[L(7, services=[OTHER, UBORKA])],
              order_group=CLEANING)
    assert d == Decision(kind="use_realization", lead_id=7)


def test_without_order_group_nothing_changes():
    """Выключатель выключен или вид заказа неизвестен — сделка берётся как раньше."""
    d = match(order_date=ORDER_DAY, candidates=[L(1, services=[FURNITURE])])
    assert d == Decision(kind="use_realization", lead_id=1)
