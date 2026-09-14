"""Закрыть сделки, забытые в CRM. По умолчанию — только показать.

Решение владельца 2026-09-02 (он посмотрел выборку карточек за разные годы):
сделки старше года закрыть, **заведённые до 2025 года — как «Закрыто и не
реализовано»** (брошенные договорённости), **с 2025 года — как «Успешно
реализовано»** (работы были сделаны). В каждой остаётся примечание, чтобы
потом было видно, кто и почему её закрыл.

Зачем. Забытая сделка выглядит для робота незаконченной работой: из-за такой
он 2026-09-02 спросил владельца по записи на 06.09.2026, хотя живых открытых
сделок у клиента не было. Замер в тот день: 60 забытых из 94 незакрытых,
51 из них старше года, вся куча — в воронке реализации на этапах
«Заказ подтверждён, мастер назначен» и «Заказ оформлен».

Запуск на сервере:

    sudo raketa-admin-bot-update --close-stale         показать, что закроется
    sudo raketa-admin-bot-update --close-stale-live    закрыть по-настоящему

Порог возраста меняется ключом `--close-stale-days=N` (по умолчанию 365),
граница «успеха» — ключом `--close-stale-success-from=ГГГГ-ММ-ДД`. Без неё
закрываются все как несостоявшиеся: записывать чужую работу в выручку по
умолчанию нельзя.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

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

NOTE_LOST = ("🤖 Закрыто при чистке CRM {today}: сделка висела незакрытой "
             "с {since} ({days} дн.). Если заказ был выполнен, поправьте статус вручную.")

NOTE_WON = ("🤖 Проведена при чистке CRM {today}: сделка висела незакрытой "
            "с {since} ({days} дн.), работа считается выполненной. "
            "Если заказа не было, поправьте статус вручную.")


async def main() -> int:
    live = os.environ.get("CLOSE_STALE_LIVE", "").strip() in ("1", "true")
    days = int(os.environ.get("CLOSE_STALE_DAYS", "") or DEFAULT_DAYS)
    limit = int(os.environ.get("CLOSE_STALE_LIMIT", "") or SAFETY_LIMIT)
    success_from = _parse_day(os.environ.get("CLOSE_STALE_SUCCESS_FROM", ""))

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

        _print_plan(doomed, stages, today, base_url=settings.amo_base_url,
                    success_from=success_from)

        if len(doomed) > limit:
            print(f"\nОстановился: под правило попало {len(doomed)} сделок, "
                  f"а предел — {limit}. Если это ожидаемо, поднимите предел ключом "
                  f"--close-stale-limit=N.")
            return 2

        if not live:
            print("\nЭто был просмотр. Закрыть по-настоящему: "
                  "sudo raketa-admin-bot-update --close-stale-live")
            return 0

        closed, failed = await _close(amo, doomed, today, success_from)
        print(f"\nЗакрыто: {closed}. Не удалось: {failed}.")
        return 0 if not failed else 1
    finally:
        await amo.close()


def _parse_day(value: str) -> Optional[date]:
    """Граница «с этой даты считаем выполненными». Пусто — границы нет."""
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def _print_plan(doomed: list[dict], stages: dict[int, tuple[int, str]], today,
                *, base_url: str, success_from: Optional[date]) -> None:
    by_stage: Counter[str] = Counter()
    counts: Counter[int] = Counter()
    money: dict[int, Decimal] = {ids.STATUS_CLOSED: Decimal(0),
                                 ids.STATUS_SUCCESS: Decimal(0)}
    for lead in doomed:
        _pipeline_id, stage = stages.get(int(lead.get("status_id") or 0),
                                         (0, str(lead.get("status_id"))))
        by_stage[stage] += 1
        status = target_status(lead, success_from)
        counts[status] += 1
        money[status] += Decimal(str(lead.get("price") or 0))

    print("Что закроется, по этапам:")
    for stage, count in by_stage.most_common():
        print(f"  {stage}: {count}")

    print("\nПо итогу сделки:")
    print(f"  «Закрыто и не реализовано»: {counts[ids.STATUS_CLOSED]} "
          f"на {money[ids.STATUS_CLOSED]} ₽ (уйдёт в потери)")
    print(f"  «Успешно реализовано»: {counts[ids.STATUS_SUCCESS]} "
          f"на {money[ids.STATUS_SUCCESS]} ₽ (уйдёт в выручку)")
    if success_from:
        print(f"  граница: заведённые с {success_from:%d.%m.%Y} считаем выполненными")

    print("\nСписок (от самых старых):")
    for lead in doomed:
        day = created_day(lead)
        age = (today - day).days if day else "—"
        pipeline_id, stage = stages.get(int(lead.get("status_id") or 0),
                                        (0, str(lead.get("status_id"))))
        mark = ("успех" if target_status(lead, success_from) == ids.STATUS_SUCCESS
                else "не реализовано")
        print(f"  #{lead.get('id')} · {day} ({age} дн.) · "
              f"{PIPELINE_NAMES.get(pipeline_id, pipeline_id)} · {stage} · "
              f"бюджет {lead.get('price') or 0} · → {mark} · "
              f"{base_url}/leads/detail/{lead.get('id')}")


def target_status(lead: dict, success_from: Optional[date]) -> int:
    """Каким статусом закрывать эту сделку.

    Решение владельца 2026-09-02, после того как он посмотрел выборку карточек:
    заведённые до 2025 года — «Закрыто и не реализовано» (это брошенные
    договорённости), с 2025 года — «Успешно реализовано» (работы были).
    Границы нет — закрываем всё как несостоявшееся, это прежнее правило.
    """
    if success_from is None:
        return ids.STATUS_CLOSED
    day = created_day(lead)
    if day is None:
        return ids.STATUS_CLOSED           # без даты не рискуем записывать в выручку
    return ids.STATUS_SUCCESS if day >= success_from else ids.STATUS_CLOSED


async def _close(amo: Any, doomed: list[dict], today,
                 success_from: Optional[date] = None) -> tuple[int, int]:
    """Закрыть отобранные сделки. Одна ошибка не останавливает остальные."""
    closed = 0
    failed = 0
    for lead in doomed:
        lead_id = int(lead.get("id") or 0)
        pipeline_id = int(lead.get("pipeline_id") or 0)
        day = created_day(lead)
        status = target_status(lead, success_from)
        note = NOTE_WON if status == ids.STATUS_SUCCESS else NOTE_LOST
        try:
            await amo.move_lead(lead_id, pipeline_id, status)
            await amo.add_note(lead_id, note.format(
                today=f"{today:%d.%m.%Y}",
                since=f"{day:%d.%m.%Y}" if day else "неизвестной даты",
                days=(today - day).days if day else "?"))
            closed += 1
            print(f"  {'проведена' if status == ids.STATUS_SUCCESS else 'закрыта'} "
                  f"#{lead_id}")
        except AmoError as exc:
            failed += 1
            print(f"  НЕ закрыта #{lead_id}: {exc}")
    return closed, failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
