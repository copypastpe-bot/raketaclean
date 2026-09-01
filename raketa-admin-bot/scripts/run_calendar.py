"""Разовый боевой прогон по конкретным записям календаря.

Зачем нужен: наблюдатель берёт в работу только записи, появившиеся после
включения функции. Иногда владельцу нужно провести именно эту запись — например,
проверить работу на живом заказе. Тогда он называет её явно, и робот проводит
её по всей цепочке: находит или создаёт сделку, заполняет поля, передаёт в работу
и дожидается автосделки сейлзбота.

Запуск на сервере:

    sudo raketa-admin-bot-update --gcal-run=<id записи>[,<id>...]           боевой
    sudo raketa-admin-bot-update --gcal-run=<id записи> --gcal-preview      репетиция

Прогон всегда пишет состояние в базу, поэтому наблюдатель потом продолжит с того
места, где остановился прогон, и ничего не задвоит.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta

from adminbot import db
from adminbot.amo.client import AmoClient
from adminbot.config import Settings
from adminbot.gcal.auth import ServiceAccountToken
from adminbot.gcal.client import GoogleCalendar
from adminbot.gcal.engine import CalendarEngine
from adminbot.gcal.event import EventKind, parse_event
from adminbot.gcal.store import MemoryCalendarStore, PgCalendarStore

LEAD_URL = "{base}/leads/detail/{lead_id}"

# Сколько ждём автосделку сейлзбота, прежде чем оставить работу наблюдателю.
WAIT_TOTAL_SEC = 720
WAIT_STEP_SEC = 30

# Статусы, из которых прогон уже не выйдет сам.
FINAL_STATUSES = ("done", "skipped", "waiting_owner", "cancelled", "error")


async def main() -> int:
    wanted = [item.strip() for item in os.environ.get("GCAL_RUN_IDS", "").split(",")
              if item.strip()]
    if not wanted:
        print("Не указано, какие записи проводить (GCAL_RUN_IDS).")
        return 2

    preview = os.environ.get("GCAL_RUN_PREVIEW", "").strip() in ("1", "true")
    settings = Settings.from_env()

    token = ServiceAccountToken.from_file(settings.gcal_key_file)
    calendar = GoogleCalendar(calendar_id=settings.gcal_calendar_ids[0], token=token)
    amo = AmoClient(base_url=settings.amo_base_url, token=settings.amo_token,
                    dry_run=preview)
    pool = await db.create_pool(settings.own_db_dsn)
    store = MemoryCalendarStore() if preview else PgCalendarStore(pool)
    engine = CalendarEngine(amo=amo, store=store, dry_run=preview,
                            salesbot_wait_sec=settings.salesbot_wait_sec)

    print("Режим:", "РЕПЕТИЦИЯ (в CRM не пишем)" if preview else "БОЕВОЙ")

    try:
        batch = await calendar.fetch(sync_token=None,
                                     sync_from=date.today() - timedelta(days=30))
        found = {raw.get("id"): raw for raw in batch.events}

        for event_id in wanted:
            raw = found.get(event_id)
            if raw is None:
                print(f"\n✗ Запись {event_id} в календаре не найдена.")
                continue
            await _run_one(engine, store, parse_event(raw), settings, preview)
    finally:
        await calendar.close()
        await token.close()
        await amo.close()
        await pool.close()
    return 0


async def _run_one(engine, store, parsed, settings: Settings, preview: bool) -> None:
    print(f"\n=== {parsed.summary or parsed.event_id} ===")
    if parsed.kind is not EventKind.ORDER:
        print(f"Это не заказ ({parsed.kind.value}) — не провожу.")
        return

    if os.environ.get("GCAL_RUN_RESET", "").strip() in ("1", "true"):
        # Разбор последствий: провести запись заново, как будто робот её не видел.
        await store.forget(parsed.event_id)
        print("   прежнее состояние записи забыто — начинаю с чистого листа")

    # Запись могла быть помечена «была в календаре до включения»: владелец просит
    # провести её явно, значит пометку снимаем.
    link = await store.get(parsed.event_id)
    if link is not None and link.status == "skipped":
        await store.update(parsed.event_id, status="new", skip_reason=None)

    link = await engine.process(parsed)

    # В репетиции ждать автосделку бессмысленно: лид не заводился, и создавать
    # её сейлзботу не по чему. Ждём только в бою.
    waited = 0
    while not preview and link.status == "waiting_salesbot" and waited < WAIT_TOTAL_SEC:
        print(f"   жду автосделку сейлзбота… ({waited} сек)")
        await asyncio.sleep(WAIT_STEP_SEC)
        waited += WAIT_STEP_SEC
        link = await engine.process(parsed)

    print(f"   клиент: {parsed.client_name or '—'}, телефон …{(parsed.phone10 or '')[-4:]}")
    print(f"   когда: {parsed.order_date:%d.%m.%Y}, услуга: {', '.join(parsed.services) or '—'}"
          f", район: {parsed.district or '—'}")
    print(f"   состояние: {link.status}")

    for title, lead_id in (("лид первичной", link.primary_lead_id),
                           ("сделка", link.real_lead_id)):
        if lead_id:
            print(f"   {title}: " + LEAD_URL.format(base=settings.amo_base_url,
                                                    lead_id=lead_id))
    if link.status == "waiting_salesbot":
        print("   автосделка ещё не появилась — доведёт наблюдатель, "
              "когда календарь работает в боевом режиме")
    if link.last_error:
        print(f"   ошибка: {link.last_error}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
