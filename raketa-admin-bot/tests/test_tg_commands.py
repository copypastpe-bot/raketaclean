"""Команды владельца: /status, /pause, /resume, /help.

Проверяем не «отправилось ли сообщение», а то, что владелец видит понятный
текст без жаргона и что выключатель действительно выключает работу.
"""

from datetime import datetime

from adminbot.amo.fields import MOSCOW_TZ
from adminbot.control import MemoryControlPanel
from adminbot.sync.watcher import TickReport
from adminbot.tg.bot import OwnerCommands
from tests.test_tg_guard import FakeMessage, make_commands

NOW = datetime(2026, 8, 25, 14, 32, tzinfo=MOSCOW_TZ)


class FakeWatcher:
    def __init__(self, report=None, at=None):
        self.last_report = report
        self.last_tick_at = at


async def test_status_shows_mode_queue_and_last_pass():
    control = MemoryControlPanel(counts={"new": 3, "in_progress": 1,
                                         "waiting_owner": 2, "done": 45})
    commands = make_commands(
        control=control, dry_run=True,
        watcher=FakeWatcher(TickReport(scanned=4, by_status={"done": 1}), NOW))

    message = FakeMessage()
    await commands.status(message)
    text = message.replies[0]

    assert "репетиция" in text                       # режим понятен без расшифровки
    assert "работаю" in text.lower()
    assert "новых: 3" in text
    assert "ждут вашего ответа: 2" in text
    assert "проведено: 45" in text
    assert "25.08 в 14:32" in text                   # когда робот смотрел в базу
    assert "dry" not in text.lower()                 # без жаргона


async def test_status_says_live_mode_when_not_a_rehearsal():
    commands = make_commands(dry_run=False)

    message = FakeMessage()
    await commands.status(message)

    assert "боевой" in message.replies[0]


async def test_status_warns_when_switched_off_in_service_settings():
    """Выключатель окружения командой не поднять — владелец должен это понимать."""
    commands = make_commands(sync_enabled=False)

    message = FakeMessage()
    await commands.status(message)
    text = message.replies[0]

    assert "выключен" in text.lower()
    assert "/resume" not in text                     # не предлагаем то, что не поможет
    assert "на сервере" in text.lower()


async def test_status_of_an_empty_queue():
    commands = make_commands(control=MemoryControlPanel(counts={}))

    message = FakeMessage()
    await commands.status(message)

    assert "заказов в очереди нет" in message.replies[0].lower()


async def test_pause_and_resume_switch_the_robot():
    control = MemoryControlPanel()
    commands = make_commands(control=control)

    await commands.pause(FakeMessage(text="/pause"))
    assert await control.is_paused() is True

    message = FakeMessage(text="/status")
    await commands.status(message)
    assert "на паузе" in message.replies[0]

    await commands.resume(FakeMessage(text="/resume"))
    assert await control.is_paused() is False


async def test_pause_twice_is_not_an_error():
    control = MemoryControlPanel()
    commands = make_commands(control=control)

    first = FakeMessage()
    second = FakeMessage()
    await commands.pause(first)
    await commands.pause(second)

    assert await control.is_paused() is True
    assert "уже" in second.replies[0].lower()


async def test_resume_reminds_about_service_switch():
    """Возобновить работу командой можно, только если сервис вообще включён."""
    commands = make_commands(sync_enabled=False)

    message = FakeMessage()
    await commands.resume(message)

    assert "выключен" in message.replies[0].lower()


async def test_help_lists_commands_in_plain_language():
    commands = make_commands()

    message = FakeMessage()
    await commands.help(message)
    text = message.replies[0]

    for command in ("/status", "/pause", "/resume", "/help"):
        assert command in text
    assert "amoCRM" in text


async def test_commands_can_run_without_a_watcher():
    """Бот может подняться раньше наблюдателя — /status не должен падать."""
    commands = OwnerCommands(owner_tg_id=42, control=MemoryControlPanel(),
                             sync_enabled=True, dry_run=True, watcher=None)

    message = FakeMessage()
    await commands.status(message)

    assert "проходов ещё не было" in message.replies[0].lower()
