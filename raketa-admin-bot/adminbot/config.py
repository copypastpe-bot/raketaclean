"""Конфигурация сервиса: читается из окружения один раз при старте.

Правила дизайна (docs/plans/2026-08-24-amo-sync-design.md §5.5):
- каждая функция имеет свой выключатель, по умолчанию ВЫКЛЮЧЕНА;
- режим репетиции (dry-run) включён по умолчанию: решения принимаем, в амо не пишем.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from adminbot.tg.session import parse_ip_pool

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

# Рабочий календарь компании и ключ служебного аккаунта на сервере (этап 2).
# Календарей может быть несколько: мебель и ковры ведут мастера в основном,
# уборки — бригадир в своём (решение владельца 2026-09-01). В настройке они
# перечисляются через запятую; порядок важен — см. `_calendar_ids`.
DEFAULT_CALENDAR_ID = "raketaclean52@gmail.com"
DEFAULT_GCAL_KEY_FILE = "~/.gcal.json"

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


def parse_calendar_ids(raw: str, default: str = DEFAULT_CALENDAR_ID) -> tuple[str, ...]:
    """Адреса календарей из строки «через запятую», по порядку и без повторов.

    Порядок важен: первый календарь считается основным и наследует закладку
    обмена, снятую до того, как календарей стало несколько (миграция 008).
    Пустые куски отбрасываем: пустой адрес — это запрос ко всему аккаунту,
    Google ответил бы отказом, и обмен встал бы целиком.

    Отдельной функцией — потому что тем же правилом пользуются скрипты
    диагностики, а расходиться им нельзя.
    """
    ids: list[str] = []
    for chunk in (raw or "").split(","):
        value = chunk.strip()
        if value and value not in ids:
            ids.append(value)
    return tuple(ids) or (default,)


def _calendar_ids(name: str, default: str) -> tuple[str, ...]:
    return parse_calendar_ids(os.environ.get(name, "").strip() or default, default)


def _manager_dials(name: str) -> tuple[str, ...]:
    """Телефоны менеджера из строки «через запятую», по порядку и без повторов.

    Порядок значим: первый номер — рабочий, туда идёт первая попытка;
    следующий — личный, туда уходит повтор после неудачи менеджера. Пустых
    значений по умолчанию нет: без телефона функция autocall просто не
    включается (проверка в main.py).
    """
    dials: list[str] = []
    for chunk in os.environ.get(name, "").split(","):
        value = chunk.strip()
        if value and value not in dials:
            dials.append(value)
    return tuple(dials)


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


def _optional_date(name: str) -> Optional[date]:
    """Дата, которой может и не быть: тогда решение принимается при запуске."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"Переменная {name} должна быть датой ГГГГ-ММ-ДД, "
                           f"получено: {raw!r}") from exc


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
    # Сколько ждём автосделку сейлзбота. Обычно она появляется за секунды, но амо
    # подтормаживает, и владелец видел задержки до 20 минут (2026-08-26). Берём
    # запас вдвое: лишний вопрос владельцу дороже лишнего ожидания.
    salesbot_wait_sec: int = 2400
    poll_interval_sec: int = 60

    # Мастер заказа → вид работ для поля «Услуга»
    service_by_master: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SERVICE_BY_MASTER))

    # Уборки клининг-контура: свой выключатель и своя репетиция.
    # Работы приходят из отдельной таблицы бота (`public.cleaning_orders`),
    # поэтому и поток свой — со своей таблицей связок.
    cleaning_sync_enabled: bool = False
    cleaning_sync_dry_run: bool = True
    # С какой даты проводить уборки. Пусто — со дня включения: старые уборки
    # владелец закрывает сам (его решение 2026-09-10).
    cleaning_backlog_from: Optional[date] = None

    # Ковры от партнёра: свой выключатель и свой режим репетиции.
    carpets_enabled: bool = False
    carpets_dry_run: bool = True
    carpets_poll_interval_sec: int = 3600

    # Календарь (этап 2): свой выключатель и своя репетиция.
    # Опрос раз в пять минут: записи появляются в рабочее время, чаще незачем,
    # а обмен приносит только изменения и стоит одного запроса.
    gcal_enabled: bool = False
    gcal_dry_run: bool = True
    gcal_poll_interval_sec: int = 300
    # Календарей может быть несколько; первый — основной (наследует закладку).
    gcal_calendar_ids: tuple[str, ...] = (DEFAULT_CALENDAR_ID,)
    gcal_key_file: str = DEFAULT_GCAL_KEY_FILE
    # С какой даты читаем календарь. Пусто — со дня включения: записи, лежавшие
    # там раньше, владелец ведёт сам (его решение 8).
    gcal_sync_from: Optional[date] = None

    # Автозвонок по заявке с сайта: свой выключатель и своя репетиция.
    # Опрос раз в полминуты: заявка остывает за минуты, тянуть нельзя.
    autocall_enabled: bool = False
    autocall_dry_run: bool = True
    autocall_poll_interval_sec: int = 30
    # Окно, когда можно звонить клиенту (часы по Москве): вне окна заявка ждёт.
    autocall_window_from_hour: int = 10
    autocall_window_to_hour: int = 20
    # АТС: адрес, ключ и телефоны менеджера.
    # Не в REQUIRED_ENV: нужны только при включённой функции autocall.
    pbx_base_url: str = ""
    pbx_api_key: str = ""
    # Телефоны менеджера по порядку: первый — рабочий, второй — личный.
    # Перечисляются через запятую в PBX_MANAGER_DIAL. Раньше здесь стоял
    # внутренний номер АТС, и цепочку «не ответил за 10 секунд — звони на
    # мобильный» строила сама АТС. Так не работает: правила переадресации
    # применяются только к входящим звонкам, а звонок робота идёт через API
    # (проверено на стенде 2026-09-02). Поэтому номера ведёт робот.
    pbx_manager_dials: tuple[str, ...] = ()
    # Токен рабочего бота и чат менеджера: уведомление уходит от имени бота,
    # с которым менеджер уже работает, а не от админ-бота владельца.
    worker_tg_token: str = ""
    manager_tg_chat_id: int = 0

    # Прямые адреса Telegram: на российском сервере имя api.telegram.org
    # не разрешается, хотя сами адреса доступны. Пусто — обычный путь.
    telegram_api_ips: tuple[str, ...] = ()

    # Прокси до Telegram: блокировка идёт волнами и гасит все прямые адреса
    # разом, поэтому трафик уводится через сервер вне РФ. Пусто — прямой путь.
    telegram_proxy_url: str = ""

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
            telegram_api_ips=tuple(parse_ip_pool(os.environ.get("TELEGRAM_API_IPS"))),
            telegram_proxy_url=(os.environ.get("TELEGRAM_PROXY_URL") or "").strip(),
            cleaning_sync_enabled=_flag("CLEANING_SYNC_ENABLED", False),
            cleaning_sync_dry_run=_flag("CLEANING_SYNC_DRY_RUN", True),
            cleaning_backlog_from=_optional_date("CLEANING_BACKLOG_FROM"),
            carpets_enabled=_flag("CARPETS_ENABLED", False),
            carpets_dry_run=_flag("CARPETS_DRY_RUN", True),
            carpets_poll_interval_sec=_int("CARPETS_POLL_INTERVAL_SEC", 3600),
            gcal_enabled=_flag("GCAL_ENABLED", False),
            gcal_dry_run=_flag("GCAL_DRY_RUN", True),
            gcal_poll_interval_sec=_int("GCAL_POLL_INTERVAL_SEC", 300),
            gcal_calendar_ids=_calendar_ids("GCAL_CALENDAR_ID", DEFAULT_CALENDAR_ID),
            gcal_key_file=(os.environ.get("GCAL_SERVICE_ACCOUNT_FILE", "").strip()
                           or DEFAULT_GCAL_KEY_FILE),
            gcal_sync_from=_optional_date("GCAL_SYNC_FROM"),
            autocall_enabled=_flag("AUTOCALL_ENABLED", False),
            autocall_dry_run=_flag("AUTOCALL_DRY_RUN", True),
            autocall_poll_interval_sec=_int("AUTOCALL_POLL_INTERVAL_SEC", 30),
            autocall_window_from_hour=_int("AUTOCALL_WINDOW_FROM", 10),
            autocall_window_to_hour=_int("AUTOCALL_WINDOW_TO", 20),
            pbx_base_url=os.environ.get("PBX_BASE_URL", "").strip().rstrip("/"),
            pbx_api_key=os.environ.get("PBX_API_KEY", "").strip(),
            pbx_manager_dials=_manager_dials("PBX_MANAGER_DIAL"),
            worker_tg_token=os.environ.get("WORKER_TG_TOKEN", "").strip(),
            manager_tg_chat_id=_int("MANAGER_TG_CHAT_ID", 0),
        )
