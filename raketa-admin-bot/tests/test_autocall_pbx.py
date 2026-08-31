"""Протокол АТС и фейк для репетиции.

Боевой клиент (OnlinePbx) появится позже, когда владелец даст ключ API.
Здесь — только контракт `Pbx` и `MemoryPbx`: фейк существует ради принципа
«репетиция не звонит и не оставляет следов» (см. store.py) — она не должна
дозваниваться до живых людей, но обязана вести себя как настоящая АТС
достаточно похоже, чтобы движок (Задача 8) гонялся против неё в тестах.
"""

from datetime import datetime, timezone

import pytest

from adminbot.autocall.chain import Outcome
from adminbot.autocall.pbx import MemoryPbx

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


async def test_call_now_does_not_touch_network_and_records_call():
    """call_now фейка не ходит в сеть — просто копит вызовы в self.calls."""
    pbx = MemoryPbx()

    call_id = await pbx.call_now(to_dial="9161234567", client_phone="9601861067")

    assert call_id == "fake-1"
    assert pbx.calls == [("9161234567", "9601861067")]


async def test_call_now_ids_grow_with_each_call():
    """Каждый следующий звонок получает свой предсказуемый id."""
    pbx = MemoryPbx()

    first = await pbx.call_now(to_dial="9161234567", client_phone="9601861067")
    second = await pbx.call_now(to_dial="9161234567", client_phone="9601861068")

    assert first == "fake-1"
    assert second == "fake-2"
    assert pbx.calls == [
        ("9161234567", "9601861067"),
        ("9161234567", "9601861068"),
    ]


async def test_call_outcome_is_none_until_programmed():
    """Пока исход не запрограммирован — звонок «ещё идёт», как в жизни."""
    pbx = MemoryPbx()
    call_id = await pbx.call_now(to_dial="9161234567", client_phone="9601861067")

    assert await pbx.call_outcome(call_id, called_at=NOW) is None


async def test_call_outcome_returns_programmed_outcome_via_constructor():
    """Исходы можно задать заранее через конструктор — для целого сценария теста."""
    pbx = MemoryPbx(outcomes={"fake-1": Outcome.CONNECTED})
    call_id = await pbx.call_now(to_dial="9161234567", client_phone="9601861067")

    assert await pbx.call_outcome(call_id, called_at=NOW) is Outcome.CONNECTED


async def test_call_outcome_returns_outcome_set_after_the_call():
    """Исход можно запрограммировать и после call_now — методом set_outcome."""
    pbx = MemoryPbx()
    call_id = await pbx.call_now(to_dial="9161234567", client_phone="9601861067")

    pbx.set_outcome(call_id, Outcome.CLIENT_NO_ANSWER)

    assert await pbx.call_outcome(call_id, called_at=NOW) is Outcome.CLIENT_NO_ANSWER


async def test_call_outcome_for_unknown_call_id_is_none():
    """Незнакомый id звонка (опечатка, чужой прогон) — тоже None, а не ошибка."""
    pbx = MemoryPbx()

    assert await pbx.call_outcome("no-such-call", called_at=NOW) is None
