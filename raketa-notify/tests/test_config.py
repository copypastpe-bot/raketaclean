"""Разбор настроек службы.

Почему этот файл появился: службу запускает systemd через `EnvironmentFile`,
а он — в отличие от `python-dotenv`, на котором живут оба бота, — считает
значением всё до конца строки, вместе с поясняющим комментарием. На боевой
установке 21.09 первый запуск упал именно на этом: строка
`NOTIFY_POLL_INTERVAL_SEC=60   # как часто заглядывать в ящик, секунд`
приехала в код целиком.

Тесты здесь держат два правила: служба переживает хвостовой комментарий,
а сам образец `.env.example` пригоден как `EnvironmentFile`.
"""

from __future__ import annotations

import re
from pathlib import Path

from notifyd.config import Settings

REQUIRED = {
    "NOTIFY_DB_DSN": "postgresql://notify:pass@127.0.0.1:5432/clients_db",
    "WORKER_TG_TOKEN": "111:worker",
    "ADMINBOT_TG_TOKEN": "222:adminbot",
    "MY_ADMIN_TG_TOKEN": "333:myadmin",
}


def _fill_env(monkeypatch, **extra) -> None:
    for name, value in {**REQUIRED, **extra}.items():
        monkeypatch.setenv(name, value)


def test_inline_comment_does_not_break_startup(monkeypatch):
    """Комментарий в конце строки — ровно то, что уронило первый запуск."""
    _fill_env(
        monkeypatch,
        NOTIFY_POLL_INTERVAL_SEC="60   # как часто заглядывать в ящик, секунд",
        NOTIFY_WATCHDOG_DB_TIMEOUT_SEC="5   # таймаут проверки «база отвечает»",
        NOTIFY_ENABLED="1   # 1 — служба отправляет",
        TECH_JOURNAL_CHAT_ID="-1002   # технический журнал, без звука",
    )

    settings = Settings.from_env()

    assert settings.poll_interval_sec == 60
    assert settings.watchdog_db_timeout_sec == 5.0
    assert settings.enabled is True
    assert settings.tech_journal_chat_id == -1002


def test_hash_inside_a_secret_is_not_a_comment(monkeypatch):
    """Решётка внутри пароля остаётся частью пароля: комментарий начинается
    только после пробела, а пробелов в строке подключения не бывает."""
    _fill_env(
        monkeypatch,
        NOTIFY_DB_DSN="postgresql://notify:pa#ss@127.0.0.1:5432/clients_db",
    )

    settings = Settings.from_env()

    assert settings.db_dsn == "postgresql://notify:pa#ss@127.0.0.1:5432/clients_db"


def test_env_example_is_usable_as_systemd_environment_file():
    """Образец не должен возвращаться к комментариям справа от значения:
    тот, кто скопирует его в .env, получит нерабочую службу."""
    example = Path(__file__).resolve().parent.parent / ".env.example"

    offenders = [line for line in example.read_text(encoding="utf-8").splitlines()
                 if re.match(r"^[A-Z0-9_]+=.*\s#", line)]

    assert offenders == [], offenders


SWITCHES = (
    "NOTIFY_ENABLED",
    "NOTIFY_DRY_RUN",
    "NOTIFY_JOURNAL_ENABLED",
    "NOTIFY_WATCHDOG_ENABLED",
    "NOTIFY_INCIDENTS_ENABLED",
    "NOTIFY_MY_ADMIN_ENABLED",
)


def _example(name: str) -> str:
    return (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")


def test_switches_live_apart_from_secrets():
    """Решение владельца 21.09: выключатели отдельно от токенов, чтобы шаги
    выката можно было вести без доступа к секретам. Тест держит раскладку:
    в файле с секретами выключателей нет, в файле выключателей есть все."""
    secrets_file = _example(".env.example")
    switches_file = _example("switches.env.example")

    for name in SWITCHES:
        assert not re.search(rf"^{name}=", secrets_file, re.M), \
            f"{name} вернулся в .env.example — секреты и выключатели снова в одном файле"
        assert re.search(rf"^{name}=", switches_file, re.M), \
            f"{name} отсутствует в switches.env.example"


def test_switches_file_holds_no_secrets():
    """Файл выключателей лежит доступным для правки без sudo — токену,
    паролю или строке подключения в нём места нет."""
    switches_file = _example("switches.env.example")

    forbidden = [line for line in switches_file.splitlines()
                 if re.match(r"^[A-Z0-9_]*(TOKEN|DSN|PASSWORD|SECRET)[A-Z0-9_]*=", line)]

    assert forbidden == [], forbidden
