"""Переходник журнала (задача 5 ТЗ 2026-09-18): разбор строк, схлопывание
повторов в окне 10 минут, потолок на цикл, маскировка ПД, доставка через
уже готового почтальона.

Источник журнала в тестах — `FakeJournalSource` (tests/conftest.py):
настоящего journald на macOS нет (ограничение среды), поэтому тест кладёт
строки сам через `.push(...)`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from notifyd.journal_adapter import (
    DEDUP_WINDOW_SEC,
    JOURNAL_KIND,
    JournalAdapter,
    parse_level_and_module,
)
from notifyd.postman import Postman, Target

from conftest import NOW, FakeJournalSource, FakeSender, fetch_outbox


class _Clock:
    """Управляемое «сейчас» — сдвигаем во времени внутри одного теста без
    настоящего asyncio.sleep (тот же приём, что `now=lambda: NOW` у
    почтальона, только с возможностью двигать время вперёд)."""

    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


async def _noop_sleep(_seconds: float) -> None:
    return None


def _adapter(pool, sources, *, enabled=True, now=None, **kwargs) -> JournalAdapter:
    return JournalAdapter(pool=pool, sources=sources, enabled=enabled,
                          now=now or (lambda: NOW), sleep=_noop_sleep, **kwargs)


async def _fetch_all_journal_rows(pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM notify.outbox WHERE kind = $1 ORDER BY id", JOURNAL_KIND)
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Разбор строки: уровень, модуль, текст — без базы
# --------------------------------------------------------------------------

def test_parses_bare_worker_bot_format():
    """logging.basicConfig(level=logging.INFO) без format= — bot.py:387."""
    parsed = parse_level_and_module("WARNING:bot:Failed to send message: timeout")
    assert parsed is not None
    assert parsed.level == "WARNING"
    assert parsed.module == "bot"
    assert parsed.text == "Failed to send message: timeout"


def test_parses_adminbot_asctime_format():
    """format="%(asctime)s %(levelname)s %(name)s: %(message)s" — adminbot/main.py:1065."""
    parsed = parse_level_and_module(
        "2026-09-18 12:34:56,789 ERROR adminbot.tg.outbox: не ушло сообщение")
    assert parsed is not None
    assert parsed.level == "ERROR"
    assert parsed.module == "adminbot.tg.outbox"
    assert parsed.text == "не ушло сообщение"


def test_unrecognized_line_returns_none():
    """Продолжение трассировки без префикса уровня — не разбираем вслепую."""
    assert parse_level_and_module('  File "bot.py", line 10, in <module>') is None


# --------------------------------------------------------------------------
# Выключатель, уровень, маскировка ПД, тег по источнику
# --------------------------------------------------------------------------

async def test_disabled_adapter_never_touches_outbox(pool):
    """Тот же приём, что test_postman.py::test_disabled_service_never_touches_outbox:
    подложенный sleep сам взводит stop, поэтому run_forever можно ждать
    напрямую — без фонового task и настоящего ожидания."""
    source = FakeJournalSource("telegram-bot.service")
    source.push("ERROR:bot:что-то упало")

    calls = {"n": 0}
    stop = asyncio.Event()

    async def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        stop.set()

    adapter = JournalAdapter(pool=pool, sources={"telegram-bot.service": source},
                             enabled=False, now=lambda: NOW, sleep=fake_sleep)
    await adapter.run_forever(stop)

    assert calls["n"] == 1
    assert await _fetch_all_journal_rows(pool) == []


async def test_info_level_is_not_forwarded(pool):
    source = FakeJournalSource("telegram-bot.service")
    source.push("INFO:bot:просто по делу, не предупреждение")
    adapter = _adapter(pool, {"telegram-bot.service": source})

    queued = await adapter.poll_once()

    assert queued == 0
    assert await _fetch_all_journal_rows(pool) == []


async def test_warning_level_is_forwarded_with_source_tag(pool):
    source = FakeJournalSource("telegram-bot.service")
    source.push("WARNING:crm.wahelp_dispatcher:Send via whatsapp failed for client 42: timeout")
    adapter = _adapter(pool, {"telegram-bot.service": source})

    queued = await adapter.poll_once()

    assert queued == 1
    rows = await _fetch_all_journal_rows(pool)
    assert len(rows) == 1
    text = rows[0]["text"]
    assert rows[0]["source"] == "notify"
    assert "#telegram-bot" in text
    assert "#crm_wahelp_dispatcher" in text          # точки модуля -> подчёркивание
    assert "[WARNING]" in text


async def test_exception_level_error_is_forwarded(pool):
    """logger.exception(...) логирует на уровне ERROR."""
    source = FakeJournalSource("raketa-admin-bot.service")
    source.push("2026-09-18 12:00:00,000 ERROR adminbot.sync.engine: сбой синхронизации")
    adapter = _adapter(pool, {"raketa-admin-bot.service": source})

    queued = await adapter.poll_once()

    assert queued == 1
    rows = await _fetch_all_journal_rows(pool)
    assert "#raketa-admin-bot" in rows[0]["text"]
    assert "#adminbot_sync_engine" in rows[0]["text"]


async def test_client_phone_in_log_line_is_masked_before_queueing(pool):
    """Подтверждённая находка разведки: notifications/outbox.py:626 кладёт в
    warning весь payload вебхука — там может быть номер клиента."""
    source = FakeJournalSource("telegram-bot.service")
    source.push(
        "WARNING:notifications.outbox:Webhook payload missing message id. "
        "event=status payload={'to': '79991234567'}"
    )
    adapter = _adapter(pool, {"telegram-bot.service": source})

    await adapter.poll_once()

    rows = await _fetch_all_journal_rows(pool)
    assert "79991234567" not in rows[0]["text"]
    assert "…4567" in rows[0]["text"]


# --------------------------------------------------------------------------
# Схлопывание повторов (окно 10 минут) — обязательное свойство ТЗ
# --------------------------------------------------------------------------

async def test_hundred_identical_errors_become_one_line_with_counter(pool):
    """Проверка из самого ТЗ: искусственный поток из 100 одинаковых ошибок
    -> в чат (в ящик) уходит одна строка со счётчиком."""
    source = FakeJournalSource("telegram-bot.service")
    for _ in range(100):
        source.push("ERROR:bot:amoCRM API auth failed; polling stopped: 401")
    adapter = _adapter(pool, {"telegram-bot.service": source})

    queued = await adapter.poll_once()

    assert queued == 1
    rows = await _fetch_all_journal_rows(pool)
    assert len(rows) == 1
    assert "(×100)" in rows[0]["text"]


async def test_repeat_within_window_is_suppressed_not_new_row(pool):
    clock = _Clock(NOW)
    source = FakeJournalSource("telegram-bot.service")
    adapter = _adapter(pool, {"telegram-bot.service": source}, now=clock)

    source.push("ERROR:bot:сбой опроса amoCRM")
    assert await adapter.poll_once() == 1               # первое — сразу одна строка

    clock.advance(60)                                     # минута спустя, то же окно
    source.push("ERROR:bot:сбой опроса amoCRM")
    assert await adapter.poll_once() == 0                # повтор в окне — молча накоплен

    rows = await _fetch_all_journal_rows(pool)
    assert len(rows) == 1                                 # ни одной новой строки


async def test_suppressed_repeats_flush_as_summary_after_window_closes(pool):
    clock = _Clock(NOW)
    source = FakeJournalSource("telegram-bot.service")
    adapter = _adapter(pool, {"telegram-bot.service": source}, now=clock)

    source.push("ERROR:bot:сбой опроса amoCRM")
    await adapter.poll_once()                             # окно открыто

    clock.advance(120)
    source.push("ERROR:bot:сбой опроса amoCRM")
    await adapter.poll_once()                             # подавлен, накоплен (accumulated=1)

    clock.advance(DEDUP_WINDOW_SEC)                        # окно истекло, новых записей нет
    queued = await adapter.poll_once()                    # проход-«уборщик» находит хвост

    assert queued == 1
    rows = await _fetch_all_journal_rows(pool)
    assert len(rows) == 2
    assert "повторилось ×1" in rows[1]["text"]


async def test_new_occurrence_after_window_starts_fresh_line(pool):
    clock = _Clock(NOW)
    source = FakeJournalSource("telegram-bot.service")
    adapter = _adapter(pool, {"telegram-bot.service": source}, now=clock)

    source.push("ERROR:bot:сбой опроса amoCRM")
    await adapter.poll_once()

    clock.advance(DEDUP_WINDOW_SEC + 1)
    source.push("ERROR:bot:сбой опроса amoCRM")
    queued = await adapter.poll_once()                     # окно истекло -> сразу новая строка

    assert queued == 1
    rows = await _fetch_all_journal_rows(pool)
    assert len(rows) == 2
    assert "(×" not in rows[1]["text"]                      # без повторов — без счётчика


# --------------------------------------------------------------------------
# Схлопывание не зависит от чисел в тексте (ревью п.6, задача 3 ТЗ 2026-09-19)
# --------------------------------------------------------------------------

async def test_repeats_with_different_numbers_collapse_into_one_line(pool):
    """Дефект ревью (п.6): раньше ключ схлопывания включал точный текст, а
    номер заказа/сделки стоит прямо в тексте записи — массовый сбой давал
    разный текст на каждый номер вместо одной строки со счётчиком. Теперь
    сравнение идёт по «форме» текста (числа заменены на заглушку)."""
    source = FakeJournalSource("telegram-bot.service")
    for order_id in range(1, 21):
        source.push(f"ERROR:bot:не удалось обработать заказ {order_id}")
    adapter = _adapter(pool, {"telegram-bot.service": source})

    queued = await adapter.poll_once()

    assert queued == 1
    rows = await _fetch_all_journal_rows(pool)
    assert len(rows) == 1
    assert "(×20)" in rows[0]["text"]


async def test_different_errors_with_numbers_do_not_collapse_together(pool):
    """Форма нужна только для чисел — разные по смыслу ошибки (разный текст
    вокруг числа) остаются разными строками, а не схлопываются в одну."""
    source = FakeJournalSource("telegram-bot.service")
    source.push("ERROR:bot:не удалось обработать заказ 5")
    source.push("ERROR:bot:не удалось отменить заказ 5")
    adapter = _adapter(pool, {"telegram-bot.service": source})

    queued = await adapter.poll_once()

    assert queued == 2
    rows = await _fetch_all_journal_rows(pool)
    assert len(rows) == 2


async def test_collapsed_line_shows_readable_text_of_first_occurrence(pool):
    """Форма — только для сравнения. В чат уходит человеческий текст первого
    вхождения с настоящим числом, заглушка наружу не просачивается."""
    source = FakeJournalSource("telegram-bot.service")
    source.push("ERROR:bot:не удалось обработать заказ 581")
    source.push("ERROR:bot:не удалось обработать заказ 582")
    adapter = _adapter(pool, {"telegram-bot.service": source})

    queued = await adapter.poll_once()

    assert queued == 1
    rows = await _fetch_all_journal_rows(pool)
    text = rows[0]["text"]
    assert "не удалось обработать заказ 581" in text      # первое вхождение, число видно
    assert "(×2)" in text
    assert "\x00" not in text                               # заглушка не просочилась в чат


async def test_masked_phone_tails_collapse_despite_different_digits(pool):
    """Ограничение задачи: маскированный телефон (…1234) — уже не ПД,
    схлопывание строк, различающихся только его хвостом, допустимо и
    желательно."""
    source = FakeJournalSource("telegram-bot.service")
    source.push(
        "WARNING:notifications.outbox:Webhook payload missing message id. "
        "event=status payload={'to': '79991234567'}"
    )
    source.push(
        "WARNING:notifications.outbox:Webhook payload missing message id. "
        "event=status payload={'to': '79997654321'}"
    )
    adapter = _adapter(pool, {"telegram-bot.service": source})

    queued = await adapter.poll_once()

    assert queued == 1
    rows = await _fetch_all_journal_rows(pool)
    assert len(rows) == 1
    assert "(×2)" in rows[0]["text"]


# --------------------------------------------------------------------------
# Потолок на цикл (при интервале 60с — потолок «на минуту» из ТЗ)
# --------------------------------------------------------------------------

async def test_cap_per_cycle_leaves_one_overflow_line(pool):
    """Модуль, а не число в тексте, делает записи различными: с задачи 3
    (ревью п.6) число в тексте на группировку не влияет, поэтому здесь нужен
    другой источник различия, чтобы получить 25 РАЗНЫХ ключей."""
    source = FakeJournalSource("telegram-bot.service")
    for i in range(25):
        source.push(f"ERROR:bot{i}:сделка не разобрана: timeout")
    adapter = _adapter(pool, {"telegram-bot.service": source}, cap_per_cycle=20)

    queued = await adapter.poll_once()

    assert queued == 21                                     # 20 обычных + 1 сводная
    rows = await _fetch_all_journal_rows(pool)
    assert rows[-1]["text"] == "ещё 5 записей, смотри журнал"


# --------------------------------------------------------------------------
# Сквозная проверка: адаптер кладёт в ящик, доставляет уже готовый почтальон
# --------------------------------------------------------------------------

async def test_queued_event_is_delivered_by_postman_to_tech_journal(pool):
    source = FakeJournalSource("telegram-bot.service")
    source.push("WARNING:bot:Failed to notify admin 1 about amoCRM API alert: timeout")
    adapter = _adapter(pool, {"telegram-bot.service": source})

    assert await adapter.poll_once() == 1

    sender = FakeSender()
    postman = Postman(pool=pool, targets={"tech_journal": Target(sender, chat_id=999)},
                      enabled=True, dry_run=False, now=lambda: NOW)
    delivered = await postman.deliver_due()

    assert delivered == 1
    assert len(sender.sent) == 1
    chat_id, text, _markup = sender.sent[0]
    assert chat_id == 999
    assert "#telegram-bot" in text

    routes = await _list_routes(pool)
    seeded = next(r for r in routes if r["kind"] == JOURNAL_KIND)
    assert seeded["address"] == "tech_journal"
    assert seeded["level"] == "grey"                        # серый по умолчанию — тихий адрес


async def _list_routes(pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT kind, address, level FROM notify.routes")
    return [dict(row) for row in rows]
