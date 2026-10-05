"""Форматтеры алертов в money-flow чат клининга."""

from __future__ import annotations

from decimal import Decimal
from zoneinfo import ZoneInfo

from .constants import (
    CASHBOOK_KIND_DEPOSIT,
    CASHBOOK_KIND_DIVIDEND,
    CASHBOOK_KIND_EXPENSE,
    CASHBOOK_KIND_INCOME,
    CASHBOOK_KIND_WITHDRAWAL,
    CASHBOOK_KINDS_DECREASE_BALANCE,
    CASHBOOK_KINDS_INCREASE_BALANCE,
    CLEANING_DIVIDEND_METHOD,
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _money(value: Decimal) -> str:
    """1234.5 → '1 234.50', 87540 → '87 540'."""
    quant = value.quantize(Decimal("0.01"))
    if quant == quant.to_integral_value():
        return f"{int(quant):,}".replace(",", " ")
    return f"{quant:,.2f}".replace(",", " ")


def format_order_provided_alert(
    *,
    order_id: int,
    foreman_name: str,
    client_phone: str,
    client_name: str,
    address: str | None,
    total_amount: Decimal,
    payments: list[tuple[str, Decimal]],
    expenses: list[tuple[str, Decimal]],
    bonuses_used: Decimal,
    bonuses_earned: Decimal,
    profit: Decimal,
    balance_after: Decimal,
    comment: str | None = None,
    olya_balance: Decimal | None = None,
) -> str:
    """`olya_balance` — остаток денег Оли после уборки; None — уборка их не
    задела, строки нет (реестр денег Оли, ТЗ 2026-10-05, задача 2)."""
    pay_line = ", ".join(f"{_money(a)}₽ {m}" for m, a in payments) or "—"
    exp_line = ", ".join(f"{m} {_money(a)}₽" for m, a in expenses) or "—"
    lines = [
        f"✅ Уборка проведена #{order_id}",
        f"Бригадир: {foreman_name}",
        f"Клиент: {client_phone} ({client_name})",
    ]
    if address:
        lines.append(f"Адрес: {address}")
    if comment:
        lines.append(f"Комментарий: {comment}")
    lines += [
        f"Сумма: {_money(total_amount)}₽",
        f"Оплата: {pay_line}",
        f"Расходы: {exp_line}",
        f"Бонусы: списано {_money(bonuses_used)}, начислено {_money(bonuses_earned)}",
        f"Прибыль по заказу: {_money(profit)}₽",
        f"Касса клининга: {_money(balance_after)}₽",
    ]
    if olya_balance is not None:
        lines.append(_olya_line(olya_balance))
    return "\n".join(lines)


def _olya_line(olya_balance: Decimal) -> str:
    """Строка остатка денег Оли в сообщениях в чат клининговых денег."""
    return f"Деньги Оли: {_money(olya_balance)}₽"


def format_dividend_payout_alert(
    *,
    recipients: list[str],
    shares: list[Decimal],
    balance_after: Decimal,
    olya_balance: Decimal | None = None,  # остаток денег Оли; None — строки нет
) -> str:
    """Сообщение в кассовый чат: кто сколько получил и что осталось."""
    lines = ["Выплата прибыли:"]
    lines += [f"{name} — {_money(share)}₽" for name, share in zip(recipients, shares)]
    lines.append(f"Остаток в кассе: {_money(balance_after)}₽")
    if olya_balance is not None:
        lines.append(_olya_line(olya_balance))
    return "\n".join(lines)


def format_dividend_payout_confirm(
    *,
    total: Decimal,
    recipients: list[str],
    shares: list[Decimal],
    balance: Decimal,
    source_line: str | None = None,  # «Источник: …» — из чьих денег; None — строки нет
) -> str:
    """Что человек видит перед подтверждением: деньги живые, показываем всё."""
    lines = [f"Выплата прибыли {_money(total)}₽:"]
    lines += [f"{name} — {_money(share)}₽" for name, share in zip(recipients, shares)]
    lines.append(f"В кассе {_money(balance)}₽, останется {_money(balance - total)}₽.")
    if source_line:
        lines.append(source_line)
    lines.append("Подтвердить?")
    return "\n".join(lines)


def format_dividend_cancel_alert(
    *, payout_id: int, amount: Decimal, balance_after: Decimal
) -> str:
    return (
        f"↩️ Отменена выплата прибыли #{payout_id}\n"
        f"Сумма: {_money(amount)}₽\n"
        f"Остаток в кассе: {_money(balance_after)}₽"
    )


def format_cash_op_alert(
    *,
    op_label: str,           # 'Приход', 'Расход', 'Изъятие'
    bucket: str,             # метод/категория
    amount: Decimal,
    comment: str | None,
    balance_after: Decimal,
    olya_balance: Decimal | None = None,  # остаток денег Оли; None — строки нет
) -> str:
    lines = [
        f"📒 Касса клининга: {op_label}",
        f"{bucket}: {_money(amount)}₽",
    ]
    if comment:
        lines.append(f"Комментарий: {comment}")
    lines.append(f"Остаток: {_money(balance_after)}₽")
    if olya_balance is not None:
        lines.append(_olya_line(olya_balance))
    return "\n".join(lines)


def format_cash_report(*, label: str, report: dict, balance_after: Decimal) -> str:
    lines = [f"📊 Касса клининга — {label}"]
    lines.append(f"Приход: {_money(report['income_total'])}₽")
    for m, v in sorted(report["income_by_method"].items()):
        lines.append(f"  • {m}: {_money(v)}₽")
    if report["gift_total"] > 0:
        lines.append(f"Сертификаты (вне кассы): {_money(report['gift_total'])}₽")
    lines.append(f"Расход: {_money(report['expense_total'])}₽")
    for c, v in sorted(report["expense_by_category"].items()):
        lines.append(f"  • {c}: {_money(v)}₽")
    if report["dividend_total"] > 0:
        lines.append(f"DIV: {_money(report['dividend_total'])}₽")
    if report["withdrawal_total"] > 0:
        lines.append(f"Изъятия: {_money(report['withdrawal_total'])}₽")
    if report["deposit_total"] > 0:
        lines.append(f"Доп. внесения: {_money(report['deposit_total'])}₽")
    lines.append(f"Прибыль (income − expense): {_money(report['profit'])}₽")
    lines.append(f"Баланс сейчас: {_money(balance_after)}₽")
    return "\n".join(lines)


def format_orders_list(*, label: str, orders: list[dict]) -> str:
    if not orders:
        return f"📋 Уборки — {label}\nНет заказов."
    lines = [f"📋 Уборки — {label} ({len(orders)} шт.)"]
    total = Decimal("0")
    for o in orders:
        time_str = o["happened_at"].strftime("%d.%m %H:%M")
        client = o["client_name"] or "Клиент"
        pay = o["pay_summary"] or "—"
        address_part = f" — {o['address']}" if o.get("address") else ""
        lines.append(
            f"#{o['id']} {time_str} {client}{address_part} — "
            f"{_money(o['total_amount'])}₽ ({pay})"
        )
        total += o["total_amount"]
    lines.append(f"Итого: {_money(total)}₽")
    return "\n".join(lines)


def format_cancel_order_alert(
    *,
    order_id: int,
    address: str | None,
    total_amount: Decimal,
    bonuses_used: int,
    bonuses_earned: int,
    cashbook_rows_deleted: int,
    balance_after: Decimal,
) -> str:
    lines = [f"↩️ Отменён заказ уборки #{order_id}"]
    if address:
        lines.append(f"Адрес: {address}")
    lines += [
        f"Сумма чека была: {_money(total_amount)}₽",
        f"Откатано строк кассы: {cashbook_rows_deleted}",
        f"Возвращено бонусов клиенту: {bonuses_used}",
        f"Снято начисленных бонусов: {bonuses_earned}",
        f"Касса клининга: {_money(balance_after)}₽",
    ]
    return "\n".join(lines)


# Подписи видов строк кассы в реестре денег Оли (`/cleaning_olya`).
_OLYA_KIND_LABELS = {
    CASHBOOK_KIND_INCOME: "Приход",
    CASHBOOK_KIND_DEPOSIT: "Внесение",
    CASHBOOK_KIND_EXPENSE: "Расход",
    CASHBOOK_KIND_DIVIDEND: "Выплата прибыли",
    CASHBOOK_KIND_WITHDRAWAL: "Изъятие",
}


def format_olya_register(*, balance: Decimal, entries: list) -> str:
    """Остаток денег Оли и последние операции, по образцу «Карты Жени» в bot.py.

    Строка операции: `дата | ±сумма | вид/категория | комментарий`. Знак — как в
    остатке (`get_olya_balance`): приход и внесение плюс, расход, выплата и
    изъятие минус. Способ оплаты или категорию пишем после вида; у выплаты и
    изъятия там служебное «Касса клининга» — его не показываем.
    """
    lines = [_olya_line(balance)]
    if not entries:
        lines.append("Операций пока нет.")
        return "\n".join(lines)
    lines.append("")
    lines.append("Последние операции:")
    for row in entries:
        dt = row["happened_at"].astimezone(MOSCOW_TZ).strftime("%d.%m %H:%M")
        kind = row["kind"]
        if kind in CASHBOOK_KINDS_INCREASE_BALANCE:
            sign = "+"
        elif kind in CASHBOOK_KINDS_DECREASE_BALANCE:
            sign = "-"
        else:
            sign = ""
        amount = _money(Decimal(row["amount"] or 0))
        what = _OLYA_KIND_LABELS.get(kind, kind)
        method = (row["method"] or "").strip()
        if method and method != CLEANING_DIVIDEND_METHOD:
            what = f"{what}/{method}"
        comment = (row["comment"] or "").strip() or "—"
        lines.append(f"{dt} | {sign}{amount}₽ | {what} | {comment}")
    return "\n".join(lines)
