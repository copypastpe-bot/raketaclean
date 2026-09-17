from adminbot.config import Settings


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("ADMINBOT_TG_TOKEN", "123:abc")
    monkeypatch.setenv("ADMINBOT_OWNER_TG_ID", "42")
    monkeypatch.setenv("BOT_DB_DSN", "postgresql://ro@localhost/clients_db")
    monkeypatch.setenv("AMO_BASE_URL", "https://raketacleancrm.amocrm.ru")
    monkeypatch.setenv("AMO_TOKEN", "tok")
    s = Settings.from_env()
    assert s.owner_tg_id == 42
    assert s.amo_sync_enabled is False      # по умолчанию ВЫКЛЮЧЕНО (дизайн: kill switch)
    assert s.amo_sync_dry_run is True       # по умолчанию репетиция, не запись


def test_settings_missing_required(monkeypatch):
    monkeypatch.delenv("ADMINBOT_TG_TOKEN", raising=False)
    import pytest
    with pytest.raises(RuntimeError, match="ADMINBOT_TG_TOKEN"):
        Settings.from_env()


# --- какую «Услугу» ставить по мастеру заказа ---

def _minimal_env(monkeypatch):
    monkeypatch.setenv("ADMINBOT_TG_TOKEN", "123:abc")
    monkeypatch.setenv("ADMINBOT_OWNER_TG_ID", "42")
    monkeypatch.setenv("BOT_DB_DSN", "postgresql://ro@localhost/clients_db")
    monkeypatch.setenv("AMO_BASE_URL", "https://raketacleancrm.amocrm.ru")
    monkeypatch.setenv("AMO_TOKEN", "tok")


def test_service_by_master_has_working_defaults(monkeypatch):
    """Действующие мастера работают без настройки: Никита и Дима — мебель, Оля — уборка."""
    _minimal_env(monkeypatch)
    monkeypatch.delenv("SERVICE_BY_MASTER", raising=False)

    mapping = Settings.from_env().service_by_master

    assert mapping["никита"] == "furniture"
    assert mapping["оля"] == "cleaning"


def test_new_master_is_added_without_touching_code(monkeypatch):
    _minimal_env(monkeypatch)
    monkeypatch.setenv("SERVICE_BY_MASTER", "Никита:furniture, Оля:cleaning, Пётр:cleaning")

    mapping = Settings.from_env().service_by_master

    assert mapping == {"никита": "furniture", "оля": "cleaning", "пётр": "cleaning"}


def test_unknown_service_name_is_rejected_at_start(monkeypatch):
    """Опечатку в настройке лучше поймать при запуске, чем в сделке клиента."""
    import pytest

    _minimal_env(monkeypatch)
    monkeypatch.setenv("SERVICE_BY_MASTER", "Никита:мебелька")

    with pytest.raises(RuntimeError, match="SERVICE_BY_MASTER"):
        Settings.from_env()


# --- календари: их может быть несколько ---

def test_calendar_defaults_to_the_main_one(monkeypatch):
    """Настройки нет — читаем основной календарь, как и раньше."""
    _minimal_env(monkeypatch)
    monkeypatch.delenv("GCAL_CALENDAR_ID", raising=False)

    assert Settings.from_env().gcal_calendar_ids == ("raketaclean52@gmail.com",)


def test_single_calendar_still_works(monkeypatch):
    _minimal_env(monkeypatch)
    monkeypatch.setenv("GCAL_CALENDAR_ID", "one@gmail.com")

    assert Settings.from_env().gcal_calendar_ids == ("one@gmail.com",)


def test_two_calendars_are_read_in_order(monkeypatch):
    """Порядок важен: первый календарь наследует закладку, снятую до этой правки."""
    _minimal_env(monkeypatch)
    monkeypatch.setenv("GCAL_CALENDAR_ID",
                       "raketaclean52@gmail.com, brigade@group.calendar.google.com")

    assert Settings.from_env().gcal_calendar_ids == (
        "raketaclean52@gmail.com", "brigade@group.calendar.google.com")


def test_sloppy_separators_do_not_create_empty_calendars(monkeypatch):
    """Лишние запятые и пробелы в .env — обычное дело; пустой адрес роняет обмен."""
    _minimal_env(monkeypatch)
    monkeypatch.setenv("GCAL_CALENDAR_ID", " , one@gmail.com ,, two@gmail.com , ")

    assert Settings.from_env().gcal_calendar_ids == ("one@gmail.com", "two@gmail.com")


def test_same_calendar_twice_is_read_once(monkeypatch):
    """Дубль в настройке означал бы два обмена и две закладки по одному адресу."""
    _minimal_env(monkeypatch)
    monkeypatch.setenv("GCAL_CALENDAR_ID", "one@gmail.com, one@gmail.com")

    assert Settings.from_env().gcal_calendar_ids == ("one@gmail.com",)


# --- автозвонок по заявке с сайта (autocall) ---

_AUTOCALL_ENV = (
    "AUTOCALL_ENABLED",
    "AUTOCALL_DRY_RUN",
    "AUTOCALL_POLL_INTERVAL_SEC",
    "AUTOCALL_WINDOW_FROM",
    "AUTOCALL_WINDOW_TO",
    "PBX_BASE_URL",
    "PBX_API_KEY",
    "PBX_MANAGER_DIAL",
    "WORKER_TG_TOKEN",
    "MANAGER_TG_CHAT_ID",
)


def test_autocall_defaults(monkeypatch):
    """Функция выключена, репетиция включена; АТС и рабочий бот не настроены."""
    _minimal_env(monkeypatch)
    for name in _AUTOCALL_ENV:
        monkeypatch.delenv(name, raising=False)

    s = Settings.from_env()

    assert s.autocall_enabled is False      # kill switch: по умолчанию ВЫКЛЮЧЕНО
    assert s.autocall_dry_run is True       # по умолчанию репетиция, не звонок
    assert s.autocall_poll_interval_sec == 30
    assert s.autocall_window_from_hour == 10
    assert s.autocall_window_to_hour == 20
    assert s.pbx_base_url == ""
    assert s.pbx_api_key == ""
    assert s.pbx_manager_dials == ()
    assert s.worker_tg_token == ""
    assert s.manager_tg_chat_id == 0


def test_autocall_env_overrides(monkeypatch):
    _minimal_env(monkeypatch)
    monkeypatch.setenv("AUTOCALL_ENABLED", "1")
    monkeypatch.setenv("AUTOCALL_DRY_RUN", "0")
    monkeypatch.setenv("AUTOCALL_POLL_INTERVAL_SEC", "15")
    monkeypatch.setenv("AUTOCALL_WINDOW_FROM", "9")
    monkeypatch.setenv("AUTOCALL_WINDOW_TO", "21")
    # Хвостовой слэш в .env срезается: иначе при склейке путей выйдет «//».
    monkeypatch.setenv("PBX_BASE_URL", "https://pbx.example.com/")
    monkeypatch.setenv("PBX_API_KEY", "pbx-key")
    monkeypatch.setenv("PBX_MANAGER_DIAL", "101")
    monkeypatch.setenv("WORKER_TG_TOKEN", "456:def")
    monkeypatch.setenv("MANAGER_TG_CHAT_ID", "777")

    s = Settings.from_env()

    assert s.autocall_enabled is True
    assert s.autocall_dry_run is False
    assert s.autocall_poll_interval_sec == 15
    assert s.autocall_window_from_hour == 9
    assert s.autocall_window_to_hour == 21
    assert s.pbx_base_url == "https://pbx.example.com"
    assert s.pbx_api_key == "pbx-key"
    assert s.pbx_manager_dials == ("101",)
    assert s.worker_tg_token == "456:def"
    assert s.manager_tg_chat_id == 777


def test_pbx_manager_dial_accepts_work_and_personal_phones(monkeypatch):
    """Два телефона менеджера — через запятую, порядок важен.

    Первый — рабочий, на него идёт первая попытка; второй — личный, туда
    уходит повтор, если рабочий молчал (решение владельца 2026-09-02).
    Пробелы вокруг запятой срезаем: в .env их ставят машинально.
    """
    _minimal_env(monkeypatch)
    monkeypatch.setenv("PBX_MANAGER_DIAL", "89001112233, 89004445566")

    s = Settings.from_env()

    assert s.pbx_manager_dials == ("89001112233", "89004445566")


# --- прокси до Telegram ---

def test_telegram_proxy_is_off_by_default(monkeypatch):
    """Пусто — значит прямой путь, как было до появления прокси."""
    _minimal_env(monkeypatch)
    monkeypatch.delenv("TELEGRAM_PROXY_URL", raising=False)
    assert Settings.from_env().telegram_proxy_url == ""


def test_telegram_proxy_is_read_from_env(monkeypatch):
    _minimal_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "  http://user:pass@75.119.153.118:39443  ")
    assert Settings.from_env().telegram_proxy_url == "http://user:pass@75.119.153.118:39443"


# --- напоминание про сделку без адреса (задача 7) ---

def test_address_reminder_is_off_by_default(monkeypatch):
    """Свой выключатель: выкатывается последним, владелец включает сам."""
    _minimal_env(monkeypatch)
    monkeypatch.delenv("ADDRESS_REMINDER_ENABLED", raising=False)
    monkeypatch.delenv("ADDRESS_REMINDER_POLL_INTERVAL_SEC", raising=False)

    s = Settings.from_env()

    assert s.address_reminder_enabled is False
    assert s.address_reminder_poll_interval_sec == 3600


def test_address_reminder_env_overrides(monkeypatch):
    _minimal_env(monkeypatch)
    monkeypatch.setenv("ADDRESS_REMINDER_ENABLED", "1")
    monkeypatch.setenv("ADDRESS_REMINDER_POLL_INTERVAL_SEC", "120")

    s = Settings.from_env()

    assert s.address_reminder_enabled is True
    assert s.address_reminder_poll_interval_sec == 120


# --- удаление заказа освобождает сделку (задача 7, ТЗ 2026-09-17) ---

def test_order_deletions_are_off_by_default(monkeypatch):
    """Свой выключатель, по умолчанию выключен и в репетиции."""
    _minimal_env(monkeypatch)
    monkeypatch.delenv("ORDER_DELETIONS_ENABLED", raising=False)
    monkeypatch.delenv("ORDER_DELETIONS_DRY_RUN", raising=False)

    s = Settings.from_env()

    assert s.order_deletions_enabled is False
    assert s.order_deletions_dry_run is True


def test_order_deletions_env_overrides(monkeypatch):
    _minimal_env(monkeypatch)
    monkeypatch.setenv("ORDER_DELETIONS_ENABLED", "1")
    monkeypatch.setenv("ORDER_DELETIONS_DRY_RUN", "0")

    s = Settings.from_env()

    assert s.order_deletions_enabled is True
    assert s.order_deletions_dry_run is False
