"""Команды владельца: /status, /pause, /resume, /help.

Проверяем не «отправилось ли сообщение», а то, что владелец видит понятный
текст без жаргона и что выключатель действительно выключает работу.
"""

from datetime import datetime

from adminbot.amo.fields import MOSCOW_TZ
from adminbot.carpets.watcher import CarpetTickReport
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


async def test_status_shows_letters_waiting_to_be_sent():
    """Копятся неотправленные сообщения — владелец должен это видеть.

    Иначе тишина робота и тишина Telegram выглядят одинаково, а это разные
    вещи: в первом случае работы не было, во втором она есть и не доехала.
    """
    commands = make_commands(dry_run=False, mail=FakeMail(waiting=2))

    message = FakeMessage()
    await commands.status(message)

    assert "Жду отправки: 2" in message.replies[0]


async def test_status_is_silent_when_nothing_is_stuck():
    """Долгов нет — лишней строки в ответе тоже нет."""
    commands = make_commands(dry_run=False, mail=FakeMail(waiting=0))

    message = FakeMessage()
    await commands.status(message)

    assert "Жду отправки" not in message.replies[0]


async def test_broken_mail_does_not_break_the_status():
    """База недоступна — /status всё равно отвечает: он и нужен в такие минуты."""
    commands = make_commands(dry_run=False, mail=FakeMail(broken=True))

    message = FakeMessage()
    await commands.status(message)

    assert "боевой" in message.replies[0]


class FakeMail:
    def __init__(self, waiting: int = 0, broken: bool = False):
        self._waiting = waiting
        self._broken = broken

    async def waiting(self) -> int:
        if self._broken:
            raise RuntimeError("база недоступна")
        return self._waiting


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


async def test_status_shows_autocall_disabled_by_default():
    commands = make_commands()

    message = FakeMessage()
    await commands.status(message)

    assert "автозвонок: выключен" in message.replies[0].lower()


async def test_status_shows_autocall_rehearsal():
    commands = make_commands(autocall_enabled=True, autocall_dry_run=True)

    message = FakeMessage()
    await commands.status(message)
    text = message.replies[0]

    assert "автозвонок: репетиция" in text.lower()


async def test_status_shows_autocall_live():
    commands = make_commands(autocall_enabled=True, autocall_dry_run=False)

    message = FakeMessage()
    await commands.status(message)
    text = message.replies[0]

    assert "Автозвонок: БОЕВОЙ" in text


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


class FakeCarpetWatcher:
    """Как настоящий CarpetWatcher: помнит режим и отдаёт готовый отчёт."""

    def __init__(self, report, dry_run=True):
        self.dry_run = dry_run
        self._report = report

    async def tick(self):
        return self._report


async def test_carpets_marks_rehearsal_in_the_report():
    """Ковры в репетиции — ручная команда должна отвечать так же, как автоотправка.

    Иначе владелец вызывает /carpets и получает отчёт, неотличимый от боевого,
    хотя ничего в CRM на самом деле не проведено.
    """
    report = CarpetTickReport(letters=1, processed=1, by_status={"done": 1})
    commands = make_commands(carpet_watcher=FakeCarpetWatcher(report, dry_run=True))

    message = FakeMessage()
    await commands.carpets(message)
    text = message.replies[-1]

    assert "РЕПЕТИЦИЯ" in text


async def test_carpets_stays_silent_about_rehearsal_when_live():
    report = CarpetTickReport(letters=1, processed=1, by_status={"done": 1})
    commands = make_commands(carpet_watcher=FakeCarpetWatcher(report, dry_run=False))

    message = FakeMessage()
    await commands.carpets(message)
    text = message.replies[-1]

    assert "РЕПЕТИЦИЯ" not in text


async def test_commands_can_run_without_a_watcher():
    """Бот может подняться раньше наблюдателя — /status не должен падать."""
    commands = OwnerCommands(owner_tg_id=42, control=MemoryControlPanel(),
                             sync_enabled=True, dry_run=True, watcher=None)

    message = FakeMessage()
    await commands.status(message)

    assert "проходов ещё не было" in message.replies[0].lower()
