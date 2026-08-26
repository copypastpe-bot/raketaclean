"""Какой сделке amoCRM соответствует строка отчёта партнёра.

Чистая функция без сети и базы — как матчер уборки, и по той же причине:
правила должны проверяться на истории, а не только в бою.

Чем ковры отличаются от уборки. Заказ диктуется партнёру голосом, поэтому
единственная надёжная связка — телефон клиента. Даты помогают слабее: в отчёте
есть «Добавление» (когда партнёр завёл заказ у себя), в сделке — когда её
создали, и расходятся они на день-три. Поэтому окно шире, чем в уборке.

Порядок правил (от надёжного к слабому):

1. открытая сделка ковровой воронки — самый частый случай: сейлзбот завёл её,
   когда оператор передал заказ партнёру, и она ждёт результата;
2. таких несколько — выбираем по дате, не вышло — спрашиваем владельца;
3. ковровая сделка уже проведена примерно в день сдачи — владелец успел сам;
4. ковровой нет, но есть свежий лид в первичной — ведём цепочку с него;
5. ничего свежего — заводим цепочку с нуля.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Collection, Optional, Sequence

from adminbot.amo import ids

# Насколько дата сделки может расходиться с «Добавлением» в отчёте партнёра.
# Партнёр заводит заказ у себя в тот же день или на день-три позже звонка,
# плюс сам заказ живёт 1–2 недели (замер по 51 заказу разведки).
CREATED_WINDOW_DAYS = 7

# Проведённая ковровая сделка считается «этой», если её дата рядом со сдачей ковров.
DELIVERED_WINDOW_DAYS = 5

# Этапы ковровой воронки, на которых сделка ещё в работе.
CARPET_OPEN_STAGES = frozenset((
    ids.CARPET_STAGE_UNSORTED,
    ids.CARPET_STAGE_HANDED_OVER,
    ids.CARPET_STAGE_IN_WORK,
))


@dataclass(frozen=True)
class CarpetLead:
    """Сделка-кандидат в том виде, в каком её видит матчер."""

    lead_id: int
    pipeline_id: int
    status_id: int
    created_date: Optional[date] = None
    order_date: Optional[date] = None
    price: Optional[Decimal] = None
    name: Optional[str] = None

    @property
    def is_carpet(self) -> bool:
        return self.pipeline_id == ids.PIPELINE_CARPETS

    @property
    def is_open_carpet(self) -> bool:
        return self.is_carpet and self.status_id in CARPET_OPEN_STAGES

    @property
    def is_delivered_carpet(self) -> bool:
        return self.is_carpet and self.status_id == ids.CARPET_STAGE_DELIVERED

    @property
    def is_open_primary(self) -> bool:
        return (self.pipeline_id == ids.PIPELINE_PRIMARY
                and self.status_id not in ids.STATUSES_FINAL)


@dataclass(frozen=True)
class CarpetDecision:
    """Решение по строке отчёта.

    kind:
      use_carpet   — довести открытую ковровую сделку
      use_primary  — провести лид первичной, дальше сделку создаст сейлзбот
      already_done — владелец провёл сам: только привязать
      create_new   — сделки нет, заводим цепочку с нуля
      ask_owner    — кандидатов несколько, правила не решили
    """

    kind: str
    lead_id: Optional[int] = None
    options: tuple[int, ...] = field(default=())


def match_carpet(row, candidates: Sequence[CarpetLead],
                 taken_lead_ids: Collection[int] = ()) -> CarpetDecision:
    """Решить, что делать со строкой отчёта партнёра."""
    taken = set(taken_lead_ids)
    # Рудиментарную ковровую воронку и занятые сделки в расчёт не берём.
    leads = [lead for lead in candidates
             if lead.pipeline_id not in ids.PIPELINES_IGNORED_CARPETS
             and lead.lead_id not in taken]

    # 1-2. Открытые ковровые сделки.
    open_carpet = [lead for lead in leads if lead.is_open_carpet]
    fresh = _fresh(open_carpet, row.added_date)
    if len(fresh) == 1:
        return CarpetDecision(kind="use_carpet", lead_id=fresh[0].lead_id)
    if len(fresh) > 1:
        return _ask(fresh)
    if len(open_carpet) == 1:
        # Дат нет вовсе — сделка всё равно единственная открытая.
        return CarpetDecision(kind="use_carpet", lead_id=open_carpet[0].lead_id)
    if len(open_carpet) > 1:
        return _ask(open_carpet)

    # 3. Проведённая ковровая сделка рядом с датой сдачи — владелец успел сам.
    delivered = [lead for lead in leads if lead.is_delivered_carpet
                 and _near(lead.order_date or lead.created_date,
                           row.return_date, DELIVERED_WINDOW_DAYS)]
    if len(delivered) == 1:
        return CarpetDecision(kind="already_done", lead_id=delivered[0].lead_id)
    if len(delivered) > 1:
        return _ask(delivered)

    # 4. Ковровой сделки нет, но лид в первичной свежий: сейлзбот создаст ковровую,
    #    когда робот переведёт лид в «Передано в работу».
    primary = _fresh([lead for lead in leads if lead.is_open_primary], row.added_date)
    if len(primary) == 1:
        return CarpetDecision(kind="use_primary", lead_id=primary[0].lead_id)
    if len(primary) > 1:
        return _ask(primary)

    # 5. Ничего подходящего.
    return CarpetDecision(kind="create_new")


def _fresh(leads: Sequence[CarpetLead], added: Optional[date]) -> list[CarpetLead]:
    """Сделки, заведённые примерно тогда же, когда партнёр принял заказ."""
    if added is None:
        return list(leads)
    return [lead for lead in leads
            if _near(lead.created_date, added, CREATED_WINDOW_DAYS)
            or _near(lead.order_date, added, CREATED_WINDOW_DAYS)]


def _near(value: Optional[date], target: Optional[date], window: int) -> bool:
    if value is None or target is None:
        return False
    return abs((value - target).days) <= window


def _ask(leads: Sequence[CarpetLead]) -> CarpetDecision:
    return CarpetDecision(kind="ask_owner",
                          options=tuple(sorted(lead.lead_id for lead in leads)))
