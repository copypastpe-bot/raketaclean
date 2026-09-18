"""Отправка через Telegram (aiogram).

В тестах подменяется фальшивым отправителем (см. tests/conftest.py) — служба
не делает ни одного настоящего сетевого обращения при прогоне тестов.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, Sequence

log = logging.getLogger(__name__)


class Sender(Protocol):
    """Всё, что почтальону нужно от Telegram — и ничего сверх того."""

    async def send(self, chat_id: Any, text: str,
                   reply_markup: Optional[dict] = None) -> Optional[int]: ...


class AiogramSender:
    """Один токен бота, только на отправку.

    Несколько токенов в одном процессе — уже проверенная в этом проекте практика
    (админ-бот шлёт менеджеру токеном рабочего бота отдельным объектом только
    на отправку). Здесь то же самое: у почтальона по объекту на каждый из трёх
    ботов (рабочий, админ-бот, My_admin), и все они живут в одном процессе.
    """

    def __init__(self, token: str, *, ip_pool: Sequence[str] = (),
                 proxy: Optional[str] = None) -> None:
        from aiogram import Bot

        from notifyd.tg_session import build_session

        # С боевого VPS имя api.telegram.org не разрешается, и оба бота ходят по
        # прямым адресам (и через прокси, если он задан). Служба обязана ходить тем
        # же путём: без этого на проде она не отправит ни одного сообщения.
        self._bot = Bot(token=token, session=build_session(ip_pool, proxy or None))

    async def send(self, chat_id: Any, text: str,
                   reply_markup: Optional[dict] = None) -> Optional[int]:
        markup = _load_markup(reply_markup)
        sent = await self._bot.send_message(chat_id, text, reply_markup=markup)
        return getattr(sent, "message_id", None)

    async def close(self) -> None:
        await self._bot.session.close()


def _load_markup(raw: Optional[dict]) -> Optional[Any]:
    """JSON-словарь кнопок обратно в объект aiogram.

    Не распознали форму — отправляем без кнопок и пишем предупреждение: текст
    важнее кнопки, лишняя ошибка разбора не должна ронять доставку целиком.
    """
    if not raw:
        return None
    try:
        if "inline_keyboard" in raw:
            from aiogram.types import InlineKeyboardMarkup

            return InlineKeyboardMarkup.model_validate(raw)
        if "keyboard" in raw:
            from aiogram.types import ReplyKeyboardMarkup

            return ReplyKeyboardMarkup.model_validate(raw)
    except Exception:                                    # noqa: BLE001
        log.warning("notify: не разобрал кнопки %r, отправляю без них", raw)
    return None
