"""Матчер («сваха»): какой сделке amoCRM соответствует заказ бота.

Чистая функция без сети и без БД — поэтому её можно прогнать по всей истории
заказов («экзамен на истории») и проверить решения против фактов.

Что робот знает о жизни заказа (дизайн §4 + уточнения владельца 2026-08-25):

1. Заказ уже проведён руками до конца — только привязать, ничего не делать.
2. Лид доведён до «Передано в работу», сейлзбот создал сделку в воронке
   реализации — она открыта на «Заказ оформлен» или «Заказ подтвержден».
   Её и надо довести до «ЗАКАЗ ВЫПОЛНЕН и Оплата получена».
3. Сделки нет вовсе — создать в первичной воронке и провести по всей цепочке.
4. Свежих сделок нет, но висят старые хвосты — спросить владельца
   («заводи новую» / «сам разберусь»). Циклы сделок короткие: за 90 дней
   ни одна верная сделка не была старше 22 дней (измерено 2026-08-25).

Ковровая воронка — параллельный процесс с партнёром «Кристалл». Ковровые сделки
никогда не относятся к заказу из бота: они не кандидаты и не помеха.

Принцип: лучше спросить владельца, чем уверенно ошибиться.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional, Sequence

from adminbot.amo import ids

# Окно сверки дат: «Дата и время заказа» в сделке и дата заказа в боте могут
# разойтись на день-два (перенос, ночное закрытие заказа). Метрика M5: ±2 дня
# разрешает 19% случаев с несколькими кандидатами.
DATE_WINDOW_DAYS = 2

# Запасной признак для проведённых сделок: когда сделку реально закрыли.
# Поле «Дата и время заказа» заполняют не всегда и не всегда верно (заказ №421:
# в сделке стояло 14.06 при заказе 01.06), а момент закрытия есть у каждой сделки.
CLOSED_WINDOW_DAYS = 3

# Живая сделка заводится незадолго до выполнения заказа. Измерено по 142 заказам
# за 90 дней: медиана 1 день, 95% укладываются в 9 дней, максимум 22 дня.
# Всё, что старше 30 дней, — недобитый хвост, а не наш заказ.
STALE_LEAD_DAYS = 30

# Сделку могут завести и ПОСЛЕ выполнения заказа: владелец разбирает CRM пачками
# (25 сделок из 142 за квартал). Небольшой запас вперёд обязателен.
FUTURE_CREATION_DAYS = 30


@dataclass(frozen=True)
class LeadInfo:
    """Сделка-кандидат в том виде, в каком её видит матчер."""

    lead_id: int
    pipeline_id: int
    status_id: int
    order_date: Optional[date] = None      # поле «Дата и время заказа»: бывает пустым и неверным
    closed_date: Optional[date] = None     # когда сделку закрыли
    created_date: Optional[date] = None    # когда сделку завели
    specialist_ids: tuple[int, ...] = ()   # поле «Специалист»: enum-значения мастеров
    name: Optional[str] = None             # для карточки-вопроса владельцу

    @property
    def is_open(self) -> bool:
        """Открытая = не проведена и не закрыта, т.е. с ней ещё предстоит работа."""
        return self.status_id not in ids.STATUSES_FINAL

    @property
    def is_success(self) -> bool:
        return self.status_id == ids.STATUS_SUCCESS

    @property
    def is_unsorted(self) -> bool:
        """«Неразобранное» — сырой след обращения, до него очередь доходит последней."""
        return self.status_id in ids.STATUSES_UNSORTED


@dataclass(frozen=True)
class Decision:
    """Решение матчера по одному заказу.

    kind:
      use_realization  — есть открытая сделка в воронке реализации (путь А)
      use_primary      — есть лид в первичной воронке, ведём цепочку с него (путь Б)
      create_new       — сделки нет вовсе, заводим с нуля (путь В)
      ask_owner        — кандидатов несколько, правила не решили (путь Г)
      ask_owner_stale  — свежих сделок нет, есть старые хвосты: «заводи новую» / «сам разберусь»
      already_done     — заказ уже проведён руками: только привязать, не трогать
    """

    kind: str
    lead_id: Optional[int] = None
    options: tuple[int, ...] = field(default=())
    duplicates: tuple[int, ...] = field(default=())   # лидам-дублям робот пишет комментарий


def _gap(order_date: date, value: Optional[date]) -> Optional[int]:
    return None if value is None else abs((value - order_date).days)


def _in_order_date_window(order_date: date, lead: LeadInfo) -> bool:
    gap = _gap(order_date, lead.order_date)
    return gap is not None and gap <= DATE_WINDOW_DAYS


def _is_fresh(order_date: date, lead: LeadInfo) -> bool:
    """Сделка заведена рядом с заказом, а не осталась хвостом с прошлых времён."""
    if lead.created_date is None:
        return True                        # даты создания нет — не наказываем
    age = (order_date - lead.created_date).days
    return -FUTURE_CREATION_DAYS <= age <= STALE_LEAD_DAYS


def _ask(leads: Iterable[LeadInfo], kind: str = "ask_owner") -> Decision:
    return Decision(kind=kind, options=tuple(sorted(lead.lead_id for lead in leads)))


def _narrow_by_specialist(leads: list[LeadInfo], master_ids: Sequence[int]) -> list[LeadInfo]:
    """Отбросить сделки чужих мастеров и оставить сделки нашего.

    Так различаются уборка и химчистка одному клиенту в один день (заказ №575)
    и отсеиваются недобитые сделки другого мастера (заказ №548).

    Порядок предпочтения:
      1. «Специалист» — наш мастер: сделка наша, остальные не нужны;
      2. «Специалист» пуст (поле заполняют не всегда): сведений нет, годится;
      3. «Специалист» — другой мастер: сделка ЧУЖАЯ, довод против неё.

    Если мастер заказа неизвестен амо, признак не применяется вовсе.
    Пустой список на выходе означает «подходящих сделок нет».
    """
    if not master_ids:
        return leads
    wanted = set(master_ids)
    ours = [lead for lead in leads if wanted & set(lead.specialist_ids)]
    if ours:
        return ours
    return [lead for lead in leads if not lead.specialist_ids]


def _choose(order_date: date, leads: list[LeadInfo], kind: str,
            master_ids: Sequence[int], duplicates: tuple[int, ...] = ()) -> Optional[Decision]:
    """Выбрать одну сделку из группы: мастер → дата заказа → единственность."""
    if not leads:
        return None

    narrowed = _narrow_by_specialist(leads, master_ids)
    if not narrowed:
        return None                        # все кандидаты — сделки чужих мастеров
    if len(narrowed) == 1:
        return Decision(kind=kind, lead_id=narrowed[0].lead_id, duplicates=duplicates)

    dated = [lead for lead in narrowed if _in_order_date_window(order_date, lead)]
    if len(dated) == 1:
        return Decision(kind=kind, lead_id=dated[0].lead_id, duplicates=duplicates)
    if len(dated) > 1:
        return _ask(dated)                 # дата не различает — вопрос владельцу

    # Дата не помогла (пустая или неверная). Дата создания — довод слишком слабый,
    # чтобы выбирать ею между сделками: спрашиваем владельца.
    return _ask(narrowed)


def _pick_completed(order_date: date, realization: list[LeadInfo],
                    master_ids: Sequence[int]) -> Optional[Decision]:
    """Заказ уже проведён руками? Тогда только привязать (дизайн §5.1).

    Три признака по убыванию надёжности: «Дата и время заказа», дата закрытия
    сделки, дата создания сделки. Последний нужен, когда владелец разбирал CRM
    пачкой: и поле даты пустое, и закрыли сделку через две недели.
    """
    # Сделка, закрытая заметно РАНЬШЕ выполнения заказа, относиться к нему не может:
    # работы тогда ещё не было (заказ №570 — заказ 17.08, сделка закрыта 04.08).
    # Небольшой допуск назад: владелец мог провести сделку в день работы, а мастер
    # закрыть заказ в боте на следующий день.
    completed = [
        lead for lead in realization
        if lead.is_success
        and (lead.closed_date is None
             or (order_date - lead.closed_date).days <= DATE_WINDOW_DAYS)
    ]
    completed = _narrow_by_specialist(completed, master_ids)
    if not completed:
        return None

    by_order_date = [(gap, lead.lead_id) for lead in completed
                     if (gap := _gap(order_date, lead.order_date)) is not None
                     and gap <= DATE_WINDOW_DAYS]
    if by_order_date:
        return Decision(kind="already_done", lead_id=min(by_order_date)[1])

    by_closed = [(gap, lead.lead_id) for lead in completed
                 if (gap := _gap(order_date, lead.closed_date)) is not None
                 and gap <= CLOSED_WINDOW_DAYS]
    if by_closed:
        return Decision(kind="already_done", lead_id=min(by_closed)[1])

    # Третий признак — самый слабый, поэтому требует данных: сделки без даты
    # создания через него не опознаём, чтобы не привязать заказ к чужой сделке.
    fresh = [lead for lead in completed
             if lead.created_date is not None and _is_fresh(order_date, lead)]
    fresh = _narrow_by_specialist(fresh, master_ids)
    if len(fresh) == 1:
        return Decision(kind="already_done", lead_id=fresh[0].lead_id)
    if len(fresh) > 1:
        return _ask(fresh)

    return None


def match(*, order_date: date, candidates: Iterable[LeadInfo],
          master_specialist_ids: Sequence[int] = ()) -> Decision:
    """Решить, что делать с заказом бота, по списку сделок его телефона."""

    # 1. Ковровые и архивные воронки — не наш случай. Заказ, заведённый в боте,
    #    ковровым быть не может: ковры приходят только через Excel партнёра.
    leads = [lead for lead in candidates if lead.pipeline_id not in ids.PIPELINES_IGNORED]

    realization = [lead for lead in leads if lead.pipeline_id == ids.PIPELINE_REALIZATION]
    primary = [lead for lead in leads if lead.pipeline_id == ids.PIPELINE_PRIMARY]

    open_realization = [lead for lead in realization if lead.is_open]
    fresh_realization = [lead for lead in open_realization if _is_fresh(order_date, lead)]

    # 2. Свежая открытая сделка реализации — самый частый случай (путь А).
    open_decision = _choose(order_date, fresh_realization, "use_realization", master_specialist_ids)
    if open_decision is not None and open_decision.kind == "use_realization":
        return open_decision

    # 3. Открытой сделки нет либо среди них не выбрать. Не проведён ли заказ уже
    #    руками? Проверяем ДО вопроса владельцу и до разбора хвостов: у постоянных
    #    клиентов годами висят забытые сделки, они перехватывали решение (заказ №508).
    done_decision = _pick_completed(order_date, realization, master_specialist_ids)
    if done_decision is not None and done_decision.kind == "already_done":
        return done_decision

    # Ни одна ветка не дала уверенного ответа — спрашиваем владельца.
    if open_decision is not None:
        return open_decision
    if done_decision is not None:
        return done_decision

    # 4. Первичная воронка: сначала обработанные лиды, «Неразобранное» — в последнюю
    #    очередь. Клиент мог звонить дважды: не дозвонился, потом дозвонился (заказ №583).
    fresh_primary = [lead for lead in primary if lead.is_open and _is_fresh(order_date, lead)]
    handled = [lead for lead in fresh_primary if not lead.is_unsorted]
    unsorted = [lead for lead in fresh_primary if lead.is_unsorted]

    if handled:
        # Остальные лиды того же клиента — дубли обращения: робот пометит их комментарием.
        others = tuple(sorted(lead.lead_id for lead in fresh_primary if lead not in handled))
        decision = _choose(order_date, handled, "use_primary", master_specialist_ids, duplicates=others)
        if decision is not None:
            return decision
    if unsorted:
        decision = _choose(order_date, unsorted, "use_primary", master_specialist_ids)
        if decision is not None:
            return decision

    # 5. Свежего ничего нет. Есть старые хвосты — решение за владельцем.
    stale = [lead for lead in open_realization + [x for x in primary if x.is_open]
             if not _is_fresh(order_date, lead)]
    if stale:
        return _ask(stale, kind="ask_owner_stale")

    # 6. Ничего подходящего — заводим сделку с нуля (путь В, ~1 раз в неделю).
    return Decision(kind="create_new")
