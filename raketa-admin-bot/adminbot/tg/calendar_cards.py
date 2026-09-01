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

from typing import Any, Optional, Sequence

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
    "ask_owner_closed": "У клиента есть закрытая сделка ровно на эту дату — "
                        "похоже, вы уже провели этот заказ сами. Это она или "
                        "заказ новый?",
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

    # По закрытой сделке работать нечего: её можно только признать «той самой».
    prefix = "linked" if reason == "ask_owner_closed" else "lead"
    rows = [[InlineKeyboardButton(text=_option_label(option, reason),
                                  callback_data=_choice(f"{prefix}_{option['lead_id']}"))]
            for option in options]
    if reason == "сейлзбот не создал автосделку":
        rows.append([InlineKeyboardButton(text="🔄 Проверить ещё раз",
                                          callback_data=_choice("retry"))])
    rows.append([
        InlineKeyboardButton(text="➕ Создать новую", callback_data=_choice("new")),
        InlineKeyboardButton(text="✋ Сам разберусь", callback_data=_choice("manual")),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _option_label(option: dict, reason: str = "") -> str:
    """Подпись кнопки: номер сделки и её дата, если она известна."""
    label = f"Сделка #{option['lead_id']}"
    if reason == "ask_owner_closed":
        label = f"✔️ Это она — #{option['lead_id']}"
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


# --- тексты для владельца ---

# Внутренние статусы → слова, понятные без объяснений.
STATUS_WORDS: tuple[tuple[str, str], ...] = (
    ("done", "сделок заполнено"),
    ("waiting_salesbot", "ждут автосделку"),
    ("waiting_owner", "ждут вашего ответа"),
    ("closing", "закрываю по вашему подтверждению"),
    ("cancelled", "отменённых заказов"),
    ("skipped", "пропущено (выходные, перемывы, записи без телефона)"),
    ("error", "с ошибкой — повторю сам"),
)


def calendar_summary_text(report: Any, counts: Optional[dict] = None) -> str:
    """Блок вечерней сводки по календарю."""
    lines = ["📅 Календарь"]

    if not report or not (report.changes or report.processed):
        lines.append("Новых записей не было.")
    else:
        lines.append(f"Изменений в календаре: {report.changes}")
        for key, title in STATUS_WORDS:
            value = (report.by_status or {}).get(key)
            if value:
                lines.append(f"   • {title}: {value}")

    if report and report.questions:
        lines.append(f"❓ Жду ответа по {len(report.questions)} записи(ям).")

    if report and report.unknown_districts:
        names = ", ".join(sorted({name.capitalize() for name in report.unknown_districts}))
        lines += ["",
                  f"Не знаю районов для приставок: {names}. "
                  "Скажите, какие это районы, — буду заполнять поле «Район города»."]

    if report and getattr(report, "districts_missing", ()):
        names = ", ".join(sorted({name.capitalize() for name in report.districts_missing}))
        lines += ["",
                  f"В списке «Район города» амо нет: {names}. "
                  "Пока их там нет, поле по таким заказам останется пустым — "
                  "добавьте значения в CRM, и я начну заполнять."]

    if report and report.failures:
        lines.append(f"⚠️ Не разобрал записей: {len(report.failures)} — попробую снова.")

    total_done = (counts or {}).get("done")
    if total_done:
        lines += ["", f"Всего по календарю оформлено сделок: {total_done}"]
    return "\n".join(lines)


def calendar_status_text(*, enabled: bool, dry_run: bool,
                         report: Optional[Any] = None) -> str:
    """Строка о календаре для команды /status."""
    if not enabled:
        lines = ["📅 Календарь: выключен настройками сервиса"]
        return "\n".join(lines)

    mode = ("репетиция — решения принимаю, в amoCRM ничего не пишу"
            if dry_run else "боевой — сделки оформляю по-настоящему")
    lines = [f"📅 Календарь: {mode}"]

    if report is None:
        lines.append("   Обменов ещё не было.")
        return "\n".join(lines)
    if report.paused:
        lines.append("   Сейчас на паузе.")
        return "\n".join(lines)

    lines.append(f"   Последний обмен: изменений {report.changes}, "
                 f"обработано {report.processed}")
    # Календарей может быть несколько (мебель у мастеров, уборки у бригадира).
    # Пока он один, разбивка ничего не добавляет — не засоряем /status.
    by_calendar = dict(getattr(report, "by_calendar", {}) or {})
    if len(by_calendar) > 1:
        for calendar_id, changes in by_calendar.items():
            lines.append(f"   • {calendar_id}: изменений {changes}")
    for calendar_id, error in (getattr(report, "calendars_failed", ()) or ()):
        lines.append(f"   ⚠️ {calendar_id}: Google не ответил ({error})")
    if report.known:
        lines.append(f"   Записей, что были в календаре до включения: {report.known} — "
                     "их не трогаю")
    if report.full_resync:
        lines.append("   Закладка обмена устарела — перечитал календарь заново.")
    return "\n".join(lines)


# --- отчёт репетиции ---

# Действия робота человеческим языком. {id} — место для номера сделки в амо.
ACTION_WORDS = {
    "create_contact": "создам контакт клиента",
    "create_lead": "создам сделку",
    "update_lead": "заполню сделку {id}",
    "move_lead": "переведу сделку {id} в работу",
    "update_contact": "поправлю имя контакта",
    "add_note": "оставлю примечание в сделке {id}",
    "ask_owner": "спрошу вас",
    "salesbot_timeout": "подожду автосделку",
}

# Внутренние состояния записи → что это значит для владельца.
STATE_WORDS = {
    "done": "готово",
    "waiting_salesbot": "жду автосделку сейлзбота",
    "waiting_owner": "жду вашего ответа",
    "closing": "закрою сделку по вашему подтверждению",
    "cancelled": "заказ отменён",
    "error": "не получилось, повторю",
}


def rehearsal_text(link: Any, actions: Sequence[dict]) -> str:
    """Что робот сделал бы с записью. Только для репетиции.

    В репетиции молчать нельзя: уверенные решения робот принимает без вопросов,
    и если о них не рассказывать, прогон покажет владельцу пустоту — а проверить
    он должен именно их.
    """
    lines = ["🧪 Репетиция по записи календаря", _client_line(link)]

    if link.status == "skipped":
        lines.append(f"Пропускаю: {link.skip_reason or 'не похоже на заказ'}.")
        lines.append("В CRM ничего не менял.")
        return "\n".join(lines)

    steps = []
    for action in actions:
        words = ACTION_WORDS.get(action.get("action", ""))
        if not words:
            continue
        step = words.format(id=f"#{action['amo_id']}" if action.get("amo_id") else "")
        if step not in steps:
            steps.append(step)

    if steps:
        lines.append("")
        lines.append("Сделал бы:")
        lines += [f"   • {step}" for step in steps]
    else:
        lines.append("Делать нечего: всё уже заполнено.")

    state = STATE_WORDS.get(link.status)
    if state and link.status != "done":
        lines.append(f"Дальше: {state}.")

    lines += ["", "В CRM ничего не менял — это репетиция."]
    return "\n".join(lines)


def deal_url(base_url: str, lead_id: Optional[int]) -> str:
    return f"{base_url.rstrip('/')}/leads/detail/{lead_id}" if lead_id else ""


def done_text(link: Any, actions: Sequence[dict], *, base_url: str) -> str:
    """Сообщение о сделанной работе — для недели наблюдения.

    Владелец проверяет по горячим следам, поэтому в сообщении ровно то, что
    нужно для проверки: чей заказ, что робот сделал со сделкой (взял готовую
    или завёл новую) и ссылка, по которой смотреть.

    Уходит один раз на запись — когда работа закончена целиком. Промежуточное
    «жду автосделку» владельцу не нужно: ссылка в такой момент ведёт на лид,
    а не на сделку, и три сообщения об одном деле только мешают проверять.
    """
    kinds = {row.get("action") for row in actions}
    created_lead = "create_lead" in kinds
    created_contact = "create_contact" in kinds

    if created_lead:
        what = "создал сделку с нуля"
    elif "move_lead" in kinds:
        what = "взял готовый лид и передал его в работу"
    else:
        what = "взял существующую сделку и дозаполнил"

    lines = ["📅 Календарь · " + what, _client_line(link)]
    if created_contact:
        lines.append("Клиента в CRM не было — завёл новый контакт.")

    url = deal_url(base_url, link.real_lead_id or link.primary_lead_id)
    if url:
        lines += ["", url]
    return "\n".join(lines)


def updated_text(link: Any, changed: Sequence[str], *, base_url: str) -> str:
    """Запись поправили после проведения — что робот подтянул в сделку."""
    lines = [
        "📅 Календарь · запись изменилась",
        _client_line(link),
        f"Поправил в сделке: {', '.join(changed)}.",
    ]
    url = deal_url(base_url, link.real_lead_id or link.primary_lead_id)
    if url:
        lines += ["", url]
    return "\n".join(lines)
