"""Ночная тишина: с 20:00 до 10:00 по Москве робот клиентам по своей инициативе не пишет.

Решение владельца 13.10 (ТЗ docs/plans/2026-10-13-quiet-hours.md). Здесь только
расчёт «ночь ли сейчас и до какого часа ждать»: без базы и без часов, момент
передаёт тот, кто спрашивает. Часы — константы, настройки нет.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
QUIET_FROM = time(20, 0)   # 20:00 — уже ночь
QUIET_UNTIL = time(10, 0)  # 10:00 — уже можно


def quiet_until(moment: datetime) -> datetime | None:
    """`None` — можно слать; иначе ближайшие 10:00 по Москве, до которых ждать.

    Границы: 20:00 — уже ночь, 10:00 — уже можно. После 20:00 ждать до 10:00
    следующего дня, после полуночи — до 10:00 того же утра.

    Момент — с часовым поясом, любым: наивное время неизвестно в каком поясе,
    а ошибка в нём — это письмо клиенту среди ночи.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("quiet_until: нужен момент с часовым поясом")
    local = moment.astimezone(MSK)
    if QUIET_UNTIL <= local.time() < QUIET_FROM:
        return None
    day = local.date()
    if local.time() >= QUIET_FROM:
        day += timedelta(days=1)
    return datetime.combine(day, QUIET_UNTIL, tzinfo=MSK)


__all__ = ["MSK", "QUIET_FROM", "QUIET_UNTIL", "quiet_until"]
