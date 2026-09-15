"""Загрузить номера заказов из файла партнёра в память робота.

Зачем нужен. Партнёр повторяет старые строки в новых файлах: месячный свод
повторяет недельные отчёты, файл отказов — недельные отказы. Робот такие повторы
пропускает сам, но только по тем заказам, которые он уже проводил. Строки из
архива за два года ему незнакомы — и любой файл, где они снова попадутся, он
принял бы за работу.

Скрипт помечает такие номера как уже сделанные: заводит привязку со статусом
`done` и путём `remembered` одной записью, без промежуточного состояния.
В amoCRM он не ходит вовсе и существующие записи не трогает — заказ, который
робот когда-то провёл по-настоящему, останется с прежним путём и прежними
сделками. Исключение одно: строка, оставшаяся от оборванной загрузки
(не `done`, без сделок и без отметок в чек-листе), доводится до конца.

Запуск на сервере:

    sudo raketa-admin-bot-update --carpets-remember=<путь к файлу>   посчитать
    sudo raketa-admin-bot-update --carpets-remember=<файл> --carpets-remember-live   записать

Путь к файлу приходит в `CARPETS_REMEMBER_FILE` (на сервере это временная копия,
доступная пользователю `adminbot`), а имя, под которым запись видна в базе, —
в `CARPETS_REMEMBER_NAME`.

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
from typing import Any, NamedTuple, Optional, Sequence

from adminbot import db
from adminbot.carpets.report import parse_report, rows_to_process
from adminbot.carpets.store import PgCarpetStore
from adminbot.config import Settings

# Путь, которым помечены заказы, загруженные из архива: робот их не проводил,
# он о них просто знает. По этому слову их видно в `--report`.
REMEMBERED_PATH = db.CARPET_REMEMBERED_PATH


class RememberCounts(NamedTuple):
    """Что скрипт сделал со строками файла (или сделал бы в режиме просмотра)."""

    known: int      # про этот заказ робот уже знает
    fresh: int      # заказ для робота новый
    fixed: int      # строка от оборванной загрузки, доводим до конца


def is_unfinished(link) -> bool:
    """След оборванной загрузки, а не настоящая работа робота.

    Прежняя версия писала строку в два шага: сначала `new`, потом `done`.
    Обрыв между шагами оставлял строку, которую движок принял бы за работу,
    а повторный запуск считал бы её уже известной и мимо неё прошёл. Такую
    строку видно по тому, что робот по ней не сделал ничего: ни сделок,
    ни отметок в чек-листе.
    """
    return (link.status != "done" and not link.lead_id
            and not link.primary_lead_id and not link.checklist)


async def remember_rows(rows: Sequence[Any], *, store: Any, live: bool,
                        source_file: Optional[str] = None) -> RememberCounts:
    """Пройти наши строки файла: новые запомнить, недоделанные починить.

    Саму строку отчёта не сохраняем: её хранят, чтобы продолжить незаконченную
    работу, а здесь продолжать нечего — и лишние ФИО с адресами в базе не нужны.
    """
    known = fresh = fixed = 0
    for row in rows:
        link = await store.get(row.partner_id)
        if link is None:
            fresh += 1
            if live:
                await store.remember_partner_row(row.partner_id, row.phone10,
                                                 source_file=source_file)
        elif is_unfinished(link):
            fixed += 1
            if live:
                await store.update(row.partner_id, status="done",
                                   path=REMEMBERED_PATH)
        else:
            known += 1
    return RememberCounts(known=known, fresh=fresh, fixed=fixed)


async def main() -> int:
    raw_path = os.environ.get("CARPETS_REMEMBER_FILE", "").strip()
    if not raw_path:
        print("Не указан файл партнёра (CARPETS_REMEMBER_FILE).")
        return 2

    path = Path(raw_path).expanduser()
    if not path.is_file():
        print(f"Файла нет: {path}")
        return 2

    # Скрипт читает временную копию, доступную пользователю adminbot, а в базу
    # пишет имя исходного файла партнёра: `carpets_remember_XXXXXX.xlsx`
    # в отчёте ничего не объясняет.
    source_file = os.environ.get("CARPETS_REMEMBER_NAME", "").strip() or path.name

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

    try:
        counts = await remember_rows(ours, store=store, live=live,
                                     source_file=source_file)
    finally:
        await pool.close()

    print(f"Робот уже знает: {counts.known}")
    print(f"{'Запомнил' if live else 'Новых для робота'}: {counts.fresh}")
    print(f"{'Починил недоделанных' if live else 'Недоделанных (починю)'}: {counts.fixed}")
    if not live:
        print("\nЧтобы записать их в память робота, повторите команду "
              "с --carpets-remember-live.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
