"""Машина попыток дозвона: правила владельца №3, №4 и предохранитель §4.5.

Контракт с движком (Задача 8): attempts_total инкрементирует движок при команде
АТС, ДО advance. Машина получает цепочку, где только что завершившаяся попытка
уже посчитана. Тесты конструируют такие состояния напрямую.
"""

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from adminbot.autocall.chain import (
    MAX_ATTEMPTS,
    NOTIFY_KINDS,
    RETRY_CLIENT,
    RETRY_MANAGER,
    Chain,
    Done,
    GaveUp,
    MoveLeadNoContact,
    NotifyManager,
    Outcome,
    Retry,
    advance,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def chain(**overrides) -> Chain:
    """Цепочка «попытка только что завершилась»: attempts_total уже учтён движком."""
    fields = dict(
        lead_id=101,
        status="calling",
        attempts_total=1,
        manager_failures=0,
        client_failures=0,
        next_action_at=None,
    )
    fields.update(overrides)
    return Chain(**fields)


# --- Константы: дословные решения владельца ---

def test_constants_match_owner_decisions():
    assert MAX_ATTEMPTS == 4                        # §4.5 дизайна
    assert RETRY_MANAGER == timedelta(minutes=5)    # решение №3
    assert RETRY_CLIENT == timedelta(minutes=10)    # решение №4


def test_notify_kinds_fixed_set():
    assert set(NOTIFY_KINDS) == {
        "client_retry_10", "no_contact_final",
        "manager_unreachable", "attempts_exhausted",
    }


def test_notify_manager_rejects_unknown_kind():
    with pytest.raises(ValueError):
        NotifyManager("made_up_kind")


# --- CONNECTED: соединились — цепочка закрыта ---

def test_connected_closes_chain_done():
    new, effects = advance(chain(), Outcome.CONNECTED, NOW)
    assert new.status == "done"
    assert new.next_action_at is None
    assert effects == [Done()]


def test_connected_after_manager_failure_still_done():
    # Смешанный исход: менеджер не взял → взял (счётчики сохраняются, статус done).
    new, effects = advance(
        chain(attempts_total=2, manager_failures=1), Outcome.CONNECTED, NOW,
    )
    assert new.status == "done"
    assert new.manager_failures == 1
    assert effects == [Done()]


def test_connected_ignores_attempts_limit():
    # Лимит — только там, где иначе был бы Retry; соединение закрывает как обычно.
    new, effects = advance(chain(attempts_total=MAX_ATTEMPTS), Outcome.CONNECTED, NOW)
    assert new.status == "done"
    assert effects == [Done()]


# --- Менеджер не взял (решение №3): повтор 5 минут, вторая неудача — стоп ---

def test_manager_first_failure_retries_in_5_minutes():
    new, effects = advance(chain(), Outcome.MANAGER_NO_ANSWER, NOW)
    assert new.status == "queued"
    assert new.manager_failures == 1
    assert new.next_action_at == NOW + timedelta(minutes=5)
    assert effects == [Retry(at=NOW + timedelta(minutes=5))]


def test_manager_second_failure_gives_up_and_notifies():
    new, effects = advance(
        chain(attempts_total=2, manager_failures=1), Outcome.MANAGER_NO_ANSWER, NOW,
    )
    assert new.status == "gave_up"
    assert new.manager_failures == 2
    assert new.next_action_at is None
    assert effects == [
        NotifyManager("manager_unreachable"),
        GaveUp("manager_unreachable"),
    ]


def test_unknown_counts_as_manager_failure():
    # История АТС не нашлась — считаем как неудачу менеджера.
    new, effects = advance(chain(), Outcome.UNKNOWN, NOW)
    assert new.manager_failures == 1
    assert effects == [Retry(at=NOW + timedelta(minutes=5))]


def test_unknown_as_second_manager_failure_gives_up():
    new, effects = advance(
        chain(attempts_total=2, manager_failures=1), Outcome.UNKNOWN, NOW,
    )
    assert new.status == "gave_up"
    assert effects == [
        NotifyManager("manager_unreachable"),
        GaveUp("manager_unreachable"),
    ]


# --- Клиент не взял (решение №4): сообщение + повтор 10 минут, вторая — этап амо ---

def test_client_first_failure_notifies_and_retries_in_10_minutes():
    new, effects = advance(chain(), Outcome.CLIENT_NO_ANSWER, NOW)
    assert new.status == "queued"
    assert new.client_failures == 1
    assert new.next_action_at == NOW + timedelta(minutes=10)
    assert effects == [
        NotifyManager("client_retry_10"),
        Retry(at=NOW + timedelta(minutes=10)),
    ]


def test_client_second_failure_moves_lead_no_contact():
    new, effects = advance(
        chain(attempts_total=2, client_failures=1), Outcome.CLIENT_NO_ANSWER, NOW,
    )
    assert new.status == "no_contact"
    assert new.client_failures == 2
    assert new.next_action_at is None
    assert effects == [
        MoveLeadNoContact(),
        NotifyManager("no_contact_final"),
    ]


def test_client_failure_after_manager_failure_mixed_chain():
    # Смешанный исход: менеджер не взял, потом клиент не взял — счётчики раздельные.
    new, effects = advance(
        chain(attempts_total=2, manager_failures=1), Outcome.CLIENT_NO_ANSWER, NOW,
    )
    assert new.manager_failures == 1
    assert new.client_failures == 1
    assert new.status == "queued"
    assert effects == [
        NotifyManager("client_retry_10"),
        Retry(at=NOW + timedelta(minutes=10)),
    ]


# --- Предохранитель §4.5: перед ЛЮБЫМ Retry проверяем лимит попыток ---

def test_limit_blocks_manager_retry():
    # (а) был бы Retry(+5), но попытки израсходованы → gave_up/attempts_exhausted.
    new, effects = advance(
        chain(attempts_total=MAX_ATTEMPTS), Outcome.MANAGER_NO_ANSWER, NOW,
    )
    assert new.status == "gave_up"
    assert new.manager_failures == 1
    assert new.next_action_at is None
    assert effects == [
        NotifyManager("attempts_exhausted"),
        GaveUp("attempts_exhausted"),
    ]


def test_limit_blocks_client_retry_without_false_promise():
    # (б) был бы Retry(+10) — лимит; «повтор через 10 минут» обещать нельзя,
    # поэтому client_retry_10 НЕ шлём, шлём attempts_exhausted.
    new, effects = advance(
        chain(attempts_total=MAX_ATTEMPTS), Outcome.CLIENT_NO_ANSWER, NOW,
    )
    assert new.status == "gave_up"
    assert new.client_failures == 1
    assert new.next_action_at is None
    assert effects == [
        NotifyManager("attempts_exhausted"),
        GaveUp("attempts_exhausted"),
    ]
    kinds = [e.kind for e in effects if isinstance(e, NotifyManager)]
    assert "client_retry_10" not in kinds


def test_limit_does_not_touch_second_client_failure():
    # Вторая неудача клиента и так ведёт в no_contact без Retry — лимит ни при чём.
    new, effects = advance(
        chain(attempts_total=MAX_ATTEMPTS, client_failures=1),
        Outcome.CLIENT_NO_ANSWER, NOW,
    )
    assert new.status == "no_contact"
    assert effects == [MoveLeadNoContact(), NotifyManager("no_contact_final")]


def test_limit_does_not_touch_second_manager_failure():
    # Вторая неудача менеджера — свой финал manager_unreachable, не attempts_exhausted.
    new, effects = advance(
        chain(attempts_total=MAX_ATTEMPTS, manager_failures=1),
        Outcome.MANAGER_NO_ANSWER, NOW,
    )
    assert new.status == "gave_up"
    assert effects == [
        NotifyManager("manager_unreachable"),
        GaveUp("manager_unreachable"),
    ]


def test_attempts_below_limit_still_retry():
    # attempts_total=3: четвёртая попытка ещё разрешена, повтор назначается.
    new, effects = advance(
        chain(attempts_total=MAX_ATTEMPTS - 1), Outcome.MANAGER_NO_ANSWER, NOW,
    )
    assert new.status == "queued"
    assert effects == [Retry(at=NOW + timedelta(minutes=5))]


# --- Финальные статусы: движок не должен звать машину по закрытой цепочке ---

@pytest.mark.parametrize("final_status", ["done", "no_contact", "gave_up"])
def test_advance_on_final_status_raises(final_status):
    with pytest.raises(ValueError):
        advance(chain(status=final_status), Outcome.CONNECTED, NOW)


# --- Неизменяемость: advance не мутирует вход ---

def test_advance_does_not_mutate_input():
    original = chain(attempts_total=2, manager_failures=1)
    snapshot = Chain(**{f: getattr(original, f) for f in (
        "lead_id", "status", "attempts_total", "manager_failures",
        "client_failures", "next_action_at",
    )})
    new, _ = advance(original, Outcome.MANAGER_NO_ANSWER, NOW)
    assert original == snapshot
    assert new is not original


def test_chain_is_frozen():
    c = chain()
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.status = "done"


def test_effects_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        NotifyManager("client_retry_10").kind = "no_contact_final"
