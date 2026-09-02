"""Незакрытые сделки CRM: как их найти и какие из них забыты.

Робот считает открытой любую сделку, которую не провели и не закрыли. В жизни
это допущение ломается о человеческий фактор: заказ выполнили, а сделку по
воронке не двинули — и она изображает незаконченную работу годами. Замер
2026-09-02: из 94 незакрытых сделок рабочих воронок 60 старше полугода,
51 — старше года, самая древняя заведена 07.12.2021.

Здесь только отбор и счёт. Закрывает сделки `scripts/close_stale.py`,
показывает — `scripts/show_stale.py`; общее у них тут, чтобы «что считается
забытым» было записано в одном месте и проверялось тестами.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional, Sequence

from adminbot.amo import ids

# Возраст, начиная с которого открытая сделка считается забытой, а не рабочей
# (решение владельца 2026-09-02). Полгода: заказ такой давности закрыт в жизни,
# но не в CRM, и новую запись календаря к нему не привязать.
FORGOTTEN_DAYS = 180

# Воронки, где сделки живут и работают. Ковры и архивные робот не трогает вовсе.
PIPELINE_NAMES = {
    ids.PIPELINE_PRIMARY: "первичная",
    ids.PIPELINE_REALIZATION: "реализация",
}


async def open_stages(amo: Any) -> dict[int, tuple[int, str]]:
    """Незавершающие этапы рабочих воронок: id этапа → (воронка, название).

    Список берём у самой амо, а не из справочника проекта: этапы владелец
    заводит сам, и незнакомый этап робот всё равно считает открытым.
    """
    payload = await amo.get("/api/v4/leads/pipelines")
    stages: dict[int, tuple[int, str]] = {}
    for pipeline in ((payload or {}).get("_embedded") or {}).get("pipelines") or []:
        pipeline_id = int(pipeline.get("id") or 0)
        if pipeline_id not in PIPELINE_NAMES:
            continue
        for status in ((pipeline.get("_embedded") or {}).get("statuses") or []):
            status_id = int(status.get("id") or 0)
            if status_id in ids.STATUSES_FINAL:
                continue
            stages[status_id] = (pipeline_id, str(status.get("name") or status_id))
    return stages


async def fetch_open_leads(amo: Any, stages: dict[int, tuple[int, str]]) -> list[dict]:
    """Все сделки на незавершающих этапах. Один запрос на этап — фильтр точный."""
    leads: list[dict] = []
    seen: set[int] = set()
    for status_id, (pipeline_id, _name) in stages.items():
        params: list[tuple[str, Any]] = [
            ("filter[statuses][0][pipeline_id]", pipeline_id),
            ("filter[statuses][0][status_id]", status_id),
        ]
        for lead in await amo.get_all("/api/v4/leads", "leads", params=params):
            lead_id = int(lead.get("id") or 0)
            if lead_id and lead_id not in seen:
                seen.add(lead_id)
                leads.append(lead)
    return leads


def created_day(lead: dict) -> Optional[date]:
    """Когда сделку завели. Нет отметки — возраст неизвестен."""
    created = lead.get("created_at")
    if not created:
        return None
    try:
        return datetime.fromtimestamp(int(created), tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


def age_days(lead: dict, today: date) -> Optional[int]:
    day = created_day(lead)
    return None if day is None else (today - day).days


def select_forgotten(leads: Sequence[dict], today: date, *,
                     older_than_days: int = FORGOTTEN_DAYS) -> list[dict]:
    """Сделки старше порога, от самых древних к молодым.

    Возраст неизвестен — сделка НЕ попадает в отбор. Это осознанная осторожность:
    закрыть живой заказ дороже, чем оставить висеть лишнюю строку.
    """
    aged = [(age_days(lead, today), lead) for lead in leads]
    return [lead for age, lead in sorted(aged, key=lambda pair: -(pair[0] or 0))
            if age is not None and age > older_than_days]
