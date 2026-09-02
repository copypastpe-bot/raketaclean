"""Закрыть сделки, забытые в CRM. По умолчанию — только показать.

Решение владельца 2026-09-02: сделки старше года закрыть как «Закрыто и не
реализовано» (статус 143) и оставить в каждой примечание, чтобы потом было
видно, кто и почему их закрыл.

Зачем. Забытая сделка выглядит для робота незаконченной работой: из-за такой
он 2026-09-02 спросил владельца по записи на 06.09.2026, хотя живых открытых
сделок у клиента не было. Замер в тот день: 60 забытых из 94 незакрытых,
51 из них старше года, вся куча — в воронке реализации на этапах
«Заказ подтверждён, мастер назначен» и «Заказ оформлен».

Запуск на сервере:

    sudo raketa-admin-bot-update --close-stale         показать, что закроется
    sudo raketa-admin-bot-update --close-stale-live    закрыть по-настоящему

Порог возраста меняется ключом `--close-stale-days=N` (по умолчанию 365).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from adminbot.amo import ids
from adminbot.amo.client import AmoClient, AmoError
from adminbot.amo.stale import (
    PIPELINE_NAMES, created_day, fetch_open_leads, open_stages, select_forgotten)
from adminbot.config import Settings

# Возраст по умолчанию: год. Владелец согласился закрывать именно такие.
DEFAULT_DAYS = 365

# Предохранитель от промаха: если под правило попало больше сделок, чем здесь,
# скрипт остановится и попросит поднять предел явно. Массовое закрытие чужих
# сделок должно быть решением человека, а не следствием опечатки в ключе.
SAFETY_LIMIT = 200

NOTE_TEXT = ("🤖 Закрыто при чистке CRM {today}: сделка висела незакрытой "
             "с {since} ({days} дн.). Если заказ был выполнен, поправьте статус вручную.")


async def main() -> int:
    live = os.environ.get("CLOSE_STALE_LIVE", "").strip() in ("1", "true")
    days = int(os.environ.get("CLOSE_STALE_DAYS", "") or DEFAULT_DAYS)
    limit = int(os.environ.get("CLOSE_STALE_LIMIT", "") or SAFETY_LIMIT)

    settings = Settings.from_env()
    amo = AmoClient(base_url=settings.amo_base_url, token=settings.amo_token,
                    dry_run=not live)
    today = datetime.now(timezone.utc).date()
    print("Режим: " + ("БОЕВОЙ — сделки будут закрыты" if live
                       else "ПРОСМОТР — в CRM ничего не меняется"))
    print(f"Порог: старше {days} дней\n")

    try:
        stages = await open_stages(amo)
        if not stages:
            print("Не удалось прочитать воронки amoCRM — проверьте токен.")
            return 1

        leads = await fetch_open_leads(amo, stages)
        doomed = select_forgotten(leads, today, older_than_days=days)
        print(f"Незакрытых сделок всего: {len(leads)}, из них старше {days} дней: "
              f"{len(doomed)}\n")
        if not doomed:
            return 0

        _print_plan(doomed, stages, today, base_url=settings.amo_base_url)

        if len(doomed) > limit:
            print(f"\nОстановился: под правило попало {len(doomed)} сделок, "
                  f"а предел — {limit}. Если это ожидаемо, поднимите предел ключом "
                  f"--close-stale-limit=N.")
            return 2

        if not live:
            print("\nЭто был просмотр. Закрыть по-настоящему: "
                  "sudo raketa-admin-bot-update --close-stale-live")
            return 0

        closed, failed = await _close(amo, doomed, today)
        print(f"\nЗакрыто: {closed}. Не удалось: {failed}.")
        return 0 if not failed else 1
    finally:
        await amo.close()


def _print_plan(doomed: list[dict], stages: dict[int, tuple[int, str]], today,
                *, base_url: str) -> None:
    by_stage: Counter[str] = Counter()
    total_price = Decimal(0)
    for lead in doomed:
        _pipeline_id, stage = stages.get(int(lead.get("status_id") or 0),
                                         (0, str(lead.get("status_id"))))
        by_stage[stage] += 1
        total_price += Decimal(str(lead.get("price") or 0))

    print("Что закроется, по этапам:")
    for stage, count in by_stage.most_common():
        print(f"  {stage}: {count}")
    print(f"Сумма бюджетов этих сделок: {total_price}\n")

    print("Список (от самых старых):")
    for lead in doomed:
        day = created_day(lead)
        age = (today - day).days if day else "—"
        pipeline_id, stage = stages.get(int(lead.get("status_id") or 0),
                                        (0, str(lead.get("status_id"))))
        print(f"  #{lead.get('id')} · {day} ({age} дн.) · "
              f"{PIPELINE_NAMES.get(pipeline_id, pipeline_id)} · {stage} · "
              f"бюджет {lead.get('price') or 0} · "
              f"{base_url}/leads/detail/{lead.get('id')}")


async def _close(amo: Any, doomed: list[dict], today) -> tuple[int, int]:
    """Закрыть отобранные сделки. Одна ошибка не останавливает остальные."""
    closed = 0
    failed = 0
    for lead in doomed:
        lead_id = int(lead.get("id") or 0)
        pipeline_id = int(lead.get("pipeline_id") or 0)
        day = created_day(lead)
        try:
            await amo.move_lead(lead_id, pipeline_id, ids.STATUS_CLOSED)
            await amo.add_note(lead_id, NOTE_TEXT.format(
                today=f"{today:%d.%m.%Y}",
                since=f"{day:%d.%m.%Y}" if day else "неизвестной даты",
                days=(today - day).days if day else "?"))
            closed += 1
            print(f"  закрыта #{lead_id}")
        except AmoError as exc:
            failed += 1
            print(f"  НЕ закрыта #{lead_id}: {exc}")
    return closed, failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
