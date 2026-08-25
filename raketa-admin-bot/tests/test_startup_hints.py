"""Понятные сообщения при неудачном старте.

Первый запуск на сервере почти всегда спотыкается о доступы. Владелец должен
прочитать «проверьте пароль базы», а не разбирать стек вызовов Python.
"""

import asyncpg

from adminbot.amo.client import AmoAuthError
from adminbot.main import startup_hint


def test_database_is_unreachable():
    hint = startup_hint(ConnectionRefusedError(61, "Connect call failed"))

    assert "баз" in hint.lower()
    assert "BOT_DB_DSN" in hint


def test_database_refuses_the_password():
    hint = startup_hint(asyncpg.InvalidPasswordError("password authentication failed"))

    assert "пароль" in hint.lower()


def test_amo_token_is_rejected():
    hint = startup_hint(AmoAuthError(401, "Unauthorized"))

    assert "amoCRM" in hint
    assert "AMO_TOKEN" in hint


def test_telegram_token_is_wrong():
    from aiogram.utils.token import TokenValidationError

    hint = startup_hint(TokenValidationError("Token is invalid!"))

    assert "BotFather" in hint


def test_unknown_failure_is_reported_as_is():
    hint = startup_hint(ValueError("что-то своё"))

    assert "что-то своё" in hint
