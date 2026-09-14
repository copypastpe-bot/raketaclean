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
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from adminbot.amo import ids
from adminbot.amo.client import MAX_PAGES, PAGE_LIMIT, AmoClient
from adminbot.amo.fields import MOSCOW_TZ, lead_contact_ids, lead_tag_names
from adminbot.autocall.leads import SITE_TAG, is_site_lead, lead_phone10
from adminbot.config import Settings
from adminbot.phone import mask

DAYS = 60                    # окно экзамена — последние два месяца истории
FRESH_FOR_MAIL = 5           # у скольких свежих заявок замеряем «письмо → сделка»
PHONE_EXAMPLES = 10          # сколько замаскированных примеров показать
PHONE_FALLBACK_COUNT = 10    # этап пуст → телефоны меряем по стольким свежим заявкам
TAGS_PREVIEW_LIMIT = 300     # сырой repr тегов длиннее не нужен
TAG_PROBE_LIMIT = 10         # сколько сделок этапа дотягиваем индивидуально

# Вердикты доследования «список против индивидуального ответа».
TAGS_HIDDEN = "hidden"       # индивидуально теги есть, в списке нет — список их прячет
TAGS_ABSENT = "absent"       # тегов нет нигде — на этапе просто нет сайтовых заявок
TAGS_VISIBLE = "visible"     # список показывает те же теги, что и индивидуальный ответ


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


@dataclass(frozen=True)
class TagProbe:
    """Одна сделка этапа: теги в списочном ответе против индивидуального."""

    lead_id: int
    in_list: tuple[str, ...]        # имена тегов в ответе списка по этапу
    individual: tuple[str, ...]     # имена тегов в ответе GET /leads/{id}


def probe_tags(list_lead: Mapping[str, Any],
               full_lead: Optional[Mapping[str, Any]]) -> TagProbe:
    """Сравнить теги одной сделки: как их видит список и как — прямой запрос."""
    return TagProbe(
        lead_id=int(list_lead["id"]),
        in_list=lead_tag_names(list_lead),
        individual=lead_tag_names(full_lead),
    )


def tags_verdict(probes: Iterable[TagProbe]) -> str:
    """Итог доследования: прячет ли фильтрованный список теги.

    hidden — хоть у одной сделки теги видны индивидуально, но не в списке;
    absent — тегов нет нигде: на этапе просто нет сайтовых заявок;
    visible — список показывает то же, что и индивидуальные ответы.
    """
    probes = list(probes)
    if any(probe.individual and not probe.in_list for probe in probes):
        return TAGS_HIDDEN
    if all(not probe.individual and not probe.in_list for probe in probes):
        return TAGS_ABSENT
    return TAGS_VISIBLE


def freshest_lead(leads: Iterable[Mapping[str, Any]]) -> Optional[dict]:
    """Самая свежая сделка по created_at. Без created_at свежесть неизвестна."""
    dated = [lead for lead in leads if lead.get("created_at") is not None]
    if not dated:
        return None
    return dict(max(dated, key=lambda lead: int(lead["created_at"])))


def freshest_n(leads: Iterable[Mapping[str, Any]], count: int) -> list[dict]:
    """count самых свежих сделок по created_at, свежие первыми."""
    ordered = sorted(leads, key=lambda lead: int(lead.get("created_at") or 0))
    return [dict(lead) for lead in reversed(ordered[-count:] if count else [])]


def contacts_missing(leads: Iterable[Mapping[str, Any]]) -> list[int]:
    """id сделок, у которых в _embedded вовсе нет ключа contacts.

    Обратная проверка запрашивается с with=contacts: ключ должен быть даже
    пустым списком. Ключа нет — амо параметр не применила, и телефоны по
    таким сделкам честно не проверить (это не то же, что «нет телефона»).
    """
    missing: list[int] = []
    for lead in leads:
        embedded = lead.get("_embedded")
        if not isinstance(embedded, Mapping) or "contacts" not in embedded:
            missing.append(int(lead.get("id") or 0))
    return missing


def msk_stamp(ts: Optional[int]) -> str:
    """Unix-время → московское, как его видит владелец в амо."""
    if not ts:
        return "—"
    moment = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(MOSCOW_TZ)
    return moment.strftime("%d.%m.%Y %H:%M")


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
    """Итог замера по одной свежей сайтовой заявке (этап у неё может быть любой)."""

    lead_id: int
    delay: Optional[int]              # None — письмо в ленте не нашлось
    types: list[tuple[str, int]]      # реальные типы примечаний с количеством
    created_msk: str = ""             # когда создана сделка, по Москве
    pipeline_id: int = 0              # где сделка СЕЙЧАС — воронка
    status_id: int = 0                # и этап


def mail_row(lead: Mapping[str, Any], notes: list[dict]) -> MailRow:
    note = find_mail_note(notes)
    return MailRow(
        lead_id=int(lead["id"]),
        delay=None if note is None else delay_sec(lead, note),
        types=note_type_counts(notes),
        created_msk=msk_stamp(lead.get("created_at")),
        pipeline_id=int(lead.get("pipeline_id") or 0),
        status_id=int(lead.get("status_id") or 0),
    )


def build_report(*, days: int, observer_total: int,
                 site: list[dict], others: list[dict],
                 tagged_total: int, gone: list[dict], all_total: int,
                 phones: dict[int, Optional[str]], tags_raw: str,
                 mail_rows: list[MailRow],
                 tag_probes: Optional[list[TagProbe]] = None,
                 freshest: Optional[Mapping[str, Any]] = None,
                 phones_from_reverse: bool = False,
                 phones_no_contacts: Optional[list[int]] = None) -> str:
    """Отчёт владельцу: русский, цифры с пояснениями, телефоны замаскированы."""
    lines = [
        f"=== Экзамен autocall: заявки с сайта за последние {days} дней ===",
        "",
        "1. Что увидит наблюдатель (этап «Новый лид», воронка первичной обработки)",
        f"   Сделок на этапе, созданных за {days} дней: {observer_total}",
        f"   с тегом «{SITE_TAG}» — их робот возьмёт в работу: {len(site)}",
        f"   без тега — их робот не тронет: {len(others)}",
    ]
    if tag_probes:
        lines += ["", "   Доследование: теги в списке против индивидуального ответа"]
        for probe in tag_probes:
            names = f" ({', '.join(probe.individual)})" if probe.individual else ""
            lines.append(f"   сделка {probe.lead_id}: тегов в списке {len(probe.in_list)}, "
                         f"индивидуально {len(probe.individual)}{names}")
        verdict = tags_verdict(tag_probes)
        if verdict == TAGS_HIDDEN:
            lines += [
                "",
                "   " + "!" * 60,
                "   !!! ФИЛЬТРОВАННЫЙ СПИСОК ПРЯЧЕТ ТЕГИ: индивидуальный ответ",
                "   !!! показывает теги, а список по этапу — нет. Наблюдателю",
                "   !!! нельзя верить тегам из списочного ответа — фильтр autocall",
                "   !!! в таком виде не увидит НИ ОДНОЙ заявки с сайта.",
                "   " + "!" * 60,
            ]
        elif verdict == TAGS_ABSENT:
            lines.append("   Предупреждение снимается: тегов нет и в индивидуальных "
                         "ответах — на этапе просто нет сайтовых заявок.")
        else:
            lines.append("   Список показывает те же теги, что и индивидуальные "
                         "ответы, — фильтр их не прячет.")
    elif not site and others:
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
    if freshest is not None:
        lines.append(f"   самая свежая сайтовая заявка: #{int(freshest['id'])}, "
                     f"создана {msk_stamp(freshest.get('created_at'))} МСК")
    if all_total >= PAGE_LIMIT * MAX_PAGES:
        lines.append(
            f"   ВНИМАНИЕ: достигнут предел выборки в {PAGE_LIMIT * MAX_PAGES} "
            "сделок — цифры этого пункта могут быть неполными."
        )

    with_phone = [(lead_id, phone) for lead_id, phone in sorted(phones.items()) if phone]
    without_phone = [lead_id for lead_id, phone in sorted(phones.items()) if not phone]
    no_contacts = set(phones_no_contacts or [])
    if phones_from_reverse:
        # На этапе сайтовых заявок нет — метрика по самым свежим из обратной
        # проверки: телефон должен извлекаться у заявки с любого этапа одинаково.
        extracted = len(with_phone)
        lines += [
            "",
            "3. Телефон по сайтовым заявкам (на этапе их нет — взяты самые "
            "свежие из обратной проверки, любые этапы)",
            f"   извлёкся: {extracted} из {len(phones)} "
            "(по свежим из обратной проверки; цель 100%)",
        ]
        if no_contacts:
            lines.append(f"   ВНИМАНИЕ: у {len(no_contacts)} сделок в ответе "
                         "обратной проверки нет _embedded.contacts — параметр "
                         "with=contacts не сработал, телефон по ним не проверить.")
        for lead_id, phone in sorted(phones.items()):
            if lead_id in no_contacts:
                lines.append(f"   #{lead_id}: в ответе нет _embedded.contacts")
            elif phone:
                lines.append(f"   #{lead_id}: {mask(phone)}")
            else:
                lines.append(f"   #{lead_id}: НЕТ ТЕЛЕФОНА")
    else:
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
        f"5. Задержка «письмо → сделка» ({len(mail_rows)} самых свежих сайтовых "
        "заявок из обратной проверки, любые воронки и этапы)",
    ]
    for row in mail_rows:
        types = ", ".join(f"{name}×{count}" for name, count in row.types) or "лента пуста"
        head = (f"   #{row.lead_id} · создана {row.created_msk or '—'} МСК · "
                f"воронка {row.pipeline_id}, этап {row.status_id}")
        if row.delay is None:
            lines.append(f"{head}: письмо в примечаниях не видно, типы такие: {types}")
        else:
            lines.append(f"{head}: {describe_delay(row.delay)} (типы примечаний: {types})")
    if not mail_rows:
        lines.append("   сайтовых заявок за период нет — замерять не на чем")

    lines += ["", "Телефоны в отчёте маскированы до последних 4 цифр."]
    return "\n".join(lines)


# --- сетевая часть: проверяется запуском на VPS, тестами не покрывается ---

async def _lead_phones(amo: AmoClient, leads: list[dict],
                       contact_cache: dict[int, Optional[dict]]
                       ) -> dict[int, Optional[str]]:
    """Телефон по каждой сделке: дотягивает контакты по id, кэш общий на вызов."""
    phones: dict[int, Optional[str]] = {}
    for lead in leads:
        contacts = []
        for contact_id in lead_contact_ids(lead):
            if contact_id not in contact_cache:
                contact_cache[contact_id] = await amo.get_contact(contact_id)
            if contact_cache[contact_id]:
                contacts.append(contact_cache[contact_id])
        phones[int(lead["id"])] = lead_phone10(lead, contacts)
    return phones


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

        # Доследование: каждую сделку этапа дотягиваем индивидуально и сравниваем
        # теги с тем, что показал фильтрованный список.
        to_probe = observer[:TAG_PROBE_LIMIT]
        print(f"Доследование тегов: дотягиваю {len(to_probe)} сделок этапа "
              "по одной…", flush=True)
        tag_probes = []
        for lead in to_probe:
            full = await amo.get_lead(int(lead["id"]))
            tag_probes.append(probe_tags(lead, full))

        print("Обратная проверка: все сделки за период, по всем воронкам…", flush=True)
        everything = await amo.get_all(
            "/api/v4/leads", "leads",
            params=[("filter[created_at][from]", since), ("with", "contacts")],
        )
        tagged_all = [lead for lead in everything if is_site_lead(lead)]
        tagged_total = len(tagged_all)
        gone = gone_from_stage(everything)
        freshest = freshest_lead(tagged_all)

        contact_cache: dict[int, Optional[dict]] = {}
        phones_from_reverse = False
        phones_no_contacts: list[int] = []
        if site:
            print(f"Достаю контакты {len(site)} сайтовых заявок…", flush=True)
            phones = await _lead_phones(amo, site, contact_cache)
        else:
            # На этапе сайтовых заявок нет — метрику телефона меряем по самым
            # свежим сделкам с тегом из обратной проверки (любые воронки/этапы).
            phones_from_reverse = True
            fallback_leads = freshest_n(tagged_all, PHONE_FALLBACK_COUNT)
            phones_no_contacts = contacts_missing(fallback_leads)
            print(f"На этапе нет сайтовых заявок — достаю контакты "
                  f"{len(fallback_leads)} самых свежих из обратной проверки…",
                  flush=True)
            phones = await _lead_phones(amo, fallback_leads, contact_cache)

        # id тега и сырой вид тегов: сначала из списка по этапу, а если этап пуст —
        # из списка обратной проверки (это тоже списочный ответ амо).
        tag_id = next((tid for lead in site + tagged_all
                       if (tid := site_tag_id(lead)) is not None), None)
        if site:
            tags_raw = tags_preview(site[0])
        elif tagged_all:
            tags_raw = "(из списка обратной проверки) " + tags_preview(tagged_all[0])
        else:
            tags_raw = ""

        # Задержку меряем по самым свежим сайтовым заявкам обратной проверки:
        # на этапе их может уже не быть, а поток письма→сделки виден и так.
        fresh = sorted(tagged_all, key=lambda lead: int(lead.get("created_at") or 0))
        fresh = list(reversed(fresh[-FRESH_FOR_MAIL:]))          # свежие первыми
        print(f"Читаю примечания {len(fresh)} свежих сайтовых заявок…", flush=True)
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
                       tags_raw=tags_raw, mail_rows=mail_rows,
                       tag_probes=tag_probes, freshest=freshest,
                       phones_from_reverse=phones_from_reverse,
                       phones_no_contacts=phones_no_contacts))
    if tag_id is not None:
        print(f"\nid тега «{SITE_TAG}»: {tag_id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
