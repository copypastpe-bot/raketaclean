"""Машина попыток дозвона: чистые правила переходов, дословно от владельца.

Здесь закодированы решения владельца 2026-08-31
(docs/plans/2026-08-31-site-lead-autocall-design.md):

- №3: менеджер не взял → повтор через 5 минут; после второй неудачи —
  сообщение и остановка по сделке.
- №4: клиент не взял → сообщение «повтор через 10 минут» + повтор; после
  второй неудачи — сделка на этап «Не было первого контакта».
- §4.5: предохранитель — не больше 4 попыток на сделку суммарно, что бы
  ни происходило; после лимита — сообщение и остановка.

Модуль чистый: ни сети, ни БД, ни now() внутри — момент времени приходит
аргументом. Поэтому каждая ветка проверяется обычным тестом без заглушек,
а движок (Задача 8) лишь исполняет возвращённые эффекты. Окно звонков
10:00–20:00 здесь НЕ учитывается: Retry.at — «желаемый момент», прогон
через окно делает движок функцией next_call_moment.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Union

MAX_ATTEMPTS = 4                       # предохранитель §4.5 дизайна
RETRY_MANAGER = timedelta(minutes=5)   # решение владельца №3
RETRY_CLIENT = timedelta(minutes=10)   # решение владельца №4

#: Фиксированный набор сообщений менеджеру; движок превращает kind в текст.
NOTIFY_KINDS: tuple[str, ...] = (
    "client_retry_10",       # «Попытка звонка не удалась, повтор через 10 минут»
    "no_contact_final",      # вторая неудача клиента: сделка ушла на этап «Не было первого контакта»
    "manager_unreachable",   # вторая неудача менеджера: робот остановился
    "attempts_exhausted",    # предохранитель §4.5: попытки израсходованы
)

# --- Словарь статусов цепочки: chain.py — единственный владелец ---
# store.py и движок импортируют константы отсюда, чтобы два списка
# статусов не разъехались молча.

STATUS_QUEUED = "queued"          # ждёт своего момента (next_action_at)
STATUS_CALLING = "calling"        # команда АТС отдана, попытка идёт
STATUS_DONE = "done"              # соединились — готово
STATUS_NO_CONTACT = "no_contact"  # сделка ушла на этап «Не было первого контакта»
STATUS_GAVE_UP = "gave_up"        # робот остановился (решение №3 или §4.5)
#: Попытка упала технически (АТС/сеть), исхода нет — движок повторит сам.
#: Машина переходов этот статус не обрабатывает, и в FINAL_STATUSES он
#: не входит: цепочка ещё жива, наблюдатель должен её довести.
STATUS_ERROR = "error"

#: Статусы, после которых цепочка закрыта и попыток больше не будет.
FINAL_STATUSES: tuple[str, ...] = (STATUS_DONE, STATUS_NO_CONTACT, STATUS_GAVE_UP)

#: Статусы, по которым цепочка ещё не закончена: их наблюдатель добирает
#: из базы. `error` тоже здесь — после сбоя цепочку нужно довести.
ACTIVE_STATUSES: tuple[str, ...] = (STATUS_QUEUED, STATUS_CALLING, STATUS_ERROR)


class Outcome(str, Enum):
    """Чем закончилась попытка звонка (определяет движок по истории АТС)."""

    CONNECTED = "connected"            # менеджер и клиент поговорили
    MANAGER_NO_ANSWER = "manager_no_answer"
    CLIENT_NO_ANSWER = "client_no_answer"
    UNKNOWN = "unknown"                # история АТС не нашлась — считаем как менеджера


@dataclass(frozen=True)
class Chain:
    """Чистая модель цепочки для машины переходов (не строка БД).

    Конверсию из/в AutocallLead делает движок (Задача 8): машина знает
    только то, что нужно правилам владельца.
    """

    lead_id: int
    status: str                        # словарь STATUS_* выше; машина переходов
                                       # работает с queued|calling, `error` чинит движок
    attempts_total: int
    manager_failures: int
    client_failures: int
    next_action_at: Optional[datetime]


# --- Эффекты: что движок должен сделать после перехода ---

@dataclass(frozen=True)
class Retry:
    """Назначить повтор. `at` — желаемый момент; окно 10:00–20:00 применит движок."""

    at: datetime


@dataclass(frozen=True)
class NotifyManager:
    """Отправить менеджеру сообщение фиксированного вида (см. NOTIFY_KINDS)."""

    kind: str

    def __post_init__(self) -> None:
        if self.kind not in NOTIFY_KINDS:
            raise ValueError(
                f"неизвестный вид сообщения менеджеру: {self.kind!r}; "
                f"допустимы {NOTIFY_KINDS}"
            )


@dataclass(frozen=True)
class MoveLeadNoContact:
    """Перенести сделку на этап «Не было первого контакта» (решение №4)."""


@dataclass(frozen=True)
class Done:
    """Соединились — цепочка закрыта, больше ничего делать не нужно."""


@dataclass(frozen=True)
class GaveUp:
    """Робот остановился по этой сделке; reason — почему (для лога и отчёта)."""

    reason: str


Effect = Union[Retry, NotifyManager, MoveLeadNoContact, Done, GaveUp]


def advance(chain: Chain, outcome: Outcome, now: datetime) -> tuple[Chain, list[Effect]]:
    """Применить исход завершившейся попытки и вернуть новую цепочку + эффекты.

    Контракт с движком (Задача 8): счётчик attempts_total инкрементирует
    ДВИЖОК при команде АТС, до advance. Сюда приходит цепочка, в которой
    только что завершившаяся попытка уже посчитана; машина attempts_total
    не трогает.

    Правила — дословные решения владельца:

    - CONNECTED → статус done, эффект Done().
    - MANAGER_NO_ANSWER или UNKNOWN → manager_failures+1; вторая неудача
      менеджера → сообщение manager_unreachable и остановка (решение №3);
      иначе повтор через 5 минут.
    - CLIENT_NO_ANSWER → client_failures+1; первая → сообщение client_retry_10
      и повтор через 10 минут (решение №4); вторая → перенос сделки на этап
      «Не было первого контакта» + сообщение no_contact_final.
    - Перед ЛЮБЫМ повтором — предохранитель §4.5: попытки израсходованы
      (attempts_total >= MAX_ATTEMPTS) → вместо повтора остановка с
      сообщением attempts_exhausted. Обещание «повтор через 10 минут» при
      этом не отправляется — оно стало бы ложью.

    Цепочка в финальном статусе — ошибка вызывающего: движок не должен
    звать машину по закрытой цепочке.
    """
    if chain.status in FINAL_STATUSES:
        raise ValueError(
            f"цепочка сделки {chain.lead_id} уже закрыта ({chain.status}): "
            "advance по финальному статусу — ошибка движка"
        )

    if outcome is Outcome.CONNECTED:
        return replace(chain, status=STATUS_DONE, next_action_at=None), [Done()]

    if outcome in (Outcome.MANAGER_NO_ANSWER, Outcome.UNKNOWN):
        updated = replace(chain, manager_failures=chain.manager_failures + 1)
        if updated.manager_failures >= 2:
            # Решение №3: после второй неудачи менеджера — сообщение и стоп.
            return _gave_up(updated, "manager_unreachable")
        return _retry(updated, now + RETRY_MANAGER)

    if outcome is Outcome.CLIENT_NO_ANSWER:
        failures = chain.client_failures + 1
        if failures >= 2:
            # Решение №4: после второй неудачи клиента — этап «Не было
            # первого контакта»; повтора нет, поэтому лимит здесь ни при чём.
            return (
                replace(chain, status=STATUS_NO_CONTACT,
                        client_failures=failures, next_action_at=None),
                [MoveLeadNoContact(), NotifyManager("no_contact_final")],
            )
        # При исчерпанном лимите _retry отбросит и client_retry_10:
        # обещание «повтор через 10 минут» стало бы ложью.
        return _retry(replace(chain, client_failures=failures),
                      now + RETRY_CLIENT,
                      before=[NotifyManager("client_retry_10")])

    raise ValueError(f"неизвестный исход попытки: {outcome!r}")


def _retry(chain: Chain, at: datetime,
           before: Optional[list[Effect]] = None) -> tuple[Chain, list[Effect]]:
    """Назначить повтор, если предохранитель §4.5 ещё позволяет попытки."""
    if chain.attempts_total >= MAX_ATTEMPTS:
        return _gave_up(chain, "attempts_exhausted")
    return (
        replace(chain, status=STATUS_QUEUED, next_action_at=at),
        [*(before or []), Retry(at=at)],
    )


def _gave_up(chain: Chain, kind: str) -> tuple[Chain, list[Effect]]:
    """Единый финал остановки: kind уведомления == причина GaveUp — по устройству.

    Так сообщение менеджеру и причина в логе не могут разъехаться, какой бы
    веткой (решение №3 или предохранитель §4.5) остановка ни случилась.
    """
    return (
        replace(chain, status=STATUS_GAVE_UP, next_action_at=None),
        [NotifyManager(kind), GaveUp(kind)],
    )
