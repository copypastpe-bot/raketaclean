"""Карточки владельцу: вопрос с кнопками, вечерняя сводка, предпросмотр хвоста.

Всё в этом модуле — чистые функции: на входе данные, на выходе текст и кнопки.
Ни сети, ни базы, поэтому каждую формулировку можно проверить тестом.

Два правила формулировок:
- телефон в сообщение попадает только последними четырьмя цифрами (правило
  проекта по персональным данным);
- никаких внутренних слов робота: владелец читает «проведено», «ждут вашего
  ответа», «создам сделку», а не статусы из базы.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from adminbot.amo import ids
from adminbot.phone import mask
from adminbot.sync.backlog import PlannedOrder, money

# Приставка callback-данных карточки-вопроса: amosync:{номер заказа}:{выбор}.
CHOICE_PREFIX = "amosync"
BACKLOG_GO = "backlog:go"
BACKLOG_HOLD = "backlog:hold"

# Почему робот спрашивает — словами владельца.
REASON_TEXTS = {
    "ask_owner": "Нашёл несколько подходящих сделок — какая из них про этот заказ?",
    "ask_owner_stale": "Свежих сделок нет, есть только старые. Завести новую?",
    "ask_owner_unrelated": "Сделка нашлась, но похожа на чужую работу: другой мастер "
                           "и сумма расходится в разы.",
    "телефон заказа не распознан": "В заказе не разобрал номер телефона — "
                                   "искать сделку не по чему.",
    "сейлзбот не создал автосделку": "Лид передан в работу, но автосделку "
                                     "так и не увидел.",
}
DEFAULT_REASON = "Не смог решить сам, как поступить с этим заказом."

# Действия робота человеческим языком. {id} — место для номера сделки в амо.
ACTION_WORDS = {
    "update_lead": "заполню сделку {id}",
    "move_lead": "переведу сделку {id} на нужный этап",
    "create_lead": "создам сделку",
    "create_contact": "создам контакт",
    "update_contact": "поправлю имя контакта",
    "complete_task": "закрою задачу",
    "add_note": "напишу комментарий в сделке {id}",
    "ask_owner": "спрошу вас",
    "salesbot_timeout": "подожду автосделку",
}


# --- карточка-вопрос ---

def question_card(order: Any, question: Optional[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """Вопрос по одному заказу: что за заказ и между чем выбирать."""
    reason = (question or {}).get("reason", "")
    options = (question or {}).get("options") or []

    text = "\n".join([
        _order_line(order),
        "",
        REASON_TEXTS.get(reason, DEFAULT_REASON),
    ])

    rows = [[_option_button(order.order_id, option)] for option in options]
    if reason == "сейлзбот не создал автосделку":
        rows.append([InlineKeyboardButton(text="🔄 Проверить ещё раз",
                                          callback_data=_choice(order.order_id, "retry"))])
    rows.append([
        InlineKeyboardButton(text="➕ Создать новую",
                             callback_data=_choice(order.order_id, "new")),
        InlineKeyboardButton(text="✋ Сам разберусь",
                             callback_data=_choice(order.order_id, "manual")),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def parse_choice(data: Optional[str]) -> Optional[tuple[int, str, Optional[int]]]:
    """Разобрать нажатие: (номер заказа, что выбрали, id сделки).

    Мусор и чужие кнопки → None: робот молча ничего не делает.
    """
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CHOICE_PREFIX or not parts[1].isdigit():
        return None

    order_id, choice = int(parts[1]), parts[2]
    if choice in ("new", "manual", "retry"):
        return order_id, choice, None
    if choice.isdigit():
        return order_id, "lead", int(choice)
    return None


def _option_button(order_id: int, option: dict) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=_option_label(option),
                                callback_data=_choice(order_id, str(option["lead_id"])))


def _option_label(option: dict) -> str:
    parts = ["Сделка"]
    when = _as_date(option.get("date"))
    parts.append(f"{when:%d.%m}" if when else f"#{option['lead_id']}")
    if option.get("price"):
        parts.append(f"· {money(option['price'])} ₽")
    return " ".join(parts)


def _choice(order_id: int, value: str) -> str:
    return f"{CHOICE_PREFIX}:{order_id}:{value}"


def _order_line(order: Any) -> str:
    parts = [f"Заказ №{order.order_id}"]
    if getattr(order, "client_name", None):
        parts.append(order.client_name)
    parts.append(mask(order.phone10))
    parts.append(f"чек {money(order.amount_total)} ₽")
    parts.append(f"{order.created_at:%d.%m}")
    return " · ".join(parts)


# --- вечерняя сводка ---

def summary_text(summary: Any) -> str:
    """Отчёт за день. Сначала то, что требует внимания владельца."""
    lines = ["📊 Вечерняя сверка"]

    if summary.waiting_owner:
        lines += ["", f"❓ Ждут вашего ответа: {len(summary.waiting_owner)} — "
                      f"{_numbers(summary.waiting_owner)}"]
    if summary.stuck:
        lines += ["", f"⚠️ Зависли: {len(summary.stuck)}"]
        lines += [f"   • №{row.order_id}: {row.detail}" for row in summary.stuck if row.detail]
    if summary.missed:
        lines += ["", f"🕳 Не разобрано: {len(summary.missed)} — "
                      f"{', '.join('№' + str(order_id) for order_id in summary.missed)}"]

    lines.append("")
    lines.append(f"✅ Проведено: {len(summary.processed)}")
    if summary.processed:
        lines.append(f"   {_pairs(summary.processed)}")
    if summary.created:
        lines.append(f"🆕 Создано новых сделок: {len(summary.created)}")
        lines.append(f"   {_pairs(summary.created)}")
    if summary.already_done:
        lines.append(f"👤 Вы провели сами: {len(summary.already_done)} — "
                     f"{_numbers(summary.already_done)}")
    if summary.in_flight:
        lines.append(f"⏳ В работе прямо сейчас: {len(summary.in_flight)}")

    if summary.is_quiet:
        lines += ["", "Хвостов нет — разбираться не с чем."]
    return "\n".join(lines)


def _numbers(rows: Sequence[Any]) -> str:
    return ", ".join(f"№{row.order_id}" for row in rows)


def _pairs(rows: Sequence[Any]) -> str:
    """«№581 → #41400001»: по какому заказу какая сделка."""
    return ", ".join(
        f"№{row.order_id} → #{row.lead_id}" if row.lead_id else f"№{row.order_id}"
        for row in rows
    )


# --- предпросмотр хвоста ---

def preview_card(plan: Sequence[PlannedOrder]) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Что робот сделает с хвостом, если разрешить."""
    if not plan:
        return "Хвост пуст: разбирать нечего.", None

    lines = [f"🔍 Предпросмотр: {len(plan)} {_orders_word(len(plan))}", ""]
    for item in plan:
        lines.append(item.title)
        for action, amo_id in item.actions:
            lines.append(f"   • {_action_words(action, amo_id)}")
        if not item.actions:
            lines.append("   • ничего делать не нужно")
        lines.append("")

    lines.append("Это репетиция: в amoCRM я пока ничего не изменил.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Поехали", callback_data=BACKLOG_GO),
        InlineKeyboardButton(text="✋ Отложить", callback_data=BACKLOG_HOLD),
    ]])
    return "\n".join(lines), keyboard


def live_report_text(done: Sequence[PlannedOrder]) -> str:
    """Отчёт после боевого прогона хвоста."""
    if not done:
        return "Заказов не было — ничего не делал."

    lines = [f"🚀 Готово: {len(done)} {_orders_word(len(done))}", ""]
    for item in done:
        lines.append(f"{item.title} — {_status_words(item.status)}")
    return "\n".join(lines)


def _action_words(action: str, amo_id: Optional[int]) -> str:
    words = ACTION_WORDS.get(action, action)
    if "{id}" not in words:
        return words
    if amo_id:
        return words.format(id=f"#{amo_id}")
    return " ".join(words.replace("{id}", "").split())    # сделка ещё не создана


def _status_words(status: str) -> str:
    return {
        "done": "проведено",
        "waiting_owner": "жду вашего ответа",
        "waiting_salesbot": "жду автосделку",
        "in_progress": "в работе",
        "error": "ошибка",
        "new": "в очереди",
    }.get(status, status)


def _orders_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "заказ"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "заказа"
    return "заказов"


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


# Воронка варианта определяет путь заказа: сделка реализации — путь А,
# лид первичной воронки — путь Б.
PATH_BY_PIPELINE = {
    ids.PIPELINE_REALIZATION: "A",
    ids.PIPELINE_PRIMARY: "B",
}


# --- ковры от партнёра ---

CARPET_PREFIX = "carpet"

CARPET_REASONS = {
    "какая сделка про этот заказ": "Нашёл несколько сделок — какая из них про эти ковры?",
    "в строке отчёта не разобрал телефон": "В строке отчёта не разобрал номер телефона — "
                                           "искать сделку не по чему.",
    "сейлзбот не создал автосделку по коврам": "Лид передан в работу, но ковровую "
                                               "автосделку так и не увидел.",
}
CARPET_DEFAULT_REASON = "Не смог решить сам, что делать с этим заказом партнёра."


def carpet_question_card(row: Any, question: Optional[dict]):
    """Вопрос по строке отчёта партнёра."""
    reason = (question or {}).get("reason", "")
    options = (question or {}).get("options") or []

    head = [f"🧶 Ковры · заказ партнёра №{row.partner_id}"]
    if getattr(row, "client_name", None):
        head.append(row.client_name)
    head.append(mask(row.phone10))
    if row.is_refusal:
        head.append("ОТКАЗ")
    else:
        head.append(f"{money(row.amount)} ₽")
    if getattr(row, "district", None):
        head.append(row.district)
    if row.return_date:
        head.append(f"сдано {row.return_date:%d.%m}")

    lines = [" · ".join(head), ""]
    if row.is_refusal and row.refusal_reason:
        lines.append(f"Причина отказа: {row.refusal_reason}")
        lines.append("")
    lines.append(CARPET_REASONS.get(reason, CARPET_DEFAULT_REASON))

    rows = [[InlineKeyboardButton(text=_option_label(option),
                                  callback_data=_carpet_choice(row.partner_id,
                                                               str(option["lead_id"])))]
            for option in options]
    rows.append([
        InlineKeyboardButton(text="➕ Завести сделку",
                             callback_data=_carpet_choice(row.partner_id, "new")),
        InlineKeyboardButton(text="✋ Сам разберусь",
                             callback_data=_carpet_choice(row.partner_id, "manual")),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def parse_carpet_choice(data: Optional[str]) -> Optional[tuple[int, str, Optional[int]]]:
    """Разобрать нажатие на ковровой карточке: (заказ партнёра, выбор, сделка)."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CARPET_PREFIX or not parts[1].isdigit():
        return None

    partner_id, choice = int(parts[1]), parts[2]
    if choice in ("new", "manual"):
        return partner_id, choice, None
    if choice.isdigit():
        return partner_id, "lead", int(choice)
    return None


def carpet_report_text(subject: Optional[str], report: Any) -> str:
    """Итог разбора одного письма партнёра."""
    counts = dict(getattr(report, "by_status", {}) or {})
    lines = [f"🧶 Разобрал отчёт партнёра: {subject or 'без темы'}", ""]
    lines.append(f"✅ Проведено: {counts.get('done', 0)} из {report.processed}")

    waiting = counts.get("waiting_owner", 0)
    if waiting:
        lines.append(f"❓ Ждут вашего ответа: {waiting}")
    in_flight = counts.get("waiting_salesbot", 0) + counts.get("in_progress", 0)
    if in_flight:
        lines.append(f"⏳ В работе (ждут автосделку): {in_flight}")
    if counts.get("error"):
        lines.append(f"⚠️ Ошибок: {counts['error']}")
    if getattr(report, "failures", ()):
        lines.append(f"⚠️ Не разобрано строк: {len(report.failures)}")

    if not (waiting or in_flight or counts.get("error") or getattr(report, "failures", ())):
        lines += ["", "Всё разобрано — разбираться не с чем."]
    return "\n".join(lines)


def _carpet_choice(partner_id: int, value: str) -> str:
    return f"{CARPET_PREFIX}:{partner_id}:{value}"
