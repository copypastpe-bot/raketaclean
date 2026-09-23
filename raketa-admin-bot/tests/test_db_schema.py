"""Тесты схемы adminbot и слоя доступа к БД.

Нужен Postgres: DSN в переменной TEST_DB_DSN. Без него тесты пропускаются.
Пример временной базы:
    initdb -D /tmp/pgdata -U postgres --auth=trust
    pg_ctl -D /tmp/pgdata -o "-p 5433 -c listen_addresses=127.0.0.1" start
    createdb -h 127.0.0.1 -p 5433 -U postgres adminbot_test
    export TEST_DB_DSN=postgresql://postgres@127.0.0.1:5433/adminbot_test
"""

import os
from datetime import date, datetime, timedelta, timezone
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
                (101, 'Юлия', '+79159496642', '79159496642', NULL, 'пр. Гагарина, 12'),
                (102, 'Наталья', '+79101112233', '79101112233', NULL, NULL)
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
        # Клининг-контур: уборки лежат в своих таблицах, и их номера пересекаются
        # с номерами химчистки — уборка №596 и заказ №596 существуют разом.
        await conn.execute(
            """
            INSERT INTO public.cleaning_foremen (id, fn, ln, phone) VALUES
                (1, 'Лариса', 'Иванова', '+79200000001')
            """
        )
        await conn.execute(
            """
            INSERT INTO public.cleaning_orders
                (id, client_id, foreman_id, address, total_amount, happened_at, deleted_at) VALUES
                (3,   102, 1, 'Гагарина 1, кв 5',   3000.00, $1, NULL),
                (596, 100, 1, 'Менделеева 15, кв 3', 12000.00, $2, NULL),
                (5,   102, 1, 'Гагарина 1, кв 5',   9000.00, $3, NULL),
                (7,   101, 1, 'пр. Гагарина, 12',    4000.00, $3, $3)
            """,
            NOW - timedelta(days=5), NOW - timedelta(hours=2), NOW,
        )
        await conn.execute(
            """
            INSERT INTO public.cleaning_order_payments (id, order_id, method, amount) VALUES
                (1, 3,   'Расчётный',            3000.00),
                (2, 596, 'Карта',                5000.00),
                (3, 596, 'Наличные',             7000.00),
                (4, 5,   'Подарочный сертификат', 2000.00),
                (5, 5,   'Наличные',             7000.00),
                (6, 7,   'Наличные',             4000.00)
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


async def test_deal_address_is_stored_on_the_link(pool):
    """Колонка `deal_address` (миграция 012) — своя у каждой таблицы связок."""
    link = await db.create_link(pool, order_id=596, phone10="9601861067")
    assert link.deal_address is None                       # пока в сделке ничего не читали

    await db.update_link(pool, 596, deal_address="ул. Ленина, 5")
    assert (await db.get_link(pool, 596)).deal_address == "ул. Ленина, 5"

    # то же самое — для уборок, своя таблица связок
    await db.create_link(pool, order_id=5, phone10="9601861067", table=db.CLEANING_LINKS_TABLE)
    await db.update_link(pool, 5, table=db.CLEANING_LINKS_TABLE, deal_address="Менделеева, 15")
    cleaning_link = await db.get_link(pool, 5, table=db.CLEANING_LINKS_TABLE)
    assert cleaning_link.deal_address == "Менделеева, 15"


async def test_address_reminder_candidates(pool):
    """Задача 7 (ТЗ 2026-09-16): кто именно созрел для напоминания и почему."""
    real_now = datetime.now(timezone.utc)
    await db.create_link(pool, order_id=596, phone10="9601861067")
    await db.update_link(pool, 596, status="done", path="C", real_lead_id=41400001)

    due = await db.fetch_links_needing_address_reminder(pool)
    assert [link.order_id for link in due] == [596]        # ещё не напоминали
    assert due[0].address_reminder_count == 0

    # напомнили только что — до завтра больше не тревожим
    await db.update_link(pool, 596, address_reminder_count=1, address_reminder_sent_at=real_now)
    assert await db.fetch_links_needing_address_reminder(pool) == []

    # прошли сутки — снова созрел
    await db.update_link(pool, 596, address_reminder_sent_at=real_now - timedelta(days=2))
    due = await db.fetch_links_needing_address_reminder(pool)
    assert [link.order_id for link in due] == [596]

    # владелец сказал «не напоминать» — молчим, несмотря на срок
    await db.update_link(pool, 596, address_reminder_muted=True)
    assert await db.fetch_links_needing_address_reminder(pool) == []

    # адрес нашёлся (например, по кнопке «Я заполнил») — тоже причина молчать
    await db.update_link(pool, 596, address_reminder_muted=False, deal_address="ул. Ленина, 5")
    assert await db.fetch_links_needing_address_reminder(pool) == []

    # потолок: даже не muted, но счётчик уже на месте — не предлагаем снова
    await db.update_link(pool, 596, deal_address=None, address_reminder_count=7)
    assert await db.fetch_links_needing_address_reminder(pool, cap=7) == []

    # то же самое — для уборок, своя таблица связок
    await db.create_link(pool, order_id=5, phone10="9601861067", table=db.CLEANING_LINKS_TABLE)
    await db.update_link(pool, 5, table=db.CLEANING_LINKS_TABLE,
                         status="done", path="C", real_lead_id=41400002)
    cleaning_due = await db.fetch_links_needing_address_reminder(pool, table=db.CLEANING_LINKS_TABLE)
    assert [link.order_id for link in cleaning_due] == [5]


async def test_contact_reminder_candidates(pool):
    """Задача 5 (ТЗ 2026-09-22): кто созрел для напоминания про расхождение
    «номер + имя» с контактом сделки. По образцу `test_address_reminder_candidates`
    (ревью 23.09 — отдельного теста на живой базе для этого источника не было)."""
    real_now = datetime.now(timezone.utc)
    await db.create_calendar_link(pool, "evt-1", kind="order", phone10="9601861067")
    await db.update_calendar_link(
        pool, "evt-1", status="in_progress",
        contact_mismatch="В записи: Наталья, …1933. В CRM: Ирина, …4455.")

    due = await db.fetch_calendar_links_needing_contact_reminder(pool)
    assert [link.event_id for link in due] == ["evt-1"]     # ещё не напоминали
    assert due[0].contact_reminder_count == 0

    # напомнили час назад — до завтра больше не тревожим
    await db.update_calendar_link(pool, "evt-1", contact_reminder_count=1,
                                  contact_reminder_sent_at=real_now - timedelta(hours=1))
    assert await db.fetch_calendar_links_needing_contact_reminder(pool) == []

    # прошли сутки — снова созрел
    await db.update_calendar_link(pool, "evt-1",
                                  contact_reminder_sent_at=real_now - timedelta(days=2))
    due = await db.fetch_calendar_links_needing_contact_reminder(pool)
    assert [link.event_id for link in due] == ["evt-1"]

    # владелец нажал «Я разобрался» — молчим, несмотря на срок
    await db.update_calendar_link(pool, "evt-1", contact_reminder_muted=True)
    assert await db.fetch_calendar_links_needing_contact_reminder(pool) == []

    # сошлось при перепроверке цикла (contact_mismatch снят) — тоже молчим
    await db.update_calendar_link(pool, "evt-1", contact_reminder_muted=False,
                                  contact_mismatch=None)
    assert await db.fetch_calendar_links_needing_contact_reminder(pool) == []

    # потолок: даже не muted, но счётчик уже на месте — не предлагаем снова
    await db.update_calendar_link(
        pool, "evt-1", contact_mismatch="В записи: Наталья, …1933. В CRM: Ирина, …4455.",
        contact_reminder_count=7)
    assert await db.fetch_calendar_links_needing_contact_reminder(pool, cap=7) == []


async def test_dead_order_link_is_not_offered_for_address_reminder(pool):
    """Задача 6 (ТЗ 2026-09-17): сироте адрес напоминать некому и незачем."""
    await db.create_link(pool, order_id=9999, phone10="9601861067")
    await db.update_link(pool, 9999, status="done", path="C", real_lead_id=41500001)
    assert await db.fetch_links_needing_address_reminder(pool) == []

    # уборка №7 в фикстуре уже удалена (deleted_at)
    await db.create_link(pool, order_id=7, phone10="9159496642",
                         table=db.CLEANING_LINKS_TABLE)
    await db.update_link(pool, 7, table=db.CLEANING_LINKS_TABLE,
                         status="done", path="C", real_lead_id=41500002)
    assert await db.fetch_links_needing_address_reminder(
        pool, table=db.CLEANING_LINKS_TABLE) == []


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


async def test_touched_order_ids_fold_the_journal_into_a_set(pool):
    """Задача 2 (ТЗ 2026-09-21): «Провёл из бота» считаем по журналу, не по updated_at.

    Одно проведение работы пишет несколько строк журнала (move_lead, update_lead,
    add_note, complete_task) — свёртка должна отдать один номер заказа, а не
    четыре записи.
    """
    real_now = datetime.now(timezone.utc)
    await db.create_link(pool, order_id=596, phone10="9601861067")
    for action in ("move_lead", "update_lead", "add_note", "complete_task"):
        await db.log_action(pool, order_id=596, action=action, dry_run=False)
    await db.create_link(pool, order_id=597, phone10="9159496642")
    await db.log_action(pool, order_id=597, action="update_lead", dry_run=False)

    touched = await db.fetch_touched_order_ids(pool, real_now - timedelta(minutes=1))
    assert touched == frozenset({596, 597})

    # Окно суток реально режет: то, что было до него, не попадает.
    old_since = real_now + timedelta(hours=1)
    assert await db.fetch_touched_order_ids(pool, old_since) == frozenset()

    # Своя таблица журнала — своя выборка, заказы и уборки не путаются.
    await db.create_link(pool, order_id=5, phone10="9601861067", table=db.CLEANING_LINKS_TABLE)
    await db.log_action(pool, order_id=5, action="update_lead", dry_run=False,
                        table=db.CLEANING_ACTIONS_TABLE)
    cleaning_touched = await db.fetch_touched_order_ids(
        pool, real_now - timedelta(minutes=1), table=db.CLEANING_ACTIONS_TABLE)
    assert cleaning_touched == frozenset({5})


async def test_touched_order_ids_ignore_the_address_reminder_going_silent(pool):
    """Ловушка задачи 2: седьмое напоминание про адрес — не «провёл из бота».

    `address_reminder_capped` — единственное действие, которое журнал пишет
    заказу уже после `done` (`sync/address_reminder.py`, седьмое, замолкающее
    напоминание пути C; первые шесть в журнал вовсе не пишут). Свёртка должна
    его игнорировать, иначе заказ, завершённый месяц назад и получивший
    сегодня только это напоминание, ложно попал бы в «Провёл из бота» —
    в отличие от пути A/B/C, доведённого настоящим действием.
    """
    real_now = datetime.now(timezone.utc)
    await db.create_link(pool, order_id=598, phone10="9601861067")
    await db.log_action(pool, order_id=598, action="address_reminder_capped", dry_run=False,
                        payload={"count": 7})
    await db.create_link(pool, order_id=599, phone10="9159496642")
    await db.log_action(pool, order_id=599, action="update_lead", dry_run=False)

    touched = await db.fetch_touched_order_ids(pool, real_now - timedelta(minutes=1))
    assert touched == frozenset({599})


async def test_update_link_and_log_writes_both_in_one_go(pool):
    """Задача 8 (ТЗ 2026-09-21): решение владельца и запись в журнал — вместе.

    `update_link_and_log` заменяет отдельные вызовы `update_link`+`log_action`
    для кнопок-ответов владельца: если связка обновилась, строка в журнале
    появляется тоже, одним и тем же вызовом.
    """
    await db.create_link(pool, order_id=596, phone10="9601861067")

    link = await db.update_link_and_log(
        pool, 596, action="answer_owner", dry_run=False,
        payload={"choice": "manual"}, status="done", path="done")

    assert link.status == "done" and link.path == "done"
    actions = await db.fetch_actions(pool, 596)
    assert len(actions) == 1
    assert actions[0]["action"] == "answer_owner"
    assert actions[0]["payload"] == {"choice": "manual"}

    # Своя таблица журнала — для уборок, как и у update_link/log_action.
    await db.create_link(pool, order_id=5, phone10="9601861067", table=db.CLEANING_LINKS_TABLE)
    await db.update_link_and_log(
        pool, 5, table=db.CLEANING_LINKS_TABLE, actions_table=db.CLEANING_ACTIONS_TABLE,
        action="answer_owner", dry_run=False, payload={"choice": "new"},
        status="new", path="C")
    cleaning_actions = await db.fetch_actions(pool, 5, table=db.CLEANING_ACTIONS_TABLE)
    assert len(cleaning_actions) == 1 and cleaning_actions[0]["payload"] == {"choice": "new"}


async def test_update_link_and_log_skips_the_journal_when_nothing_was_saved(pool):
    """Работы уже нет — обновлять нечего, и запись в журнале не должна появиться."""
    result = await db.update_link_and_log(
        pool, 999999, action="answer_owner", dry_run=False,
        payload={"choice": "manual"}, status="done", path="done")

    assert result is None
    assert await db.fetch_actions(pool, 999999) == []


async def test_count_owner_handled_counts_only_manual_choice_in_window(pool):
    """Задача 8: «Передано администратору» — только «Сам разберусь», и только за сутки.

    Связка не различает нажатие «Сам разберусь» от автоматического
    `already_done` (обе пишут status="done", path="done") — считаем по журналу.
    """
    real_now = datetime.now(timezone.utc)
    await db.create_link(pool, order_id=596, phone10="9601861067")
    await db.update_link_and_log(pool, 596, action="answer_owner", dry_run=False,
                                 payload={"choice": "manual"}, status="done", path="done")
    await db.create_link(pool, order_id=597, phone10="9159496642")
    # «Заводи новую» тоже пишется в журнал (часть 1 задачи 8), но в счётчик не входит.
    await db.update_link_and_log(pool, 597, action="answer_owner", dry_run=False,
                                 payload={"choice": "new"}, status="new", path="C")
    # Автоматическое already_done журнал вообще не пишет — считать нечего.
    await db.update_link(pool, 597, status="done", path="done")

    count = await db.count_owner_handled(pool, real_now - timedelta(minutes=1))
    assert count == 1

    # Окно суток режет реально.
    assert await db.count_owner_handled(pool, real_now + timedelta(hours=1)) == 0

    # Своя таблица журнала — для уборок.
    await db.create_link(pool, order_id=5, phone10="9601861067", table=db.CLEANING_LINKS_TABLE)
    await db.update_link_and_log(
        pool, 5, table=db.CLEANING_LINKS_TABLE, actions_table=db.CLEANING_ACTIONS_TABLE,
        action="answer_owner", dry_run=False, payload={"choice": "manual"},
        status="done", path="done")
    cleaning_count = await db.count_owner_handled(
        pool, real_now - timedelta(minutes=1), table=db.CLEANING_ACTIONS_TABLE)
    assert cleaning_count == 1


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
    # В пропущенных лежит строка сводки, а не голый номер: владельцу нужны
    # телефон и дата заказа прямо в сообщении (его решение 2026-08-26).
    assert [row.order_id for row in summary.missed] == [597]
    assert summary.missed[0].phone10 == "9159496642"
    assert summary.missed[0].order_date is not None
    assert summary.total_orders == 2
    assert summary.is_quiet is False


async def test_summary_source_counts_processed_today_from_the_journal(pool):
    """Задача 2 (ТЗ 2026-09-21): «Провёл из бота» — сквозь весь источник среза.

    596 доведён и журнал тронут сегодня — считается. 597 доведён давно и сегодня
    журнал молчит по нему — не считается, хотя связка тоже «done»/путь A.
    """
    from adminbot.sync.reconcile import PgSummarySource, build_summary

    real_now = datetime.now(timezone.utc)
    await db.create_link(pool, order_id=596, phone10="9601861067")
    await db.update_link(pool, 596, status="done", path="A", real_lead_id=41400001)
    await db.log_action(pool, order_id=596, action="complete_task", dry_run=False)

    await db.create_link(pool, order_id=597, phone10="9159496642")
    await db.update_link(pool, 597, status="done", path="A", real_lead_id=41400002)
    # у 597 в журнале ничего сегодня — только сама связка

    source = PgSummarySource(pool, pool, NOW.date() - timedelta(days=3), now=lambda: real_now)
    summary = build_summary(await source.collect(), now=NOW)

    assert summary.processed_today == 1
    assert sorted(row.order_id for row in summary.processed) == [596, 597]

    # Окно можно сузить — тест не должен зависеть от реальных часов. «Сейчас»
    # сдвинуто на два часа вперёд, окно — час: действие, записанное только что,
    # оказывается за пределами окна.
    narrow = PgSummarySource(pool, pool, NOW.date() - timedelta(days=3), touched_window_hours=1,
                             now=lambda: real_now + timedelta(hours=2))
    assert (await narrow.collect()).touched_order_ids == frozenset()


async def test_summary_source_counts_handed_to_owner_from_the_journal(pool):
    """Задача 8: «Передано администратору» — сквозь весь источник среза заказов.

    596 — владелец нажал «Сам разберусь» сегодня, считается. 597 — «заводи
    новую» тоже в журнале, но choice другой, в счётчик не входит.
    """
    from adminbot.sync.reconcile import PgSummarySource, build_summary

    real_now = datetime.now(timezone.utc)
    await db.create_link(pool, order_id=596, phone10="9601861067")
    await db.update_link_and_log(pool, 596, action="answer_owner", dry_run=False,
                                 payload={"choice": "manual"}, status="done", path="done")
    await db.create_link(pool, order_id=597, phone10="9159496642")
    await db.update_link_and_log(pool, 597, action="answer_owner", dry_run=False,
                                 payload={"choice": "new"}, status="new", path="C")

    source = PgSummarySource(pool, pool, NOW.date() - timedelta(days=3), now=lambda: real_now)
    summary = build_summary(await source.collect(), now=NOW)

    assert summary.handed_to_owner == 1


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
    """Скрипт экзамена ходит в БД сам, мимо слоя db.py — проверяем и этот путь.

    Окно считаем от даты фикстуры, а не «последние три дня»: скрипт отмеряет дни
    от текущего момента, и с наступлением новых суток тест иначе разваливается.
    """
    from scripts.history_exam import load_orders

    days_since_fixture = (datetime.now(timezone.utc).date() - NOW.date()).days
    orders = await load_orders(TEST_DB_DSN, days=days_since_fixture + 2)

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


# --- ковры от партнёра ---

async def test_carpet_link_lifecycle(pool):
    """Ключ — номер заказа партнёра: месячный свод не должен обработаться повторно."""
    link = await db.create_carpet_link(pool, 44426, "9601945325", "Договоры (11).xlsx")
    assert link.status == "new" and link.source_file == "Договоры (11).xlsx"

    await db.update_carpet_link(pool, 44426, status="in_progress", lead_id=31516051)
    await db.mark_carpet_step(pool, 44426, "fill_carpet_lead")
    await db.mark_carpet_step(pool, 44426, "move_carpet_delivered")

    link = await db.get_carpet_link(pool, 44426)
    assert link.lead_id == 31516051
    assert set(link.checklist) == {"fill_carpet_lead", "move_carpet_delivered"}

    # тот же заказ приходит второй раз — строка одна, отметки на месте
    again = await db.create_carpet_link(pool, 44426, "9601945325")
    assert again.lead_id == 31516051 and len(again.checklist) == 2


async def test_update_carpet_link_and_log_writes_both_in_one_go(pool):
    """Задача 8: то же самое, что у заказов, но для ковровой связки."""
    await db.create_carpet_link(pool, 44426, "9601945325")

    link = await db.update_carpet_link_and_log(
        pool, 44426, action="answer_owner", dry_run=False,
        payload={"choice": "manual"}, status="done", path=None)

    assert link.status == "done"
    rows = await _fetch_carpet_actions(pool, 44426)
    assert len(rows) == 1 and rows[0]["payload"] == {"choice": "manual"}


async def test_update_carpet_link_and_log_skips_the_journal_when_nothing_was_saved(pool):
    result = await db.update_carpet_link_and_log(
        pool, 999999, action="answer_owner", dry_run=False,
        payload={"choice": "manual"}, status="done")

    assert result is None
    assert await _fetch_carpet_actions(pool, 999999) == []


async def _fetch_carpet_actions(pool, partner_id: int) -> list[dict]:
    """Журнал ковровой связки: своей `fetch_actions` у неё, в отличие от заказов, нет."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM adminbot.carpet_actions WHERE partner_id = $1 ORDER BY id",
            partner_id)
    return [dict(row) for row in rows]


async def test_carpet_question_survives_restart(pool):
    await db.create_carpet_link(pool, 44345, "9108970195")
    question = {"reason": "какая сделка про этот заказ",
                "options": [{"lead_id": 1, "date": "2026-08-12"}]}

    await db.update_carpet_link(pool, 44345, status="waiting_owner", question=question)

    link = await db.get_carpet_link(pool, 44345)
    assert link.question["options"][0]["lead_id"] == 1


async def test_carpet_taken_leads_are_not_reused(pool):
    """У клиента два заказа подряд — каждой работе своя сделка."""
    await db.create_carpet_link(pool, 44426, "9601945325")
    await db.update_carpet_link(pool, 44426, lead_id=31516051)
    await db.create_carpet_link(pool, 44427, "9601945325")

    taken = await db.fetch_carpet_taken_leads(pool, [31516051], exclude_partner_id=44427)
    assert taken == {31516051}


async def test_carpet_taken_lead_is_found_regardless_of_phone(pool):
    """Задача 6, ТЗ 2026-09-22: занятость — по номеру сделки, не по телефону.

    Тот же человек со вторым номером не должен выглядеть новым клиентом.
    """
    await db.create_carpet_link(pool, 44426, "9601945325")
    await db.update_carpet_link(pool, 44426, lead_id=31516051)
    await db.create_carpet_link(pool, 44427, "9219998877")

    taken = await db.fetch_carpet_taken_leads(pool, [31516051], exclude_partner_id=44427)
    assert taken == {31516051}


async def test_carpet_taken_lead_is_found_via_primary_lead_id_too(pool):
    """Круг правок по ревью 22.09: старая дыра в `fetch_carpet_taken_leads`.

    Путь `use_primary` пишет лид первичной в `primary_lead_id`, а не в
    `lead_id` — до этой правки функция смотрела только `lead_id`, и второй
    заказ того же клиента с кандидатом-совпадением по `primary_lead_id` не
    видел его занятым.
    """
    await db.create_carpet_link(pool, 44426, "9601945325")
    await db.update_carpet_link(pool, 44426, primary_lead_id=41500001)
    await db.create_carpet_link(pool, 44427, "9601945325")

    taken = await db.fetch_carpet_taken_leads(pool, [41500001], exclude_partner_id=44427)
    assert taken == {41500001}


async def test_unfinished_carpet_rows_are_found(pool):
    await db.create_carpet_link(pool, 1, "9601945325")
    await db.create_carpet_link(pool, 2, "9601945325")
    await db.update_carpet_link(pool, 2, status="done")

    active = await db.fetch_carpet_links_by_status(pool, ("new", "waiting_salesbot"))
    assert [link.partner_id for link in active] == [1]
    assert await db.count_carpet_links_by_status(pool) == {"new": 1, "done": 1}


async def test_processed_letter_is_remembered(pool):
    """Страховка на случай, если пометка в почтовом ящике не поставилась."""
    assert await db.letter_state(pool, "17") is None

    await db.remember_letter(pool, "17", "отчёт с 17.08 по 23.08",
                             ["Договоры (11).xlsx"], rows_total=4)

    assert await db.letter_state(pool, "17") == "processed"
    await db.remember_letter(pool, "17", "тот же", ["Договоры (11).xlsx"], 4)   # без дублей


async def test_held_letter_waits_for_the_owner(pool):
    """Отложенное письмо: робот его не проводит, пока владелец не снимет отложение."""
    await db.hold_letter(pool, "42", "отчёт за два года", ["архив.xlsx"],
                         rows_total=536, reason="строк 536, порог 100")

    assert await db.letter_state(pool, "42") == "held"
    held = await db.held_letters(pool)
    assert [(row["uid"], row["rows_total"], row["held_reason"]) for row in held] == [
        ("42", 536, "строк 536, порог 100")]
    assert held[0]["held_at"] is not None

    assert await db.release_letter(pool, "42") is True
    assert await db.release_letter(pool, "42") is False      # снимать больше нечего
    assert await db.letter_state(pool, "42") == "released"   # владелец разрешил провести
    assert await db.held_letters(pool) == []

    await db.remember_letter(pool, "42", "отчёт за два года", ["архив.xlsx"], 536)

    assert await db.letter_state(pool, "42") == "processed"


async def test_release_of_an_unknown_letter_changes_nothing(pool):
    assert await db.release_letter(pool, "нет такого") is False
    assert await db.letter_state(pool, "нет такого") is None


# --- календарь (этап 2) ---


async def test_calendar_event_survives_a_repeated_exchange(pool):
    """Запись календаря берётся в работу один раз: ключ — вечный id записи Google.

    Повторный обмен случается при каждом протухании закладки, и вторая сделка
    по той же записи — недопустима.
    """
    from adminbot.gcal.store import PgCalendarStore

    store = PgCalendarStore(pool)
    await store.create("evt-1", kind="order", phone10="9601861067",
                       order_date=date(2026, 8, 27), district="советский",
                       services=["mattress"], client_name="Юлия")
    await store.create("evt-1", kind="order", phone10="9601861067")

    link = await store.get("evt-1")

    assert link.status == "new"
    assert link.district == "советский"
    assert link.services == ("mattress",)
    assert (await db.count_calendar_links_by_status(pool)) == {"new": 1}


async def test_calendar_progress_and_bookmark_are_stored(pool):
    from adminbot.gcal.store import PgCalendarStore

    store = PgCalendarStore(pool)
    await store.create("evt-1", kind="order", phone10="9601861067")

    await store.update("evt-1", status="waiting_salesbot", primary_lead_id=41400001)
    await store.mark_step("evt-1", "fill_primary")
    await store.log("evt-1", "update_lead", dry_run=False, entity="lead", amo_id=41400001,
                    payload={"price": 0})
    await store.save_cursor("main@gmail.com", "TOKEN-1", sync_from=date(2026, 8, 27))

    link = await store.get("evt-1")
    assert link.status == "waiting_salesbot"
    assert "fill_primary" in link.checklist
    assert [row.event_id for row in await store.pending()] == ["evt-1"]
    assert await store.cursor("main@gmail.com") == ("TOKEN-1", date(2026, 8, 27))


async def test_calendar_created_counts_distinct_events_since(pool):
    """Задача 2 (ТЗ 2026-09-21): «Завёл из календаря» — свёртка create_lead по журналу."""
    from adminbot.gcal.store import PgCalendarStore

    store = PgCalendarStore(pool)
    real_now = datetime.now(timezone.utc)
    await store.create("evt-1", kind="order", phone10="9601861067")
    await store.log("evt-1", "create_contact", dry_run=False)
    await store.log("evt-1", "create_lead", dry_run=False, entity="lead", amo_id=41400001)
    await store.create("evt-2", kind="boat", phone10=None)
    await store.log("evt-2", "create_lead", dry_run=False, entity="lead", amo_id=41400002)

    assert await db.count_calendar_created(pool, real_now - timedelta(minutes=1)) == 2
    # Окно суток режет реально.
    assert await db.count_calendar_created(pool, real_now + timedelta(hours=1)) == 0


async def test_update_calendar_link_and_log_writes_both_in_one_go(pool):
    """Задача 8: то же самое, что у заказов, но для записи календаря."""
    await db.create_calendar_link(pool, "evt-1", kind="order", phone10="9605379757")

    link = await db.update_calendar_link_and_log(
        pool, "evt-1", action="answer_owner", dry_run=False,
        payload={"choice": "manual"}, status="skipped", skip_reason="владелец разбирается сам")

    assert link.status == "skipped"
    rows = await db.fetch_calendar_actions(pool, "evt-1")
    assert len(rows) == 1
    assert rows[0]["action"] == "answer_owner"
    assert rows[0]["payload"] == {"choice": "manual"}


async def test_update_calendar_link_and_log_skips_the_journal_when_nothing_was_saved(pool):
    result = await db.update_calendar_link_and_log(
        pool, "evt-missing", action="answer_owner", dry_run=False,
        payload={"choice": "manual"}, status="skipped")

    assert result is None
    assert await db.fetch_calendar_actions(pool, "evt-missing") == []


async def test_count_calendar_owner_handled_counts_only_manual_choice_in_window(pool):
    """Задача 8: та же логика счётчика, что у заказов, но по журналу календаря."""
    real_now = datetime.now(timezone.utc)
    await db.create_calendar_link(pool, "evt-1", kind="order", phone10="9605379757")
    await db.update_calendar_link_and_log(
        pool, "evt-1", action="answer_owner", dry_run=False,
        payload={"choice": "manual"}, status="skipped", skip_reason="владелец разбирается сам")
    await db.create_calendar_link(pool, "evt-2", kind="order", phone10="9159496642")
    # «Оставить как есть» — другой choice, в счётчик не входит.
    await db.update_calendar_link_and_log(
        pool, "evt-2", action="answer_owner", dry_run=False,
        payload={"choice": "keep"}, status="cancelled", skip_reason="владелец оставил как есть")

    count = await db.count_calendar_owner_handled(pool, real_now - timedelta(minutes=1))
    assert count == 1
    assert await db.count_calendar_owner_handled(pool, real_now + timedelta(hours=1)) == 0


# --- закладки, когда календарей несколько (2026-09-01) ---


async def _put_legacy_cursor(pool, token: str, sync_from: date) -> None:
    """Закладка, снятая до того, как календарей стало несколько (миграция 008)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO adminbot.gcal_cursor (calendar_id, sync_token, sync_from) "
            "VALUES ('', $1, $2)", token, sync_from)


async def test_each_calendar_keeps_its_own_bookmark(pool):
    """Два календаря — две закладки: чужая пачка изменений не должна теряться."""
    await db.save_calendar_cursor(pool, "main@gmail.com", "TOKEN-MAIN", date(2026, 9, 1))
    await db.save_calendar_cursor(pool, "brigade@group.calendar.google.com",
                                  "TOKEN-BRIGADE", date(2026, 9, 2))

    assert await db.get_calendar_cursor(pool, "main@gmail.com") == (
        "TOKEN-MAIN", date(2026, 9, 1))
    assert await db.get_calendar_cursor(pool, "brigade@group.calendar.google.com") == (
        "TOKEN-BRIGADE", date(2026, 9, 2))


async def test_calendar_without_bookmark_starts_clean(pool):
    """Новый календарь закладки не имеет — его первый обмен только запоминает."""
    assert await db.get_calendar_cursor(pool, "brigade@group.calendar.google.com") == (
        None, None)


async def test_old_bookmark_goes_to_the_main_calendar(pool):
    """Закладка «до нескольких календарей» достаётся первому из настроек.

    Потерять её — получить от Google полную перезагрузку календаря вместо
    изменений: робот увидел бы все записи разом.
    """
    await _put_legacy_cursor(pool, "OLD-TOKEN", date(2026, 8, 27))

    inherited = await db.get_calendar_cursor(pool, "main@gmail.com", inherit_legacy=True)

    assert inherited == ("OLD-TOKEN", date(2026, 8, 27))
    # Усыновление одноразовое: второму календарю чужая закладка не достанется.
    assert await db.get_calendar_cursor(
        pool, "brigade@group.calendar.google.com", inherit_legacy=True) == (None, None)
    # И она уже своя: повторное чтение без флага возвращает то же самое.
    assert await db.get_calendar_cursor(pool, "main@gmail.com") == (
        "OLD-TOKEN", date(2026, 8, 27))


async def test_migration_008_keeps_the_bookmark_of_a_live_database(pool):
    """На боевой базе закладка уже лежит — миграция обязана её сохранить.

    Проверяем ровно тот путь, которым пойдёт сервер: схема до 008, закладка
    по-старому (`id = 1`), затем миграция. Потерянная здесь строка означала бы
    полную перезагрузку календаря на первом же обмене после деплоя.
    """
    migration_008 = next(m for m in MIGRATIONS if m.name.startswith("008_"))
    older = [m for m in MIGRATIONS if m.name < "008_"]

    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS adminbot CASCADE")
        for migration in older:
            await conn.execute(migration.read_text())
        await conn.execute(
            "INSERT INTO adminbot.gcal_cursor (id, sync_token, sync_from) "
            "VALUES (1, 'LIVE-TOKEN', $1)", date(2026, 8, 27))

        await conn.execute(migration_008.read_text())
        # Скрипт обновления прогоняет миграции при каждом деплое — повтор безопасен.
        await conn.execute(migration_008.read_text())

    assert await db.get_calendar_cursor(pool, "main@gmail.com", inherit_legacy=True) == (
        "LIVE-TOKEN", date(2026, 8, 27))


async def test_own_bookmark_is_not_overwritten_by_the_old_one(pool):
    """У основного календаря уже своя закладка — старую строку он не забирает."""
    await db.save_calendar_cursor(pool, "main@gmail.com", "FRESH", date(2026, 9, 1))
    await _put_legacy_cursor(pool, "OLD-TOKEN", date(2026, 8, 27))

    assert await db.get_calendar_cursor(pool, "main@gmail.com", inherit_legacy=True) == (
        "FRESH", date(2026, 9, 1))


async def test_calendar_lead_taken_by_another_event_is_not_reused(pool):
    """Одна сделка не обслуживает две записи календаря."""
    from adminbot.gcal.store import PgCalendarStore

    store = PgCalendarStore(pool)
    await store.create("evt-1", kind="order", phone10="9601861067")
    await store.update("evt-1", real_lead_id=41400002)
    await store.create("evt-2", kind="order", phone10="9601861067")

    assert await store.taken_leads([41400002], exclude_event_id="evt-2") == {41400002}
    assert await store.taken_leads([41400002], exclude_event_id="evt-1") == set()


async def test_calendar_lead_taken_by_another_phone_of_the_same_person_is_not_reused(pool):
    """Задача 6, ТЗ 2026-09-22: занятость — по номеру сделки, не по телефону записи.

    Наталья звонит с одного номера, дежурный менеджер записывает в календарь
    другой — без этой правки сделка первой записи достанется второй.
    """
    from adminbot.gcal.store import PgCalendarStore

    store = PgCalendarStore(pool)
    await store.create("evt-1", kind="order", phone10="9601861067")
    await store.update("evt-1", real_lead_id=41400002)
    await store.create("evt-2", kind="order", phone10="9219998877")

    assert await store.taken_leads([41400002], exclude_event_id="evt-2") == {41400002}


# --- почта владельца (миграция 009) ---


async def test_undelivered_message_survives_in_the_outbox(pool):
    """Долг ложится в базу целиком — с кнопками, сроком и назначением.

    Кнопки здесь не украшение: карточка-вопрос без них станет сообщением,
    на которое владельцу нечем ответить.
    """
    from adminbot.tg.outbox import PgMailStore

    store = PgMailStore(pool)
    keyboard = {"inline_keyboard": [[{"text": "Закрыть", "callback_data": "gcal:close"}]]}
    letter_id = await store.add(
        chat_id=190933209, kind="gcal_question", ref="evt-1", text="Закрыть сделку?",
        reply_markup=keyboard, expires_at=NOW + timedelta(hours=24),
        next_try_at=NOW, error="TimeoutError: нет связи")

    due = await store.due(NOW)

    assert [letter["id"] for letter in due] == [letter_id]
    assert due[0]["reply_markup"] == keyboard          # jsonb вернулся словарём
    assert due[0]["ref"] == "evt-1"
    assert await store.waiting() == 1


async def test_delivered_and_dropped_letters_leave_the_queue(pool):
    """Доставленное и протухшее больше не созревают — очередь остаётся короткой."""
    from adminbot.tg.outbox import PgMailStore

    store = PgMailStore(pool)
    common = dict(chat_id=190933209, kind="summary", ref=None, reply_markup=None,
                  expires_at=NOW + timedelta(hours=6), next_try_at=NOW,
                  error="TimeoutError")
    delivered = await store.add(text="Сводка", **common)
    dropped = await store.add(text="Вторая сводка", **common)
    waiting = await store.add(text="Третья сводка", **common)

    await store.mark_sent(delivered, 4242, NOW)
    await store.drop(dropped, NOW, "протухло")

    assert [letter["id"] for letter in await store.due(NOW)] == [waiting]
    assert await store.waiting() == 1


async def test_postponed_letter_waits_for_its_turn(pool):
    """Неудачная попытка отодвигает следующую и считается."""
    from adminbot.tg.outbox import PgMailStore

    store = PgMailStore(pool)
    letter_id = await store.add(
        chat_id=190933209, kind="gcal_done", ref="evt-1", text="Сделка заведена",
        reply_markup=None, expires_at=NOW + timedelta(hours=24), next_try_at=NOW,
        error="TimeoutError")

    await store.postpone(letter_id, NOW + timedelta(minutes=3), "TimeoutError: снова")

    assert await store.due(NOW) == []
    later = await store.due(NOW + timedelta(minutes=3))
    assert later[0]["attempts"] == 2


async def test_finished_records_without_a_report_are_found(pool):
    """Работа сделана, отчёт не ушёл — робот найдёт такую запись и вернётся к ней."""
    from adminbot.gcal.store import PgCalendarStore

    store = PgCalendarStore(pool)
    await store.create("evt-lost", kind="order", phone10="9601861067")
    await store.update("evt-lost", status="done", real_lead_id=31600001)
    await store.create("evt-told", kind="order", phone10="9159496642")
    await store.update("evt-told", status="done", real_lead_id=31600002, done_msg_id=77)
    await store.create("evt-busy", kind="order", phone10="9159496642")
    await store.update("evt-busy", status="waiting_salesbot")

    since = datetime.now(timezone.utc) - timedelta(days=3)
    debts = await store.finished_without_report(since)

    assert [link.event_id for link in debts] == ["evt-lost"]
    assert await store.finished_without_report(
        datetime.now(timezone.utc) + timedelta(days=1)) == []


async def test_letters_of_a_forgotten_record_are_visible(pool):
    """Забывая запись, владелец должен видеть её недосланные письма.

    Иначе робот погасил бы долги молча, а владелец так и не узнал бы, о чём
    ему собирались написать.
    """
    from adminbot.tg.outbox import PgMailStore

    store = PgMailStore(pool)
    waiting = await store.add(
        chat_id=190933209, kind="gcal_done", ref="evt-dead", text="Сделка заведена",
        reply_markup=None, expires_at=NOW + timedelta(hours=24), next_try_at=NOW,
        error="TimeoutError")
    delivered = await store.add(
        chat_id=190933209, kind="gcal_done", ref="evt-dead", text="Ушло раньше",
        reply_markup=None, expires_at=NOW + timedelta(hours=24), next_try_at=NOW,
        error="TimeoutError")
    await store.mark_sent(delivered, 555, NOW)
    await store.add(
        chat_id=190933209, kind="gcal_done", ref="evt-other", text="Про другую запись",
        reply_markup=None, expires_at=NOW + timedelta(hours=24), next_try_at=NOW,
        error="TimeoutError")

    letters = await db.fetch_owner_letters_for(pool, "evt-dead")

    assert [letter["id"] for letter in letters] == [waiting]
    assert letters[0]["preview"] == "Сделка заведена"
    assert await db.fetch_owner_letters_for(pool, "evt-missing") == []


# --- уборки клининг-контура (2026-09-10) ---


async def test_cleaning_orders_come_from_their_own_tables(pool):
    """Уборка приходит целиком: свой адрес, бригадир, оплата и подпись."""
    orders = await db.fetch_unprocessed_cleaning_orders(
        pool, pool, since=NOW.date() - timedelta(days=10))

    # по времени работы: 3 (пять дней назад), 596 (два часа назад), 5 (сейчас).
    # Уборка №7 удалена — её в списке нет вовсе.
    assert [o.order_id for o in orders] == [3, 596, 5]

    cleaning = {o.order_id: o for o in orders}[596]
    assert cleaning.kind == "cleaning" and cleaning.label == "Уборка"
    assert cleaning.phone10 == "9601861067"
    assert cleaning.client_name == "Ирина"
    assert cleaning.address == "Менделеева 15, кв 3"     # адрес свой, не из карточки клиента
    assert cleaning.amount_total == Decimal("12000.00")
    assert cleaning.masters == [("Лариса Иванова", "+79200000001")]
    assert cleaning.rating_score is None                 # оценки у клининга нет
    assert cleaning.awaiting_wire_payment is False       # оплату вносят при проведении
    assert cleaning.service_kind == "cleaning"
    assert cleaning.specialist_enums == (952251,)        # «Ольга Скоропашкина»


async def test_payment_method_is_the_first_row_of_the_order(pool):
    """Способов оплаты бывает несколько; основной — первый внесённый.

    Так же считает и сам бот, когда начисляет бонусы (`cleaning/handlers.py`).
    """
    by_id = {o.order_id: o for o in
             await db.fetch_cleaning_orders_since(pool, NOW.date() - timedelta(days=10))}

    assert by_id[596].payment_method == "Карта"                 # вторая строка — «Наличные»
    assert by_id[5].payment_method == "Подарочный сертификат"   # вторая строка — «Наличные»
    assert by_id[3].payment_method == "Расчётный"


async def test_deleted_cleaning_is_never_taken_into_work(pool):
    """Уборку удалили в боте — в CRM её проводить нельзя."""
    orders = await db.fetch_cleaning_orders_since(pool, NOW.date() - timedelta(days=60))

    assert 7 not in [o.order_id for o in orders]
    assert await db.fetch_cleaning_orders_by_ids(pool, [7]) == []


async def test_repeat_client_is_seen_across_both_tables(pool):
    """«Источник сделки»: первый раз — сарафан, дальше — повторный заказ.

    Клиент мог прийти сначала за химчисткой, а уборку заказать впервые — это
    всё равно повторное обращение, и «сарафаном» его называть нельзя.
    """
    by_id = {o.order_id: o for o in
             await db.fetch_cleaning_orders_since(pool, NOW.date() - timedelta(days=60))}

    assert by_id[3].is_repeat_client is False    # у Натальи это первая работа вообще
    assert by_id[5].is_repeat_client is True     # у неё же была уборка пять дней назад
    assert by_id[596].is_repeat_client is True   # у Ирины химчистка месяц назад


async def test_cleaning_and_order_with_the_same_number_are_two_works(pool):
    """Номера таблиц бота пересекаются: связки лежат раздельно."""
    await db.create_link(pool, order_id=596, phone10="9601861067")
    await db.create_link(pool, order_id=596, phone10="9601861067",
                         table=db.CLEANING_LINKS_TABLE)
    await db.update_link(pool, 596, table=db.CLEANING_LINKS_TABLE,
                         status="done", path="A", real_lead_id=41400002)

    assert (await db.get_link(pool, 596)).status == "new"          # заказ не тронут
    assert (await db.get_link(pool, 596, table=db.CLEANING_LINKS_TABLE)).status == "done"

    # уборка №596 взята в работу — в очередь новых уборок она больше не попадает
    fresh = await db.fetch_unprocessed_cleaning_orders(
        pool, pool, since=NOW.date() - timedelta(days=60))
    assert [o.order_id for o in fresh] == [3, 5]


async def test_a_lead_taken_by_one_stream_is_not_reused_by_the_other(pool):
    """Уборка и химчистка одному клиенту в один день — это две сделки."""
    await db.create_link(pool, order_id=597, phone10="9601861067")
    await db.update_link(pool, 597, real_lead_id=31500001)
    await db.create_link(pool, order_id=5, phone10="9601861067",
                         table=db.CLEANING_LINKS_TABLE)
    await db.update_link(pool, 5, table=db.CLEANING_LINKS_TABLE, real_lead_id=31500002)

    # уборка не берёт сделку, занятую химчисткой...
    assert await db.fetch_taken_lead_ids(pool, [31500001, 31500002], 5,
                                         table=db.CLEANING_LINKS_TABLE) == {31500001}
    # ...и наоборот
    assert await db.fetch_taken_lead_ids(pool, [31500001, 31500002], 597) == {31500002}


async def test_lead_taken_by_another_phone_of_the_same_person_is_not_reused(pool):
    """Задача 6, ТЗ 2026-09-22: занятость — по номеру сделки, не по телефону.

    Одна и та же Наталья с двумя номерами не должна выглядеть для робота
    как два разных клиента — иначе сделка первой работы достаётся второй.
    """
    await db.create_link(pool, order_id=597, phone10="9601861067")
    await db.update_link(pool, 597, real_lead_id=31500001)
    await db.create_link(pool, order_id=598, phone10="9219998877")

    assert await db.fetch_taken_lead_ids(pool, [31500001], 598) == {31500001}


async def test_dead_order_link_does_not_hold_the_lead_taken(pool):
    """Задача 6 (ТЗ 2026-09-17): у сироты заказ пропал — сделка свободна.

    Химчистка удаляется физически: заказа №9999 в `public.orders` не было и
    не будет. Уборка №7 в фикстуре уже помечена `deleted_at`. Ни та, ни
    другая связка не должна держать свою сделку занятой.
    """
    await db.create_link(pool, order_id=9999, phone10="9601861067")
    await db.update_link(pool, 9999, real_lead_id=41500001)
    await db.create_link(pool, order_id=7, phone10="9159496642",
                         table=db.CLEANING_LINKS_TABLE)
    await db.update_link(pool, 7, table=db.CLEANING_LINKS_TABLE, real_lead_id=41500002)

    # заказ 9999 мёртв в своей же таблице связок...
    assert await db.fetch_taken_lead_ids(pool, [41500001, 41500002], 1) == set()
    # ...и он же мёртв, когда его смотрят из «чужой» таблицы (проверка обеих веток UNION)
    assert await db.fetch_taken_lead_ids(pool, [41500001, 41500002], 1,
                                         table=db.CLEANING_LINKS_TABLE) == set()
    # уборка №7 удалена — её лид тоже свободен
    assert await db.fetch_taken_lead_ids(pool, [41500002], 1) == set()


async def test_cleaning_journal_is_its_own(pool):
    await db.create_link(pool, order_id=5, phone10="9601861067",
                         table=db.CLEANING_LINKS_TABLE)
    await db.log_action(pool, order_id=5, action="update_lead", dry_run=False,
                        amo_entity="lead", amo_id=41400002, payload={"price": 9000},
                        table=db.CLEANING_ACTIONS_TABLE)
    await db.mark_checklist_step(pool, 5, "fill_realization",
                                 table=db.CLEANING_LINKS_TABLE)

    actions = await db.fetch_actions(pool, 5, table=db.CLEANING_ACTIONS_TABLE)
    assert [row["action"] for row in actions] == ["update_lead"]
    assert await db.fetch_actions(pool, 5) == []      # в журнале заказов пусто
    link = await db.get_link(pool, 5, table=db.CLEANING_LINKS_TABLE)
    assert set(link.checklist) == {"fill_realization"}


async def test_cleaning_source_takes_new_and_unfinished(pool):
    """Источник работы по уборкам: новые плюс недоделанные, как у заказов."""
    from adminbot.sync.watcher import PgCleaningSource

    await db.create_link(pool, order_id=3, phone10="79101112233",
                         table=db.CLEANING_LINKS_TABLE)
    await db.update_link(pool, 3, table=db.CLEANING_LINKS_TABLE,
                         status="error", last_error="AmoError: 502")
    await db.create_link(pool, order_id=596, phone10="9601861067",
                         table=db.CLEANING_LINKS_TABLE)
    await db.update_link(pool, 596, table=db.CLEANING_LINKS_TABLE,
                         status="done", path="A", real_lead_id=41400002)

    source = PgCleaningSource(pool, pool, NOW.date() - timedelta(days=60))
    orders = await source.pending()

    assert [o.order_id for o in orders] == [3, 5]      # 3 — с ошибкой, 5 — новая
    assert orders[0].client_name == "Наталья"          # уборка приходит целиком


async def test_evening_summary_sees_cleanings_separately(pool):
    """Вечерняя сверка по уборкам считает свою таблицу, а не таблицу заказов."""
    from adminbot.sync.reconcile import PgCleaningSummarySource, build_summary

    await db.create_link(pool, order_id=596, phone10="9601861067",
                         table=db.CLEANING_LINKS_TABLE)
    await db.update_link(pool, 596, table=db.CLEANING_LINKS_TABLE,
                         status="done", path="A", real_lead_id=41400002)

    snapshot = await PgCleaningSummarySource(
        pool, pool, NOW.date() - timedelta(days=60)).collect()
    summary = build_summary(snapshot, now=NOW)

    assert [row.order_id for row in summary.processed] == [596]
    assert sorted(row.order_id for row in summary.missed) == [3, 5]
    assert summary.total_orders == 3


async def test_evening_summary_of_cleanings_counts_handed_to_owner(pool):
    """Задача 8: «Передано администратору» считается и по уборкам — своя таблица."""
    from adminbot.sync.reconcile import PgCleaningSummarySource, build_summary

    await db.create_link(pool, order_id=5, phone10="9601861067",
                         table=db.CLEANING_LINKS_TABLE)
    await db.update_link_and_log(
        pool, 5, table=db.CLEANING_LINKS_TABLE, actions_table=db.CLEANING_ACTIONS_TABLE,
        action="answer_owner", dry_run=False, payload={"choice": "manual"},
        status="done", path="done")

    snapshot = await PgCleaningSummarySource(
        pool, pool, NOW.date() - timedelta(days=60)).collect()
    summary = build_summary(snapshot, now=NOW)

    assert summary.handed_to_owner == 1


async def test_calendar_jobs_view_has_all_columns(pool):
    """Представление `adminbot.calendar_jobs` для рабочего бота со всеми колонками."""
    from adminbot.gcal.store import PgCalendarStore

    store = PgCalendarStore(pool)
    await store.create("evt-1", kind="order", phone10="9601861067",
                       order_date=date(2026, 9, 23), client_name="Юлия")
    await store.update("evt-1", status="in_progress", primary_lead_id=41400001,
                       real_lead_id=41400002)

    # проверяем, что представление существует и отдаёт строку с нужными колонками
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM adminbot.calendar_jobs WHERE event_id = $1", "evt-1")

    assert len(rows) == 1
    row = dict(rows[0])

    # ожидаемые колонки по ТЗ задачи 8
    expected_columns = {
        "event_id", "phone10", "phones", "order_date", "client_name", "address",
        "services", "primary_lead_id", "real_lead_id", "status", "order_id"
    }
    actual_columns = set(row.keys())

    assert actual_columns == expected_columns
    assert row["event_id"] == "evt-1"
    assert row["phone10"] == "9601861067"
    assert row["primary_lead_id"] == 41400001
    assert row["real_lead_id"] == 41400002
    assert row["status"] == "in_progress"
