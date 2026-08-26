"""Разовый прогон движка по заказам: репетиция или одна боевая сделка.

По умолчанию — РЕПЕТИЦИЯ: робот читает боевую amoCRM, решает, что сделал бы,
и печатает план. В CRM не уходит ничего.

Боевой режим включается только двумя явными ключами сразу: `--live` и конкретный
`--order`. Провести «всё сразу» этим скриптом нельзя — это делается из Telegram
после предпросмотра, когда у владельца есть кнопка «стоп».

Примеры:
    python -m scripts.run_orders --since 2026-08-21              # репетиция по хвосту
    python -m scripts.run_orders --order 596                     # репетиция по одному заказу
    python -m scripts.run_orders --order 596 --live              # боевое проведение одного заказа
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
from dotenv import dotenv_values

from adminbot.amo import ids
from adminbot.amo.client import AmoClient
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import Order
from adminbot.phone import for_owner, last10
from adminbot.sync.engine import Engine
from adminbot.sync.specialists import SpecialistIndex
from adminbot.sync.store import MemoryLinkStore

DEFAULT_BOT_ENV = Path("/opt/telegram-bot/.env")
DEFAULT_AMO_ENV = Path.home() / ".amo_write.env"

ORDERS_SQL = """
SELECT o.id, o.phone, o.phone_digits, o.customer_name, o.created_at,
       o.amount_total, o.rating_score, o.payment_method, o.awaiting_wire_payment,
       EXISTS (SELECT 1 FROM public.orders prev WHERE prev.phone_digits = o.phone_digits AND prev.created_at < o.created_at) AS is_repeat_client,
       c.full_name AS client_full_name,
       COALESCE(NULLIF(TRIM(c.address), ''), NULLIF(TRIM(c.last_order_addr), '')) AS address,
       COALESCE((
           SELECT jsonb_agg(jsonb_build_array(m.name, m.phone) ORDER BY m.is_primary DESC, m.name)
           FROM (
               SELECT COALESCE(NULLIF(TRIM(s.full_name), ''),
                               NULLIF(TRIM(CONCAT_WS(' ', s.first_name, s.last_name)), '')) AS name,
                      s.phone AS phone, (s.id = o.master_id) AS is_primary
               FROM public.staff s
               WHERE s.id = o.master_id
                  OR s.id IN (SELECT om.master_id FROM public.order_masters om WHERE om.order_id = o.id)
           ) m WHERE m.name IS NOT NULL
       ), '[]'::jsonb) AS masters
FROM public.orders o
LEFT JOIN public.clients c ON c.id = o.client_id
WHERE ($1::bigint IS NULL OR o.id = $1)
  AND ($2::date IS NULL OR o.created_at >= ($2::date AT TIME ZONE 'Europe/Moscow'))
ORDER BY o.created_at, o.id
"""

ACTION_NAMES = {
    "update_lead": "заполнить сделку",
    "move_lead": "перевести этап",
    "create_lead": "создать сделку",
    "create_contact": "создать контакт",
    "complete_task": "закрыть задачу",
    "add_note": "написать комментарий",
    "ask_owner": "спросить владельца",
    "salesbot_timeout": "сейлзбот молчит",
}

STATUS_NAMES = {
    "done": "проведено",
    "waiting_owner": "ждёт вашего ответа",
    "waiting_salesbot": "ждёт автосделку сейлзбота",
    "error": "ошибка",
    "in_progress": "в работе",
}


async def load_orders(dsn: str, order_id: Optional[int], since: Optional[date]) -> list[Order]:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                                  schema="pg_catalog")
        rows = await conn.fetch(ORDERS_SQL, order_id, since)
    finally:
        await conn.close()
    return [
        Order(
            order_id=row["id"],
            phone10=last10(row["phone_digits"]) or last10(row["phone"]),
            created_at=row["created_at"].astimezone(MOSCOW_TZ),
            amount_total=row["amount_total"] or 0,
            masters=[(str(name), phone) for name, phone in (row["masters"] or [])],
            rating_score=row["rating_score"],
            payment_method=row["payment_method"],
            awaiting_wire_payment=bool(row["awaiting_wire_payment"]),
            is_repeat_client=bool(row["is_repeat_client"]),
            client_name=row["client_full_name"] or row["customer_name"],
            address=row["address"],
        )
        for row in rows
    ]


def describe(order: Order, link, actions: list[dict]) -> str:
    lines = [
        f"\n=== Заказ №{order.order_id} · {for_owner(order.phone10)} · "
        f"{order.created_at:%d.%m.%Y %H:%M} · {order.amount_total}₽ · "
        f"{', '.join(order.master_names) or 'мастер неизвестен'} ==="
    ]
    path_names = {"A": "автосделка уже есть", "B": "через лид первичной воронки",
                  "C": "сделки нет — создать с нуля", "done": "уже проведено вами"}
    if link.path:
        lines.append(f"путь: {path_names.get(link.path, link.path)}")
    lines.append(f"итог: {STATUS_NAMES.get(link.status, link.status)}")
    if link.real_lead_id:
        lines.append(f"сделка реализации: #{link.real_lead_id}")
    if link.primary_lead_id:
        lines.append(f"лид первичной: #{link.primary_lead_id}")
    if link.last_error:
        lines.append(f"ошибка: {link.last_error}")

    own = [row for row in actions if row["order_id"] == order.order_id]
    if own:
        lines.append("действия:")
        for row in own:
            name = ACTION_NAMES.get(row["action"], row["action"])
            target = f" #{row['amo_id']}" if row["amo_id"] else ""
            mark = "" if row["dry_run"] else " ← ВЫПОЛНЕНО"
            lines.append(f"  • {name}{target}{mark}: {_pretty(row['action'], row['payload'])}")
    else:
        lines.append("действий не требуется")
    return "\n".join(lines)


FIELD_NAMES = {
    ids.FIELD_ORDER_DATETIME: "дата и время заказа",
    ids.FIELD_ADDRESS: "адрес",
    ids.FIELD_SERVICE: "услуга",
    ids.FIELD_SPECIALIST: "специалист",
    ids.FIELD_PAYMENT_TYPE: "вариант оплаты",
    ids.FIELD_CLIENT_TYPE: "тип клиента",
    ids.FIELD_PAYMENT_DATE: "дата оплаты",
    ids.FIELD_SOURCE: "источник сделки",
}
DATE_FIELDS = (ids.FIELD_ORDER_DATETIME, ids.FIELD_PAYMENT_DATE)
STAGE_NAMES = {
    ids.STATUS_SUCCESS: "успех (проведено и оплачено)",
    ids.REAL_STAGE_DONE: "«Заказ выполнен» (ждём оплату по счёту)",
    ids.PRIM_STAGE_NEW_LEAD: "«Новый лид»",
}

# Расшифровка значений списков подтягивается из амо один раз за запуск.
ENUM_NAMES: dict[int, str] = {}


def _pretty(action: str, payload) -> str:
    """Показать действие по-человечески, а не куском JSON."""
    if action == "move_lead" and isinstance(payload, dict):
        status = payload.get("status_id")
        pipeline = "первичная" if payload.get("pipeline_id") == ids.PIPELINE_PRIMARY else "реализация"
        return f"воронка {pipeline} → {STAGE_NAMES.get(status, status)}"

    if action in ("add_note", "update_contact"):
        if isinstance(payload, list) and payload:
            payload = payload[0]
        text = (payload or {}).get("params", {}).get("text") or (payload or {}).get("name") or ""
        return str(text).replace("\n", " / ")

    body = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(body, dict):
        return _short(payload)

    parts = []
    if body.get("price") is not None:
        parts.append(f"бюджет {body['price']} ₽")
    if body.get("name"):
        parts.append(f"название «{body['name']}»")
    for field in body.get("custom_fields_values") or []:
        name = FIELD_NAMES.get(field.get("field_id"), f"поле {field.get('field_id')}")
        values = []
        for item in field.get("values") or []:
            if "enum_id" in item:
                values.append(ENUM_NAMES.get(item["enum_id"], f"вариант {item['enum_id']}"))
            elif field.get("field_id") in DATE_FIELDS:
                values.append(datetime.fromtimestamp(int(item["value"]),
                                                     tz=MOSCOW_TZ).strftime("%d.%m.%Y %H:%M"))
            else:
                values.append(str(item.get("value")))
        parts.append(f"{name}: {', '.join(values)}")
    return "; ".join(parts) if parts else _short(payload)


def _short(payload) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text if len(text) <= 220 else text[:217] + "…"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Прогон движка по заказам")
    parser.add_argument("--order", type=int, help="один конкретный заказ")
    parser.add_argument("--since", type=date.fromisoformat, help="заказы начиная с даты")
    parser.add_argument("--live", action="store_true",
                        help="БОЕВОЙ режим: реально писать в amoCRM (только с --order)")
    parser.add_argument("--bot-env", type=Path, default=DEFAULT_BOT_ENV)
    parser.add_argument("--amo-env", type=Path, default=DEFAULT_AMO_ENV,
                        help="файл с токеном записи (для боевого режима)")
    args = parser.parse_args()

    if not args.order and not args.since:
        print("Укажите --order или --since", file=sys.stderr)
        return 2
    if args.live and not args.order:
        print("Боевой режим только по одному заказу: добавьте --order", file=sys.stderr)
        return 2

    bot_env = dotenv_values(args.bot_env)
    amo_env = dotenv_values(args.amo_env) if args.amo_env.exists() else {}

    dsn = bot_env.get("DB_DSN")
    # В репетиции хватает токена чтения; для записи нужен токен новой интеграции.
    token = amo_env.get("AMOCRM_API_TOKEN") or bot_env.get("AMOCRM_API_TOKEN")
    base_url = (amo_env.get("AMOCRM_API_BASE") or bot_env.get("AMOCRM_API_BASE") or "").rstrip("/")
    if not base_url:
        domain = amo_env.get("AMOCRM_ACCOUNT_DOMAIN") or bot_env.get("AMOCRM_ACCOUNT_DOMAIN")
        base_url = f"https://{domain}" if domain else ""

    if args.live and not amo_env.get("AMOCRM_API_TOKEN"):
        print(f"Для боевого режима нужен токен записи в {args.amo_env}", file=sys.stderr)
        return 2
    if not all((dsn, token, base_url)):
        print("Не хватает доступов: DB_DSN / AMOCRM_API_TOKEN / AMOCRM_API_BASE", file=sys.stderr)
        return 2

    orders = await load_orders(dsn, args.order, args.since)
    if not orders:
        print("Заказов не найдено")
        return 0

    mode = "БОЕВОЙ РЕЖИМ — записи уйдут в amoCRM" if args.live else "репетиция — в amoCRM ничего не пишем"
    print(f"Заказов: {len(orders)}. Режим: {mode}.")

    client = AmoClient(base_url=base_url, token=token, dry_run=not args.live)
    store = MemoryLinkStore()
    try:
        specialist_enums = await client.get_lead_field_enums(ids.FIELD_SPECIALIST)
        specialists = SpecialistIndex.from_enums(specialist_enums)
        # Расшифровка значений списков — чтобы план читался человеком, а не машиной.
        for field_id in (ids.FIELD_SERVICE, ids.FIELD_PAYMENT_TYPE, ids.FIELD_CLIENT_TYPE,
                         ids.FIELD_SOURCE):
            for enum in await client.get_lead_field_enums(field_id):
                ENUM_NAMES[enum["id"]] = enum["value"]
        for enum in specialist_enums:
            ENUM_NAMES[enum["id"]] = enum["value"]
        engine = Engine(amo=client, store=store, specialists=specialists, dry_run=not args.live)

        for order in orders:
            link = await engine.process_order(order)
            print(describe(order, link, store.actions), flush=True)
    finally:
        await client.close()

    print(f"\nИтого заказов: {len(orders)}, действий: {len(store.actions)}.")
    if not args.live:
        print("Это была репетиция: в amoCRM не изменилось ничего.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
