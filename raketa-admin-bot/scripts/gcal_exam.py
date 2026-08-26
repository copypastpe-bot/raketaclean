"""Экзамен разбора календаря на боевых записях.

Зачем: тесты проверяют то, что я предвидел, а календарь владельца заполняется
от руки полгода подряд. Экзамен прогоняет разбор по всем 462 настоящим записям
и показывает, где он ошибается, — до того как робот заведёт лишнюю сделку.

Запуск (файлы разведки содержат ПД и в git не попадают):

    GCAL_FIXTURES_DIR=~/Projects/tgbot-v1/recon/data \\
        .venv/bin/python -m scripts.gcal_exam

Гейт перед следующей фазой (docs/plans/2026-08-26-calendar-plan.md):
  - ноль блокировок, теплоходов и перемывов, принятых за заказ;
  - телефон извлечён у >= 98% заказов;
  - услуга распознана у >= 95% заказов;
  - район распознан у >= 90% заказов с известной приставкой.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

from adminbot.gcal.event import EventKind, ParsedEvent, parse_event

FILES = ("calendar_p1.json", "calendar_p2.json")

# Записи, которые заказом заведомо не являются: если разбор назвал такую заказом,
# робот завёл бы лишнюю сделку. Это единственная ошибка, которая стоит денег.
NOT_ORDER_MARKS = ("⛔", "перемыв", "⁉")
KNOWN_BOATS = ("толстой", "пушкин", "русь", "чернышевский", "кучкин")


def load_events(directory: Path) -> list[dict]:
    events: list[dict] = []
    for name in FILES:
        path = directory / name
        if not path.exists():
            raise SystemExit(f"Нет файла разведки: {path}")
        events.extend(json.loads(path.read_text()).get("events") or [])
    return events


def is_not_order(raw: dict) -> bool:
    summary = (raw.get("summary") or "").lower()
    return (any(mark in summary for mark in NOT_ORDER_MARKS)
            or any(boat in summary for boat in KNOWN_BOATS))


def main() -> int:
    directory = Path(os.path.expanduser(os.environ.get("GCAL_FIXTURES_DIR", ""))).resolve()
    events = load_events(directory)
    parsed: list[tuple[dict, ParsedEvent]] = [(raw, parse_event(raw)) for raw in events]

    kinds = Counter(p.kind.value for _, p in parsed)
    orders = [(raw, p) for raw, p in parsed if p.kind is EventKind.ORDER]

    wrongly_taken = [raw.get("summary") for raw, p in orders if is_not_order(raw)]
    with_phone = [p for _, p in orders if p.phone10]
    with_service = [p for _, p in orders if p.services]
    with_prefix = [p for _, p in orders if p.district or p.unknown_district]
    with_district = [p for p in with_prefix if p.district]
    with_comment = [p for _, p in orders if p.comment.strip()]

    print(f"Записей всего: {len(parsed)}")
    for kind, count in kinds.most_common():
        print(f"  {kind:10s} {count}")

    print(f"\nЗаказов: {len(orders)}")
    share = lambda part: f"{len(part)} ({len(part) / max(len(orders), 1):.1%})"  # noqa: E731
    print(f"  телефон найден:   {share(with_phone)}")
    print(f"  услуга распознана:{share(with_service)}")
    print(f"  комментарий есть: {share(with_comment)}")
    print(f"  приставка района: {share(with_prefix)}, "
          f"из них распознано {len(with_district)}")

    unknown = Counter(p.unknown_district for _, p in parsed if p.unknown_district)
    if unknown:
        print("\nНепонятные приставки (робот оставит район пустым и скажет владельцу):")
        for prefix, count in unknown.most_common():
            print(f"  {prefix} — {count}")

    no_service = [p.summary for _, p in orders if not p.services]
    if no_service:
        print(f"\nЗаказы без распознанной услуги ({len(no_service)}):")
        for summary in no_service[:20]:
            print(f"  {summary}")

    skipped = [(p.summary, p.skip_reason) for _, p in parsed if p.kind is EventKind.SKIP]
    if skipped:
        print(f"\nПропущено молча ({len(skipped)}):")
        for summary, reason in skipped[:20]:
            print(f"  {summary} — {reason}")

    print("\n--- Гейт ---")
    checks = [
        ("не-заказы, принятые за заказ", len(wrongly_taken), 0, "=="),
        ("телефон, %", 100 * len(with_phone) / max(len(orders), 1), 98.0, ">="),
        ("услуга, %", 100 * len(with_service) / max(len(orders), 1), 95.0, ">="),
        ("район, %", 100 * len(with_district) / max(len(with_prefix), 1), 90.0, ">="),
    ]
    failed = False
    for name, value, threshold, op in checks:
        ok = value == threshold if op == "==" else value >= threshold
        failed = failed or not ok
        shown = f"{value:.1f}" if isinstance(value, float) else str(value)
        print(f"  {'OK ' if ok else 'FAIL'} {name}: {shown} (нужно {op} {threshold})")

    if wrongly_taken:
        print("\nПринятые за заказ ошибочно:")
        for summary in wrongly_taken:
            print(f"  {summary}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
