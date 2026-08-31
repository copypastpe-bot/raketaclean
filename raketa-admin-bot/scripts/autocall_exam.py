"""Экзамен фильтра autocall на боевой истории amoCRM — ТОЛЬКО ЧТЕНИЕ.

Перед включением автозвонка проверяем на настоящих данных, что робот возьмёт
в работу ровно заявки с сайта и ни одной чужой сделки:

  1) этап «Новый лид» за 60 дней: сколько сделок всего, сколько с тегом
     «Заявка с сайта» (их робот возьмёт), сколько без тега (их не тронет);
  2) обратная проверка полноты: сколько сделок с тегом уже УШЛИ с этапа —
     наблюдатель их не увидит, для старых заявок это норма, но цифру знаем;
  3) телефон: у скольких сайтовых заявок он извлекается (цель 100%);
  4) сырой вид `_embedded.tags` в списочном ответе — подтверждение, что тег
     виден без отдельного запроса, и заодно его настоящий id;
  5) задержка «письмо → сделка» по свежим заявкам: `created_at` сделки против
     времени письма в её ленте примечаний.

Телефоны в отчёте маскируются до последних 4 цифр: отчёт уходит в текст
сессии и может попасть в git-артефакты. Id сделок выводить можно.

Запуск на сервере (амо доступна только оттуда), из /opt/raketa-admin-bot/app:

    sudo -u adminbot env $(grep -E "^(AMO_|ADMINBOT_|BOT_DB_)" \\
        /opt/raketa-admin-bot/.env | xargs) \\
        /opt/raketa-admin-bot/.venv/bin/python -m scripts.autocall_exam
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from adminbot.amo import ids
from adminbot.amo.client import MAX_PAGES, PAGE_LIMIT, AmoClient
from adminbot.amo.fields import lead_contact_ids
from adminbot.autocall.leads import SITE_TAG, is_site_lead, lead_phone10
from adminbot.config import Settings
from adminbot.phone import mask

DAYS = 60                    # окно экзамена — последние два месяца истории
FRESH_FOR_MAIL = 5           # у скольких свежих заявок замеряем «письмо → сделка»
PHONE_EXAMPLES = 10          # сколько замаскированных примеров показать
TAGS_PREVIEW_LIMIT = 300     # сырой repr тегов длиннее не нужен


# --- разборная часть: работает над уже полученными ответами амо ---

def split_by_tag(leads: Iterable[Mapping[str, Any]]
                 ) -> tuple[list[dict], list[dict]]:
    """Разложить сделки этапа: с тегом «Заявка с сайта» и без него."""
    site: list[dict] = []
    others: list[dict] = []
    for lead in leads:
        (site if is_site_lead(lead) else others).append(dict(lead))
    return site, others


def gone_from_stage(leads: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Сделки с тегом сайта, которые уже НЕ на этапе «Новый лид».

    Вход — сделки за период по всем воронкам. Наблюдатель таких не увидит:
    для старых заявок это норма, но цифра нужна в отчёте.
    """
    return [
        dict(lead) for lead in leads
        if is_site_lead(lead)
        and not (int(lead.get("pipeline_id") or 0) == ids.PIPELINE_PRIMARY
                 and int(lead.get("status_id") or 0) == ids.PRIM_STAGE_NEW_LEAD)
    ]


def site_tag_id(lead: Optional[Mapping[str, Any]]) -> Optional[int]:
    """id тега «Заявка с сайта» — план велит прочитать его экзаменом."""
    if not lead:
        return None
    wanted = SITE_TAG.casefold()
    for tag in (lead.get("_embedded") or {}).get("tags") or []:
        if not isinstance(tag, Mapping) or tag.get("id") is None:
            continue
        if str(tag.get("name") or "").strip().casefold() == wanted:
            try:
                return int(tag["id"])
            except (TypeError, ValueError):
                continue
    return None


def tags_preview(lead: Optional[Mapping[str, Any]],
                 limit: int = TAGS_PREVIEW_LIMIT) -> str:
    """Сырой вид _embedded.tags — обрезанный repr, без телефонов и имён.

    В тегах только id, название тега и цвет — ПД клиента там нет.
    """
    raw = repr(((lead or {}).get("_embedded") or {}).get("tags"))
    return raw if len(raw) <= limit else raw[:limit] + "…"


def note_type_counts(notes: Iterable[Mapping[str, Any]]) -> list[tuple[str, int]]:
    """Реальные типы примечаний сделки с количеством — чтобы узнать фактический."""
    counter = Counter(str(note.get("note_type") or "?") for note in notes)
    return counter.most_common()


def find_mail_note(notes: Iterable[Mapping[str, Any]]) -> Optional[dict]:
    """Примечание письма — самое раннее с «mail» в типе (amomail_message и т.п.).

    Самое раннее, потому что письмо с сайта — первое событие в ленте заявки.
    None — письмо программно не видно; отчёт честно назовёт реальные типы.
    """
    mail_notes = [
        note for note in notes
        if "mail" in str(note.get("note_type") or "").lower()
        and note.get("created_at") is not None
    ]
    if not mail_notes:
        return None
    return dict(min(mail_notes, key=lambda note: int(note["created_at"])))


def delay_sec(lead: Mapping[str, Any], note: Mapping[str, Any]) -> Optional[int]:
    """Задержка «письмо → сделка»: created_at сделки минус created_at письма."""
    lead_ts, note_ts = lead.get("created_at"), note.get("created_at")
    if lead_ts is None or note_ts is None:
        return None
    return int(lead_ts) - int(note_ts)


def _fmt_sec(sec: int) -> str:
    minutes, seconds = divmod(abs(sec), 60)
    return f"{minutes} мин {seconds} с" if minutes else f"{seconds} с"


def describe_delay(sec: int) -> str:
    """Задержка словами. Отрицательная — письмо легло в ленту после сделки:
    значит, по created_at примечания задержку письма не измерить."""
    if sec >= 0:
        return f"сделка появилась через {_fmt_sec(sec)} после письма"
    return f"примечание письма записано на {_fmt_sec(sec)} позже создания сделки"


@dataclass(frozen=True)
class MailRow:
    """Итог замера по одной свежей заявке."""

    lead_id: int
    delay: Optional[int]              # None — письмо в ленте не нашлось
    types: list[tuple[str, int]]      # реальные типы примечаний с количеством


def mail_row(lead: Mapping[str, Any], notes: list[dict]) -> MailRow:
    note = find_mail_note(notes)
    return MailRow(
        lead_id=int(lead["id"]),
        delay=None if note is None else delay_sec(lead, note),
        types=note_type_counts(notes),
    )


def build_report(*, days: int, observer_total: int,
                 site: list[dict], others: list[dict],
                 tagged_total: int, gone: list[dict], all_total: int,
                 phones: dict[int, Optional[str]], tags_raw: str,
                 mail_rows: list[MailRow]) -> str:
    """Отчёт владельцу: русский, цифры с пояснениями, телефоны замаскированы."""
    lines = [
        f"=== Экзамен autocall: заявки с сайта за последние {days} дней ===",
        "",
        "1. Что увидит наблюдатель (этап «Новый лид», воронка первичной обработки)",
        f"   Сделок на этапе, созданных за {days} дней: {observer_total}",
        f"   с тегом «{SITE_TAG}» — их робот возьмёт в работу: {len(site)}",
        f"   без тега — их робот не тронет: {len(others)}",
    ]
    if not site and others:
        lines += [
            "   ВНИМАНИЕ: на этапе есть сделки, но ни одного тега не видно —",
            "   проверить, отдаёт ли списочный ответ _embedded.tags.",
        ]

    lines += [
        "",
        "2. Обратная проверка полноты (все воронки, те же дни)",
        f"   Всего сделок за период: {all_total}, из них с тегом «{SITE_TAG}»: {tagged_total}",
        f"   ушли с этапа «Новый лид»: {len(gone)} — наблюдатель их уже не увидит;",
        "   для старых, давно разобранных заявок это норма.",
    ]
    if all_total >= PAGE_LIMIT * MAX_PAGES:
        lines.append(
            f"   ВНИМАНИЕ: достигнут предел выборки в {PAGE_LIMIT * MAX_PAGES} "
            "сделок — цифры этого пункта могут быть неполными."
        )

    with_phone = [(lead_id, phone) for lead_id, phone in sorted(phones.items()) if phone]
    without_phone = [lead_id for lead_id, phone in sorted(phones.items()) if not phone]
    lines += [
        "",
        "3. Телефон по сайтовым заявкам с этапа",
        f"   извлёкся: {len(with_phone)} из {len(phones)} (цель 100%)",
    ]
    for lead_id in without_phone:
        lines.append(f"   без телефона: #{lead_id} — робот по ней не позвонит")
    if with_phone:
        examples = ", ".join(f"#{lead_id} {mask(phone)}"
                             for lead_id, phone in with_phone[:PHONE_EXAMPLES])
        lines.append(f"   примеры (телефон — последние 4 цифры): {examples}")

    lines += [
        "",
        "4. Сырой вид _embedded.tags в списочном ответе",
        f"   {tags_raw or '—'}",
        "",
        f"5. Задержка «письмо → сделка» ({len(mail_rows)} свежих заявок)",
    ]
    for row in mail_rows:
        types = ", ".join(f"{name}×{count}" for name, count in row.types) or "лента пуста"
        if row.delay is None:
            lines.append(f"   #{row.lead_id}: письмо в примечаниях не видно, "
                         f"типы такие: {types}")
        else:
            lines.append(f"   #{row.lead_id}: {describe_delay(row.delay)} "
                         f"(типы примечаний: {types})")
    if not mail_rows:
        lines.append("   свежих сайтовых заявок нет — замерять не на чем")

    lines += ["", "Телефоны в отчёте маскированы до последних 4 цифр."]
    return "\n".join(lines)


# --- сетевая часть: проверяется запуском на VPS, тестами не покрывается ---

async def main() -> int:
    settings = Settings.from_env()
    amo = AmoClient(base_url=settings.amo_base_url, token=settings.amo_token,
                    dry_run=True)                       # только чтение
    since = int(time.time()) - DAYS * 86400
    try:
        print(f"Читаю этап «Новый лид» за {DAYS} дней…", flush=True)
        observer = await amo.find_leads_created_since(
            ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD, since)
        site, others = split_by_tag(observer)

        print("Обратная проверка: все сделки за период, по всем воронкам…", flush=True)
        everything = await amo.get_all(
            "/api/v4/leads", "leads",
            params=[("filter[created_at][from]", since), ("with", "contacts")],
        )
        tagged_total = sum(1 for lead in everything if is_site_lead(lead))
        gone = gone_from_stage(everything)

        print(f"Достаю контакты {len(site)} сайтовых заявок…", flush=True)
        phones: dict[int, Optional[str]] = {}
        contact_cache: dict[int, Optional[dict]] = {}
        for lead in site:
            contacts = []
            for contact_id in lead_contact_ids(lead):
                if contact_id not in contact_cache:
                    contact_cache[contact_id] = await amo.get_contact(contact_id)
                if contact_cache[contact_id]:
                    contacts.append(contact_cache[contact_id])
            phones[int(lead["id"])] = lead_phone10(lead, contacts)

        tag_id = next((tid for lead in site
                       if (tid := site_tag_id(lead)) is not None), None)
        tags_raw = tags_preview(site[0]) if site else ""

        fresh = sorted(site, key=lambda lead: int(lead.get("created_at") or 0))
        fresh = list(reversed(fresh[-FRESH_FOR_MAIL:]))          # свежие первыми
        print(f"Читаю примечания {len(fresh)} свежих заявок…", flush=True)
        mail_rows = []
        for lead in fresh:
            notes = await amo.get_all(f"/api/v4/leads/{int(lead['id'])}/notes", "notes")
            mail_rows.append(mail_row(lead, notes))
    finally:
        await amo.close()

    print()
    print(build_report(days=DAYS, observer_total=len(observer),
                       site=site, others=others,
                       tagged_total=tagged_total, gone=gone,
                       all_total=len(everything), phones=phones,
                       tags_raw=tags_raw, mail_rows=mail_rows))
    if tag_id is not None:
        print(f"\nid тега «{SITE_TAG}»: {tag_id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
