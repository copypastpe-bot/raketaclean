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

from notifyd import db
from notifyd.config import Settings
from notifyd.postman import Postman, Target
from notifyd.telegram import AiogramSender

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
    senders = {
        "worker": AiogramSender(settings.worker_tg_token),
        "adminbot": AiogramSender(settings.adminbot_tg_token),
        "my_admin": AiogramSender(settings.my_admin_tg_token),
    }
    targets = build_targets(settings, senders)

    postman = Postman(pool=pool, targets=targets, enabled=settings.enabled,
                      dry_run=settings.dry_run,
                      poll_interval_sec=settings.poll_interval_sec,
                      batch_limit=settings.batch_limit)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info("notify: служба запущена (enabled=%s, dry_run=%s, адресов настроено=%s)",
             settings.enabled, settings.dry_run, len(targets))
    try:
        await postman.run_forever(stop)
    finally:
        for sender in senders.values():
            await sender.close()
        await pool.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
