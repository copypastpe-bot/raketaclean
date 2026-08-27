"""Точка входа сервиса: собрать робота и запустить три занятия сразу.

Запуск:  python -m adminbot.main

Занятия работают параллельно и не мешают друг другу:
1) наблюдатель — раз в минуту доводит заказы до проведённых сделок;
2) вечерняя сверка — в 21:00 МСК отчитывается за день;
3) бот владельца — принимает команды;
4) ковры — раз в час забирает отчёты партнёра из почты (если включены).

У каждой функции свой выключатель: ковры можно поднять или погасить,
не трогая уборку, и наоборот.

Глобальных переменных здесь нет: всё, что нужно частям робота, собирается
в объекте `App` и передаётся явно. Так любую часть можно поднять отдельно
(в тестах, в разовом прогоне) и не тащить за собой весь сервис.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import asyncpg
from aiogram import Bot, Dispatcher

from adminbot import db
from adminbot.amo import ids
from adminbot.amo.client import AmoAuthError, AmoClient, AmoError
from adminbot.amo.fields import MOSCOW_TZ
from adminbot.config import Settings
from adminbot.carpets.engine import CarpetEngine
from adminbot.gcal.engine import CalendarEngine
from adminbot.gcal.store import MemoryCalendarStore, PgCalendarStore
from adminbot.gcal.watcher import CalendarWatcher
from adminbot.carpets.store import MemoryCarpetStore, PgCarpetStore
from adminbot.carpets.watcher import CarpetWatcher
from adminbot.control import PgControlPanel, sync_allowed
from adminbot.mail import MailBox, mail_settings_from_env
from adminbot.sync.backlog import BacklogRunner
from adminbot.sync.engine import Engine, service_enums
from adminbot.sync.reconcile import PgSummarySource, Reconciler
from adminbot.sync.specialists import SpecialistIndex
from adminbot.sync.store import MemoryLinkStore, PgLinkStore
from adminbot.sync.watcher import PgOrderSource, Watcher
from adminbot.tg.bot import CalendarAnswers, CarpetAnswers, OwnerAnswers, OwnerCommands, build_router
from adminbot.tg.calendar_cards import (
    boat_card, calendar_question_card, calendar_summary_text, cancellation_card,
    rehearsal_text)
from adminbot.tg.cards import (
    carpet_question_card, carpet_report_text, question_card, summary_text)
from adminbot.tg.session import build_session

log = logging.getLogger("adminbot")

# Сколько ждать перед новой попыткой опроса, если Telegram не отвечает.
TELEGRAM_RETRY_SEC = 30


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
    carpet_watcher: Optional[Any] = None
    calendar_watcher: Optional[Any] = None
    calendar_token: Optional[Any] = None

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
        if self.carpet_watcher is not None:
            background.append(asyncio.create_task(
                self.carpet_watcher.run_forever(self.stop), name="carpets"))
        if self.calendar_watcher is not None:
            background.append(asyncio.create_task(
                self.calendar_watcher.run_forever(self.stop), name="calendar"))
        try:
            await self._poll_until_stopped()
        finally:
            self.stop.set()
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)

    async def _poll_until_stopped(self) -> None:
        """Опрашивать Telegram, переживая обрывы связи.

        Telegram из России доступен нестабильно: хватает нескольких секунд без
        ответа, чтобы aiogram упал с таймаутом. Раньше это роняло весь процесс —
        вместе с работой по CRM, которая от Telegram вообще не зависит. Теперь
        обрыв связи стоит паузы и новой попытки, а сделки продолжают оформляться.
        """
        from aiogram.exceptions import TelegramNetworkError

        while not self.stop.is_set():
            try:
                await self.dispatcher.start_polling(self.bot, handle_signals=False,
                                                    close_bot_session=False)
                return                                 # штатная остановка
            except TelegramNetworkError as exc:
                log.warning("Telegram не отвечает (%s). Повторю через %s секунд; "
                            "работа с CRM продолжается", exc, TELEGRAM_RETRY_SEC)
                await asyncio.sleep(TELEGRAM_RETRY_SEC)

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

    # На российском сервере имя api.telegram.org не разрешается: если заданы
    # прямые адреса, ходим по ним (тот же приём, что у рабочего бота компании).
    bot = Bot(token=settings.tg_token, session=build_session(settings.telegram_api_ips))
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
    # Календарь собирается ниже, но в вечернюю сверку он попадает здесь же:
    # владелец получает одну картину дня, а не два разрозненных сообщения.

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

    # Ковры от партнёра: своя цепочка и свой выключатель. Если функция выключена
    # или нет доступа к почте, наблюдатель просто не создаётся — уборка работает.
    carpet_watcher, carpet_store = _build_carpets(settings, own_pool, bot, live_amo,
                                                  rehearsal_amo)

    # Календарь: своя цепочка, свой выключатель и свой ключ доступа. Нет ключа —
    # календарь просто не поднимается, остальные функции работают.
    calendar_watcher, calendar_store, calendar_token = _build_calendar(
        settings, own_pool, bot, live_amo, rehearsal_amo)
    reconciler.calendar_watcher = calendar_watcher
    reconciler.on_calendar = _make_calendar_summary_sender(bot, settings.owner_tg_id)

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(
        OwnerCommands(
            owner_tg_id=settings.owner_tg_id,
            control=control,
            sync_enabled=settings.amo_sync_enabled,
            dry_run=settings.amo_sync_dry_run,
            watcher=watcher,
            backlog=backlog,
            carpet_watcher=carpet_watcher,
            calendar_watcher=calendar_watcher,
            calendar_enabled=settings.gcal_enabled,
            calendar_dry_run=settings.gcal_dry_run,
        ),
        OwnerAnswers(owner_tg_id=settings.owner_tg_id, store=store, backlog=backlog),
        CarpetAnswers(owner_tg_id=settings.owner_tg_id, store=carpet_store)
        if carpet_store else None,
        CalendarAnswers(owner_tg_id=settings.owner_tg_id, store=calendar_store)
        if calendar_store else None,
    ))

    return App(settings=settings, bot_pool=bot_pool, own_pool=own_pool,
               amo_clients=(rehearsal_amo, live_amo), bot=bot,
               dispatcher=dispatcher, watcher=watcher, reconciler=reconciler,
               carpet_watcher=carpet_watcher,
               calendar_watcher=calendar_watcher,
               calendar_token=calendar_token,
               stop=asyncio.Event())


def _build_carpets(settings: Settings, own_pool: Any, bot: Bot,
                   live_amo: AmoClient, rehearsal_amo: AmoClient):
    """Собрать разбор ковров. Возвращает (наблюдатель, хранилище) или (None, None).

    Функция выключена или нет доступа к почте — ковры просто не поднимаются,
    а уборка работает как ни в чём не бывало. Это и есть «свой выключатель
    у каждой функции» из дизайна.
    """
    if not settings.carpets_enabled:
        log.info("Ковры: функция выключена настройкой CARPETS_ENABLED")
        return None, None

    try:
        mail_settings = mail_settings_from_env()
    except RuntimeError as exc:
        log.warning("Ковры не подняты: %s", exc)
        return None, None

    # Та же тонкость, что и в уборке: в репетиции отметки шагов не должны попадать
    # в базу, иначе боевой прогон сочтёт работу выполненной и пропустит её.
    store = MemoryCarpetStore() if settings.carpets_dry_run else PgCarpetStore(own_pool)
    engine = CarpetEngine(
        amo=rehearsal_amo if settings.carpets_dry_run else live_amo,
        store=store,
        dry_run=settings.carpets_dry_run,
        salesbot_wait_sec=settings.salesbot_wait_sec,
    )
    watcher = CarpetWatcher(
        engine=engine,
        mailbox=MailBox(connect=mail_settings.connect, folder=mail_settings.folder),
        store=store,
        poll_interval_sec=settings.carpets_poll_interval_sec,
        on_question=_make_carpet_question_sender(bot, settings.owner_tg_id),
        on_report=_make_carpet_report_sender(bot, settings.owner_tg_id),
        dry_run=settings.carpets_dry_run,
    )
    log.info("Ковры: включены, режим %s, почта %s/%s",
             "репетиция" if settings.carpets_dry_run else "БОЕВОЙ",
             mail_settings.user, mail_settings.folder)
    return watcher, store


def _build_calendar(settings: Settings, own_pool: Any, bot: Bot,
                    live_amo: AmoClient, rehearsal_amo: AmoClient):
    """Собрать работу по календарю. Возвращает (наблюдатель, хранилище, ключ).

    Функция выключена или ключ служебного аккаунта недоступен — календарь просто
    не поднимается, а уборка и ковры работают как ни в чём не бывало.
    """
    if not settings.gcal_enabled:
        log.info("Календарь: функция выключена настройкой GCAL_ENABLED")
        return None, None, None

    # Подпись ключа Google требует библиотеки google-auth. Загружаем её здесь,
    # а не при старте сервиса: если на сервере её вдруг не окажется, не поднимется
    # только календарь, а уборка и ковры продолжат работать.
    try:
        from adminbot.gcal.auth import GCalKeyError, ServiceAccountToken
        from adminbot.gcal.client import GoogleCalendar
    except ImportError as exc:
        log.warning("Календарь не поднят: нет библиотеки для ключа Google (%s). "
                    "Поможет `pip install -r requirements.txt`", exc)
        return None, None, None

    try:
        token = ServiceAccountToken.from_file(settings.gcal_key_file)
    except GCalKeyError as exc:
        log.warning("Календарь не поднят: %s", exc)
        return None, None, None

    # Та же тонкость, что в уборке и коврах: в репетиции ни отметки шагов, ни
    # закладка обмена не должны попадать в базу. Иначе боевой запуск получит от
    # Google «изменений нет» и пропустит всё, что робот посмотрел вхолостую.
    store = MemoryCalendarStore() if settings.gcal_dry_run else PgCalendarStore(own_pool)
    engine = CalendarEngine(
        amo=rehearsal_amo if settings.gcal_dry_run else live_amo,
        store=store,
        dry_run=settings.gcal_dry_run,
        salesbot_wait_sec=settings.salesbot_wait_sec,
    )
    watcher = CalendarWatcher(
        calendar=GoogleCalendar(calendar_id=settings.gcal_calendar_id, token=token),
        engine=engine,
        store=store,
        sync_from=settings.gcal_sync_from or datetime.now(MOSCOW_TZ).date(),
        poll_interval_sec=settings.gcal_poll_interval_sec,
        on_question=_make_calendar_question_sender(bot, settings.owner_tg_id),
        on_rehearsal=_make_rehearsal_sender(bot, settings.owner_tg_id),
        dry_run=settings.gcal_dry_run,
    )
    log.info("Календарь: включён, режим %s, календарь %s, читаю с %s",
             "репетиция" if settings.gcal_dry_run else "БОЕВОЙ",
             settings.gcal_calendar_id, watcher.sync_from)
    return watcher, store, token


def _make_calendar_summary_sender(bot: Bot, owner_tg_id: int):
    """Вечерняя строка про календарь — вслед за сводкой по заказам."""

    async def send(report) -> None:
        await bot.send_message(owner_tg_id, calendar_summary_text(report))

    return send


def _make_rehearsal_sender(bot: Bot, owner_tg_id: int):
    """Отчёт репетиции: что робот сделал бы с записью календаря."""

    async def send(link, actions) -> None:
        await bot.send_message(owner_tg_id, rehearsal_text(link, actions))

    return send


def _make_calendar_question_sender(bot: Bot, owner_tg_id: int):
    """Карточка по записи календаря. Какая именно — зависит от того, что случилось."""

    async def send(link) -> Optional[int]:
        reason = (link.question or {}).get("reason", "")
        if reason.startswith("заказ отменён"):
            text, keyboard = cancellation_card(link)
        elif reason.startswith("теплоход"):
            text, keyboard = boat_card(link)
        else:
            text, keyboard = calendar_question_card(link)

        try:
            sent = await bot.send_message(owner_tg_id, text, reply_markup=keyboard)
        except Exception:                              # noqa: BLE001
            log.exception("Календарь, запись %s: карточку отправить не удалось",
                          link.event_id)
            return None
        return sent.message_id

    return send


def _make_carpet_question_sender(bot: Bot, owner_tg_id: int):
    async def send(row, link) -> Optional[int]:
        text, keyboard = carpet_question_card(row, link.question)
        try:
            sent = await bot.send_message(owner_tg_id, text, reply_markup=keyboard)
        except Exception:                              # noqa: BLE001
            log.exception("Ковры, заказ №%s: карточку отправить не удалось", row.partner_id)
            return None
        return sent.message_id

    return send


def _make_carpet_report_sender(bot: Bot, owner_tg_id: int):
    async def send(letter, report) -> None:
        try:
            await bot.send_message(owner_tg_id, carpet_report_text(letter.subject, report))
        except Exception:                              # noqa: BLE001
            log.exception("Ковры: отчёт по письму отправить не удалось")

    return send


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


def startup_hint(exc: BaseException) -> str:
    """Перевести ошибку запуска в понятную владельцу подсказку.

    Первый запуск на сервере почти всегда спотыкается о доступы, и разбирать
    стек вызовов владелец не должен.
    """
    from aiogram.utils.token import TokenValidationError

    if isinstance(exc, asyncpg.InvalidPasswordError):
        return ("База не приняла пароль пользователя adminbot. "
                "Проверьте BOT_DB_DSN и ADMINBOT_DB_DSN в файле .env")
    if isinstance(exc, (ConnectionRefusedError, OSError)) and not isinstance(exc, AmoError):
        return ("Не могу подключиться к базе данных. Проверьте BOT_DB_DSN "
                "в файле .env и что Postgres запущен")
    if isinstance(exc, AmoAuthError):
        return "amoCRM не принимает токен: проверьте AMO_TOKEN в файле .env"
    if isinstance(exc, TokenValidationError):
        return "Telegram не принимает токен бота: проверьте ADMINBOT_TG_TOKEN (выдаёт BotFather)"
    return f"{type(exc).__name__}: {exc}"


async def main(settings: Optional[Settings] = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        app = await build_app(settings or Settings.from_env())
    except Exception as exc:                           # noqa: BLE001
        log.error("Запуск не удался. %s", startup_hint(exc))
        raise SystemExit(1) from exc
    _install_stop_handlers(app)
    try:
        await app.run()
    finally:
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
