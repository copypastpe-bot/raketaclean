"""Тесты схемы adminbot и слоя доступа к БД.

Нужен Postgres: DSN в переменной TEST_DB_DSN. Без него тесты пропускаются.
Пример временной базы:
    initdb -D /tmp/pgdata -U postgres --auth=trust
    pg_ctl -D /tmp/pgdata -o "-p 5433 -c listen_addresses=127.0.0.1" start
    createdb -h 127.0.0.1 -p 5433 -U postgres adminbot_test
    export TEST_DB_DSN=postgresql://postgres@127.0.0.1:5433/adminbot_test
"""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from adminbot import db

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DB_DSN, reason="TEST_DB_DSN не задан — нужен Postgres")

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "migrations").glob("*.sql"))
BOT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bot_schema_min.sql"

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def pool():
    """Чистая тестовая база: схема adminbot + урезанные таблицы бота с данными."""
    pool = await db.create_pool(TEST_DB_DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS adminbot CASCADE")
        for migration in MIGRATIONS:
            await conn.execute(migration.read_text())
        await conn.execute(BOT_FIXTURE.read_text())
        await conn.execute(
            """
            INSERT INTO public.staff (id, full_name, first_name, last_name, phone) VALUES
                (1, 'Никита Иванов', 'Никита', 'Иванов', '+79101251720'),
                (2, NULL, 'Оля', 'Петрова', NULL)
            """
        )
        await conn.execute(
            """
            INSERT INTO public.clients (id, full_name, phone, phone_digits, address, last_order_addr) VALUES
                (100, 'Ирина', '+79601861067', '79601861067', 'ул. Ленина, 5', NULL),
                (101, 'Юлия', '+79159496642', '79159496642', NULL, 'пр. Гагарина, 12')
            """
        )
        await conn.execute(
            """
            INSERT INTO public.orders
                (id, phone, phone_digits, customer_name, client_id, master_id,
                 amount_total, upsale_amount, rating_score, created_at) VALUES
                (596, '+79601861067', '79601861067', 'Ирина', 100, 1, 5950.00, 950.00, 5, $1),
                (597, '+79159496642', '79159496642', 'Юлия',  101, 2, 3300.00,   0.00, NULL, $2),
                (500, '+79601861067', '79601861067', 'Ирина', 100, 1, 1000.00,   0.00, NULL, $3)
            """,
            NOW, NOW - timedelta(days=1), NOW - timedelta(days=30),
        )
        await conn.execute(
            """
            INSERT INTO public.order_masters (order_id, master_id) VALUES
                (596, 1), (596, 2), (597, 2)
            """
        )
    try:
        yield pool
    finally:
        await pool.close()


async def test_fetch_unprocessed_orders_reads_bot_data(pool):
    orders = await db.fetch_unprocessed_orders(pool, pool, since=NOW.date() - timedelta(days=3))

    assert [o.order_id for o in orders] == [597, 596]      # старые сначала: 597 (вчера), 596 (сегодня)

    order = orders[1]
    assert order.phone10 == "9601861067"                   # канонические 10 цифр
    assert order.amount_total == Decimal("5950.00")     # бюджет сделки в амо = финальная сумма чека
    assert not hasattr(order, "upsell_amount")          # доп. продажа в амо не уходит (только ЗП мастера)
    assert order.rating_score == 5                         # оценка есть → «Получить ОС» закроем
    assert order.client_name == "Ирина"
    assert order.address == "ул. Ленина, 5"
    assert order.master_names == ["Никита Иванов", "Оля Петрова"]   # основной мастер первым
    assert order.masters == [("Никита Иванов", "+79101251720"), ("Оля Петрова", None)]

    older = await db.fetch_unprocessed_orders(pool, pool, since=NOW.date() - timedelta(days=60))
    assert [o.order_id for o in older] == [500, 597, 596]


async def test_fetch_unprocessed_skips_linked_orders(pool):
    await db.create_link(pool, order_id=596, phone10="9601861067")

    orders = await db.fetch_unprocessed_orders(pool, pool, since=NOW.date() - timedelta(days=3))
    assert [o.order_id for o in orders] == [597]           # 596 уже привязан — не берём повторно


async def test_link_lifecycle_and_checklist(pool):
    link = await db.create_link(pool, order_id=596, phone10="9601861067")
    assert link.status == "new" and link.checklist == {}

    await db.update_link(pool, 596, status="in_progress", path="A", real_lead_id=41463832)
    await db.mark_checklist_step(pool, 596, "fill_realization")
    await db.mark_checklist_step(pool, 596, "move_realization_done")

    link = await db.get_link(pool, 596)
    assert link.status == "in_progress"
    assert link.path == "A"
    assert link.real_lead_id == 41463832
    assert set(link.checklist) == {"fill_realization", "move_realization_done"}

    # повторная запись того же шага не ломает чек-лист (идемпотентность, дизайн §5.4)
    await db.mark_checklist_step(pool, 596, "fill_realization")
    link = await db.get_link(pool, 596)
    assert len(link.checklist) == 2

    assert await db.get_link(pool, 999999) is None


async def test_actions_journal(pool):
    await db.create_link(pool, order_id=596, phone10="9601861067")
    await db.log_action(
        pool, order_id=596, action="update_lead", amo_entity="lead",
        amo_id=41463832, dry_run=True, payload={"price": 5950},
    )
    actions = await db.fetch_actions(pool, 596)
    assert len(actions) == 1
    assert actions[0]["action"] == "update_lead"
    assert actions[0]["dry_run"] is True
    assert actions[0]["payload"] == {"price": 5950}


async def test_fetch_orders_by_ids_returns_only_requested(pool):
    """Наблюдателю нужно вернуться к конкретным заказам, а не перебирать весь хвост."""
    orders = await db.fetch_orders_by_ids(pool, [596, 500])

    assert [o.order_id for o in orders] == [500, 596]      # порядок — по времени заказа
    assert await db.fetch_orders_by_ids(pool, []) == []


async def test_active_links_and_summary_rows(pool):
    await db.create_link(pool, order_id=596, phone10="9601861067")
    await db.update_link(pool, 596, status="waiting_salesbot", path="B", primary_lead_id=41400001)
    await db.create_link(pool, order_id=597, phone10="9159496642")
    await db.update_link(pool, 597, status="done", path="A", real_lead_id=41400002)

    active = await db.fetch_link_ids_by_status(pool, ("waiting_salesbot", "error", "new"))
    assert active == [596]                                  # завершённый заказ в работу не берём

    links = await db.fetch_links_for_orders(pool, [596, 597, 500])
    assert [link.order_id for link in links] == [596, 597]  # у 500 привязки нет
    assert links[1].path == "A" and links[1].real_lead_id == 41400002


async def test_watcher_source_takes_new_and_unfinished_orders(pool):
    """Источник работы наблюдателя: новые заказы плюс недоделанные."""
    from adminbot.sync.watcher import PgOrderSource

    await db.create_link(pool, order_id=596, phone10="9601861067")
    await db.update_link(pool, 596, status="error", last_error="AmoError: 502")
    await db.create_link(pool, order_id=597, phone10="9159496642")
    await db.update_link(pool, 597, status="done", path="A", real_lead_id=41400002)

    source = PgOrderSource(pool, pool, NOW.date() - timedelta(days=60))
    orders = await source.pending()

    assert [o.order_id for o in orders] == [500, 596]       # 500 — новый, 596 — с ошибкой
    assert orders[1].client_name == "Ирина"                 # заказ приходит целиком, а не одним id


async def test_summary_source_sees_orders_robot_never_touched(pool):
    """Вечерняя сверка ловит именно пропуски: заказ есть, а привязки нет."""
    from adminbot.sync.reconcile import PgSummarySource, build_summary

    await db.create_link(pool, order_id=596, phone10="9601861067")
    await db.update_link(pool, 596, status="done", path="A", real_lead_id=41400002)

    snapshot = await PgSummarySource(pool, pool, NOW.date() - timedelta(days=3)).collect()
    summary = build_summary(snapshot, now=NOW)

    assert [row.order_id for row in summary.processed] == [596]
    assert summary.missed == (597,)
    assert summary.total_orders == 2
    assert summary.is_quiet is False


async def test_pause_survives_a_restart(pool):
    """Пауза владельца лежит в базе, а не в памяти сервиса."""
    from adminbot.control import PgControlPanel

    panel = PgControlPanel(pool)
    assert await panel.is_paused() is False

    await panel.set_paused(True)
    assert await PgControlPanel(pool).is_paused() is True     # «перезапуск»: новый объект

    await panel.set_paused(False)
    assert await PgControlPanel(pool).is_paused() is False


async def test_queue_counts_for_status_command(pool):
    from adminbot.control import PgControlPanel

    await db.create_link(pool, order_id=596, phone10="9601861067")
    await db.create_link(pool, order_id=597, phone10="9159496642")
    await db.update_link(pool, 597, status="done", path="A", real_lead_id=1)

    assert await PgControlPanel(pool).queue_counts() == {"new": 1, "done": 1}


async def test_history_exam_loads_orders_from_bot_db(pool):
    """Скрипт экзамена ходит в БД сам, мимо слоя db.py — проверяем и этот путь."""
    from scripts.history_exam import load_orders

    orders = await load_orders(TEST_DB_DSN, days=3)

    assert [o.order_id for o in orders] == [597, 596]
    by_id = {o.order_id: o for o in orders}
    assert by_id[596].masters == [("Никита Иванов", "+79101251720"), ("Оля Петрова", None)]
    assert by_id[596].phone10 == "9601861067"


async def test_no_writes_to_public_schema(pool):
    """Хард-правило проекта: в схему бота не пишем. Проверяем правами БД."""
    async with pool.acquire() as conn:
        await conn.execute("CREATE ROLE adminbot_ro_test LOGIN")
        await conn.execute("REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM adminbot_ro_test")
        await conn.execute("GRANT USAGE ON SCHEMA public TO adminbot_ro_test")
        await conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO adminbot_ro_test")
    ro_dsn = TEST_DB_DSN.replace("postgres@", "adminbot_ro_test@")
    ro_pool = await db.create_pool(ro_dsn, min_size=1, max_size=1)
    try:
        orders = await db.fetch_unprocessed_orders(ro_pool, pool, since=NOW.date() - timedelta(days=3))
        assert len(orders) == 2                            # читать может
        with pytest.raises(Exception):                     # писать — нет
            async with ro_pool.acquire() as conn:
                await conn.execute("UPDATE public.orders SET amount_total = 1 WHERE id = 596")
    finally:
        await ro_pool.close()
        async with pool.acquire() as conn:
            await conn.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM adminbot_ro_test")
            await conn.execute("REVOKE ALL ON SCHEMA public FROM adminbot_ro_test")
            await conn.execute("DROP ROLE IF EXISTS adminbot_ro_test")
