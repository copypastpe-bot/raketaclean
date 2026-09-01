"""Устойчивость сервиса к обрывам связи с Telegram.

Telegram из России доступен нестабильно. Работа с amoCRM от него не зависит,
поэтому обрыв связи не должен ронять весь процесс: сделки продолжают
оформляться, а опрос возобновляется сам.
"""

from dataclasses import replace
import asyncio

from aiogram.exceptions import TelegramNetworkError

from adminbot.main import App


class FakeDispatcher:
    def __init__(self, failures: int):
        self.failures = failures
        self.attempts = 0

    async def start_polling(self, bot, **kwargs):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise TelegramNetworkError(method=None, message="Request timeout error")
        await asyncio.sleep(0)                     # штатный опрос

    async def stop_polling(self):
        pass


def make_app(dispatcher) -> App:
    return App(settings=None, bot_pool=None, own_pool=None, amo_clients=(),
               bot=None, dispatcher=dispatcher, watcher=None, reconciler=None,
               stop=asyncio.Event())


async def test_polling_resumes_after_a_network_error(monkeypatch):
    monkeypatch.setattr("adminbot.main.TELEGRAM_RETRY_SEC", 0)
    dispatcher = FakeDispatcher(failures=2)

    await make_app(dispatcher)._poll_until_stopped()

    assert dispatcher.attempts == 3                # две неудачи и рабочая попытка


async def test_polling_gives_up_when_service_is_stopping(monkeypatch):
    """Сигнал на остановку важнее повторных попыток."""
    monkeypatch.setattr("adminbot.main.TELEGRAM_RETRY_SEC", 0)
    dispatcher = FakeDispatcher(failures=99)
    app = make_app(dispatcher)

    async def stop_soon():
        await asyncio.sleep(0)
        app.stop.set()

    await asyncio.gather(app._poll_until_stopped(), stop_soon())

    assert dispatcher.attempts >= 1


async def test_calendar_absence_does_not_break_the_service():
    """Календарь выключен или ключа нет — остальные функции работают.

    Проверяется то, ради чего у каждой функции свой выключатель: робот уже
    в бою с уборкой и коврами, и новая функция не имеет права его уронить.
    """
    from adminbot.config import Settings
    from adminbot.main import _build_calendar

    settings = Settings(
        tg_token="t", owner_tg_id=1, bot_db_dsn="postgresql://x", own_db_dsn="postgresql://x",
        amo_base_url="https://x", amo_token="t", gcal_enabled=False)
    assert _build_calendar(settings, None, None, None, None) == (None, None, None)

    # Функция включена, но ключа на сервере нет — тоже не падаем.
    with_key_missing = replace(settings, gcal_enabled=True,
                               gcal_key_file="/nonexistent/gcal.json")
    assert _build_calendar(with_key_missing, None, None, None, None) == (None, None, None)


def test_every_configured_calendar_reaches_the_watcher(monkeypatch):
    """Второй календарь должен доехать до наблюдателя, а не потеряться в сборке.

    Порядок сохраняется: первому в списке достаётся закладка обмена, снятая
    до того, как календарей стало несколько.
    """
    from adminbot.config import Settings
    from adminbot.gcal import auth
    from adminbot.main import _build_calendar

    class FakeToken:
        @classmethod
        def from_file(cls, path):
            return cls()

        async def close(self):
            pass

    monkeypatch.setattr(auth, "ServiceAccountToken", FakeToken)

    settings = Settings(
        tg_token="t", owner_tg_id=1, bot_db_dsn="postgresql://x",
        own_db_dsn="postgresql://x", amo_base_url="https://x", amo_token="t",
        gcal_enabled=True, gcal_dry_run=True,
        gcal_calendar_ids=("main@gmail.com", "brigade@group.calendar.google.com"))

    watcher, store, token = _build_calendar(settings, None, object(), object(), object())

    assert [calendar.calendar_id for calendar in watcher.calendars] == [
        "main@gmail.com", "brigade@group.calendar.google.com"]


# --- _build_autocall: тот же приём «свой выключатель, мягкая деградация» ---

def _autocall_settings(**overrides):
    from adminbot.config import Settings

    return Settings(
        tg_token="t", owner_tg_id=1, bot_db_dsn="postgresql://x", own_db_dsn="postgresql://x",
        amo_base_url="https://x", amo_token="t", **overrides)


def test_autocall_disabled_returns_none():
    from adminbot.main import _build_autocall

    settings = _autocall_settings(autocall_enabled=False)

    assert _build_autocall(settings, None, None, None, None) == (None, None)


def test_autocall_dry_run_builds_watcher_with_memory_transport():
    """Репетиция: хранилище и АТС живут в памяти — правило «репетиция не
    оставляет следов» распространяется и на автозвонок. Токен рабочего бота
    не задан — второй элемент (бот менеджера) отсутствует."""
    from adminbot.autocall.pbx import MemoryPbx
    from adminbot.autocall.store import MemoryAutocallStore
    from adminbot.autocall.watcher import AutocallWatcher
    from adminbot.main import _build_autocall

    settings = _autocall_settings(autocall_enabled=True, autocall_dry_run=True)

    watcher, manager_bot = _build_autocall(settings, None, None, None, object())

    assert isinstance(watcher, AutocallWatcher)
    assert isinstance(watcher.store, MemoryAutocallStore)
    assert isinstance(watcher.engine.pbx, MemoryPbx)
    assert manager_bot is None


def test_autocall_dry_run_with_worker_settings_exposes_manager_bot():
    """Заданы WORKER_TG_TOKEN/MANAGER_TG_CHAT_ID — _build_autocall отдаёт
    бота менеджера наружу (не прячет его в замыкании отправителя), чтобы
    App мог закрыть его сессию при остановке сервиса (ревью Задачи 11)."""
    from aiogram import Bot

    from adminbot.main import _build_autocall

    settings = _autocall_settings(
        autocall_enabled=True, autocall_dry_run=True,
        worker_tg_token="123456:ABCDEF-fake-token-value", manager_tg_chat_id=999)

    watcher, manager_bot = _build_autocall(settings, None, None, None, object())

    assert watcher is not None
    assert isinstance(manager_bot, Bot)


def test_autocall_live_without_pbx_settings_degrades_softly(caplog):
    """Боевой режим без ключей АТС — функция просто не поднимается."""
    from adminbot.main import _build_autocall

    settings = _autocall_settings(autocall_enabled=True, autocall_dry_run=False)

    with caplog.at_level("WARNING"):
        result = _build_autocall(settings, None, None, object(), object())

    assert result == (None, None)
    assert any("PBX" in record.getMessage() for record in caplog.records)


def test_autocall_live_with_pbx_settings_builds_online_pbx():
    """Боевой режим с заданными ключами — с Задачи 7 это настоящий клиент
    OnlinePbx (раньше здесь была временная заглушка через getattr)."""
    from adminbot.autocall.pbx import OnlinePbx
    from adminbot.main import _build_autocall

    settings = _autocall_settings(
        autocall_enabled=True, autocall_dry_run=False,
        pbx_base_url="https://pbx.example", pbx_api_key="key", pbx_manager_dial="100")

    watcher, _manager_bot = _build_autocall(settings, None, None, object(), object())

    assert watcher is not None
    assert isinstance(watcher.engine.pbx, OnlinePbx)
    assert watcher.engine.pbx.base_url == "https://pbx.example"
    assert watcher.engine.pbx.api_key == "key"
    assert watcher.engine.manager_dial == "100"


def test_autocall_bad_window_raises():
    """Кривое окно валит старт — опечатку ловим при запуске, не в бою."""
    import pytest
    from adminbot.main import _build_autocall

    settings = _autocall_settings(
        autocall_enabled=True, autocall_window_from_hour=20, autocall_window_to_hour=10)

    with pytest.raises(RuntimeError):
        _build_autocall(settings, None, None, None, None)


# --- App.close(): сессия менеджерского бота закрывается вместе с сервисом ---

class FakeBotSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeBot:
    def __init__(self):
        self.session = FakeBotSession()


class FakePool:
    async def close(self):
        pass


async def test_close_closes_autocall_manager_bot_session():
    """App.close() должен закрыть сессию отдельного send-only бота менеджера —
    иначе после systemctl restart она остаётся висеть (ревью Задачи 11):
    aiogram открывает aiohttp-сессию лениво при первой отправке, и её никто,
    кроме App, не знает как закрыть."""
    main_bot = FakeBot()
    manager_bot = FakeBot()
    app = App(settings=None, bot_pool=FakePool(), own_pool=FakePool(), amo_clients=(),
              bot=main_bot, dispatcher=None, watcher=None, reconciler=None,
              stop=asyncio.Event(), autocall_manager_bot=manager_bot)

    await app.close()

    assert main_bot.session.closed is True
    assert manager_bot.session.closed is True


async def test_close_without_autocall_manager_bot_does_not_break():
    """Транспорт менеджеру не настроен (autocall_manager_bot=None по умолчанию) —
    close() не должен спотыкаться об отсутствующего бота."""
    app = App(settings=None, bot_pool=FakePool(), own_pool=FakePool(), amo_clients=(),
              bot=FakeBot(), dispatcher=None, watcher=None, reconciler=None,
              stop=asyncio.Event())

    await app.close()                                  # не падает
