"""Канонические строки клининг-контура."""

from decimal import Decimal

CLEANING_PAYMENT_METHODS = ["Наличные", "Карта", "Расчётный"]
CLEANING_GIFT_CERT_LABEL = "Подарочный сертификат"
CLEANING_ALL_PAYMENT_LABELS = CLEANING_PAYMENT_METHODS + [CLEANING_GIFT_CERT_LABEL]

CLEANING_EXPENSE_CATEGORIES = ["Химия", "ЗП клинеров", "ГСМ", "Прочее"]

CLEANING_DIVIDEND_METHOD = "Касса клининга"

CASHBOOK_KIND_INCOME = "income"
CASHBOOK_KIND_EXPENSE = "expense"
CASHBOOK_KIND_DIVIDEND = "dividend"
CASHBOOK_KIND_WITHDRAWAL = "withdrawal"
CASHBOOK_KIND_DEPOSIT = "deposit"
# Перемещение между кучками «Деньги Ольга» ↔ «Касса (Дима)»: cash_holder строки —
# кучка-источник. Всю кассу не меняет, поэтому нет ни в одном наборе ниже
# (ТЗ docs/plans/2026-10-05-olya-money-move.md, задача 2).
CASHBOOK_KIND_MOVE = "move"

CASHBOOK_KINDS_INCREASE_BALANCE = {CASHBOOK_KIND_INCOME, CASHBOOK_KIND_DEPOSIT}
CASHBOOK_KINDS_DECREASE_BALANCE = {
    CASHBOOK_KIND_EXPENSE,
    CASHBOOK_KIND_DIVIDEND,
    CASHBOOK_KIND_WITHDRAWAL,
}
CASHBOOK_KINDS_PNL = {CASHBOOK_KIND_INCOME, CASHBOOK_KIND_EXPENSE}

# «Чьи деньги» у строки кассы (cleaning_cashbook.cash_holder, миграция 0017).
# 'olya' — деньги компании на руках у Оли, 'dima' — обычная касса.
CASH_HOLDER_OLYA = "olya"
CASH_HOLDER_DIMA = "dima"
# Подписи кучек, которые видит человек (решение владельца 05.10, ТЗ
# docs/plans/2026-10-05-olya-money-move.md, задача 1).
CASH_HOLDER_OLYA_LABEL = "Деньги Ольга"
CASH_HOLDER_DIMA_LABEL = "Касса (Дима)"
# Оплаты по уборке, которые попадают к Оле (решение владельца 05.10, п.1).
CLEANING_OLYA_PAYMENT_METHODS = ("Наличные", "Карта")

ZERO = Decimal("0")
