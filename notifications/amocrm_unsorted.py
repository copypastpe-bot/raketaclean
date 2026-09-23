"""Карточки «Неразобранного» amoCRM для админов: вид, сборка, текст, окно отправки.

ТЗ docs/plans/2026-09-23-amo-unsorted-cards.md, задача 1.

Новая запись в «Неразобранном» воронки 1 превращается в карточку: пропущенный звонок
(категория `sip`) или новое сообщение (категория `chats`). Карточка уходит обоим
админам и повторяется раз в час, пока кто-то не нажмёт кнопку.

Здесь только чистые функции: без Telegram и без базы. Отправку, дела в таблице
`amocrm_unsorted_cards` и приём нажатия делают опрос и рассылка в `bot.py`.
Окно, интервал напоминаний и тексты — константами ниже, одно место.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from notifications.amocrm_api import (
    _extract_phone,
    _first_text,
    _nested_text,
    build_lead_link,
    extract_contact_phone,
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Окно отправки по московскому времени: начало входит, конец — нет (20:00 уже вне окна).
WINDOW_START = time(9, 0)
WINDOW_END = time(20, 0)
# Напоминание — через час после прошлой отправки; вне окна — в ближайшие 9:00.
REMINDER_INTERVAL = timedelta(hours=1)

# Вид карточки; те же значения лежат в колонке `amocrm_unsorted_cards.kind`.
KIND_CALL = "call"
KIND_MESSAGE = "message"

_CATEGORY_KIND = {"sip": KIND_CALL, "chats": KIND_MESSAGE}
_EVENT_TIME_FIELD = {KIND_CALL: "called_at", KIND_MESSAGE: "received_at"}
_TITLE = {KIND_CALL: "Пропущенный звонок", KIND_MESSAGE: "Новое сообщение"}
_TIME_LABEL = {KIND_CALL: "Время звонка", KIND_MESSAGE: "Время сообщения"}
_DONE_BUTTON = {KIND_CALL: "Я перезвонил", KIND_MESSAGE: "Ответил"}
_CLOSED_MARK = {KIND_CALL: "✅ Перезвонил", KIND_MESSAGE: "✅ Ответил"}

# Текст кнопки-ссылки на сделку (одинаковый у обоих видов).
OPEN_LEAD_BUTTON_TEXT = "Перейти в сделку"


@dataclass(slots=True, frozen=True)
class UnsortedCard:
    """Карточка по одной записи «Неразобранного».

    Поля (кроме `link`) совпадают с колонками `amocrm_unsorted_cards`, поэтому
    карточку можно собрать и из строки таблицы: ссылка тогда —
    `build_lead_link(api_base, lead_id)` из `notifications.amocrm_api`.
    """

    uid: str
    kind: str  # KIND_CALL или KIND_MESSAGE
    lead_id: int | None
    contact_name: str | None
    phone: str | None
    event_at: datetime  # время звонка или сообщения, с часовым поясом
    link: str | None  # ссылка на сделку; нет сделки — None


def unsorted_kind(item: Mapping[str, Any]) -> str | None:
    """Вид записи «Неразобранного»: `sip` → KIND_CALL, `chats` → KIND_MESSAGE, иное → None."""
    category = str(item.get("category") or "").strip().casefold()
    return _CATEGORY_KIND.get(category)


def build_unsorted_card(
    item: Mapping[str, Any],
    *,
    api_base: str,
    contact: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> UnsortedCard:
    """Собрать карточку из записи «Неразобранного» и контакта amoCRM.

    `contact` — ответ `AmoCRMAPIClient.fetch_contact` по первому контакту записи
    (или None, если контакта нет или запрос не удался). Имя и телефон берутся так же,
    как в прежнем `build_unsorted_alert`. Время события — `metadata.called_at`
    (звонок) или `metadata.received_at` (сообщение); нет поля — `created_at` записи;
    нет и его — `now` (по умолчанию текущее время). Вид записи должен быть известен:
    для записи, у которой `unsorted_kind` вернул None, — ValueError.
    """
    kind = unsorted_kind(item)
    if kind is None:
        raise ValueError(f"unsupported unsorted category: {item.get('category')!r}")
    embedded = item.get("_embedded") if isinstance(item.get("_embedded"), Mapping) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    lead_id = _first_embedded_id(embedded, "leads")
    sender = str(metadata.get("from") or "")
    contact_name = _first_text(
        _nested_text(metadata, "client", "name"),
        str(contact.get("name") or "").strip() if contact else None,
        sender.strip() if not _extract_phone(sender) else None,
        str(metadata.get("name") or "").strip(),
    )
    phone = _normalize_ru_phone(
        _first_text(
            extract_contact_phone(contact),
            _extract_phone(str(metadata.get("phone") or "")),
            _extract_phone(sender),
        )
    )
    event_ts = _positive_int(metadata.get(_EVENT_TIME_FIELD[kind])) or _positive_int(
        item.get("created_at")
    )
    if event_ts:
        event_at = datetime.fromtimestamp(event_ts, tz=MOSCOW_TZ)
    else:
        event_at = _to_moscow(now) if now is not None else datetime.now(MOSCOW_TZ)
    return UnsortedCard(
        uid=str(item.get("uid") or item.get("id") or "").strip(),
        kind=kind,
        lead_id=lead_id,
        contact_name=contact_name,
        phone=phone,
        event_at=event_at,
        link=build_lead_link(api_base, lead_id),
    )


def render_card_text(card: UnsortedCard, *, closed: bool = False) -> str:
    """Текст карточки строго по решению владельца 2 (время — московское).

    Пустое значение оставляет строку («Номер:» без номера). `closed=True` — закрытый
    вид после нажатия: тот же текст плюс внизу «✅ Перезвонил» или «✅ Ответил».
    """
    event_local = _to_moscow(card.event_at)
    lines = [
        _TITLE[card.kind],
        _line("Клиент", card.contact_name),
        _line("Номер", card.phone),
        _line(_TIME_LABEL[card.kind], event_local.strftime("%d.%m %H:%M")),
        _line("Ссылка", card.link),
    ]
    if closed:
        lines.append(_CLOSED_MARK[card.kind])
    return "\n".join(lines)


def done_button_text(kind: str) -> str:
    """Текст кнопки «разобрано»: «Я перезвонил» у звонка, «Ответил» у сообщения."""
    return _DONE_BUTTON[kind]


def first_send_at(now: datetime) -> datetime:
    """Время первой отправки карточки: в окне 9:00–20:00 МСК — `now`, вне окна — ближайшие 9:00.

    `now` — с часовым поясом (без пояса — ValueError). Ответ — в московском времени.
    """
    return _align_to_window(now)


def next_reminder_at(sent_at: datetime) -> datetime:
    """Время следующего напоминания: `sent_at` + час; попало на 20:00 или позже — ближайшие 9:00 МСК.

    `sent_at` — время последней отправки, с часовым поясом. Ответ — в московском времени.
    """
    return _align_to_window(_to_moscow(sent_at) + REMINDER_INTERVAL)


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


def _normalize_ru_phone(raw: str | None) -> str | None:
    """Российский номер — к виду `+7XXXXXXXXXX`, остальное (иностранный, мусор) — как пришло.

    Российский = после отбрасывания всего, кроме цифр: 11 цифр с первой 7 или 8,
    либо 10 цифр с первой 9. Пусто на входе — пусто на выходе.
    """
    if not raw:
        return raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return f"+7{digits[1:]}"
    if len(digits) == 10 and digits[0] == "9":
        return f"+7{digits}"
    return raw


def _line(label: str, value: str | None) -> str:
    return f"{label}: {value}" if value else f"{label}:"


def _first_embedded_id(embedded: Mapping[str, Any], key: str) -> int | None:
    entries = embedded.get(key) or []
    if entries and isinstance(entries[0], Mapping):
        return _positive_int(entries[0].get("id"))
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value) if isinstance(value, (int, float)) else int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


__all__ = [
    "KIND_CALL",
    "KIND_MESSAGE",
    "MOSCOW_TZ",
    "OPEN_LEAD_BUTTON_TEXT",
    "REMINDER_INTERVAL",
    "UnsortedCard",
    "WINDOW_END",
    "WINDOW_START",
    "build_unsorted_card",
    "done_button_text",
    "first_send_at",
    "next_reminder_at",
    "render_card_text",
    "unsorted_kind",
]
