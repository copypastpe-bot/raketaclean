"""Текст команды «что сейчас сломано» (ТЗ 2026-09-18, задача 9) — только для
My_admin, только состояние ТЕХНИКИ: живы ли боты, идёт ли опрос CRM, очередь
рассылки, долги почты, открытые инциденты (в порядке, названном в ТЗ).

Версия для My_assistant (состояние ДЕЛ: висяки, сделки без адреса) в эту
задачу не входит (уточнение координатора 6 ТЗ) — в схеме шины таких данных
пока нет, второе ТЗ.

My_admin — сам технический канал (решение владельца 18.09: «техника —
My_admin»), поэтому текст может быть чуть более техническим, чем письма
владельцу в остальных местах проекта — упрощать до делового языка здесь не
нужно, в отличие от эскалации в My_assistant (`notifyd.incidents`).

Чистая функция — без похода в Telegram и без похода в базу: числа и списки
ей приносит `notifyd.admin_bot.AdminBotListener.cmd_status`, а здесь только
форматирование, поэтому тестируется без БД и без aiogram.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from notifyd.watchdog import (
    CheckResult, KEY_ADMIN_HEARTBEAT, KEY_AMOCRM_POLL, KEY_CLIENT_HEARTBEAT,
    KEY_DATABASE, KEY_PROXY, KEY_WORKER_HEARTBEAT,
)

# Порядок и подписи — как перечислено в ТЗ: «живы ли боты, идёт ли опрос
# CRM, очередь рассылки, долги почты, открытые инциденты».
_HEARTBEAT_KEYS = (KEY_WORKER_HEARTBEAT, KEY_CLIENT_HEARTBEAT, KEY_ADMIN_HEARTBEAT)

_INCIDENT_STATE_RU = {"open": "открыт", "acked": "признан"}


def format_status_text(*, checks: Sequence[CheckResult], dispatch_stats: dict,
                       outbox_pending: int, incidents: Sequence[dict],
                       now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    by_key = {c.key: c for c in checks}

    lines = [f"Состояние техники на {now.astimezone(timezone.utc):%d.%m %H:%M} UTC:", "",
             "Боты:"]
    for key in _HEARTBEAT_KEYS:
        lines.append(_check_line(by_key.get(key), key))

    lines.append("")
    lines.append(_check_line(by_key.get(KEY_AMOCRM_POLL), KEY_AMOCRM_POLL))
    db_result = by_key.get(KEY_DATABASE)
    if db_result is not None:
        lines.append(_check_line(db_result, KEY_DATABASE))
    proxy_result = by_key.get(KEY_PROXY)
    if proxy_result is not None:
        lines.append(_check_line(proxy_result, KEY_PROXY))

    lines.append("")
    lines.append(_dispatch_line(dispatch_stats, now))
    lines.append(f"Долги почты службы: {outbox_pending}" if outbox_pending
                else "Долги почты службы: нет")

    lines.append("")
    lines.append(_incidents_block(incidents, now))
    return "\n".join(lines)


def _check_line(result: Optional[CheckResult], key: str) -> str:
    if result is None:
        return f"• {key} — не проверено"
    if result.ok:
        return f"• {key} — жив" if "пульс" in key else f"• {key} — работает"
    return f"• {key} — сломано: {result.detail}" if result.detail else f"• {key} — сломано"


def _dispatch_line(stats: dict, now: datetime) -> str:
    pending = int(stats.get("pending_due") or 0)
    if pending == 0:
        return "Очередь рассылки клиентам: пусто"
    last_sent_at = stats.get("last_sent_at")
    tail = f", последняя отправка {_ago(now, last_sent_at)}" if last_sent_at else \
        ", отправок ещё не было"
    return f"Очередь рассылки клиентам: {pending} ждут отправки{tail}"


def _incidents_block(incidents: Sequence[dict], now: datetime) -> str:
    if not incidents:
        return "Открытых инцидентов нет."
    lines = [f"Открытые инциденты ({len(incidents)}):"]
    for incident in incidents:
        state = _INCIDENT_STATE_RU.get(incident["state"], incident["state"])
        lines.append(f"• {incident['key']} — {state}, {_ago(now, incident['opened_at'])}")
    return "\n".join(lines)


def _ago(now: datetime, when: Optional[datetime]) -> str:
    if when is None:
        return "никогда"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    sec = int((now - when).total_seconds())
    if sec < 60:
        return f"{sec} сек назад"
    if sec < 3600:
        return f"{sec // 60} мин назад"
    return f"{sec // 3600} ч назад"
