"""Сверка: что робот записал в ковровые сделки против отчёта партнёра.

Только чтение — ни в CRM, ни в почте ничего не меняется. Письма читаются все,
включая уже разобранные: сверка нужна как раз после того, как работа сделана.

Пример:
    python -m scripts.verify_carpets
"""

from __future__ import annotations

import argparse
import asyncio
import imaplib
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values

from adminbot.amo import ids
from adminbot.amo.client import AmoClient
from adminbot.amo.fields import MOSCOW_TZ, field_value
from adminbot.carpets.report import parse_report, rows_to_process
from adminbot.mail import MailBox, MailSettings
from adminbot.phone import for_owner
from scripts.run_carpets import DEFAULT_MAIL_ENV
from scripts.run_orders import DEFAULT_AMO_ENV, DEFAULT_BOT_ENV

OK, BAD = "✔", "✘"

DISTRICT_NAMES = {enum_id: name.capitalize()
                  for name, enum_id in ids.DISTRICT_ENUMS.items()}
PAYMENT_NAMES = {ids.PAYMENT_ENUM_CASH: "Наличка", ids.PAYMENT_ENUM_CARD: "Перевод на карту",
                 ids.PAYMENT_ENUM_WIRE: "Безнал", ids.PAYMENT_ENUM_ACQUIRING: "Эквайринг"}


class AllLettersBox(MailBox):
    """Тот же ящик, но берём и прочитанные письма: сверяем уже сделанную работу."""

    def _fetch_new(self):
        box = self._connect()
        try:
            self._select(box, readonly=True)
            ok, data = box.uid("search", None, "ALL")
            letters = []
            for uid in (data[0] or b"").split():
                letter = self._read_letter(box, uid)
                if letter is not None and letter.attachments:
                    letters.append(letter)
            return letters
        finally:
            try:
                box.logout()
            except Exception:                          # noqa: BLE001
                pass


async def main() -> int:
    parser = argparse.ArgumentParser(description="Сверка ковровых сделок с отчётом партнёра")
    parser.add_argument("--mail-env", type=Path, default=DEFAULT_MAIL_ENV)
    parser.add_argument("--bot-env", type=Path, default=DEFAULT_BOT_ENV)
    parser.add_argument("--amo-env", type=Path, default=DEFAULT_AMO_ENV)
    args = parser.parse_args()

    mail_env = dotenv_values(args.mail_env)
    bot_env = dotenv_values(args.bot_env)
    amo_env = dotenv_values(args.amo_env) if args.amo_env.exists() else {}
    token = amo_env.get("AMOCRM_API_TOKEN") or bot_env.get("AMOCRM_API_TOKEN")
    base_url = (bot_env.get("AMOCRM_API_BASE") or "").rstrip("/")
    if not base_url:
        base_url = f"https://{bot_env.get('AMOCRM_ACCOUNT_DOMAIN')}"

    settings = MailSettings(
        host=mail_env.get("MAIL_IMAP_HOST", "imap.yandex.ru"),
        user=mail_env["MAIL_USER"], password=mail_env["MAIL_APP_PASSWORD"],
        folder=mail_env.get("MAIL_FOLDER", "INBOX"),
        port=int(mail_env.get("MAIL_IMAP_PORT", "993")),
    )
    letters = await AllLettersBox(connect=settings.connect,
                                 folder=settings.folder).fetch_new()
    rows = [row for letter in letters for data in letter.attachments.values()
            for row in parse_report(data)]
    completed, refused = rows_to_process(rows)
    print(f"Строк в отчётах: {len(completed)} выполненных, {len(refused)} отказов\n")

    client = AmoClient(base_url=base_url, token=token, dry_run=True)
    problems = 0
    try:
        for row in completed + refused:
            lead = await _find_lead(client, row)
            if lead is None:
                print(f"{BAD} №{row.partner_id} · {for_owner(row.phone10)} — "
                      f"ковровой сделки не нашёл\n")
                problems += 1
                continue

            checks = _check(row, lead)
            bad = [line for mark, line in checks if mark == BAD]
            problems += len(bad)
            head = (f"№{row.partner_id} · {for_owner(row.phone10)} · {row.amount} ₽ · "
                    f"сделка #{lead['id']}")
            print(f"{BAD if bad else OK} {head}")
            for mark, line in checks:
                print(f"      {mark} {line}")
            empty = _empty_fields(lead)
            if empty:
                print(f"      · пустые поля сделки: {', '.join(empty)}")
            print()
    finally:
        await client.close()

    print(f"Итого расхождений: {problems}")
    return 1 if problems else 0


async def _find_lead(client: AmoClient, row) -> Optional[dict]:
    """Ковровая сделка клиента, доведённая до «Заказ доставлен»."""
    leads = await client.find_leads_by_phone(row.phone10)
    carpet = [lead for lead in leads
              if int(lead.get("pipeline_id") or 0) == ids.PIPELINE_CARPETS]
    wanted = ids.CARPET_STAGE_REFUSED if row.is_refusal else ids.CARPET_STAGE_DELIVERED
    finished = [lead for lead in carpet if int(lead.get("status_id") or 0) == wanted]
    if not finished:
        return None
    # Самая свежая: её робот и трогал.
    return max(finished, key=lambda lead: int(lead.get("updated_at") or 0))


def _check(row, lead: dict) -> list[tuple[str, str]]:
    checks: list[tuple[str, str]] = []

    def add(condition: bool, text: str) -> None:
        checks.append((OK if condition else BAD, text))

    price = Decimal(str(lead.get("price") or 0))
    add(price == row.amount, f"бюджет {price} ₽ (в отчёте {row.amount} ₽)")

    stage = int(lead.get("status_id") or 0)
    expected_stage = (ids.CARPET_STAGE_REFUSED if row.is_refusal
                      else ids.CARPET_STAGE_DELIVERED)
    add(stage == expected_stage,
        "этап «Закрыто и не реализовано»" if row.is_refusal else "этап «Заказ доставлен»")

    if row.is_refusal:
        return checks

    add(_date_of(lead, ids.FIELD_CARPET_PICKUP) == row.pickup_date,
        f"дата забора {_show(_date_of(lead, ids.FIELD_CARPET_PICKUP))} "
        f"(в отчёте {_show(row.pickup_date)})")
    add(_date_of(lead, ids.FIELD_CARPET_RETURN) == row.return_date,
        f"дата возврата {_show(_date_of(lead, ids.FIELD_CARPET_RETURN))} "
        f"(в отчёте {_show(row.return_date)})")

    district = field_value(lead, ids.FIELD_DISTRICT)
    expected_district = ids.DISTRICT_ENUMS.get((row.district or "").lower())
    if expected_district:
        add(_same_enum(district, expected_district, DISTRICT_NAMES),
            f"район {district} (в отчёте {row.district})")

    payment = field_value(lead, ids.FIELD_PAYMENT_TYPE)
    if row.payment_method:
        add(payment is not None, f"вариант оплаты {payment} (в отчёте {row.payment_method})")

    service = field_value(lead, ids.FIELD_SERVICE)
    add(service is not None, f"услуга {service}")
    specialist = field_value(lead, ids.FIELD_SPECIALIST)
    add(specialist is not None, f"специалист {specialist}")
    return checks


# Поля ковровой сделки, которые владелец видит в карточке. Смотрим, что осталось
# незаполненным: часть данных робот сейчас не берёт ниоткуда.
WATCHED_FIELDS = {
    ids.FIELD_ADDRESS: "адрес",
    ids.FIELD_ORDER_DATETIME: "дата и время заказа",
    ids.FIELD_PAYMENT_DATE: "дата оплаты",
    ids.FIELD_PAYMENT_TYPE: "вариант оплаты",
    ids.FIELD_CLIENT_TYPE: "тип клиента",
    ids.FIELD_SOURCE: "источник сделки",
    ids.FIELD_DISTRICT: "район города",
    ids.FIELD_COMMENT: "комментарий к заказу",
    ids.FIELD_SERVICE: "услуга",
    ids.FIELD_SPECIALIST: "специалист",
    ids.FIELD_CARPET_PICKUP: "дата забора ковра",
    ids.FIELD_CARPET_RETURN: "дата возврата ковра",
}


def _empty_fields(lead: dict) -> list[str]:
    return [name for field_id, name in WATCHED_FIELDS.items()
            if field_value(lead, field_id) in (None, "")]


def _same_enum(value, expected_id: int, names: dict) -> bool:
    """Амо отдаёт то название варианта, то его номер."""
    if value == expected_id:
        return True
    return str(value).strip().lower() == str(names.get(expected_id, "")).strip().lower()


def _date_of(lead: dict, field_id: int):
    raw = field_value(lead, field_id)
    if raw in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=MOSCOW_TZ).date()
    except (TypeError, ValueError):
        return None


def _show(value) -> str:
    return value.strftime("%d.%m.%Y") if value else "пусто"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
