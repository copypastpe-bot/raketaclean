"""Отбор забытых сделок и их закрытие.

Здесь проверяется не удобство, а безопасность: скрипт закрывает сделки в
боевой CRM пачкой, и цена ошибки несимметрична. Закрыть живой заказ —
испортить работу и статистику; оставить лишний хвост — потерять строчку
в списке. Поэтому все сомнения решаются в пользу «не трогать».
"""

from datetime import date, datetime, timezone

import pytest

from adminbot.amo import ids
from adminbot.amo.stale import (
    age_days, created_day, fetch_open_leads, open_stages, select_forgotten)

TODAY = date(2026, 9, 2)


def a_lead(lead_id: int, created: date = None, *, status=ids.REAL_STAGE_CONFIRMED,
           pipeline=ids.PIPELINE_REALIZATION, **extra) -> dict:
    lead = {"id": lead_id, "pipeline_id": pipeline, "status_id": status, **extra}
    if created is not None:
        lead["created_at"] = int(datetime(created.year, created.month, created.day,
                                          tzinfo=timezone.utc).timestamp())
    return lead


class FakePipelines:
    """amoCRM, которая отдаёт воронки и сделки по этапам."""

    def __init__(self, pipelines: list[dict], leads: dict[int, list[dict]]):
        self.pipelines = pipelines
        self.leads = leads
        self.asked: list[tuple[int, int]] = []

    async def get(self, path, params=None):
        assert path == "/api/v4/leads/pipelines"
        return {"_embedded": {"pipelines": self.pipelines}}

    async def get_all(self, path, key, *, params=None):
        fields = dict(params)
        pipeline_id = int(fields["filter[statuses][0][pipeline_id]"])
        status_id = int(fields["filter[statuses][0][status_id]"])
        self.asked.append((pipeline_id, status_id))
        return list(self.leads.get(status_id, []))


def a_pipeline(pipeline_id: int, statuses: list[tuple[int, str]]) -> dict:
    return {"id": pipeline_id,
            "_embedded": {"statuses": [{"id": sid, "name": name}
                                       for sid, name in statuses]}}


# --- отбор ---

def test_forgotten_leads_are_the_old_ones():
    """Старше порога — забытые, моложе — рабочие."""
    leads = [a_lead(1, date(2024, 9, 11)), a_lead(2, date(2026, 8, 20))]

    doomed = select_forgotten(leads, TODAY, older_than_days=365)

    assert [lead["id"] for lead in doomed] == [1]


def test_a_lead_without_a_creation_date_is_never_touched():
    """Возраст неизвестен — не трогаем. Закрыть живой заказ дороже.

    Пустая дата заведения встречается у старых записей амо, и «раз дата
    пустая, значит древняя» — ровно то допущение, которое закрывает чужую
    работающую сделку.
    """
    doomed = select_forgotten([a_lead(1)], TODAY, older_than_days=365)

    assert doomed == []


def test_oldest_go_first():
    """Список идёт от самых древних: их владелец закрывает без сомнений."""
    leads = [a_lead(1, date(2023, 1, 1)), a_lead(2, date(2021, 12, 7)),
             a_lead(3, date(2022, 6, 13))]

    doomed = select_forgotten(leads, TODAY, older_than_days=365)

    assert [lead["id"] for lead in doomed] == [2, 3, 1]


def test_the_threshold_is_exclusive():
    """Ровно на пороге сделка ещё не забыта — граница трактуется мягко."""
    exactly = a_lead(1, date(2025, 9, 2))              # ровно 365 дней

    assert select_forgotten([exactly], TODAY, older_than_days=365) == []
    assert select_forgotten([exactly], TODAY, older_than_days=364) != []


def test_age_and_creation_day_survive_broken_data():
    """Мусор в дате не роняет разбор: сделка просто считается без возраста."""
    assert created_day({"created_at": "не число"}) is None
    assert age_days({"created_at": None}, TODAY) is None
    assert age_days(a_lead(1, date(2026, 8, 3)), TODAY) == 30


# --- чтение воронок ---

async def test_only_working_pipelines_are_scanned():
    """Ковры и архивные воронки не наши: робот в них не работает вовсе."""
    amo = FakePipelines(
        pipelines=[
            a_pipeline(ids.PIPELINE_REALIZATION,
                       [(ids.REAL_STAGE_CREATED, "Заказ оформлен"),
                        (ids.STATUS_SUCCESS, "Успешно реализовано")]),
            a_pipeline(ids.PIPELINE_CARPETS, [(42638824, "Неразобранное")]),
        ],
        leads={})

    stages = await open_stages(amo)

    assert list(stages) == [ids.REAL_STAGE_CREATED]     # финальные и ковры отброшены
    assert stages[ids.REAL_STAGE_CREATED][1] == "Заказ оформлен"


async def test_final_stages_are_not_open():
    """«Успешно реализовано» и «Закрыто и не реализовано» — не наша забота."""
    amo = FakePipelines(
        pipelines=[a_pipeline(ids.PIPELINE_PRIMARY,
                              [(ids.PRIM_STAGE_NEW_LEAD, "Новый лид"),
                               (ids.STATUS_CLOSED, "Закрыто и не реализовано")])],
        leads={})

    stages = await open_stages(amo)

    assert ids.STATUS_CLOSED not in stages


async def test_leads_are_collected_once_per_lead():
    """Сделка, попавшая в два ответа, считается один раз."""
    stages = {ids.REAL_STAGE_CREATED: (ids.PIPELINE_REALIZATION, "Заказ оформлен"),
              ids.REAL_STAGE_CONFIRMED: (ids.PIPELINE_REALIZATION, "Мастер назначен")}
    amo = FakePipelines(pipelines=[], leads={
        ids.REAL_STAGE_CREATED: [a_lead(1, date(2024, 1, 1))],
        ids.REAL_STAGE_CONFIRMED: [a_lead(1, date(2024, 1, 1)),
                                   a_lead(2, date(2024, 2, 1))],
    })

    leads = await fetch_open_leads(amo, stages)

    assert sorted(lead["id"] for lead in leads) == [1, 2]
    assert len(amo.asked) == 2                          # по запросу на этап


# --- закрытие ---

@pytest.fixture
def closer():
    from scripts.close_stale import _close
    return _close


async def test_closing_moves_to_the_final_status_and_leaves_a_note(closer):
    """Закрываем статусом «не реализовано» и помечаем, кто и почему закрыл."""
    from tests.fakes import FakeAmo

    amo = FakeAmo()
    lead = a_lead(29174771, date(2024, 9, 11))

    closed, failed = await closer(amo, [lead], TODAY)

    assert (closed, failed) == (1, 0)
    assert amo.calls_of("move_lead") == [
        (29174771, ids.PIPELINE_REALIZATION, ids.STATUS_CLOSED)]
    _lead_id, note = amo.calls_of("add_note")[0]
    assert "11.09.2024" in note                         # с какого числа висела
    assert "вручную" in note                            # что делать, если ошиблись


def test_the_year_decides_the_final_status():
    """Заведённые с 2025 года — успех, всё что раньше — «не реализовано».

    Владелец посмотрел карточки за разные годы: ранние — брошенные
    договорённости, поздние — выполненные работы, которые не довели в CRM.
    """
    from scripts.close_stale import target_status

    border = date(2025, 1, 1)

    assert target_status(a_lead(1, date(2024, 12, 31)), border) == ids.STATUS_CLOSED
    assert target_status(a_lead(2, date(2025, 1, 1)), border) == ids.STATUS_SUCCESS
    assert target_status(a_lead(3, date(2025, 8, 17)), border) == ids.STATUS_SUCCESS


def test_without_a_border_nothing_goes_into_revenue():
    """Границы не задали — закрываем как несостоявшиеся.

    Записать чужую работу в выручку по умолчанию нельзя: это тихо исказило бы
    отчётность, а тихих искажений денег в проекте быть не должно.
    """
    from scripts.close_stale import target_status

    assert target_status(a_lead(1, date(2025, 8, 17)), None) == ids.STATUS_CLOSED


def test_a_lead_without_a_date_never_counts_as_revenue():
    """Дата неизвестна — в выручку не пишем, даже если граница задана."""
    from scripts.close_stale import target_status

    assert target_status(a_lead(1), date(2025, 1, 1)) == ids.STATUS_CLOSED


async def test_a_deal_after_the_border_is_marked_as_done(closer):
    """Успешная — своё примечание: «работа считается выполненной»."""
    from tests.fakes import FakeAmo

    amo = FakeAmo()

    closed, failed = await closer(amo, [a_lead(7, date(2025, 6, 27))], TODAY,
                                  date(2025, 1, 1))

    assert (closed, failed) == (1, 0)
    assert amo.calls_of("move_lead") == [
        (7, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS)]
    _lead_id, note = amo.calls_of("add_note")[0]
    assert "выполненной" in note


async def test_one_failure_does_not_stop_the_rest(closer):
    """Амо ответила ошибкой по одной сделке — остальные всё равно закрываем."""
    from adminbot.amo.client import AmoError
    from tests.fakes import FakeAmo

    class Stubborn(FakeAmo):
        async def move_lead(self, lead_id, pipeline_id, status_id):
            if lead_id == 2:
                raise AmoError(400, "amo не в духе")
            return await super().move_lead(lead_id, pipeline_id, status_id)

    amo = Stubborn()
    leads = [a_lead(1, date(2023, 1, 1)), a_lead(2, date(2023, 2, 1)),
             a_lead(3, date(2023, 3, 1))]

    closed, failed = await closer(amo, leads, TODAY)

    assert (closed, failed) == (2, 1)
    assert [call[0] for call in amo.calls_of("add_note")] == [1, 3]


async def test_preview_changes_nothing_in_crm(closer):
    """Просмотр обязан быть немым: он и нужен, чтобы решиться.

    Клиент амо в режиме просмотра намерение возвращает, но запрос не шлёт —
    проверяем по самой сделке: её этап остался прежним.
    """
    from tests.fakes import FakeAmo

    amo = FakeAmo(dry_run=True)
    amo.add_lead(1, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CONFIRMED)

    await closer(amo, [a_lead(1, date(2023, 1, 1))], TODAY)

    assert amo.leads[1]["status_id"] == ids.REAL_STAGE_CONFIRMED   # не закрыта
