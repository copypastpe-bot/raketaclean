"""Точка входа сервиса: собрать робота и запустить три занятия сразу.

Запуск:  python -m adminbot.main

Три занятия работают параллельно и не мешают друг другу:
1) наблюдатель — раз в минуту доводит заказы до проведённых сделок;
2) вечерняя сверка — в 21:00 МСК отчитывается за день;
3) бот владельца — принимает команды.

Глобальных переменных здесь нет: всё, что нужно частям робота, собирается
в объекте `App` и передаётся явно. Так любую часть можно поднять отдельно
(в тестах, в разовом прогоне) и не тащить за собой весь сервис.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from typing import Any, Optional

from aiogram import Bot, Dispatcher

from adminbot import db
from adminbot.amo import ids
from adminbot.amo.client import AmoClient
from adminbot.config import Settings
from adminbot.control import PgControlPanel, sync_allowed
from adminbot.sync.backlog import BacklogRunner
from adminbot.sync.engine import Engine, service_enums
from adminbot.sync.reconcile import PgSummarySource, Reconciler
from adminbot.sync.specialists import SpecialistIndex
from adminbot.sync.store import MemoryLinkStore, PgLinkStore
from adminbot.sync.watcher import PgOrderSource, Watcher
from adminbot.tg.bot import OwnerAnswers, OwnerCommands, build_router
from adminbot.tg.cards import question_card, summary_text

log = logging.getLogger("adminbot")


@dataclass
class App:
    """Собранный сервис: все части, которые нужно уметь запустить и остановить."""

    settings: Settings
    bot_pool: Any
    own_pool: Any
    amo_clients: tuple[AmoClient, ...]
    bot: Bot
    dispatcher: Dispatcher
    watcher: Watcher
    reconciler: Reconciler
    stop: asyncio.Event

    async def run(self) -> None:
        """Запустить всё до сигнала остановки."""
        log.info(
            "Старт: функция %s, режим %s, хвост с %s",
            "включена" if self.settings.amo_sync_enabled else "выключена",
            "репетиция" if self.settings.amo_sync_dry_run else "БОЕВОЙ",
            self.settings.backlog_from,
        )
        background = [
            asyncio.create_task(self.watcher.run_forever(self.stop), name="watcher"),
            asyncio.create_task(self.reconciler.run_forever(self.stop), name="reconcile"),
            asyncio.create_task(self._stop_polling_on_signal(), name="stopper"),
        ]
        try:
            await self.dispatcher.start_polling(self.bot, handle_signals=False,
                                                close_bot_session=False)
        finally:
            self.stop.set()
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)

    async def _stop_polling_on_signal(self) -> None:
        """Systemd прислал стоп — снимаем бота с опроса, дальше сработает finally."""
        await self.stop.wait()
        await self.dispatcher.stop_polling()

    async def close(self) -> None:
        for client in self.amo_clients:
            await client.close()
        await self.bot.session.close()
        await self.bot_pool.close()
        if self.own_pool is not self.bot_pool:
            await self.own_pool.close()


async def build_app(settings: Settings) -> App:
    """Собрать сервис из настроек: связи с базами, амо, движок, бот."""
    bot_pool = await db.create_pool(settings.bot_db_dsn)
    own_pool = (bot_pool if settings.own_db_dsn == settings.bot_db_dsn
                else await db.create_pool(settings.own_db_dsn))

    # Два клиента амо с разными правами на запись. Репетиционный читает CRM,
    # но никогда в неё не пишет; боевой пишет. Роль клиента видна из его имени,
    # и её нельзя случайно переключить на ходу.
    rehearsal_amo = AmoClient(base_url=settings.amo_base_url, token=settings.amo_token,
                              dry_run=True)
    live_amo = AmoClient(base_url=settings.amo_base_url, token=settings.amo_token,
                         dry_run=False)
    amo = rehearsal_amo if settings.amo_sync_dry_run else live_amo

    # Список «Специалистов» читаем один раз при старте: он меняется редко,
    # а сопоставление мастера со значением списка нужно на каждом заказе.
    specialists = SpecialistIndex.from_enums(
        await amo.get_lead_field_enums(ids.FIELD_SPECIALIST))

    control = PgControlPanel(own_pool)

    # Важная тонкость репетиции. Движок отмечает выполненные шаги в хранилище,
    # чтобы после сбоя продолжить с места остановки. Если писать такие отметки
    # в базу ещё и в репетиции, боевой прогон решит, что работа уже сделана,
    # и молча пропустит её. Поэтому репетиция живёт в памяти процесса: отметки
    # не попадают в базу, но в пределах запуска робот помнит, о чём уже спросил.
    store = MemoryLinkStore() if settings.amo_sync_dry_run else PgLinkStore(own_pool)
    services = service_enums(settings.service_by_master)
    engine = Engine(amo=amo, store=store, specialists=specialists,
                    dry_run=settings.amo_sync_dry_run,
                    salesbot_wait_sec=settings.salesbot_wait_sec,
                    service_by_master=services)

    bot = Bot(token=settings.tg_token)
    source = PgOrderSource(bot_pool, own_pool, settings.backlog_from)
    watcher = Watcher(
        engine=engine,
        source=source,
        is_enabled=sync_allowed(sync_enabled=settings.amo_sync_enabled, control=control),
        poll_interval_sec=settings.poll_interval_sec,
        on_question=_make_question_sender(bot, settings.owner_tg_id),
    )

    reconciler = Reconciler(
        watcher=watcher,
        source=PgSummarySource(bot_pool, own_pool, settings.backlog_from),
        on_summary=_make_summary_sender(bot, settings.owner_tg_id),
        hour_msk=settings.reconcile_hour_msk,
    )

    # Хвост проводится отдельным, всегда боевым движком: кнопка «Поехали» —
    # это осознанное разрешение владельца, даже когда сервис работает в репетиции.
    backlog = BacklogRunner(
        fetch_orders=source.pending,
        rehearsal_engine=lambda scratch: Engine(
            amo=rehearsal_amo, store=scratch, specialists=specialists, dry_run=True,
            salesbot_wait_sec=settings.salesbot_wait_sec, service_by_master=services),
        live_engine=Engine(amo=live_amo, store=PgLinkStore(own_pool),
                           specialists=specialists, dry_run=False,
                           salesbot_wait_sec=settings.salesbot_wait_sec,
                           service_by_master=services),
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(
        OwnerCommands(
            owner_tg_id=settings.owner_tg_id,
            control=control,
            sync_enabled=settings.amo_sync_enabled,
            dry_run=settings.amo_sync_dry_run,
            watcher=watcher,
            backlog=backlog,
        ),
        OwnerAnswers(owner_tg_id=settings.owner_tg_id, store=store, backlog=backlog),
    ))

    return App(settings=settings, bot_pool=bot_pool, own_pool=own_pool,
               amo_clients=(rehearsal_amo, live_amo), bot=bot,
               dispatcher=dispatcher, watcher=watcher, reconciler=reconciler,
               stop=asyncio.Event())


def _make_summary_sender(bot: Bot, owner_tg_id: int):
    """Вечерняя сводка владельцу."""

    async def send(summary) -> None:
        await bot.send_message(owner_tg_id, summary_text(summary))

    return send


def _make_question_sender(bot: Bot, owner_tg_id: int):
    """Карточка-вопрос владельцу. Возвращает id сообщения — признак «уже спросили».

    Если Telegram недоступен, возвращаем None: наблюдатель попробует ещё раз
    на следующем проходе, и вопрос не потеряется.
    """

    async def send(order, link) -> Optional[int]:
        text, keyboard = question_card(order, link.question)
        try:
            sent = await bot.send_message(owner_tg_id, text, reply_markup=keyboard)
        except Exception:                              # noqa: BLE001
            log.exception("Заказ №%s: карточку отправить не удалось", order.order_id)
            return None
        return sent.message_id

    return send


def _install_stop_handlers(app: App) -> None:
    """Остановка по сигналу systemd: доработать тик и выйти, а не рвать посередине."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.stop.set)
        except NotImplementedError:               # на некоторых платформах недоступно
            pass


async def main(settings: Optional[Settings] = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = await build_app(settings or Settings.from_env())
    _install_stop_handlers(app)
    try:
        await app.run()
    finally:
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
