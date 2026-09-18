"""Доступ к схеме `notify`: почтовый ящик (`notify.outbox`) и справочник
маршрутов (`notify.routes`). Форма таблиц — `migrations/001_notify_schema.sql`,
здесь её не повторяем и не переизобретаем.

`notify.incidents` сюда не входит — это отдельная задача (8), не эта.

Ниже также чтения для сторожа (задача 7, `notifyd/watchdog.py`): пульс
админ-бота (`notify.service_heartbeats`, миграция 002) и существующие таблицы
ботов схемы `public` (`service_heartbeats`, `amocrm_api_state`,
`notification_outbox`) — сторож их только читает, права см. в 002.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    """jsonb <-> dict без ручного json.loads на каждый вызов (приём из adminbot/db.py)."""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 5) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, init=_init_connection)


# --------------------------------------------------------------------------
# Почтовый ящик notify.outbox
# --------------------------------------------------------------------------

async def insert_event(pool: asyncpg.Pool, *, kind: str, text: str, source: str,
                       now: datetime, expires_at: datetime,
                       ref: Optional[str] = None) -> int:
    """Положить новое событие в ящик — из самой службы (сторож, переходник
    журнала), а не от ботов: те кладут события своим отдельным клиентом
    (`notifications/notify_bus.py`, `raketa-admin-bot/adminbot/notify_bus.py`),
    сюда не заходя. `reply_markup` здесь не параметр: у событий самой службы
    кнопок не бывает.

    `next_try_at` ставится в `now` явно, а не отдаётся дефолту колонки
    (`DEFAULT now()`) — источники времени разные: вызывающий код (почтальон,
    переходник журнала) может работать на подложенных часах в тестах, а
    дефолт колонки — это всегда настоящее время самого Postgres. Если бы
    `next_try_at` брался из дефолта, «созревшесть» события в `claim_due`
    сравнивалась бы с ЧУЖИМИ часами, независимо от того, какое `now`
    подложено в тесте вызывающему коду."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO notify.outbox (kind, text, ref, source, next_try_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            kind, text, ref, source, now, expires_at,
        )


# Аренда строки на время попытки доставки. Не путать с BACKOFF_SEC в
# notifyd/postman.py (та пауза — после ПОДТВЕРЖДЁННОЙ неудачи). Эта — страховка
# от двух одновременно работающих циклов почтальона: строка помечена «занята»
# ближайшие CLAIM_LEASE_SEC секунд, и если процесс упадёт посреди отправки не
# отметившись, другой (или тот же после перезапуска) проход всё равно её увидит,
# когда аренда истечёт.
async def claim_due(pool: asyncpg.Pool, *, now: datetime, limit: int,
                    lease_seconds: int) -> list[dict]:
    """Атомарно забрать созревшие боевые события в работу.

    `FOR UPDATE SKIP LOCKED` внутри одного UPDATE — два процесса, глядящие
    в ящик одновременно, разбирают разные строки, а не одну и ту же дважды.
    Репетиции (`dry_run = true` в самой строке) сюда не попадают: боевой проход
    такие строки не трогает вовсе (комментарий к колонке в миграции 001).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH claim AS (
                SELECT id
                FROM notify.outbox
                WHERE status = 'pending'
                  AND dry_run = false
                  AND next_try_at <= $1
                ORDER BY id
                LIMIT $2
                FOR UPDATE SKIP LOCKED
            )
            UPDATE notify.outbox AS o
            SET next_try_at = $1 + make_interval(secs => $3::double precision)
            FROM claim
            WHERE o.id = claim.id
            RETURNING o.id, o.kind, o.text, o.reply_markup, o.ref, o.source,
                      o.attempts, o.expires_at
            """,
            now, limit, lease_seconds,
        )
    return [dict(row) for row in rows]


async def peek_due(pool: asyncpg.Pool, *, now: datetime, limit: int) -> list[dict]:
    """Только посмотреть, что созрело — ничего не занимая.

    Только для репетиции: репетиция не должна отбирать работу у боевого
    прохода, поэтому здесь простой SELECT без FOR UPDATE и без побочных эффектов.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, kind, text, reply_markup, ref, source, attempts, expires_at
            FROM notify.outbox
            WHERE status = 'pending'
              AND dry_run = false
              AND next_try_at <= $1
            ORDER BY id
            LIMIT $2
            """,
            now, limit,
        )
    return [dict(row) for row in rows]


async def mark_sent(pool: asyncpg.Pool, outbox_id: int, message_id: Optional[int],
                    now: datetime) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE notify.outbox
            SET status = 'sent', sent_at = $2, message_id = $3
            WHERE id = $1
            """,
            outbox_id, now, message_id,
        )


async def postpone(pool: asyncpg.Pool, outbox_id: int, next_try_at: datetime,
                   error: str) -> None:
    """Попытка не удалась — считаем её и назначаем следующую (приём из книги
    долгов админ-бота: attempts растёт, next_try_at сдвигается по backoff)."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE notify.outbox
            SET attempts = attempts + 1, next_try_at = $2, error = $3
            WHERE id = $1
            """,
            outbox_id, next_try_at, error[:500],
        )


async def mark_dropped(pool: asyncpg.Pool, outbox_id: int, error: str) -> None:
    """Событие погашено: протухло или маршрут выключен. `sent_at`/`message_id`
    остаются пустыми — их смысл строго «доставлено», а не «разобрано»."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE notify.outbox
            SET status = 'dropped', error = $2
            WHERE id = $1
            """,
            outbox_id, error[:500],
        )


# --------------------------------------------------------------------------
# Справочник маршрутов notify.routes
# --------------------------------------------------------------------------

_ROUTE_COLUMNS = "kind, address, level, tag, enabled, comment, updated_at"


async def get_route(pool: asyncpg.Pool, kind: str) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_ROUTE_COLUMNS} FROM notify.routes WHERE kind = $1", kind,
        )
    return dict(row) if row is not None else None


async def list_routes(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT {_ROUTE_COLUMNS} FROM notify.routes ORDER BY kind")
    return [dict(row) for row in rows]


async def upsert_route_address(pool: asyncpg.Pool, kind: str, address: str) -> None:
    """Завести маршрут (если его не было) или поменять адрес существующего.

    Единственная команда, которая может СОЗДАТЬ строку: без адреса маршрут
    не имеет смысла, а остальные поля (уровень/тег/включённость) берут
    разумные значения по умолчанию из самой таблицы.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO notify.routes (kind, address)
            VALUES ($1, $2)
            ON CONFLICT (kind) DO UPDATE
                SET address = $2, updated_at = now()
            """,
            kind, address,
        )


async def update_route_level(pool: asyncpg.Pool, kind: str, level: str) -> bool:
    """True — маршрут существовал и обновлён; False — такого вида события нет."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE notify.routes SET level = $2, updated_at = now()
            WHERE kind = $1
            RETURNING kind
            """,
            kind, level,
        )
    return row is not None


async def update_route_tag(pool: asyncpg.Pool, kind: str, tag: Optional[str]) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE notify.routes SET tag = $2, updated_at = now()
            WHERE kind = $1
            RETURNING kind
            """,
            kind, tag,
        )
    return row is not None


async def set_route_enabled(pool: asyncpg.Pool, kind: str, enabled: bool) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE notify.routes SET enabled = $2, updated_at = now()
            WHERE kind = $1
            RETURNING kind
            """,
            kind, enabled,
        )
    return row is not None


# --------------------------------------------------------------------------
# Чтения для сторожа (задача 7): пульс ботов и существующие таблицы ботов.
# --------------------------------------------------------------------------

async def fetch_heartbeat(pool: asyncpg.Pool, *, table: str, service_key: str) -> Optional[dict]:
    """Одна строка пульса — `table` это `public.service_heartbeats` (рабочий
    и клиентский боты) или `notify.service_heartbeats` (админ-бот, миграция
    002: писать в `public` ему нельзя). `table` — только имя из кода этого
    модуля, никогда пользовательский ввод, поэтому f-строка безопасна (тот же
    приём, что у `table` в raketa-admin-bot/adminbot/db.py)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT service_key, display_name, status, last_seen_at "
            f"FROM {table} WHERE service_key = $1",
            service_key,
        )
    return dict(row) if row is not None else None


async def ping_database(pool: asyncpg.Pool) -> bool:
    """«База отвечает» — тривиальный SELECT 1 через тот же пул, что у всей
    службы. Таймаут — забота вызывающего кода (asyncio.wait_for снаружи)."""
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT 1") == 1


async def fetch_amocrm_last_poll(pool: asyncpg.Pool, *, stream: str = "lead_events") -> Optional[datetime]:
    """Когда опрос amoCRM последний раз успешно прошёл цикл.

    `amocrm_api_state.updated_at` (bot.py:_amocrm_set_cursor) обновляется на
    КАЖДЫЙ успешный проход `_amocrm_poll_new_leads_once`, даже если новых
    событий не было — а значит застывшая отметка и есть точный сигнал того,
    что цикл `amocrm_api_polling_loop` встал (факт 6 ТЗ: он выходит навсегда
    при ошибке авторизации, ничего больше не пишет). Нет строки вовсе — опрос
    либо выключен, либо ещё не сделал ни одного цикла."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT updated_at FROM public.amocrm_api_state WHERE stream = $1", stream,
        )


async def fetch_dispatch_stats(pool: asyncpg.Pool, *, now: datetime) -> dict:
    """Состояние рассыльщика сообщений клиентам (`notifications/outbox.py`):
    когда последний раз что-то реально ушло (`sent_at` ставится один раз,
    при первой успешной отправке — mark_outbox_sent, COALESCE) и сколько
    созревших писем ждёт (status='pending' и время подошло — тот же фильтр,
    что у самого рассыльщика в pick_ready_batch)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT MAX(sent_at) FROM public.notification_outbox
                 WHERE sent_at IS NOT NULL) AS last_sent_at,
                (SELECT COUNT(*) FROM public.notification_outbox
                 WHERE status = 'pending' AND scheduled_at <= $1) AS pending_due
            """,
            now,
        )
    return {"last_sent_at": row["last_sent_at"], "pending_due": int(row["pending_due"])}
