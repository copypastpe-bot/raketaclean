"""Протокол АТС для автозвонка и фейк для тестов и репетиции.

Боевой клиент (OnlinePbx) появится отдельно, когда владелец даст ключ API —
здесь только контракт `Pbx` и `MemoryPbx`. Движок (Задача 8) знает лишь эти
два метода: запустить звонок «менеджер → отбивка → клиент» и спросить, чем
он закончился. Как АТС это делает внутри — не касается ни машины переходов
(chain.py), ни хранилища (store.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol

from adminbot.autocall.chain import Outcome


class Pbx(Protocol):
    """Минимум, который движку нужен от телефонии."""

    async def call_now(self, *, to_dial: str, client_phone: str) -> str:
        """Запустить звонок «менеджеру → отбивка → клиенту», вернуть id в АТС."""
        ...

    async def call_outcome(self, call_id: str, *, called_at: datetime) -> Optional[Outcome]:
        """Чем закончился звонок.

        None — звонок ещё идёт либо истории в АТС пока нет (движок спросит
        ещё раз позже); иначе — исход из adminbot.autocall.chain.Outcome.
        """
        ...


class MemoryPbx:
    """Фейковая АТС в памяти: для тестов и репетиции. В сеть не ходит никогда.

    Тот же принцип, что у MemoryAutocallStore: «репетиция не оставляет
    следов» — реальный звонок живому человеку украл бы у неё право быть
    репетицией. Поэтому call_now ничего не набирает, а просто запоминает
    вызов в self.calls и выдаёт предсказуемый id ("fake-1", "fake-2", …);
    исход звонка не выдумывается автоматически, а программируется заранее
    тестом — через конструктор (`outcomes`) или `set_outcome`.
    """

    def __init__(self, outcomes: Optional[dict[str, Outcome]] = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._outcomes: dict[str, Outcome] = dict(outcomes or {})
        self._next_id = 1

    async def call_now(self, *, to_dial: str, client_phone: str) -> str:
        call_id = f"fake-{self._next_id}"
        self._next_id += 1
        self.calls.append((to_dial, client_phone))
        return call_id

    async def call_outcome(self, call_id: str, *, called_at: datetime) -> Optional[Outcome]:
        # called_at не используется: фейку не нужно время, чтобы отдать
        # запрограммированный исход — оно нужно только боевому клиенту,
        # который ходит в реальную историю звонков АТС.
        return self._outcomes.get(call_id)

    def set_outcome(self, call_id: str, outcome: Outcome) -> None:
        """Запрограммировать исход звонка — можно и после call_now."""
        self._outcomes[call_id] = outcome
