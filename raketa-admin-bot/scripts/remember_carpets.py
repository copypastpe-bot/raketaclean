"""Загрузить номера заказов из файла партнёра в память робота.

Зачем нужен. Партнёр повторяет старые строки в новых файлах: месячный свод
повторяет недельные отчёты, файл отказов — недельные отказы. Робот такие повторы
пропускает сам, но только по тем заказам, которые он уже проводил. Строки из
архива за два года ему незнакомы — и любой файл, где они снова попадутся, он
принял бы за работу.

Скрипт помечает такие номера как уже сделанные: заводит привязку со статусом
`done` и путём `remembered`. В amoCRM он не ходит вовсе и существующие записи
не трогает — заказ, который робот когда-то провёл по-настоящему, останется
с прежним путём и прежними сделками.

Запуск на сервере:

    sudo raketa-admin-bot-update --carpets-remember=<путь к файлу>   посчитать
    sudo raketa-admin-bot-update --carpets-remember=<файл> --carpets-remember-live   записать

Без `--carpets-remember-live` печатаются только счётчики: сколько в файле наших
строк, сколько новых для робота и сколько он уже знает.

В файле партнёра лежат ФИО, телефоны и адреса клиентов — в репозиторий он не
попадает и не должен. Скрипт получает путь через переменную окружения, сам файл
никуда не копирует и ничего из него не печатает, кроме счётчиков.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from adminbot import db
from adminbot.carpets.report import parse_report, rows_to_process
from adminbot.carpets.store import PgCarpetStore
from adminbot.config import Settings

# Путь, которым помечены заказы, загруженные из архива: робот их не проводил,
# он о них просто знает. По этому слову их видно в `--report`.
REMEMBERED_PATH = "remembered"


async def main() -> int:
    raw_path = os.environ.get("CARPETS_REMEMBER_FILE", "").strip()
    if not raw_path:
        print("Не указан файл партнёра (CARPETS_REMEMBER_FILE).")
        return 2

    path = Path(raw_path).expanduser()
    if not path.is_file():
        print(f"Файла нет: {path}")
        return 2

    live = os.environ.get("CARPETS_REMEMBER_LIVE", "").strip() in ("1", "true")
    print("Режим:", "ЗАПИСЫВАЮ (память робота меняется)" if live
          else "просмотр (ничего не меняю)")

    try:
        rows = parse_report(path.read_bytes())
    except Exception as exc:                       # noqa: BLE001 — кривой файл не повод падать стеком
        print(f"Файл разобрать не удалось: {type(exc).__name__}: {exc}")
        return 2

    completed, refused = rows_to_process(rows)
    ours = completed + refused
    print(f"Строк в файле: {len(rows)}, наших: {len(ours)} "
          f"({len(completed)} выполненных, {len(refused)} отказов)")

    settings = Settings.from_env()
    pool = await db.create_pool(settings.own_db_dsn)
    store = PgCarpetStore(pool)
    known = 0
    fresh = 0

    try:
        for row in ours:
            if await store.get(row.partner_id) is not None:
                known += 1
                continue
            fresh += 1
            if live:
                # Саму строку отчёта не сохраняем: её хранят, чтобы продолжить
                # незаконченную работу, а здесь продолжать нечего — и лишние
                # ФИО с адресами в базе не нужны.
                await store.create(row.partner_id, row.phone10,
                                   source_file=path.name)
                await store.update(row.partner_id, status="done",
                                   path=REMEMBERED_PATH)
    finally:
        await pool.close()

    print(f"Робот уже знает: {known}")
    print(f"{'Запомнил' if live else 'Новых для робота'}: {fresh}")
    if not live:
        print("\nЧтобы записать их в память робота, повторите команду "
              "с --carpets-remember-live.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
