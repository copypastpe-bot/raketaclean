"""Экзамен на истории: прогон матчера по заказам бота против реальной amoCRM.

РЕЖИМ ТОЛЬКО ЧТЕНИЕ. Скрипт ничего не меняет ни в амо, ни в БД бота.

Два режима:

* `--mode as-of-order` (по умолчанию) — «экзамен во времени». Для каждого заказа
  восстанавливаем, как выглядела CRM В МОМЕНТ заказа: какие сделки уже
  существовали и какие были ещё открыты. Это проверяет ту ветку, которой робот
  будет жить каждый день — выбор среди ОТКРЫТЫХ сделок.

* `--mode now` — прогон по сегодняшнему состоянию CRM. Вся история уже разобрана
  владельцем, поэтому здесь почти всегда срабатывает ветка «уже проведено руками».

Считаем три числа из дизайна §6:
  - доля решённых автоматически (цель ≥92%);
  - доля вопросов владельцу (цель ≤8%);
  - «уверенно, но неверно» (цель 0) — самое важное число.

Запуск:
    python -m scripts.history_exam --days 90

Доступы (оба на чтение):
    DB_DSN            — из ~/Projects/tgbot-v1/.env (--bot-env)
    AMOCRM_API_TOKEN  — из ./.env.exam (--amo-env)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
from dotenv import dotenv_values

from adminbot.amo import ids
from adminbot.amo.client import AmoClient
from adminbot.amo.fields import MOSCOW_TZ, order_date_msk, specialist_ids
from adminbot.models import Order
from adminbot.phone import last10
from adminbot.sync.matcher import (
    CLOSED_WINDOW_DAYS, DATE_WINDOW_DAYS, STALE_LEAD_DAYS, Decision, LeadInfo, match,
)
from adminbot.sync.specialists import SpecialistIndex

DEFAULT_BOT_ENV = Path.home() / "Projects" / "tgbot-v1" / ".env"
DEFAULT_AMO_ENV = Path(__file__).resolve().parent.parent / ".env.exam"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "docs" / "plans" / "history-exam-result.md"

# Потолок здравого смысла: рекорд по разведке — 53 сделки у постоянного B2B-клиента.
# Больше сотни означает, что амо отдала чужие сделки, а не сделки клиента.
MAX_LEADS_PER_CLIENT = 150

VERDICT_AUTO = "auto"                 # решено автоматически и совпало с фактом
VERDICT_WEAK = "weak"                 # совпало, но факт подтверждён слабым признаком
VERDICT_PENDING = "pending"           # робот сработал бы, заказ ещё не проведён руками
VERDICT_QUESTION = "question"         # вопрос: какую сделку брать
VERDICT_QUESTION_STALE = "question_stale"   # вопрос: только старые хвосты, что делать
VERDICT_WRONG = "wrong"               # уверенно, но мимо — этого быть не должно
VERDICT_NO_PHONE = "no_phone"         # телефон заказа не распознан

# Названия признаков, которыми опознан «факт»
SOURCE_ORDER_DATE = "по полю «Дата и время заказа»"
SOURCE_CLOSED = "по дате закрытия сделки"
WEAK_SOURCES = (SOURCE_CLOSED,)


@dataclass
class ExamRow:
    order: Order
    decision: Decision
    fact_lead_id: Optional[int]
    verdict: str
    note: str = ""
    candidates: list[LeadInfo] = field(default_factory=list)

    @property
    def phone_masked(self) -> str:
        return "…" + (self.order.phone10 or "0000")[-4:]


ORDERS_SQL = """
SELECT o.id, o.phone, o.phone_digits, o.customer_name, o.created_at,
       o.amount_total, o.rating_score,
       c.full_name AS client_full_name,
       COALESCE(NULLIF(TRIM(c.address), ''), NULLIF(TRIM(c.last_order_addr), '')) AS address,
       COALESCE((
           SELECT jsonb_agg(jsonb_build_array(m.name, m.phone) ORDER BY m.is_primary DESC, m.name)
           FROM (
               SELECT COALESCE(NULLIF(TRIM(s.full_name), ''),
                               NULLIF(TRIM(CONCAT_WS(' ', s.first_name, s.last_name)), '')) AS name,
                      s.phone AS phone,
                      (s.id = o.master_id) AS is_primary
               FROM public.staff s
               WHERE s.id = o.master_id
                  OR s.id IN (SELECT om.master_id FROM public.order_masters om WHERE om.order_id = o.id)
           ) m
           WHERE m.name IS NOT NULL
       ), '[]'::jsonb) AS masters
FROM public.orders o
LEFT JOIN public.clients c ON c.id = o.client_id
WHERE o.created_at >= now() - ($1::int || ' days')::interval
ORDER BY o.created_at, o.id
"""


async def _init_json(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def load_orders(dsn: str, days: int) -> list[Order]:
    conn = await asyncpg.connect(dsn)
    try:
        await _init_json(conn)          # у одиночного подключения кодек ставится вручную
        rows = await conn.fetch(ORDERS_SQL, days)
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
            client_name=row["client_full_name"] or row["customer_name"],
            address=row["address"],
        )
        for row in rows
    ]


def _to_msk_date(stamp: Optional[int]) -> Optional[date]:
    if not stamp:
        return None
    return datetime.fromtimestamp(int(stamp), tz=timezone.utc).astimezone(MOSCOW_TZ).date()


def _closed_date(lead: dict) -> Optional[date]:
    return _to_msk_date(lead.get("closed_at"))


def _created_date(lead: dict) -> Optional[date]:
    return _to_msk_date(lead.get("created_at"))


def to_lead_info(lead: dict) -> LeadInfo:
    """Сделка в том виде, в каком её видит матчер СЕГОДНЯ."""
    return LeadInfo(
        lead_id=int(lead["id"]),
        pipeline_id=int(lead.get("pipeline_id") or 0),
        status_id=int(lead.get("status_id") or 0),
        order_date=order_date_msk(lead),
        closed_date=_closed_date(lead),
        created_date=_created_date(lead),
        specialist_ids=specialist_ids(lead),
        name=lead.get("name"),
    )


# Чем подменяем неизвестный этап у сделки, которая на момент заказа была открыта.
# Точный этап в истории не сохраняется, но матчеру важно лишь «открыта или нет».
OPEN_STAGE_STANDIN = {
    ids.PIPELINE_REALIZATION: ids.REAL_STAGE_CREATED,
    ids.PIPELINE_PRIMARY: ids.PRIM_STAGE_NEW_LEAD,
}


def to_lead_info_as_of(lead: dict, moment: datetime) -> Optional[LeadInfo]:
    """Сделка в том виде, в каком её увидел бы робот В МОМЕНТ заказа.

    None — если на тот момент сделки ещё не существовало.
    """
    created_at = lead.get("created_at")
    if created_at and int(created_at) > moment.timestamp():
        return None

    closed_at = lead.get("closed_at")
    was_open = not closed_at or int(closed_at) > moment.timestamp()
    pipeline_id = int(lead.get("pipeline_id") or 0)
    status_id = int(lead.get("status_id") or 0)

    if was_open and status_id in ids.STATUSES_FINAL:
        # Сейчас закрыта, но тогда была ещё в работе: точный этап неизвестен.
        status_id = OPEN_STAGE_STANDIN.get(pipeline_id, ids.REAL_STAGE_CREATED)

    return LeadInfo(
        lead_id=int(lead["id"]),
        pipeline_id=pipeline_id,
        status_id=status_id,
        order_date=order_date_msk(lead),
        closed_date=None if was_open else _closed_date(lead),
        created_date=_created_date(lead),
        specialist_ids=specialist_ids(lead),
        name=lead.get("name"),
    )


def find_fact_lead(order_date: date, leads: list[dict]) -> tuple[Optional[int], str]:
    """Какая сделка РЕАЛЬНО проведена по этому заказу (факт для сверки).

    Сначала по полю «Дата и время заказа» (±2 дня), затем по моменту закрытия
    сделки (±3 дня) — для сделок, где поле даты не заполняли.

    Дату создания сделки здесь СОЗНАТЕЛЬНО не используем: этим признаком
    пользуется сам матчер, и проверка перестала бы быть независимой.
    """
    completed = [
        lead for lead in leads
        if int(lead.get("pipeline_id") or 0) == ids.PIPELINE_REALIZATION
        and int(lead.get("status_id") or 0) == ids.STATUS_SUCCESS
    ]
    if not completed:
        return None, ""

    dated = [
        (abs((order_date_msk(lead) - order_date).days), int(lead["id"]))
        for lead in completed if order_date_msk(lead) is not None
        and abs((order_date_msk(lead) - order_date).days) <= DATE_WINDOW_DAYS
    ]
    if dated:
        return min(dated)[1], SOURCE_ORDER_DATE

    by_closed = [
        (abs((_closed_date(lead) - order_date).days), int(lead["id"]))
        for lead in completed if _closed_date(lead) is not None
        and abs((_closed_date(lead) - order_date).days) <= CLOSED_WINDOW_DAYS
    ]
    if by_closed:
        return min(by_closed)[1], SOURCE_CLOSED

    return None, ""


def classify(order: Order, decision: Decision, fact_lead_id: Optional[int],
             candidates: list[LeadInfo], fact_source: str = "") -> tuple[str, str]:
    """Сравнить решение матчера с фактом."""
    if decision.kind == "ask_owner":
        return VERDICT_QUESTION, f"кандидаты: {', '.join('#' + str(x) for x in decision.options)}"

    if decision.kind == "ask_owner_stale":
        return (VERDICT_QUESTION_STALE,
                f"только старые хвосты: {', '.join('#' + str(x) for x in decision.options)}")

    hit_kinds = ("use_realization", "already_done", "use_primary")
    if decision.kind in hit_kinds:
        target = decision.lead_id
        if fact_lead_id is None:
            open_target = any(c.lead_id == target and c.is_open for c in candidates)
            if open_target:
                return VERDICT_PENDING, f"взял бы сделку #{target}, руками ещё не проведено"
            if decision.kind == "use_primary":
                return VERDICT_AUTO, f"через первичную сделку #{target} (путь Б)"
            return VERDICT_WRONG, f"взял #{target}, но проведённой сделки по заказу не видно"
        if target == fact_lead_id:
            verdict = VERDICT_WEAK if fact_source in WEAK_SOURCES else VERDICT_AUTO
            return verdict, f"сделка #{target}" + (f" ({fact_source})" if fact_source else "")
        if decision.kind == "use_primary":
            return VERDICT_WRONG, f"пошёл бы через первичную #{target}, а проведена #{fact_lead_id}"
        return VERDICT_WRONG, f"взял #{target}, а проведена #{fact_lead_id}"

    # create_new
    if fact_lead_id is None:
        return VERDICT_AUTO, "сделки нет — создал бы новую (путь В)"
    return VERDICT_WRONG, f"создал бы дубль: сделка #{fact_lead_id} уже проведена"


async def collect_candidates(client: AmoClient, phone10: str, cache: dict) -> list[dict]:
    """Все сделки, найденные по телефону клиента. Кэш — постоянные клиенты повторяются."""
    if phone10 in cache:
        return cache[phone10]

    leads = await client.find_leads_by_phone(phone10)
    # Страховка от аварии 2026-08-25: если из амо вдруг снова поедет вся база
    # вместо сделок клиента, лучше остановиться, чем скормить матчеру чужое.
    if len(leads) > MAX_LEADS_PER_CLIENT:
        raise RuntimeError(
            f"по телефону …{phone10[-4:]} пришло {len(leads)} сделок — "
            f"это не похоже на одного клиента, проверьте запрос к амо"
        )
    cache[phone10] = leads
    return leads


async def run_exam(orders: list[Order], client: AmoClient, pause: float,
                   specialists: SpecialistIndex, as_of_order: bool) -> list[ExamRow]:
    cache: dict[str, list[dict]] = {}
    rows: list[ExamRow] = []

    for index, order in enumerate(orders, 1):
        if not order.phone10:
            rows.append(ExamRow(order, Decision(kind="create_new"), None, VERDICT_NO_PHONE,
                                "телефон заказа не распознан"))
            continue

        raw_leads = await collect_candidates(client, order.phone10, cache)

        if as_of_order:
            candidates = [info for info in
                          (to_lead_info_as_of(lead, order.created_at) for lead in raw_leads)
                          if info is not None]
        else:
            candidates = [to_lead_info(lead) for lead in raw_leads]

        master_ids = specialists.resolve_many(order.masters)
        decision = match(order_date=order.order_date, candidates=candidates,
                         master_specialist_ids=master_ids)
        fact_lead_id, fact_source = find_fact_lead(order.order_date, raw_leads)
        verdict, note = classify(order, decision, fact_lead_id, candidates, fact_source)
        rows.append(ExamRow(order, decision, fact_lead_id, verdict, note, candidates))

        if index % 25 == 0:
            print(f"  обработано {index}/{len(orders)}…", flush=True)
        await asyncio.sleep(pause)

    return rows


def _listing(rows: list[ExamRow], title: str, note: str = "") -> list[str]:
    if not rows:
        return []
    lines = [f"## {title}", ""]
    if note:
        lines += [note, ""]
    for row in rows:
        lines.append(
            f"- Заказ №{row.order.order_id} · {row.phone_masked} · "
            f"{row.order.order_date.isoformat()} — {row.note}"
        )
    lines.append("")
    return lines


def build_report(rows: list[ExamRow], days: int, as_of_order: bool) -> str:
    counts = Counter(row.verdict for row in rows)
    total = len(rows) or 1
    auto = counts[VERDICT_AUTO] + counts[VERDICT_WEAK] + counts[VERDICT_PENDING]
    questions = counts[VERDICT_QUESTION] + counts[VERDICT_QUESTION_STALE]

    def pct(n: int) -> str:
        return f"{100 * n / total:.1f}%"

    mode = ("во времени: CRM восстановлена на момент каждого заказа"
            if as_of_order else "по сегодняшнему состоянию CRM")

    lines = [
        "# Экзамен на истории — результат",
        "",
        f"Дата прогона: {datetime.now(MOSCOW_TZ).date().isoformat()}. "
        f"Период: {days} дней. Заказов: {len(rows)}. Режим: только чтение, {mode}.",
        "",
        "## Итог",
        "",
        "| Исход | Кол-во | Доля |",
        "|---|---|---|",
        f"| Решено автоматически и подтверждено фактом | {counts[VERDICT_AUTO]} | {pct(counts[VERDICT_AUTO])} |",
        f"| Совпало, но подтверждение слабое | {counts[VERDICT_WEAK]} | {pct(counts[VERDICT_WEAK])} |",
        f"| Сработал бы, заказ ещё не проведён руками | {counts[VERDICT_PENDING]} | {pct(counts[VERDICT_PENDING])} |",
        f"| Вопрос: какую сделку брать | {counts[VERDICT_QUESTION]} | {pct(counts[VERDICT_QUESTION])} |",
        f"| Вопрос: только старые хвосты | {counts[VERDICT_QUESTION_STALE]} | {pct(counts[VERDICT_QUESTION_STALE])} |",
        f"| **Уверенно, но неверно** | **{counts[VERDICT_WRONG]}** | {pct(counts[VERDICT_WRONG])} |",
        f"| Телефон не распознан | {counts[VERDICT_NO_PHONE]} | {pct(counts[VERDICT_NO_PHONE])} |",
        "",
        f"**Автоматически проводимо: {pct(auto)}** (порог дизайна ≥92%). "
        f"Вопросов: {pct(questions)} (порог ≤8%). "
        f"Уверенно-неверных: {counts[VERDICT_WRONG]} (порог 0).",
        "",
    ]

    lines += _listing([r for r in rows if r.verdict in (VERDICT_WRONG, VERDICT_NO_PHONE)],
                      "Ошибки (разбирать в первую очередь)")
    lines += _listing([r for r in rows if r.verdict == VERDICT_WEAK],
                      "Подтверждено слабым признаком — проверить глазами",
                      "Сделка опознана по дате закрытия, а не по «Дате и времени заказа».")
    lines += _listing([r for r in rows if r.verdict == VERDICT_QUESTION],
                      "Вопросы: какую сделку брать")
    lines += _listing([r for r in rows if r.verdict == VERDICT_QUESTION_STALE],
                      "Вопросы: остались только старые хвосты",
                      "Робот предложит две кнопки: «заводи новую» и «сам разберусь».")

    lines += [
        "## Как считалось",
        "",
        "«Факт» — сделка воронки реализации на успешном этапе, найденная по полю "
        f"«Дата и время заказа» (±{DATE_WINDOW_DAYS} дня), а если оно пустое или неверное — "
        f"по дате закрытия сделки (±{CLOSED_WINDOW_DAYS} дня). Дата создания сделки в проверке "
        "СОЗНАТЕЛЬНО не используется: этим признаком пользуется матчер, и проверка "
        "перестала бы быть независимой.",
        "",
        f"Граница «живая сделка / старый хвост» — {STALE_LEAD_DAYS} дней "
        "(измерено по истории: медиана 1 день, максимум 22 дня).",
        "",
        "Телефоны маскированы до последних 4 цифр.",
        "",
    ]
    if as_of_order:
        lines += [
            "Оговорка режима «во времени»: точный этап сделки на момент заказа в амо "
            "не сохраняется. Известно лишь, была ли она тогда открыта — этого матчеру "
            "достаточно, но различить «Неразобранное» и «Новый лид» задним числом нельзя.",
            "",
        ]
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Экзамен матчера на истории (только чтение)")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--mode", choices=("as-of-order", "now"), default="as-of-order",
                        help="as-of-order: CRM на момент заказа; now: сегодняшнее состояние")
    parser.add_argument("--bot-env", type=Path, default=DEFAULT_BOT_ENV)
    parser.add_argument("--amo-env", type=Path, default=DEFAULT_AMO_ENV)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--pause", type=float, default=0.15, help="пауза между заказами, сек")
    parser.add_argument("--limit", type=int, default=0, help="взять только первые N заказов (отладка)")
    args = parser.parse_args()

    bot_env = dotenv_values(args.bot_env)
    amo_env = dotenv_values(args.amo_env)

    dsn = bot_env.get("DB_DSN")
    token = amo_env.get("AMOCRM_API_TOKEN")
    base_url = (amo_env.get("AMOCRM_API_BASE") or "").rstrip("/")
    if not base_url and amo_env.get("AMOCRM_ACCOUNT_DOMAIN"):
        base_url = f"https://{amo_env['AMOCRM_ACCOUNT_DOMAIN'].strip()}"

    missing = [name for name, value in
               (("DB_DSN", dsn), ("AMOCRM_API_TOKEN", token), ("AMOCRM_API_BASE", base_url)) if not value]
    if missing:
        print(f"Не хватает доступов: {', '.join(missing)}", file=sys.stderr)
        print(f"  DB_DSN ожидается в {args.bot_env}", file=sys.stderr)
        print(f"  AMOCRM_* ожидаются в {args.amo_env}", file=sys.stderr)
        return 2

    as_of_order = args.mode == "as-of-order"
    print(f"Беру заказы бота за {args.days} дней…", flush=True)
    orders = await load_orders(dsn, args.days)
    if args.limit:
        orders = orders[: args.limit]
    print(f"Заказов: {len(orders)}. Иду в amoCRM (только чтение)…", flush=True)

    client = AmoClient(base_url=base_url, token=token)
    try:
        enums = await client.get_lead_field_enums(ids.FIELD_SPECIALIST)
        specialists = SpecialistIndex.from_enums(enums)
        print(f"Справочник «Специалист»: {len(enums)} значений, "
              f"из них с телефоном {len(specialists.by_phone)}", flush=True)
        rows = await run_exam(orders, client, args.pause, specialists, as_of_order)
    finally:
        await client.close()

    report = build_report(rows, args.days, as_of_order)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")

    print()
    print(report.split("## Ошибки")[0].split("## Подтверждено")[0].split("## Вопросы")[0])
    print(f"Отчёт сохранён: {args.out}")

    return 1 if any(row.verdict == VERDICT_WRONG for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
