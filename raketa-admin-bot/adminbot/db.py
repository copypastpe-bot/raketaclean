"""Доступ к данным: чтение БД рабочего бота и работа со своей схемой adminbot.

ХАРД-ПРАВИЛО ПРОЕКТА: в схему `public` (таблицы рабочего бота) не пишем никогда —
здесь для неё есть только SELECT. Всё собственное состояние живёт в схеме `adminbot`.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from adminbot.models import AmoLink, Order
from adminbot.phone import last10

# Колонки adminbot.amo_links, которые разрешено менять через update_link.
_UPDATABLE_LINK_FIELDS = frozenset(
    {"phone10", "status", "path", "primary_lead_id", "real_lead_id", "question_msg_id", "last_error"}
)

# Заказы рабочего бота за период. Мастера: основной (orders.master_id) первым,
# затем помощники из order_masters. Адрес — из карточки клиента.
_SELECT_ORDERS = """
SELECT
    o.id,
    o.phone,
    o.phone_digits,
    o.customer_name,
    o.created_at,
    o.amount_total,
    o.rating_score,
    o.payment_method,
    o.awaiting_wire_payment,
    EXISTS (
        SELECT 1 FROM public.orders prev
        WHERE prev.phone_digits = o.phone_digits AND prev.created_at < o.created_at
    ) AS is_repeat_client,
    c.full_name AS client_full_name,
    COALESCE(NULLIF(TRIM(c.address), ''), NULLIF(TRIM(c.last_order_addr), '')) AS address,
    COALESCE((
        SELECT jsonb_agg(jsonb_build_array(m.name, m.phone) ORDER BY m.is_primary DESC, m.name)
        FROM (
            SELECT
                COALESCE(
                    NULLIF(TRIM(s.full_name), ''),
                    NULLIF(TRIM(CONCAT_WS(' ', s.first_name, s.last_name)), '')
                ) AS name,
                s.phone AS phone,
                (s.id = o.master_id) AS is_primary
            FROM public.staff s
            WHERE s.id = o.master_id
               OR s.id IN (SELECT om.master_id FROM public.order_masters om WHERE om.order_id = o.id)
        ) m
        WHERE m.name IS NOT NULL
    ), '[]'::jsonb) AS masters
FROM public.orders o
LEFT JOIN public.clients c ON c.id = o.client_id
WHERE o.created_at >= ($1::date AT TIME ZONE 'Europe/Moscow')
ORDER BY o.created_at, o.id
"""


async def _init_connection(conn: asyncpg.Connection) -> None:
    """jsonb ↔ dict без ручного json.dumps на каждом вызове."""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 5) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, init=_init_connection)


def _order_from_row(row: asyncpg.Record) -> Order:
    phone10 = last10(row["phone_digits"]) or last10(row["phone"])
    return Order(
        order_id=row["id"],
        phone10=phone10,
        created_at=row["created_at"],
        amount_total=Decimal(row["amount_total"] or 0),
        masters=[(str(name), phone) for name, phone in (row["masters"] or [])],
        rating_score=row["rating_score"],
        payment_method=row["payment_method"],
        awaiting_wire_payment=bool(row["awaiting_wire_payment"]),
        is_repeat_client=bool(row["is_repeat_client"]),
        client_name=(row["client_full_name"] or row["customer_name"]),
        address=row["address"],
    )


async def fetch_orders_since(bot_pool: asyncpg.Pool, since: date) -> list[Order]:
    """Все заказы бота начиная с даты `since` (по московскому времени)."""
    async with bot_pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_ORDERS, since)
    return [_order_from_row(row) for row in rows]


async def fetch_linked_order_ids(own_pool: asyncpg.Pool, order_ids: list[int]) -> set[int]:
    """Какие из заказов уже взяты в работу (есть строка в adminbot.amo_links)."""
    if not order_ids:
        return set()
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT order_id FROM adminbot.amo_links WHERE order_id = ANY($1::bigint[])", order_ids
        )
    return {row["order_id"] for row in rows}


async def fetch_unprocessed_orders(
    bot_pool: asyncpg.Pool, own_pool: asyncpg.Pool, since: date
) -> list[Order]:
    """Заказы с даты `since`, по которым робот ещё ничего не начинал."""
    orders = await fetch_orders_since(bot_pool, since)
    linked = await fetch_linked_order_ids(own_pool, [o.order_id for o in orders])
    return [o for o in orders if o.order_id not in linked]


def _link_from_row(row: Optional[asyncpg.Record]) -> Optional[AmoLink]:
    if row is None:
        return None
    checklist = row["checklist"]
    if isinstance(checklist, str):          # на случай пула без нашего json-кодека
        checklist = json.loads(checklist)
    return AmoLink(
        order_id=row["order_id"],
        phone10=row["phone10"],
        status=row["status"],
        path=row["path"],
        primary_lead_id=row["primary_lead_id"],
        real_lead_id=row["real_lead_id"],
        checklist=checklist or {},
        question_msg_id=row["question_msg_id"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_link(
    own_pool: asyncpg.Pool, order_id: int, phone10: Optional[str], status: str = "new"
) -> AmoLink:
    """Взять заказ в работу. Повторный вызов ничего не портит (идемпотентность)."""
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO adminbot.amo_links (order_id, phone10, status)
            VALUES ($1, $2, $3)
            ON CONFLICT (order_id) DO UPDATE SET updated_at = now()
            RETURNING *
            """,
            order_id, phone10 or "", status,
        )
    return _link_from_row(row)


async def get_link(own_pool: asyncpg.Pool, order_id: int) -> Optional[AmoLink]:
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM adminbot.amo_links WHERE order_id = $1", order_id)
    return _link_from_row(row)


async def update_link(own_pool: asyncpg.Pool, order_id: int, **fields: Any) -> Optional[AmoLink]:
    """Обновить разрешённые поля привязки. Имена колонок — только из белого списка."""
    unknown = set(fields) - _UPDATABLE_LINK_FIELDS
    if unknown:
        raise ValueError(f"Недопустимые поля привязки: {sorted(unknown)}")
    if not fields:
        return await get_link(own_pool, order_id)

    names = list(fields)
    assignments = ", ".join(f"{name} = ${i + 2}" for i, name in enumerate(names))
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE adminbot.amo_links SET {assignments}, updated_at = now() "
            f"WHERE order_id = $1 RETURNING *",
            order_id, *[fields[name] for name in names],
        )
    return _link_from_row(row)


async def mark_checklist_step(own_pool: asyncpg.Pool, order_id: int, step: str) -> None:
    """Отметить выполненный шаг чек-листа — робот продолжит с этого места после сбоя."""
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE adminbot.amo_links
            SET checklist = checklist || jsonb_build_object($2::text, to_jsonb(now())),
                updated_at = now()
            WHERE order_id = $1
            """,
            order_id, step,
        )


async def log_action(
    own_pool: asyncpg.Pool,
    *,
    order_id: int,
    action: str,
    dry_run: bool,
    amo_entity: Optional[str] = None,
    amo_id: Optional[int] = None,
    payload: Optional[dict] = None,
) -> None:
    """Записать в журнал, что робот сделал (или сделал бы в режиме репетиции)."""
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO adminbot.amo_actions (order_id, action, amo_entity, amo_id, dry_run, payload)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            order_id, action, amo_entity, amo_id, dry_run, payload,
        )


async def fetch_actions(own_pool: asyncpg.Pool, order_id: int) -> list[dict]:
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM adminbot.amo_actions WHERE order_id = $1 ORDER BY id", order_id
        )
    return [dict(row) for row in rows]


async def fetch_taken_lead_ids(own_pool: asyncpg.Pool, phone10: str,
                               exclude_order_id: int) -> set[int]:
    """Сделки, уже закреплённые за другими заказами этого клиента.

    Одна сделка не может закрывать два заказа: у клиента бывает несколько работ
    подряд, и каждой полагается своя сделка.
    """
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT primary_lead_id, real_lead_id
            FROM adminbot.amo_links
            WHERE phone10 = $1 AND order_id <> $2
            """,
            phone10, exclude_order_id,
        )
    taken: set[int] = set()
    for row in rows:
        taken.update(value for value in (row["primary_lead_id"], row["real_lead_id"]) if value)
    return taken


async def count_links_by_status(own_pool: asyncpg.Pool) -> dict[str, int]:
    """Сводка очереди для команды /status и вечерней сверки."""
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, count(*) AS n FROM adminbot.amo_links GROUP BY status"
        )
    return {row["status"]: row["n"] for row in rows}


async def apply_migration(pool: asyncpg.Pool, sql_path: str) -> None:
    """Применить SQL-файл миграции (используется в тестах и при развёртывании)."""
    with open(sql_path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    async with pool.acquire() as conn:
        await conn.execute(sql)
