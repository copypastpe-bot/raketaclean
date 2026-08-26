"""Устойчивость сервиса к обрывам связи с Telegram.

Telegram из России доступен нестабильно. Работа с amoCRM от него не зависит,
поэтому обрыв связи не должен ронять весь процесс: сделки продолжают
оформляться, а опрос возобновляется сам.
"""

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
