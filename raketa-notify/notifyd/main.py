"""Точка входа службы-почтальона (raketa-notify).

Работа: раз в NOTIFY_POLL_INTERVAL_SEC секунд смотрит в notify.outbox, что
созрело, смотрит маршрут в notify.routes, отправляет и отмечает. Подробности
самого цикла — notifyd/postman.py.

Собственные логи службы идут просто на сервер (в journald через systemd) —
самонаблюдения служба не делает (решение владельца, ТЗ «Чего не делаем»).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Optional

from notifyd import db
from notifyd.admin_bot import AdminBotListener
from notifyd.config import Settings
from notifyd.incidents import IncidentManager
from notifyd.journal_adapter import WATCHED_UNITS, JournalAdapter
from notifyd.journal_source import SystemdJournalSource
from notifyd.postman import Postman, Target
from notifyd.telegram import AiogramSender
from notifyd.watchdog import Watchdog

log = logging.getLogger(__name__)

# Какой бот физически пишет в какой адрес справочника. Инфраструктурное
# решение исполнителя задачи 3 (в ТЗ не расписано): my_assistant — это и есть
# существующий бот админ-бота (решение владельца 18.09), my_admin — новый бот;
# work_chat/ops_feed/tech_journal/manager — рабочий бот, он уже пишет в чат
# логов и в чат менеджера сегодня (карта служебных сообщений). Меняется без
# правки кода — только этот словарь, если владелец решит иначе.
ADDRESS_SENDER_ALIAS: dict[str, str] = {
    "work_chat": "worker",
    "ops_feed": "worker",
    "tech_journal": "worker",
    "manager": "worker",
    "my_assistant": "adminbot",
    "my_admin": "my_admin",
}


def build_targets(settings: Settings, senders: dict[str, AiogramSender]) -> dict[str, Target]:
    """Адрес -> (отправитель, чат). Адрес без номера чата просто не попадает
    в словарь: почтальон тогда отложит событие и громко напишет в журнал,
    а не упадёт и не отправит в пустоту."""
    chat_ids = {
        "work_chat": settings.work_chat_id,
        "ops_feed": settings.ops_feed_chat_id,
        "tech_journal": settings.tech_journal_chat_id,
        "my_assistant": settings.my_assistant_chat_id,
        "my_admin": settings.my_admin_chat_id,
        "manager": settings.manager_chat_id,
    }
    targets: dict[str, Target] = {}
    for address, chat_id in chat_ids.items():
        if chat_id is None:
            continue
        alias = ADDRESS_SENDER_ALIAS[address]
        targets[address] = Target(sender=senders[alias], chat_id=chat_id)
    return targets


async def run() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    pool = await db.create_pool(settings.db_dsn)
    # Дорога до Telegram у всех трёх отправителей одна и та же — та, которой
    # ходят сами боты: прямые адреса плюс прокси, если он задан.
    road = {"ip_pool": settings.telegram_api_ips, "proxy": settings.telegram_proxy_url}
    senders = {
        "worker": AiogramSender(settings.worker_tg_token, **road),
        "adminbot": AiogramSender(settings.adminbot_tg_token, **road),
        "my_admin": AiogramSender(settings.my_admin_tg_token, **road),
    }
    targets = build_targets(settings, senders)

    postman = Postman(pool=pool, targets=targets, enabled=settings.enabled,
                      dry_run=settings.dry_run,
                      poll_interval_sec=settings.poll_interval_sec,
                      batch_limit=settings.batch_limit)

    # Переходник журнала (задача 5) — свой цикл в том же процессе; кладёт
    # в notify.outbox, дальше тот же почтальон доставляет как любое другое
    # событие. Источники настоящие (journalctl) всегда, даже если сам
    # переходник выключен — JournalAdapter.run_forever их просто не трогает.
    journal_adapter = JournalAdapter(
        pool=pool,
        sources={unit: SystemdJournalSource(unit) for unit in WATCHED_UNITS},
        enabled=settings.journal_enabled,
        poll_interval_sec=settings.journal_poll_interval_sec,
        cap_per_cycle=settings.journal_max_per_minute,
    )

    # Сторож (задача 7) — единственный цикл проверок в службе. «Прокси отвечает»
    # проверяется через уже поднятую сессию рабочего бота (тем же путём,
    # которым идёт доставка) — второй сессии специально под проверку не
    # заводим, senders["worker"] в build_targets всегда есть (WORKER_TG_TOKEN
    # обязателен, см. REQUIRED_ENV).
    watchdog = Watchdog(
        pool=pool,
        proxy_probe=senders["worker"].ping,
        enabled=settings.watchdog_enabled,
        poll_interval_sec=settings.watchdog_poll_interval_sec,
        heartbeat_max_age_sec=settings.watchdog_heartbeat_max_age_sec,
        amocrm_max_age_sec=settings.watchdog_amocrm_max_age_sec,
        db_timeout_sec=settings.watchdog_db_timeout_sec,
        proxy_timeout_sec=settings.watchdog_proxy_timeout_sec,
        dispatch_max_age_sec=settings.watchdog_dispatch_max_age_sec,
    )

    # Инциденты (задача 8) — свой выключатель, но НЕ свой цикл: результаты
    # приносит цикл сторожа (on_results ниже). Раздельные циклы звали
    # check_once() на одном объекте и затирали друг другу замер очереди
    # (замечание 2 ревью 18.09); выключатели при этом остались раздельными,
    # как того и хочет «Порядок выката» ТЗ — инциденты включаются ПОСЛЕДНИМИ,
    # когда владелец уже обжился со сторожем и переходником журнала.
    incidents = IncidentManager(pool=pool, enabled=settings.incidents_enabled)

    # Слушатель My_admin (кнопки инцидентов + команда /status, задачи 8-9).
    # Поднимается на ТОМ ЖЕ объекте Bot, которым почтальон уже отправляет
    # (AiogramSender.bot) — второй сессии на тот же токен не заводим.
    my_admin_listener: Optional[AdminBotListener] = None
    if settings.my_admin_listener_enabled:
        if settings.my_admin_owner_tg_id is None:
            log.error("notify: NOTIFY_MY_ADMIN_ENABLED=1, но MY_ADMIN_OWNER_TG_ID не "
                     "задан — слушатель My_admin не поднимаю, кнопки и /status не "
                     "будут работать (остальная служба работает как обычно)")
        else:
            my_admin_listener = AdminBotListener(
                bot=senders["my_admin"].bot, pool=pool, watchdog=watchdog,
                owner_tg_id=settings.my_admin_owner_tg_id,
            )

    if settings.incidents_enabled and not settings.watchdog_enabled:
        log.warning("notify: инциденты включены, а сторож выключен — проверок никто "
                   "не делает, тревог не будет. По «Порядку выката» сначала "
                   "NOTIFY_WATCHDOG_ENABLED=1, потом NOTIFY_INCIDENTS_ENABLED=1")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info("notify: служба запущена (enabled=%s, dry_run=%s, адресов настроено=%s, "
             "переходник журнала enabled=%s, сторож enabled=%s, инциденты enabled=%s, "
             "слушатель My_admin enabled=%s)",
             settings.enabled, settings.dry_run, len(targets), settings.journal_enabled,
             settings.watchdog_enabled, settings.incidents_enabled,
             my_admin_listener is not None)

    tasks = [postman.run_forever(stop), journal_adapter.run_forever(stop),
            watchdog.run_forever(stop, on_results=incidents.process_checks)]
    if my_admin_listener is not None:
        tasks.append(my_admin_listener.run_forever(stop))

    try:
        await asyncio.gather(*tasks)
    finally:
        if my_admin_listener is not None:
            await my_admin_listener.stop_polling()
        for sender in senders.values():
            await sender.close()
        await pool.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
