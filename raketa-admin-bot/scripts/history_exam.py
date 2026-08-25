"""Экзамен на истории: прогон матчера по заказам бота против реальной amoCRM.

РЕЖИМ ТОЛЬКО ЧТЕНИЕ. Скрипт ничего не меняет ни в амо, ни в БД бота.

Что делает: берёт заказы бота за период, по телефону каждого находит сделки в амо,
спрашивает у матчера решение и сравнивает его с фактом — какая сделка реально
проведена в воронке реализации. Считает три числа из дизайна §6:
  - доля решённых автоматически (цель ≥92%);
  - доля вопросов владельцу (цель ≤8%);
  - «уверенно, но неверно» (цель 0) — самое важное число.

Запуск:
    python -m scripts.history_exam --days 90

Доступы (оба на чтение):
    DB_DSN            — из ~/Projects/tgbot-v1/.env (--bot-env)
    AMOCRM_API_TOKEN  — из ./.env.exam (--amo-env), копируется с сервера владельцем
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
from dotenv import dotenv_values

from adminbot.amo import ids
from adminbot.amo.client import AmoClient
from adminbot.amo.fields import MOSCOW_TZ, order_date_msk
from adminbot.models import Order
from adminbot.phone import last10
from adminbot.sync.matcher import DATE_WINDOW_DAYS, Decision, LeadInfo, match

DEFAULT_BOT_ENV = Path.home() / "Projects" / "tgbot-v1" / ".env"
DEFAULT_AMO_ENV = Path(__file__).resolve().parent.parent / ".env.exam"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "docs" / "plans" / "history-exam-result.md"

# Окно для запасного признака «когда сделку реально закрыли». Поле «Дата и время
# заказа» заполнено лишь у 78% сделок, а вот момент закрытия есть всегда.
CLOSED_WINDOW_DAYS = 3

# Потолок здравого смысла: рекорд по разведке — 53 сделки у постоянного B2B-клиента.
# Больше сотни означает, что амо отдала чужие сделки, а не сделки клиента.
MAX_LEADS_PER_CLIENT = 150

VERDICT_AUTO = "auto"           # решено автоматически и совпало с фактом
VERDICT_PENDING = "pending"     # робот сработал бы, заказ ещё не проведён руками
VERDICT_QUESTION = "question"   # честный вопрос владельцу
VERDICT_WRONG = "wrong"         # уверенно, но мимо — этого быть не должно
VERDICT_NO_PHONE = "no_phone"   # телефон заказа не распознан


@dataclass
class ExamRow:
    order: Order
    decision: Decision
    fact_lead_id: Optional[int]
    verdict: str
    note: str = ""
    candidates: list[LeadInfo] = field(default_factory=list)
    # Решение принято по запасному признаку (дате закрытия сделки), а не по
    # «Дате и времени заказа». Такие случаи и матчер, и проверка считают одинаково,
    # то есть проверка их не подтверждает — нужен взгляд человека.
    by_closed_date: bool = False

    @property
    def phone_masked(self) -> str:
        return "…" + (self.order.phone10 or "0000")[-4:]


ORDERS_SQL = """
SELECT o.id, o.phone, o.phone_digits, o.customer_name, o.created_at,
       o.amount_total, o.rating_score,
       c.full_name AS client_full_name,
       COALESCE(NULLIF(TRIM(c.address), ''), NULLIF(TRIM(c.last_order_addr), '')) AS address
FROM public.orders o
LEFT JOIN public.clients c ON c.id = o.client_id
WHERE o.created_at >= now() - ($1::int || ' days')::interval
ORDER BY o.created_at, o.id
"""


async def load_orders(dsn: str, days: int) -> list[Order]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(ORDERS_SQL, days)
    finally:
        await conn.close()
    return [
        Order(
            order_id=row["id"],
            phone10=last10(row["phone_digits"]) or last10(row["phone"]),
            created_at=row["created_at"].astimezone(MOSCOW_TZ),
            amount_total=row["amount_total"] or 0,
            master_names=[],
            rating_score=row["rating_score"],
            client_name=row["client_full_name"] or row["customer_name"],
            address=row["address"],
        )
        for row in rows
    ]


def _closed_date(lead: dict) -> Optional[date]:
    raw = lead.get("closed_at")
    if not raw:
        return None
    return datetime.fromtimestamp(int(raw), tz=timezone.utc).astimezone(MOSCOW_TZ).date()


def to_lead_info(lead: dict) -> LeadInfo:
    return LeadInfo(
        lead_id=int(lead["id"]),
        pipeline_id=int(lead.get("pipeline_id") or 0),
        status_id=int(lead.get("status_id") or 0),
        order_date=order_date_msk(lead),
        closed_date=_closed_date(lead),
        name=lead.get("name"),
    )


def find_fact_lead(order_date: date, leads: list[dict]) -> tuple[Optional[int], str]:
    """Какая сделка РЕАЛЬНО проведена по этому заказу (факт для сверки).

    Сначала по полю «Дата и время заказа» (±2 дня), затем по моменту закрытия
    сделки (±3 дня) — для сделок, где поле даты не заполняли.
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
        return min(dated)[1], "по полю «Дата и время заказа»"

    by_closed = [
        (abs((_closed_date(lead) - order_date).days), int(lead["id"]))
        for lead in completed if _closed_date(lead) is not None
        and abs((_closed_date(lead) - order_date).days) <= CLOSED_WINDOW_DAYS
    ]
    if by_closed:
        return min(by_closed)[1], "по дате закрытия сделки"

    return None, ""


def classify(order: Order, decision: Decision, fact_lead_id: Optional[int],
             candidates: list[LeadInfo]) -> tuple[str, str]:
    """Сравнить решение матчера с фактом."""
    if decision.kind == "ask_owner":
        return VERDICT_QUESTION, f"кандидаты: {', '.join('#' + str(x) for x in decision.options)}"

    if decision.kind in ("use_realization", "already_done"):
        target = decision.lead_id
        if fact_lead_id is None:
            open_target = any(c.lead_id == target and c.is_open for c in candidates)
            if open_target:
                return VERDICT_PENDING, f"взял бы открытую сделку #{target}, руками ещё не проведено"
            return VERDICT_WRONG, f"взял #{target}, но проведённой сделки по заказу не видно"
        if target == fact_lead_id:
            return VERDICT_AUTO, f"сделка #{target}"
        return VERDICT_WRONG, f"взял #{target}, а проведена #{fact_lead_id}"

    if decision.kind == "use_primary":
        if fact_lead_id is None:
            return VERDICT_AUTO, f"через первичную сделку #{decision.lead_id} (путь Б)"
        return VERDICT_WRONG, f"пошёл бы через первичную #{decision.lead_id}, а проведена #{fact_lead_id}"

    # create_new
    if fact_lead_id is None:
        return VERDICT_AUTO, "сделки нет — создал бы новую (путь В)"
    return VERDICT_WRONG, f"создал бы дубль: сделка #{fact_lead_id} уже проведена"


def _used_closed_date(order_date: date, decision: Decision, candidates: list[LeadInfo]) -> bool:
    """Решение опирается на дату закрытия сделки, а не на «Дату и время заказа»?"""
    if decision.kind != "already_done":
        return False
    chosen = next((lead for lead in candidates if lead.lead_id == decision.lead_id), None)
    if chosen is None or chosen.order_date is None:
        return True
    return abs((chosen.order_date - order_date).days) > DATE_WINDOW_DAYS


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


async def run_exam(orders: list[Order], client: AmoClient, pause: float) -> list[ExamRow]:
    cache: dict[str, list[dict]] = {}
    rows: list[ExamRow] = []

    for index, order in enumerate(orders, 1):
        if not order.phone10:
            rows.append(ExamRow(order, Decision(kind="create_new"), None, VERDICT_NO_PHONE,
                                "телефон заказа не распознан"))
            continue

        raw_leads = await collect_candidates(client, order.phone10, cache)
        candidates = [to_lead_info(lead) for lead in raw_leads]
        decision = match(order_date=order.order_date, candidates=candidates)
        fact_lead_id, fact_source = find_fact_lead(order.order_date, raw_leads)
        verdict, note = classify(order, decision, fact_lead_id, candidates)
        if fact_source and verdict == VERDICT_AUTO:
            note = f"{note} ({fact_source})"
        rows.append(ExamRow(order, decision, fact_lead_id, verdict, note, candidates,
                            by_closed_date=_used_closed_date(order.order_date, decision, candidates)))

        if index % 25 == 0:
            print(f"  обработано {index}/{len(orders)}…", flush=True)
        await asyncio.sleep(pause)

    return rows


def build_report(rows: list[ExamRow], days: int) -> str:
    counts = Counter(row.verdict for row in rows)
    total = len(rows) or 1
    auto = counts[VERDICT_AUTO] + counts[VERDICT_PENDING]

    def pct(n: int) -> str:
        return f"{100 * n / total:.1f}%"

    lines = [
        "# Экзамен на истории — результат",
        "",
        f"Дата прогона: {datetime.now(MOSCOW_TZ).date().isoformat()}. "
        f"Период: {days} дней. Заказов: {len(rows)}. Режим: только чтение.",
        "",
        "## Итог",
        "",
        "| Исход | Кол-во | Доля |",
        "|---|---|---|",
        f"| Решено автоматически и совпало с фактом | {counts[VERDICT_AUTO]} | {pct(counts[VERDICT_AUTO])} |",
        f"| Робот сработал бы, заказ ещё не проведён руками | {counts[VERDICT_PENDING]} | {pct(counts[VERDICT_PENDING])} |",
        f"| Вопрос владельцу | {counts[VERDICT_QUESTION]} | {pct(counts[VERDICT_QUESTION])} |",
        f"| **Уверенно, но неверно** | **{counts[VERDICT_WRONG]}** | {pct(counts[VERDICT_WRONG])} |",
        f"| Телефон не распознан | {counts[VERDICT_NO_PHONE]} | {pct(counts[VERDICT_NO_PHONE])} |",
        "",
        f"**Автоматически проводимо: {pct(auto)}** (порог дизайна ≥92%). "
        f"Вопросов: {pct(counts[VERDICT_QUESTION])} (порог ≤8%). "
        f"Уверенно-неверных: {counts[VERDICT_WRONG]} (порог 0).",
        "",
    ]

    problems = [row for row in rows if row.verdict in (VERDICT_WRONG, VERDICT_NO_PHONE)]
    if problems:
        lines += ["## Ошибки (разбирать в первую очередь)", ""]
        for row in problems:
            lines.append(
                f"- Заказ №{row.order.order_id} · {row.phone_masked} · "
                f"{row.order.order_date.isoformat()} — {row.note}"
            )
        lines.append("")

    fallback = [row for row in rows if row.by_closed_date]
    if fallback:
        lines += [
            "## Решено по дате закрытия сделки — проверить глазами",
            "",
            "В этих заказах поле «Дата и время заказа» не совпало с датой заказа, "
            "и робот опознал сделку по моменту её закрытия. Проверка «правды» устроена "
            "так же, поэтому подтвердить эти случаи может только человек.",
            "",
        ]
        for row in fallback:
            lines.append(
                f"- Заказ №{row.order.order_id} · {row.phone_masked} · "
                f"{row.order.order_date.isoformat()} → сделка #{row.decision.lead_id}"
            )
        lines.append("")

    questions = [row for row in rows if row.verdict == VERDICT_QUESTION]
    if questions:
        lines += ["## Вопросы владельцу", ""]
        for row in questions:
            lines.append(
                f"- Заказ №{row.order.order_id} · {row.phone_masked} · "
                f"{row.order.order_date.isoformat()} — {row.note}"
            )
        lines.append("")

    lines += [
        "## Как считалось",
        "",
        "«Факт» — сделка воронки реализации на успешном этапе, найденная сначала по полю "
        "«Дата и время заказа» (±2 дня), затем по дате закрытия сделки (±3 дня). "
        "Телефоны в отчёте маскированы до последних 4 цифр.",
        "",
    ]
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Экзамен матчера на истории (только чтение)")
    parser.add_argument("--days", type=int, default=90)
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

    print(f"Беру заказы бота за {args.days} дней…", flush=True)
    orders = await load_orders(dsn, args.days)
    if args.limit:
        orders = orders[: args.limit]
    print(f"Заказов: {len(orders)}. Иду в amoCRM (только чтение)…", flush=True)

    client = AmoClient(base_url=base_url, token=token)
    try:
        rows = await run_exam(orders, client, args.pause)
    finally:
        await client.close()

    report = build_report(rows, args.days)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")

    print()
    print(report.split("## Ошибки")[0].split("## Вопросы")[0])
    print(f"Отчёт сохранён: {args.out}")

    wrong = sum(1 for row in rows if row.verdict == VERDICT_WRONG)
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
