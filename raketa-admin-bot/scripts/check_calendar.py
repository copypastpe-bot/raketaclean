"""Проверка доступа к календарю: читает ближайшие записи и ничего не меняет.

Запускается на сервере перед включением функции:

    sudo raketa-admin-bot-update --gcal-check

Что показывает: видит ли робот календарь, как он разбирает записи и что именно
собирается написать в CRM. В amoCRM и в базу при этом не уходит ничего.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta

from adminbot.gcal.auth import GCalKeyError, ServiceAccountToken
from adminbot.gcal.client import GCalError, GoogleCalendar
from adminbot.gcal.event import EventKind, parse_event
from adminbot.tg.calendar_cards import SERVICE_WORDS

DEFAULT_CALENDAR = "raketaclean52@gmail.com"
KIND_WORDS = {
    EventKind.ORDER: "заказ",
    EventKind.BLOCK: "выходной мастера",
    EventKind.REWASH: "перемыв по гарантии",
    EventKind.UNSETTLED: "дата не подтверждена",
    EventKind.BOAT: "теплоход",
    EventKind.SKIP: "пропускаю",
    EventKind.CANCELLED: "запись удалена",
}


async def main() -> int:
    calendar_id = os.environ.get("GCAL_CALENDAR_ID", "").strip() or DEFAULT_CALENDAR
    key_file = os.environ.get("GCAL_SERVICE_ACCOUNT_FILE", "").strip() or "~/.gcal.json"

    try:
        token = ServiceAccountToken.from_file(key_file)
    except GCalKeyError as exc:
        print(f"Ключ доступа не работает: {exc}")
        return 1

    calendar = GoogleCalendar(calendar_id=calendar_id, token=token)
    try:
        batch = await calendar.fetch(sync_token=None,
                                     sync_from=date.today() - timedelta(days=1))
    except GCalError as exc:
        print(f"Календарь прочитать не удалось: {exc}")
        return 1
    finally:
        await calendar.close()
        await token.close()

    print(f"Календарь {calendar_id} доступен. Записей в ближайшие дни: "
          f"{len(batch.events)}\n")

    for raw in batch.events[:5]:
        parsed = parse_event(raw)
        services = ", ".join(SERVICE_WORDS.get(kind, kind) for kind in parsed.services)
        print(f"• {parsed.summary or '(без заголовка)'}")
        print(f"    вид: {KIND_WORDS.get(parsed.kind, parsed.kind.value)}"
              + (f", услуга: {services}" if services else "")
              + (f", район: {parsed.district}" if parsed.district else "")
              + (f", когда: {parsed.order_date:%d.%m.%Y}" if parsed.order_date else ""))
        if parsed.phone10:
            print(f"    телефон найден: …{parsed.phone10[-4:]}")
        elif parsed.kind is EventKind.ORDER:
            print("    телефон НЕ найден")

    print("\nВ amoCRM и в базу ничего не записано — это только проверка.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
