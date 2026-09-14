"""Пульт управления функцией amo_sync: пауза владельца и состояние очереди.

Выключателей два, и они разные по смыслу:

1) настройка сервиса `AMO_SYNC_ENABLED` — «функция вообще существует».
   Меняется только на сервере при запуске, командой из Telegram её не поднять;
2) пауза владельца — «сейчас не трогай CRM». Живёт в базе (схема adminbot),
   переживает перезапуск сервиса и переключается командами /pause и /resume.

Работать роботу можно, только когда включено первое и снята вторая.
"""

from __future__ import annotations

from typing import Optional, Protocol

import asyncpg

from adminbot import db

# Ключ паузы в adminbot.settings.
PAUSE_KEY = "amo_sync_paused"


class ControlPanel(Protocol):
    """Что нужно боту и наблюдателю от пульта — и ничего сверх того."""

    async def is_paused(self) -> bool: ...

    async def set_paused(self, paused: bool) -> None: ...

    async def queue_counts(self) -> dict[str, int]: ...


class MemoryControlPanel:
    """Пульт в памяти: для тестов и разовых прогонов без базы."""

    def __init__(self, *, paused: bool = False, counts: Optional[dict[str, int]] = None) -> None:
        self.paused = paused
        self.counts = counts or {}

    async def is_paused(self) -> bool:
        return self.paused

    async def set_paused(self, paused: bool) -> None:
        self.paused = paused

    async def queue_counts(self) -> dict[str, int]:
        return dict(self.counts)


class PgControlPanel:
    """Боевой пульт: пауза хранится в базе, поэтому переживает перезапуск."""

    def __init__(self, own_pool: asyncpg.Pool) -> None:
        self._pool = own_pool

    async def is_paused(self) -> bool:
        return await db.get_setting(self._pool, PAUSE_KEY) == "1"

    async def set_paused(self, paused: bool) -> None:
        await db.set_setting(self._pool, PAUSE_KEY, "1" if paused else "0")

    async def queue_counts(self) -> dict[str, int]:
        return await db.count_links_by_status(self._pool)


def sync_allowed(*, sync_enabled: bool, control: ControlPanel):
    """Собрать выключатель для наблюдателя: включено настройкой И не на паузе."""

    async def allowed() -> bool:
        if not sync_enabled:
            return False
        return not await control.is_paused()

    return allowed
