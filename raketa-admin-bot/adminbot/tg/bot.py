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

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command

from adminbot.control import ControlPanel
from adminbot.tg.cards import (
    CARPET_PREFIX, carpet_report_text, parse_carpet_choice,
    BACKLOG_GO, BACKLOG_HOLD, CHOICE_PREFIX, PATH_BY_PIPELINE, live_report_text,
    parse_choice, preview_card,
)

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
    "/backlog — показать хвост непроведённых заказов и что я с ними сделаю\n"
    "/carpets — разобрать отчёты партнёра по коврам из почты\n"
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
        backlog: Optional[Any] = None,
        carpet_watcher: Optional[Any] = None,
    ) -> None:
        self.owner_tg_id = owner_tg_id
        self.control = control
        self.sync_enabled = sync_enabled
        self.dry_run = dry_run
        self.watcher = watcher
        self.backlog = backlog
        self.carpet_watcher = carpet_watcher

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

    async def backlog_preview(self, message: Any) -> None:
        """Показать хвост и спросить разрешения его провести."""
        if self.backlog is None:
            await message.answer("Разбор хвоста сейчас недоступен.")
            return

        await message.answer("Смотрю хвост, это займёт минуту…")
        plan = await self.backlog.preview()
        text, keyboard = preview_card(plan)
        await message.answer(text, reply_markup=keyboard)

    async def carpets(self, message: Any) -> None:
        """Что лежит в почте от партнёра по коврам и что с этим будет."""
        if self.carpet_watcher is None:
            await message.answer("Разбор ковров сейчас выключен.")
            return

        await message.answer("Смотрю почту партнёра…")
        report = await self.carpet_watcher.tick()
        if report.paused:
            await message.answer("Ковры на паузе — ничего не трогаю.")
            return
        if not report.letters and not report.processed:
            await message.answer("Новых отчётов от партнёра нет.")
            return
        await message.answer(carpet_report_text(f"писем: {report.letters}", report))

    async def help(self, message: Any) -> None:
        await message.answer(HELP_TEXT)

    async def unknown(self, message: Any) -> None:
        """Владелец написал не команду. Не отказ, а короткая подсказка."""
        await message.answer(
            "Я понимаю только команды:\n\n"
            "/status — что происходит\n"
            "/backlog — хвост непроведённых заказов\n"
            "/pause · /resume — остановить и продолжить\n"
            "/help — подробнее"
        )

    async def stranger(self, message: Any) -> None:
        user = getattr(message, "from_user", None)
        log.warning("Чужое сообщение боту от %s", getattr(user, "id", "неизвестно"))
        await message.answer(OWNER_ONLY_REPLY)


class CarpetAnswers:
    """Нажатия на карточках по коврам.

    Как и в уборке, ответ не идёт в CRM напрямую: он записывается рядом с заказом
    партнёра, а работу доделает ближайший проход наблюдателя.
    """

    def __init__(self, *, owner_tg_id: int, store: Any) -> None:
        self.owner_tg_id = owner_tg_id
        self.store = store

    async def on_choice(self, callback: Any) -> None:
        user = getattr(callback, "from_user", None)
        if not (user and user.id == self.owner_tg_id):
            return

        choice = parse_carpet_choice(getattr(callback, "data", None))
        if choice is None:
            await callback.answer()
            return

        partner_id, kind, lead_id = choice
        link = await self.store.get(partner_id)
        if link is None:
            await callback.answer("Этого заказа у меня уже нет.")
            return

        if kind == "manual":
            fields = {"status": "done", "path": None, "question": None}
            reply = "понял, оставляю вам."
        elif kind == "new":
            fields = {"status": "new", "path": "scratch", "question": None}
            reply = "заведу сделку с нуля."
        else:
            fields = {"status": "new", "path": None, "lead_id": lead_id, "question": None}
            reply = f"беру сделку #{lead_id}."

        await self.store.update(partner_id, **fields)
        log.info("Ковры, заказ партнёра №%s: владелец выбрал %s", partner_id, kind)
        await callback.answer()
        await callback.message.edit_text(f"Ковры, заказ №{partner_id}: {reply}")


class OwnerAnswers:
    """Нажатия на кнопки карточек.

    Ответ владельца никогда не идёт в amoCRM напрямую: он записывается рядом
    с заказом, а работу доделает обычный проход наблюдателя. Поэтому решение
    не потеряется, даже если робота перезапустят сразу после нажатия.
    """

    def __init__(self, *, owner_tg_id: int, store: Any, backlog: Optional[Any] = None) -> None:
        self.owner_tg_id = owner_tg_id
        self.store = store
        self.backlog = backlog

    async def on_choice(self, callback: Any) -> None:
        if not self._is_owner(callback):
            return
        choice = parse_choice(getattr(callback, "data", None))
        if choice is None:
            await callback.answer()
            return

        order_id, kind, lead_id = choice
        link = await self.store.get(order_id)
        if link is None:
            log.warning("Ответ по заказу №%s, которого нет в базе", order_id)
            await callback.answer("Этого заказа у меня уже нет.")
            return

        fields, reply = self._apply_choice(link, kind, lead_id)
        await self.store.update(order_id, **fields)
        log.info("Заказ №%s: владелец выбрал %s", order_id, kind)
        await callback.answer()
        await callback.message.edit_text(f"Заказ №{order_id}: {reply}")

    async def on_backlog(self, callback: Any) -> None:
        if not self._is_owner(callback):
            return
        data = getattr(callback, "data", None)

        if data == BACKLOG_HOLD:
            await callback.answer()
            await callback.message.edit_text("Отложил. Хвост никуда не денется — "
                                             "покажу снова по команде /backlog.")
            return

        if data != BACKLOG_GO or self.backlog is None:
            await callback.answer()
            return

        await callback.answer("Поехали")
        await callback.message.edit_text("Провожу хвост…")
        done = await self.backlog.run_live()
        await callback.message.edit_text(live_report_text(done))

    # --- внутреннее ---

    def _is_owner(self, event: Any) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id == self.owner_tg_id)

    def _apply_choice(self, link: Any, kind: str, lead_id: Optional[int]):
        """Что записать по выбору владельца и что ему ответить."""
        if kind == "manual":
            return ({"status": "done", "path": "done", "question": None},
                    "понял, оставляю вам. Ничего трогать не буду.")

        if kind == "new":
            return ({"status": "new", "path": "C", "question": None},
                    "создам новую сделку с нуля.")

        if kind == "retry":
            return ({"status": "new", "question": None},
                    "проверю ещё раз.")

        # Выбрана конкретная сделка: путь зависит от того, в какой она воронке.
        pipeline_id = self._pipeline_of(link, lead_id)
        path = PATH_BY_PIPELINE.get(pipeline_id, "A")
        fields = {"status": "new", "path": path, "question": None}
        fields["real_lead_id" if path == "A" else "primary_lead_id"] = lead_id
        return fields, f"беру сделку #{lead_id}."

    @staticmethod
    def _pipeline_of(link: Any, lead_id: Optional[int]) -> Optional[int]:
        for option in ((link.question or {}).get("options") or []):
            if option.get("lead_id") == lead_id:
                return option.get("pipeline_id")
        return None


def build_router(commands: OwnerCommands, answers: Optional[OwnerAnswers] = None,
                 carpets: Optional[CarpetAnswers] = None) -> Router:
    """Собрать роутер: сначала команды владельца, последним — отказ всем прочим."""
    router = Router(name="owner")
    owner = OwnerOnly(commands.owner_tg_id)

    router.message.register(commands.status, owner, Command("status"))
    router.message.register(commands.backlog_preview, owner, Command("backlog"))
    router.message.register(commands.carpets, owner, Command("carpets"))
    router.message.register(commands.pause, owner, Command("pause"))
    router.message.register(commands.resume, owner, Command("resume"))
    router.message.register(commands.help, owner, Command("help", "start"))
    # Порядок важен: сначала владелец с любым другим сообщением, потом все прочие.
    router.message.register(commands.unknown, owner)
    router.message.register(commands.stranger)

    if answers is not None:
        router.callback_query.register(answers.on_choice, owner,
                                       F.data.startswith(f"{CHOICE_PREFIX}:"))
        router.callback_query.register(answers.on_backlog, owner,
                                       F.data.startswith("backlog:"))
    if carpets is not None:
        router.callback_query.register(carpets.on_choice, owner,
                                       F.data.startswith(f"{CARPET_PREFIX}:"))
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
