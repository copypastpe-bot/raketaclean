"""Сторож проведения заказов: время напоминаний, окно, текст, названия услуг.

ТЗ docs/plans/2026-10-13-order-close-reminder.md, задача 1.

Запись-заказ в календаре есть, а заказа в боте нет — в рабочие чаты уходит
напоминание «Заказ не проведён в боте» с кнопкой «Напомнить завтра». Первое —
на следующий день после даты записи с 10:00 МСК, повтор — каждые 30 минут до
20:00, ночью молчит.

Здесь только чистые функции: без Telegram и без базы. Выбор кандидатов и
проверку «проведён» делает `bot.py` (`_order_close_candidates`), состояние —
таблица `order_close_reminders` (миграция 0018). Окно, интервал, глубина
и тексты — константами ниже, одно место.

Текст — простой, без разметки: отправлять без `parse_mode` (у бота его по
умолчанию нет), тогда `<`, `>`, `&` в именах и адресах экранировать не нужно.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

WINDOW_START = time(10, 0)
WINDOW_END = time(20, 0)
REMINDER_INTERVAL = timedelta(minutes=30)
# Глубина выборки записей при запуске и в каждом проходе: от «сегодня − 14» до «вчера».
LOOKBACK_DAYS = 14

SERVICE_NAMES = {
    "furniture": "мебель",
    "mattress": "матрас",
    "cleaning": "клининг",
    "carpeting": "ковры",
    "rug_home": "ковёр на дому",
    "windows": "окна",
}

TITLE = "⚠️ Заказ не проведён в боте"
CALL_TO_ACTION = "Проведите заказ в боте."
SNOOZE_BUTTON_TEXT = "Напомнить завтра"
DONE_MARK = "✅ Заказ проведён"
SNOOZED_MARK = "Отложено до завтра"


def in_window(moment: datetime) -> bool:
    """В окне ли напоминаний: [10:00, 20:00) по Москве. `moment` — с часовым поясом."""
    local_time = _to_moscow(moment).time()
    return WINDOW_START <= local_time < WINDOW_END


def first_send_at(order_date: date, now: datetime) -> datetime:
    """Время первого напоминания по записи с датой `order_date` (московская дата).

    Раньше `order_date + 1 день, 10:00 МСК` — нельзя; позже — сразу (`now`),
    но в окне: вне окна — ближайшие 10:00. `now` — с часовым поясом.
    Ответ — в московском времени.
    """
    earliest = datetime.combine(order_date + timedelta(days=1), WINDOW_START, tzinfo=MOSCOW_TZ)
    local_now = _to_moscow(now)
    return _align_to_window(max(earliest, local_now))


def next_reminder_at(sent_at: datetime) -> datetime:
    """Время повтора: `sent_at` + 30 минут; вне окна — ближайшие 10:00 МСК.

    `sent_at` — время последней отправки, с часовым поясом. Ответ — в московском времени.
    """
    return _align_to_window(_to_moscow(sent_at) + REMINDER_INTERVAL)


def snooze_until(now: datetime) -> datetime:
    """«Напомнить завтра»: 10:00 МСК следующего календарного дня после `now`.

    Считается по московской дате: нажали в 01:30 МСК 6-го — тишина до 10:00 7-го.
    `now` — с часовым поясом. Ответ — в московском времени.
    """
    local_now = _to_moscow(now)
    return datetime.combine(local_now.date() + timedelta(days=1), WINDOW_START, tzinfo=MOSCOW_TZ)


def service_names(codes: Iterable[str] | None) -> str:
    """Услуги записи словами через запятую: порядок — как в записи, первая с большой буквы.

    Незнакомый код — как есть; пустые коды пропускаются; нет услуг — пустая строка.
    """
    names = []
    for code in codes or ():
        key = str(code or "").strip()
        if key:
            names.append(SERVICE_NAMES.get(key, key))
    if not names:
        return ""
    names[0] = names[0][:1].upper() + names[0][1:]
    return ", ".join(names)


def render_text(
    *,
    order_date: date,
    client_name: str | None,
    address: str | None,
    services: Iterable[str] | None,
) -> str:
    """Текст напоминания по решению владельца 4 (без телефона клиента и без фразы про календарь).

    Вторая строка — «дата · имя · адрес», пустые имя и адрес пропускаются;
    третья — услуги (нет услуг — строки нет). Кнопка «Напомнить завтра» —
    отдельно (`SNOOZE_BUTTON_TEXT`).
    """
    lines = [TITLE, *_order_lines(order_date, client_name, address, services)]
    lines.extend(("", CALL_TO_ACTION))
    return "\n".join(lines)

def render_done_text(
    *,
    order_date: date,
    client_name: str | None,
    address: str | None,
    services: Iterable[str] | None,
) -> str:
    """Вид последнего напоминания, когда заказ провели (задача 2): без кнопки.

    Первая строка — «✅ Заказ проведён» вместо заголовка, строки заказа — как
    в напоминании, призыва нет: по сообщению в чате видно, какой заказ закрылся.
    """
    return "\n".join([DONE_MARK, *_order_lines(order_date, client_name, address, services)])

def render_snoozed_text(
    *,
    order_date: date,
    client_name: str | None,
    address: str | None,
    services: Iterable[str] | None,
    snoozed_by: str | None,
) -> str:
    """Вид напоминания после «Напомнить завтра» (задача 2): без кнопки.

    Как напоминание, только призыв заменён на «Отложено до завтра — <кто нажал>»;
    имени нет — просто «Отложено до завтра».
    """
    who = (snoozed_by or "").strip()
    mark = f"{SNOOZED_MARK} — {who}" if who else SNOOZED_MARK
    lines = [TITLE, *_order_lines(order_date, client_name, address, services)]
    lines.extend(("", mark))
    return "\n".join(lines)

def _order_lines(
    order_date: date,
    client_name: str | None,
    address: str | None,
    services: Iterable[str] | None,
) -> list[str]:
    """Строки заказа: «дата · имя · адрес» (пустые пропускаются) и услуги, если есть."""
    details = [order_date.strftime("%d.%m")]
    for value in (client_name, address):
        text = (value or "").strip()
        if text:
            details.append(text)
    lines = [" · ".join(details)]
    services_line = service_names(services)
    if services_line:
        lines.append(services_line)
    return lines


def _align_to_window(moment: datetime) -> datetime:
    local = _to_moscow(moment)
    if local.time() < WINDOW_START:
        return datetime.combine(local.date(), WINDOW_START, tzinfo=MOSCOW_TZ)
    if local.time() >= WINDOW_END:
        return datetime.combine(local.date() + timedelta(days=1), WINDOW_START, tzinfo=MOSCOW_TZ)
    return local


def _to_moscow(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("datetime without timezone")
    return moment.astimezone(MOSCOW_TZ)


__all__ = [
    "CALL_TO_ACTION",
    "DONE_MARK",
    "LOOKBACK_DAYS",
    "MOSCOW_TZ",
    "REMINDER_INTERVAL",
    "SERVICE_NAMES",
    "SNOOZED_MARK",
    "SNOOZE_BUTTON_TEXT",
    "TITLE",
    "WINDOW_END",
    "WINDOW_START",
    "first_send_at",
    "in_window",
    "next_reminder_at",
    "render_done_text",
    "render_snoozed_text",
    "render_text",
    "service_names",
    "snooze_until",
]
