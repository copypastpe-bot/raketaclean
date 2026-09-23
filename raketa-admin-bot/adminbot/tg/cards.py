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
from html import escape
from typing import Any, Optional, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from adminbot.amo import ids
from adminbot.amo.fields import as_msk
from adminbot.phone import for_owner, mask
from adminbot.sync.backlog import PlannedOrder, money
from adminbot.sync.matcher import PLACEHOLDER_PRICE

# Приставка callback-данных карточки-вопроса: amosync:{номер заказа}:{выбор}.
# У уборок приставка своя: номера работ в базе бота пересекаются, и без неё
# ответ по «Уборке №5» ушёл бы в «Заказ №5».
CHOICE_PREFIX = "amosync"
CLEANING_CHOICE_PREFIX = "amoclean"
PREFIX_BY_KIND = {"order": CHOICE_PREFIX, "cleaning": CLEANING_CHOICE_PREFIX}
BACKLOG_GO = "backlog:go"
BACKLOG_HOLD = "backlog:hold"

# Пометка репетиции (задача 3, 16.09): в CRM ничего не изменилось, а карточка
# без неё неотличима от боевой. Общая для заказов, уборок, ковров и календаря.
REHEARSAL_PREFIX = "🎭 РЕПЕТИЦИЯ · "


def mark_rehearsal(text: str, dry_run: bool) -> str:
    """Добавить пометку репетиции в первую строку, если это репетиция."""
    return f"{REHEARSAL_PREFIX}{text}" if dry_run else text

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

def question_card(order: Any, question: Optional[dict],
                  *, dry_run: bool = False,
                  base_url: Optional[str] = None) -> tuple[str, InlineKeyboardMarkup]:
    """Вопрос по одной работе: что за работа и между чем выбирать.

    Сделки-варианты на кнопках раньше выглядели одинаково («Сделка 20.09 ·
    сумма» трижды у Натальи 20.09, факт 22.09) — теперь кнопка несёт только
    номер, а различающий текст (дата, адрес, сумма, ссылка) идёт списком над
    кнопками (задача 7, ТЗ 2026-09-22).
    """
    reason = (question or {}).get("reason", "")
    options = (question or {}).get("options") or []
    prefix = PREFIX_BY_KIND.get(getattr(order, "kind", "order"), CHOICE_PREFIX)

    lines = [
        _order_line(order),
        "",
        REASON_TEXTS.get(reason, DEFAULT_REASON),
    ]
    if options:
        lines += ["", *_option_lines(options, base_url)]
    text = mark_rehearsal("\n".join(lines), dry_run)

    rows = [[_option_button(number, order.order_id, option, prefix)]
            for number, option in enumerate(options, 1)]
    if reason == "сейлзбот не создал автосделку":
        rows.append([InlineKeyboardButton(
            text="🔄 Проверить ещё раз",
            callback_data=_choice(order.order_id, "retry", prefix))])
    rows.append([
        InlineKeyboardButton(text="➕ Создать новую",
                             callback_data=_choice(order.order_id, "new", prefix)),
        InlineKeyboardButton(text="✋ Сам разберусь",
                             callback_data=_choice(order.order_id, "manual", prefix)),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def parse_choice(data: Optional[str],
                 prefix: str = CHOICE_PREFIX) -> Optional[tuple[int, str, Optional[int]]]:
    """Разобрать нажатие: (номер работы, что выбрали, id сделки).

    Мусор и чужие кнопки → None: робот молча ничего не делает. Приставка — часть
    проверки: карточка уборки и карточка заказа с одним номером не должны
    отвечать друг за друга.
    """
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != prefix or not parts[1].isdigit():
        return None

    order_id, choice = int(parts[1]), parts[2]
    if choice in ("new", "manual", "retry"):
        return order_id, choice, None
    if choice.isdigit():
        return order_id, "lead", int(choice)
    return None


def _option_button(number: int, order_id: int, option: dict,
                   prefix: str) -> InlineKeyboardButton:
    """Кнопка варианта — только номер, различие в тексте над кнопками."""
    return InlineKeyboardButton(
        text=str(number),
        callback_data=_choice(order_id, str(option["lead_id"]), prefix))


def _option_lines(options: Sequence[dict], base_url: Optional[str]) -> list[str]:
    """Пронумерованный список сделок-вариантов текстом: `1) 20.09 · адрес · ссылка`."""
    return [f"{number}) {_option_line(option, base_url)}"
            for number, option in enumerate(options, 1)]


def _option_line(option: dict, base_url: Optional[str]) -> str:
    parts = []
    when = _as_date(option.get("date"))
    parts.append(f"{when:%d.%m}" if when else f"#{option['lead_id']}")
    if option.get("address"):
        parts.append(option["address"])
    price = option.get("price")
    if price and price >= PLACEHOLDER_PRICE:
        parts.append(f"{money(price)} ₽")
    link = _deal_url(base_url, option.get("lead_id"))
    if link:
        parts.append(link)
    return " · ".join(parts)


def _deal_url(base_url: Optional[str], lead_id: Optional[int]) -> str:
    """Ссылка на сделку в амо — тем же способом, что и calendar_cards.deal_url."""
    if not base_url or not lead_id:
        return ""
    return f"{base_url.rstrip('/')}/leads/detail/{lead_id}"


def _choice(order_id: int, value: str, prefix: str = CHOICE_PREFIX) -> str:
    return f"{prefix}:{order_id}:{value}"


def _order_line(order: Any) -> str:
    parts = [f"{getattr(order, 'label', 'Заказ')} №{order.order_id}"]
    if getattr(order, "client_name", None):
        parts.append(order.client_name)
    parts.append(for_owner(order.phone10))
    parts.append(f"чек {money(order.amount_total)} ₽")
    parts.append(f"заказ {as_msk(order.created_at):%d.%m.%Y %H:%M}")
    return " · ".join(parts)


# --- карточка «сделка без адреса» (ТЗ 2026-09-16, задача 7) ---

# Приставки своих кнопок — отдельные от карточки-вопроса (CHOICE_PREFIX и
# CLEANING_CHOICE_PREFIX): номера работ пересекаются, а к моменту этой карточки
# заказ уже «done», так что путать с ask_owner нечего, но приставка должна
# оставаться однозначной сама по себе.
ADDR_PREFIX = "addr"
CLEANING_ADDR_PREFIX = "addrclean"


def address_missing_card(link: Any, *, label: str = "Заказ", prefix: str = ADDR_PREFIX,
                         base_url: str, reminder_no: int, cap: int = 7,
                         ) -> tuple[str, InlineKeyboardMarkup]:
    """Сделка заведена с нуля, а адреса в ней нет — просим владельца вписать его в CRM."""
    lead_id = link.real_lead_id or link.primary_lead_id
    lines = [f"📍 {label} №{link.order_id} · сделку завёл с нуля, адреса в ней нет"]
    if link.phone10:
        lines.append(for_owner(link.phone10))
    lines += ["", "Впишите адрес прямо в сделку в amoCRM — дальше подхвачу сам."]
    if lead_id:
        lines += ["", f"{base_url.rstrip('/')}/leads/detail/{lead_id}"]
    lines += ["", f"Напоминание {reminder_no} из {cap}."]

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я заполнил",
                             callback_data=_choice(link.order_id, "filled", prefix)),
        InlineKeyboardButton(text="🔕 Не напоминать",
                             callback_data=_choice(link.order_id, "mute", prefix)),
    ]])
    return "\n".join(lines), keyboard


def parse_address_choice(data: Optional[str],
                         prefix: str = ADDR_PREFIX) -> Optional[tuple[int, str]]:
    """Разобрать нажатие на карточке «адреса нет»: (номер работы, что выбрали).

    Свой парсер, а не `parse_choice`: там выбор — из фиксированного списка сделок
    и служебных слов («new», «manual», «retry»), «filled»/«mute» в нём чужие.
    """
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != prefix or not parts[1].isdigit():
        return None
    choice = parts[2]
    if choice not in ("filled", "mute"):
        return None
    return int(parts[1]), choice


# --- вечерняя сводка (ТЗ 2026-09-21-evening-summary-rework.md) ---

# Хвосты — категории и подписи, в порядке утверждённого макета.
_TAIL_CATEGORIES = (
    ("waiting_owner", "❓ Ждут вашего ответа"),
    ("failed", "⛔ Сбой робота"),
    ("stale", "⏳ Зависли дольше часа"),
    ("missed", "🕳 Не разобрано"),
)
# Не больше позиций в категории, дальше «…и ещё N» (умолчание координатора 21.09,
# владелец не возражал — меняется одной строкой).
TAIL_CAP = 10

# Признак «календарный проход не отработал» (задача 9, ТЗ
# 2026-09-21-evening-summary-rework.md): число в «Завёл из календаря» и так
# остаётся честным нулём, но сам сбой владелец должен увидеть там, где он
# и так ищет проблемы, — строкой в хвостах, а не только в журнале сервера.
CALENDAR_FAILURE_LINE = "⛔ Сбой: календарный проход не отработал"


def summary_text(summary: Any, *, calendar_created: int = 0, calendar_handled: int = 0,
                 calendar_failed: bool = False, base_url: Optional[str] = None) -> str:
    """Отчёт за день: сверху четыре числа — что сделал робот, — снизу хвосты —
    что требует вмешательства владельца.

    Числа и хвосты — две независимые части (см. «Связь со службой оповещений»,
    ТЗ 2026-09-21-evening-summary-rework.md): своя функция на каждую
    (`_numbers_block`, `_tails_block`), здесь только их склейка. Когда потоки
    переедут на шину — первая часть в операционную ленту, вторая владельцу
    лично, — резать нужно будет по этому шву.

    `calendar_created`/`calendar_handled` — календарный контур считается
    отдельно от `amo_links`/`cleaning_links` (`db.count_calendar_created`,
    `db.count_calendar_owner_handled`, задачи 2 и 8 того же ТЗ) и подмешивается
    сюда вызывающей стороной, а не собирается этой функцией.

    `calendar_failed` (задача 9) — календарный проход упал, число выше честный
    ноль, но день из-за этого не тихий: `CALENDAR_FAILURE_LINE` идёт первой
    строкой хвостов.

    `base_url` (задача 5) — адрес CRM (`AMO_BASE_URL`), если он известен
    вызывающей стороне: номер сделки в строках хвостов становится кликабельной
    ссылкой `#<номер>` на карточку в amoCRM. `OwnerMail` включает HTML-разметку
    для этого сообщения той же связкой (`Purpose.parse_mode`, `tg/outbox.py`),
    поэтому текст ошибки в хвосте «Сбой робота» экранируется — без адреса
    ссылка не строится, и строка выглядит как раньше.
    """
    cleaning = getattr(summary, "cleaning", None)

    lines = ["📊 Вечерняя сверка", ""]
    lines += _numbers_block(summary, calendar_created=calendar_created,
                            calendar_handled=calendar_handled)
    if cleaning is not None:
        lines += ["", "🧹 Уборки"] + _numbers_block(cleaning)

    lines.append("")
    quiet = (summary.is_quiet and (cleaning is None or cleaning.is_quiet)
            and not calendar_failed)
    lines += (["Хвостов нет — разбираться не с чем."] if quiet
             else _tails_block(summary, calendar_failed=calendar_failed, base_url=base_url))
    return "\n".join(lines)


def _numbers_block(summary: Any, *, calendar_created: int = 0, calendar_handled: int = 0) -> list[str]:
    """Четыре числа «что сделал робот» за сутки («Ждут адрес» — исключение,
    весь накопленный хвост). Ни одного движения — «Событий не было.» вместо
    четырёх нулей (пустой случай, задача 4).
    """
    numbers = (
        ("Завёл из календаря", calendar_created),
        ("Провёл из бота", summary.processed_today),
        ("Передано администратору", summary.handed_to_owner + calendar_handled),
        ("Ждут адрес", summary.waiting_address),
    )
    if not any(count for _, count in numbers):
        return ["Событий не было."]
    return [f"{label}: {count}" for label, count in numbers]


def _tails_block(summary: Any, *, calendar_failed: bool = False,
                 base_url: Optional[str] = None) -> list[str]:
    """Что требует вмешательства владельца — заказы и уборки вместе, одной
    картиной: владельцу нужен один список хвостов, а не два по потокам
    (то же правило, что уже держит `DailySummary.is_quiet`)."""
    cleaning = getattr(summary, "cleaning", None)
    lines = ["⚠️ Хвосты"]
    if calendar_failed:
        lines.append("")
        lines.append(CALENDAR_FAILURE_LINE)
    for field, label in _TAIL_CATEGORIES:
        rows = list(getattr(summary, field))
        if cleaning is not None:
            rows += list(getattr(cleaning, field))
        lines.append("")
        lines.append(f"{label}: {len(rows)}")
        lines += _tail_rows(rows, with_detail=(field == "failed"), base_url=base_url)
    return lines


def _tail_rows(rows: Sequence[Any], *, with_detail: bool,
               base_url: Optional[str] = None) -> list[str]:
    """Строки одной категории хвостов: заказ, телефон, дата, сделка.

    У сбоя (`with_detail`) следующей строкой идёт текст ошибки — остальные
    категории его не показывают, макет утверждён без него.

    `base_url` (задача 5) — известный адрес CRM превращает номер сделки
    в кликабельную ссылку; заодно включается HTML-экранирование текста ошибки
    (без него ссылка не строится, и всё выглядит как раньше — символ вроде
    `<` в тексте ошибки иначе сломал бы разбор сообщения в Telegram).
    """
    lines: list[str] = []
    for row in rows[:TAIL_CAP]:
        parts = [f"№{row.order_id}"]
        phone = getattr(row, "phone10", None)
        if phone:
            parts.append(for_owner(phone))
        when = getattr(row, "order_date", None)
        if when:
            parts.append(f"{when:%d.%m}")
        lead_id = getattr(row, "lead_id", None)
        if lead_id:
            parts.append(_lead_ref(lead_id, base_url))
        lines.append("   • " + " · ".join(parts))
        if with_detail:
            detail = getattr(row, "detail", None)
            if detail:
                lines.append(f"     {escape(detail, quote=False) if base_url else detail}")
    if len(rows) > TAIL_CAP:
        lines.append(f"   …и ещё {len(rows) - TAIL_CAP}")
    return lines


def _lead_ref(lead_id: int, base_url: Optional[str]) -> str:
    """Номер сделки в строке хвоста: `#<номер>`, а если известен адрес CRM —
    тот же текст кликабельной ссылкой на карточку в amoCRM (задача 5)."""
    text = f"#{lead_id}"
    if not base_url:
        return text
    url = f"{base_url.rstrip('/')}/leads/detail/{lead_id}"
    return f'<a href="{url}">{text}</a>'


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


def carpet_question_card(row: Any, question: Optional[dict], *, dry_run: bool = False,
                         base_url: Optional[str] = None):
    """Вопрос по строке отчёта партнёра."""
    reason = (question or {}).get("reason", "")
    options = (question or {}).get("options") or []

    head = [f"🧶 Ковры · заказ партнёра №{row.partner_id}"]
    if getattr(row, "client_name", None):
        head.append(row.client_name)
    head.append(for_owner(row.phone10))
    if row.is_refusal:
        head.append("ОТКАЗ")
    else:
        head.append(f"{money(row.amount)} ₽")
    if getattr(row, "district", None):
        head.append(row.district)
    if row.added_date:
        head.append(f"заказ у партнёра {row.added_date:%d.%m.%Y}")
    if row.return_date:
        head.append(f"сдано {row.return_date:%d.%m.%Y}")

    lines = [" · ".join(head), ""]
    if row.is_refusal and row.refusal_reason:
        lines.append(f"Причина отказа: {row.refusal_reason}")
        lines.append("")
    lines.append(CARPET_REASONS.get(reason, CARPET_DEFAULT_REASON))
    if options:
        lines += ["", *_option_lines(options, base_url)]

    rows = [[InlineKeyboardButton(text=str(number),
                                  callback_data=_carpet_choice(row.partner_id,
                                                               str(option["lead_id"])))]
            for number, option in enumerate(options, 1)]
    rows.append([
        InlineKeyboardButton(text="➕ Завести сделку",
                             callback_data=_carpet_choice(row.partner_id, "new")),
        InlineKeyboardButton(text="✋ Сам разберусь",
                             callback_data=_carpet_choice(row.partner_id, "manual")),
    ])
    return mark_rehearsal("\n".join(lines), dry_run), InlineKeyboardMarkup(inline_keyboard=rows)


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


def carpet_report_text(subject: Optional[str], report: Any, *, dry_run: bool = False) -> str:
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
    return mark_rehearsal("\n".join(lines), dry_run)


def carpet_held_text(subject: Optional[str], reason: str, rows_total: int,
                     refused_total: int, uid: str, *, dry_run: bool = False) -> str:
    """Письмо партнёра отложено: робот его не проводил и ждёт решения владельца.

    Сообщение уходит один раз на письмо. Владельцу нужны две вещи: почему робот
    остановился и что с этим делать — поэтому обе команды прямо в тексте.
    """
    completed = max(rows_total - refused_total, 0)
    return mark_rehearsal("\n".join([
        f"🧶 Ковры: письмо «{subject or 'без темы'}» отложено, не проведено.",
        f"Причина: {reason}.",
        f"В файле: {completed} выполненных, {refused_total} отказ(ов).",
        "",
        f"Если это нормальный отчёт: sudo raketa-admin-bot-update --carpets-release={uid}",
        "Если архив или чужой файл: удалите письмо из папки robot_amo.",
    ]), dry_run)


def _carpet_choice(partner_id: int, value: str) -> str:
    return f"{CARPET_PREFIX}:{partner_id}:{value}"


# --- сообщения о сделанной работе (неделя наблюдения с 2026-08-27) ---

# Путь заказа → что это значило на деле, словами владельца.
PATH_WORDS = {
    "A": "взял готовую сделку и довёл до конца",
    "B": "заполнил лид, передал в работу и довёл сделку до конца",
    "C": "создал сделку с нуля и довёл до конца",
    "done": "ничего не делал: вы уже провели её сами, я только запомнил связку",
}


def order_done_text(order: Any, link: Any, *, base_url: str, dry_run: bool = False) -> str:
    """Сообщение о проведённой работе — чтобы проверить по горячим следам."""
    label = getattr(order, "label", "Заказ")
    lines = [
        f"✅ {label} из бота · " + PATH_WORDS.get(link.path or "", "провёл сделку"),
        _order_line(order),
    ]
    lead_id = link.real_lead_id or link.primary_lead_id
    if lead_id:
        lines += ["", f"{base_url.rstrip('/')}/leads/detail/{lead_id}"]
    return mark_rehearsal("\n".join(lines), dry_run)


def wire_payment_synced_text(order: Any, link: Any, *, base_url: str, tasks_closed: int,
                             stage_moved: bool, dry_run: bool = False) -> str:
    """Сделка доведена после оплаты по счёту (задача 11, ТЗ 2026-09-22).

    Отчёт уходит и при частичной доводке (сумма и/или задачи поправлены, а
    сделка уже была финальной) — тогда `stage_moved=False`, и слов о переводе
    стадии в тексте нет (ревью 23.09): владелец не должен подумать, что стадию
    поменяли, если её не трогали.
    """
    label = getattr(order, "label", "Заказ")
    parts = [f"{label} №{order.order_id}"]
    if getattr(order, "client_name", None):
        parts.append(order.client_name)
    parts.append(for_owner(order.phone10))
    parts.append(f"{money(order.amount_total)} ₽ по счёту")
    lead_id = link.real_lead_id
    if lead_id:
        if stage_moved:
            parts.append(f"сделка #{lead_id} → «выполнено и оплата получена»")
        else:
            parts.append(f"сделка #{lead_id}")
    parts.append(f"закрыто задач: {tasks_closed}")
    lines = ["💸 " + " · ".join(parts)]
    if lead_id:
        lines += ["", _deal_url(base_url, lead_id)]
    return mark_rehearsal("\n".join(lines), dry_run)
