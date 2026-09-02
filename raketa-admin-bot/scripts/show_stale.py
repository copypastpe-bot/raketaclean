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
import sys
from collections import Counter
from datetime import date, datetime, timezone
from typing import Optional

from adminbot.amo.client import AmoClient
from adminbot.amo.stale import (
    FORGOTTEN_DAYS, PIPELINE_NAMES, age_days, fetch_open_leads, open_stages)
from adminbot.config import Settings

# Сколько самых старых сделок показать поимённо: список нужен, чтобы владелец
# мог открыть их в CRM и закрыть, а не просто узнать число.
SHOW_OLDEST = 15


async def main() -> int:
    settings = Settings.from_env()
    amo = AmoClient(base_url=settings.amo_base_url, token=settings.amo_token,
                    dry_run=True)                       # только чтение
    today = datetime.now(timezone.utc).date()
    try:
        stages = await open_stages(amo)
        if not stages:
            print("Не удалось прочитать воронки amoCRM — проверьте токен.")
            return 1

        leads = await fetch_open_leads(amo, stages)
        print(f"Незакрытых сделок в рабочих воронках: {len(leads)}\n")
        if not leads:
            return 0

        aged = sorted(((age_days(lead, today), lead) for lead in leads),
                      key=lambda pair: -(pair[0] or 0))
        _print_buckets(aged)
        _print_by_stage(aged, stages)
        _print_oldest(aged, stages, base_url=settings.amo_base_url)
    finally:
        await amo.close()
    return 0


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


def _print_by_stage(aged: list[tuple[Optional[int], dict]],
                    stages: dict[int, tuple[int, str]]) -> None:
    """По воронкам и этапам — и сколько на каждом забытых.

    Владельцу это нужнее общего числа: по этапу видно, что именно не доделано.
    «Заказ выполнен» без закрытия — забыли провести; «Новый лид» годовой
    давности — заявка, до которой не дошли руки.
    """
    totals: Counter[int] = Counter()
    forgotten: Counter[int] = Counter()
    by_pipeline: Counter[int] = Counter()
    forgotten_by_pipeline: Counter[int] = Counter()

    for age, lead in aged:
        status_id = int(lead.get("status_id") or 0)
        pipeline_id = int(lead.get("pipeline_id") or 0)
        totals[status_id] += 1
        by_pipeline[pipeline_id] += 1
        if age is not None and age > FORGOTTEN_DAYS:
            forgotten[status_id] += 1
            forgotten_by_pipeline[pipeline_id] += 1

    print("\nПо воронкам (всего / из них старше полугода):")
    for pipeline_id, count in by_pipeline.most_common():
        name = PIPELINE_NAMES.get(pipeline_id, str(pipeline_id))
        print(f"  {name}: {count} / {forgotten_by_pipeline[pipeline_id]}")

    print("\nПо этапам (всего / из них старше полугода):")
    for status_id, count in totals.most_common():
        pipeline_id, name = stages.get(status_id, (0, str(status_id)))
        print(f"  {PIPELINE_NAMES.get(pipeline_id, pipeline_id)} · {name}: "
              f"{count} / {forgotten[status_id]}")


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
