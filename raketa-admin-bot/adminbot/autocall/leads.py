"""Отбор заявок: какая сделка наша и по какому номеру звонить.

Сайт вешает на сделку тег «Заявка с сайта» — по нему autocall отличает свои
заявки от лидов, заведённых руками на том же этапе. С 2026-09-23 наравне с ним
идёт тег «Отклик на промо»: такую сделку заводит сам админ-бот, когда клиент
ответил «1» на промо (решение владельца 7, ТЗ 2026-09-23-promo-autocall.md).
Телефон достаём из контактов сделки: у сделки контактов может быть несколько,
главный помечен флагом is_main.

Чистый модуль: работает над уже полученными ответами амо, в сеть не ходит.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from adminbot.amo.fields import contact_phones, lead_tag_names

# Тег, которым сайт помечает свои заявки в амо.
SITE_TAG = "Заявка с сайта"
# Тег сделки по отклику на промо (её заводит модуль promo_callback).
PROMO_TAG = "Отклик на промо"
# Теги, по которым автозвонок считает сделку своей: любой из них.
AUTOCALL_TAGS = (SITE_TAG, PROMO_TAG)


def is_site_lead(lead: Optional[Mapping[str, Any]]) -> bool:
    """Наша ли это заявка: есть ли на сделке тег «Заявка с сайта» или «Отклик на промо».

    Имя функции осталось от времён одного тега — наблюдатель и экзамен зовут её
    по-прежнему. Тег в амо правят руками, поэтому сравниваем без регистра и без
    краевых пробелов — «заявка с сайта» тоже считается. Часть имени не
    считается: просто «Заявка» — это другой тег.
    """
    wanted = {tag.casefold() for tag in AUTOCALL_TAGS}
    return any(name.strip().casefold() in wanted for name in lead_tag_names(lead))


def lead_phone10(
    lead: Optional[Mapping[str, Any]],
    contacts: Iterable[Mapping[str, Any]],
) -> Optional[str]:
    """Телефон клиента по сделке — 10 цифр, как везде в матчере.

    `contacts` — полные контакты, уже полученные вызывающим: в _embedded
    сделки амо кладёт только id и флаг is_main, без телефонов. Берём главный
    контакт сделки (is_main), без флага — первый; у контакта — первый телефон.
    Телефона нет или номер не разобран → None: что делать с такой заявкой,
    решает наблюдатель, не этот модуль.
    """
    by_id: dict[int, Mapping[str, Any]] = {}
    for contact in contacts:
        if isinstance(contact, Mapping) and contact.get("id") is not None:
            try:
                by_id[int(contact["id"])] = contact
            except (TypeError, ValueError):
                continue

    contact_id = _main_contact_id(lead)
    if contact_id is None:
        return None
    phones = contact_phones(by_id.get(contact_id))
    return phones[0] if phones else None


def _main_contact_id(lead: Optional[Mapping[str, Any]]) -> Optional[int]:
    """id главного контакта сделки; без флага is_main — первого по списку."""
    if not lead:
        return None
    embedded = lead.get("_embedded") or {}
    first: Optional[int] = None
    for contact in embedded.get("contacts") or []:
        if not isinstance(contact, Mapping) or contact.get("id") is None:
            continue
        try:
            contact_id = int(contact["id"])
        except (TypeError, ValueError):
            continue
        if contact.get("is_main"):
            return contact_id
        if first is None:
            first = contact_id
    return first
