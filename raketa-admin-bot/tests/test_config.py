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
