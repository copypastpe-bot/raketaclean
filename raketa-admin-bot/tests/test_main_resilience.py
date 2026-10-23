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
        pbx_base_url="https://pbx.example", pbx_api_key="key",
        pbx_manager_dials=("89001112233", "89004445566"))

    watcher, _manager_bot = _build_autocall(settings, None, None, object(), object())

    assert watcher is not None
    assert isinstance(watcher.engine.pbx, OnlinePbx)
    assert watcher.engine.pbx.base_url == "https://pbx.example"
    assert watcher.engine.pbx.api_key == "key"
    # Оба телефона менеджера доезжают до движка в заданном порядке.
    assert watcher.engine.manager_dials == ("89001112233", "89004445566")


_WORKER = dict(worker_tg_token="123456:ABCDEF-fake-token-value", manager_tg_chat_id=999)
_PBX = dict(pbx_base_url="https://pbx.example", pbx_api_key="key",
            pbx_manager_dials=("89001112233",))


def test_autocall_button_is_off_by_default():
    """Выключатель AUTOCALL_MANAGER_BUTTON выключен — движок без лички и нажатий."""
    from adminbot.main import _build_autocall

    settings = _autocall_settings(autocall_enabled=True, autocall_dry_run=False,
                                  **_PBX, **_WORKER)
    watcher, _bot = _build_autocall(settings, None, None, object(), object(),
                                    bot_pool=object())

    assert watcher.engine.manager_button is False
    assert watcher.engine.manager_chat is None
    assert watcher.engine.manager_callbacks is None


def test_autocall_button_live_reads_presses_through_bot_pool():
    """Бой: нажатия — из таблицы рабочего бота через bot_pool (ТЗ 2026-10-23)."""
    from adminbot.autocall.store import PgManagerCallbackSource
    from adminbot.main import _build_autocall

    bot_pool = object()
    settings = _autocall_settings(autocall_enabled=True, autocall_dry_run=False,
                                  autocall_manager_button=True, **_PBX, **_WORKER)
    watcher, _bot = _build_autocall(settings, None, None, object(), object(),
                                    bot_pool=bot_pool)

    assert watcher.engine.manager_button is True
    assert watcher.engine.manager_chat is not None
    assert isinstance(watcher.engine.manager_callbacks, PgManagerCallbackSource)
    assert watcher.engine.manager_callbacks.pool is bot_pool


def test_autocall_button_rehearsal_keeps_presses_in_memory():
    from adminbot.autocall.store import MemoryManagerCallbacks
    from adminbot.main import _build_autocall

    settings = _autocall_settings(autocall_enabled=True, autocall_dry_run=True,
                                  autocall_manager_button=True, **_WORKER)
    watcher, _bot = _build_autocall(settings, None, None, None, object(),
                                    bot_pool=object())

    assert isinstance(watcher.engine.manager_callbacks, MemoryManagerCallbacks)


def test_autocall_button_without_manager_transport_stays_off(caplog):
    """Кнопку некуда поставить (нет WORKER_TG_TOKEN/MANAGER_TG_CHAT_ID) — робот
    работает по-прежнему, а в журнале сказано, почему кнопки нет."""
    from adminbot.main import _build_autocall

    settings = _autocall_settings(autocall_enabled=True, autocall_dry_run=False,
                                  autocall_manager_button=True, **_PBX)
    with caplog.at_level("WARNING"):
        watcher, manager_bot = _build_autocall(settings, None, None, object(), object(),
                                               bot_pool=object())

    assert manager_bot is None
    assert watcher.engine.manager_button is False
    assert any("Я уже перезвонил" in record.getMessage() for record in caplog.records)


def test_autocall_pbx_recheck_is_off_by_default():
    """Выключатель AUTOCALL_PBX_RECHECK выключен — историю АТС перед повтором не смотрим."""
    from adminbot.main import _build_autocall

    settings = _autocall_settings(autocall_enabled=True, autocall_dry_run=False, **_PBX)
    watcher, _bot = _build_autocall(settings, None, None, object(), object())

    assert watcher.engine.pbx_recheck is False


def test_autocall_pbx_recheck_switch_reaches_engine():
    """Проверка истории АТС не зависит от кнопки: включается своим выключателем."""
    from adminbot.main import _build_autocall

    settings = _autocall_settings(autocall_enabled=True, autocall_dry_run=False,
                                  autocall_pbx_recheck=True, **_PBX)
    watcher, _bot = _build_autocall(settings, None, None, object(), object())

    assert watcher.engine.pbx_recheck is True
    assert watcher.engine.manager_button is False


class FakeManagerBot:
    """Send-only бот рабочего бота: что отправлено и что поправлено."""

    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, chat_id, text, **kwargs):
        from types import SimpleNamespace
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=4242)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)


async def test_manager_chat_sends_card_with_button_and_edits_to_final_without_it():
    from datetime import datetime

    from adminbot.amo.fields import MOSCOW_TZ
    from adminbot.autocall.engine import ManagerCard
    from adminbot.autocall.store import MemoryAutocallStore
    from adminbot.main import _make_autocall_manager_chat

    store = MemoryAutocallStore()
    await store.create(31750907, phone10="9001234567")
    settings = _autocall_settings(**_WORKER)
    bot = FakeManagerBot()
    chat = _make_autocall_manager_chat(settings, bot, store)
    moment = datetime(2026, 10, 23, 10, 12, tzinfo=MOSCOW_TZ)

    message_id = await chat.send(31750907, ManagerCard(
        kind="retry_manager", button=True, retry_at=moment, now=moment))

    assert message_id == 4242
    chat_id, text, kwargs = bot.sent[0]
    assert chat_id == 999
    assert text == ("Робот не дозвонился до вас по заявке. Повтор в 10:12.\n"
                    "Телефон клиента: +79001234567\n"
                    "https://x/leads/detail/31750907")
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert [(b.text, b.callback_data) for b in buttons] == [
        ("Я уже перезвонил", "autocall_called:31750907")]

    await chat.edit(4242, 31750907, ManagerCard(kind="manager_called", button=False))

    edit = bot.edited[0]
    assert (edit["chat_id"], edit["message_id"]) == (999, 4242)
    assert edit["text"].startswith("Вы перезвонили клиенту сами — робот остановился.")
    assert edit["reply_markup"].inline_keyboard == []      # кнопка убрана


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


class FakeClosable:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def test_close_closes_google_and_pbx_sessions():
    """Ключ Google, календари и телефония автозвонка держат свои сессии aiohttp.
    Без закрытия — три «Unclosed client session» в журнале на каждом перезапуске
    (п.13, 30.09)."""
    from types import SimpleNamespace
    token, calendar, pbx = FakeClosable(), FakeClosable(), FakeClosable()
    app = App(settings=None, bot_pool=FakePool(), own_pool=FakePool(), amo_clients=(),
              bot=FakeBot(), dispatcher=None, watcher=None, reconciler=None,
              stop=asyncio.Event(), calendar_token=token,
              calendar_watcher=SimpleNamespace(calendars=(calendar,)),
              autocall_watcher=SimpleNamespace(engine=SimpleNamespace(pbx=pbx)))

    await app.close()

    assert token.closed and calendar.closed and pbx.closed


# --- разбор удалений (задача 5/7, ТЗ 2026-09-17): свой выключатель и письмо владельцу ---

def _minimal_settings(**overrides):
    from adminbot.config import Settings

    return Settings(
        tg_token="t", owner_tg_id=1, bot_db_dsn="postgresql://x", own_db_dsn="postgresql://x",
        amo_base_url="https://raketacleancrm.amocrm.ru", amo_token="t", **overrides)


def test_deletions_disabled_returns_none():
    from adminbot.main import _build_deletions

    settings = _minimal_settings(order_deletions_enabled=False)

    assert _build_deletions(settings, None, None, object(), object()) is None


def test_deletions_rehearsal_uses_the_rehearsal_amo_client():
    from adminbot.main import _build_deletions

    settings = _minimal_settings(order_deletions_enabled=True, order_deletions_dry_run=True)
    live, rehearsal = object(), object()

    handler = _build_deletions(settings, None, None, live, rehearsal)

    assert handler is not None
    assert handler.amo is rehearsal
    # dry_run обязателен: без него репетиция ставила бы отметку "разобрано"
    # по-настоящему и забирала бы записи у последующего боя (см. deletions.py).
    assert handler.dry_run is True


def test_deletions_live_uses_the_live_amo_client():
    from adminbot.main import _build_deletions

    settings = _minimal_settings(order_deletions_enabled=True, order_deletions_dry_run=False)
    live, rehearsal = object(), object()

    handler = _build_deletions(settings, None, None, live, rehearsal)

    assert handler.amo is live
    assert handler.dry_run is False


# --- доводка оплаты по счёту (задача 11, ТЗ 2026-09-22): свой выключатель и dry_run ---

def _empty_specialists():
    from adminbot.sync.specialists import SpecialistIndex

    return SpecialistIndex.from_enums([])


def test_wire_payment_disabled_returns_none():
    from adminbot.main import _build_wire_payment

    settings = _minimal_settings(wire_payment_enabled=False)

    assert _build_wire_payment(settings, None, None, None, _empty_specialists(), {},
                               object(), object()) is None


def test_wire_payment_rehearsal_uses_the_rehearsal_amo_client():
    from adminbot.main import _build_wire_payment

    settings = _minimal_settings(wire_payment_enabled=True, wire_payment_dry_run=True)
    live, rehearsal = object(), object()

    sync = _build_wire_payment(settings, None, None, None, _empty_specialists(), {},
                               live, rehearsal)

    assert sync is not None
    assert sync.engine.amo is rehearsal
    # dry_run обязателен: без него репетиция ставила бы payment_synced_at
    # по-настоящему и забирала бы связки у последующего боя.
    assert sync.engine.dry_run is True


def test_wire_payment_live_uses_the_live_amo_client():
    from adminbot.main import _build_wire_payment

    settings = _minimal_settings(wire_payment_enabled=True, wire_payment_dry_run=False)
    live, rehearsal = object(), object()

    sync = _build_wire_payment(settings, None, None, None, _empty_specialists(), {},
                               live, rehearsal)

    assert sync.engine.amo is live
    assert sync.engine.dry_run is False


def _make_outcome(outcome: str, **fields):
    from datetime import datetime, timezone

    from adminbot.models import AmoLink, DeletionRecord
    from adminbot.sync.deletions import DeletionOutcome

    record = DeletionRecord(kind=fields.pop("kind", "order"), order_id=596,
                            deleted_at=datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc))
    link = AmoLink(order_id=596, phone10="9601861067", status="done")
    return DeletionOutcome(record=record, link=link, outcome=outcome, **fields)


def test_deletion_text_mentions_phone_and_deal_link():
    from adminbot.main import _deletion_text

    text = _deletion_text(_make_outcome("reopened", lead_id=41400001),
                          "https://raketacleancrm.amocrm.ru", dry_run=False)

    assert "+79601861067" in text                     # полный телефон (правило for_owner)
    assert "Заказ подтвержден" in text
    assert "https://raketacleancrm.amocrm.ru/leads/detail/41400001" in text
    assert "🎭 РЕПЕТИЦИЯ" not in text


def test_deletion_text_marks_rehearsal():
    from adminbot.main import _deletion_text

    text = _deletion_text(_make_outcome("reopened", lead_id=41400001),
                          "https://x", dry_run=True)

    assert text.startswith("🎭 РЕПЕТИЦИЯ")


def test_deletion_text_held_by_other_names_the_other_order():
    from adminbot.main import _deletion_text

    outcome = _make_outcome("held_by_other", lead_id=41400001,
                            other_kind="cleaning", other_order_id=777)
    text = _deletion_text(outcome, "https://x", dry_run=False)

    assert "№777" in text and "уборкой" in text


def test_deletion_text_lead_gone_has_no_dead_crm_link():
    from adminbot.main import _deletion_text

    text = _deletion_text(_make_outcome("lead_gone", lead_id=41400001), "https://x", dry_run=False)

    assert "leads/detail" not in text
    assert "связку убрал" in text


# --- отклики на промо (ТЗ 2026-09-23, задача 6): свой выключатель и dry_run ---

def test_promo_callback_disabled_returns_none():
    from adminbot.main import _build_promo_callback

    settings = _minimal_settings(promo_callback_enabled=False)

    assert _build_promo_callback(settings, None, None, None, object(), object()) is None


def test_promo_callback_rehearsal_uses_the_rehearsal_amo_client():
    from adminbot.main import _build_promo_callback

    settings = _minimal_settings(promo_callback_enabled=True, promo_callback_dry_run=True)
    live, rehearsal = object(), object()

    sync = _build_promo_callback(settings, None, None, None, live, rehearsal)

    assert sync.amo is rehearsal
    assert sync.dry_run is True and sync.mode == "rehearsal"


def test_promo_callback_live_uses_the_live_amo_client():
    from adminbot.main import _build_promo_callback

    settings = _minimal_settings(promo_callback_enabled=True, promo_callback_dry_run=False)
    live, rehearsal = object(), object()

    sync = _build_promo_callback(settings, None, None, None, live, rehearsal)

    assert sync.amo is live
    assert sync.dry_run is False and sync.mode == "live"
    assert sync.on_done is not None                                 # бой пишет «завёл сделку»


async def test_promo_callback_failure_letter_goes_to_owner_mail():
    from adminbot.main import MAIL_PROMO_CALLBACK_FAILED, _make_promo_callback_failure_sender
    from adminbot.promo_callback.sync import PromoCallback

    class Mail:
        def __init__(self):
            self.sent = []

        async def send(self, text, *, kind, ref=None, reply_markup=None):
            self.sent.append((text, kind, ref))

    mail = Mail()
    send = _make_promo_callback_failure_sender(mail, "https://x", dry_run=False)

    await send(PromoCallback(id=5, source="client", phone="+79601861067", name="Ирина"),
               None, "amoCRM 500")

    [(text, kind, ref)] = mail.sent
    assert kind == MAIL_PROMO_CALLBACK_FAILED and ref == 5
    assert "позвоните руками" in text and "+79601861067" in text


async def test_promo_callback_done_letter_goes_to_owner_mail():
    from adminbot.main import MAIL_PROMO_CALLBACK_DONE, _make_promo_callback_done_sender
    from adminbot.promo_callback.sync import PromoCallback

    class Mail:
        def __init__(self):
            self.sent = []

        async def send(self, text, *, kind, ref=None, reply_markup=None):
            self.sent.append((text, kind, ref))

    mail = Mail()
    send = _make_promo_callback_done_sender(mail, "https://x")

    await send(PromoCallback(id=5, source="client", phone="+79601861067", name="Ирина"), 777)

    [(text, kind, ref)] = mail.sent
    assert kind == MAIL_PROMO_CALLBACK_DONE and ref == 5
    assert text.startswith("Отклик на промо: завёл сделку")
    assert "+79601861067" in text and text.endswith("https://x/leads/detail/777")


# --- «Повторный заказ» по оценке клиента (ТЗ 2026-09-28, задача 4): свой выключатель и dry_run ---

def _rated_order(**overrides):
    from datetime import datetime, timezone

    from adminbot.feedback.models import RatedOrder

    values = dict(order_id=42, lead_id=777, score=5, comment=None,
                  replied_at=datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))
    values.update(overrides)
    return RatedOrder(**values)


class _FakeMail:
    def __init__(self):
        self.sent = []

    async def send(self, text, *, kind, ref=None, reply_markup=None):
        self.sent.append((text, kind, ref))


def test_feedback_disabled_returns_none():
    from adminbot.main import _build_feedback

    settings = _minimal_settings(feedback_tasks_enabled=False)

    assert _build_feedback(settings, None, None, None, object(), object()) is None


def test_feedback_rehearsal_uses_the_rehearsal_amo_client():
    from adminbot.main import _build_feedback

    settings = _minimal_settings(feedback_tasks_enabled=True, feedback_tasks_dry_run=True)
    live, rehearsal = object(), object()

    sync = _build_feedback(settings, None, None, None, live, rehearsal)

    assert sync.amo is rehearsal
    assert sync.dry_run is True and sync.mode == "rehearsal"


def test_feedback_live_uses_the_live_amo_client():
    from adminbot.main import _build_feedback

    settings = _minimal_settings(feedback_tasks_enabled=True, feedback_tasks_dry_run=False)
    live, rehearsal = object(), object()

    sync = _build_feedback(settings, None, None, None, live, rehearsal)

    assert sync.amo is live
    assert sync.dry_run is False and sync.mode == "live"


async def test_feedback_rehearsal_letter_goes_to_owner_mail():
    from adminbot.main import MAIL_FEEDBACK_REHEARSAL, _make_feedback_rehearsal_sender

    mail = _FakeMail()
    send = _make_feedback_rehearsal_sender(mail, "https://x")

    await send(_rated_order(), ["закрыл бы «Повторный заказ»"])

    [(text, kind, ref)] = mail.sent
    assert kind == MAIL_FEEDBACK_REHEARSAL and ref == 42
    assert "№42" in text


async def test_feedback_failure_letter_marked_in_rehearsal():
    """Решение координатора: сбой в репетиции тоже помечен как репетиционный."""
    from adminbot.main import MAIL_FEEDBACK_FAILED, _make_feedback_failure_sender

    mail = _FakeMail()
    send = _make_feedback_failure_sender(mail, "https://x", dry_run=True)

    await send(_rated_order(score=2, comment="плохо"), "amoCRM 500")

    [(text, kind, ref)] = mail.sent
    assert kind == MAIL_FEEDBACK_FAILED and ref == 42
    assert text.startswith("🎭 РЕПЕТИЦИЯ")


async def test_feedback_failure_letter_not_marked_when_live():
    from adminbot.main import _make_feedback_failure_sender

    mail = _FakeMail()
    send = _make_feedback_failure_sender(mail, "https://x", dry_run=False)

    await send(_rated_order(score=2, comment="плохо"), "amoCRM 500")

    [(text, _, _)] = mail.sent
    assert not text.startswith("🎭 РЕПЕТИЦИЯ")

