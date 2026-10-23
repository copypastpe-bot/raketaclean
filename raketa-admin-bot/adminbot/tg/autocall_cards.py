"""Тексты сообщений автозвонка: менеджеру и владельцу.

Тексты менеджеру утверждены владельцем 2026-09-01 (контрольная точка
Задачи 12, docs/plans/2026-08-31-autocall-implementation.md, шаг 4). Здесь
только строители текста: вход — данные, выход — строка. Ни сети, ни Telegram,
ни фабрик-отправителей — их собирает main.py в Задаче 11.

Полный телефон клиента — в сообщениях менеджеру и владельцу целиком: бот
личный, а владельцу (и менеджеру) номер нужен, чтобы дозвониться самим, не
открывая CRM (решение владельца 2026-08-26). Маска — только в логах
(`adminbot.phone.mask`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from adminbot.amo.fields import MOSCOW_TZ
from adminbot.autocall.chain import (
    CANCEL_CLIENT_CALLED,
    CANCEL_MANAGER_CALLED,
    CARD_KINDS,
    CARD_RETRY_CLIENT,
    CARD_RETRY_MANAGER,
    NOTIFY_KINDS,
    STATUS_DONE,
)
from adminbot.phone import for_owner

#: Кнопка «Я уже перезвонил» (ТЗ 2026-10-23). Сообщение уходит ключом рабочего
#: бота, поэтому нажатие получает он: префикс тот же, что
#: `AUTOCALL_CALLED_CB_PREFIX` в корневом bot.py, — дословное значение
#: закреплено тестом в обеих кодовых базах.
AUTOCALL_CALLED_CB_PREFIX = "autocall_called:"
CALLED_BUTTON_TEXT = "Я уже перезвонил"

# Тексты по kind — дословные черновики от владельца (см. docstring модуля).
_MANAGER_TEXTS: dict[str, str] = {
    "client_retry_10":
        "Попытка звонка не удалась, повтор через 10 минут.",
    "no_contact_final":
        "Клиент дважды не ответил. Сделка перенесена в «Не было 1-го "
        "касания» — дальше вручную.",
    "manager_unreachable":
        "Не дозвонились до менеджера по заявке. Робот больше не "
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


# --- сообщение менеджеру с кнопкой «Я уже перезвонил» (ТЗ 2026-10-23) ---

#: Первая строка, пока впереди повтор; дальше — «Повтор в 10:12.».
_RETRY_HEADS: dict[str, str] = {
    CARD_RETRY_MANAGER: "Робот не дозвонился до вас по заявке.",
    CARD_RETRY_CLIENT: "Клиент не взял трубку.",
}


def called_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    """Одна кнопка «Я уже перезвонил» с номером сделки."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=CALLED_BUTTON_TEXT, callback_data=f"{AUTOCALL_CALLED_CB_PREFIX}{lead_id}",
    )]])


def _hhmm(moment: datetime) -> str:
    return moment.astimezone(MOSCOW_TZ).strftime("%H:%M")


def _retry_phrase(retry_at: datetime, now: Optional[datetime]) -> str:
    """«Повтор в 10:12.»; окно звонков унесло повтор на другой день — с датой."""
    local = retry_at.astimezone(MOSCOW_TZ)
    if now is not None and local.date() != now.astimezone(MOSCOW_TZ).date():
        return f"Повтор {local:%d.%m} в {local:%H:%M}."
    return f"Повтор в {local:%H:%M}."


def _need(moment: Optional[datetime], kind: str, name: str) -> datetime:
    if moment is None:
        raise ValueError(f"сообщение менеджеру {kind!r} без времени ({name})")
    return moment


def manager_card_text(kind: str, lead_id: int, *, base_url: str,
                      phone10: Optional[str] = None,
                      retry_at: Optional[datetime] = None,
                      at: Optional[datetime] = None,
                      now: Optional[datetime] = None) -> str:
    """Текст сообщения менеджеру с кнопкой — и того же сообщения, правленного на итог.

    Виды — chain.CARD_KINDS. Пока впереди повтор (retry_*): время повтора по
    Москве, `retry_at` — уже после окна звонков, `now` — чтобы назвать дату,
    если повтор не сегодня. Итог: done и client_called — с моментом `at`
    (соединил / звонок через АТС), manager_called — без времени, остальные —
    нынешние тексты `manager_text`. Дальше, как и сейчас, телефон и ссылка.

    Незнакомый вид или вид без нужного времени — ValueError: пустое обещание
    «повтор в …» хуже явной ошибки.
    """
    if kind in _RETRY_HEADS:
        moment = _need(retry_at, kind, "retry_at")
        head = f"{_RETRY_HEADS[kind]} {_retry_phrase(moment, now)}"
    elif kind == STATUS_DONE:
        head = f"Робот соединил вас с клиентом в {_hhmm(_need(at, kind, 'at'))}."
    elif kind == CANCEL_MANAGER_CALLED:
        head = "Вы перезвонили клиенту сами — робот остановился."
    elif kind == CANCEL_CLIENT_CALLED:
        head = (f"С клиентом уже был звонок через АТС в {_hhmm(_need(at, kind, 'at'))} "
                "— робот остановился.")
    elif kind in CARD_KINDS:
        return manager_text(kind, lead_id, base_url=base_url, phone10=phone10)
    else:
        raise ValueError(
            f"неизвестный вид сообщения менеджеру с кнопкой: {kind!r}; "
            f"допустимы {CARD_KINDS}"
        )

    lines = [head]
    if phone10:
        lines.append(f"Телефон клиента: {for_owner(phone10)}")
    lines.append(_deal_url(base_url, lead_id))
    return "\n".join(lines)


def rehearsal_text(lead_id: int, phone10: Optional[str], *, base_url: str) -> str:
    """Владельцу в репетиции: что робот сделал бы, но не сделал.

    В репетиции робот не звонит и не пишет в амо — молчать об этом решении
    нельзя, иначе прогон покажет владельцу пустоту вместо того, что нужно
    проверить (тот же принцип, что и у rehearsal_text календаря).

    phone10 бывает None (запись пропала из хранилища или телефон не
    сохранился) — тогда строку телефона просто пропускаем, как в
    manager_text, а не печатаем «телефон не распознан» на ровном месте.
    """
    lines = ["Репетиция: позвонил бы сейчас менеджеру по заявке."]
    if phone10:
        lines.append(f"Телефон клиента: {for_owner(phone10)}")
    lines.append(_deal_url(base_url, lead_id))
    return "\n".join(lines)


def connected_text(lead_id: int, phone10: Optional[str], *, base_url: str) -> str:
    """Владельцу при соединении менеджера с клиентом — неделя наблюдения.

    Одно сообщение на одну доведённую до конца цепочку: владелец проверяет
    по горячим следам, промежуточные шаги ему не нужны.

    phone10 — тот же случай, что и в rehearsal_text: None пропускает строку
    телефона целиком, а не подставляет заглушку «телефон не распознан».
    """
    lines = ["Автозвонок: соединил менеджера с клиентом по заявке."]
    if phone10:
        lines.append(f"Телефон клиента: {for_owner(phone10)}")
    lines.append(_deal_url(base_url, lead_id))
    return "\n".join(lines)


def no_phone_text(lead_id: int, *, base_url: str) -> str:
    """Владельцу — заявка (с сайта или отклик на промо) пришла без телефона, звонить некому.

    Наблюдатель уже закрыл цепочку финалом "gave_up" (звонить нечем), но
    молчать об этом нельзя: заявка была, и владельцу нужно посмотреть сделку
    самому — возможно, номер потерялся при вводе на сайте.
    """
    return "\n".join([
        "Заявка без телефона — робот звонить не может, посмотрите сделку.",
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
