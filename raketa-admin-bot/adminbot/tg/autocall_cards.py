"""Тексты сообщений автозвонка: менеджеру и владельцу.

Черновики — владелец утвердит их перед боевым запуском (контрольная точка
Задачи 12, docs/plans/2026-08-31-autocall-implementation.md, шаг 4). Здесь
только строители текста: вход — данные, выход — строка. Ни сети, ни Telegram,
ни фабрик-отправителей — их собирает main.py в Задаче 11.

Полный телефон клиента — в сообщениях менеджеру и владельцу целиком: бот
личный, а владельцу (и менеджеру) номер нужен, чтобы дозвониться самим, не
открывая CRM (решение владельца 2026-08-26). Маска — только в логах
(`adminbot.phone.mask`).
"""

from __future__ import annotations

from typing import Optional

from adminbot.autocall.chain import NOTIFY_KINDS
from adminbot.phone import for_owner

# Тексты по kind — дословные черновики от владельца (см. docstring модуля).
_MANAGER_TEXTS: dict[str, str] = {
    "client_retry_10":
        "Попытка звонка не удалась, повтор через 10 минут.",
    "no_contact_final":
        "Клиент дважды не ответил. Сделка перенесена в «Не было 1-го "
        "касания» — дальше вручную.",
    "manager_unreachable":
        "Не дозвонились до менеджера по заявке с сайта. Робот больше не "
        "звонит по ней.",
    "attempts_exhausted":
        "Попытки звонка по заявке исчерпаны (4). Робот остановился.",
}


def _deal_url(base_url: str, lead_id: int) -> str:
    """Ссылка на сделку в амо — тем же способом, что и calendar_cards.deal_url."""
    return f"{base_url.rstrip('/')}/leads/detail/{lead_id}"


def manager_text(kind: str, lead_id: int, *, base_url: str,
                 phone10: Optional[str] = None) -> str:
    """Сообщение менеджеру по исходу попытки дозвона (см. chain.NOTIFY_KINDS).

    Движок передаёт сюда только значения из NOTIFY_KINDS — но если где-то в
    цепочке появится опечатка или новый kind без текста, лучше упасть здесь
    явной ошибкой, чем молча отправить менеджеру пустоту.
    """
    try:
        head = _MANAGER_TEXTS[kind]
    except KeyError:
        raise ValueError(
            f"неизвестный вид сообщения менеджеру: {kind!r}; "
            f"допустимы {NOTIFY_KINDS}"
        ) from None

    lines = [head]
    if phone10:
        lines.append(f"Телефон клиента: {for_owner(phone10)}")
    lines.append(_deal_url(base_url, lead_id))
    return "\n".join(lines)


def rehearsal_text(lead_id: int, phone10: str, *, base_url: str) -> str:
    """Владельцу в репетиции: что робот сделал бы, но не сделал.

    В репетиции робот не звонит и не пишет в амо — молчать об этом решении
    нельзя, иначе прогон покажет владельцу пустоту вместо того, что нужно
    проверить (тот же принцип, что и у rehearsal_text календаря).
    """
    return "\n".join([
        "Репетиция: позвонил бы сейчас менеджеру по заявке с сайта.",
        f"Телефон клиента: {for_owner(phone10)}",
        _deal_url(base_url, lead_id),
    ])


def connected_text(lead_id: int, phone10: str, *, base_url: str) -> str:
    """Владельцу при соединении менеджера с клиентом — неделя наблюдения.

    Одно сообщение на одну доведённую до конца цепочку: владелец проверяет
    по горячим следам, промежуточные шаги ему не нужны.
    """
    return "\n".join([
        "Автозвонок: соединил менеджера с клиентом по заявке с сайта.",
        f"Телефон клиента: {for_owner(phone10)}",
        _deal_url(base_url, lead_id),
    ])


def stuck_alert_text(minutes: int) -> str:
    """Владельцу при недоступности АТС дольше порога — без ссылки на сделку.

    Тревога не про конкретную заявку, а про транспорт целиком, поэтому
    вместо ссылки — сколько минут АТС уже не отвечает.
    """
    return (
        f"АТС не отвечает уже {minutes} мин — автозвонки стоят. "
        "Робот продолжает попытки."
    )
