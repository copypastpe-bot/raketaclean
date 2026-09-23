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
from datetime import datetime, timedelta
from typing import Any, Optional

import asyncpg
from aiogram import Bot, Dispatcher

from adminbot import db
from adminbot.amo import ids
from adminbot.amo.client import AmoAuthError, AmoClient, AmoError
from adminbot.amo.fields import MOSCOW_TZ, as_msk
from adminbot.config import Settings
from adminbot.autocall import pbx as autocall_pbx
from adminbot.autocall.engine import AutocallEngine
from adminbot.autocall.pbx import MemoryPbx
from adminbot.autocall.store import MemoryAutocallStore, PgAutocallStore
from adminbot.autocall.watcher import AutocallWatcher
from adminbot.autocall.window import validate_window
from adminbot.carpets.engine import CarpetEngine
from adminbot.gcal.engine import CalendarEngine
from adminbot.gcal.store import MemoryCalendarStore, PgCalendarStore
from adminbot.gcal.watcher import CalendarWatcher
from adminbot.carpets.store import MemoryCarpetStore, PgCarpetStore
from adminbot.carpets.watcher import CarpetWatcher
from adminbot.control import PgControlPanel, sync_allowed
from adminbot.heartbeat import HeartbeatWriter
from adminbot.mail import MailBox, mail_settings_from_env
from adminbot.phone import for_owner
from adminbot.sync.address_reminder import DEFAULT_CAP, AddressReminder, PgReminderSource
from adminbot.sync.backlog import BacklogRunner
from adminbot.sync.deletions import DeletionHandler, DeletionOutcome
from adminbot.sync.engine import Engine, service_enums
from adminbot.sync.reconcile import (
    TOUCHED_WINDOW_HOURS, PgCleaningSummarySource, PgSummarySource, Reconciler)
from adminbot.sync.specialists import SpecialistIndex
from adminbot.sync.store import MemoryLinkStore, PgCleaningLinkStore, PgLinkStore
from adminbot.sync.watcher import PgCleaningSource, PgOrderSource, Watcher
from adminbot.tg.autocall_cards import (
    connected_text, manager_text, no_phone_text, rehearsal_text as autocall_rehearsal_text)
from adminbot.tg.bot import (
    AddressAnswers, CalendarAnswers, CarpetAnswers, OwnerAnswers, OwnerCommands, build_router)
from adminbot.tg.calendar_cards import (
    boat_card, calendar_question_card, cancellation_card,
    done_text, no_realization_text, rehearsal_text, updated_text)
from adminbot.tg.cards import (
    ADDR_PREFIX, CLEANING_ADDR_PREFIX, CLEANING_CHOICE_PREFIX, address_missing_card,
    carpet_held_text, carpet_question_card, carpet_report_text, mark_rehearsal,
    order_done_text, question_card, summary_text)
from adminbot.tg.outbox import (
    SUMMARY_TTL_SEC, MemoryMailStore, OwnerMail, PgMailStore, Purpose)
from adminbot.tg.session import build_session

log = logging.getLogger("adminbot")

# Сколько ждать перед новой попыткой опроса, если Telegram не отвечает.
TELEGRAM_RETRY_SEC = 30

# Виды сообщений владельцу. Вид нужен почте: по нему она знает, нужен ли
# недоставленный долг ещё и что отметить после доставки (см. tg/outbox.py).
MAIL_SUMMARY = "summary"                       # вечерняя сводка — устаревает за часы
MAIL_ORDER_DONE = "order_done"
MAIL_ORDER_QUESTION = "order_question"
MAIL_CLEANING_DONE = "cleaning_done"
MAIL_CLEANING_QUESTION = "cleaning_question"
MAIL_GCAL_DONE = "gcal_done"
MAIL_GCAL_QUESTION = "gcal_question"
MAIL_GCAL_UPDATED = "gcal_updated"
MAIL_GCAL_REHEARSAL = "gcal_rehearsal"
MAIL_GCAL_NO_DEAL = "gcal_no_deal"
MAIL_CARPET_QUESTION = "carpet_question"
MAIL_CARPET_REPORT = "carpet_report"
MAIL_CARPET_HELD = "carpet_held"
MAIL_ADDRESS_REMINDER = "address_reminder"
MAIL_CLEANING_ADDRESS_REMINDER = "cleaning_address_reminder"
MAIL_ORDER_DELETION = "order_deletion"
MAIL_AUTOCALL_REHEARSAL = "autocall_rehearsal"
MAIL_AUTOCALL_CONNECTED = "autocall_connected"
MAIL_AUTOCALL_NO_PHONE = "autocall_no_phone"
MAIL_AUTOCALL_MANAGER = "autocall_manager"


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
    # Почта владельца: досылает сообщения, не ушедшие с первой попытки.
    # Отдельное занятие, потому что связь возвращается сама по себе, а не
    # тогда, когда роботу случилось обработать очередную запись.
    mail: Optional[Any] = None
    carpet_watcher: Optional[Any] = None
    calendar_watcher: Optional[Any] = None
    calendar_token: Optional[Any] = None
    autocall_watcher: Optional[Any] = None
    cleaning_watcher: Optional[Any] = None
    # Напоминание про сделку без адреса (задача 7): свой выключатель, поэтому
    # оба поля обычно пусты, даже когда amo_sync и уборки уже в бою.
    address_reminder: Optional[Any] = None
    cleaning_address_reminder: Optional[Any] = None
    # Отдельный send-only бот для сообщений менеджеру (WORKER_TG_TOKEN) — не участвует
    # в опросе, но его aiohttp-сессия открывается лениво при первой отправке и должна
    # закрыться вместе с сервисом, как и сессия self.bot.
    autocall_manager_bot: Optional[Bot] = None
    # Пульс админ-бота (оповещения, задача 6): свой выключатель, поэтому обычно
    # пусто, даже когда остальное уже в бою.
    heartbeat: Optional[Any] = None

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
        if self.autocall_watcher is not None:
            background.append(asyncio.create_task(
                self.autocall_watcher.run_forever(self.stop), name="autocall"))
        if self.cleaning_watcher is not None:
            background.append(asyncio.create_task(
                self.cleaning_watcher.run_forever(self.stop), name="cleaning"))
        if self.address_reminder is not None:
            background.append(asyncio.create_task(
                self.address_reminder.run_forever(self.stop), name="address_reminder"))
        if self.cleaning_address_reminder is not None:
            background.append(asyncio.create_task(
                self.cleaning_address_reminder.run_forever(self.stop),
                name="cleaning_address_reminder"))
        if self.mail is not None:
            background.append(asyncio.create_task(
                self.mail.run_forever(self.stop), name="mail"))
        if self.heartbeat is not None:
            background.append(asyncio.create_task(
                self.heartbeat.run_forever(self.stop), name="heartbeat"))
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
        if self.autocall_manager_bot is not None:
            await self.autocall_manager_bot.session.close()
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
                    child_by_note=settings.amo_child_by_note,
                    service_by_master=services)

    # На российском сервере имя api.telegram.org не разрешается: если заданы
    # прямые адреса, ходим по ним. Блокировка идёт волнами и гасит все адреса
    # разом, поэтому при заданном прокси трафик уходит через сервер вне РФ
    # (тот же приём, что у рабочего бота компании).
    bot = Bot(token=settings.tg_token,
              session=build_session(settings.telegram_api_ips, settings.telegram_proxy_url))

    # Единственная дверь, через которую робот пишет владельцу. Telegram здесь
    # отвечает через раз, и сообщение, не ушедшее с первой попытки, раньше
    # пропадало навсегда (решение владельца 2026-09-02: доотправлять).
    # parse_mode="HTML" — вечерняя сводка несёт кликабельные ссылки на сделки
    # в хвостах (задача 5, ТЗ 2026-09-21-evening-summary-rework.md); остальные
    # письма это не задевает — разметка включена точечно, по назначению.
    mail = OwnerMail(bot=bot, chat_id=settings.owner_tg_id,
                     store=PgMailStore(own_pool),
                     purposes={MAIL_SUMMARY: Purpose(ttl_sec=SUMMARY_TTL_SEC,
                                                     parse_mode="HTML")})

    # Удаление заказа освобождает сделку (задача 5, ТЗ 2026-09-17): свой
    # выключатель (задача 7). Не отдельный цикл — первый шаг тика наблюдателя
    # (решение владельца 4), поэтому подключается к Watcher параметром, а не
    # своей задачей в App.run.
    deletions_handler = _build_deletions(settings, own_pool, mail, live_amo, rehearsal_amo)

    source = PgOrderSource(bot_pool, own_pool, settings.backlog_from)
    watcher = Watcher(
        engine=engine,
        source=source,
        is_enabled=sync_allowed(sync_enabled=settings.amo_sync_enabled, control=control),
        poll_interval_sec=settings.poll_interval_sec,
        on_question=_make_question_sender(mail, settings.amo_base_url,
                                          dry_run=settings.amo_sync_dry_run),
        # Неделя наблюдения (решение владельца 2026-08-27): о каждом проведённом
        # заказе робот пишет владельцу сразу, со ссылкой на сделку.
        on_done=_make_order_done_sender(mail, settings.amo_base_url,
                                        dry_run=settings.amo_sync_dry_run),
        handle_deletions=deletions_handler.run if deletions_handler is not None else None,
    )
    mail.register(MAIL_ORDER_QUESTION, _question_purpose(store))

    # Напоминание про сделку без адреса (задача 7): своё хранилище, всегда
    # боевое (Postgres) — даже если amo_sync сейчас в репетиции и watcher выше
    # пишет отметки шагов в память. Иначе счётчик напоминаний рос бы только
    # в пределах одного запуска и никогда не долетал бы до потолка.
    address_store = PgLinkStore(own_pool)
    address_reminder, address_answers = _build_address_reminder(
        settings, own_pool, mail, live_amo, store=address_store, table=db.LINKS_TABLE,
        kind=MAIL_ADDRESS_REMINDER, prefix=ADDR_PREFIX, label="Заказ")
    if address_reminder is not None:
        mail.register(MAIL_ADDRESS_REMINDER, _address_reminder_purpose(address_store))

    # Уборки клининг-контура: тот же движок и тот же справочник специалистов,
    # но своя таблица связок, свои выключатели и своя очередь. Пауза — общая:
    # владелец останавливает робота одной кнопкой.
    cleaning_watcher, cleaning_store, cleaning_backlog_from = _build_cleaning(
        settings, bot_pool, own_pool, mail, control, specialists, services,
        live_amo, rehearsal_amo, deletions_handler)
    cleaning_address_reminder = cleaning_address_answers = None
    if cleaning_store is not None:
        mail.register(MAIL_CLEANING_QUESTION, _question_purpose(cleaning_store))

        # То же напоминание, но для уборок: своя таблица связок, тот же движок
        # (ТЗ 2026-09-16, задача 7 работает одинаково для обеих таблиц).
        cleaning_address_store = PgCleaningLinkStore(own_pool)
        cleaning_address_reminder, cleaning_address_answers = _build_address_reminder(
            settings, own_pool, mail, live_amo, store=cleaning_address_store,
            table=db.CLEANING_LINKS_TABLE, kind=MAIL_CLEANING_ADDRESS_REMINDER,
            prefix=CLEANING_ADDR_PREFIX, label="Уборка")
        if cleaning_address_reminder is not None:
            mail.register(MAIL_CLEANING_ADDRESS_REMINDER,
                          _address_reminder_purpose(cleaning_address_store))

    reconciler = Reconciler(
        watcher=watcher,
        source=PgSummarySource(bot_pool, own_pool, settings.backlog_from),
        on_summary=_make_summary_sender(mail, settings.amo_base_url),
        hour_msk=settings.reconcile_hour_msk,
        cleaning_watcher=cleaning_watcher,
        cleaning_source=(PgCleaningSummarySource(bot_pool, own_pool, cleaning_backlog_from)
                         if cleaning_watcher is not None else None),
    )
    # Календарь собирается ниже, но в вечернюю сверку он попадает здесь же:
    # владелец получает одну картину дня, а не два разрозненных сообщения.

    # Хвост проводится отдельным, всегда боевым движком: кнопка «Поехали» —
    # это осознанное разрешение владельца, даже когда сервис работает в репетиции.
    backlog = BacklogRunner(
        fetch_orders=source.pending,
        rehearsal_engine=lambda scratch: Engine(
            amo=rehearsal_amo, store=scratch, specialists=specialists, dry_run=True,
            salesbot_wait_sec=settings.salesbot_wait_sec,
            child_by_note=settings.amo_child_by_note, service_by_master=services),
        live_engine=Engine(amo=live_amo, store=PgLinkStore(own_pool),
                           specialists=specialists, dry_run=False,
                           salesbot_wait_sec=settings.salesbot_wait_sec,
                           child_by_note=settings.amo_child_by_note,
                           service_by_master=services),
    )

    # Ковры от партнёра: своя цепочка и свой выключатель. Если функция выключена
    # или нет доступа к почте, наблюдатель просто не создаётся — уборка работает.
    carpet_watcher, carpet_store = _build_carpets(settings, own_pool, mail, live_amo,
                                                  rehearsal_amo)
    if carpet_store is not None:
        mail.register(MAIL_CARPET_QUESTION, _question_purpose(carpet_store))

    # Календарь: своя цепочка, свой выключатель и свой ключ доступа. Нет ключа —
    # календарь просто не поднимается, остальные функции работают.
    calendar_watcher, calendar_store, calendar_token = _build_calendar(
        settings, own_pool, mail, live_amo, rehearsal_amo)
    if calendar_store is not None:
        mail.register(MAIL_GCAL_QUESTION, _question_purpose(calendar_store))
        mail.register(MAIL_GCAL_DONE, _gcal_done_purpose(calendar_store))
    # Числа календарного контура вплетаются в единую вечернюю сводку, вторым
    # сообщением больше не уходят (задача 6, ТЗ 2026-09-21-evening-summary-rework.md).
    # Функция выключена — calendar_watcher is None, и reconciler просто не считает
    # календарные числа (Reconciler._calendar_counts отдаёт нули).
    reconciler.calendar_counts = (
        _make_calendar_counts(calendar_watcher, own_pool)
        if calendar_watcher is not None else None
    )

    # Автозвонок по заявке с сайта: своя цепочка, свой выключатель и своя АТС.
    # Кривое окно валит старт осознанно (опечатку ловим при запуске); нет
    # ключей АТС или боевого клиента ещё нет — функция просто не поднимается,
    # остальные части сервиса работают как ни в чём не бывало. manager_bot —
    # отдельный send-only бот для сообщений менеджеру (или None, если транспорт
    # не настроен); App должен закрыть его сессию при остановке сервиса.
    autocall_watcher, autocall_manager_bot = _build_autocall(
        settings, own_pool, mail, live_amo, rehearsal_amo)

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
            autocall_enabled=settings.autocall_enabled,
            autocall_dry_run=settings.autocall_dry_run,
            cleaning_enabled=settings.cleaning_sync_enabled,
            cleaning_dry_run=settings.cleaning_sync_dry_run,
            mail=mail,
        ),
        OwnerAnswers(owner_tg_id=settings.owner_tg_id, store=store, backlog=backlog),
        CarpetAnswers(owner_tg_id=settings.owner_tg_id, store=carpet_store)
        if carpet_store else None,
        CalendarAnswers(owner_tg_id=settings.owner_tg_id, store=calendar_store)
        if calendar_store else None,
        OwnerAnswers(owner_tg_id=settings.owner_tg_id, store=cleaning_store,
                     prefix=CLEANING_CHOICE_PREFIX, label="Уборка")
        if cleaning_store else None,
        address=address_answers,
        cleaning_address=cleaning_address_answers,
    ))

    # Пульс админ-бота (оповещения, задача 6): своя таблица notify.
    # service_heartbeats — писать в public.service_heartbeats этому боту
    # нельзя (хард-правило), а notify — территория, где право писать уже
    # есть (см. adminbot/heartbeat.py).
    heartbeat = (
        HeartbeatWriter(pool=own_pool, poll_interval_sec=settings.heartbeat_interval_sec)
        if settings.heartbeat_enabled else None
    )

    return App(settings=settings, bot_pool=bot_pool, own_pool=own_pool,
               amo_clients=(rehearsal_amo, live_amo), bot=bot,
               dispatcher=dispatcher, watcher=watcher, reconciler=reconciler,
               carpet_watcher=carpet_watcher,
               calendar_watcher=calendar_watcher,
               calendar_token=calendar_token,
               autocall_watcher=autocall_watcher,
               autocall_manager_bot=autocall_manager_bot,
               cleaning_watcher=cleaning_watcher,
               address_reminder=address_reminder,
               cleaning_address_reminder=cleaning_address_reminder,
               mail=mail,
               heartbeat=heartbeat,
               stop=asyncio.Event())


def _build_cleaning(settings: Settings, bot_pool: Any, own_pool: Any, mail: OwnerMail,
                    control: Any, specialists: SpecialistIndex, services: dict[str, int],
                    live_amo: AmoClient, rehearsal_amo: AmoClient,
                    deletions_handler: Optional[DeletionHandler] = None):
    """Собрать проведение уборок. Возвращает (наблюдатель, хранилище, дата хвоста).

    Функция выключена — уборки просто не поднимаются, а заказы, ковры, календарь
    и автозвонок работают как ни в чём не бывало.

    Движок здесь тот же класс, что и у заказов: уборка отличается не ходом работы,
    а источником и подписью. Хвост по умолчанию начинается днём включения —
    старые уборки владелец закрывает сам (его решение 2026-09-10).
    """
    if not settings.cleaning_sync_enabled:
        log.info("Уборки: функция выключена настройкой CLEANING_SYNC_ENABLED")
        return None, None, None

    backlog_from = settings.cleaning_backlog_from or datetime.now(MOSCOW_TZ).date()

    # Та же тонкость, что у заказов, ковров и календаря: в репетиции отметки
    # шагов не должны попадать в базу, иначе боевой прогон сочтёт работу
    # сделанной и молча пропустит её.
    store = (MemoryLinkStore() if settings.cleaning_sync_dry_run
             else PgCleaningLinkStore(own_pool))
    engine = Engine(
        amo=rehearsal_amo if settings.cleaning_sync_dry_run else live_amo,
        store=store, specialists=specialists,
        dry_run=settings.cleaning_sync_dry_run,
        salesbot_wait_sec=settings.salesbot_wait_sec,
        child_by_note=settings.amo_child_by_note,
        service_by_master=services,
    )
    watcher = Watcher(
        engine=engine,
        source=PgCleaningSource(bot_pool, own_pool, backlog_from),
        # Пауза общая с заказами: владелец жмёт одну кнопку /pause.
        is_enabled=sync_allowed(sync_enabled=settings.cleaning_sync_enabled, control=control),
        poll_interval_sec=settings.poll_interval_sec,
        on_question=_make_cleaning_question_sender(mail, settings.amo_base_url,
                                                   dry_run=settings.cleaning_sync_dry_run),
        on_done=_make_cleaning_done_sender(mail, settings.amo_base_url,
                                           dry_run=settings.cleaning_sync_dry_run),
        # Тот же разбор удалений, что и у заказов химчистки (задача 5, ТЗ
        # 2026-09-17): один обработчик на оба вида работ, но гонка «удаление
        # раньше нового заказа» закрывается только если он — первый шаг ОБОИХ
        # тиков, а не только заказов. Оба наблюдателя зовут один и тот же
        # объект почти одновременно — безопасно не потому, что разбор сам по
        # себе идемпотентен (первая версия так и думала, ревью 17.09 нашло
        # гонку), а потому что `DeletionHandler.run` целиком идёт под своим
        # `asyncio.Lock`: второй вошедший ждёт первого и видит его отметки.
        handle_deletions=deletions_handler.run if deletions_handler is not None else None,
    )
    log.info("Уборки: включены, режим %s, хвост с %s",
             "репетиция" if settings.cleaning_sync_dry_run else "БОЕВОЙ", backlog_from)
    return watcher, store, backlog_from


def _build_deletions(settings: Settings, own_pool: Any, mail: OwnerMail,
                     live_amo: AmoClient, rehearsal_amo: AmoClient) -> Optional[DeletionHandler]:
    """Собрать разбор удалений (задача 5, ТЗ 2026-09-17). None — функция выключена.

    Свой выключатель (задача 7), по умолчанию выключен — заводится отдельно,
    когда amo_sync/уборки уже проверены в бою. Своя репетиция, в отличие от
    напоминания про адрес: отметка о разборе (`adminbot.order_deletions_seen`)
    необратима, и `DeletionHandler` сам не ставит её в режиме `dry_run` — иначе
    репетиция забрала бы у последующего боя те самые записи, которые нужно
    разобрать по-настоящему (см. докстринг класса). `dry_run` передаётся ему
    явно; выбор клиента амо (`rehearsal_amo`/`live_amo`) идёт по тому же флагу.
    """
    if not settings.order_deletions_enabled:
        log.info("Удаления: функция выключена настройкой ORDER_DELETIONS_ENABLED")
        return None

    handler = DeletionHandler(
        own_pool=own_pool,
        amo=rehearsal_amo if settings.order_deletions_dry_run else live_amo,
        dry_run=settings.order_deletions_dry_run,
        on_notify=_make_deletion_notifier(mail, settings.amo_base_url,
                                          dry_run=settings.order_deletions_dry_run),
    )
    log.info("Удаления: включены, режим %s",
             "репетиция" if settings.order_deletions_dry_run else "БОЕВОЙ")
    return handler


def _build_address_reminder(settings: Settings, own_pool: Any, mail: OwnerMail,
                            live_amo: AmoClient, *, store: Any, table: str, kind: str,
                            prefix: str, label: str):
    """Собрать напоминание про сделку без адреса. Возвращает (цикл, обработчик кнопок).

    Свой выключатель (задача 7, ТЗ 2026-09-16), по умолчанию выключен —
    выкатывается последним, когда задачи 3-6 уже подтверждены рабочими
    (порядок выката из того же ТЗ). Обе проверки сделки в CRM — по кнопке
    «Я заполнил» и перед каждым напоминанием (задача 10) — всегда идут боевым
    клиентом: это чтение, dry_run на него не влияет, а связки, которые видит
    цикл, — всегда настоящие (см. вызов в build_app).
    """
    if not settings.address_reminder_enabled:
        return None, None

    reminder = AddressReminder(
        source=PgReminderSource(own_pool, table=table),
        store=store,
        amo=live_amo,
        on_reminder=_make_address_reminder_sender(mail, kind=kind, prefix=prefix, label=label,
                                                   amo_base_url=settings.amo_base_url),
        poll_interval_sec=settings.address_reminder_poll_interval_sec,
    )
    answers = AddressAnswers(owner_tg_id=settings.owner_tg_id, store=store, amo=live_amo,
                             prefix=prefix, label=label)
    log.info("Напоминание про адрес (%s): включено, опрос раз в %s сек",
             label.lower(), settings.address_reminder_poll_interval_sec)
    return reminder, answers


def _build_carpets(settings: Settings, own_pool: Any, mail: OwnerMail,
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
        child_by_note=settings.amo_child_by_note,
    )
    watcher = CarpetWatcher(
        engine=engine,
        mailbox=MailBox(connect=mail_settings.connect, folder=mail_settings.folder),
        store=store,
        poll_interval_sec=settings.carpets_poll_interval_sec,
        max_rows=settings.carpets_max_rows,
        on_question=_make_carpet_question_sender(mail, settings.amo_base_url,
                                                 dry_run=settings.carpets_dry_run),
        on_report=_make_carpet_report_sender(mail, dry_run=settings.carpets_dry_run),
        on_held=_make_carpet_hold_sender(mail, dry_run=settings.carpets_dry_run),
        dry_run=settings.carpets_dry_run,
    )
    log.info("Ковры: включены, режим %s, почта %s/%s",
             "репетиция" if settings.carpets_dry_run else "БОЕВОЙ",
             mail_settings.user, mail_settings.folder)
    return watcher, store


def _build_calendar(settings: Settings, own_pool: Any, mail: OwnerMail,
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
        child_by_note=settings.amo_child_by_note,
    )
    watcher = CalendarWatcher(
        calendars=[GoogleCalendar(calendar_id=calendar_id, token=token)
                   for calendar_id in settings.gcal_calendar_ids],
        engine=engine,
        store=store,
        sync_from=settings.gcal_sync_from or datetime.now(MOSCOW_TZ).date(),
        poll_interval_sec=settings.gcal_poll_interval_sec,
        on_question=_make_calendar_question_sender(mail, settings.amo_base_url,
                                                   dry_run=settings.gcal_dry_run),
        on_rehearsal=_make_rehearsal_sender(mail),
        on_done=_make_calendar_done_sender(mail, settings.amo_base_url),
        on_updated=_make_calendar_updated_sender(mail, settings.amo_base_url,
                                                 dry_run=settings.gcal_dry_run),
        on_no_deal=_make_calendar_no_deal_sender(mail),
        dry_run=settings.gcal_dry_run,
    )
    log.info("Календарь: включён, режим %s, календарь %s, читаю с %s",
             "репетиция" if settings.gcal_dry_run else "БОЕВОЙ",
             ", ".join(settings.gcal_calendar_ids), watcher.sync_from)
    return watcher, store, token


def _build_autocall(settings: Settings, own_pool: Any, mail: OwnerMail,
                    live_amo: AmoClient, rehearsal_amo: AmoClient):
    """Собрать автозвонок по заявке с сайта. Возвращает (наблюдатель, бот менеджера).

    Функция выключена, окно звонков задано неверно или АТС не поднимается
    (нет ключей доступа либо боевого клиента в модуле ещё нет — он появится
    в Задаче 7) — робот просто не звонит, остальные части сервиса работают
    как ни в чём не бывало (тот же приём, что у календаря без ключа Google);
    оба значения тогда — (None, None).

    Второй элемент — отдельный send-only бот для сообщений менеджеру (или
    None, если транспорт не настроен): его сессию должен закрыть App.close(),
    поэтому наружу он выходит вместе с наблюдателем, а не прячется в замыкании.
    """
    if not settings.autocall_enabled:
        log.info("Автозвонок: функция выключена настройкой AUTOCALL_ENABLED")
        return None, None

    # Опечатку в окне ловим при запуске: упавший старт дешевле, чем робот,
    # который звонит клиенту в три часа ночи (тот же принцип, что у
    # _service_by_master в config.py). Ошибка уходит наружу осознанно.
    validate_window(settings.autocall_window_from_hour, settings.autocall_window_to_hour)

    # Та же тонкость, что в уборке, коврах и календаре: в репетиции курсор
    # опроса и отметки шагов не должны попадать в базу — иначе боевой запуск
    # решит, что работа уже сделана, и пропустит её.
    store = (MemoryAutocallStore() if settings.autocall_dry_run
             else PgAutocallStore(own_pool))

    if settings.autocall_dry_run:
        # Репетиция не звонит и не оставляет следов — фейковая АТС в памяти.
        pbx: Any = MemoryPbx()
    else:
        if not (settings.pbx_base_url and settings.pbx_api_key
                and settings.pbx_manager_dials):
            log.warning("Автозвонок не поднят: не заданы PBX_BASE_URL/PBX_API_KEY/"
                       "PBX_MANAGER_DIAL")
            return None, None
        OnlinePbx = getattr(autocall_pbx, "OnlinePbx", None)
        if OnlinePbx is None:
            log.warning("Автозвонок не поднят: боевой клиент АТС ещё не готов "
                       "(появится в Задаче 7)")
            return None, None
        pbx = OnlinePbx(base_url=settings.pbx_base_url, api_key=settings.pbx_api_key)

    amo = rehearsal_amo if settings.autocall_dry_run else live_amo
    manager_sender, manager_bot = _make_autocall_manager_sender(settings, mail, store)
    engine = AutocallEngine(
        pbx=pbx, amo=amo, store=store, manager_dials=settings.pbx_manager_dials,
        window_from_hour=settings.autocall_window_from_hour,
        window_to_hour=settings.autocall_window_to_hour,
        notify_manager=manager_sender,
        notify_owner_rehearsal=_make_autocall_rehearsal_sender(
            mail, settings.amo_base_url),
        notify_owner_connected=_make_autocall_connected_sender(
            mail, settings.amo_base_url, store),
        dry_run=settings.autocall_dry_run,
    )
    watcher = AutocallWatcher(
        amo=amo, engine=engine, store=store,
        poll_interval_sec=settings.autocall_poll_interval_sec,
        on_no_phone=_make_autocall_no_phone_sender(mail, settings.amo_base_url),
    )
    log.info("Автозвонок: включён, режим %s",
             "репетиция" if settings.autocall_dry_run else "БОЕВОЙ")
    return watcher, manager_bot


def _question_purpose(store: Any) -> Purpose:
    """Карточка-вопрос, доставленная с опозданием: отметить и не спросить дважды.

    Отметка `question_msg_id` — это и признак «уже спросили», и ключ, по которому
    ответ владельца находит свою запись. Без неё нажатие кнопки на доставленной
    карточке было бы ответом в никуда.
    """

    async def still_needed(ref: str) -> bool:
        link = await store.get(_ref_key(ref))
        return link is not None and not link.question_msg_id

    async def on_delivered(ref: str, message_id: int) -> None:
        await store.update(_ref_key(ref), question_msg_id=message_id)

    return Purpose(still_needed=still_needed, on_delivered=on_delivered)


def _gcal_done_purpose(store: Any) -> Purpose:
    """Отчёт о сделанной работе по записи календаря — ровно один на запись."""

    async def still_needed(ref: str) -> bool:
        link = await store.get(ref)
        return link is not None and not link.done_msg_id

    async def on_delivered(ref: str, message_id: int) -> None:
        await store.update(ref, done_msg_id=message_id)

    return Purpose(still_needed=still_needed, on_delivered=on_delivered)


def _ref_key(ref: str) -> Any:
    """К чему относится сообщение: у заказов и ковров это номер, у записи — строка."""
    text = str(ref)
    return int(text) if text.lstrip("-").isdigit() else text


def _address_reminder_purpose(store: Any) -> Purpose:
    """Напоминание, доставленное с опозданием: не слать, если нужда уже отпала.

    Отпасть она могла, пока долг ждал своей очереди в почте: владелец успел
    вписать адрес и нажать «Я заполнил» на предыдущей карточке, либо сказать
    «Не напоминать». В любом из этих случаев повторное письмо — уже лишнее.
    """

    async def still_needed(ref: str) -> bool:
        link = await store.get(_ref_key(ref))
        return (link is not None and not link.address_reminder_muted
                and not link.deal_address)

    return Purpose(still_needed=still_needed)


def _make_calendar_counts(calendar_watcher: Any, own_pool: Any, *,
                          touched_window_hours: int = TOUCHED_WINDOW_HOURS,
                          now=lambda: datetime.now(MOSCOW_TZ)):
    """Календарные числа вечерней сводки (задача 6, ТЗ
    2026-09-21-evening-summary-rework.md): «Завёл из календаря» и календарная
    часть «Передано администратору» — свой догоняющий проход и свой подсчёт по
    журналу действий, тем же окном суток, что и остальные числа сводки
    (db.count_calendar_created, db.count_calendar_owner_handled, задачи 2 и 8).

    Раньше этот проход кончался отдельным сообщением «📅 Календарь»
    (calendar_summary_text) — теперь числа уходят внутри единой сводки, а
    `Reconciler._calendar_counts` ловит сбой здесь так же, как раньше ловил
    сбой перед отправкой того сообщения.
    """

    async def counts() -> tuple[int, int]:
        await calendar_watcher.tick()
        since = now() - timedelta(hours=touched_window_hours)
        created = await db.count_calendar_created(own_pool, since)
        handled = await db.count_calendar_owner_handled(own_pool, since)
        return created, handled

    return counts


# Что стало со сделкой после разбора удаления — эмодзи в начало сообщения
# (задача 5, ТЗ 2026-09-17). Ключи — `DeletionOutcome.outcome`.
_DELETION_EMOJI = {
    "held_by_other": "🔗",
    "lead_gone": "🗑",
    "left_open": "⏳",
    "reopened": "↩️",
}


def _deletion_text(outcome: DeletionOutcome, amo_base_url: str, dry_run: bool) -> str:
    """Письмо владельцу об одном разобранном удалении.

    Полный телефон и дата допустимы (правило `adminbot.phone.for_owner`): бот
    приватный, владелец — единственный получатель, и ему нужно дозвониться
    клиенту, не открывая CRM.
    """
    record, link = outcome.record, outcome.link
    label = "Уборка" if record.kind == "cleaning" else "Заказ"
    when = as_msk(record.deleted_at).strftime("%d.%m.%Y %H:%M")
    emoji = _DELETION_EMOJI.get(outcome.outcome, "🤖")

    if outcome.outcome == "held_by_other":
        other_label = "уборкой" if outcome.other_kind == "cleaning" else "заказом"
        body = (f"сделка #{outcome.lead_id} осталась за {other_label} "
                f"№{outcome.other_order_id} — не трогал")
    elif outcome.outcome == "lead_gone":
        body = f"сделки #{outcome.lead_id} в CRM больше нет — связку убрал"
    elif outcome.outcome == "left_open":
        body = f"сделка #{outcome.lead_id} ещё открыта — не трогал, посмотрите сами"
    else:                                          # reopened
        body = f"сделка #{outcome.lead_id} закрыта — вернул на этап «Заказ подтвержден»"

    lines = [f"{emoji} {label} №{record.order_id} удалён — {body}.",
             f"Телефон: {for_owner(link.phone10)}. Удалён: {when} МСК."]
    if outcome.lead_id and outcome.outcome != "lead_gone":
        lines += ["", f"{amo_base_url.rstrip('/')}/leads/detail/{outcome.lead_id}"]
    return mark_rehearsal("\n".join(lines), dry_run)


def _make_deletion_notifier(mail: OwnerMail, amo_base_url: str, dry_run: bool):
    """Письмо владельцу о разобранном удалении (задача 5)."""

    async def notify(outcome: DeletionOutcome) -> None:
        await mail.send(_deletion_text(outcome, amo_base_url, dry_run),
                        kind=MAIL_ORDER_DELETION, ref=outcome.record.order_id)

    return notify


def _make_order_done_sender(mail: OwnerMail, amo_base_url: str, dry_run: bool = False):
    """Сообщение о проведённом заказе из бота."""

    async def send(order, link) -> None:
        await mail.send(order_done_text(order, link, base_url=amo_base_url, dry_run=dry_run),
                        kind=MAIL_ORDER_DONE, ref=order.order_id)

    return send


def _make_address_reminder_sender(mail: OwnerMail, *, kind: str, prefix: str, label: str,
                                  amo_base_url: str):
    """Карточка «сделка без адреса» — та же на каждое напоминание, счётчик растёт."""

    async def send(link, count: int) -> None:
        text, keyboard = address_missing_card(link, label=label, prefix=prefix,
                                              base_url=amo_base_url, reminder_no=count,
                                              cap=DEFAULT_CAP)
        await mail.send(text, kind=kind, ref=link.order_id, reply_markup=keyboard)

    return send


def _make_cleaning_done_sender(mail: OwnerMail, amo_base_url: str, dry_run: bool = False):
    """Сообщение о проведённой уборке. Подпись берётся из самой работы."""

    async def send(order, link) -> None:
        await mail.send(order_done_text(order, link, base_url=amo_base_url, dry_run=dry_run),
                        kind=MAIL_CLEANING_DONE, ref=order.order_id)

    return send


def _make_cleaning_question_sender(mail: OwnerMail, amo_base_url: str, dry_run: bool = False):
    """Карточка-вопрос по уборке: те же кнопки, своя приставка (см. tg/cards.py)."""

    async def send(order, link) -> Optional[int]:
        text, keyboard = question_card(order, link.question, dry_run=dry_run,
                                       base_url=amo_base_url)
        return await mail.send(text, kind=MAIL_CLEANING_QUESTION, ref=order.order_id,
                               reply_markup=keyboard)

    return send


def _make_calendar_done_sender(mail: OwnerMail, amo_base_url: str):
    """Сообщение о сделке, заведённой по записи календаря.

    Возвращает номер отправленного сообщения — по нему наблюдатель понимает,
    что об этой работе владелец уже извещён, и второй раз не пишет. Связи нет —
    возвращаем None: сообщение стало долгом почты, и отметку поставит она сама,
    когда доставит. Отметить раньше времени значило бы потерять отчёт, как
    это случилось 2026-09-02.
    """

    async def send(link, actions) -> Optional[int]:
        return await mail.send(done_text(link, actions, base_url=amo_base_url),
                               kind=MAIL_GCAL_DONE, ref=link.event_id)

    return send


def _make_calendar_updated_sender(mail: OwnerMail, amo_base_url: str, dry_run: bool = False):
    """Сообщение о том, что правка записи доехала до сделки."""

    async def send(link, changed) -> None:
        await mail.send(updated_text(link, changed, base_url=amo_base_url, dry_run=dry_run),
                        kind=MAIL_GCAL_UPDATED, ref=link.event_id)

    return send


def _make_calendar_no_deal_sender(mail: OwnerMail):
    """Запись удалили, а сделки реализации нет — короткий отчёт (задача 4 ТЗ 2026-09-22)."""

    async def send(link, _payload=None) -> None:
        await mail.send(no_realization_text(link), kind=MAIL_GCAL_NO_DEAL, ref=link.event_id)

    return send


def _make_rehearsal_sender(mail: OwnerMail):
    """Отчёт репетиции: что робот сделал бы с записью календаря."""

    async def send(link, actions) -> None:
        await mail.send(rehearsal_text(link, actions), kind=MAIL_GCAL_REHEARSAL,
                        ref=link.event_id)

    return send


def _make_calendar_question_sender(mail: OwnerMail, amo_base_url: str, dry_run: bool = False):
    """Карточка по записи календаря. Какая именно — зависит от того, что случилось."""

    async def send(link) -> Optional[int]:
        reason = (link.question or {}).get("reason", "")
        if reason.startswith("заказ отменён"):
            text, keyboard = cancellation_card(link, dry_run=dry_run)
        elif reason.startswith("теплоход"):
            text, keyboard = boat_card(link, dry_run=dry_run)
        else:
            text, keyboard = calendar_question_card(link, dry_run=dry_run,
                                                     base_url=amo_base_url)

        return await mail.send(text, kind=MAIL_GCAL_QUESTION, ref=link.event_id,
                               reply_markup=keyboard)

    return send


def _make_carpet_question_sender(mail: OwnerMail, amo_base_url: str, dry_run: bool = False):
    async def send(row, link) -> Optional[int]:
        text, keyboard = carpet_question_card(row, link.question, dry_run=dry_run,
                                              base_url=amo_base_url)
        return await mail.send(text, kind=MAIL_CARPET_QUESTION, ref=row.partner_id,
                               reply_markup=keyboard)

    return send


def _make_carpet_report_sender(mail: OwnerMail, dry_run: bool = False):
    async def send(letter, report) -> None:
        await mail.send(carpet_report_text(letter.subject, report, dry_run=dry_run),
                        kind=MAIL_CARPET_REPORT)

    return send


def _make_carpet_hold_sender(mail: OwnerMail, dry_run: bool = False):
    """Письмо отложено: робот не провёл ни одной строки и ждёт решения владельца."""
    async def send(letter, reason: str, rows_total: int, refused_total: int) -> None:
        await mail.send(carpet_held_text(letter.subject, reason, rows_total,
                                         refused_total, letter.uid, dry_run=dry_run),
                        kind=MAIL_CARPET_HELD)

    return send


def _make_autocall_manager_sender(settings: Settings, mail: OwnerMail, store: Any):
    """Сообщение менеджеру по исходу попытки дозвона.

    Возвращает (отправитель, бот менеджера). Второй элемент нужен вызывающему
    (`_build_autocall`) только затем, чтобы отдать его наружу в `App` — сессию
    этого бота открывает aiogram лениво при первой отправке, и закрыть её
    должен `App.close()`, а не это замыкание, которое переживает сам процесс.

    Уходит от рабочего бота, с которым менеджер уже общается, а не от
    админ-бота владельца (решение Задачи 9). Отдельный `Bot` поднимается один
    раз здесь и работает только на отправку — polling ему не поручаем, за
    обновлениями следит сам рабочий бот. Токена или чата менеджера нет —
    сообщение уходит владельцу с пометкой, что доставить его некому (мягкая
    деградация вместо тишины), а бот менеджера тогда не создаётся вовсе.
    """
    manager_bot: Optional[Bot] = None
    if settings.worker_tg_token and settings.manager_tg_chat_id:
        manager_bot = Bot(token=settings.worker_tg_token,
                          session=build_session(settings.telegram_api_ips,
                                                settings.telegram_proxy_url))

    async def send(lead_id: int, kind: str) -> None:
        link = await store.get(lead_id)
        phone10 = link.phone10 if link is not None else None
        text = manager_text(kind, lead_id, base_url=settings.amo_base_url,
                            phone10=phone10)
        if manager_bot is not None:
            await manager_bot.send_message(settings.manager_tg_chat_id, text)
            return
        # Транспорта до менеджера нет — говорим владельцу. Это сообщение ему,
        # значит идёт почтой: обрыв связи не должен съесть и его.
        await mail.send(f"(менеджеру не отправлено: транспорт не настроен)\n{text}",
                        kind=MAIL_AUTOCALL_MANAGER, ref=lead_id)

    return send, manager_bot


def _make_autocall_rehearsal_sender(mail: OwnerMail, amo_base_url: str):
    """Репетиция: что робот сделал бы, если бы дошёл до звонка."""

    async def send(lead_id: int, phone10: Optional[str]) -> None:
        await mail.send(autocall_rehearsal_text(lead_id, phone10, base_url=amo_base_url),
                        kind=MAIL_AUTOCALL_REHEARSAL, ref=lead_id)

    return send


def _make_autocall_connected_sender(mail: OwnerMail, amo_base_url: str,
                                    store: Any):
    """Отчёт владельцу о соединении — неделя наблюдения (дизайн §6.5).

    Движок передаёт только lead_id, телефон достаём из хранилища сами. Если
    записи уже нет или телефон не сохранился, `connected_text` сам пропускает
    строку телефона (phone10=None) — ни заглушки, ни литерала "None" в
    сообщении не будет.
    """

    async def send(lead_id: int) -> None:
        link = await store.get(lead_id)
        phone10 = link.phone10 if link is not None else None
        await mail.send(connected_text(lead_id, phone10, base_url=amo_base_url),
                        kind=MAIL_AUTOCALL_CONNECTED, ref=lead_id)

    return send


def _make_autocall_no_phone_sender(mail: OwnerMail, amo_base_url: str):
    """Заявка с сайта пришла без телефона — владелец должен посмотреть сам."""

    async def send(lead_id: int) -> None:
        await mail.send(no_phone_text(lead_id, base_url=amo_base_url),
                        kind=MAIL_AUTOCALL_NO_PHONE, ref=lead_id)

    return send


def _make_summary_sender(mail: OwnerMail, amo_base_url: str):
    """Вечерняя сводка владельцу — одним сообщением, включая числа календарного
    контура (задача 6) и признак его сбоя (задача 9), ТЗ
    2026-09-21-evening-summary-rework.md. `amo_base_url` — номера сделок
    в хвостах становятся ссылками на карточку в amoCRM (задача 5); почта
    отправляет это сообщение с HTML-разметкой (`Purpose.parse_mode`, собран
    в `build_app`)."""

    async def send(summary, *, calendar_created: int = 0, calendar_handled: int = 0,
                   calendar_failed: bool = False) -> None:
        await mail.send(summary_text(summary, calendar_created=calendar_created,
                                     calendar_handled=calendar_handled,
                                     calendar_failed=calendar_failed,
                                     base_url=amo_base_url), kind=MAIL_SUMMARY)

    return send


def _make_question_sender(mail: OwnerMail, amo_base_url: str, dry_run: bool = False):
    """Карточка-вопрос владельцу. Возвращает id сообщения — признак «уже спросили».

    Если Telegram недоступен, возвращаем None: карточка стала долгом почты.
    Наблюдатель попробует ещё раз на следующем проходе, а почта — по своему
    расписанию; кто успеет первым, тот и спросит, второй увидит проставленную
    отметку и промолчит.
    """

    async def send(order, link) -> Optional[int]:
        text, keyboard = question_card(order, link.question, dry_run=dry_run,
                                       base_url=amo_base_url)
        return await mail.send(text, kind=MAIL_ORDER_QUESTION, ref=order.order_id,
                               reply_markup=keyboard)

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
