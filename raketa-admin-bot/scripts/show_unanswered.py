"""Неотвеченные сообщения в чатах amoCRM за последние две недели. Только чтение.

Просьба владельца 2026-10-29. Как считается «неотвечено» —
`adminbot/amo/unanswered.py`. Две кучки: клиенту не ответили вовсе и после
клиента писал только робот (автосообщение амо, сейлзбот).

Запуск на сервере:

    sudo raketa-admin-bot-update --unanswered
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from adminbot.amo.client import MAX_PAGES, AmoClient, AmoError
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.amo.unanswered import (
    EVENTS_PAGE, UNANSWERED_DAYS, Waiting, fetch_chat_events, find_waiting,
    outgoing_authors, stage_names, user_names)
from adminbot.config import Settings


async def main() -> int:
    settings = Settings.from_env()
    amo = AmoClient(base_url=settings.amo_base_url, token=settings.amo_token,
                    dry_run=True)                       # только чтение
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=UNANSWERED_DAYS)
    try:
        events = await fetch_chat_events(amo, int(since.timestamp()))
        print(f"Сообщений в чатах за {UNANSWERED_DAYS} дней (с "
              f"{since.astimezone(MOSCOW_TZ):%d.%m %H:%M}): {len(events)}")
        if len(events) >= EVENTS_PAGE * MAX_PAGES:
            print(f"⚠ Упёрся в предел {EVENTS_PAGE * MAX_PAGES} событий — "
                  f"самые свежие могли не попасть в выборку.")

        await _print_authors(amo, events)

        waiting = find_waiting(events)
        stages, leads = await _lead_stages(amo, waiting)
        silent = [w for w in waiting if not w.robot_replied]
        robot = [w for w in waiting if w.robot_replied]

        print(f"\nБез ответа: {len(silent)}")
        for item in silent:
            print("  " + _line(item, now, stages, leads, settings.amo_base_url))
        print(f"\nПосле клиента писал только робот: {len(robot)}")
        for item in robot:
            print("  " + _line(item, now, stages, leads, settings.amo_base_url))
    finally:
        await amo.close()
    return 0


async def _print_authors(amo, events) -> None:
    authors = outgoing_authors(events)
    if not authors:
        return
    try:
        names = await user_names(amo)
    except AmoError:
        names = {}
    parts = [f"{'робот' if uid == 0 else names.get(uid, f'пользователь {uid}')} — {count}"
             for uid, count in sorted(authors.items(), key=lambda pair: -pair[1])]
    print("Кто писал исходящие: " + ", ".join(parts))


async def _lead_stages(amo, waiting: list[Waiting]):
    lead_ids = [w.entity_id for w in waiting if w.entity_type == "lead"]
    if not lead_ids:
        return {}, {}
    stages = await stage_names(amo)
    leads = {int(lead["id"]): lead for lead in await amo.get_leads_by_ids(lead_ids)}
    return stages, leads


def _ago(stamp: int, now: datetime) -> str:
    hours = int((now.timestamp() - stamp) // 3600)
    return f"{hours} ч" if hours < 48 else f"{hours // 24} дн"


def _line(item: Waiting, now: datetime, stages, leads, base_url: str) -> str:
    when = datetime.fromtimestamp(item.since, tz=MOSCOW_TZ)
    if item.entity_type == "lead":
        lead = leads.get(item.entity_id) or {}
        stage = stages.get((int(lead.get("pipeline_id") or 0),
                            int(lead.get("status_id") or 0)), "этап неизвестен")
        where = f"{stage} · {base_url}/leads/detail/{item.entity_id}"
    elif item.entity_type == "contact":
        where = f"контакт без сделки · {base_url}/contacts/detail/{item.entity_id}"
    else:
        where = f"{item.entity_type} #{item.entity_id}"
    return (f"ждёт с {when:%d.%m %H:%M} ({_ago(item.since, now)}) · "
            f"сообщений клиента: {item.messages} · {where}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
