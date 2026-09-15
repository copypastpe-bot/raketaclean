"""Прогон отчётов партнёра по коврам: репетиция или боевое проведение.

По умолчанию — РЕПЕТИЦИЯ: робот читает почту и amoCRM, решает, что сделал бы,
и печатает план. В CRM не уходит ничего, письма не помечаются разобранными.

Боевой режим включается явным ключом `--live`.

Прогон из файла (`--file`) почту не трогает вовсе: доступы к ящику не читаются
и не проверяются, письмо собирается из указанного xlsx. Боевой прогон из файла
разрешён только с белым списком сделок (`--allow`): файл может оказаться
не тем, а в CRM за минуты появятся десятки лишних сделок — так и случилось
15.09.2026. Белый список — это ещё и защита в самом клиенте амо: создавать
что-либо нельзя вовсе, писать можно только в названные сделки.

Примеры:
    python -m scripts.run_carpets                 # репетиция по письмам из папки
    python -m scripts.run_carpets --live          # боевое проведение
    python -m scripts.run_carpets --file отчёт.xlsx                  # репетиция по файлу
    python -m scripts.run_carpets --file отчёт.xlsx --live --allow 31587353,31613297
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
from adminbot.carpets.engine import CarpetEngine
from adminbot.carpets.report import parse_report, rows_to_process
from adminbot.carpets.store import MemoryCarpetStore
from adminbot.mail import Letter, MailBox, MailSettings
from adminbot.phone import for_owner
from scripts.run_orders import DEFAULT_AMO_ENV, DEFAULT_BOT_ENV

DEFAULT_MAIL_ENV = Path.home() / ".mail_robot.env"

ACTION_NAMES = {
    "update_lead": "заполнить сделку",
    "move_lead": "перевести этап",
    "create_lead": "создать сделку",
    "create_contact": "создать контакт",
    "add_note": "написать комментарий",
    "ask_owner": "спросить владельца",
    "salesbot_timeout": "сейлзбот молчит",
    "refusal_without_lead": "отказ без сделки — трогать нечего",
}
STATUS_NAMES = {
    "done": "проведено",
    "waiting_owner": "ждёт вашего ответа",
    "waiting_salesbot": "ждёт автосделку сейлзбота",
    "in_progress": "в работе",
    "error": "ошибка",
}
# Успешный этап во всех воронках — номер 142, но называется он везде по-своему:
# в коврах «Заказ доставлен», в первичной «Передано в работу». Поэтому имя этапа
# зависит от воронки, а не только от номера.
STAGE_NAMES = {
    (ids.PIPELINE_CARPETS, ids.CARPET_STAGE_DELIVERED): "«Заказ доставлен»",
    (ids.PIPELINE_CARPETS, ids.CARPET_STAGE_REFUSED): "«Закрыто и не реализовано»",
    (ids.PIPELINE_PRIMARY, ids.STATUS_SUCCESS): "«Передано в работу»",
    (ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD): "«Новый лид»",
}


class AllowListAmo(AmoClient):
    """Клиент амо, которому разрешено трогать только названные сделки.

    Второй предохранитель для прогона из файла: даже если файл окажется не тем,
    робот не сможет ни создать сделку или контакт, ни закрыть чужую задачу, ни
    записать что-нибудь в сделку, которой нет в списке. Ошибка защиты роняет
    строку в `error` — движок ловит её сам, — и в амо не уходит ничего.
    """

    def __init__(self, *args, allowed, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.allowed = {int(lead_id) for lead_id in allowed}

    # --- создавать нельзя вовсе ---

    async def create_lead(self, *args, **kwargs):
        raise RuntimeError("прогон из файла: создавать сделки запрещено")

    async def create_contact(self, *args, **kwargs):
        raise RuntimeError("прогон из файла: создавать контакты запрещено")

    async def update_contact(self, *args, **kwargs):
        raise RuntimeError("прогон из файла: менять контакты запрещено")

    async def complete_task(self, *args, **kwargs):
        raise RuntimeError("прогон из файла: закрывать задачи запрещено")

    # --- писать можно только в сделки из списка ---

    async def update_lead(self, lead_id: int, **kwargs):
        self._allow(lead_id)
        return await super().update_lead(lead_id, **kwargs)

    async def add_note(self, lead_id: int, text: str):
        self._allow(lead_id)
        return await super().add_note(lead_id, text)

    async def move_lead(self, lead_id: int, pipeline_id: int, status_id: int):
        self._allow(lead_id)
        if pipeline_id != ids.PIPELINE_CARPETS:
            raise RuntimeError(
                f"прогон из файла: сделку {lead_id} можно двигать только "
                f"в воронке ковров, а не в {pipeline_id}")
        return await super().move_lead(lead_id, pipeline_id, status_id)

    def _allow(self, lead_id: int) -> None:
        if int(lead_id) not in self.allowed:
            raise RuntimeError(f"прогон из файла: сделки {lead_id} нет в белом списке")


def parse_allow(value: str) -> set[int]:
    """«31587353,31613297» → множество номеров сделок."""
    return {int(item.strip()) for item in (value or "").split(",") if item.strip()}


def letter_from_file(path: Path) -> Letter:
    """Письмо, собранное из файла: почта для такого прогона не нужна вовсе."""
    return Letter(uid=f"file:{path.name}", subject=path.name, sender="владелец",
                  attachments={path.name: path.read_bytes()})


async def main() -> int:
    parser = argparse.ArgumentParser(description="Прогон отчётов партнёра по коврам")
    parser.add_argument("--live", action="store_true",
                        help="БОЕВОЙ режим: писать в amoCRM")
    parser.add_argument("--file", type=Path,
                        help="взять отчёт из файла, а не из почты")
    parser.add_argument("--allow", default="",
                        help="белый список сделок через запятую: только их можно менять")
    parser.add_argument("--mail-env", type=Path, default=DEFAULT_MAIL_ENV)
    parser.add_argument("--bot-env", type=Path, default=DEFAULT_BOT_ENV)
    parser.add_argument("--amo-env", type=Path, default=DEFAULT_AMO_ENV)
    args = parser.parse_args()

    allowed = parse_allow(args.allow)
    if args.file and args.live and not allowed:
        print("боевой прогон из файла только с белым списком сделок (--allow)",
              file=sys.stderr)
        return 2
    if args.file and not args.file.is_file():
        print(f"Файла нет: {args.file}", file=sys.stderr)
        return 2

    bot_env = dotenv_values(args.bot_env)
    amo_env = dotenv_values(args.amo_env) if args.amo_env.exists() else {}

    token = amo_env.get("AMOCRM_API_TOKEN") or bot_env.get("AMOCRM_API_TOKEN")
    base_url = (amo_env.get("AMOCRM_API_BASE") or bot_env.get("AMOCRM_API_BASE") or "").rstrip("/")
    if not base_url:
        domain = amo_env.get("AMOCRM_ACCOUNT_DOMAIN") or bot_env.get("AMOCRM_ACCOUNT_DOMAIN")
        base_url = f"https://{domain}" if domain else ""
    if args.live and not amo_env.get("AMOCRM_API_TOKEN"):
        print(f"Для боевого режима нужен токен записи в {args.amo_env}", file=sys.stderr)
        return 2
    if not all((token, base_url)):
        print("Не хватает доступов к amoCRM", file=sys.stderr)
        return 2

    # Прогон из файла почту не трогает: доступы к ящику даже не читаются.
    if args.file:
        letters = [letter_from_file(args.file)]
    else:
        mail_env = dotenv_values(args.mail_env)
        if not mail_env.get("MAIL_USER"):
            print("Не хватает доступов к почте", file=sys.stderr)
            return 2
        settings = MailSettings(
            host=mail_env.get("MAIL_IMAP_HOST", "imap.yandex.ru"),
            user=mail_env["MAIL_USER"], password=mail_env["MAIL_APP_PASSWORD"],
            folder=mail_env.get("MAIL_FOLDER", "INBOX"),
            port=int(mail_env.get("MAIL_IMAP_PORT", "993")),
        )
        letters = await MailBox(connect=settings.connect, folder=settings.folder).fetch_new()
    if not letters:
        print("Новых писем с отчётами нет")
        return 0

    mode = ("БОЕВОЙ РЕЖИМ — записи уйдут в amoCRM" if args.live
            else "репетиция — в amoCRM ничего не пишем")
    print(f"Писем: {len(letters)}. Режим: {mode}.")
    if allowed:
        print(f"Белый список сделок: {', '.join(str(x) for x in sorted(allowed))}. "
              f"Создавать что-либо в амо запрещено.")
    print()

    if allowed:
        client = AllowListAmo(base_url=base_url, token=token, dry_run=not args.live,
                              allowed=allowed)
    else:
        client = AmoClient(base_url=base_url, token=token, dry_run=not args.live)
    store = MemoryCarpetStore()
    engine = CarpetEngine(amo=client, store=store, dry_run=not args.live)
    processed = 0
    try:
        for letter in letters:
            print(f"===== {letter.subject} =====")
            for name, data in letter.attachments.items():
                rows = parse_report(data)
                completed, refused = rows_to_process(rows)
                print(f"  {name}: к проведению {len(completed)}, отказов {len(refused)}\n")

                for row in completed + refused:
                    link = await engine.process_row(row, source_file=name)
                    print(describe(row, link, store.actions))
                    processed += 1
    finally:
        await client.close()

    print(f"\nИтого строк: {processed}, действий: {len(store.actions)}.")
    if not args.live:
        print("Это была репетиция: в amoCRM не изменилось ничего, письма не тронуты.")
    return 0


def describe(row, link, actions: list[dict]) -> str:
    head = (f"  Заказ партнёра №{row.partner_id} · {for_owner(row.phone10)} · "
            f"{row.amount} ₽ · {row.district or 'район не указан'}"
            + (f" · заказ у партнёра {row.added_date:%d.%m.%Y}" if row.added_date else ""))
    if row.is_refusal:
        head += f" · ОТКАЗ: {row.refusal_reason or 'причина не указана'}"
    elif row.return_date:
        head += f" · сдано {row.return_date:%d.%m}"

    lines = [head, f"     итог: {STATUS_NAMES.get(link.status, link.status)}"]
    if link.lead_id:
        lines.append(f"     ковровая сделка: #{link.lead_id}")
    if link.primary_lead_id:
        lines.append(f"     лид первичной: #{link.primary_lead_id}")
    if link.last_error:
        lines.append(f"     ошибка: {link.last_error}")

    own = [action for action in actions if action["partner_id"] == row.partner_id]
    for action in own:
        name = ACTION_NAMES.get(action["action"], action["action"])
        target = f" #{action['amo_id']}" if action["amo_id"] else ""
        detail = _detail(action)
        lines.append(f"     • {name}{target}{detail}")
    if not own:
        lines.append("     • действий не требуется")
    return "\n".join(lines) + "\n"


def _detail(action: dict) -> str:
    payload = action.get("payload")
    if action["action"] == "move_lead" and isinstance(payload, dict):
        key = (payload.get("pipeline_id"), payload.get("status_id"))
        stage = STAGE_NAMES.get(key, payload.get("status_id"))
        return f" → {stage}"
    if action["action"] == "update_lead" and isinstance(payload, dict):
        price = payload.get("price")
        fields = payload.get("custom_fields_values") or []
        return f": бюджет {price} ₽, полей {len(fields)}"
    if action["action"] == "ask_owner" and isinstance(payload, dict):
        return f": {payload.get('reason')}"
    return ""


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
