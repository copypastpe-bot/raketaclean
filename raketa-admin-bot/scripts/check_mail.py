"""Проверка почтового ящика робота: что лежит и как разбирается (только чтение).

Ничего не помечает прочитанным и никуда не пишет — можно запускать сколько угодно.

Пример:
    python -m scripts.check_mail --mail-env ~/.mail_robot.env
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values

from adminbot.carpets.report import parse_report, rows_to_process
from adminbot.mail import MailBox, MailSettings
from adminbot.phone import for_owner

DEFAULT_MAIL_ENV = Path.home() / ".mail_robot.env"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Что лежит в почте робота")
    parser.add_argument("--mail-env", type=Path, default=DEFAULT_MAIL_ENV)
    parser.add_argument("--rows", type=int, default=3, help="сколько строк показывать")
    args = parser.parse_args()

    env = dotenv_values(args.mail_env)
    if not env.get("MAIL_USER") or not env.get("MAIL_APP_PASSWORD"):
        print(f"Нет доступов к почте в {args.mail_env}", file=sys.stderr)
        return 2

    settings = MailSettings(
        host=env.get("MAIL_IMAP_HOST", "imap.yandex.ru"),
        user=env["MAIL_USER"],
        password=env["MAIL_APP_PASSWORD"],
        folder=env.get("MAIL_FOLDER", "INBOX"),
        port=int(env.get("MAIL_IMAP_PORT", "993")),
    )
    box = MailBox(connect=settings.connect, folder=settings.folder)

    letters = await box.fetch_new()
    print(f"Папка {settings.folder}: неразобранных писем с отчётами — {len(letters)}\n")

    for letter in letters:
        print(f"=== {letter.subject} ===")
        print(f"    от {letter.sender}, {letter.date}")
        for name, data in letter.attachments.items():
            try:
                rows = parse_report(data)
            except Exception as exc:                # noqa: BLE001
                print(f"    {name}: разобрать не удалось — {exc}")
                continue

            done, refused = rows_to_process(rows)
            print(f"    {name}: строк {len(rows)} — к проведению {len(done)}, "
                  f"отказов {len(refused)}")
            for row in (done + refused)[: args.rows]:
                mark = "отказ" if row.is_refusal else f"{row.amount} ₽"
                dates = (f"забор {row.pickup_date:%d.%m}, сдача {row.return_date:%d.%m}"
                         if row.pickup_date and row.return_date else row.refusal_reason or "")
                print(f"       №{row.partner_id} · {for_owner(row.phone10)} · {mark} · "
                      f"{row.district or '?'} · {dates}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
