"""Фоновый проход дозаполнения адреса (`run_address_backfill`, bot.py).

Задача 4 ТЗ «адреса до конца» (docs/plans/2026-09-16-addresses.md, 2026-09-16):
мастера адрес не вводят, рабочий бот в amoCRM за ним больше не ходит (задача 3).
Адрес заказа, `clients.address` и `clients.last_order_addr` дозаполняются из
связки, которую ведёт админ-бот в своей схеме (`adminbot.amo_links` для
химчистки, `adminbot.cleaning_links` для уборок, колонка `deal_address`).

Один и тот же проход обслуживает обычный случай (связка появляется через
минуту-две после заказа) и поздний (адрес дописан на следующий день после
«Я заполнил» владельца) — проверяем именно это: связки нет -> ничего не
происходит; связка появилась -> адрес доезжает до всех трёх мест.
"""

import unittest
from unittest import mock

import bot


class FakeTransaction:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        self.log.append("tx:begin")
        return self

    async def __aexit__(self, *exc):
        self.log.append("tx:commit")
        return False


class FakeConn:
    """Отдаёт заготовленные строки по очереди на каждый fetch, запоминает execute."""

    def __init__(self, fetch_results, *, fail_on_order_id=None):
        self._fetch_results = list(fetch_results)
        self.executed: list[tuple[str, tuple]] = []
        self.log: list[str] = []
        self._fail_on_order_id = fail_on_order_id

    def transaction(self):
        return FakeTransaction(self.log)

    async def fetch(self, query, *args):
        return self._fetch_results.pop(0) if self._fetch_results else []

    async def execute(self, query, *args):
        if (
            self._fail_on_order_id is not None
            and "UPDATE orders SET address" in query
            and args[1] == self._fail_on_order_id
        ):
            raise RuntimeError("boom")
        self.executed.append((query, args))
        return "UPDATE 1"


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class AddressBackfillTests(unittest.IsolatedAsyncioTestCase):
    def _install_pool(self, conn):
        pool_patch = mock.patch.object(bot, "pool", FakePool(conn))
        pool_patch.start()
        self.addCleanup(pool_patch.stop)

    async def test_no_pool_is_noop(self):
        with mock.patch.object(bot, "pool", None):
            await bot.run_address_backfill()  # не должно падать

    async def test_link_with_address_fills_order_and_client(self):
        conn = FakeConn(fetch_results=[
            [{"order_id": 637, "client_id": 42, "deal_address": "г. Москва, ул. Ленина, д. 5"}],
            [],  # проход по cleaning_orders — связок нет
        ])
        self._install_pool(conn)

        await bot.run_address_backfill()

        order_update = next(
            args for q, args in conn.executed if "UPDATE orders SET address" in q
        )
        self.assertEqual(order_update, ("г. Москва, ул. Ленина, д. 5", 637))

        client_update = next(
            args for q, args in conn.executed if "UPDATE clients SET address" in q
        )
        self.assertEqual(client_update[0], "г. Москва, ул. Ленина, д. 5")
        self.assertEqual(client_update[1], 42)
        # адрес пишется и в last_order_addr той же командой
        client_query = next(q for q, _ in conn.executed if "UPDATE clients SET address" in q)
        self.assertIn("last_order_addr", client_query)

    async def test_link_without_address_is_skipped_silently(self):
        """Связка появилась (робот её уже завёл), но адреса в сделке ещё нет —
        проход не пишет ничего и не падает: адрес может приехать позже."""
        conn = FakeConn(fetch_results=[
            [{"order_id": 637, "client_id": 42, "deal_address": None}],
            [{"order_id": 638, "client_id": 43, "deal_address": "   "}],
        ])
        self._install_pool(conn)

        await bot.run_address_backfill()

        self.assertEqual(conn.executed, [])

    async def test_no_link_yet_is_skipped(self):
        """Связки нет вовсе (INNER JOIN ничего не вернул) — тоже тихо ждём."""
        conn = FakeConn(fetch_results=[[], []])
        self._install_pool(conn)

        await bot.run_address_backfill()

        self.assertEqual(conn.executed, [])

    async def test_cleaning_orders_are_backfilled_too(self):
        """Задача 4 явно требует обе пары таблиц: химчистка и уборки."""
        conn = FakeConn(fetch_results=[
            [],  # orders — пусто
            [{"order_id": 88, "client_id": 7, "deal_address": "Мира, 10"}],
        ])
        self._install_pool(conn)

        await bot.run_address_backfill()

        order_update = next(
            args for q, args in conn.executed if "UPDATE cleaning_orders SET address" in q
        )
        self.assertEqual(order_update, ("Мира, 10", 88))

    async def test_one_bad_row_does_not_stop_the_pass(self):
        """Один заказ упал (например, конфликт в БД) — остальные всё равно дозаполняются."""
        conn = FakeConn(
            fetch_results=[
                [
                    {"order_id": 1, "client_id": 10, "deal_address": "Первая, 1"},
                    {"order_id": 2, "client_id": 20, "deal_address": "Вторая, 2"},
                ],
                [],
            ],
            fail_on_order_id=1,
        )
        self._install_pool(conn)

        await bot.run_address_backfill()  # не должно упасть наружу

        order_update = next(
            args for q, args in conn.executed if "UPDATE orders SET address" in q
        )
        self.assertEqual(order_update, ("Вторая, 2", 2))


if __name__ == "__main__":
    unittest.main()
