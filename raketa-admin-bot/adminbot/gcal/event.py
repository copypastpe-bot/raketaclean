"""Разбор записи календаря: что записано, для кого и что из этого пишем в CRM.

Календарь владельца — рабочий блокнот, а не форма ввода. В нём вперемешку лежат
заказы, выходные мастеров, гарантийные перемывы и рейсы теплоходов; заголовок
пишется от руки, телефон — как получилось. Поэтому разбор устроен так:

1. **Сначала отсеиваем то, что заказом не является.** Лишняя сделка в CRM — самая
   дорогая ошибка этого модуля, дороже пропущенной записи: пропущенный заказ всё
   равно придёт из бота после выполнения, а выдуманный придётся удалять руками.
2. **Телефон ищем консервативно.** Описание полно цен и размеров («Кресло
   800-1500₽», «Матрас 160/1»), и посимвольный скан по всему тексту рано или
   поздно склеит из них номер. Поэтому кандидатом считается только группа ровно
   в 10-11 цифр, а разбор идёт построчно.
3. **Ничего не выдумываем.** Непонятная приставка района — поле пустое, а саму
   приставку робот покажет владельцу в вечерней сводке.

Решения владельца, на которых стоит этот разбор, — `docs/plans/2026-08-26-calendar-plan.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

from adminbot.phone import last10

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class EventKind(str, Enum):
    """Что за запись и как с ней поступать."""

    ORDER = "order"          # обычный заказ — ведём сделку
    CANCELLED = "cancelled"  # запись удалена = заказ отменён (решение 2)
    BLOCK = "block"          # «⛔️Никита» — выходной мастера, пропускаем
    REWASH = "rewash"        # «Перемыв» — гарантийный выезд, денег нет (решение 6)
    UNSETTLED = "unsettled"  # «⁉️» — дата не твёрдая, ждём владельца (решение 9)
    BOAT = "boat"            # теплоход: заводим только по кнопке (решение 7)
    SKIP = "skip"            # заказом не выглядит — молчим (решение 10)


# --- приставка заголовка → район из списка амо (ключи словаря DISTRICT_ENUMS) ---
# Владелец пишет район сокращённо и по-разному: «Сорм» и «Сор» — один район.
DISTRICT_BY_PREFIX: dict[str, str] = {
    "ниж": "нижегородский",
    "сов": "советский",
    "лен": "ленинский",
    "авт": "автозаводский",
    "сорм": "сормовский", "сор": "сормовский",
    "кан": "канавинский",
    "при": "приокский", "приок": "приокский",
    "мос": "московский", "моск": "московский",
    "кст": "кстовский", "ксто": "кстовский",
    "бор": "борский",
    "ап": "анкудиновка",
}

# --- слово из записи → вид услуги ---
# Мебель не детализируем (решение 11): форму дивана в записи не видно, а список
# амо требует выбрать «прямой / угловой / П-образный» — выдумывать нельзя.
SERVICE_BY_WORD: dict[str, str] = {
    "матрас": "mattress", "матрасы": "mattress", "матрасов": "mattress",
    "диван": "furniture", "диваны": "furniture", "мебель": "furniture",
    "стул": "furniture", "стулья": "furniture", "стульев": "furniture",
    "кресло": "furniture", "кресла": "furniture", "пуфик": "furniture",
    "кровать": "furniture", "тахта": "furniture", "уголок": "furniture",
    "изголовье": "furniture", "каркас": "furniture", "подушка": "furniture",
    "подушки": "furniture", "коляска": "furniture", "лежанка": "furniture",
    "ковролин": "carpeting",
    "уборка": "cleaning", "генеральная": "cleaning", "поддерживающая": "cleaning",
    "послестрой": "cleaning", "кухня": "cleaning", "кух": "cleaning",
    "санузел": "cleaning", "перемыв": "rewash",
    "окна": "windows", "окно": "windows", "створки": "windows",
}

# Ковёр на дому ищем отдельно и ПОСЛЕ ковролина: «ковролин» тоже начинается с «ковр».
_RUG_RE = re.compile(r"\bков(?:[её]р|ры|ров|ра)\b", re.IGNORECASE)

# Названия теплоходов (B2B без телефона). Список расширяется строкой здесь же:
# новое судно до этого момента попадёт в SKIP, то есть робот просто промолчит.
BOAT_NAMES: frozenset[str] = frozenset({
    "толстой", "пушкин", "русь", "чернышевский", "кучкин", "теплоход",
})

# Пометки владельца в заголовке.
_BLOCK_MARK = "⛔"
_UNSETTLED_MARKS = ("⁉", "перенос")

# Строки описания, которые в CRM не уезжают (решение 3): коды доступа и пароли.
_SECRET_WORDS = ("код", "пароль", "домофон", "кейбокс", "keybox", "wi-fi", "wifi", "сеть")

# Разделители ВНУТРИ номера: их склеиваем, прежде чем искать цифры.
_PHONE_GLUE_RE = re.compile(r"(?<=\d)[\s\-‑–—()   ]+(?=\d)")
_DIGITS_RE = re.compile(r"\d+")

# Заголовок вида «Сов! Матрас, Юлия» или «Сов, Уборка, Андрей».
_SUMMARY_RE = re.compile(r"^\s*(?P<prefix>[А-Яа-яЁё]{2,6})\s*[!,]\s*(?P<rest>.+)$")


@dataclass(frozen=True)
class ParsedEvent:
    """Запись календаря, разобранная на то, что нужно роботу."""

    event_id: str
    kind: EventKind
    order_date: Optional[date] = None
    phones: tuple[str, ...] = ()
    client_name: Optional[str] = None
    district: Optional[str] = None
    unknown_district: Optional[str] = None   # приставка есть, а района такого нет
    services: tuple[str, ...] = ()
    address: Optional[str] = None
    comment: str = ""
    summary: str = ""
    skip_reason: Optional[str] = None

    @property
    def phone10(self) -> Optional[str]:
        """Основной телефон клиента — первый в записи."""
        return self.phones[0] if self.phones else None


def parse_event(raw: dict) -> ParsedEvent:
    """Разобрать запись Google Calendar. Ничего не запрашивает и не пишет."""
    event_id = str(raw.get("id") or "")

    # Удалённая запись приходит только в инкрементальном обмене и почти пустой:
    # у неё есть id и status, остального Google не отдаёт.
    if raw.get("status") == "cancelled":
        return ParsedEvent(event_id=event_id, kind=EventKind.CANCELLED)

    summary = (raw.get("summary") or "").strip()
    description = raw.get("description") or ""
    order_date = _start_date(raw.get("start") or {})

    if _BLOCK_MARK in summary:
        return ParsedEvent(event_id=event_id, kind=EventKind.BLOCK,
                           order_date=order_date, summary=summary)

    lowered = summary.lower()
    if any(mark in lowered for mark in _UNSETTLED_MARKS):
        return ParsedEvent(event_id=event_id, kind=EventKind.UNSETTLED,
                           order_date=order_date, summary=summary)

    district, unknown_district, rest = _split_summary(summary)
    services = _services(rest, description)
    phones = _phones(f"{rest}\n{description}")
    name = _client_name(rest)

    if "rewash" in services:
        return ParsedEvent(event_id=event_id, kind=EventKind.REWASH,
                           order_date=order_date, summary=summary,
                           client_name=name, district=district)

    if not phones:
        # Без телефона робот не найдёт клиента в CRM. Теплоход спросим у владельца,
        # остальное молча оставим боту: заказ придёт оттуда после выполнения.
        kind = EventKind.BOAT if _looks_like_boat(rest, services) else EventKind.SKIP
        reason = None if kind is EventKind.BOAT else "телефон не найден"
        return ParsedEvent(event_id=event_id, kind=kind, order_date=order_date,
                           summary=summary, client_name=_boat_name(rest) or name,
                           district=district, unknown_district=unknown_district,
                           services=services, skip_reason=reason,
                           address=(raw.get("location") or None),
                           comment=_comment(description))

    return ParsedEvent(
        event_id=event_id, kind=EventKind.ORDER, order_date=order_date,
        phones=phones, client_name=name, district=district,
        unknown_district=unknown_district, services=services,
        address=(raw.get("location") or None), comment=_comment(description),
        summary=summary)


# --- разбор частей ---


def _start_date(start: dict) -> Optional[date]:
    """Дата заказа — всегда московская: по ней ищем сделку и сверяемся с ботом."""
    stamp = start.get("dateTime")
    if stamp:
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:                     # запись без смещения — читаем как МСК
            moment = moment.replace(tzinfo=MOSCOW_TZ)
        return moment.astimezone(MOSCOW_TZ).date()

    whole_day = start.get("date")
    if whole_day:
        return date.fromisoformat(whole_day[:10])
    return None


def _split_summary(summary: str) -> tuple[Optional[str], Optional[str], str]:
    """Отделить приставку района от остального заголовка.

    Возвращает (район, непонятная приставка, остаток). Приставкой считается
    только то, что отделено «!» или запятой и не является названием услуги:
    в «Ковролин Виктория» приставки нет вовсе.
    """
    match = _SUMMARY_RE.match(summary)
    if not match:
        return None, None, summary

    prefix = match.group("prefix").lower()
    rest = match.group("rest").strip()

    if prefix in SERVICE_BY_WORD:                     # «Диван, Татьяна» — это не район
        return None, None, summary

    district = DISTRICT_BY_PREFIX.get(prefix)
    return district, (None if district else prefix), rest


def _services(rest: str, description: str) -> tuple[str, ...]:
    """Какие услуги названы в записи. Порядок сохраняем, повторы убираем."""
    found: list[str] = []
    for word in re.findall(r"[А-Яа-яЁёA-Za-z]+", rest.lower()):
        kind = SERVICE_BY_WORD.get(word)
        if kind and kind not in found:
            found.append(kind)

    if not found:
        # Заголовок молчит — смотрим состав в описании («Диван\nКресло\nТахта»).
        for word in re.findall(r"[А-Яа-яЁёA-Za-z]+", description.lower()):
            kind = SERVICE_BY_WORD.get(word)
            if kind and kind not in found:
                found.append(kind)

    # Ковёр на дому — отдельная услуга амо; ищем в обоих текстах, но не путаем
    # с ковролином, у которого свой пункт списка.
    if "carpeting" not in found and _RUG_RE.search(f"{rest} {description}"):
        found.append("rug_home")

    return tuple(found)


def _phones(text: str) -> tuple[str, ...]:
    """Все телефоны текста, в порядке появления, без повторов.

    Кандидат — группа ровно из 10 или 11 цифр после склейки разделителей внутри
    номера. Всё, что короче или длиннее, — цена, размер или слипшийся мусор:
    лучше не найти телефон, чем привязать заказ к чужой сделке.
    """
    found: list[str] = []
    for line in text.splitlines():
        glued = _PHONE_GLUE_RE.sub("", line)
        for group in _DIGITS_RE.findall(glued):
            if len(group) not in (10, 11):
                continue
            digits = last10(group)
            if digits and digits not in found:
                found.append(digits)
    return tuple(found)


def _client_name(rest: str) -> Optional[str]:
    """Имя клиента из заголовка: то, что стоит после услуги.

    Нужно только для карточки владельцу и для нового контакта — настоящее имя
    придёт из бота вместе с заказом.
    """
    tail = rest.split(",")[-1].strip() if "," in rest else rest
    words = [word for word in re.findall(r"[А-ЯЁ][а-яё]+", tail)
             if word.lower() not in SERVICE_BY_WORD]
    return words[-1] if words else None


def _looks_like_boat(rest: str, services: tuple[str, ...]) -> bool:
    """Теплоход: знакомое название и никакой домашней услуги в заголовке."""
    if services:
        return False
    words = {word.lower() for word in re.findall(r"[А-Яа-яЁё]+", rest)}
    return bool(words & BOAT_NAMES)


def _boat_name(rest: str) -> Optional[str]:
    for word in re.findall(r"[А-Яа-яЁё]+", rest):
        if word.lower() in BOAT_NAMES:
            return word
    return None


def _comment(description: str) -> str:
    """Текст записи для поля «Комментарий к заказу».

    Убираем строки с телефонами и строки с кодами доступа: в CRM они не нужны,
    а утечь могут (решение владельца 3). Всё остальное — состав, цены, жаргон
    и пожелания клиента — сохраняем как есть: это рабочий язык мастера.
    """
    kept: list[str] = []
    for line in description.splitlines():
        stripped = line.strip()
        if not stripped:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        lowered = stripped.lower()
        if any(word in lowered for word in _SECRET_WORDS):
            continue
        if _phones(stripped):
            continue
        kept.append(stripped)

    while kept and kept[-1] == "":
        kept.pop()
    return "\n".join(kept)
