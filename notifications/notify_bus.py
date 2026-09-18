"""Клиент «положить событие» — общий контракт со службой оповещений (шина).

ТЗ `docs/plans/2026-09-18-notifications-bus.md`, задача 2. Единственная
обязанность модуля: одна вставка строки в `notify.outbox` — никаких походов
в сеть, никакой доставки, никакого разбора маршрутов. Кто и как унесёт это
письмо, решает служба-почтальон (каталог `raketa-notify/`, отдельная
территория и отдельные задачи того же ТЗ). Форма таблицы — источник истины
из `raketa-notify/migrations/001_notify_schema.sql`, здесь она не изобретается
заново.

Админ-бот пишет тем же контрактом (таблицей), но своим отдельным клиентом
`raketa-admin-bot/adminbot/notify_bus.py` — общий код между ботами не едет
единым путём деплоя, поэтому общим остаётся только контракт.

Где событие рождается внутри уже открытой транзакции (деньги, заказы) —
сюда передаётся тот же `conn`, и запись ложится в неё же: если бизнес-
операция откатится, откатится и событие. Тот же приём уже применялся для
регистра удалённых заказов (`bot.py::_record_deleted_order`) и оправдал себя.

Вставка обёрнута в свою `conn.transaction()`. Если `conn` уже находится
внутри транзакции вызывающего кода, asyncpg превращает этот блок в
SAVEPOINT: неудачная вставка откатывается только до него, не трогая то, что
вокруг, — а значит внешняя транзакция способна закоммититься, даже если
это письмо не легло. Если же исключение всё-таки случилось, оно гасится
здесь и не выходит наружу: оповещение не имеет права уронить основную
работу бота, только предупреждение в журнал.

Существующие отправки этим модулем пока не пользуются — на этапе задачи 2
его вызывают только тесты.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import asyncpg

log = logging.getLogger(__name__)

# Значение колонки `notify.outbox.source` для этого бота. CHECK-ограничение
# таблицы допускает только 'worker' / 'adminbot' / 'notify'
# (raketa-notify/migrations/001_notify_schema.sql).
_SOURCE = "worker"

# Срок годности события по умолчанию. Какой вид события живёт дольше или
# короче — решение справочника `notify.routes` (задача 4 того же ТЗ), не
# этого клиента. Сутки — тот же срок, что и в `adminbot.owner_outbox`
# (DEFAULT_TTL_SEC, raketa-admin-bot/adminbot/tg/outbox.py), проверенном
# месяцем боевой работы.
_DEFAULT_TTL = timedelta(hours=24)


async def put_event(
    conn: asyncpg.Connection,
    *,
    kind: str,
    text: str,
    ref: Optional[Any] = None,
    reply_markup: Optional[Mapping[str, Any]] = None,
) -> Optional[int]:
    """Положить событие в общий почтовый ящик `notify.outbox`.

    `conn` — уже открытое соединение (например, из `pool.acquire()`), при
    необходимости внутри чужой транзакции: см. docstring модуля. `kind` —
    вид события для справочника маршрутов; `ref` — на что событие ссылается
    (номер заказа, сделки), приводится к строке. `reply_markup` — кнопки как
    обычный словарь; в этом боте нет кодека asyncpg для jsonb, поэтому здесь
    он сериализуется вручную (как и в `notifications/outbox.py`).

    Возвращает id новой строки в ящике или `None`, если вставка не удалась —
    в этом случае в журнал уходит предупреждение, а вызывающий код
    продолжает работу как ни в чём не бывало.
    """
    expires_at = datetime.now(timezone.utc) + _DEFAULT_TTL
    markup_json = (
        json.dumps(reply_markup, ensure_ascii=False) if reply_markup is not None else None
    )
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO notify.outbox (kind, text, reply_markup, ref, source, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                kind,
                text,
                markup_json,
                None if ref is None else str(ref),
                _SOURCE,
                expires_at,
            )
    except Exception:  # noqa: BLE001 — оповещение не имеет права уронить основную работу
        log.exception("Оповещения: событие (%s) не положено в ящик", kind)
        return None
    return row["id"]
