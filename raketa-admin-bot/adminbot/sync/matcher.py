"""Матчер («сваха»): какой сделке amoCRM соответствует заказ бота.

Чистая функция без сети и без БД — поэтому её можно прогнать по всей истории
заказов за квартал («экзамен на истории», задача 6 плана).

Правила выведены из разведки (`tgbot-v1/recon/06-matching-metrics.md`) и дизайна §4.
Ключевой факт: на один заказ в амо существуют ДВЕ сделки — успешная в первичной
воронке и отдельная в воронке реализации, созданная сейлзботом. Поэтому воронка
реализации всегда имеет приоритет, а «две сделки по телефону» — это не спор.

Принцип: лучше спросить владельца, чем уверенно ошибиться.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from adminbot.amo import ids

# Окно сверки дат: «Дата и время заказа» в сделке и дата заказа в боте могут
# разойтись на день-два (перенос, ночное закрытие заказа). Метрика M5: ±2 дня
# разрешает 19% случаев с несколькими кандидатами.
DATE_WINDOW_DAYS = 2

# Запасной признак для проведённых сделок: когда сделку реально закрыли.
# Поле «Дата и время заказа» заполняют не всегда и не всегда верно (заказ №421:
# в сделке стояло 14.06 при заказе 01.06), а момент закрытия есть у каждой сделки
# и почти всегда совпадает с датой заказа плюс день-два.
CLOSED_WINDOW_DAYS = 3


@dataclass(frozen=True)
class LeadInfo:
    """Сделка-кандидат в том виде, в каком её видит матчер."""

    lead_id: int
    pipeline_id: int
    status_id: int
    order_date: Optional[date] = None    # поле «Дата и время заказа», может быть пустым/протухшим
    closed_date: Optional[date] = None   # когда сделку закрыли — запасной признак
    name: Optional[str] = None           # для карточки-вопроса владельцу

    @property
    def is_open(self) -> bool:
        """Открытая = не проведена и не закрыта, т.е. с ней ещё предстоит работа."""
        return self.status_id not in ids.STATUSES_FINAL

    @property
    def is_success(self) -> bool:
        return self.status_id == ids.STATUS_SUCCESS


@dataclass(frozen=True)
class Decision:
    """Решение матчера по одному заказу.

    kind:
      use_realization — есть автосделка в воронке реализации (путь А)
      use_primary     — есть только лид в первичной воронке (путь Б)
      create_new      — сделки нет вовсе, заводим с нуля (путь В)
      ask_owner       — кандидатов несколько, правила не решили (путь Г)
      already_done    — заказ уже проведён руками: только привязать, не трогать
    """

    kind: str
    lead_id: Optional[int] = None
    options: tuple[int, ...] = field(default=())


def _order_date_gap(order_date: date, lead: LeadInfo) -> Optional[int]:
    """На сколько дней «Дата и время заказа» сделки расходится с датой заказа."""
    if lead.order_date is None:
        return None
    return abs((lead.order_date - order_date).days)


def _closed_gap(order_date: date, lead: LeadInfo) -> Optional[int]:
    """На сколько дней момент закрытия сделки расходится с датой заказа."""
    if lead.closed_date is None:
        return None
    return abs((lead.closed_date - order_date).days)


def _in_order_date_window(order_date: date, lead: LeadInfo) -> bool:
    gap = _order_date_gap(order_date, lead)
    return gap is not None and gap <= DATE_WINDOW_DAYS


def _ask(leads: Iterable[LeadInfo]) -> Decision:
    return Decision(kind="ask_owner", options=tuple(sorted(lead.lead_id for lead in leads)))


def _pick_open(order_date: date, open_leads: list[LeadInfo], kind: str) -> Optional[Decision]:
    """Выбор среди открытых сделок: сначала по дате, потом по единственности."""
    if not open_leads:
        return None

    dated = [lead for lead in open_leads if _in_order_date_window(order_date, lead)]
    if len(dated) == 1:
        return Decision(kind=kind, lead_id=dated[0].lead_id)
    if len(dated) > 1:
        return _ask(dated)

    # Дата не помогла (пустая или протухшая). Если открытая сделка одна — она и есть.
    if len(open_leads) == 1:
        return Decision(kind=kind, lead_id=open_leads[0].lead_id)
    return None          # несколько открытых без дат — решаем дальше по цепочке


def _pick_completed(order_date: date, realization: list[LeadInfo]) -> Optional[Decision]:
    """Проведённая сделка по этому заказу — владелец успел раньше робота (дизайн §5.1).

    Сначала по «Дате и времени заказа», затем по моменту закрытия сделки: поле даты
    заполняют не всегда и не всегда верно, а закрытие фиксируется само.
    """
    completed = [lead for lead in realization if lead.is_success]
    if not completed:
        return None

    by_order_date = [(gap, lead.lead_id) for lead in completed
                     if (gap := _order_date_gap(order_date, lead)) is not None and gap <= DATE_WINDOW_DAYS]
    if by_order_date:
        return Decision(kind="already_done", lead_id=min(by_order_date)[1])

    by_closed = [(gap, lead.lead_id) for lead in completed
                 if (gap := _closed_gap(order_date, lead)) is not None and gap <= CLOSED_WINDOW_DAYS]
    if by_closed:
        return Decision(kind="already_done", lead_id=min(by_closed)[1])

    return None


def match(*, order_date: date, candidates: Iterable[LeadInfo]) -> Decision:
    """Решить, что делать с заказом бота, по списку сделок его телефона."""

    # 1. Ковровые и архивные воронки — не наш случай. Заказ, заведённый в боте,
    #    ковровым быть не может: ковры приходят только через Excel партнёра.
    leads = [lead for lead in candidates if lead.pipeline_id not in ids.PIPELINES_IGNORED]

    realization = [lead for lead in leads if lead.pipeline_id == ids.PIPELINE_REALIZATION]
    primary = [lead for lead in leads if lead.pipeline_id == ids.PIPELINE_PRIMARY]
    open_realization = [lead for lead in realization if lead.is_open]

    # 2. Открытая сделка реализации на дату заказа — самый частый случай (путь А).
    dated_open = [lead for lead in open_realization if _in_order_date_window(order_date, lead)]
    if len(dated_open) == 1:
        return Decision(kind="use_realization", lead_id=dated_open[0].lead_id)
    if len(dated_open) > 1:
        return _ask(dated_open)          # две открытые на одну дату — вопрос владельцу

    # 3. Открытой сделки на дату нет. Проверяем, не проведён ли заказ руками.
    #    Важно делать это ДО разбора открытых без дат: у постоянных клиентов годами
    #    висят забытые открытые сделки, и раньше они перехватывали решение (заказ №508).
    decision = _pick_completed(order_date, realization)
    if decision is not None:
        return decision

    # 4. Проведённой нет. Единственная открытая сделка реализации — берём её,
    #    даже если дата в ней пустая или протухшая.
    decision = _pick_open(order_date, open_realization, "use_realization")
    if decision is not None:
        return decision
    if open_realization:
        return _ask(open_realization)    # несколько открытых, дата не решает

    # 5. В реализации пусто — работаем с открытым лидом первичной воронки (путь Б).
    open_primary = [lead for lead in primary if lead.is_open]
    decision = _pick_open(order_date, open_primary, "use_primary")
    if decision is not None:
        return decision
    if open_primary:
        return _ask(open_primary)

    # 6. Ничего подходящего — заводим сделку с нуля (путь В, ~1 раз в неделю).
    return Decision(kind="create_new")
