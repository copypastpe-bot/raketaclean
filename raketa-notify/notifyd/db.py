"""Доступ к схеме `notify`: почтовый ящик (`notify.outbox`) и справочник
маршрутов (`notify.routes`). Форма таблиц — `migrations/001_notify_schema.sql`,
здесь её не повторяем и не переизобретаем.

Ниже также чтения для сторожа (задача 7, `notifyd/watchdog.py`): пульс
админ-бота (`notify.service_heartbeats`, миграция 002) и существующие таблицы
ботов схемы `public` (`service_heartbeats`, `amocrm_api_state`,
`notification_outbox`) — сторож их только читает, права см. в 002.

Ближе к концу файла — `notify.incidents` (задача 8, `notifyd/incidents.py`):
открытие/напоминание/закрытие/эскалация. Все операции над одной строкой
инцидента написаны как один атомарный INSERT ... ON CONFLICT или UPDATE ...
WHERE ... RETURNING — тем же приёмом, что `db.claim_due` у почтальона:
условие проверяется под блокировкой строки, поэтому два одновременно
работающих цикла сторожа не заводят два инцидента и не шлют напоминание
дважды (см. комментарий к каждой функции и `notify_incidents_open_key_idx`
в миграции 001)."""

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
                       ref: Optional[str] = None,
                       reply_markup: Optional[dict] = None) -> int:
    """Положить новое событие в ящик — из самой службы (сторож, переходник
    журнала, инциденты задачи 8), а не от ботов: те кладут события своим
    отдельным клиентом (`notifications/notify_bus.py`,
    `raketa-admin-bot/adminbot/notify_bus.py`), сюда не заходя.

    `reply_markup` — опционально: большинство собственных событий службы
    (переходник журнала) кнопок не имеют и просто не передают этот параметр;
    у инцидентов (задача 8, `notifyd/incidents.py`) кнопки есть («сел
    разбираться», «отложить») — тот же JSON-словарь, что и у ботов
    (см. `_load_markup` в `notifyd/telegram.py`).

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
            INSERT INTO notify.outbox (kind, text, ref, source, next_try_at, expires_at,
                                       reply_markup)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
            """,
            kind, text, ref, source, now, expires_at, reply_markup,
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


# --------------------------------------------------------------------------
# Инциденты notify.incidents (задача 8): открытие, напоминание, признание,
# закрытие, эскалация. Каждая функция — один INSERT ... ON CONFLICT или
# UPDATE ... WHERE ... RETURNING: RETURNING отдаёт строку только тому
# вызову, который реально что-то поменял, поэтому «два одновременных
# прохода сторожа не заводят два инцидента» проверяется не в Python
# (не memory-флагом), а самой базой — под блокировкой строки.
# --------------------------------------------------------------------------

_INCIDENT_COLUMNS = ("id, key, level, address, state, detail, opened_at, acked_at, "
                    "ack_until, closed_at, escalated_at, last_notified_at, notify_count")


async def open_incident(pool: asyncpg.Pool, *, key: str, level: str, address: str,
                        detail: str, now: datetime) -> Optional[dict]:
    """Завести инцидент, если открытого/признанного с этим `key` ещё нет.

    Партиальный уникальный индекс `notify_incidents_open_key_idx` (миграция
    001, `UNIQUE (key) WHERE state <> 'closed'`) — тот самый механизм «одна
    открытая запись на поломку» из ТЗ. `ON CONFLICT DO NOTHING` — вернёт
    строку ТОЛЬКО если инцидент и правда только что заведён этим вызовом;
    если он уже был открыт (или признан), вызов не вставляет ничего и
    отдаёт None — вызывающий код (`notifyd.incidents`) тогда должен читать
    существующий инцидент отдельно (`claim_reminder_due`), а не заводить
    второе сообщение."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO notify.incidents (key, level, address, detail, opened_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (key) WHERE state <> 'closed' DO NOTHING
            RETURNING {_INCIDENT_COLUMNS}
            """,
            key, level, address, detail, now,
        )
    return dict(row) if row is not None else None


async def close_incident(pool: asyncpg.Pool, key: str, now: datetime) -> Optional[dict]:
    """Закрыть инцидент («починилось») — если он был открыт или признан.

    Возвращает строку только тому вызову, который его и закрыл: второй
    одновременный проход, увидевший ту же «проверка снова ок», найдёт
    `state` уже `'closed'` и получит None — второго «отбой, работает»
    не будет (проверка ТЗ «две одновременные проверки не порождают двух
    сообщений» применительно к выздоровлению)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE notify.incidents
            SET state = 'closed', closed_at = $2
            WHERE key = $1 AND state <> 'closed'
            RETURNING {_INCIDENT_COLUMNS}
            """,
            key, now,
        )
    return dict(row) if row is not None else None


async def claim_reminder_due(pool: asyncpg.Pool, *, key: str, now: datetime,
                             red_interval_sec: int, yellow_interval_sec: int,
                             yellow_cap: int) -> Optional[dict]:
    """Атомарно решить «пора напомнить» и сразу отметить попытку — одним
    UPDATE, а не «проверить, потом записать»: Postgres перепроверяет WHERE
    под блокировкой строки, поэтому два одновременных прохода не напомнят
    об одном и том же инциденте дважды (второй увидит уже обновлённый
    `last_notified_at` и не пройдёт условие по времени).

    Красный (`level='red'`): каждые `red_interval_sec` секунд, круглосуточно,
    без потолка — но не пока тишина признана и не истекла (`state='acked'
    AND ack_until > now`). Истёкшая тишина возвращает инцидент в `'open'`
    тем же UPDATE — «напоминания возвращаются», как того требует ТЗ.

    Жёлтый (`level='yellow'`): раз в `yellow_interval_sec` секунд, но не
    больше `yellow_cap` напоминаний всего (первое сообщение уже считается —
    `notify_count` считает все отправки одному инциденту, а не только
    повторы). Кнопка «отложить» не трогает `notify_count` — она только
    двигает `ack_until` (см. `ack_incident`), поэтому отложенное напоминание
    не расходует потолок.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE notify.incidents
            SET last_notified_at = $2, notify_count = notify_count + 1, state = 'open'
            WHERE key = $1
              AND state <> 'closed'
              AND (state = 'open' OR ack_until <= $2)
              AND (
                    (level = 'red'
                     AND (last_notified_at IS NULL
                          OR $2 - last_notified_at >= make_interval(secs => $3::double precision)))
                 OR (level = 'yellow' AND notify_count < $5
                     AND (last_notified_at IS NULL
                          OR $2 - last_notified_at >= make_interval(secs => $4::double precision)))
              )
            RETURNING {_INCIDENT_COLUMNS}
            """,
            key, now, red_interval_sec, yellow_interval_sec, yellow_cap,
        )
    return dict(row) if row is not None else None


async def ack_incident(pool: asyncpg.Pool, *, incident_id: int, until: datetime,
                       now: datetime) -> Optional[dict]:
    """Кнопка «сел разбираться» (красный) или «отложить» (жёлтый) — обе
    делают одно и то же: тише до `until`. Разница только в том, какой срок
    подставит вызывающий код (`notifyd.incidents.RED_ACK_SILENCE_SEC` или
    `YELLOW_SNOOZE_SEC`) и в тексте ответа кнопки."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE notify.incidents
            SET state = 'acked', acked_at = $3, ack_until = $2
            WHERE id = $1 AND state <> 'closed'
            RETURNING {_INCIDENT_COLUMNS}
            """,
            incident_id, until, now,
        )
    return dict(row) if row is not None else None


async def get_incident(pool: asyncpg.Pool, incident_id: int) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_INCIDENT_COLUMNS} FROM notify.incidents WHERE id = $1", incident_id)
    return dict(row) if row is not None else None


async def list_open_incidents(pool: asyncpg.Pool) -> list[dict]:
    """Открытые и признанные инциденты — для команды «что сейчас сломано»
    (задача 9). Признанный («сел разбираться», тишина ещё не истекла) всё
    равно считается открытым: поломка не устранена, просто владелец уже
    в курсе."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_INCIDENT_COLUMNS} FROM notify.incidents WHERE state <> 'closed' "
            f"ORDER BY opened_at")
    return [dict(row) for row in rows]


async def claim_escalations(pool: asyncpg.Pool, *, now: datetime,
                            threshold_sec: int) -> list[dict]:
    """Красные инциденты старше `threshold_sec`, ещё не эскалированные —
    атомарно отмечает `escalated_at` всем найденным сразу (не проверять их
    снова на следующем проходе).

    Какие из них и правда стоит дублировать в My_assistant (не все красные
    поломки «бьют по клиентам или заказам» — ТЗ, задача 8) решает
    вызывающий код (`notifyd.incidents.ESCALATE_KEYS`): сюда попадают ВСЕ
    красные инциденты старше часа без разбора ключа, включая те, что не
    выбирают эскалацию (например, пульс админ-бота, — это внутренний
    инструмент владельца, а не то, что бьёт по клиентам). Для них
    `escalated_at` тоже ставится, просто вызывающий код не шлёт по ним
    сообщение — так следующий проход не спрашивает про них снова."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            UPDATE notify.incidents
            SET escalated_at = $1
            WHERE state <> 'closed' AND level = 'red' AND escalated_at IS NULL
              AND $1 - opened_at >= make_interval(secs => $2::double precision)
            RETURNING {_INCIDENT_COLUMNS}
            """,
            now, threshold_sec,
        )
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Долги почты службы — для команды «что сейчас сломано» (задача 9).
# --------------------------------------------------------------------------

async def count_outbox_pending(pool: asyncpg.Pool) -> int:
    """Сколько событий в `notify.outbox` ждёт отправки прямо сейчас
    (не отправлено и не погашено; в работе с растущим `attempts` — тоже
    считается, статус всё равно `'pending'`, см. `db.postpone`). Репетиция
    (`dry_run=true`) не в счёт: она не про реальные долги доставки."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM notify.outbox WHERE status = 'pending' AND dry_run = false"
        )
