"""Административные кассовые операции и отмена заказа уборки.

Никаких aiogram-зависимостей — только данные и запись в БД.
Используется из handlers.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import asyncpg

from .cashbook import get_holder_balance
from .constants import (
    CASHBOOK_KIND_DEPOSIT,
    CASHBOOK_KIND_DIVIDEND,
    CASHBOOK_KIND_EXPENSE,
    CASHBOOK_KIND_MOVE,
    CASHBOOK_KIND_WITHDRAWAL,
    CASH_HOLDER_DIMA,
    CASH_HOLDER_DIMA_LABEL,
    CASH_HOLDER_OLYA,
    CASH_HOLDER_OLYA_LABEL,
    CLEANING_DIVIDEND_METHOD,
)


async def add_cash_income(
    conn: asyncpg.Connection,
    *,
    method: str,
    amount: Decimal,
    comment: str | None,
    cash_holder: str,
) -> int:
    """Ручной приход (deposit) — НЕ income от заказа, отдельная категория.

    cash_holder — чьи деньги: 'olya' | 'dima' (CASH_HOLDER_*), без умолчания.
    """
    return await conn.fetchval(
        """
        INSERT INTO cleaning_cashbook (kind, method, amount, comment, cash_holder)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        CASHBOOK_KIND_DEPOSIT,
        method,
        amount,
        comment,
        cash_holder,
    )


async def add_cash_expense(
    conn: asyncpg.Connection,
    *,
    category: str,
    amount: Decimal,
    comment: str | None,
    cash_holder: str,
) -> int:
    """cash_holder — из чьих денег: 'olya' | 'dima' (CASH_HOLDER_*), без умолчания."""
    return await conn.fetchval(
        """
        INSERT INTO cleaning_cashbook (kind, method, amount, comment, cash_holder)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        CASHBOOK_KIND_EXPENSE,
        category,
        amount,
        comment,
        cash_holder,
    )


async def add_cash_withdrawal(
    conn: asyncpg.Connection, *, amount: Decimal, comment: str | None, cash_holder: str
) -> int:
    """cash_holder — из чьих денег: 'olya' | 'dima' (CASH_HOLDER_*), без умолчания."""
    return await conn.fetchval(
        """
        INSERT INTO cleaning_cashbook (kind, method, amount, comment, cash_holder)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        CASHBOOK_KIND_WITHDRAWAL,
        CLEANING_DIVIDEND_METHOD,  # тот же «Касса клининга» как single source
        amount,
        comment,
        cash_holder,
    )


async def cancel_order(
    conn: asyncpg.Connection, *, order_id: int
) -> dict | None:
    """Откатить заказ уборки целиком.

    Soft-delete заказа, всех строк cleaning_cashbook по order_id, плюс
    обратный пересчёт бонусов клиента в общей таблице. Возвращает dict
    с фактами для построения алерта, или None если заказа нет / уже
    отменён. `olya_rows_deleted` — сколько из откатанных строк кассы были
    деньгами Оли: больше нуля — в сообщение об отмене идёт её остаток.
    """
    order = await conn.fetchrow(
        """
        SELECT id, client_id, foreman_id, total_amount,
               bonuses_used, bonuses_earned, address, deleted_at
        FROM cleaning_orders
        WHERE id = $1
        """,
        order_id,
    )
    if order is None or order["deleted_at"] is not None:
        return None

    now_utc = datetime.now(timezone.utc)

    # 1. Помечаем заказ удалённым
    await conn.execute(
        "UPDATE cleaning_orders SET deleted_at = $1 WHERE id = $2",
        now_utc,
        order_id,
    )

    # 2. Soft-delete всех cashbook-строк по этому заказу
    cb_affected = await conn.fetchrow(
        """
        WITH upd AS (
            UPDATE cleaning_cashbook
            SET deleted_at = $1
            WHERE order_id = $2 AND deleted_at IS NULL
            RETURNING cash_holder
        )
        SELECT count(*) AS total,
               count(*) FILTER (WHERE cash_holder = $3) AS olya
        FROM upd
        """,
        now_utc,
        order_id,
        CASH_HOLDER_OLYA,
    )

    # 3. Откат бонусов (зеркальные транзакции, чтобы история была полной)
    used = int(order["bonuses_used"] or 0)
    earned = int(order["bonuses_earned"] or 0)
    if used > 0:
        await conn.execute(
            """
            INSERT INTO bonus_transactions
                (client_id, delta, reason, created_at, happened_at, meta)
            VALUES ($1, $2, 'refund', $3, $3,
                    jsonb_build_object('source','cleaning_cancel','order_id',$4::int))
            """,
            order["client_id"],
            used,                    # вернули клиенту то, что списали
            now_utc,
            order_id,
        )
    if earned > 0:
        await conn.execute(
            """
            INSERT INTO bonus_transactions
                (client_id, delta, reason, created_at, happened_at, meta)
            VALUES ($1, $2, 'reversal', $3, $3,
                    jsonb_build_object('source','cleaning_cancel','order_id',$4::int))
            """,
            order["client_id"],
            -earned,                 # сняли то, что начислили
            now_utc,
            order_id,
        )
    if used or earned:
        await conn.execute(
            "UPDATE clients SET bonus_balance = bonus_balance + $1 - $2 WHERE id = $3",
            used,
            earned,
            order["client_id"],
        )

    return {
        "order_id": order_id,
        "client_id": order["client_id"],
        "address": order["address"],
        "total_amount": Decimal(order["total_amount"]),
        "bonuses_used": used,
        "bonuses_earned": earned,
        "cashbook_rows_deleted": int(cb_affected["total"] or 0),
        "olya_rows_deleted": int(cb_affected["olya"] or 0),
    }


async def list_recent_dividends(
    conn: asyncpg.Connection, *, limit: int = 5
) -> list[asyncpg.Record]:
    """Последние выплаты — чтобы администратор нашёл id ошибочной."""
    return await conn.fetch(
        """
        SELECT id, amount, happened_at
        FROM cleaning_cashbook
        WHERE kind = $1 AND deleted_at IS NULL
        ORDER BY id DESC
        LIMIT $2
        """,
        CASHBOOK_KIND_DIVIDEND,
        limit,
    )


async def cancel_dividend(
    conn: asyncpg.Connection, *, payout_id: int
) -> asyncpg.Record | None:
    """Мягко удаляет строку выплаты. None, если её нет или уже отменена.

    Возвращает `amount` и `cash_holder` отменённой строки: по метке
    обработчик решает, нужна ли в сообщении строка «Деньги Оли».

    Отменяем только выплаты: перепутать id с расходом или заказом нельзя,
    иначе одна опечатка администратора тихо развернёт чужую операцию.
    """
    return await conn.fetchrow(
        """
        UPDATE cleaning_cashbook
        SET deleted_at = $1
        WHERE id = $2 AND kind = $3 AND deleted_at IS NULL
        RETURNING amount, cash_holder
        """,
        datetime.now(timezone.utc),
        payout_id,
        CASHBOOK_KIND_DIVIDEND,
    )


# ---------- перемещение между кучками ----------
# ТЗ docs/plans/2026-10-05-olya-money-move.md, задача 2. Перемещение — одна
# строка kind = 'move'; cash_holder — кучка-источник, method — подпись маршрута
# для людей (логика по method не ветвится). Удаление — deleted_at.

# Ключ pg_advisory_xact_lock: записи и удаления перемещений не пересекаются.
# Прочие операции кассы замок не берут — это принято («Устройство» ТЗ).
CASH_MOVE_LOCK_KEY = 2026100501

_CASH_HOLDER_LABELS = {
    CASH_HOLDER_OLYA: CASH_HOLDER_OLYA_LABEL,
    CASH_HOLDER_DIMA: CASH_HOLDER_DIMA_LABEL,
}
_OTHER_HOLDER = {CASH_HOLDER_OLYA: CASH_HOLDER_DIMA, CASH_HOLDER_DIMA: CASH_HOLDER_OLYA}


def cash_holder_label(holder: str) -> str:
    """'olya' → «Деньги Ольга», 'dima' → «Касса (Дима)»."""
    return _CASH_HOLDER_LABELS[holder]


def cash_move_route(source: str) -> str:
    """Маршрут по кучке-источнику: «Деньги Ольга → Касса (Дима)» или обратно."""
    return f"{cash_holder_label(source)} → {cash_holder_label(_OTHER_HOLDER[source])}"


class CashMoveRefused(Exception):
    """Отказ перемещения или его удаления по остатку.

    `holder` — кучка ('olya' | 'dima'), `label` — её подпись для людей,
    `balance` — сумма для ответа (что именно — см. подклассы).
    """

    def __init__(self, holder: str, balance: Decimal) -> None:
        super().__init__(holder, balance)
        self.holder = holder
        self.balance = balance

    @property
    def label(self) -> str:
        return cash_holder_label(self.holder)


class CashMoveExceedsBalance(CashMoveRefused):
    """Сумма больше остатка источника: `holder` — источник, `balance` — его остаток сейчас."""


class CashMoveDeleteGoesNegative(CashMoveRefused):
    """Удаление уводит кучку в минус: `holder` — эта кучка, `balance` — «станет» (< 0)."""


async def _lock_cash_moves(conn: asyncpg.Connection) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", CASH_MOVE_LOCK_KEY)


async def record_cash_move(
    conn: asyncpg.Connection, *, source: str, amount: Decimal, comment: str | None
) -> int:
    """Переместить `amount` из кучки `source` ('olya' | 'dima') в другую. Возвращает id.

    В транзакции под замком перепроверяет остаток источника: больше него —
    CashMoveExceedsBalance, ничего не пишется. Неизвестный источник или сумма
    не больше нуля — ValueError. Внутри транзакции вызывающего замок держится
    до её конца.
    """
    if source not in _OTHER_HOLDER:
        raise ValueError(f"неизвестная кучка-источник: {source!r}")
    if amount <= 0:
        raise ValueError(f"сумма перемещения должна быть больше нуля: {amount}")
    async with conn.transaction():
        await _lock_cash_moves(conn)
        balance = await get_holder_balance(conn, source)
        if amount > balance:
            raise CashMoveExceedsBalance(source, balance)
        return await conn.fetchval(
            """
            INSERT INTO cleaning_cashbook (kind, method, amount, comment, cash_holder)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            CASHBOOK_KIND_MOVE,
            cash_move_route(source),
            amount,
            comment,
            source,
        )


async def get_cash_move(
    conn: asyncpg.Connection, *, move_id: int
) -> asyncpg.Record | None:
    """Неудалённое перемещение: id, happened_at, cash_holder, method, amount, comment.

    None — нет такого id, это не перемещение или оно уже удалено.
    """
    return await conn.fetchrow(
        """
        SELECT id, happened_at, cash_holder, method, amount, comment
        FROM cleaning_cashbook
        WHERE id = $1 AND kind = $2 AND deleted_at IS NULL
        """,
        move_id,
        CASHBOOK_KIND_MOVE,
    )


async def check_cash_move_delete(
    conn: asyncpg.Connection, *, move_id: int
) -> asyncpg.Record | None:
    """Можно ли удалить перемещение — без записи (для шага подтверждения).

    Возвращает строку как get_cash_move; None — не найдено или уже удалено.
    Удаление вернёт деньги источнику и заберёт у получателя, поэтому в минус
    может уйти только получатель: тогда CashMoveDeleteGoesNegative.
    """
    move = await get_cash_move(conn, move_id=move_id)
    if move is None:
        return None
    receiver = _OTHER_HOLDER[move["cash_holder"]]
    after = await get_holder_balance(conn, receiver) - Decimal(move["amount"])
    if after < 0:
        raise CashMoveDeleteGoesNegative(receiver, after)
    return move


async def delete_cash_move(
    conn: asyncpg.Connection, *, move_id: int
) -> asyncpg.Record | None:
    """Удалить перемещение (deleted_at). Возвращает удалённую строку как get_cash_move.

    В транзакции под замком повторяет check_cash_move_delete: None — не найдено
    или уже удалено, CashMoveDeleteGoesNegative — ничего не меняется.
    """
    async with conn.transaction():
        await _lock_cash_moves(conn)
        if await check_cash_move_delete(conn, move_id=move_id) is None:
            return None
        return await conn.fetchrow(
            """
            UPDATE cleaning_cashbook
            SET deleted_at = $1
            WHERE id = $2 AND kind = $3 AND deleted_at IS NULL
            RETURNING id, happened_at, cash_holder, method, amount, comment
            """,
            datetime.now(timezone.utc),
            move_id,
            CASHBOOK_KIND_MOVE,
        )


async def list_recent_cash_moves(
    conn: asyncpg.Connection, *, limit: int = 10
) -> list[asyncpg.Record]:
    """Последние неудалённые перемещения, новые сверху: id, happened_at,
    cash_holder, method, amount, comment."""
    return await conn.fetch(
        """
        SELECT id, happened_at, cash_holder, method, amount, comment
        FROM cleaning_cashbook
        WHERE kind = $1 AND deleted_at IS NULL
        ORDER BY id DESC
        LIMIT $2
        """,
        CASHBOOK_KIND_MOVE,
        limit,
    )
