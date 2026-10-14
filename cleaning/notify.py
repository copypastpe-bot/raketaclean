"""Оповещения в money-flow чат клининга."""

from __future__ import annotations

import logging
import os

from aiogram import Bot

logger = logging.getLogger(__name__)

CLEANING_MONEY_FLOW_CHAT_ID = int(os.getenv("CLEANING_MONEY_FLOW_CHAT_ID", "0") or "0")


async def send_cleaning_money_flow(bot: Bot, text: str) -> None:
    """Шлёт сообщение в клининг money-flow чат, если он задан.

    Send-and-forget с логированием ошибки — внутренний канал админов,
    ретраи делать намеренно не будем (повторяет паттерн MONEY_FLOW_CHAT_ID).

    Обычным текстом, без разметки: комментарий админа вставляется как есть, и
    `<`, `>`, `&` в нём ломали разбор HTML — Telegram отказывал, а запись об
    операции в чат молча не приходила. Разметки в этих сообщениях нет
    (решение владельца 14.10).
    """
    if not CLEANING_MONEY_FLOW_CHAT_ID:
        logger.debug("CLEANING_MONEY_FLOW_CHAT_ID is not set; skipping cleaning notify")
        return
    try:
        await bot.send_message(
            CLEANING_MONEY_FLOW_CHAT_ID,
            text,
            parse_mode=None,
        )
    except Exception as exc:
        logger.warning(
            "Failed to send cleaning money-flow message to chat_id=%s: %s",
            CLEANING_MONEY_FLOW_CHAT_ID,
            exc,
        )
