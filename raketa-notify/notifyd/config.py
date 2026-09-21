"""Настройки службы: читаются из окружения один раз при старте.

Правило проекта (ТЗ 2026-09-18, задача 3): выключатель обязателен и по умолчанию
ВЫКЛЮЧЕН; режим репетиции — по умолчанию ВКЛЮЧЁН, как у остальных функций обоих
ботов (адрес, куда реально шлём, включает владелец явно).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from notifyd.tg_session import parse_ip_pool

# Обязательные переменные окружения; порядок важен — сообщение об ошибке
# называет первую недостающую (тот же приём, что в adminbot/config.py).
REQUIRED_ENV = (
    "NOTIFY_DB_DSN",
    "WORKER_TG_TOKEN",
    "ADMINBOT_TG_TOKEN",
    "MY_ADMIN_TG_TOKEN",
)

_TRUE_VALUES = {"1", "true"}

# Адреса справочника notify.routes (CHECK-ограничение миграции 001) — здесь же
# перечислены, чтобы CLI и служба сверялись с одним и тем же списком.
ADDRESSES = ("work_chat", "ops_feed", "tech_journal", "my_assistant", "my_admin", "manager")
LEVELS = ("red", "yellow", "grey")


# systemd читает EnvironmentFile буквально: всё после «=» и до конца строки —
# это значение, вместе с нашим поясняющим комментарием. python-dotenv, на
# котором живут оба бота, хвостовой комментарий срезает сам, поэтому привычка
# копировать строки из настроек ботов («60   # как часто заглядывать…»)
# роняла службу на старте — поймано на боевой установке 21.09.
# Срезаем сами. Перед решёткой обязателен пробел, поэтому пароль, токен или
# URL с «#» внутри не пострадают: пробелов в них не бывает.
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")


def _clean(raw: Optional[str]) -> str:
    """Значение переменной без хвостового комментария и внешних пробелов."""
    if raw is None:
        return ""
    return _INLINE_COMMENT_RE.sub("", raw).strip()


def _require(name: str) -> str:
    value = _clean(os.environ.get(name))
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


def _flag(name: str, default: bool) -> bool:
    raw = _clean(os.environ.get(name))
    if not raw:
        return default
    return raw.lower() in _TRUE_VALUES


def _int(name: str, default: int) -> int:
    raw = _clean(os.environ.get(name))
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Переменная {name} должна быть целым числом, получено: {raw!r}"
        ) from exc


def _float(name: str, default: float) -> float:
    raw = _clean(os.environ.get(name))
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Переменная {name} должна быть числом, получено: {raw!r}"
        ) from exc


def _chat_id(name: str) -> Optional[int]:
    """Номер чата для одного адреса справочника.

    Пусто — адрес не настроен. Это не ошибка сама по себе: почтальон падает
    не при старте, а только если справочник и правда сослался на пустой адрес
    (тогда событие откладывается, а не отправляется в пустоту).
    """
    raw = _clean(os.environ.get(name))
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Переменная {name} должна быть числовым id чата, получено: {raw!r}"
        ) from exc


@dataclass(frozen=True)
class Settings:
    """Настройки службы. Неизменяемы после чтения окружения."""

    db_dsn: str

    # Токены. Рабочий бот и админ-бот — уже существующие боты (их токены просто
    # копируются в .env этой службы); My_admin — новый бот, завести должен владелец.
    worker_tg_token: str
    adminbot_tg_token: str
    my_admin_tg_token: str

    enabled: bool = False    # kill switch: по умолчанию служба ничего не отправляет
    dry_run: bool = True     # по умолчанию репетиция: решения принимаем, никуда не шлём

    poll_interval_sec: int = 60
    batch_limit: int = 20

    # Переходник журнала (задача 5) — свой выключатель, по умолчанию
    # ВЫКЛЮЧЕН (правило проекта), отдельно от NOTIFY_ENABLED почтальона:
    # можно читать journalctl и класть в ящик, не трогая остальную службу,
    # и наоборот — почтальон работает, даже если переходник выключен.
    journal_enabled: bool = False
    journal_poll_interval_sec: int = 60
    journal_max_per_minute: int = 20

    # Сторож (задача 7) — свой выключатель, по умолчанию ВЫКЛЮЧЕН (по «Порядку
    # выката» ТЗ включается вместе с пульсом ботов, задача 6, шагом позже
    # переходника журнала). Определяет только «сломано/работает» — инциденты
    # (задача 8) не пишет и не открывает.
    watchdog_enabled: bool = False
    watchdog_poll_interval_sec: int = 60
    # Пороги «сломано» — по возрасту последней отметки/успеха. 5 минут для
    # пульса — то же число, что уже проверено на клиентском боте
    # (CLIENT_BOT_HEALTH_MAX_AGE_SEC, bot.py:319); для amoCRM — тот же порядок
    # (опрос по умолчанию раз в 30 сек, AMOCRM_POLL_INTERVAL_SEC, bot.py:268,
    # 5 минут — большой запас на разовую заминку CRM).
    watchdog_heartbeat_max_age_sec: int = 300
    watchdog_amocrm_max_age_sec: int = 300
    watchdog_db_timeout_sec: float = 5.0
    watchdog_proxy_timeout_sec: float = 5.0
    # Рассыльщик клиентам ходит раз в CLIENT_MESSAGING_INTERVAL_SEC (по
    # умолчанию 600 сек, bot.py:284) — порог с запасом в 3 цикла, чтобы одна
    # медленная попытка не считалась поломкой.
    watchdog_dispatch_max_age_sec: int = 1800

    # Инциденты и напоминания (задача 8) — свой выключатель, по умолчанию
    # ВЫКЛЮЧЕН, по «Порядку выката» ТЗ включается ПОСЛЕДНИМ, после сторожа
    # (задачи 6-7). Расписание напоминаний (10 минут/сутки, потолок,
    # эскалация) — решения владельца, зафиксированы константами в
    # notifyd/incidents.py, а не настройками: это не операционная подстройка,
    # а зафиксированное правило.
    incidents_enabled: bool = False

    # Слушатель My_admin (кнопки инцидентов задачи 8, команда /status задачи
    # 9) — свой выключатель, отдельно от incidents_enabled: /status полезен
    # и до включения напоминаний, а кнопки без открытых инцидентов безвредны.
    # owner_tg_id — решение исполнителя (см. notifyd/admin_bot.py): без него
    # слушатель не поднимается вовсе (в журнал — почему), а не принимает
    # команды от кого попало.
    my_admin_listener_enabled: bool = False
    my_admin_owner_tg_id: Optional[int] = None

    # Дорога до Telegram. С боевого VPS имя api.telegram.org не разрешается, и оба
    # бота давно ходят по прямым адресам и через прокси. Служба обязана ходить так же,
    # иначе на проде не отправит ничего (найдено координатором при проверке задачи 3).
    telegram_api_ips: tuple[str, ...] = field(default_factory=tuple)
    telegram_proxy_url: str = ""

    # Номера чатов по адресам справочника notify.routes.address. Какой бот
    # физически пишет в какой адрес — решение исполнителя этой задачи (не в ТЗ):
    # my_assistant — существующий бот админ-бота (решение владельца 18.09, это
    # он и есть); my_admin — новый бот; work_chat/ops_feed/tech_journal/manager —
    # рабочий бот, он уже пишет в чат логов и чат менеджера сегодня (факты
    # разведки). Комментарий явный, чтобы владелец мог поправить, если не угадано.
    work_chat_id: Optional[int] = None
    ops_feed_chat_id: Optional[int] = None
    tech_journal_chat_id: Optional[int] = None
    my_assistant_chat_id: Optional[int] = None
    my_admin_chat_id: Optional[int] = None
    manager_chat_id: Optional[int] = None

    @classmethod
    def from_env(cls) -> "Settings":
        """Собрать настройки из переменных окружения.

        Обязательные переменные без значения -> RuntimeError с именем переменной.
        """
        values = {name: _require(name) for name in REQUIRED_ENV}
        return cls(
            db_dsn=values["NOTIFY_DB_DSN"],
            worker_tg_token=values["WORKER_TG_TOKEN"],
            adminbot_tg_token=values["ADMINBOT_TG_TOKEN"],
            my_admin_tg_token=values["MY_ADMIN_TG_TOKEN"],
            enabled=_flag("NOTIFY_ENABLED", False),
            dry_run=_flag("NOTIFY_DRY_RUN", True),
            poll_interval_sec=_int("NOTIFY_POLL_INTERVAL_SEC", 60),
            batch_limit=_int("NOTIFY_BATCH_LIMIT", 20),
            journal_enabled=_flag("NOTIFY_JOURNAL_ENABLED", False),
            journal_poll_interval_sec=_int("NOTIFY_JOURNAL_POLL_INTERVAL_SEC", 60),
            journal_max_per_minute=_int("NOTIFY_JOURNAL_MAX_PER_MINUTE", 20),
            watchdog_enabled=_flag("NOTIFY_WATCHDOG_ENABLED", False),
            watchdog_poll_interval_sec=_int("NOTIFY_WATCHDOG_POLL_INTERVAL_SEC", 60),
            watchdog_heartbeat_max_age_sec=_int("NOTIFY_WATCHDOG_HEARTBEAT_MAX_AGE_SEC", 300),
            watchdog_amocrm_max_age_sec=_int("NOTIFY_WATCHDOG_AMOCRM_MAX_AGE_SEC", 300),
            watchdog_db_timeout_sec=_float("NOTIFY_WATCHDOG_DB_TIMEOUT_SEC", 5.0),
            watchdog_proxy_timeout_sec=_float("NOTIFY_WATCHDOG_PROXY_TIMEOUT_SEC", 5.0),
            watchdog_dispatch_max_age_sec=_int("NOTIFY_WATCHDOG_DISPATCH_MAX_AGE_SEC", 1800),
            incidents_enabled=_flag("NOTIFY_INCIDENTS_ENABLED", False),
            my_admin_listener_enabled=_flag("NOTIFY_MY_ADMIN_ENABLED", False),
            my_admin_owner_tg_id=_chat_id("MY_ADMIN_OWNER_TG_ID"),
            work_chat_id=_chat_id("WORK_CHAT_ID"),
            ops_feed_chat_id=_chat_id("OPS_FEED_CHAT_ID"),
            tech_journal_chat_id=_chat_id("TECH_JOURNAL_CHAT_ID"),
            my_assistant_chat_id=_chat_id("MY_ASSISTANT_CHAT_ID"),
            my_admin_chat_id=_chat_id("MY_ADMIN_CHAT_ID"),
            manager_chat_id=_chat_id("MANAGER_CHAT_ID"),
            telegram_api_ips=tuple(parse_ip_pool(_clean(os.environ.get("TELEGRAM_API_IPS")))),
            telegram_proxy_url=_clean(os.environ.get("TELEGRAM_PROXY_URL")),
        )
