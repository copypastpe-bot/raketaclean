"""Закрыть «Новые лиды», до которых не дошли. По умолчанию — только показать.

Решение владельца 2026-10-29: сделки этапа «Новый лид» старше недели, по
телефонам которых в календаре нет заказа, закрыть как «Закрыто и не
реализовано» с причиной «Пропала потребность». В каждой остаётся примечание,
чтобы потом было видно, кто и почему её закрыл. Что считается заказом и какие
сделки не трогаются — `adminbot/amo/new_leads.py`.

Запуск на сервере:

    sudo raketa-admin-bot-update --close-new-leads         показать, что закроется
    sudo raketa-admin-bot-update --close-new-leads-live    закрыть по-настоящему

Боевой запуск отбирает сделки заново: что успел взять в работу менеджер
между просмотром и закрытием, уже не в «Новом лиде» и не закроется.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime
from typing import Any, Optional

from adminbot import db
from adminbot.amo import ids
from adminbot.amo.client import AmoClient, AmoError
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.amo.new_leads import (
    LOSS_REASON_NAME, NEW_LEAD_DAYS, Plan, created_day_msk, fetch_calendar_marks,
    fetch_contact_phones, fetch_new_leads, find_loss_reason, lead_contact_ids,
    plan_cleanup)
from adminbot.amo.stale import select_forgotten
from adminbot.config import Settings
from adminbot.phone import mask

SAFETY_LIMIT = 200

NOTE = "🤖 Закрыто при чистке CRM {today}: заказов в календаре по телефону нет."


async def main() -> int:
    live = os.environ.get("CLOSE_NEW_LEADS_LIVE", "").strip() in ("1", "true")
    settings = Settings.from_env()
    amo = AmoClient(base_url=settings.amo_base_url, token=settings.amo_token,
                    dry_run=not live)
    pool = await db.create_pool(settings.own_db_dsn)
    today = datetime.now(MOSCOW_TZ).date()
    print("Режим: " + ("БОЕВОЙ — сделки будут закрыты" if live
                       else "ПРОСМОТР — в CRM ничего не меняется"))
    print(f"Порог: «Новый лид» старше {NEW_LEAD_DAYS} дней\n")

    try:
        reasons = await amo.get_loss_reasons()
        reason_id = find_loss_reason(reasons, LOSS_REASON_NAME)
        if reason_id is None:
            names = ", ".join(f"«{r.get('name')}»" for r in reasons) or "список пуст"
            print(f"В amoCRM нет причины отказа «{LOSS_REASON_NAME}». Есть: {names}.")
            return 1
        print(f"Причина отказа «{LOSS_REASON_NAME}» найдена (id {reason_id}).")

        leads = await fetch_new_leads(amo)
        aged = select_forgotten(leads, today, older_than_days=NEW_LEAD_DAYS)
        print(f"В «Новом лиде» сделок: {len(leads)}, из них старше "
              f"{NEW_LEAD_DAYS} дней: {len(aged)}\n")
        if not aged:
            return 0

        phones_by_contact = await fetch_contact_phones(
            amo, (cid for lead in aged for cid in lead_contact_ids(lead)))
        all_phones = sorted({p for phones in phones_by_contact.values() for p in phones})
        marks, memory_from = await fetch_calendar_marks(pool, all_phones)
        plan = plan_cleanup(aged, phones_by_contact, marks)
        _print_plan(plan, today, memory_from, base_url=settings.amo_base_url)

        if len(plan.to_close) > SAFETY_LIMIT:
            print(f"\nОстановился: под правило попало {len(plan.to_close)} сделок, "
                  f"а предел — {SAFETY_LIMIT}. Ничего не закрыто.")
            return 2

        if not live:
            print("\nЭто был просмотр. Закрыть по-настоящему: "
                  "sudo raketa-admin-bot-update --close-new-leads-live")
            return 0

        closed, failed = await _close(amo, plan.to_close, reason_id, today)
        print(f"\nЗакрыто: {closed}. Не удалось: {failed}.")
        return 0 if not failed else 1
    finally:
        await amo.close()
        await pool.close()


def _line(lead: dict, today: date, base_url: str) -> str:
    day = created_day_msk(lead)
    age = (today - day).days if day else "—"
    when = f"{day:%d.%m.%Y}" if day else "—"
    return (f"#{lead.get('id')} · заведена {when} ({age} дн.) · "
            f"{base_url}/leads/detail/{lead.get('id')}")


def _print_plan(plan: Plan, today: date, memory_from: Optional[date], *,
                base_url: str) -> None:
    def blind(lead: dict) -> bool:
        day = created_day_msk(lead)
        return memory_from is not None and day is not None and day < memory_from

    print(f"Закрою: {len(plan.to_close)}")
    for lead in plan.to_close:
        print("  " + _line(lead, today, base_url)
              + (" · ⚠ сверка неполная" if blind(lead) else ""))

    print(f"\nНе трогаю — на телефон есть запись в календаре: {len(plan.with_order)}")
    for lead, mark in plan.with_order:
        print(f"  {_line(lead, today, base_url)} · телефон {mask(mark.phone10)} · "
              f"запись на {mark.day:%d.%m.%Y}")

    print(f"\nНе трогаю — у контакта нет телефона: {len(plan.no_phone)}")
    for lead in plan.no_phone:
        print("  " + _line(lead, today, base_url))

    blind_count = sum(1 for lead in plan.to_close if blind(lead))
    if memory_from is not None and blind_count:
        print(f"\n⚠ Календарь робот помнит с {memory_from:%d.%m.%Y}. Сделок, "
              f"заведённых раньше, в списке «Закрою»: {blind_count} — по ним "
              f"заказ до этого дня не виден.")


async def _close(amo: Any, leads: list[dict], reason_id: int,
                 today: date) -> tuple[int, int]:
    """Закрыть отобранные сделки. Одна ошибка не останавливает остальные."""
    closed = 0
    failed = 0
    for lead in leads:
        lead_id = int(lead.get("id") or 0)
        try:
            await amo.move_lead(lead_id, ids.PIPELINE_PRIMARY, ids.STATUS_CLOSED,
                                loss_reason_id=reason_id)
            await amo.add_note(lead_id, NOTE.format(today=f"{today:%d.%m}"))
            closed += 1
            print(f"  закрыта #{lead_id}")
        except AmoError as exc:
            failed += 1
            print(f"  НЕ закрыта #{lead_id}: {exc}")
    return closed, failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
