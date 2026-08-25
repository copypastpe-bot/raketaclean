"""Разбор ответов amoCRM: кастомные поля сделки, телефоны контакта.

Амо отдаёт кастомные поля списком `custom_fields_values` — здесь превращаем
это в понятные значения. Часовой пояс аккаунта — Москва (recon/01-amocrm.md).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from adminbot.amo import ids
from adminbot.phone import last10

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def field_value(entity: Optional[Mapping[str, Any]], field_id: int) -> Any:
    """Значение кастомного поля сущности или None, если поле не заполнено."""
    if not entity:
        return None
    for field in entity.get("custom_fields_values") or []:
        if not isinstance(field, Mapping) or field.get("field_id") != field_id:
            continue
        for item in field.get("values") or []:
            if isinstance(item, Mapping) and item.get("value") not in (None, ""):
                return item["value"]
    return None


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
