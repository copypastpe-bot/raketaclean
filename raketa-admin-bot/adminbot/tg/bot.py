"""Бот владельца: единственный пульт управления роботом.

Два правила этого модуля.

1. Бот личный. Он ходит в боевую CRM компании, поэтому команды принимает
   ровно от одного человека. Всем остальным — вежливый отказ и ничего больше.
2. Владелец не обязан знать внутренние слова робота. Наружу идут понятные
   формулировки: «репетиция», «на паузе», «ждут вашего ответа», а не
   dry-run, waiting_owner и статусы из базы.

Обработчики здесь тонкие: они складывают текст и отвечают. Всё, что можно
проверить без Telegram, вынесено в чистые функции ниже.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from aiogram import Router
from aiogram.filters import BaseFilter, Command

from adminbot.control import ControlPanel

log = logging.getLogger(__name__)

OWNER_ONLY_REPLY = (
    "Это личный бот владельца компании. Отвечать другим я не умею."
)

# Внутренние статусы очереди → слова, понятные без объяснений.
QUEUE_NAMES: tuple[tuple[str, str], ...] = (
    ("new", "новых"),
    ("in_progress", "в работе"),
    ("waiting_salesbot", "ждут автосделку"),
    ("waiting_owner", "ждут вашего ответа"),
    ("error", "ошибок"),
    ("done", "проведено"),
)

HELP_TEXT = (
    "🤖 Я слежу за заказами рабочего бота и сам оформляю по ним сделки в amoCRM.\n\n"
    "/status — что происходит: режим, пауза, очередь заказов\n"
    "/pause — остановиться: в amoCRM ничего трогать не буду\n"
    "/resume — продолжить работу\n"
    "/help — эта справка\n\n"
    "Если по заказу непонятно, к какой сделке его отнести, я пришлю карточку "
    "с кнопками — выберите вариант, дальше сделаю сам."
)


class OwnerOnly(BaseFilter):
    """Пропускает только владельца."""

    def __init__(self, owner_tg_id: int) -> None:
        self.owner_tg_id = owner_tg_id

    async def __call__(self, event: Any) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id == self.owner_tg_id)


class OwnerCommands:
    """Обработчики команд владельца."""

    def __init__(
        self,
        *,
        owner_tg_id: int,
        control: ControlPanel,
        sync_enabled: bool,
        dry_run: bool,
        watcher: Optional[Any] = None,
    ) -> None:
        self.owner_tg_id = owner_tg_id
        self.control = control
        self.sync_enabled = sync_enabled
        self.dry_run = dry_run
        self.watcher = watcher

    async def status(self, message: Any) -> None:
        await message.answer(status_text(
            sync_enabled=self.sync_enabled,
            dry_run=self.dry_run,
            paused=await self.control.is_paused(),
            counts=await self.control.queue_counts(),
            last_tick_at=getattr(self.watcher, "last_tick_at", None),
            last_report=getattr(self.watcher, "last_report", None),
        ))

    async def pause(self, message: Any) -> None:
        if await self.control.is_paused():
            await message.answer("Я уже на паузе. Продолжить — /resume.")
            return
        await self.control.set_paused(True)
        log.info("Владелец поставил amo_sync на паузу")
        await message.answer(
            "Поставил на паузу. Пока не скажете /resume, в amoCRM ничего не трогаю.\n"
            "Заказы никуда не денутся: разберу их, когда продолжим."
        )

    async def resume(self, message: Any) -> None:
        if not self.sync_enabled:
            await message.answer(
                "Робот выключен настройками сервиса. Этот выключатель снимается "
                "на сервере — командой его не поднять."
            )
            return
        was_paused = await self.control.is_paused()
        await self.control.set_paused(False)
        log.info("Владелец снял паузу с amo_sync")
        await message.answer(
            "Работаю дальше. Ближайший проход — в течение минуты."
            if was_paused else "Я и так работаю. Ничего менять не стал."
        )

    async def help(self, message: Any) -> None:
        await message.answer(HELP_TEXT)

    async def stranger(self, message: Any) -> None:
        user = getattr(message, "from_user", None)
        log.warning("Чужое сообщение боту от %s", getattr(user, "id", "неизвестно"))
        await message.answer(OWNER_ONLY_REPLY)


def build_router(commands: OwnerCommands) -> Router:
    """Собрать роутер: сначала команды владельца, последним — отказ всем прочим."""
    router = Router(name="owner")
    owner = OwnerOnly(commands.owner_tg_id)

    router.message.register(commands.status, owner, Command("status"))
    router.message.register(commands.pause, owner, Command("pause"))
    router.message.register(commands.resume, owner, Command("resume"))
    router.message.register(commands.help, owner, Command("help", "start"))
    router.message.register(commands.stranger)          # порядок важен: это «всё остальное»
    return router


# --- тексты ---

def status_text(*, sync_enabled: bool, dry_run: bool, paused: bool,
                counts: dict[str, int], last_tick_at: Optional[datetime] = None,
                last_report: Optional[Any] = None) -> str:
    """Ответ на /status — состояние робота человеческими словами."""
    mode = ("репетиция — решения принимаю, в amoCRM ничего не пишу"
            if dry_run else "боевой — сделки оформляю по-настоящему")

    if not sync_enabled:
        state = "выключен настройками сервиса (включается на сервере, не командой)"
    elif paused:
        state = "на паузе, возобновить — /resume"
    else:
        state = "работаю"

    lines = [
        "🤖 Робот amo_sync",
        "",
        f"Режим: {mode}",
        f"Состояние: {state}",
        "",
        _queue_block(counts),
    ]

    if last_tick_at:
        scanned = getattr(last_report, "scanned", None)
        tail = f", просмотрено заказов: {scanned}" if scanned is not None else ""
        lines.append(f"\nПоследний проход: {last_tick_at:%d.%m в %H:%M}{tail}")
    else:
        lines.append("\nПроходов ещё не было.")
    return "\n".join(lines)


def _queue_block(counts: dict[str, int]) -> str:
    rows = [f"• {title}: {counts[key]}" for key, title in QUEUE_NAMES if counts.get(key)]
    if not rows:
        return "Заказов в очереди нет."
    return "\n".join(["Очередь заказов:", *rows])
