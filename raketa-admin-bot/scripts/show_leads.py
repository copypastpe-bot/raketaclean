"""Показать сделки клиента по телефону — диагностика, ничего не меняет.

Нужен, когда надо понять глазами, что в CRM на самом деле: сколько у клиента
сделок, в каких они воронках и на каких этапах. Робот видит ровно это же.

Запуск на сервере:

    sudo raketa-admin-bot-update --leads=9601861067
"""

from __future__ import annotations

import asyncio
import os
import sys

from adminbot.amo import ids
from adminbot.amo.client import AmoClient
from adminbot.amo.fields import (
    contact_phones, field_value, lead_contact_ids, order_date_msk)
from adminbot.config import Settings
from adminbot.phone import last10

PIPELINE_NAMES = {
    ids.PIPELINE_PRIMARY: "первичная",
    ids.PIPELINE_REALIZATION: "реализация",
    ids.PIPELINE_CARPETS: "ковры",
    ids.PIPELINE_CARPETS_LEGACY: "ковры (старая)",
}


async def main() -> int:
    phone = last10(os.environ.get("SHOW_LEADS_PHONE", ""))
    wanted = [item.strip() for item in os.environ.get("SHOW_LEADS_IDS", "").split(",")
              if item.strip()]
    if not phone and not wanted:
        print("Не указан ни телефон (SHOW_LEADS_PHONE), ни сделки (SHOW_LEADS_IDS).")
        return 2

    settings = Settings.from_env()
    amo = AmoClient(base_url=settings.amo_base_url, token=settings.amo_token,
                    dry_run=True)                       # только чтение
    try:
        if wanted:
            leads = [lead for lead in
                     [await amo.get_lead(int(lead_id)) for lead_id in wanted] if lead]
            print(f"Сделок запрошено: {len(wanted)}, найдено: {len(leads)}\n")
            # Заодно показываем ВСЕ сделки этого клиента: по ним видно, не увёл ли
            # робот заказ в чужую сделку.
            phone = phone or await _phone_of(amo, leads)
        else:
            leads = await amo.find_leads_by_phone(phone)
            print(f"Сделок по телефону …{phone[-4:]}: {len(leads)}\n")

        if wanted and phone:
            everything = await amo.find_leads_by_phone(phone)
            known = {int(lead["id"]) for lead in leads}
            leads += [lead for lead in everything if int(lead["id"]) not in known]
            print(f"Всего сделок у клиента …{phone[-4:]}: {len(leads)}\n")

        for lead in sorted(leads, key=lambda item: int(item.get("id", 0))):
            lead_id = lead.get("id")
            pipeline = PIPELINE_NAMES.get(int(lead.get("pipeline_id") or 0),
                                          str(lead.get("pipeline_id")))
            status = lead.get("status_id")
            when = order_date_msk(lead)
            print(f"#{lead_id} · {lead.get('name') or '—'}")
            print(f"    воронка: {pipeline}, этап: {status}, бюджет: {lead.get('price')}")
            print(f"    дата заказа: {when or '—'}, "
                  f"адрес: {field_value(lead, ids.FIELD_ADDRESS) or '—'}")
            print(f"    район: {field_value(lead, ids.FIELD_DISTRICT) or '—'}, "
                  f"услуга: {field_value(lead, ids.FIELD_SERVICE) or '—'}")
            comment = field_value(lead, ids.FIELD_COMMENT)
            if comment:
                print(f"    комментарий: {str(comment)[:120]}")
            print(f"    ссылка: {settings.amo_base_url}/leads/detail/{lead_id}")
            print()
    finally:
        await amo.close()
    return 0


async def _phone_of(amo, leads) -> str:
    """Телефон клиента по первой сделке — чтобы найти все остальные его сделки."""
    for lead in leads:
        for contact_id in lead_contact_ids(lead):
            contact = await amo.get_contact(contact_id)
            for phone in contact_phones(contact or {}):
                digits = last10(phone)
                if digits:
                    return digits
    return ""


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
