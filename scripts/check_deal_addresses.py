"""Разведка: есть ли в amoCRM адрес у сделок, которые робот завёл с нуля.

Только чтение. Ни в CRM, ни в базу ничего не пишет — можно запускать сколько угодно.

Зачем. Колонка `deal_address` в связках админ-бота появилась 17.09, поэтому у всей
прежней истории она пуста. Напоминания «сделка без адреса» отбирают связки ровно
по этому признаку и, если их включить, начнут слать карточки по старым сделкам.
Перед включением нужно знать факт: адрес в тех сделках есть или его там нет вовсе.

Запуск на сервере (amoCRM отвечает только с VPS):

    cd /opt/telegram-bot && set -a && . .env && set +a && .venv/bin/python scripts/check_deal_addresses.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg

from notifications.amo_exchange import AMO_FIELD_ADDRESS, field_values
from notifications.amocrm_api import AmoCRMAPIClient

# Связки, которые попадут под напоминания: робот завёл сделку с нуля (путь C),
# работу закончил, адреса в связке нет и владелец её не заглушил.
SQL = """
SELECT %(label)s AS kind, order_id, real_lead_id, primary_lead_id,
       to_char(updated_at AT TIME ZONE 'Europe/Moscow', 'DD.MM.YYYY') AS updated
FROM %(table)s
WHERE status = 'done' AND path = 'C' AND deal_address IS NULL
  AND address_reminder_muted = false
ORDER BY order_id
"""


async def main() -> int:
    dsn = os.environ.get("DB_DSN", "").strip()
    base = os.environ.get("AMOCRM_API_BASE", "").strip().rstrip("/")
    token = os.environ.get("AMOCRM_API_TOKEN", "").strip()
    if not dsn or not base or not token:
        print("Нет DB_DSN или доступа к amoCRM в окружении. Запускать из /opt/telegram-bot "
              "с подгруженным .env.")
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        rows = []
        for label, table in (("Заказ", "adminbot.amo_links"),
                             ("Уборка", "adminbot.cleaning_links")):
            rows.extend(await conn.fetch(SQL % {"label": f"'{label}'", "table": table}))
    finally:
        await conn.close()

    if not rows:
        print("Связок без адреса нет — напоминать роботу не о чем.")
        return 0

    print(f"Связок без адреса: {len(rows)}. Иду в amoCRM за каждой.\n")
    print(f"{'контур':8} {'№':>5}  {'сделка':>10}  {'заведено':10}  адрес в CRM")
    print("-" * 78)

    found = missing = gone = 0
    async with AmoCRMAPIClient(base, token) as amo:
        for row in rows:
            lead_id = row["real_lead_id"] or row["primary_lead_id"]
            if not lead_id:
                verdict, mark = "сделки в связке нет", "—"
                gone += 1
            else:
                try:
                    lead = await amo.fetch_lead(int(lead_id))
                except Exception as exc:  # noqa: BLE001 — разведка не должна падать на одной сделке
                    verdict, mark = f"CRM не ответила: {type(exc).__name__}", "?"
                    gone += 1
                else:
                    address = next(iter(field_values(dict(lead), AMO_FIELD_ADDRESS)), None)
                    address = str(address).strip() if address else ""
                    if address:
                        verdict, mark = address, "✔"
                        found += 1
                    else:
                        verdict, mark = "пусто", "·"
                        missing += 1
            print(f"{row['kind']:8} {row['order_id']:>5}  {lead_id or '—':>10}  "
                  f"{row['updated']:10}  {mark} {verdict}")

    print("-" * 78)
    print(f"Адрес есть: {found}   пусто: {missing}   не проверить: {gone}")
    print()
    if found:
        print(f"Эти {found} можно перенести в связки — дальше бот разложит их сам.")
    if missing:
        print(f"По этим {missing} заполнять нечего: адреса в CRM нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
