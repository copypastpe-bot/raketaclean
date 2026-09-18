"""My_admin: приём обновлений — кнопки инцидентов и команда «что сейчас
сломано» (ТЗ 2026-09-18, задачи 8-9).

У бота My_admin нет другого слушателя (уточнение координатора 5 ТЗ):
почтальон (`notifyd.postman` через `notifyd.telegram.AiogramSender`) этим
же токеном только ОТПРАВЛЯЕТ. Эта служба сама поднимает `aiogram.Dispatcher`
на ТОМ ЖЕ объекте `Bot` (см. `AiogramSender.bot`) — один `Bot` одновременно
на приём (`start_polling`) и отправку — обычное для aiogram использование,
тот же приём, что в `adminbot/main.py` (один `self.bot` и для рассылки, и
для polling). Админ-бот для этого не трогаем (правило ТЗ): у него нет прав
писать в схему `notify`, и заводить их незачем.

Кнопки и команду принимает только владелец (`MY_ADMIN_OWNER_TG_ID`) — решение
этого исполнителя, в тексте ТЗ не названо явно (бот на момент ТЗ ещё не
существовал), но необходимое: это тревожный канал, а «сел разбираться» от
чужого человека тихо гасит настоящую аварию. Владелец добавляет свой
Telegram id в тот же список, что токен и номер чата (см. `docs/runbook.md`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import BaseFilter, Command

from notifyd import db
from notifyd.incidents import (
    ACK_CALLBACK_PREFIX, RED_ACK_SILENCE_SEC, SNOOZE_CALLBACK_PREFIX, YELLOW_SNOOZE_SEC,
)
from notifyd.status import format_status_text
from notifyd.watchdog import Watchdog

log = logging.getLogger(__name__)

# Тот же порядок паузы при обрыве связи, что у admin_bot (adminbot/main.py:
# TELEGRAM_RETRY_SEC).
TELEGRAM_RETRY_SEC = 30

_ACK_REPLY = "Принято, час не напоминаю. Если поломка ещё жива — вернусь сама."
_SNOOZE_REPLY = "Отложил на сутки."
_UNKNOWN_INCIDENT_REPLY = "Этого инцидента уже нет (закрыт или устарел)."
_BAD_BUTTON_REPLY = "Не понял, какой это инцидент."


class OwnerOnly(BaseFilter):
    """Пропускает только владельца — тот же приём, что `adminbot/tg/bot.py`."""

    def __init__(self, owner_tg_id: int) -> None:
        self.owner_tg_id = owner_tg_id

    async def __call__(self, event: Any) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id == self.owner_tg_id)


class AdminBotListener:
    """Слушатель My_admin: один `Dispatcher` на командe `/status` и на
    кнопках инцидентов. Свой выключатель — решает вызывающий код
    (`notifyd.main`), здесь достаточно просто не создавать/не запускать
    объект, если он выключен."""

    def __init__(self, *, bot: Any, pool: Any, watchdog: Watchdog, owner_tg_id: int,
                now: Optional[Callable[[], datetime]] = None,
                sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.bot = bot
        self.pool = pool
        self.watchdog = watchdog
        self.owner_tg_id = owner_tg_id
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.dispatcher = self._build_dispatcher()

    def _build_dispatcher(self) -> Any:
        from aiogram import Dispatcher

        router = Router(name="my_admin")
        owner = OwnerOnly(self.owner_tg_id)
        router.message.register(self.cmd_status, owner, Command("status"))
        router.callback_query.register(self.on_ack, owner,
                                       F.data.startswith(f"{ACK_CALLBACK_PREFIX}:"))
        router.callback_query.register(self.on_snooze, owner,
                                       F.data.startswith(f"{SNOOZE_CALLBACK_PREFIX}:"))
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        return dispatcher

    # --- задача 9: «что сейчас сломано» ---

    async def cmd_status(self, message: Any) -> None:
        now = self._now()
        checks = await self.watchdog.check_once()
        dispatch_stats = await db.fetch_dispatch_stats(self.pool, now=now)
        pending = await db.count_outbox_pending(self.pool)
        incidents = await db.list_open_incidents(self.pool)
        text = format_status_text(checks=checks, dispatch_stats=dispatch_stats,
                                  outbox_pending=pending, incidents=incidents, now=now)
        await message.answer(text)

    # --- задача 8: кнопки инцидентов ---

    async def on_ack(self, callback: Any) -> None:
        await self._handle_button(callback, silence_sec=RED_ACK_SILENCE_SEC,
                                  reply=_ACK_REPLY)

    async def on_snooze(self, callback: Any) -> None:
        await self._handle_button(callback, silence_sec=YELLOW_SNOOZE_SEC,
                                  reply=_SNOOZE_REPLY)

    async def _handle_button(self, callback: Any, *, silence_sec: int, reply: str) -> None:
        incident_id = _parse_incident_id(getattr(callback, "data", None))
        if incident_id is None:
            await callback.answer(_BAD_BUTTON_REPLY)
            return
        now = self._now()
        incident = await db.ack_incident(self.pool, incident_id=incident_id,
                                         until=now + timedelta(seconds=silence_sec), now=now)
        await callback.answer(reply if incident is not None else _UNKNOWN_INCIDENT_REPLY)
        message = getattr(callback, "message", None)
        if message is not None:
            try:
                await message.edit_reply_markup(reply_markup=None)
            except Exception:                             # noqa: BLE001 — сообщение могли
                pass                                       # уже поменять или удалить

    # --- приём обновлений ---

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        watcher = None
        if stop is not None:
            watcher = asyncio.create_task(self._stop_on_signal(stop))
        try:
            while stop is None or not stop.is_set():
                try:
                    await self.dispatcher.start_polling(self.bot, handle_signals=False,
                                                        close_bot_session=False)
                    return                                # штатная остановка
                except TelegramNetworkError as exc:
                    log.warning("notify: My_admin не отвечает (%s), повторю через %s сек",
                               exc, TELEGRAM_RETRY_SEC)
                    await self.sleep(TELEGRAM_RETRY_SEC)
        finally:
            if watcher is not None:
                watcher.cancel()

    async def _stop_on_signal(self, stop: asyncio.Event) -> None:
        await stop.wait()
        await self.dispatcher.stop_polling()

    async def stop_polling(self) -> None:
        await self.dispatcher.stop_polling()


def _parse_incident_id(data: Optional[str]) -> Optional[int]:
    if not data or ":" not in data:
        return None
    try:
        return int(data.rsplit(":", 1)[1])
    except ValueError:
        return None
