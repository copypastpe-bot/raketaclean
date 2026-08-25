"""Наблюдатель: раз в минуту заглядывает в базу и доводит заказы до конца.

Ни Postgres, ни amoCRM, ни Telegram: источник заказов, движок, часы и сон —
двойники. Проверяем именно поведение цикла: пауза, живучесть при сбое одного
заказа, единственность вопроса владельцу.
"""

import asyncio
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import AmoLink, Order
from adminbot.sync.watcher import Watcher

ORDER_MOMENT = datetime(2026, 8, 24, 17, 53, tzinfo=MOSCOW_TZ)


def make_order(order_id=596):
    return Order(order_id=order_id, phone10="9601861067", created_at=ORDER_MOMENT,
                 amount_total=Decimal("5950"), client_name="Ирина")


def make_link(order_id=596, status="done", **extra):
    return AmoLink(order_id=order_id, phone10="9601861067", status=status, **extra)


class FakeSource:
    """Очередь заказов «на сейчас»."""

    def __init__(self, orders):
        self.orders = list(orders)
        self.calls = 0

    async def pending(self):
        self.calls += 1
        return list(self.orders)


class FakeEngine:
    """Движок-двойник: возвращает заранее назначенный итог по каждому заказу."""

    def __init__(self, results=None, store=None):
        self.results = results or {}
        self.store = store or FakeLinkStore()
        self.processed = []
        self.raise_on = set()

    async def process_order(self, order):
        self.processed.append(order.order_id)
        if order.order_id in self.raise_on:
            raise RuntimeError("база отвалилась посреди заказа")
        link = self.results.get(order.order_id, make_link(order.order_id))
        self.store.links[order.order_id] = link
        return link


class FakeLinkStore:
    def __init__(self):
        self.links = {}

    async def update(self, order_id, **fields):
        self.links[order_id] = replace(self.links[order_id], **fields)
        return self.links[order_id]


async def test_tick_processes_pending_orders():
    source = FakeSource([make_order(596), make_order(597)])
    engine = FakeEngine({596: make_link(596, "done"), 597: make_link(597, "error")})

    report = await Watcher(engine=engine, source=source).tick()

    assert engine.processed == [596, 597]
    assert report.scanned == 2
    assert report.by_status == {"done": 1, "error": 1}
    assert report.paused is False


async def test_paused_watcher_touches_nothing():
    """Выключатель владельца: ни базы бота, ни амо — вообще ни одного действия."""
    source = FakeSource([make_order()])
    engine = FakeEngine()

    report = await Watcher(engine=engine, source=source,
                           is_enabled=lambda: False).tick()

    assert report.paused is True
    assert report.scanned == 0
    assert source.calls == 0            # в базу даже не заглянули
    assert engine.processed == []


async def test_question_is_asked_once():
    """Карточка-вопрос уходит владельцу один раз, а не каждую минуту."""
    order = make_order()
    source = FakeSource([order])
    engine = FakeEngine({596: make_link(596, "waiting_owner")})
    sent = []

    async def on_question(order, link):
        sent.append(order.order_id)
        return 777                       # id отправленного сообщения в Telegram

    watcher = Watcher(engine=engine, source=source, on_question=on_question)
    report = await watcher.tick()

    assert sent == [596]
    assert report.questions == (596,)
    assert engine.store.links[596].question_msg_id == 777

    # следующий тик видит записанный id карточки и молчит
    engine.results[596] = engine.store.links[596]
    report = await watcher.tick()
    assert sent == [596]
    assert report.questions == ()


async def test_question_retried_when_sending_failed():
    """Телеграм не ответил — вопрос не потерян, попробуем на следующем тике."""
    source = FakeSource([make_order()])
    engine = FakeEngine({596: make_link(596, "waiting_owner")})
    attempts = []

    async def on_question(order, link):
        attempts.append(order.order_id)
        return None                      # отправить не удалось

    watcher = Watcher(engine=engine, source=source, on_question=on_question)
    await watcher.tick()
    await watcher.tick()

    assert attempts == [596, 596]
    assert engine.store.links[596].question_msg_id is None


async def test_one_broken_order_does_not_stop_the_tick():
    """Сбой на одном заказе не должен ронять весь проход."""
    source = FakeSource([make_order(596), make_order(597)])
    engine = FakeEngine({597: make_link(597, "done")})
    engine.raise_on = {596}

    report = await Watcher(engine=engine, source=source).tick()

    assert engine.processed == [596, 597]          # второй заказ всё равно обработан
    assert report.by_status == {"done": 1}
    assert [order_id for order_id, _ in report.failures] == [596]
    assert "база отвалилась" in report.failures[0][1]


async def test_run_forever_sleeps_between_ticks_and_stops():
    source = FakeSource([make_order()])
    engine = FakeEngine()
    stop = asyncio.Event()
    naps = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) == 3:
            stop.set()

    watcher = Watcher(engine=engine, source=source, poll_interval_sec=60, sleep=fake_sleep)
    await watcher.run_forever(stop)

    assert naps == [60, 60, 60]
    assert len(engine.processed) == 3


async def test_run_forever_survives_a_broken_tick():
    """Упавший тик (например, база недоступна) не убивает наблюдателя."""
    engine = FakeEngine()
    stop = asyncio.Event()
    ticks = []

    class BrokenSource(FakeSource):
        async def pending(self):
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("Postgres недоступен")
            return []

    async def fake_sleep(seconds):
        if len(ticks) >= 2:
            stop.set()

    watcher = Watcher(engine=engine, source=BrokenSource([]), sleep=fake_sleep)
    await watcher.run_forever(stop)

    assert len(ticks) == 2      # после падения цикл продолжил работу
