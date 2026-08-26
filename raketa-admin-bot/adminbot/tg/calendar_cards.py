"""Карточки по календарю: отмена заказа, теплоход, спорная сделка.

Чистые функции: на входе запись, на выходе текст и кнопки. Ни сети, ни базы —
поэтому каждую формулировку можно проверить тестом.

Почему в кнопке нет идентификатора записи: у записи Google он длинный (26+
символов), а в кнопку Telegram влезает 64 байта на всё. Поэтому кнопка несёт
только выбор, а сама запись находится по сообщению, на котором нажали, —
её номер сообщения робот запомнил, когда карточку отправлял.

Телефон и дата показываются целиком: бот личный, других получателей нет,
а владельцу нужно позвонить клиенту, не открывая CRM (решение 2026-08-26).
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from adminbot.phone import for_owner

CHOICE_PREFIX = "gcal"

# Вид работ → слово для владельца.
SERVICE_WORDS = {
    "mattress": "матрас", "furniture": "мебель", "carpeting": "ковролин",
    "rug_home": "ковёр", "cleaning": "уборка", "windows": "окна",
}

REASON_TEXTS = {
    "ask_owner": "Нашёл несколько подходящих сделок — какая из них про этот заказ?",
    "ask_owner_stale": "Свежих сделок нет, есть только старые. Завести новую?",
    "сейлзбот не создал автосделку": "Лид передан в работу, но автосделку так и не увидел.",
}
DEFAULT_REASON = "Не смог решить сам, как поступить с этой записью календаря."


def parse_calendar_choice(data: Optional[str]) -> Optional[str]:
    """Что выбрал владелец. Чужие и мусорные кнопки → None."""
    parts = (data or "").split(":")
    if len(parts) != 2 or parts[0] != CHOICE_PREFIX or not parts[1]:
        return None
    return parts[1]


def _choice(value: str) -> str:
    return f"{CHOICE_PREFIX}:{value}"


def cancellation_card(link: Any) -> tuple[str, InlineKeyboardMarkup]:
    """Запись удалена — значит заказ отменён (решение владельца 2).

    Робот сам сделку не закрывает: удаление бывает переносом и случайностью,
    а закрытая сделка портит статистику.
    """
    lead_id = link.real_lead_id or link.primary_lead_id
    text = "\n".join([
        "❌ Заказ отменён — запись удалена из календаря",
        _client_line(link),
        f"Сделка #{lead_id} — закрыть её как несостоявшуюся?",
    ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 Закрыть сделку", callback_data=_choice("close")),
        InlineKeyboardButton(text="✋ Оставить как есть", callback_data=_choice("keep")),
    ]])
    return text, keyboard


def boat_card(link: Any) -> tuple[str, InlineKeyboardMarkup]:
    """Теплоход: ни телефона, ни цены — заводим только по кнопке (решение 7)."""
    question = link.question or {}
    name = question.get("boat") or link.client_name or "теплоход"
    when = question.get("when") or (
        f"{link.order_date:%d.%m}" if link.order_date else "дата не указана")

    text = "\n".join([
        f"🚢 Теплоход «{name}», {when}",
        "Телефона и суммы в записи нет — заведу сделку на юрлицо, остальное за вами.",
    ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Завести сделку", callback_data=_choice("boat_create")),
        InlineKeyboardButton(text="🚫 Пропустить", callback_data=_choice("boat_skip")),
    ]])
    return text, keyboard


def calendar_question_card(link: Any) -> tuple[str, InlineKeyboardMarkup]:
    """Робот не смог выбрать сделку сам."""
    question = link.question or {}
    reason = question.get("reason", "")
    options = question.get("options") or []

    text = "\n".join([
        "📅 Запись календаря",
        _client_line(link),
        "",
        REASON_TEXTS.get(reason, DEFAULT_REASON),
    ])

    rows = [[InlineKeyboardButton(text=_option_label(option),
                                  callback_data=_choice(f"lead_{option['lead_id']}"))]
            for option in options]
    if reason == "сейлзбот не создал автосделку":
        rows.append([InlineKeyboardButton(text="🔄 Проверить ещё раз",
                                          callback_data=_choice("retry"))])
    rows.append([
        InlineKeyboardButton(text="➕ Создать новую", callback_data=_choice("new")),
        InlineKeyboardButton(text="✋ Сам разберусь", callback_data=_choice("manual")),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _option_label(option: dict) -> str:
    """Подпись кнопки: номер сделки и её дата, если она известна."""
    label = f"Сделка #{option['lead_id']}"
    when = option.get("date")
    return f"{label} · {when[8:10]}.{when[5:7]}" if when else label


def _client_line(link: Any) -> str:
    """Кто и когда — так, чтобы владельцу хватило без похода в CRM."""
    parts = []
    if link.client_name:
        parts.append(link.client_name)
    if link.phone10:
        parts.append(for_owner(link.phone10))
    if link.order_date:
        parts.append(f"{link.order_date:%d.%m.%Y}")
    services = ", ".join(SERVICE_WORDS.get(kind, kind) for kind in (link.services or ()))
    if services:
        parts.append(services)
    if link.district:
        parts.append(link.district.capitalize())
    return " · ".join(parts)
