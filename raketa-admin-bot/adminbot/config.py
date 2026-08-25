"""Конфигурация сервиса: читается из окружения один раз при старте.

Правила дизайна (docs/plans/2026-08-24-amo-sync-design.md §5.5):
- каждая функция имеет свой выключатель, по умолчанию ВЫКЛЮЧЕНА;
- режим репетиции (dry-run) включён по умолчанию: решения принимаем, в амо не пишем.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

# Обязательные переменные окружения; порядок важен — сообщение об ошибке
# называет первую недостающую.
REQUIRED_ENV = (
    "ADMINBOT_TG_TOKEN",
    "ADMINBOT_OWNER_TG_ID",
    "BOT_DB_DSN",
    "AMO_BASE_URL",
    "AMO_TOKEN",
)

_TRUE_VALUES = {"1", "true"}

# Хвост непроведённых заказов разбираем с этой даты (решение владельца №5).
DEFAULT_BACKLOG_FROM = date(2026, 8, 21)

# Какую «Услугу» ставить в сделке, если поле пустое (решение владельца №7).
# Ключ — часть имени мастера, значение — вид работ. Новый мастер добавляется
# строкой в настройках, без правки кода.
SERVICE_KINDS = ("furniture", "cleaning")
DEFAULT_SERVICE_BY_MASTER: dict[str, str] = {
    "никита": "furniture",
    "дмитрий": "furniture",
    "дима": "furniture",
    "ольга": "cleaning",
    "оля": "cleaning",
}


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from exc


def _date(name: str, default: date) -> date:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"Переменная {name} должна быть датой ГГГГ-ММ-ДД, получено: {raw!r}") from exc


def _service_by_master(name: str, default: dict[str, str]) -> dict[str, str]:
    """Разобрать строку «Никита:furniture, Оля:cleaning».

    Опечатку в названии услуги ловим при запуске: в сделке клиента она обойдётся
    дороже, чем упавший старт сервиса.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return dict(default)

    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        if not pair.strip():
            continue
        master, _, service = pair.partition(":")
        service = service.strip().lower()
        if service not in SERVICE_KINDS:
            raise RuntimeError(
                f"Переменная {name}: неизвестная услуга {service!r} у мастера "
                f"{master.strip()!r}. Допустимо: {', '.join(SERVICE_KINDS)}"
            )
        mapping[master.strip().lower()] = service
    return mapping


@dataclass(frozen=True)
class Settings:
    """Настройки сервиса. Неизменяемы после чтения окружения."""

    # Telegram владельца
    tg_token: str
    owner_tg_id: int

    # Базы данных
    bot_db_dsn: str          # БД рабочего бота — ТОЛЬКО ЧТЕНИЕ
    own_db_dsn: str          # своя схема adminbot (по умолчанию тот же Postgres)

    # amoCRM
    amo_base_url: str
    amo_token: str

    # Выключатели функции amo_sync
    amo_sync_enabled: bool = False   # kill switch: по умолчанию функция выключена
    amo_sync_dry_run: bool = True    # по умолчанию репетиция: решаем, но не пишем

    # Режим работы
    backlog_from: date = DEFAULT_BACKLOG_FROM
    reconcile_hour_msk: int = 21
    salesbot_wait_sec: int = 600     # сколько ждём автосделку сейлзбота (дизайн §5.3)
    poll_interval_sec: int = 60

    # Мастер заказа → вид работ для поля «Услуга»
    service_by_master: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SERVICE_BY_MASTER))

    @classmethod
    def from_env(cls) -> "Settings":
        """Собрать настройки из переменных окружения.

        Обязательные переменные без значения → RuntimeError с именем переменной.
        """
        values = {name: _require(name) for name in REQUIRED_ENV}

        try:
            owner_tg_id = int(values["ADMINBOT_OWNER_TG_ID"])
        except ValueError as exc:
            raise RuntimeError(
                "Переменная ADMINBOT_OWNER_TG_ID должна быть числовым Telegram id"
            ) from exc

        bot_db_dsn = values["BOT_DB_DSN"]
        own_db_dsn = os.environ.get("ADMINBOT_DB_DSN", "").strip() or bot_db_dsn

        return cls(
            tg_token=values["ADMINBOT_TG_TOKEN"],
            owner_tg_id=owner_tg_id,
            bot_db_dsn=bot_db_dsn,
            own_db_dsn=own_db_dsn,
            amo_base_url=values["AMO_BASE_URL"].rstrip("/"),
            amo_token=values["AMO_TOKEN"],
            amo_sync_enabled=_flag("AMO_SYNC_ENABLED", False),
            amo_sync_dry_run=_flag("AMO_SYNC_DRY_RUN", True),
            backlog_from=_date("AMO_SYNC_BACKLOG_FROM", DEFAULT_BACKLOG_FROM),
            reconcile_hour_msk=_int("AMO_SYNC_RECONCILE_HOUR_MSK", 21),
            salesbot_wait_sec=_int("AMO_SYNC_SALESBOT_WAIT_SEC", 600),
            poll_interval_sec=_int("AMO_SYNC_POLL_INTERVAL_SEC", 60),
            service_by_master=_service_by_master("SERVICE_BY_MASTER",
                                                 DEFAULT_SERVICE_BY_MASTER),
        )
