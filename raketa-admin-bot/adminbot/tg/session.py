"""Связь с Telegram с российского сервера.

Проблема простая: с этого VPS имя api.telegram.org не разрешается в адрес,
хотя сами адреса Telegram доступны. Рабочий бот компании давно живёт по прямым
адресам — админ-бот повторяет тот же приём (порт из `tgbot-v1/bot.py:336`).

Как это работает: для имени api.telegram.org подставляем адрес из списка,
предварительно проверив, что он отвечает. Выбранный адрес запоминаем, чтобы
не проверять связь на каждом запросе. Все прочие имена разрешаются обычным путём.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from typing import Any, Awaitable, Callable, Optional, Sequence

from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

log = logging.getLogger(__name__)

TELEGRAM_HOST = "api.telegram.org"

# Сколько ждём отклика адреса при проверке и как долго держимся за выбранный.
PROBE_TIMEOUT_SEC = 3.0
RECHECK_SEC = 300.0

_SEPARATORS = re.compile(r"[,\s;]+")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def parse_ip_pool(raw: Optional[str]) -> list[str]:
    """Разобрать настройку «1.1.1.1, 2.2.2.2» в список адресов без повторов."""
    if not raw:
        return []
    pool: list[str] = []
    for part in _SEPARATORS.split(raw.strip()):
        part = part.strip()
        if not part or part in pool:
            continue
        if not _IPV4.match(part) and ":" not in part:
            log.warning("TELEGRAM_API_IPS: %r не похоже на адрес — пропускаю", part)
            continue
        pool.append(part)
    return pool


async def _can_connect(ip: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host=ip, port=port, family=family),
            timeout=PROBE_TIMEOUT_SEC,
        )
    except Exception:                                  # noqa: BLE001 — молчит, значит не годится
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:                                  # noqa: BLE001
        pass
    return True


class TelegramIPResolver(AbstractResolver):
    """Резолвер, который знает адреса Telegram наизусть."""

    def __init__(
        self,
        ip_pool: Sequence[str],
        *,
        probe: Callable[[str, int], Awaitable[bool]] = _can_connect,
        default: Optional[Any] = None,
        recheck_sec: float = RECHECK_SEC,
    ) -> None:
        self._pool = list(ip_pool)
        self._probe = probe
        self._default = default
        self._recheck_sec = recheck_sec
        self._chosen: Optional[str] = None
        self._chosen_until = 0.0
        self._lock = asyncio.Lock()

    async def resolve(self, host: str, port: int = 0,
                      family: int = socket.AF_UNSPEC) -> list[dict[str, Any]]:
        if host == TELEGRAM_HOST and self._pool:
            resolved_port = port or 443
            return [_record(host, await self._pick(resolved_port), resolved_port)]

        if self._default is None:
            self._default = DefaultResolver()
        return await self._default.resolve(host, port, family)

    async def close(self) -> None:
        if self._default is not None:
            await self._default.close()

    async def _pick(self, port: int) -> str:
        now = time.monotonic()
        if self._chosen and now < self._chosen_until:
            return self._chosen

        async with self._lock:
            if self._chosen and time.monotonic() < self._chosen_until:
                return self._chosen                    # выбрали, пока мы ждали очереди

            for candidate in self._pool:
                if await self._probe(candidate, port):
                    if candidate != self._chosen:
                        log.info("Telegram отвечает по адресу %s", candidate)
                    self._chosen = candidate
                    self._chosen_until = time.monotonic() + self._recheck_sec
                    return candidate

            # Связи нет ни по одному адресу. Отдаём первый: пусть ошибку покажет
            # сам запрос, а не молчаливое зависание на выборе.
            log.warning("Ни один адрес Telegram не отвечает, пробую %s", self._pool[0])
            self._chosen = self._pool[0]
            self._chosen_until = time.monotonic() + 5.0
            return self._chosen


def _record(host: str, ip: str, port: int) -> dict[str, Any]:
    return {
        "hostname": host,
        "host": ip,
        "port": port,
        "family": socket.AF_INET6 if ":" in ip else socket.AF_INET,
        "proto": 0,
        "flags": socket.AI_NUMERICHOST,
    }


def build_session(ip_pool: Sequence[str] = (), proxy: Optional[str] = None):
    """Сессия для бота: с прямыми адресами Telegram, если они заданы.

    aiogram импортируется здесь, а не наверху файла: разбор адресов нужен
    конфигурации, а конфигурацией пользуются скрипты прогонов, которым
    Telegram не нужен вовсе.
    """
    from aiogram.client.session.aiohttp import AiohttpSession

    session = AiohttpSession(proxy=proxy or None)
    if ip_pool:
        session._connector_init["resolver"] = TelegramIPResolver(ip_pool)
        session._connector_init["ttl_dns_cache"] = 0
    return session
