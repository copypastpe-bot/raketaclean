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
