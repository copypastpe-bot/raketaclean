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


@dataclass(frozen=True)
class LeadInfo:
    """Сделка-кандидат в том виде, в каком её видит матчер."""

    lead_id: int
    pipeline_id: int
    status_id: int
    order_date: Optional[date] = None    # поле «Дата и время заказа», может быть пустым/протухшим
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


def _within_window(order_date: date, lead: LeadInfo) -> bool:
    if lead.order_date is None:
        return False
    return abs((lead.order_date - order_date).days) <= DATE_WINDOW_DAYS


def _pick_by_date(order_date: date, leads: list[LeadInfo]) -> Optional[LeadInfo]:
    """Одна сделка, чья дата укладывается в окно. None — если таких не ровно одна."""
    near = [lead for lead in leads if _within_window(order_date, lead)]
    return near[0] if len(near) == 1 else None


def _ask(leads: Iterable[LeadInfo]) -> Decision:
    return Decision(kind="ask_owner", options=tuple(sorted(lead.lead_id for lead in leads)))


def _resolve(order_date: date, leads: list[LeadInfo], kind: str) -> Optional[Decision]:
    """Общая развилка: одна сделка → берём; несколько → пробуем дату; иначе вопрос."""
    if not leads:
        return None
    if len(leads) == 1:
        return Decision(kind=kind, lead_id=leads[0].lead_id)
    chosen = _pick_by_date(order_date, leads)
    if chosen is not None:
        return Decision(kind=kind, lead_id=chosen.lead_id)
    return _ask(leads)


def match(*, order_date: date, candidates: Iterable[LeadInfo]) -> Decision:
    """Решить, что делать с заказом бота, по списку сделок его телефона."""

    # 1. Ковровые и архивные воронки — не наш случай. Заказ, заведённый в боте,
    #    ковровым быть не может: ковры приходят только через Excel партнёра.
    leads = [lead for lead in candidates if lead.pipeline_id not in ids.PIPELINES_IGNORED]

    realization = [lead for lead in leads if lead.pipeline_id == ids.PIPELINE_REALIZATION]
    primary = [lead for lead in leads if lead.pipeline_id == ids.PIPELINE_PRIMARY]

    # 2. Приоритет — открытые сделки воронки реализации (путь А).
    decision = _resolve(order_date, [lead for lead in realization if lead.is_open], "use_realization")
    if decision is not None:
        return decision

    # 3. Открытых нет, но есть проведённая сделка с подходящей датой —
    #    владелец успел провести заказ руками. Привязываем, ничего не меняем (дизайн §5.1).
    done_near = [lead for lead in realization if lead.is_success and _within_window(order_date, lead)]
    if done_near:
        # Несколько проведённых с датой в окне — редкость; берём ближайшую по дате,
        # при равенстве — меньший id, чтобы решение было воспроизводимым.
        # Риска нет: в этой ветке робот в амо ничего не пишет.
        best = min(done_near, key=lambda lead: (abs((lead.order_date - order_date).days), lead.lead_id))
        return Decision(kind="already_done", lead_id=best.lead_id)

    # 4. В реализации пусто — работаем с открытым лидом первичной воронки (путь Б).
    decision = _resolve(order_date, [lead for lead in primary if lead.is_open], "use_primary")
    if decision is not None:
        return decision

    # 5. Ничего подходящего — заводим сделку с нуля (путь В, ~1 раз в неделю).
    return Decision(kind="create_new")
