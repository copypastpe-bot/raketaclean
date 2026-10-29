"""«Новые лиды», до которых не дошли: какие из них закрыть как несостоявшиеся.

Решение владельца 2026-10-29: сделки этапа «Новый лид» старше недели, по
телефонам которых в календаре нет заказа, закрыть как «Закрыто и не
реализовано» с причиной «Пропала потребность». Свежие остаются менеджеру.

Заказ узнаём по памяти робота о календаре (`adminbot.gcal_events`): каждую
запись он сохраняет с телефонами и датой заказа, в Google за этим ходить не
нужно. Сомнения решаются в пользу «не трогать»: у контакта нет телефона — не
закрываем; на телефон есть запись не раньше дня, когда завели сделку, — не
закрываем, показываем владельцу.

Здесь только отбор; закрывает `scripts/close_new_leads.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ, contact_phones

NEW_LEAD_DAYS = 7
LOSS_REASON_NAME = "Пропала потребность"

# Записи календаря, которые заказом не считаются: выходной мастера, «заказом
# не выглядит» и удалённые. Перемыв, теплоход и «⁉️» считаются: клиент был
# в календаре после заявки — закрывать его сделку рано.
NOT_ORDER_KINDS = ("block", "skip", "cancelled")

CONTACTS_BATCH = 50


@dataclass(frozen=True)
class CalendarMark:
    """Запись календаря на телефон: день заказа, а нет даты — день, когда её увидели."""

    phone10: str
    day: date


@dataclass
class Plan:
    to_close: list[dict] = field(default_factory=list)
    with_order: list[tuple[dict, CalendarMark]] = field(default_factory=list)
    no_phone: list[dict] = field(default_factory=list)


def created_day_msk(lead: Mapping[str, Any]) -> Optional[date]:
    """День, когда завели сделку, по Москве: с ним сверяется дата заказа."""
    created = lead.get("created_at")
    if not created:
        return None
    try:
        return datetime.fromtimestamp(int(created), tz=MOSCOW_TZ).date()
    except (TypeError, ValueError, OSError):
        return None


def lead_contact_ids(lead: Mapping[str, Any]) -> list[int]:
    contacts = ((lead.get("_embedded") or {}).get("contacts")) or []
    return [int(contact["id"]) for contact in contacts
            if isinstance(contact, Mapping) and contact.get("id")]


def find_loss_reason(reasons: Iterable[Mapping[str, Any]], name: str) -> Optional[int]:
    """id причины отказа по названию, без учёта регистра и пробелов по краям."""
    wanted = name.strip().casefold()
    for reason in reasons:
        if str(reason.get("name") or "").strip().casefold() == wanted and reason.get("id"):
            return int(reason["id"])
    return None


def plan_cleanup(leads: Sequence[dict], phones_by_contact: Mapping[int, list[str]],
                 marks: Mapping[str, list[date]]) -> Plan:
    """Разложить уже отобранные по возрасту сделки на три кучки.

    Заказ клиента до этой заявки — не довод: он про прошлую сделку. Довод —
    запись не раньше дня, когда завели эту. Неизвестен день заведения —
    любая запись на телефон останавливает закрытие.
    """
    plan = Plan()
    for lead in leads:
        phones: list[str] = []
        for contact_id in lead_contact_ids(lead):
            for phone in phones_by_contact.get(contact_id) or []:
                if phone not in phones:
                    phones.append(phone)
        if not phones:
            plan.no_phone.append(lead)
            continue

        since = created_day_msk(lead)
        hit: Optional[CalendarMark] = None
        for phone in phones:
            later = sorted(day for day in marks.get(phone) or []
                           if since is None or day >= since)
            if later:
                hit = CalendarMark(phone, later[0])
                break
        if hit is None:
            plan.to_close.append(lead)
        else:
            plan.with_order.append((lead, hit))
    return plan


async def fetch_new_leads(amo: Any) -> list[dict]:
    """Все сделки этапа «Новый лид» первичной воронки, сразу с контактами."""
    params: list[tuple[str, Any]] = [
        ("filter[statuses][0][pipeline_id]", ids.PIPELINE_PRIMARY),
        ("filter[statuses][0][status_id]", ids.PRIM_STAGE_NEW_LEAD),
        ("with", "contacts"),
    ]
    return await amo.get_all("/api/v4/leads", "leads", params=params)


async def fetch_contact_phones(amo: Any, contact_ids: Iterable[int]) -> dict[int, list[str]]:
    """Телефоны контактов пакетами: один запрос на полсотни контактов."""
    unique = sorted({int(contact_id) for contact_id in contact_ids})
    phones: dict[int, list[str]] = {}
    for start in range(0, len(unique), CONTACTS_BATCH):
        chunk = unique[start:start + CONTACTS_BATCH]
        params: list[tuple[str, Any]] = [("filter[id][]", value) for value in chunk]
        for contact in await amo.get_all("/api/v4/contacts", "contacts", params=params):
            if contact.get("id"):
                phones[int(contact["id"])] = contact_phones(contact)
    return phones


CALENDAR_MARKS_SQL = """
SELECT phone,
       COALESCE(e.order_date, (e.created_at AT TIME ZONE 'Europe/Moscow')::date) AS day
FROM adminbot.gcal_events e,
     unnest(COALESCE(
         (SELECT array_agg(value) FROM jsonb_array_elements_text(e.event_data->'phones')),
         ARRAY[e.phone10])) AS phone
WHERE phone = ANY($1::text[])
  AND e.kind <> ALL($2::text[])
  AND e.status <> 'cancelled'
"""

CALENDAR_MEMORY_SQL = """
SELECT min((created_at AT TIME ZONE 'Europe/Moscow')::date) FROM adminbot.gcal_events
"""


async def fetch_calendar_marks(pool: Any, phones: Sequence[str]
                               ) -> tuple[dict[str, list[date]], Optional[date]]:
    """Дни записей календаря по телефонам и с какого дня робот помнит календарь.

    Раньше этого дня записей в памяти нет — для сделок старше него сверка
    неполная, и скрипт их помечает.
    """
    marks: dict[str, list[date]] = {}
    async with pool.acquire() as conn:
        if phones:
            for row in await conn.fetch(CALENDAR_MARKS_SQL, list(phones),
                                        list(NOT_ORDER_KINDS)):
                if row["day"] is not None:
                    marks.setdefault(row["phone"], []).append(row["day"])
        memory_from = await conn.fetchval(CALENDAR_MEMORY_SQL)
    return marks, memory_from
