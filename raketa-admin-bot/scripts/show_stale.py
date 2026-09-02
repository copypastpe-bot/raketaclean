"""Сколько в CRM висит незакрытых сделок и какого они возраста.

Диагностика, ничего не меняет. Нужна затем, что робот считает открытой любую
сделку, которую не провели и не закрыли, — а забытая сделка выглядит для него
незаконченной работой сколь угодно долго. Живой случай 2026-09-02: по записи
календаря на 06.09.2026 робот спросил владельца, потому что у клиента висела
сделка от 11.09.2024 на этапе «Заказ подтверждён, мастер назначен».

Запуск на сервере:

    sudo raketa-admin-bot-update --stale

Считаются две рабочие воронки — первичная и реализация. Ковры и архивные
воронки робот и так не трогает (`ids.PIPELINES_IGNORED`).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any, Optional

from adminbot.amo import ids
from adminbot.amo.client import AmoClient
from adminbot.config import Settings

# Возраст, начиная с которого сделка считается забытой, а не рабочей.
# Полгода — решение владельца 2026-09-02: заказ такой давности закрыт
# в жизни, но не в CRM, и новую запись календаря к нему не привязать.
FORGOTTEN_DAYS = 180

PIPELINE_NAMES = {
    ids.PIPELINE_PRIMARY: "первичная",
    ids.PIPELINE_REALIZATION: "реализация",
}

# Сколько самых старых сделок показать поимённо: список нужен, чтобы владелец
# мог открыть их в CRM и закрыть, а не просто узнать число.
SHOW_OLDEST = 15


async def main() -> int:
    settings = Settings.from_env()
    amo = AmoClient(base_url=settings.amo_base_url, token=settings.amo_token,
                    dry_run=True)                       # только чтение
    today = datetime.now(timezone.utc).date()
    try:
        stages = await _open_stages(amo)
        if not stages:
            print("Не удалось прочитать воронки amoCRM — проверьте токен.")
            return 1

        leads = await _open_leads(amo, stages)
        print(f"Незакрытых сделок в рабочих воронках: {len(leads)}\n")
        if not leads:
            return 0

        aged = sorted(((_age_days(lead, today), lead) for lead in leads),
                      key=lambda pair: -(pair[0] or 0))
        _print_buckets(aged)
        _print_by_stage(leads, stages)
        _print_oldest(aged, stages, base_url=settings.amo_base_url)
    finally:
        await amo.close()
    return 0


async def _open_stages(amo: AmoClient) -> dict[int, tuple[int, str]]:
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


async def _open_leads(amo: AmoClient, stages: dict[int, tuple[int, str]]) -> list[dict]:
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


def _age_days(lead: dict, today: date) -> Optional[int]:
    created = lead.get("created_at")
    if not created:
        return None
    day = datetime.fromtimestamp(int(created), tz=timezone.utc).date()
    return (today - day).days


def _print_buckets(aged: list[tuple[Optional[int], dict]]) -> None:
    buckets = Counter()
    for age, _lead in aged:
        if age is None:
            buckets["возраст неизвестен"] += 1
        elif age <= 30:
            buckets["до месяца — рабочие"] += 1
        elif age <= 90:
            buckets["1–3 месяца"] += 1
        elif age <= FORGOTTEN_DAYS:
            buckets["3–6 месяцев"] += 1
        elif age <= 365:
            buckets["полгода–год — забытые"] += 1
        else:
            buckets["больше года — забытые"] += 1

    print("По возрасту:")
    for name in ("до месяца — рабочие", "1–3 месяца", "3–6 месяцев",
                 "полгода–год — забытые", "больше года — забытые",
                 "возраст неизвестен"):
        if buckets[name]:
            print(f"  {name}: {buckets[name]}")

    forgotten = sum(1 for age, _ in aged if age is not None and age > FORGOTTEN_DAYS)
    print(f"\nСтарше {FORGOTTEN_DAYS} дней (робот будет заводить новую сделку "
          f"молча): {forgotten}")


def _print_by_stage(leads: list[dict], stages: dict[int, tuple[int, str]]) -> None:
    counts = Counter(int(lead.get("status_id") or 0) for lead in leads)
    print("\nПо этапам:")
    for status_id, count in counts.most_common():
        pipeline_id, name = stages.get(status_id, (0, str(status_id)))
        print(f"  {PIPELINE_NAMES.get(pipeline_id, pipeline_id)} · {name}: {count}")


def _print_oldest(aged: list[tuple[Optional[int], dict]],
                  stages: dict[int, tuple[int, str]], *, base_url: str) -> None:
    print(f"\nСамые старые ({SHOW_OLDEST}):")
    for age, lead in aged[:SHOW_OLDEST]:
        created = lead.get("created_at")
        when = (datetime.fromtimestamp(int(created), tz=timezone.utc).date()
                if created else "—")
        _pipeline_id, stage = stages.get(int(lead.get("status_id") or 0),
                                         (0, str(lead.get("status_id"))))
        print(f"  #{lead.get('id')} · заведена {when} ({age} дн.) · {stage} · "
              f"{base_url}/leads/detail/{lead.get('id')}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
