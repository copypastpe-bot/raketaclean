"""Показать значения списочных полей сделки в amoCRM (только чтение).

Зачем: константы вроде «Услуга → Чистка мебели» задаются числами, а числа
проверяются глазами лишь одним способом — спросить у самой CRM, как они
называются сегодня. Скрипт ничего не меняет, только читает справочник.

Примеры:
    python -m scripts.show_field_enums                 # поля, которые заполняет робот
    python -m scripts.show_field_enums 271915 39243    # конкретные поля по id
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values

from adminbot.amo import ids
from adminbot.amo.client import AmoClient

DEFAULT_BOT_ENV = Path("/opt/telegram-bot/.env")
DEFAULT_AMO_ENV = Path.home() / ".amo_write.env"

# Поля, значения которых робот проставляет сам.
FIELDS = {
    ids.FIELD_SERVICE: "Услуга",
    ids.FIELD_SPECIALIST: "Специалист",
    ids.FIELD_PAYMENT_TYPE: "Вариант оплаты",
    ids.FIELD_CLIENT_TYPE: "Тип клиента",
    ids.FIELD_SOURCE: "Источник сделки",
}

# Что робот подставляет сейчас — чтобы расхождение бросалось в глаза.
IN_USE = {
    ids.SERVICE_ENUM_FURNITURE: "мебель (Никита, Дмитрий)",
    ids.SERVICE_ENUM_CLEANING: "клининг (Ольга)",
    ids.PAYMENT_ENUM_CASH: "оплата: наличные",
    ids.PAYMENT_ENUM_CARD: "оплата: карта",
    ids.PAYMENT_ENUM_WIRE: "оплата: р/с",
    ids.CLIENT_TYPE_COMPANY: "тип клиента: юрлицо",
    ids.CLIENT_TYPE_PERSON: "тип клиента: физлицо",
    ids.SOURCE_ENUM_WORD_OF_MOUTH: "источник: сарафан",
    ids.SOURCE_ENUM_REPEAT: "источник: повторный заказ",
}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Справочник значений полей сделки")
    parser.add_argument("fields", nargs="*", type=int, help="id полей (по умолчанию — рабочие)")
    parser.add_argument("--bot-env", type=Path, default=DEFAULT_BOT_ENV)
    parser.add_argument("--amo-env", type=Path, default=DEFAULT_AMO_ENV)
    args = parser.parse_args()

    bot_env = dotenv_values(args.bot_env) if args.bot_env.exists() else {}
    amo_env = dotenv_values(args.amo_env) if args.amo_env.exists() else {}

    token = amo_env.get("AMOCRM_API_TOKEN") or bot_env.get("AMOCRM_API_TOKEN")
    base_url = (amo_env.get("AMOCRM_API_BASE") or bot_env.get("AMOCRM_API_BASE") or "").rstrip("/")
    if not base_url:
        domain = amo_env.get("AMOCRM_ACCOUNT_DOMAIN") or bot_env.get("AMOCRM_ACCOUNT_DOMAIN")
        base_url = f"https://{domain}" if domain else ""
    if not token or not base_url:
        print("Не хватает доступов: AMOCRM_API_TOKEN / AMOCRM_API_BASE", file=sys.stderr)
        return 2

    field_ids = args.fields or list(FIELDS)
    client = AmoClient(base_url=base_url, token=token, dry_run=True)   # запись не нужна
    try:
        for field_id in field_ids:
            title = FIELDS.get(field_id, f"поле {field_id}")
            print(f"\n=== {title} (id {field_id}) ===")
            for enum in await client.get_lead_field_enums(field_id):
                mark = f"   ← {IN_USE[enum['id']]}" if enum["id"] in IN_USE else ""
                print(f"  {enum['id']:>10}  {enum['value']}{mark}")
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
