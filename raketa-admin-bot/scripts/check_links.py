"""Сверка: у каждого ли заказа бота есть сделка в amoCRM (только чтение).

Отвечает на один вопрос владельца: «всё ли доехало до CRM». Для каждого заказа
за период ищет проведённую сделку воронки реализации — по полю «Дата и время
заказа» (±2 дня), а если оно пустое или неверное, по дате закрытия (±3 дня).
Та же проверка, что и в экзамене на истории, поэтому результаты сопоставимы.

Ничего никуда не пишет.

Пример:
    python -m scripts.check_links --since 2026-08-14
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values

from adminbot.amo import ids
from adminbot.amo.client import AmoClient
from adminbot.amo.fields import order_date_msk
from adminbot.phone import for_owner
from scripts.history_exam import find_fact_lead
from scripts.run_orders import DEFAULT_AMO_ENV, DEFAULT_BOT_ENV, load_orders


async def main() -> int:
    parser = argparse.ArgumentParser(description="Сверка заказов бота со сделками amoCRM")
    parser.add_argument("--since", type=date.fromisoformat, required=True,
                        help="с какой даты проверять, ГГГГ-ММ-ДД")
    parser.add_argument("--bot-env", type=Path, default=DEFAULT_BOT_ENV)
    parser.add_argument("--amo-env", type=Path, default=DEFAULT_AMO_ENV)
    parser.add_argument("--pause", type=float, default=0.15)
    args = parser.parse_args()

    bot_env = dotenv_values(args.bot_env)
    amo_env = dotenv_values(args.amo_env) if args.amo_env.exists() else {}
    dsn = bot_env.get("DB_DSN")
    token = amo_env.get("AMOCRM_API_TOKEN") or bot_env.get("AMOCRM_API_TOKEN")
    base_url = (amo_env.get("AMOCRM_API_BASE") or bot_env.get("AMOCRM_API_BASE") or "").rstrip("/")
    if not base_url:
        domain = amo_env.get("AMOCRM_ACCOUNT_DOMAIN") or bot_env.get("AMOCRM_ACCOUNT_DOMAIN")
        base_url = f"https://{domain}" if domain else ""
    if not all((dsn, token, base_url)):
        print("Не хватает доступов: DB_DSN / AMOCRM_API_TOKEN / AMOCRM_API_BASE", file=sys.stderr)
        return 2

    orders = await load_orders(dsn, None, args.since)
    print(f"Заказов с {args.since:%d.%m.%Y}: {len(orders)}. Читаю amoCRM…\n", flush=True)

    client = AmoClient(base_url=base_url, token=token, dry_run=True)
    linked: list[tuple] = []
    missing: list[tuple] = []
    # Одна сделка не может закрывать два заказа: у клиента бывает несколько работ подряд.
    taken_by_phone: dict[str, set[int]] = {}

    try:
        for order in orders:
            if not order.phone10:
                missing.append((order, None, "телефон не распознан"))
                continue

            leads = await client.find_leads_by_phone(order.phone10)
            taken = taken_by_phone.setdefault(order.phone10, set())
            lead_id, source = find_fact_lead(order.order_date, leads, order.amount_total, taken)

            if lead_id:
                taken.add(lead_id)
                linked.append((order, lead_id, source))
            else:
                # Проведённой сделки нет. Показываем, что у клиента вообще есть:
                # открытая сделка — это одно, пустая карточка — совсем другое.
                missing.append((order, None, _describe_leads(leads)))
            await asyncio.sleep(args.pause)
    finally:
        await client.close()

    _print_table("Заказы со сделкой", linked)
    _print_table("Заказы БЕЗ сделки", missing)

    total = len(orders)
    print(f"\nИтого: {len(linked)} из {total} заказов имеют сделку в amoCRM.")
    if missing:
        print(f"Без сделки: {len(missing)} — перечислены выше.")
    return 1 if missing else 0


SOURCE_NAMES = {
    "order_date": "по дате заказа",
    "closed": "по дате закрытия",
}

PIPELINE_NAMES = {
    ids.PIPELINE_PRIMARY: "первичная",
    ids.PIPELINE_REALIZATION: "реализация",
    ids.PIPELINE_CARPETS: "ковры",
    ids.PIPELINE_CARPETS_LEGACY: "ковры",
}


def _describe_leads(leads: list[dict]) -> str:
    """Что вообще есть у клиента в CRM, если проведённой сделки не нашлось."""
    if not leads:
        return "у клиента нет ни одной сделки"

    parts = []
    for lead in leads[:4]:
        pipeline = PIPELINE_NAMES.get(int(lead.get("pipeline_id") or 0), "другая воронка")
        status_id = int(lead.get("status_id") or 0)
        state = ("проведена" if status_id == ids.STATUS_SUCCESS
                 else "закрыта" if status_id == ids.STATUS_CLOSED else "открыта")
        when = order_date_msk(lead)
        parts.append(f"#{lead['id']} {pipeline}/{state}"
                     + (f" от {when:%d.%m}" if when else ""))
    tail = f" и ещё {len(leads) - 4}" if len(leads) > 4 else ""
    return "есть: " + ", ".join(parts) + tail


def _print_table(title: str, rows: list[tuple]) -> None:
    if not rows:
        return
    print(f"=== {title}: {len(rows)} ===")
    for order, lead_id, note in rows:
        masters = ", ".join(order.master_names) or "мастер неизвестен"
        target = f"#{lead_id} ({SOURCE_NAMES.get(note, note)})" if lead_id else note
        print(f"  №{order.order_id} · {order.created_at:%d.%m.%Y} · {order.amount_total}₽ · "
              f"{for_owner(order.phone10)} · {masters} → {target}")
    print()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
