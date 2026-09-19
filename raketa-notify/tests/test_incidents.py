"""Инциденты и напоминания (ТЗ 2026-09-18, задача 8).

Каждый тест здесь доказывает одно требование владельца, а не «код
запускается»: расписание красного и жёлтого, кнопка «сел разбираться»,
автоматический отбой, эскалация в My_assistant, переживание перезапуска
службы и отсутствие дублей при двух одновременных проходах.

Нужен настоящий Postgres: состояние инцидента живёт в базе, и проверять
его на заглушках бессмысленно — именно там ограничение «одна открытая
запись на поломку», ради которого всё и затевалось.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Sequence

from notifyd import db, incidents
from notifyd.incidents import IncidentManager
from notifyd.watchdog import CheckResult, KEY_ADMIN_HEARTBEAT, KEY_DISPATCH

from conftest import NOW, fetch_incident, insert_incident


def failing(key: str = KEY_DISPATCH, level: str = "red") -> CheckResult:
    return CheckResult(key=key, ok=False, detail="не отвечает", level=level)


def healthy(key: str = KEY_DISPATCH, level: str = "red") -> CheckResult:
    return CheckResult(key=key, ok=True, detail="", level=level)


def manager(pool: Any, moment) -> IncidentManager:
    """Менеджер с застывшими часами: расписание проверяется переводом
    стрелок, а не ожиданием десяти настоящих минут. Своего цикла у него нет —
    результаты проверок приносит цикл сторожа, здесь их подаёт тест."""
    return IncidentManager(pool=pool, enabled=True, now=lambda: moment)


async def events(pool: Any, kind: str) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind, text, reply_markup, ref FROM notify.outbox"
            " WHERE kind = $1 ORDER BY id", kind)
    return [dict(r) for r in rows]


async def open_incidents(pool: Any) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, key, state FROM notify.incidents WHERE state <> 'closed'")
    return [dict(r) for r in rows]


async def test_red_incident_opens_and_alerts_with_button(pool):
    """Первая поломка: инцидент заводится, тревога уходит с кнопкой."""
    queued = await manager(pool, NOW).process_checks([failing()])

    assert queued == 1
    sent = await events(pool, KEY_DISPATCH)
    assert len(sent) == 1
    assert sent[0]["reply_markup"] is not None, "у красной тревоги должна быть кнопка"
    assert len(await open_incidents(pool)) == 1


async def test_red_repeats_every_ten_minutes_not_sooner(pool):
    """Повтор ровно по расписанию: минутой раньше — тишина."""
    await manager(pool, NOW).process_checks([failing()])

    soon = NOW + timedelta(seconds=incidents.RED_REMINDER_SEC - 60)
    assert await manager(pool, soon).process_checks([failing()]) == 0

    due = NOW + timedelta(seconds=incidents.RED_REMINDER_SEC)
    assert await manager(pool, due).process_checks([failing()]) == 1
    assert len(await events(pool, KEY_DISPATCH)) == 2


async def test_ack_silences_for_an_hour_and_then_returns(pool):
    """«Сел разбираться» гасит на час. Не починилось — напоминания вернулись."""
    await manager(pool, NOW).process_checks([failing()])
    incident_id = (await open_incidents(pool))[0]["id"]

    acked = await db.ack_incident(
        pool, incident_id=incident_id,
        until=NOW + timedelta(seconds=incidents.RED_ACK_SILENCE_SEC), now=NOW)
    assert acked is not None

    inside = NOW + timedelta(minutes=30)
    assert await manager(pool, inside).process_checks([failing()]) == 0, \
        "в течение часа после кнопки робот молчит"

    # Через час срабатывает не только напоминание, но и законная эскалация
    # (поломка не закрыта час), поэтому считаем именно напоминания по инциденту.
    before = len(await events(pool, KEY_DISPATCH))
    after = NOW + timedelta(seconds=incidents.RED_ACK_SILENCE_SEC + 60)
    await manager(pool, after).process_checks([failing()])
    assert len(await events(pool, KEY_DISPATCH)) == before + 1, \
        "час прошёл, поломка жива — напоминания возвращаются"


async def test_recovery_closes_incident_and_sends_all_clear(pool):
    """Починилось — робот сам говорит «отбой», инцидент закрывается."""
    await manager(pool, NOW).process_checks([failing()])
    later = NOW + timedelta(minutes=20)

    assert await manager(pool, later).process_checks([healthy()]) == 1
    assert await open_incidents(pool) == []

    texts = [e["text"] for e in await events(pool, KEY_DISPATCH)]
    assert any("Отбой" in t for t in texts), texts


async def test_yellow_reminds_daily_and_stops_at_cap(pool):
    """Жёлтый: раз в сутки и не больше трёх сообщений всего."""
    moment = NOW
    yellow = failing(key=KEY_ADMIN_HEARTBEAT, level="yellow")
    assert await manager(pool, moment).process_checks([yellow]) == 1

    for _ in range(incidents.YELLOW_CAP - 1):
        moment += timedelta(seconds=incidents.YELLOW_REMINDER_SEC)
        assert await manager(pool, moment).process_checks([yellow]) == 1

    moment += timedelta(seconds=incidents.YELLOW_REMINDER_SEC)
    assert await manager(pool, moment).process_checks([yellow]) == 0, \
        "потолок исчерпан — жёлтый замолкает"
    assert len(await events(pool, KEY_ADMIN_HEARTBEAT)) == incidents.YELLOW_CAP


async def test_escalation_after_an_hour_goes_to_assistant_once(pool):
    """Красная поломка, не закрытая за час, дублируется владельцу словами
    последствия — и только один раз."""
    insert_id = await insert_incident(
        pool, key=KEY_DISPATCH, level="red", state="open",
        opened_at=NOW - timedelta(seconds=incidents.ESCALATION_AFTER_SEC + 60),
        last_notified_at=NOW)

    queued = await manager(pool, NOW).process_checks([])
    assert queued == 1

    escalations = await events(pool, incidents.ESCALATION_KIND)
    assert len(escalations) == 1
    assert escalations[0]["reply_markup"] is None, "в эскалации кнопки быть не должно"
    assert "клиент" in escalations[0]["text"].lower(), escalations[0]["text"]

    assert await manager(pool, NOW + timedelta(minutes=10)).process_checks([]) == 0
    assert len(await events(pool, incidents.ESCALATION_KIND)) == 1
    assert (await fetch_incident(pool, insert_id))["escalated_at"] is not None


async def test_incident_survives_service_restart(pool):
    """Состояние живёт в базе: новая жизнь службы видит ту же поломку
    и не заводит вторую."""
    await manager(pool, NOW).process_checks([failing()])

    restarted = manager(pool, NOW + timedelta(minutes=1))   # другой объект = другой процесс
    assert await restarted.process_checks([failing()]) == 0
    assert len(await open_incidents(pool)) == 1


async def test_two_parallel_passes_send_one_message(pool):
    """Два одновременных прохода дают одну тревогу, а не две.

    Проверка из ТЗ. Ловится не памятью процесса, а ограничением базы на
    одну открытую запись по ключу поломки.
    """
    first, second = manager(pool, NOW), manager(pool, NOW)
    queued = await asyncio.gather(first.process_checks([failing()]),
                                  second.process_checks([failing()]))

    assert sum(queued) == 1, f"ожидалась одна тревога, вышло {queued}"
    assert len(await events(pool, KEY_DISPATCH)) == 1
    assert len(await open_incidents(pool)) == 1


async def test_disabled_incidents_do_nothing(pool):
    """Порядок выката: сторож уже включён, инциденты ещё нет. Проверки идут,
    но notify.incidents не трогается и ни одной тревоги не уходит."""
    mgr = IncidentManager(pool=pool, enabled=False, now=lambda: NOW)

    queued = await mgr.process_checks([failing()])

    assert queued == 0
    assert await open_incidents(pool) == []
    assert await events(pool, KEY_DISPATCH) == []
