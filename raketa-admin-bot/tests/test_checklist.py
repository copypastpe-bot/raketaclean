"""Чек-лист исполнителя: какой шаг делать следующим.

Смысл чек-листа — идемпотентность (дизайн §5.4): каждый выполненный шаг
записывается в своё хранилище, поэтому после сбоя, перезапуска сервиса или
повторного тика робот продолжает с места остановки и не делает работу дважды.
"""

import pytest

from adminbot.sync.checklist import StepContext, next_step, steps_for

RATED = StepContext(has_rating=True)
WITH_DUPLICATES = StepContext(has_duplicates=True)


def done(*names):
    """Чек-лист, в котором перечисленные шаги уже выполнены."""
    return {name: "2026-08-25T10:00:00Z" for name in names}


# --- путь А: автосделка уже есть, довести до конца ---

def test_path_a_runs_in_order():
    assert next_step("A", {}) == "fill_realization"
    assert next_step("A", done("fill_realization")) == "move_realization_done"
    assert next_step("A", done("fill_realization", "move_realization_done")) == "close_autotasks"


def test_path_a_finishes_without_feedback_task_when_client_did_not_rate():
    checklist = done("fill_realization", "move_realization_done", "close_autotasks")
    assert next_step("A", checklist) is None


def test_feedback_task_closed_only_when_client_rated():
    """Решение владельца №9: нет оценки — задачу «Получить ОС» оставляем ему."""
    checklist = done("fill_realization", "move_realization_done", "close_autotasks")
    assert next_step("A", checklist, RATED) == "close_feedback_task"
    assert next_step("A", done(*steps_for("A", RATED)), RATED) is None


# --- путь Б: есть только лид первичной воронки ---

def test_path_b_starts_with_primary_and_continues_into_path_a():
    assert next_step("B", {}) == "fill_primary"
    assert next_step("B", done("fill_primary")) == "move_primary_success"
    assert next_step("B", done("fill_primary", "move_primary_success")) == "wait_salesbot"
    # после автосделки сейлзбота продолжаем как в пути А
    assert next_step("B", done("fill_primary", "move_primary_success",
                               "wait_salesbot")) == "fill_realization"


def test_path_b_marks_duplicate_leads_first():
    """Клиент звонил дважды: второму лиду робот пишет комментарий (заказ №583)."""
    assert next_step("B", {}, WITH_DUPLICATES) == "note_duplicates"
    assert next_step("B", done("note_duplicates"), WITH_DUPLICATES) == "fill_primary"


def test_no_duplicates_no_step():
    assert "note_duplicates" not in steps_for("B", StepContext())


# --- путь В: сделки нет вовсе ---

def test_path_c_creates_contact_and_lead_then_follows_path_b():
    assert next_step("C", {}) == "ensure_contact"
    assert next_step("C", done("ensure_contact")) == "create_primary_lead"
    # сделку создаём уже заполненной, поэтому отдельного «заполнить» нет
    assert next_step("C", done("ensure_contact", "create_primary_lead")) == "move_primary_success"


def test_path_c_full_sequence():
    assert steps_for("C") == (
        "ensure_contact", "create_primary_lead", "move_primary_success", "wait_salesbot",
        "fill_realization", "move_realization_done", "close_autotasks",
    )


# --- заказ уже проведён руками ---

def test_already_done_path_has_no_steps():
    """Дизайн §5.1: только привязать, в амо ничего не менять."""
    assert steps_for("done") == ()
    assert next_step("done", {}) is None


# --- идемпотентность ---

def test_finished_checklist_asks_for_nothing():
    assert next_step("A", done(*steps_for("A"))) is None
    assert next_step("B", done(*steps_for("B"))) is None
    assert next_step("C", done(*steps_for("C"))) is None


def test_repeated_call_returns_the_same_step():
    """Повторный тик до записи результата не сдвигает чек-лист."""
    checklist = done("fill_realization")
    assert next_step("A", checklist) == next_step("A", checklist) == "move_realization_done"


def test_unknown_steps_in_checklist_do_not_break_it():
    """Старая запись из прошлой версии робота не должна ломать разбор."""
    checklist = done("fill_realization", "какой_то_старый_шаг")
    assert next_step("A", checklist) == "move_realization_done"


def test_gap_in_the_middle_is_filled():
    """Шаг пропущен (например, упали на нём) — вернёмся именно к нему."""
    checklist = done("fill_realization", "close_autotasks")
    assert next_step("A", checklist) == "move_realization_done"


def test_unknown_path_is_an_error():
    with pytest.raises(ValueError, match="Z"):
        next_step("Z", {})
