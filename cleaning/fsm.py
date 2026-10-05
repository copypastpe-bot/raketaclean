"""FSM states for cleaning order completion."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class CleaningOrderFSM(StatesGroup):
    phone = State()
    name = State()
    comment = State()
    amount = State()
    bonus_spend = State()
    pay_method = State()
    pay_amount = State()
    pay_more = State()
    expense_category = State()
    expense_amount = State()
    expense_more = State()
    confirm = State()


# cash_holder — шаг «Оля / Дима»: из чьих денег операция (cleaning_cashbook.cash_holder,
# ТЗ docs/plans/2026-10-05-olya-money-register.md, задача 3).

class CleaningDividendFSM(StatesGroup):
    amount = State()
    cash_holder = State()
    confirm = State()


class CleaningDividendCancelFSM(StatesGroup):
    confirm = State()


class CleaningCashAddFSM(StatesGroup):
    method = State()
    amount = State()
    cash_holder = State()
    comment = State()
    confirm = State()


class CleaningCashExpenseFSM(StatesGroup):
    category = State()
    amount = State()
    cash_holder = State()
    comment = State()
    confirm = State()


class CleaningCashWithdrawalFSM(StatesGroup):
    amount = State()
    cash_holder = State()
    comment = State()
    confirm = State()


class CleaningCashMoveFSM(StatesGroup):
    # Перемещение между кучками «Деньги Ольга» ↔ «Касса (Дима)»
    # (ТЗ docs/plans/2026-10-05-olya-money-move.md, задача 3).
    source = State()
    amount = State()
    comment = State()
    confirm = State()

class CleaningCancelOrderFSM(StatesGroup):
    confirm = State()


class CleaningClientLookupFSM(StatesGroup):
    phone = State()


class CleaningForemanExpenseFSM(StatesGroup):
    amount = State()
    category = State()
    cash_holder = State()  # только у не-клинера; клинер всегда тратит деньги Оли
    comment = State()
    confirm = State()
