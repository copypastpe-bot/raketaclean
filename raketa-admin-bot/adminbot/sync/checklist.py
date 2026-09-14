"""Чек-лист исполнителя: последовательность шагов по каждому пути.

Зачем он нужен: заказ проводится не одним действием, а цепочкой (заполнить,
перевести этап, дождаться автосделки, закрыть задачи). Между шагами робота может
остановить что угодно — перезапуск сервиса, недоступность амо, пауза владельца.
Каждый выполненный шаг записывается в `adminbot.amo_links.checklist`, поэтому
после сбоя работа продолжается с места остановки, а не начинается заново
и не делается дважды (дизайн §5.4).

Логика здесь чистая: ни сети, ни базы — только «что дальше по записанному».
"""

from __future__ import annotations

from dataclasses import dataclass

# Путь А (дизайн §4): автосделка в воронке реализации уже есть — довести до конца.
PATH_A: tuple[str, ...] = (
    "fill_realization",        # бюджет, услуга, дата заказа, адрес, оплата, специалист
    "fix_contact_name",        # заменить «Входящий 79…» на имя клиента из бота
    "close_autotasks",         # «Назначь мастера», «Выполни заказ» и прочие
    "close_feedback_task",     # «Получить ОС» — только если клиент поставил оценку боту
    # Смена этапа идёт ПОСЛЕ закрытия задач: переход в финал порождает новые задачи
    # сейлзбота (например, «получить оплату»), и их трогать нельзя.
    "move_realization_done",   # → «ЗАКАЗ ВЫПОЛНЕН и Оплата получена» либо «Заказ выполнен»
    "note_robot_done",         # отметка в сделке: провёл робот, вот чем
)

# Путь Б: есть только лид первичной воронки — довести его до «Передано в работу»,
# дождаться автосделки сейлзбота и дальше как в пути А.
PATH_B: tuple[str, ...] = (
    "note_duplicates",         # пометить комментарием другие обращения того же клиента
    "fill_primary",
    "move_primary_success",    # → «Передано в работу», после этого сейлзбот создаёт сделку
    "wait_salesbot",           # до 10 минут ожидания автосделки (дизайн §5.3)
) + PATH_A

# Путь В: сделки нет вовсе — найти или создать контакт и завести сделку.
# Отдельного «заполнить» нет: сделка создаётся сразу заполненной.
PATH_C: tuple[str, ...] = (
    "ensure_contact",
    "create_primary_lead",
) + tuple(step for step in PATH_B if step not in ("note_duplicates", "fill_primary"))

# Заказ уже проведён владельцем вручную: только привязка, в амо ничего не трогаем.
PATH_DONE: tuple[str, ...] = ()

PATHS: dict[str, tuple[str, ...]] = {
    "A": PATH_A,
    "B": PATH_B,
    "C": PATH_C,
    "done": PATH_DONE,
}


@dataclass(frozen=True)
class StepContext:
    """Обстоятельства заказа, от которых зависит состав шагов."""

    has_rating: bool = False       # клиент поставил оценку боту → задачу «Получить ОС» закрываем
    has_duplicates: bool = False   # у клиента есть другие обращения → пометить их комментарием


# Шаги, которые делаются не всегда, и условие их применимости.
_CONDITIONAL = {
    "close_feedback_task": lambda ctx: ctx.has_rating,
    "note_duplicates": lambda ctx: ctx.has_duplicates,
}


def steps_for(path: str, context: StepContext = StepContext()) -> tuple[str, ...]:
    """Полная последовательность шагов пути с учётом обстоятельств заказа."""
    if path not in PATHS:
        raise ValueError(f"Неизвестный путь заказа: {path}")
    return tuple(
        step for step in PATHS[path]
        if step not in _CONDITIONAL or _CONDITIONAL[step](context)
    )


def next_step(path: str, checklist: dict, context: StepContext = StepContext()) -> str | None:
    """Первый невыполненный шаг пути. None — работа по заказу закончена.

    Незнакомые записи в чек-листе игнорируются: состав шагов мог измениться
    между версиями робота, и это не повод сбиваться.
    """
    for step in steps_for(path, context):
        if step not in (checklist or {}):
            return step
    return None
