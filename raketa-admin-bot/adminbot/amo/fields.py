"""Разбор ответов amoCRM: кастомные поля сделки, телефоны контакта.

Амо отдаёт кастомные поля списком `custom_fields_values` — здесь превращаем
это в понятные значения. Часовой пояс аккаунта — Москва (recon/01-amocrm.md).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from adminbot.amo import ids
from adminbot.phone import last10

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def as_msk(moment: datetime) -> datetime:
    """Время для печати человеку — по Москве, а не как оно лежит в базе.

    В базе рабочего бота время приходит в UTC (aware) — переводим в MSK.
    Наивное время (без часового пояса) считаем уже московским, менять нечего.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=MOSCOW_TZ)
    return moment.astimezone(MOSCOW_TZ)


def field_value(entity: Optional[Mapping[str, Any]], field_id: int) -> Any:
    """Значение кастомного поля сущности или None, если поле не заполнено."""
    if not entity:
        return None
    for field in entity.get("custom_fields_values") or []:
        if not isinstance(field, Mapping) or field.get("field_id") != field_id:
            continue
        for item in field.get("values") or []:
            if not isinstance(item, Mapping):
                continue
            if item.get("value") not in (None, ""):
                return item["value"]
            # У списочных полей значение бывает только номером варианта. Поле всё
            # равно заполнено, и затирать его нельзя.
            if item.get("enum_id") is not None:
                return item["enum_id"]
    return None


async def fetch_lead_address(amo: Any, link: Any) -> Optional[str]:
    """Перечитать сделку в amoCRM и достать «Адрес» из неё заново.

    Общий код для кнопки «Я заполнил» (`tg/bot.py`, задача 7 ТЗ 2026-09-16) и
    суточной перепроверки перед напоминанием (`sync/address_reminder.py`,
    задача 10 того же ТЗ) — оба не верят тому, что лежит в связке, и идут
    в CRM заново: сделка могла измениться с прошлого раза.
    """
    lead_id = link.real_lead_id or link.primary_lead_id
    if lead_id is None:
        return None
    return field_value(await amo.get_lead(lead_id), ids.FIELD_ADDRESS)


def order_date_msk(lead: Optional[Mapping[str, Any]]) -> Optional[date]:
    """Дата из поля «Дата и время заказа» по московскому времени.

    Именно её матчер сверяет с датой заказа из бота (±2 дня).
    """
    raw = field_value(lead, ids.FIELD_ORDER_DATETIME)
    if raw in (None, ""):
        return None
    try:
        stamp = int(raw)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(MOSCOW_TZ).date()


def contact_phones(contact: Optional[Mapping[str, Any]]) -> list[str]:
    """Все телефоны контакта в виде 10 цифр — ключ сравнения с заказом бота."""
    if not contact:
        return []
    phones: list[str] = []
    for field in contact.get("custom_fields_values") or []:
        if not isinstance(field, Mapping):
            continue
        if str(field.get("field_code") or "").upper() != "PHONE":
            continue
        for item in field.get("values") or []:
            if not isinstance(item, Mapping):
                continue
            digits = last10(str(item.get("value") or ""))
            if digits and digits not in phones:
                phones.append(digits)
    return phones


# --- сборка значений полей для записи ---

def text_field(field_id: int, value: Any) -> dict:
    """Текстовое поле сделки: адрес, комментарий."""
    return {"field_id": field_id, "values": [{"value": value}]}


def checkbox_field(field_id: int, value: bool) -> dict:
    """Галочка: амо принимает её булевым значением, а не строкой «1».

    Снятая галочка отправляется явным `False`, а не пустым значением: пустое
    амо трактует как «не трогай это поле», и снять пометку стало бы нечем.
    """
    return {"field_id": field_id, "values": [{"value": bool(value)}]}


def enum_field(field_id: int, enum_id: int) -> dict:
    """Значение из списка: «Услуга», «Специалист»."""
    return {"field_id": field_id, "values": [{"enum_id": enum_id}]}


def enums_field(field_id: int, enum_ids: Sequence[int]) -> dict:
    """Несколько значений одного списка сразу.

    «Услуга» — поле с множественным выбором, и запись календаря вида
    «Уборка + Мебель» должна попасть в сделку целиком. Отдельными полями это
    не отправить: амо возьмёт последнее.
    """
    return {"field_id": field_id, "values": [{"enum_id": enum_id} for enum_id in enum_ids]}


def datetime_field(field_id: int, moment: datetime) -> dict:
    """Дата и время: амо хранит их как unix-время.

    Время без часового пояса считаем московским — аккаунт живёт в Москве.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MOSCOW_TZ)
    return {"field_id": field_id, "values": [{"value": int(moment.timestamp())}]}


def date_field(field_id: int, day: date) -> dict:
    """Поле типа «дата»: амо хранит начало дня по Москве как unix-время."""
    moment = datetime(day.year, day.month, day.day, tzinfo=MOSCOW_TZ)
    return {"field_id": field_id, "values": [{"value": int(moment.timestamp())}]}


def enum_ids(entity: Optional[Mapping[str, Any]], field_id: int) -> tuple[int, ...]:
    """Выбранные значения multiselect-поля (id вариантов из списка).

    Для «Специалиста» это мастера, назначенные на сделку; для «Услуги» — виды работ.
    """
    if not entity:
        return ()
    found: list[int] = []
    for field in entity.get("custom_fields_values") or []:
        if not isinstance(field, Mapping) or field.get("field_id") != field_id:
            continue
        for item in field.get("values") or []:
            if not isinstance(item, Mapping) or item.get("enum_id") is None:
                continue
            try:
                value = int(item["enum_id"])
            except (TypeError, ValueError):
                continue
            if value not in found:
                found.append(value)
    return tuple(found)


def specialist_ids(lead: Optional[Mapping[str, Any]]) -> tuple[int, ...]:
    """Мастера, указанные в сделке в поле «Специалист»."""
    return enum_ids(lead, ids.FIELD_SPECIALIST)


def contact_lead_ids(contact: Optional[Mapping[str, Any]]) -> list[int]:
    """id сделок, привязанных к контакту (нужен параметр with=leads).

    Это единственный надёжный источник связи «клиент → его сделки»: фильтр
    `/api/v4/leads?filter[contacts][id]=…` амо молча игнорирует.
    """
    if not contact:
        return []
    embedded = contact.get("_embedded") or {}
    ids_: list[int] = []
    for lead in embedded.get("leads") or []:
        if isinstance(lead, Mapping) and lead.get("id") is not None:
            try:
                ids_.append(int(lead["id"]))
            except (TypeError, ValueError):
                continue
    return ids_


def lead_tag_names(lead: Optional[Mapping[str, Any]]) -> tuple[str, ...]:
    """Имена тегов сделки из _embedded.tags.

    По тегу «Заявка с сайта» autocall отличает заявки с сайта от остальных.
    Сделка без тегов — обычное дело: возвращаем пустой кортеж, не ошибку.
    """
    if not lead:
        return ()
    embedded = lead.get("_embedded") or {}
    names: list[str] = []
    for tag in embedded.get("tags") or []:
        if isinstance(tag, Mapping) and tag.get("name"):
            names.append(str(tag["name"]))
    return tuple(names)


def lead_contact_ids(lead: Optional[Mapping[str, Any]]) -> list[int]:
    """id контактов, привязанных к сделке (нужен параметр with=contacts)."""
    if not lead:
        return []
    embedded = lead.get("_embedded") or {}
    ids_: list[int] = []
    for contact in embedded.get("contacts") or []:
        if isinstance(contact, Mapping) and contact.get("id") is not None:
            try:
                ids_.append(int(contact["id"]))
            except (TypeError, ValueError):
                continue
    return ids_
