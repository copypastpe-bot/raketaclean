"""Пульс админ-бота (ТЗ 2026-09-18 «оповещения», задача 6).

Раз в минуту отмечается «я жив» — но не в `public.service_heartbeats`
(там уже отмечается рабочий бот и, отдельно, клиентский, факт 7 ТЗ): этому
боту нельзя писать в схему `public` вообще (хард-правило проекта,
raketa-admin-bot/CLAUDE.md, технически закреплено с 2026-08-26 — роль
`adminbot` имеет там только SELECT). Своё состояние живёт в отдельной
таблице `notify.service_heartbeats` (миграция `raketa-notify/migrations/
002_watchdog_schema.sql`) — тот же формат колонок, что у `public.
service_heartbeats`, но в схеме `notify`, где у этого бота уже есть право
писать (тем же путём, что и `notify_bus.put_event`). Сторож
(`raketa-notify/notifyd/watchdog.py`, задача 7) читает обе таблицы одним
и тем же приёмом.

Как и `notify_bus.py`, отдельного модуля на оба бота нет — рабочий бот
пишет свой пульс сам, в bot.py (задача 6 того же ТЗ), в `public.
service_heartbeats`, своим кодом: общего пути деплоя у ботов нет
(rsync/git pull, см. notify_bus.py).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# service_key — та же строка, что читает сторож (ADMIN_BOT_SERVICE_KEY,
# raketa-notify/notifyd/watchdog.py). Общего модуля нет (см. docstring),
# поэтому значения совпадают буквально, не по импорту.
SERVICE_KEY = "raketa-admin-bot"
DISPLAY_NAME = "Админ-бот"

DEFAULT_POLL_INTERVAL_SEC = 60


class HeartbeatWriter:
    """Один фоновый цикл — тот же приём `run_forever(stop)`, что у остальных
    компонентов `App` (mail, carpet_watcher, ...): выключатель проверяется
    снаружи (build_app не создаёт объект вовсе, если он выключен), поэтому
    здесь никакого kill switch нет — только сам цикл."""

    def __init__(self, *, pool: Any,
                 now: Optional[Callable[[], datetime]] = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC) -> None:
        self.pool = pool
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.poll_interval_sec = poll_interval_sec

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.write_once()
            except Exception:                            # noqa: BLE001
                log.exception("Пульс админ-бота: проход не удался")
            await self.sleep(self.poll_interval_sec)

    async def write_once(self) -> None:
        now = self._now()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO notify.service_heartbeats
                    (service_key, display_name, status, last_seen_at, last_ok_at, updated_at)
                VALUES ($1, $2, 'ok', $3, $3, $3)
                ON CONFLICT (service_key) DO UPDATE
                SET display_name = EXCLUDED.display_name,
                    status = 'ok',
                    last_seen_at = EXCLUDED.last_seen_at,
                    last_ok_at = EXCLUDED.last_ok_at,
                    updated_at = EXCLUDED.updated_at
                """,
                SERVICE_KEY, DISPLAY_NAME, now,
            )
