"""Почтальон: разбирает notify.outbox, смотрит маршрут в notify.routes,
отправляет и отмечает.

Механика повторов и протухания перенесена из книги долгов админ-бота
(adminbot/tg/outbox.py, ТЗ факт 2): та же пауза между попытками, то же правило
«протухло — отмена». Не перенесено — проверка «нужна ли ещё» (`still_needed`
у админ-бота): там она была нужна для карточек с кнопками у конкретных фич
владельца, здесь в ТЗ этой задачи её нет, и придумывать не будем.

Отличие от книги долгов, вызванное самой архитектурой (вариант А из ТЗ):
на один и тот же ящик может смотреть больше одного работающего цикла
почтальона (два процесса, деплой поверх работающей службы), поэтому взятие
строки в работу — не обычный SELECT, а атомарный UPDATE с
`FOR UPDATE SKIP LOCKED` (db.claim_due) с временной «арендой» строки.

Режим репетиции не пишет в notify.outbox и notify.routes вообще ничего —
только читает и логирует, что бы отправила. Это четвёртый случай в проекте,
когда репетиция обязана не оставлять следов (предыдущие три раза это было
дефектом, найденным по факту).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from notifyd import db
from notifyd.telegram import Sender

log = logging.getLogger(__name__)

# Паузы между повторами — те же числа, что в книге долгов админ-бота
# (adminbot/tg/outbox.py: BACKOFF_SEC).
BACKOFF_SEC: tuple[int, ...] = (60, 180, 600, 1800)

# Аренда строки на время одной попытки доставки (см. комментарий в db.claim_due).
CLAIM_LEASE_SEC = 120

BATCH_LIMIT = 20
POLL_INTERVAL_SEC = 60

# Вид события без маршрута не теряется — уходит в технический журнал с этим тегом.
UNKNOWN_KIND_ADDRESS = "tech_journal"
UNKNOWN_KIND_TAG = "неизвестный-вид"


@dataclass(frozen=True)
class Target:
    """Куда физически слать сообщение для одного адреса справочника."""

    sender: Sender
    chat_id: Any


@dataclass(frozen=True)
class _Route:
    address: Optional[str]   # None — маршрут выключен, событие гасим
    tag: Optional[str]
    unknown_kind: bool


class Postman:
    """Один цикл разбора ящика. Можно поднять несколько экземпляров сразу —
    `db.claim_due` гарантирует, что одно событие достанется только одному."""

    def __init__(self, *, pool: Any, targets: dict[str, Target], enabled: bool,
                 dry_run: bool, now: Optional[Callable[[], datetime]] = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 poll_interval_sec: int = POLL_INTERVAL_SEC,
                 batch_limit: int = BATCH_LIMIT,
                 claim_lease_sec: int = CLAIM_LEASE_SEC) -> None:
        self.pool = pool
        self.targets = targets
        self.enabled = enabled
        self.dry_run = dry_run
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.poll_interval_sec = poll_interval_sec
        self.batch_limit = batch_limit
        self.claim_lease_sec = claim_lease_sec

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        if not self.enabled:
            log.info("notify: служба выключена (выключатель в настройках), ничего "
                     "не отправляю")
            while stop is None or not stop.is_set():
                await self.sleep(self.poll_interval_sec)
            return

        if self.dry_run:
            log.info("notify: режим репетиции — только смотрю и логирую, ящик не трогаю")

        while stop is None or not stop.is_set():
            try:
                if self.dry_run:
                    await self.rehearse_once()
                else:
                    await self.deliver_due()
            except Exception:                            # noqa: BLE001
                log.exception("notify: проход почтальона не удался")
            await self.sleep(self.poll_interval_sec)

    async def deliver_due(self) -> int:
        """Боевой проход: забрать созревшее, разобрать маршрут, отправить, отметить."""
        now = self._now()
        claimed = await db.claim_due(self.pool, now=now, limit=self.batch_limit,
                                     lease_seconds=self.claim_lease_sec)
        delivered = 0
        for row in claimed:
            if await self._settle(row, now):
                delivered += 1
        return delivered

    async def rehearse_once(self) -> int:
        """Репетиция: то же самое чтение, но без единой записи и без Telegram."""
        now = self._now()
        due = await db.peek_due(self.pool, now=now, limit=self.batch_limit)
        for row in due:
            if row["expires_at"] <= now:
                log.info("notify (репетиция): событие %s (%s) протухло бы, не "
                         "отправляю", row["id"], row["kind"])
                continue
            route = await self._resolve_route(row["kind"])
            if route.address is None:
                log.info("notify (репетиция): маршрут %s выключен — событие %s "
                         "погасло бы", row["kind"], row["id"])
                continue
            if route.unknown_kind:
                log.warning("notify (репетиция): вид события без маршрута — %s "
                           "(событие %s) — увела бы в технический журнал",
                           row["kind"], row["id"])
            target = self.targets.get(route.address)
            where = route.address if target is not None else f"{route.address} (нет отправителя)"
            log.info("notify (репетиция): отправила бы в %s (kind=%s%s): %s",
                     where, row["kind"],
                     f" тег=#{route.tag}" if route.tag else "", _short(row["text"]))
        return len(due)

    async def _settle(self, row: dict, now: datetime) -> bool:
        if row["expires_at"] <= now:
            log.warning("notify: событие %s (%s) протухло, не отправляю",
                       row["id"], row["kind"])
            await db.mark_dropped(self.pool, row["id"], "протухло")
            return False

        route = await self._resolve_route(row["kind"])
        if route.address is None:
            log.info("notify: маршрут %s выключен, событие %s погашено",
                     row["kind"], row["id"])
            await db.mark_dropped(self.pool, row["id"], "маршрут выключен")
            return False

        if route.unknown_kind:
            log.warning("notify: вид события без маршрута — %s (событие %s), "
                       "увожу в технический журнал с тегом #%s",
                       row["kind"], row["id"], route.tag)

        target = self.targets.get(route.address)
        if target is None:
            log.error("notify: для адреса %s не настроен отправитель, откладываю "
                     "событие %s", route.address, row["id"])
            await db.postpone(self.pool, row["id"], self._next_try(row, now),
                              f"нет отправителя для адреса {route.address}")
            return False

        text = row["text"] + (f"\n\n#{route.tag}" if route.tag else "")
        try:
            message_id = await target.sender.send(target.chat_id, text,
                                                   row.get("reply_markup"))
        except Exception as exc:                         # noqa: BLE001 — Telegram падает
            log.warning("notify: не ушло событие %s (%s): %s",
                       row["id"], row["kind"], exc)
            await db.postpone(self.pool, row["id"], self._next_try(row, now),
                              f"{type(exc).__name__}: {exc}")
            return False

        await db.mark_sent(self.pool, row["id"], message_id, now)
        log.info("notify: событие %s (%s) доставлено в %s", row["id"], row["kind"],
                 route.address)
        return True

    async def _resolve_route(self, kind: str) -> _Route:
        route = await db.get_route(self.pool, kind)
        if route is None:
            return _Route(address=UNKNOWN_KIND_ADDRESS, tag=UNKNOWN_KIND_TAG,
                         unknown_kind=True)
        if not route["enabled"]:
            return _Route(address=None, tag=route["tag"], unknown_kind=False)
        return _Route(address=route["address"], tag=route["tag"], unknown_kind=False)

    def _next_try(self, row: dict, now: datetime) -> datetime:
        attempts = int(row.get("attempts") or 0)
        pause = BACKOFF_SEC[min(attempts, len(BACKOFF_SEC) - 1)]
        return now + timedelta(seconds=pause)


def _short(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
