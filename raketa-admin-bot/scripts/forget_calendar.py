"""Забыть запись календаря — разбор последствий, когда запись уже удалена.

Зачем нужен. Робот держит правило «одна сделка — одна запись календаря»: сделки,
закреплённые за другими записями, он из поиска выбрасывает. Запись, удалённую из
календаря, он из этого списка не вычёркивает — она остаётся в базе вместе со
сделкой. Пока это так, вернувшийся заказ того же клиента считается новым: старая
сделка «занята» мёртвой записью, кандидатов не остаётся, и робот молча заводит
вторую сделку. Ровно это случилось 2026-09-06 (запись от 31.08 держала сделку
31587353, для новой записи была заведена 31613297).

Прогнать такую запись заново нельзя: `--gcal-run` ищет её в календарях, а её там
уже нет. Поэтому мёртвую запись нужно забыть отдельно — тогда сделка снова
свободна, и повторный прогон живой записи привяжется к ней.

Забываем и незаконченные письма владельцу по этой записи: отчёт о работе, которой
больше нет, — мусор в очереди.

Запуск на сервере:

    sudo raketa-admin-bot-update --gcal-forget=<id записи>[,<id>...]   показать
    sudo raketa-admin-bot-update --gcal-forget=<id записи> --gcal-forget-live   забыть

Без `--gcal-forget-live` скрипт только показывает, что будет забыто: удаление
памяти о записи необратимо, а восстановить её из календаря нечем.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

from adminbot import db
from adminbot.config import Settings
from adminbot.gcal.store import PgCalendarStore
from adminbot.phone import mask

# Состояния, из которых робот сам уже не выйдет: забывать такие безопасно.
SETTLED_STATUSES = ("cancelled", "skipped", "done", "error")

DROP_REASON = "нужда отпала: запись забыта"


async def main() -> int:
    wanted = [item.strip() for item in os.environ.get("GCAL_FORGET_IDS", "").split(",")
              if item.strip()]
    if not wanted:
        print("Не указано, какие записи забыть (GCAL_FORGET_IDS).")
        return 2

    live = os.environ.get("GCAL_FORGET_LIVE", "").strip() in ("1", "true")
    settings = Settings.from_env()
    pool = await db.create_pool(settings.own_db_dsn)
    store = PgCalendarStore(pool)

    print("Режим:", "ЗАБЫВАЮ (память робота меняется)" if live else "просмотр (ничего не меняю)")

    try:
        for event_id in wanted:
            await _forget_one(pool, store, event_id, live)
    finally:
        await pool.close()

    if not live:
        print("\nЧтобы забыть эти записи, повторите команду с --gcal-forget-live.")
    return 0


async def _forget_one(pool, store: PgCalendarStore, event_id: str, live: bool) -> None:
    print(f"\n=== {event_id} ===")
    link = await store.get(event_id)
    if link is None:
        print("   робот такой записи не помнит — забывать нечего")
        return

    print(f"   вид: {link.kind}, состояние: {link.status}, путь: {link.path or '—'}")
    print(f"   клиент: {link.client_name or '—'}, телефон {mask(link.phone10)}, "
          f"дата заказа: {link.order_date or '—'}")
    for title, lead_id in (("лид первичной", link.primary_lead_id),
                           ("сделка", link.real_lead_id)):
        if lead_id:
            print(f"   {title}: {lead_id} — освободится и снова станет свободной для поиска")
    if not link.primary_lead_id and not link.real_lead_id:
        print("   сделок за записью не числится — на поиск её забвение не влияет")
    if link.skip_reason:
        print(f"   примечание: {link.skip_reason}")
    if link.status == "waiting_owner":
        print("   ⚠ по записи висит вопрос владельцу: кнопки карточки перестанут работать")
    if link.status not in SETTLED_STATUSES:
        print(f"   ⚠ запись ещё в работе ({link.status}) — робот пройдёт её заново "
              f"и может завести вторую сделку")

    letters = await db.fetch_owner_letters_for(pool, event_id)
    if letters:
        print(f"   в очереди на отправку: {len(letters)}")
        for letter in letters:
            print(f"     · {letter['kind']}, попыток {letter['attempts']}: "
                  f"{(letter['preview'] or '').splitlines()[0][:60]}")
    else:
        print("   в очереди на отправку по этой записи ничего не ждёт")

    if not live:
        return

    now = datetime.now(timezone.utc)
    for letter in letters:
        await db.drop_owner_letter(pool, letter["id"], now, DROP_REASON)
    if letters:
        print(f"   долги погашены: {len(letters)}")
    await store.forget(event_id)
    print("   запись забыта")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
