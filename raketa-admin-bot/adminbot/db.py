"""Доступ к данным: чтение БД рабочего бота и работа со своей схемой adminbot.

ХАРД-ПРАВИЛО ПРОЕКТА: в схему `public` (таблицы рабочего бота) не пишем никогда —
здесь для неё есть только SELECT. Всё собственное состояние живёт в схеме `adminbot`.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional, Sequence

import asyncpg

from adminbot.amo import ids
from adminbot.models import AmoLink, AutocallLead, CalendarLink, CarpetLink, Order
from adminbot.phone import last10

# Колонки adminbot.amo_links, которые разрешено менять через update_link.
_UPDATABLE_LINK_FIELDS = frozenset(
    {"phone10", "status", "path", "primary_lead_id", "real_lead_id", "question",
     "question_msg_id", "last_error"}
)

# Два потока работы — две таблицы связок. Номера `orders.id` и `cleaning_orders.id`
# пересекаются: заказ №5 и уборка №5 существуют одновременно, и в общей таблице
# слились бы в одну работу. Строение таблиц одинаковое, поэтому запросы ниже
# принимают имя таблицы, а не дублируются.
#
# Имена берутся только отсюда — снаружи их не задают, поэтому подстановка в текст
# запроса безопасна.
LINKS_TABLE = "adminbot.amo_links"
ACTIONS_TABLE = "adminbot.amo_actions"
CLEANING_LINKS_TABLE = "adminbot.cleaning_links"
CLEANING_ACTIONS_TABLE = "adminbot.cleaning_actions"

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
WHERE ($1::date IS NULL OR o.created_at >= ($1::date AT TIME ZONE 'Europe/Moscow'))
  AND ($2::bigint[] IS NULL OR o.id = ANY($2::bigint[]))
ORDER BY o.created_at, o.id
"""

# Уборки клининг-контура за период. Отличия от химчистки:
# - адрес свой, в самом заказе (у химчистки его берут из карточки клиента);
# - мастер один — бригадир, и он идёт только в примечание сделки: «Специалистом»
#   у уборки всегда стоит Ольга (решение владельца 2026-09-10);
# - оплат может быть несколько строк; основной считается первая по номеру —
#   так же считает и сам бот при начислении бонусов;
# - `deleted_at` не пустой — уборку удалили, в амо её проводить нельзя;
# - «повторный клиент» смотрит обе таблицы бота: человек мог сначала заказать
#   химчистку, а уборку — впервые, и «сарафаном» это уже не назвать.
_SELECT_CLEANING_ORDERS = """
SELECT
    co.id,
    co.happened_at,
    co.total_amount,
    co.address,
    c.full_name AS client_full_name,
    c.phone AS client_phone,
    c.phone_digits AS client_phone_digits,
    NULLIF(TRIM(CONCAT_WS(' ', f.fn, f.ln)), '') AS foreman_name,
    f.phone AS foreman_phone,
    (
        SELECT p.method
        FROM public.cleaning_order_payments p
        WHERE p.order_id = co.id
        ORDER BY p.id
        LIMIT 1
    ) AS payment_method,
    (
        EXISTS (
            SELECT 1 FROM public.cleaning_orders prev
            WHERE prev.client_id = co.client_id
              AND prev.deleted_at IS NULL
              AND prev.happened_at < co.happened_at
        )
        OR EXISTS (
            SELECT 1 FROM public.orders po
            WHERE po.phone_digits = c.phone_digits
              AND po.created_at < co.happened_at
        )
    ) AS is_repeat_client
FROM public.cleaning_orders co
JOIN public.clients c ON c.id = co.client_id
LEFT JOIN public.cleaning_foremen f ON f.id = co.foreman_id
WHERE co.deleted_at IS NULL
  AND ($1::date IS NULL OR co.happened_at >= ($1::date AT TIME ZONE 'Europe/Moscow'))
  AND ($2::bigint[] IS NULL OR co.id = ANY($2::bigint[]))
ORDER BY co.happened_at, co.id
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
        rows = await conn.fetch(_SELECT_ORDERS, since, None)
    return [_order_from_row(row) for row in rows]


async def fetch_orders_by_ids(bot_pool: asyncpg.Pool, order_ids: Sequence[int]) -> list[Order]:
    """Заказы по номерам — наблюдателю, чтобы вернуться к незавершённым."""
    if not order_ids:
        return []
    async with bot_pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_ORDERS, None, list(order_ids))
    return [_order_from_row(row) for row in rows]


def _cleaning_order_from_row(row: asyncpg.Record) -> Order:
    """Уборка в том же виде, в каком движок получает заказ химчистки.

    «Услуга» и «Специалист» проставлены здесь, а не выведены из мастера: у уборки
    их задал владелец раз и навсегда, а бригадир меняется от смены к смене.
    Оценки клиента у клининга нет вовсе — значит, задачу «Получить ОС» робот не
    закрывает (решение владельца №9 без изменений).
    """
    phone10 = last10(row["client_phone_digits"]) or last10(row["client_phone"])
    foreman = row["foreman_name"]
    return Order(
        order_id=row["id"],
        kind="cleaning",
        phone10=phone10,
        created_at=row["happened_at"],
        amount_total=Decimal(row["total_amount"] or 0),
        masters=[(str(foreman), row["foreman_phone"])] if foreman else [],
        rating_score=None,
        payment_method=row["payment_method"],
        awaiting_wire_payment=False,
        is_repeat_client=bool(row["is_repeat_client"]),
        client_name=row["client_full_name"],
        address=row["address"],
        service_kind="cleaning",
        specialist_enums=(ids.SPECIALIST_ENUM_CLEANING,),
    )


async def fetch_cleaning_orders_since(bot_pool: asyncpg.Pool, since: date) -> list[Order]:
    """Все уборки начиная с даты `since` (по московскому времени)."""
    async with bot_pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_CLEANING_ORDERS, since, None)
    return [_cleaning_order_from_row(row) for row in rows]


async def fetch_cleaning_orders_by_ids(
    bot_pool: asyncpg.Pool, order_ids: Sequence[int]
) -> list[Order]:
    """Уборки по номерам — наблюдателю, чтобы вернуться к незавершённым."""
    if not order_ids:
        return []
    async with bot_pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_CLEANING_ORDERS, None, list(order_ids))
    return [_cleaning_order_from_row(row) for row in rows]


async def fetch_linked_order_ids(own_pool: asyncpg.Pool, order_ids: list[int],
                                 *, table: str = LINKS_TABLE) -> set[int]:
    """Какие из заказов уже взяты в работу (есть строка в таблице связок)."""
    if not order_ids:
        return set()
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT order_id FROM {table} WHERE order_id = ANY($1::bigint[])", order_ids
        )
    return {row["order_id"] for row in rows}


async def fetch_unprocessed_orders(
    bot_pool: asyncpg.Pool, own_pool: asyncpg.Pool, since: date
) -> list[Order]:
    """Заказы с даты `since`, по которым робот ещё ничего не начинал."""
    orders = await fetch_orders_since(bot_pool, since)
    linked = await fetch_linked_order_ids(own_pool, [o.order_id for o in orders])
    return [o for o in orders if o.order_id not in linked]


async def fetch_unprocessed_cleaning_orders(
    bot_pool: asyncpg.Pool, own_pool: asyncpg.Pool, since: date
) -> list[Order]:
    """Уборки с даты `since`, по которым робот ещё ничего не начинал."""
    orders = await fetch_cleaning_orders_since(bot_pool, since)
    linked = await fetch_linked_order_ids(own_pool, [o.order_id for o in orders],
                                          table=CLEANING_LINKS_TABLE)
    return [o for o in orders if o.order_id not in linked]


def _link_from_row(row: Optional[asyncpg.Record]) -> Optional[AmoLink]:
    if row is None:
        return None
    checklist = row["checklist"]
    if isinstance(checklist, str):          # на случай пула без нашего json-кодека
        checklist = json.loads(checklist)
    question = row["question"]
    if isinstance(question, str):
        question = json.loads(question)
    return AmoLink(
        order_id=row["order_id"],
        phone10=row["phone10"],
        status=row["status"],
        path=row["path"],
        primary_lead_id=row["primary_lead_id"],
        real_lead_id=row["real_lead_id"],
        checklist=checklist or {},
        question=question,
        question_msg_id=row["question_msg_id"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_link(
    own_pool: asyncpg.Pool, order_id: int, phone10: Optional[str], status: str = "new",
    *, table: str = LINKS_TABLE,
) -> AmoLink:
    """Взять заказ в работу. Повторный вызов ничего не портит (идемпотентность)."""
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO {table} (order_id, phone10, status)
            VALUES ($1, $2, $3)
            ON CONFLICT (order_id) DO UPDATE SET updated_at = now()
            RETURNING *
            """,
            order_id, phone10 or "", status,
        )
    return _link_from_row(row)


async def get_link(own_pool: asyncpg.Pool, order_id: int,
                   *, table: str = LINKS_TABLE) -> Optional[AmoLink]:
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT * FROM {table} WHERE order_id = $1", order_id)
    return _link_from_row(row)


async def update_link(own_pool: asyncpg.Pool, order_id: int, *, table: str = LINKS_TABLE,
                      **fields: Any) -> Optional[AmoLink]:
    """Обновить разрешённые поля привязки. Имена колонок — только из белого списка."""
    unknown = set(fields) - _UPDATABLE_LINK_FIELDS
    if unknown:
        raise ValueError(f"Недопустимые поля привязки: {sorted(unknown)}")
    if not fields:
        return await get_link(own_pool, order_id, table=table)

    names = list(fields)
    assignments = ", ".join(f"{name} = ${i + 2}" for i, name in enumerate(names))
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE {table} SET {assignments}, updated_at = now() "
            f"WHERE order_id = $1 RETURNING *",
            order_id, *[fields[name] for name in names],
        )
    return _link_from_row(row)


async def mark_checklist_step(own_pool: asyncpg.Pool, order_id: int, step: str,
                              *, table: str = LINKS_TABLE) -> None:
    """Отметить выполненный шаг чек-листа — робот продолжит с этого места после сбоя."""
    async with own_pool.acquire() as conn:
        await conn.execute(
            f"""
            UPDATE {table}
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
    table: str = ACTIONS_TABLE,
) -> None:
    """Записать в журнал, что робот сделал (или сделал бы в режиме репетиции)."""
    async with own_pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO {table} (order_id, action, amo_entity, amo_id, dry_run, payload)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            order_id, action, amo_entity, amo_id, dry_run, payload,
        )


async def fetch_actions(own_pool: asyncpg.Pool, order_id: int,
                        *, table: str = ACTIONS_TABLE) -> list[dict]:
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM {table} WHERE order_id = $1 ORDER BY id", order_id
        )
    return [dict(row) for row in rows]


async def fetch_taken_lead_ids(own_pool: asyncpg.Pool, phone10: str,
                               exclude_order_id: int,
                               *, table: str = LINKS_TABLE) -> set[int]:
    """Сделки, уже закреплённые за другими работами этого клиента.

    Одна сделка не может закрывать две работы: у клиента бывает несколько работ
    подряд, и каждой полагается своя сделка.

    Смотрим ОБЕ таблицы связок. Уборка и химчистка одному клиенту в один день —
    это две сделки (памятка владельца 2026-09-02), и сделку, занятую химчисткой,
    уборка брать не должна, и наоборот. Номер работы исключается только в своей
    таблице: в чужой такой же номер — совсем другая работа.
    """
    other = CLEANING_LINKS_TABLE if table == LINKS_TABLE else LINKS_TABLE
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT primary_lead_id, real_lead_id
            FROM {table}
            WHERE phone10 = $1 AND order_id <> $2
            UNION
            SELECT primary_lead_id, real_lead_id
            FROM {other}
            WHERE phone10 = $1
            """,
            phone10, exclude_order_id,
        )
    taken: set[int] = set()
    for row in rows:
        taken.update(value for value in (row["primary_lead_id"], row["real_lead_id"]) if value)
    return taken


async def fetch_link_ids_by_status(
    own_pool: asyncpg.Pool, statuses: Sequence[str], *, table: str = LINKS_TABLE
) -> list[int]:
    """Номера заказов, работа по которым ещё не закончена."""
    if not statuses:
        return []
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT order_id FROM {table} WHERE status = ANY($1::text[]) ORDER BY order_id",
            list(statuses),
        )
    return [row["order_id"] for row in rows]


async def fetch_links_for_orders(
    own_pool: asyncpg.Pool, order_ids: Sequence[int], *, table: str = LINKS_TABLE
) -> list[AmoLink]:
    """Всё, что робот записал по этим заказам, — сырьё для вечерней сводки."""
    if not order_ids:
        return []
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM {table} WHERE order_id = ANY($1::bigint[]) ORDER BY order_id",
            list(order_ids),
        )
    return [_link_from_row(row) for row in rows]


async def count_links_by_status(own_pool: asyncpg.Pool,
                                *, table: str = LINKS_TABLE) -> dict[str, int]:
    """Сводка очереди для команды /status и вечерней сверки."""
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT status, count(*) AS n FROM {table} GROUP BY status"
        )
    return {row["status"]: row["n"] for row in rows}


# --- ковры от партнёра ---

# Колонки adminbot.carpet_links, которые разрешено менять.
_UPDATABLE_CARPET_FIELDS = frozenset(
    {"phone10", "status", "path", "lead_id", "primary_lead_id", "question",
     "question_msg_id", "last_error", "source_file"}
)


def _carpet_from_row(row: Optional[asyncpg.Record]) -> Optional[CarpetLink]:
    if row is None:
        return None
    checklist = row["checklist"]
    if isinstance(checklist, str):
        checklist = json.loads(checklist)
    question = row["question"]
    if isinstance(question, str):
        question = json.loads(question)
    return CarpetLink(
        partner_id=row["partner_id"],
        phone10=row["phone10"],
        status=row["status"],
        path=row["path"],
        lead_id=row["lead_id"],
        primary_lead_id=row["primary_lead_id"],
        checklist=checklist or {},
        question=question,
        question_msg_id=row["question_msg_id"],
        last_error=row["last_error"],
        source_file=row["source_file"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_carpet_link(own_pool: asyncpg.Pool, partner_id: int,
                             phone10: Optional[str],
                             source_file: Optional[str] = None,
                             row_data: Optional[dict] = None) -> CarpetLink:
    """Взять строку отчёта в работу. Повторный вызов ничего не портит.

    Саму строку сохраняем рядом: письмо будет разобрано и помечено прочитанным
    задолго до того, как сейлзбот заведёт сделку, и продолжить будет нечем.
    """
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO adminbot.carpet_links (partner_id, phone10, source_file, row_data)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (partner_id) DO UPDATE SET updated_at = now()
            RETURNING *
            """,
            partner_id, phone10 or "", source_file, row_data,
        )
    return _carpet_from_row(row)


async def fetch_pending_carpet_rows(own_pool: asyncpg.Pool,
                                    statuses: Sequence[str]) -> list[tuple[dict, CarpetLink]]:
    """Незавершённые заказы партнёра вместе с сохранёнными строками отчёта."""
    if not statuses:
        return []
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM adminbot.carpet_links "
            "WHERE status = ANY($1::text[]) AND row_data IS NOT NULL ORDER BY partner_id",
            list(statuses),
        )
    pending = []
    for row in rows:
        data = row["row_data"]
        if isinstance(data, str):
            data = json.loads(data)
        pending.append((data, _carpet_from_row(row)))
    return pending


async def get_carpet_link(own_pool: asyncpg.Pool, partner_id: int) -> Optional[CarpetLink]:
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM adminbot.carpet_links WHERE partner_id = $1", partner_id)
    return _carpet_from_row(row)


async def update_carpet_link(own_pool: asyncpg.Pool, partner_id: int,
                             **fields: Any) -> Optional[CarpetLink]:
    unknown = set(fields) - _UPDATABLE_CARPET_FIELDS
    if unknown:
        raise ValueError(f"Недопустимые поля ковровой привязки: {sorted(unknown)}")
    if not fields:
        return await get_carpet_link(own_pool, partner_id)

    names = list(fields)
    assignments = ", ".join(f"{name} = ${i + 2}" for i, name in enumerate(names))
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE adminbot.carpet_links SET {assignments}, updated_at = now() "
            f"WHERE partner_id = $1 RETURNING *",
            partner_id, *[fields[name] for name in names],
        )
    return _carpet_from_row(row)


async def mark_carpet_step(own_pool: asyncpg.Pool, partner_id: int, step: str) -> None:
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE adminbot.carpet_links
            SET checklist = checklist || jsonb_build_object($2::text, to_jsonb(now())),
                updated_at = now()
            WHERE partner_id = $1
            """,
            partner_id, step,
        )


async def log_carpet_action(
    own_pool: asyncpg.Pool, *, partner_id: int, action: str, dry_run: bool,
    amo_entity: Optional[str] = None, amo_id: Optional[int] = None,
    payload: Optional[dict] = None,
) -> None:
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO adminbot.carpet_actions
                (partner_id, action, amo_entity, amo_id, dry_run, payload)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            partner_id, action, amo_entity, amo_id, dry_run, payload,
        )


async def fetch_carpet_taken_leads(own_pool: asyncpg.Pool, phone10: str,
                                   exclude_partner_id: int) -> set[int]:
    """Ковровые сделки, уже закреплённые за другими заказами того же клиента."""
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT lead_id FROM adminbot.carpet_links
            WHERE phone10 = $1 AND partner_id <> $2 AND lead_id IS NOT NULL
            """,
            phone10, exclude_partner_id,
        )
    return {row["lead_id"] for row in rows}


async def fetch_carpet_links_by_status(own_pool: asyncpg.Pool,
                                       statuses: Sequence[str]) -> list[CarpetLink]:
    """Заказы партнёра, работа по которым ещё не закончена."""
    if not statuses:
        return []
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM adminbot.carpet_links WHERE status = ANY($1::text[]) "
            "ORDER BY partner_id",
            list(statuses),
        )
    return [_carpet_from_row(row) for row in rows]


async def count_carpet_links_by_status(own_pool: asyncpg.Pool) -> dict[str, int]:
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, count(*) AS n FROM adminbot.carpet_links GROUP BY status")
    return {row["status"]: row["n"] for row in rows}


async def remember_letter(own_pool: asyncpg.Pool, uid: str, subject: Optional[str],
                          files: Sequence[str], rows_total: int) -> None:
    """Записать, что письмо разобрано: страховка на случай сбоя пометки в почте.

    Письмо было отложено и владелец снял отложение — отметки об этом снимаются:
    письмо проведено, и вспоминать, что когда-то оно ждало решения, незачем.
    """
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO adminbot.carpet_letters (uid, subject, files, rows_total)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (uid) DO UPDATE SET processed_at = now(),
                held_reason = NULL, held_at = NULL, released_at = NULL
            """,
            uid, subject, list(files), rows_total,
        )


async def letter_state(own_pool: asyncpg.Pool, uid: str) -> Optional[str]:
    """Что робот помнит о письме: `processed`, `held`, `released` или ничего.

    `None` — письмо роботу незнакомо. `released` — владелец снял отложение:
    письмо проводится, и порог строк к нему уже не применяется (иначе снятие
    ничего бы не меняло и робот отложил бы письмо снова).
    """
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT held_reason, released_at FROM adminbot.carpet_letters WHERE uid = $1",
            uid)
    if row is None:
        return None
    if row["held_reason"] is None:
        return "processed"
    return "held" if row["released_at"] is None else "released"


async def hold_letter(own_pool: asyncpg.Pool, uid: str, subject: Optional[str],
                      files: Sequence[str], rows_total: int, reason: str) -> None:
    """Отложить письмо: робот его не проводит и ждёт решения владельца."""
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO adminbot.carpet_letters
                (uid, subject, files, rows_total, held_reason, held_at)
            VALUES ($1, $2, $3, $4, $5, now())
            ON CONFLICT (uid) DO UPDATE SET subject = EXCLUDED.subject,
                files = EXCLUDED.files, rows_total = EXCLUDED.rows_total,
                held_reason = EXCLUDED.held_reason, held_at = now(), released_at = NULL
            """,
            uid, subject, list(files), rows_total, reason,
        )


async def release_letter(own_pool: asyncpg.Pool, uid: str) -> bool:
    """Снять отложение. Отвечает, было ли что снимать."""
    async with own_pool.acquire() as conn:
        released = await conn.fetchval(
            """
            UPDATE adminbot.carpet_letters SET released_at = now()
            WHERE uid = $1 AND held_reason IS NOT NULL AND released_at IS NULL
            RETURNING uid
            """,
            uid)
    return released is not None


async def held_letters(own_pool: asyncpg.Pool) -> list[dict]:
    """Письма, которые робот отложил и ждёт решения владельца."""
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT uid, subject, rows_total, held_reason, held_at
            FROM adminbot.carpet_letters
            WHERE held_reason IS NOT NULL AND released_at IS NULL
            ORDER BY held_at
            """)
    return [dict(row) for row in rows]


async def get_setting(own_pool: asyncpg.Pool, key: str) -> Optional[str]:
    """Настройка, которую владелец меняет из Telegram (например, пауза)."""
    async with own_pool.acquire() as conn:
        return await conn.fetchval("SELECT value FROM adminbot.settings WHERE key = $1", key)


async def set_setting(own_pool: asyncpg.Pool, key: str, value: str) -> None:
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO adminbot.settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            key, value,
        )


async def apply_migration(pool: asyncpg.Pool, sql_path: str) -> None:
    """Применить SQL-файл миграции (используется в тестах и при развёртывании)."""
    with open(sql_path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    async with pool.acquire() as conn:
        await conn.execute(sql)


# --- календарь (этап 2) ---

# Колонки adminbot.gcal_events, которые разрешено менять.
_UPDATABLE_GCAL_FIELDS = frozenset(
    {"kind", "status", "skip_reason", "path", "phone10", "client_name", "district",
     "services", "order_date", "event_data", "primary_lead_id", "real_lead_id",
     "order_id", "question", "question_msg_id", "done_msg_id", "last_error"}
)


def _as_dict(value: Any) -> Optional[dict]:
    """jsonb приходит словарём, но пул без нашего кодека отдал бы строку."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _calendar_from_row(row: Optional[asyncpg.Record]) -> Optional[CalendarLink]:
    if row is None:
        return None
    checklist = row["checklist"]
    if isinstance(checklist, str):
        checklist = json.loads(checklist)
    question = row["question"]
    if isinstance(question, str):
        question = json.loads(question)
    return CalendarLink(
        event_id=row["event_id"],
        kind=row["kind"],
        status=row["status"],
        phone10=row["phone10"],
        order_date=row["order_date"],
        client_name=row["client_name"],
        district=row["district"],
        services=tuple(row["services"] or ()),
        event_data=_as_dict(row["event_data"]),
        skip_reason=row["skip_reason"],
        path=row["path"],
        primary_lead_id=row["primary_lead_id"],
        real_lead_id=row["real_lead_id"],
        order_id=row["order_id"],
        checklist=checklist or {},
        question=question,
        question_msg_id=row["question_msg_id"],
        done_msg_id=row["done_msg_id"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def get_calendar_link(own_pool: asyncpg.Pool, event_id: str) -> Optional[CalendarLink]:
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM adminbot.gcal_events WHERE event_id = $1", event_id)
    return _calendar_from_row(row)


async def create_calendar_link(own_pool: asyncpg.Pool, event_id: str, *, kind: str,
                               phone10: Optional[str] = None,
                               **fields: Any) -> CalendarLink:
    """Взять запись календаря в работу. Повторный обмен ничего не портит.

    Разобранную запись сохраняем рядом (`event_data`): робот может ждать
    автосделку сейлзбота дольше, чем живёт содержимое одного обмена, и продолжать
    цепочку будет нечем.
    """
    unknown = set(fields) - _UPDATABLE_GCAL_FIELDS
    if unknown:
        raise ValueError(f"Недопустимые поля записи календаря: {sorted(unknown)}")

    columns = ["event_id", "kind", "phone10", *fields]
    values = [event_id, kind, phone10, *[fields[name] for name in fields]]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO adminbot.gcal_events ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT (event_id) DO UPDATE SET updated_at = now()
            RETURNING *
            """,
            *values,
        )
    return _calendar_from_row(row)


async def update_calendar_link(own_pool: asyncpg.Pool, event_id: str,
                               **fields: Any) -> Optional[CalendarLink]:
    unknown = set(fields) - _UPDATABLE_GCAL_FIELDS
    if unknown:
        raise ValueError(f"Недопустимые поля записи календаря: {sorted(unknown)}")
    if not fields:
        return await get_calendar_link(own_pool, event_id)

    names = list(fields)
    assignments = ", ".join(f"{name} = ${i + 2}" for i, name in enumerate(names))
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE adminbot.gcal_events SET {assignments}, updated_at = now() "
            f"WHERE event_id = $1 RETURNING *",
            event_id, *[_gcal_value(name, fields[name]) for name in names],
        )
    return _calendar_from_row(row)


def _gcal_value(name: str, value: Any) -> Any:
    """Кортеж услуг в text[] уходит списком: asyncpg кортежи не принимает."""
    if name == "services" and isinstance(value, tuple):
        return list(value)
    return value


async def mark_calendar_step(own_pool: asyncpg.Pool, event_id: str, step: str) -> None:
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE adminbot.gcal_events
            SET checklist = checklist || jsonb_build_object($2::text, to_jsonb(now())),
                updated_at = now()
            WHERE event_id = $1
            """,
            event_id, step,
        )


async def log_calendar_action(
    own_pool: asyncpg.Pool, *, event_id: str, action: str, dry_run: bool,
    amo_entity: Optional[str] = None, amo_id: Optional[int] = None,
    payload: Optional[dict] = None,
) -> None:
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO adminbot.gcal_actions
                (event_id, action, amo_entity, amo_id, dry_run, payload)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            event_id, action, amo_entity, amo_id, dry_run, payload,
        )


async def fetch_calendar_taken_leads(own_pool: asyncpg.Pool, phone10: str,
                                     exclude_event_id: str) -> set[int]:
    """Сделки, уже закреплённые за ДРУГИМИ записями календаря того же клиента.

    Заказы бота здесь не учитываются намеренно: запись календаря и заказ из бота —
    обычно один и тот же заказ, и общая сделка у них правильная.
    """
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT real_lead_id, primary_lead_id FROM adminbot.gcal_events
            WHERE phone10 = $1 AND event_id <> $2
            """,
            phone10, exclude_event_id,
        )
    taken: set[int] = set()
    for row in rows:
        taken.update(lead for lead in (row["real_lead_id"], row["primary_lead_id"]) if lead)
    return taken


async def fetch_pending_calendar_links(own_pool: asyncpg.Pool,
                                       statuses: Sequence[str]) -> list[CalendarLink]:
    """Записи календаря, работа по которым ещё не закончена."""
    if not statuses:
        return []
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM adminbot.gcal_events WHERE status = ANY($1::text[]) "
            "ORDER BY order_date NULLS LAST, event_id",
            list(statuses),
        )
    return [_calendar_from_row(row) for row in rows]


async def fetch_calendar_links_without_report(own_pool: asyncpg.Pool,
                                              since: datetime) -> list[CalendarLink]:
    """Записи, по которым работа сделана, а владельцу не отчитались.

    Обычно таких нет: отметка ставится сразу после того, как Telegram принял
    сообщение. Строка появляется здесь, когда связь оборвалась ровно в момент
    отправки, — и без этого запроса отчёт пропал бы навсегда (2026-09-02).

    `since` отсекает древность: колонка done_msg_id появилась 2026-08-28, и у
    всего, что проведено раньше, она пуста по историческим причинам.
    """
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM adminbot.gcal_events
            WHERE status = 'done' AND done_msg_id IS NULL AND updated_at >= $1
            ORDER BY updated_at
            """,
            since,
        )
    return [_calendar_from_row(row) for row in rows]


async def count_calendar_links_by_status(own_pool: asyncpg.Pool) -> dict[str, int]:
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, count(*) AS n FROM adminbot.gcal_events GROUP BY status")
    return {row["status"]: row["n"] for row in rows}


async def get_calendar_cursor(own_pool: asyncpg.Pool, calendar_id: str,
                              *, inherit_legacy: bool = False,
                              ) -> tuple[Optional[str], Optional[date]]:
    """Закладка обмена с Google по одному календарю: (токен, дата включения).

    `inherit_legacy` ставит только первый календарь из настроек. До миграции 008
    закладка была одна и хранилась без имени календаря; терять её нельзя —
    иначе Google вместо изменений отдаст весь календарь целиком. Поэтому
    безымянная строка достаётся первому календарю: переименовываем её на месте,
    одним запросом, и повторно она уже никому не попадётся.
    """
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT sync_token, sync_from FROM adminbot.gcal_cursor WHERE calendar_id = $1",
            calendar_id)
        if row is None and inherit_legacy:
            row = await conn.fetchrow(
                """
                UPDATE adminbot.gcal_cursor
                SET calendar_id = $1, updated_at = now()
                WHERE calendar_id = ''
                RETURNING sync_token, sync_from
                """,
                calendar_id)
    if row is None:
        return None, None
    return row["sync_token"], row["sync_from"]


async def save_calendar_cursor(own_pool: asyncpg.Pool, calendar_id: str,
                               sync_token: Optional[str], sync_from: date) -> None:
    """Сохранить закладку календаря. В репетиции сюда не приходят — там память."""
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO adminbot.gcal_cursor (calendar_id, sync_token, sync_from)
            VALUES ($1, $2, $3)
            ON CONFLICT (calendar_id) DO UPDATE
            SET sync_token = EXCLUDED.sync_token,
                sync_from = EXCLUDED.sync_from,
                updated_at = now()
            """,
            calendar_id, sync_token, sync_from,
        )


async def find_calendar_link_by_question_msg(own_pool: asyncpg.Pool,
                                             message_id: int) -> Optional[CalendarLink]:
    """Запись, по которой владельцу отправлена именно эта карточка.

    Нажатие приходит без идентификатора записи: он у Google длинный, а в кнопку
    Telegram влезает 64 байта на всё. Зато известно сообщение, на котором нажали.
    """
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM adminbot.gcal_events WHERE question_msg_id = $1 "
            "ORDER BY updated_at DESC LIMIT 1",
            message_id,
        )
    return _calendar_from_row(row)


async def delete_calendar_link(own_pool: asyncpg.Pool, event_id: str) -> None:
    """Забыть запись календаря. Только для ручного разбора последствий."""
    async with own_pool.acquire() as conn:
        await conn.execute("DELETE FROM adminbot.gcal_events WHERE event_id = $1", event_id)


async def fetch_calendar_actions(own_pool: asyncpg.Pool, event_id: str,
                                 limit: int = 20) -> list[dict]:
    """Что робот делал по записи календаря — для сообщения владельцу."""
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT action, amo_entity, amo_id, dry_run, payload, created_at
            FROM adminbot.gcal_actions WHERE event_id = $1
            ORDER BY id DESC LIMIT $2
            """,
            event_id, limit,
        )
    return [dict(row) for row in rows]


# --- автозвонок по заявке с сайта (autocall) ---

# Колонки adminbot.autocall_leads, которые разрешено менять.
_UPDATABLE_AUTOCALL_FIELDS = frozenset(
    {"phone10", "status", "attempts_total", "manager_failures", "client_failures",
     "next_action_at", "call_id", "called_at", "last_error"}
)


def _autocall_from_row(row: Optional[asyncpg.Record]) -> Optional[AutocallLead]:
    if row is None:
        return None
    return AutocallLead(
        lead_id=row["lead_id"],
        status=row["status"],
        phone10=row["phone10"],
        attempts_total=row["attempts_total"],
        manager_failures=row["manager_failures"],
        client_failures=row["client_failures"],
        next_action_at=row["next_action_at"],
        call_id=row["call_id"],
        called_at=row["called_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def get_autocall_lead(own_pool: asyncpg.Pool, lead_id: int) -> Optional[AutocallLead]:
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM adminbot.autocall_leads WHERE lead_id = $1", lead_id)
    return _autocall_from_row(row)


async def create_autocall_lead(own_pool: asyncpg.Pool, lead_id: int, *,
                               phone10: Optional[str] = None,
                               **fields: Any) -> AutocallLead:
    """Завести цепочку попыток. Повторный проход наблюдателя ничего не портит."""
    unknown = set(fields) - _UPDATABLE_AUTOCALL_FIELDS
    if unknown:
        raise ValueError(f"Недопустимые поля цепочки автозвонка: {sorted(unknown)}")

    columns = ["lead_id", "phone10", *fields]
    values = [lead_id, phone10, *[fields[name] for name in fields]]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO adminbot.autocall_leads ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT (lead_id) DO UPDATE SET updated_at = now()
            RETURNING *
            """,
            *values,
        )
    return _autocall_from_row(row)


async def update_autocall_lead(own_pool: asyncpg.Pool, lead_id: int,
                               **fields: Any) -> Optional[AutocallLead]:
    unknown = set(fields) - _UPDATABLE_AUTOCALL_FIELDS
    if unknown:
        raise ValueError(f"Недопустимые поля цепочки автозвонка: {sorted(unknown)}")
    if not fields:
        return await get_autocall_lead(own_pool, lead_id)

    names = list(fields)
    assignments = ", ".join(f"{name} = ${i + 2}" for i, name in enumerate(names))
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE adminbot.autocall_leads SET {assignments}, updated_at = now() "
            f"WHERE lead_id = $1 RETURNING *",
            lead_id, *[fields[name] for name in names],
        )
    return _autocall_from_row(row)


async def fetch_due_autocall_leads(own_pool: asyncpg.Pool, statuses: Sequence[str],
                                   now: datetime) -> list[AutocallLead]:
    """Незаконченные цепочки, у которых срок подошёл. Просроченные — первыми.

    Без срока (next_action_at пуст) — действовать сразу: цепочку только завели.
    """
    if not statuses:
        return []
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM adminbot.autocall_leads
            WHERE status = ANY($1::text[])
              AND (next_action_at IS NULL OR next_action_at <= $2)
            ORDER BY next_action_at NULLS FIRST, lead_id
            """,
            list(statuses), now,
        )
    return [_autocall_from_row(row) for row in rows]


async def log_autocall_action(own_pool: asyncpg.Pool, *, lead_id: int, action: str,
                              dry_run: bool, payload: Optional[dict] = None) -> None:
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO adminbot.autocall_actions (lead_id, action, dry_run, payload)
            VALUES ($1, $2, $3, $4)
            """,
            lead_id, action, dry_run, payload,
        )


async def fetch_autocall_actions(own_pool: asyncpg.Pool, lead_id: int,
                                 limit: int = 20) -> list[dict]:
    """Что робот делал по заявке — свежее первым, для сообщения владельцу."""
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT action, dry_run, payload, created_at
            FROM adminbot.autocall_actions WHERE lead_id = $1
            ORDER BY id DESC LIMIT $2
            """,
            lead_id, limit,
        )
    return [dict(row) for row in rows]


async def add_owner_letter(own_pool: asyncpg.Pool, *, chat_id: int, kind: str,
                           ref: Optional[str], text: str,
                           reply_markup: Optional[dict], expires_at: datetime,
                           next_try_at: datetime, last_error: Optional[str]) -> int:
    """Положить недоставленное сообщение в долг. Возвращает его номер."""
    async with own_pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO adminbot.owner_outbox
                (chat_id, kind, ref, text, reply_markup, expires_at, next_try_at,
                 attempts, last_error)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8)
            RETURNING id
            """,
            chat_id, kind, ref, text,
            json.dumps(reply_markup) if reply_markup is not None else None,
            expires_at, next_try_at, last_error,
        )


async def fetch_due_owner_letters(own_pool: asyncpg.Pool, now: datetime,
                                  limit: int = 20) -> list[dict]:
    """Созревшие долги, старые первыми: владелец читает их в порядке событий."""
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, chat_id, kind, ref, text, reply_markup, attempts, expires_at
            FROM adminbot.owner_outbox
            WHERE sent_at IS NULL AND dropped_at IS NULL AND next_try_at <= $1
            ORDER BY id
            LIMIT $2
            """,
            now, limit,
        )
    letters = []
    for row in rows:
        letter = dict(row)
        letter["reply_markup"] = _as_dict(letter["reply_markup"])
        letters.append(letter)
    return letters


async def fetch_owner_letters_for(own_pool: asyncpg.Pool, ref: str) -> list[dict]:
    """Незаконченные долги по одной записи: они ещё ждут отправки.

    Нужно при ручном разборе последствий: если робот забывает запись, её
    недоставленные письма теряют смысл, и владелец должен видеть, что именно
    он гасит вместе с ней.
    """
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, kind, ref, attempts, next_try_at, expires_at,
                   left(text, 80) AS preview
            FROM adminbot.owner_outbox
            WHERE ref = $1 AND sent_at IS NULL AND dropped_at IS NULL
            ORDER BY id
            """,
            ref,
        )
    return [dict(row) for row in rows]


async def mark_owner_letter_sent(own_pool: asyncpg.Pool, letter_id: int,
                                 message_id: Optional[int], now: datetime) -> None:
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE adminbot.owner_outbox
            SET sent_at = $2, message_id = $3
            WHERE id = $1
            """,
            letter_id, now, message_id,
        )


async def postpone_owner_letter(own_pool: asyncpg.Pool, letter_id: int,
                                next_try_at: datetime, error: str) -> None:
    """Попытка не удалась — считаем её и назначаем следующую."""
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE adminbot.owner_outbox
            SET attempts = attempts + 1, next_try_at = $2, last_error = $3
            WHERE id = $1
            """,
            letter_id, next_try_at, error[:500],
        )


async def drop_owner_letter(own_pool: asyncpg.Pool, letter_id: int, now: datetime,
                            reason: str) -> None:
    """Долг больше не нужен: протух или нужда в нём отпала."""
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE adminbot.owner_outbox
            SET dropped_at = $2, drop_reason = $3
            WHERE id = $1
            """,
            letter_id, now, reason,
        )


async def count_owner_letters_waiting(own_pool: asyncpg.Pool) -> int:
    """Сколько сообщений владельцу сейчас ждут отправки — строка для /status."""
    async with own_pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT count(*) FROM adminbot.owner_outbox
            WHERE sent_at IS NULL AND dropped_at IS NULL
            """) or 0


async def get_autocall_cursor(own_pool: asyncpg.Pool) -> Optional[datetime]:
    """Курсор опроса амо: с какого created_at читать. None — курсора ещё нет."""
    async with own_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT created_from FROM adminbot.autocall_cursor WHERE id = 1")
    return None if row is None else row["created_from"]


async def save_autocall_cursor(own_pool: asyncpg.Pool, created_from: datetime) -> None:
    """Сохранить курсор. В репетиции сюда не приходят — там хранилище в памяти."""
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO adminbot.autocall_cursor (id, created_from)
            VALUES (1, $1)
            ON CONFLICT (id) DO UPDATE
            SET created_from = EXCLUDED.created_from,
                updated_at = now()
            """,
            created_from,
        )
