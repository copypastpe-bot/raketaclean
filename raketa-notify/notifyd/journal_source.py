"""Источник записей systemd journal — подменяемый (ТЗ 2026-09-18, задача 5).

Разработка идёт на macOS, где journald нет вовсе (ограничение среды из ТЗ).
Поэтому источник записей — интерфейс `JournalSource` с двумя реализациями:
настоящей (`SystemdJournalSource`, читает `journalctl` — так и написано в
ТЗ: «настоящий (чтение journalctl на сервере)») и поддельной для тестов
(`FakeJournalSource`, `tests/conftest.py` — тот же приём, что `FakeSender`
в `notifyd/telegram.py`).

Курсор не сохраняется на диск между перезапусками службы: при первом вызове
`SystemdJournalSource` запоминает курсор ТЕКУЩЕГО конца журнала и ничего не
возвращает, дальше читает только то, что появилось после него. Это значит,
что при перезапуске службы возможен короткий пробел (то, что случилось между
остановкой и стартом адаптера, в технический чат не попадёт) — осознанный
компромисс, а не недосмотр: сам journald ничего не теряет и не чистится,
и владелец в любой момент может посмотреть его руками (`journalctl -u ...`).
Тащить отдельное хранение курсора (файл на диске или новая таблица в схеме
notify) ради полноты необязательной для работы ботов функции для этой
задачи избыточно.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

log = logging.getLogger(__name__)

_CURSOR_MARKER = "-- cursor: "


@dataclass(frozen=True)
class JournalEntry:
    """Одна запись журнала как есть — без разбора уровня/модуля Python-
    логирования: это дело `notifyd.journal_adapter`, не источника."""

    unit: str
    timestamp: datetime
    message: str


class JournalSource(Protocol):
    """Всё, что адаптеру нужно от источника журнала — и ничего сверх того."""

    async def read_new(self) -> list[JournalEntry]: ...


class SystemdJournalSource:
    """Настоящий источник: `journalctl` для одной службы systemd.

    Права на чтение: члены группы `systemd-journal` могут читать журнал
    любых юнитов независимо от того, под каким пользователем они запущены
    (сам смысл этой группы) — юнит службы (`deploy/raketa-notify.service`)
    добавляет её через `SupplementaryGroups=`.
    """

    def __init__(self, unit: str, *, journalctl_bin: str = "journalctl") -> None:
        self.unit = unit
        self._bin = journalctl_bin
        self._cursor: Optional[str] = None

    async def read_new(self) -> list[JournalEntry]:
        if self._cursor is None:
            await self._establish_cursor()
            return []
        return await self._read_since_cursor()

    async def _establish_cursor(self) -> None:
        """Узнать курсор конца журнала, не читая ни одной старой записи.

        `-n 0 --show-cursor` печатает только служебную строку с курсором
        (не JSON — см. journalctl(1)), поэтому здесь свой, не-JSON разбор.
        """
        text = await self._run_raw("-n", "0", "--show-cursor")
        if text is None:
            return
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(_CURSOR_MARKER):
                self._cursor = line[len(_CURSOR_MARKER):].strip()
        if self._cursor is None:
            log.warning("notify: не удалось получить курсор журнала для %s "
                       "— попробую на следующем проходе", self.unit)

    async def _read_since_cursor(self) -> list[JournalEntry]:
        rows = await self._run_json("--after-cursor", self._cursor)
        entries: list[JournalEntry] = []
        for row in rows:
            cursor = row.get("__CURSOR")
            if cursor:
                self._cursor = cursor
            message = row.get("MESSAGE")
            if not isinstance(message, str):
                continue   # бинарные сообщения (массив байт) — не наш случай
            entries.append(JournalEntry(
                unit=self.unit,
                timestamp=_parse_realtime(row.get("__REALTIME_TIMESTAMP")),
                message=message,
            ))
        return entries

    async def _run_raw(self, *args: str) -> Optional[str]:
        proc = await self._spawn(*args)
        if proc is None:
            return None
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.warning("notify: journalctl (%s) для %s завершился с кодом %s: %s",
                       " ".join(args), self.unit, proc.returncode,
                       stderr.decode(errors="replace")[:300])
            return None
        return stdout.decode(errors="replace")

    async def _run_json(self, *args: str) -> list[dict]:
        text = await self._run_raw("-o", "json", *args)
        if text is None:
            return []
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue    # например, хвостовая "-- cursor: ..." не-JSON строка
        return rows

    async def _spawn(self, *args: str):
        try:
            return await asyncio.create_subprocess_exec(
                self._bin, "-u", self.unit, "--no-pager", *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except Exception:                                  # noqa: BLE001
            log.warning("notify: не смог запустить journalctl для %s", self.unit,
                       exc_info=True)
            return None


def _parse_realtime(raw: object) -> datetime:
    """`__REALTIME_TIMESTAMP` — микросекунды unix-времени строкой."""
    try:
        micros = int(raw)                                   # type: ignore[arg-type]
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)
