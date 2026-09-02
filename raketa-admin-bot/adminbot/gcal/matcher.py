"""Какой сделке amoCRM соответствует запись календаря.

Чистая функция без сети и без БД — как и матчер заказов, чтобы её решения можно
было проверить на истории и в репетиции.

Чем этот случай отличается от заказа из бота:

1. **Заказ ещё не выполнен.** Робот смотрит вперёд, а не назад: закрытая сделка —
   как правило, прошлая работа того же клиента, и в работу она не берётся.
   Исключение — закрытая ровно на дату записи: владелец мог провести этот самый
   заказ сам, поэтому робот спрашивает, а не заводит вторую (решение 2026-08-27).
2. **Нет ни суммы, ни мастера.** В записи календаря их просто нет, поэтому
   отсеять чужую сделку по цене и «Специалисту» нечем. Остаётся дата.
3. **Запись появляется задолго до работы.** Постоянные клиенты бронируют за три
   недели, и лид, заведённый месяц назад, — норма, а не забытый хвост. Для заказов
   из бота действует обратное правило (граница 14 дней), потому что там сделка
   заводится незадолго до выполнения.

Принцип прежний: лучше спросить владельца, чем уверенно ошибиться.
"""

from __future__ import annotations

from datetime import date
from typing import Collection, Iterable, Optional

from adminbot.amo import ids
from adminbot.sync.matcher import DATE_WINDOW_DAYS, Decision, LeadInfo

# Насколько раньше работы мог появиться лид. Записи в календаре делаются заранее
# (постоянные клиенты — за три недели), поэтому месячный лид нормален. Всё, что
# старше двух месяцев, — хвост: заводить поверх него новую сделку молча нельзя.
MAX_LEAD_AGE_DAYS = 60

# Насколько позже работы мог появиться лид. Обычно ноль: запись создаётся первой.
# Небольшой запас нужен на случай, если клиент написал уже после договорённости.
MAX_LEAD_FUTURE_DAYS = 7

# Возраст, начиная с которого открытая сделка считается забытой, а не рабочей.
# Решение владельца 2026-09-02: полгода. Такой заказ давно закрыт в жизни, но
# в CRM его никто не закрыл — и робот видел незаконченную работу там, где её
# нет. Проверка на живой базе в тот день: из 93 незакрытых сделок 60 старше
# полугода, 51 — старше года, самая древняя заведена в декабре 2021-го.
# Спрашивать владельца по каждой такой — значит спрашивать постоянно.
FORGOTTEN_LEAD_DAYS = 180


def _within_dates(order_date: date, lead: LeadInfo) -> bool:
    if lead.order_date is None:
        return False
    return abs((lead.order_date - order_date).days) <= DATE_WINDOW_DAYS


def _is_timely(order_date: date, lead: LeadInfo) -> bool:
    """Лид мог быть заведён под этот заказ, а не остаться от прошлой жизни."""
    if _within_dates(order_date, lead):
        return True
    if lead.created_date is None:
        return True                            # даты создания нет — не наказываем
    age = (order_date - lead.created_date).days
    return -MAX_LEAD_FUTURE_DAYS <= age <= MAX_LEAD_AGE_DAYS


def _is_forgotten(order_date: date, lead: LeadInfo) -> bool:
    """Сделка, которую забыли закрыть, а не работа, которую предстоит сделать.

    Возраст меряем по дате заведения; её нет — по дате заказа в самой сделке.
    Ни того ни другого нет — хвост считаем живым: сомнение решается в пользу
    вопроса владельцу, потому что вторая сделка по живому заказу дороже, чем
    лишний вопрос.
    """
    reference = lead.created_date or lead.order_date
    if reference is None:
        return False
    return (order_date - reference).days > FORGOTTEN_LEAD_DAYS


def _ask(leads: Iterable[LeadInfo], kind: str = "ask_owner") -> Decision:
    return Decision(kind=kind, options=tuple(sorted(lead.lead_id for lead in leads)))


def _choose(order_date: date, leads: list[LeadInfo], kind: str,
            duplicates: tuple[int, ...] = ()) -> Optional[Decision]:
    """Выбрать одну сделку: единственность → дата → вопрос владельцу."""
    if not leads:
        return None
    if len(leads) == 1:
        return Decision(kind=kind, lead_id=leads[0].lead_id, duplicates=duplicates)

    dated = [lead for lead in leads if _within_dates(order_date, lead)]
    if len(dated) == 1:
        return Decision(kind=kind, lead_id=dated[0].lead_id, duplicates=duplicates)

    # Ни единственности, ни даты — различать нечем: у записи календаря нет ни
    # суммы, ни мастера, которыми матчер заказов разводит похожие сделки.
    return _ask(dated or leads)


def match_event(*, order_date: date, candidates: Iterable[LeadInfo],
                taken_lead_ids: Collection[int] = ()) -> Decision:
    """Решить, что делать с записью календаря, по сделкам её телефона.

    taken_lead_ids — сделки, занятые ДРУГИМИ записями календаря. Сделки заказов
    бота сюда не входят намеренно: запись календаря и заказ из бота — обычно
    один и тот же заказ, и общая сделка у них правильная.
    """
    taken = set(taken_lead_ids)
    mine = [lead for lead in candidates
            if lead.pipeline_id not in ids.PIPELINES_IGNORED
            and lead.lead_id not in taken]
    ours = [lead for lead in mine if lead.is_open]

    realization = [lead for lead in ours if lead.pipeline_id == ids.PIPELINE_REALIZATION]
    primary = [lead for lead in ours if lead.pipeline_id == ids.PIPELINE_PRIMARY]

    # 1. Сделка реализации уже есть (сейлзбот успел её создать) — работаем с ней.
    timely_realization = [lead for lead in realization if _is_timely(order_date, lead)]
    decision = _choose(order_date, timely_realization, "use_realization")
    if decision is not None:
        return decision

    # 2. Лид первичной воронки: обработанные вперёд, «Неразобранное» — в последнюю
    #    очередь. Клиент мог сначала не дозвониться, а потом написать.
    timely_primary = [lead for lead in primary if _is_timely(order_date, lead)]
    handled = [lead for lead in timely_primary if not lead.is_unsorted]
    unsorted = [lead for lead in timely_primary if lead.is_unsorted]

    if handled:
        others = tuple(sorted(lead.lead_id for lead in timely_primary if lead not in handled))
        decision = _choose(order_date, handled, "use_primary", duplicates=others)
        if decision is not None:
            return decision
    if unsorted:
        decision = _choose(order_date, unsorted, "use_primary")
        if decision is not None:
            return decision

    # 3. Свежего нет, но висят старые хвосты. Те, что моложе полугода, ещё могут
    #    быть про этот заказ — решает владелец. Забытые (см. FORGOTTEN_LEAD_DAYS)
    #    работе не мешают: заводим новую сделку и упоминаем их в отчёте, чтобы
    #    владелец знал, что в CRM висит мусор (решение владельца 2026-09-02).
    stale = [lead for lead in ours if not _is_timely(order_date, lead)]
    forgotten = [lead for lead in stale if _is_forgotten(order_date, lead)]
    recent = [lead for lead in stale if lead not in forgotten]
    if recent:
        # Древние в кнопках не показываем: привязывать сегодняшний заказ к сделке
        # двухлетней давности владелец не станет, а лишняя кнопка путает.
        return _ask(recent, kind="ask_owner_stale")
    if forgotten:
        return Decision(kind="create_new",
                        forgotten=tuple(sorted(lead.lead_id for lead in forgotten)))

    # 4. Открытого ничего нет. Но если у клиента есть ЗАКРЫТАЯ сделка ровно на
    #    дату записи — владелец мог провести этот заказ сам, до того как записал
    #    его в календарь. Молча заводить вторую нельзя (решение владельца 2026-08-27).
    same_day_closed = [lead for lead in mine
                       if not lead.is_open and _within_dates(order_date, lead)]
    if same_day_closed:
        return _ask(same_day_closed, kind="ask_owner_closed")

    # 5. Ничего нет — заводим цепочку с нуля.
    return Decision(kind="create_new")
