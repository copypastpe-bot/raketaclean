"""Отложенные письма партнёра: показать и снять отложение.

Зачем нужен. Робот не проводит письмо, в котором наших строк больше порога
(`CARPETS_MAX_ROWS`) или чьё вложение не разобралось: он откладывает такое письмо
и один раз пишет владельцу. Письмо остаётся в папке непрочитанным и само по себе
больше ничего не делает — ждёт решения. Решений два:

- это нормальный отчёт, просто большой — снять отложение, и ближайший часовой
  проход проведёт письмо как обычное (порог к нему уже не применяется);
- это архив или чужой файл — удалить письмо из папки `robot_amo`, и робот
  о нём больше не вспомнит.

Запуск на сервере:

    sudo raketa-admin-bot-update --carpets-held             показать отложенные
    sudo raketa-admin-bot-update --carpets-release=<uid>    снять отложение

UID письма печатается в списке и в сообщении владельцу. Это постоянный номер
письма в папке: он не меняется, даже если из папки удалить соседнее письмо.
"""

from __future__ import annotations

import asyncio
import os
import sys

from adminbot import db
from adminbot.config import Settings


async def main() -> int:
    uid = os.environ.get("CARPETS_RELEASE_UID", "").strip()

    settings = Settings.from_env()
    pool = await db.create_pool(settings.own_db_dsn)
    try:
        if uid:
            return await _release(pool, uid)
        return await _show(pool)
    finally:
        await pool.close()


async def _show(pool) -> int:
    letters = await db.held_letters(pool)
    if not letters:
        print("Отложенных писем нет: робот ничего не ждёт.")
        return 0

    print(f"Отложенных писем: {len(letters)}\n")
    for letter in letters:
        when = letter["held_at"]
        print(f"UID {letter['uid']}: {letter['subject'] or 'без темы'}")
        print(f"   строк: {letter['rows_total']}, причина: {letter['held_reason']}")
        print(f"   отложено: {when:%d.%m.%Y %H:%M} UTC" if when else "   отложено: —")
    print("\nПровести письмо: sudo raketa-admin-bot-update --carpets-release=<UID>")
    print("Не проводить: удалите письмо из папки robot_amo.")
    return 0


async def _release(pool, uid: str) -> int:
    if await db.release_letter(pool, uid):
        print(f"Отложение с письма {uid} снято.")
        print("Ближайший часовой проход проведёт его как обычное письмо.")
        return 0

    print(f"Письмо {uid} среди отложенных не числится — снимать нечего.")
    print("Список отложенных: sudo raketa-admin-bot-update --carpets-held")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
