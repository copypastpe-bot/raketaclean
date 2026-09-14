"""Два выключателя робота и их совместное поведение.

Выключатель сервиса (настройка на сервере) — главный: пока он опущен, никакие
команды из Telegram работу не возобновят. Пауза владельца — оперативная:
её ставят и снимают командой, и она переживает перезапуск сервиса.
"""

from adminbot.control import MemoryControlPanel, sync_allowed


async def test_pause_is_off_by_default():
    assert await MemoryControlPanel().is_paused() is False


async def test_pause_switches_both_ways():
    control = MemoryControlPanel()

    await control.set_paused(True)
    assert await control.is_paused() is True

    await control.set_paused(False)
    assert await control.is_paused() is False


async def test_robot_works_only_when_enabled_and_not_paused():
    control = MemoryControlPanel()
    allowed = sync_allowed(sync_enabled=True, control=control)

    assert await allowed() is True

    await control.set_paused(True)
    assert await allowed() is False


async def test_service_switch_wins_over_resume():
    """Снятая пауза не поднимает робота, если он выключен настройками сервиса."""
    control = MemoryControlPanel(paused=False)
    allowed = sync_allowed(sync_enabled=False, control=control)

    assert await allowed() is False
