"""Канонический разбор телефона.

Порт посимвольного скана из рабочего бота (`tgbot-v1/bot.py:3251`,
`normalize_phone_for_db`) — чтобы админ-бот и бот считали телефон одинаково.

Отличие от оригинала: если номер не распознан, возвращаем None, а не исходную
строку. Мусорный «телефон» в матчере опаснее пустоты: по нему можно привязать
заказ к чужой сделке в амо.
"""

from __future__ import annotations

from typing import Optional

# Номер начинается только с этих цифр — форматы, встречающиеся в боте и календаре.
_START_DIGITS = ("7", "8", "9")
# 7/8 — код страны впереди (11 цифр), 9 — номер без кода (10 цифр).
_EXPECTED_LEN = {"7": 11, "8": 11, "9": 10}


def _scan_digits(raw: str) -> Optional[str]:
    """Вернуть первую подходящую последовательность цифр телефона или None.

    Скан идёт по символам: до старта пропускаем всё, что не 7/8/9 (так цены,
    суммы и даты в тексте не сбивают счёт); после старта берём подряд идущие
    цифры, игнорируя разделители, и останавливаемся, набрав нужную длину.
    """
    if not raw:
        return None

    first: Optional[str] = None
    buf: list[str] = []

    for ch in raw:
        if not ch.isdigit():
            continue
        if first is None:
            if ch not in _START_DIGITS:
                continue
            first = ch
        buf.append(ch)
        if len(buf) == _EXPECTED_LEN[first]:
            return "".join(buf)

    # Цифр набралось меньше нужного — номера в строке нет.
    return None


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Привести телефон к виду +7XXXXXXXXXX. None, если номер не распознан."""
    digits = _scan_digits(raw or "")
    if digits is None:
        return None
    if len(digits) == 10:                 # 9XXXXXXXXX
        return "+7" + digits
    if digits.startswith("8"):            # 8XXXXXXXXXX
        return "+7" + digits[1:]
    return "+" + digits                   # 7XXXXXXXXXX


def last10(raw: Optional[str]) -> Optional[str]:
    """Вернуть 10 цифр номера без кода страны — ключ сравнения в матчере."""
    normalized = normalize_phone(raw)
    if normalized is None:
        return None
    return normalized[-10:]


def mask(raw: Optional[str]) -> str:
    """Замаскировать телефон для логов и сообщений: только последние 4 цифры."""
    digits = last10(raw)
    if digits is None:
        return "…"
    return "…" + digits[-4:]


def for_owner(raw: Optional[str]) -> str:
    """Телефон в сообщение владельцу — целиком и в кликабельном виде.

    Бот личный: других получателей у сообщений нет, а владельцу номер нужен,
    чтобы позвонить клиенту, не заходя в CRM. В журналах сервера телефон
    по-прежнему маскируется (mask): их читает не только бот.
    """
    return normalize_phone(raw) or "телефон не распознан"
