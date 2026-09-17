"""Обработчик удалений (ТЗ 2026-09-17 «удаление заказа освобождает сделку»,
задача 5): пять веток разбора плюс отдельный случай «связка есть, сделки ещё
нет».

Нужен Postgres: DSN в переменной TEST_DB_DSN (как в test_deletions_source.py) —
ветка 2 («сделка занята связкой другого живого заказа») проверяется одним
запросом, который джойнит `adminbot.amo_links` / `cleaning_links` с
`public.deleted_orders` / `cleaning_orders`, и без настоящей базы этот джойн
не проверить (урок 17.09 — дефект `ON CONFLICT` тесты без живой базы не ловили).

amoCRM и Telegram — двойники: `FakeAmo` из tests/fakes.py и собственный
список уведомлений вместо `OwnerMail`.
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adminbot import db
from adminbot.amo import ids
from adminbot.sync.deletions import DeletionHandler, fetch_pending_deletions
from adminbot.sync.store import PgCleaningLinkStore, PgLinkStore
from tests.fakes import FakeAmo

TEST_DB_DSN = os.environ.get("TEST_DB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DB_DSN, reason="TEST_DB_DSN не задан — нужен Postgres")

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "migrations").glob("*.sql"))
BOT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bot_schema_min.sql"

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def pool():
    """Чистая тестовая база: все миграции админ-бота + урезанные таблицы
    рабочего бота, с данными на каждую ветку разбора сразу.

    Номера заказов подобраны так, чтобы ветки не пересекались друг с другом:
    у каждого свой номер сделки в амо.
    """
    pool = await db.create_pool(TEST_DB_DSN, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS adminbot CASCADE")
        for migration in MIGRATIONS:
            await conn.execute(migration.read_text())
        await conn.execute(BOT_FIXTURE.read_text())

        # Удалённые заказы химчистки: №100 (без связки), №101 (связка без
        # сделки), №102 (сделка занята другим живым заказом), №104 (сделки
        # нет в CRM), №105 (сделка открыта), №106 (сделка закрыта).
        await conn.execute(
            """
            INSERT INTO public.deleted_orders (order_id, phone_digits, deleted_at) VALUES
                (100, '79601861067', $1), (101, '79601861067', $1),
                (102, '79601861067', $1), (104, '79601861067', $1),
                (105, '79601861067', $1), (106, '79601861067', $1)
            """,
            NOW,
        )
        # Заказ №103 — живой (не в deleted_orders): держит сделку №5002 занятой.
        await conn.execute(
            "INSERT INTO public.orders (id, phone_digits, amount_total) VALUES (103, '79601861067', 1000)"
        )

        # Уборка №200 (сделка закрыта, тот же путь для клининг-контура) и
        # уборка №210 (сделка занята ЖИВЫМ заказом химчистки №211 — проверка,
        # что «другой живой» ищется в ОБЕИХ таблицах связок, а не только в своей).
        await conn.execute(
            """
            INSERT INTO public.cleaning_orders (id, client_id, foreman_id, address,
                                                total_amount, happened_at, deleted_at) VALUES
                (200, 1, 1, 'Гагарина 1', 5000, $1, $1),
                (210, 1, 1, 'Гагарина 1', 5000, $1, $1)
            """,
            NOW,
        )
        await conn.execute(
            "INSERT INTO public.orders (id, phone_digits, amount_total) VALUES (211, '79601861067', 2000)"
        )
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_link(pool, order_id, *, table=db.LINKS_TABLE, status="new",
                     real_lead_id=None, primary_lead_id=None):
    store = PgCleaningLinkStore(pool) if table == db.CLEANING_LINKS_TABLE else PgLinkStore(pool)
    await store.create(order_id, "9601861067")
    fields = {"status": status}
    if real_lead_id is not None:
        fields["real_lead_id"] = real_lead_id
    if primary_lead_id is not None:
        fields["primary_lead_id"] = primary_lead_id
    await store.update(order_id, **fields)


class NotifyLog:
    """Собирает всё, что обработчик хотел бы сказать владельцу."""

    def __init__(self):
        self.outcomes = []

    async def __call__(self, outcome):
        self.outcomes.append(outcome)


async def _seen_outcome(pool, kind, order_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome FROM adminbot.order_deletions_seen WHERE kind = $1 AND order_id = $2",
            kind, order_id,
        )
    return None if row is None else row["outcome"]


async def test_no_link_marks_seen_and_stays_silent(pool):
    """Ветка 1 (решение 7): связки нет — молчим."""
    notify = NotifyLog()
    handler = DeletionHandler(own_pool=pool, amo=FakeAmo(), on_notify=notify, dry_run=False)

    handled = await handler.run()

    assert handled >= 1
    assert await _seen_outcome(pool, "order", 100) == "no_link"
    assert notify.outcomes == []


async def test_link_without_a_lead_is_cancelled_silently(pool):
    """Связка есть, но робот не дошёл до CRM (оба lead_id пусты) — молчим,
    но связку помечаем отменённой, чтобы не считалась вечно активной."""
    await _seed_link(pool, 101, status="waiting_owner")
    notify = NotifyLog()
    handler = DeletionHandler(own_pool=pool, amo=FakeAmo(), on_notify=notify, dry_run=False)

    await handler.run()

    assert await _seen_outcome(pool, "order", 101) == "no_lead"
    link = await PgLinkStore(pool).get(101)
    assert link.status == "cancelled"
    assert link.address_reminder_muted is True
    assert not any(o.record.order_id == 101 for o in notify.outcomes)


async def test_deal_held_by_another_live_order_is_left_untouched(pool):
    """Ветка 2 (решение 5): сделка занята связкой другого живого заказа —
    статус не трогаем, только примечание и письмо владельцу."""
    await _seed_link(pool, 102, real_lead_id=5002)
    await _seed_link(pool, 103, real_lead_id=5002)          # заказ №103 живой
    amo = FakeAmo()
    amo.add_lead(5002, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE)
    notify = NotifyLog()
    handler = DeletionHandler(own_pool=pool, amo=amo, on_notify=notify, dry_run=False)

    await handler.run()

    assert await _seen_outcome(pool, "order", 102) == "held_by_other"
    assert amo.calls_of("move_lead") == []                  # этап не тронут
    note = amo.calls_of("add_note")
    assert len(note) == 1 and note[0][0] == 5002
    assert "№103" in note[0][1]
    outcome = next(o for o in notify.outcomes if o.record.order_id == 102)
    assert outcome.outcome == "held_by_other"
    assert outcome.other_order_id == 103 and outcome.other_kind == "order"
    # своя сделка осталась при связке — решение 1, ничего не удаляем
    assert (await PgLinkStore(pool).get(102)).real_lead_id == 5002


async def test_deal_held_across_link_tables_is_detected_too(pool):
    """«Другой живой» ищется в ОБЕИХ таблицах связок: уборку удалили, а сделка
    занята живым заказом химчистки."""
    await _seed_link(pool, 210, table=db.CLEANING_LINKS_TABLE, real_lead_id=5021)
    await _seed_link(pool, 211, real_lead_id=5021)           # заказ химчистки, живой
    amo = FakeAmo()
    amo.add_lead(5021, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_DONE)
    notify = NotifyLog()
    handler = DeletionHandler(own_pool=pool, amo=amo, on_notify=notify, dry_run=False)

    await handler.run()

    outcome = next(o for o in notify.outcomes if o.record.kind == "cleaning" and o.record.order_id == 210)
    assert outcome.outcome == "held_by_other"
    assert outcome.other_kind == "order" and outcome.other_order_id == 211


async def test_deal_missing_in_crm_is_reported_and_link_dropped(pool):
    """Ветка 3 (решение 3, факт 11): ответ без id — сделки больше нет."""
    await _seed_link(pool, 104, real_lead_id=5004)
    amo = FakeAmo()
    amo.leads[5004] = {}                                    # «удалённая» сделка амо
    notify = NotifyLog()
    handler = DeletionHandler(own_pool=pool, amo=amo, on_notify=notify, dry_run=False)

    await handler.run()

    assert await _seen_outcome(pool, "order", 104) == "lead_gone"
    assert amo.calls_of("add_note") == []                   # писать некуда
    assert amo.calls_of("move_lead") == []
    link = await PgLinkStore(pool).get(104)
    assert link.status == "cancelled" and link.address_reminder_muted is True
    outcome = next(o for o in notify.outcomes if o.record.order_id == 104)
    assert outcome.outcome == "lead_gone" and outcome.lead_id == 5004


async def test_open_deal_is_left_untouched(pool):
    """Ветка 4 (решение 6): сделка открыта — статус не трогаем."""
    await _seed_link(pool, 105, real_lead_id=5005)
    amo = FakeAmo()
    amo.add_lead(5005, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CREATED)   # не финальный этап
    notify = NotifyLog()
    handler = DeletionHandler(own_pool=pool, amo=amo, on_notify=notify, dry_run=False)

    await handler.run()

    assert await _seen_outcome(pool, "order", 105) == "left_open"
    assert amo.calls_of("move_lead") == []
    assert len(amo.calls_of("add_note")) == 1
    assert amo.leads[5005]["status_id"] == ids.REAL_STAGE_CREATED         # этап не менялся
    outcome = next(o for o in notify.outcomes if o.record.order_id == 105)
    assert outcome.outcome == "left_open"


async def test_closed_deal_is_reopened_to_confirmed_stage(pool):
    """Ветка 5: сделка закрыта — возвращаем на «Заказ подтвержден»."""
    await _seed_link(pool, 106, real_lead_id=5006)
    amo = FakeAmo()
    amo.add_lead(5006, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS)
    notify = NotifyLog()
    handler = DeletionHandler(own_pool=pool, amo=amo, on_notify=notify, dry_run=False)

    await handler.run()

    assert await _seen_outcome(pool, "order", 106) == "reopened"
    moved = amo.calls_of("move_lead")
    assert moved == [(5006, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CONFIRMED)]
    assert amo.leads[5006]["status_id"] == ids.REAL_STAGE_CONFIRMED
    assert len(amo.calls_of("add_note")) == 1
    link = await PgLinkStore(pool).get(106)
    assert link.status == "cancelled" and link.address_reminder_muted is True
    assert link.real_lead_id == 5006                        # сделка при связке осталась (решение 1/2)
    outcome = next(o for o in notify.outcomes if o.record.order_id == 106)
    assert outcome.outcome == "reopened" and outcome.lead_id == 5006


async def test_rehearsal_leaves_no_traces_and_reprocesses_every_time(pool):
    """Правило проекта: репетиция не должна оставлять следов, которые заберут
    работу у последующего боя. Отметка о разборе необратима (второго шанса
    у записи нет), поэтому в dry_run обработчик решает и логирует действие
    (`amo_actions`, `dry_run=True`), но не ставит `order_deletions_seen`, не
    гасит связку и не пишет владельцу — иначе, включив функцию по-настоящему,
    владелец обнаружил бы уже «разобранные» удаления с закрытыми сделками."""
    await _seed_link(pool, 106, real_lead_id=5006)
    # dry_run=True на самом amo — как боевой сервис передаёт rehearsal_amo,
    # когда ORDER_DELETIONS_DRY_RUN=1 (main.py:_build_deletions): move_lead и
    # add_note не пишут по-настоящему, но intent.performed=False фиксируется
    # в журнале действий тем же способом, что и у движка.
    amo = FakeAmo(dry_run=True)
    amo.add_lead(5006, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS)
    notify = NotifyLog()
    handler = DeletionHandler(own_pool=pool, amo=amo, on_notify=notify, dry_run=True)

    first = await handler.run()

    assert first >= 1
    assert await _seen_outcome(pool, "order", 106) is None
    link = await PgLinkStore(pool).get(106)
    assert link.status != "cancelled" and link.real_lead_id == 5006
    assert notify.outcomes == []                          # владельцу в репетиции не пишем

    # решение при этом действительно принято и видно в журнале действий —
    # "движок" в репетиции тоже не молчит, просто не пишет в CRM по-настоящему
    async with pool.acquire() as conn:
        actions = await conn.fetch(
            "SELECT action, dry_run FROM adminbot.amo_actions WHERE order_id = 106 ORDER BY id")
    assert [(r["action"], r["dry_run"]) for r in actions] == [
        ("move_lead", True), ("add_note", True),
    ]

    # запись всё ещё пендинг — и на СЛЕДУЮЩЕМ проходе репетиция разберёт её
    # заново (это ожидаемо, а не сбой), а после переключения в бой разберёт
    # по-настоящему
    second = await handler.run()
    assert second == first
    assert await _seen_outcome(pool, "order", 106) is None
    pending = await fetch_pending_deletions(pool)
    assert any(r.kind == "order" and r.order_id == 106 for r in pending)


async def test_cleaning_deletion_uses_its_own_table_too(pool):
    """Тот же путь для уборки — своя таблица связок, тот же результат."""
    await _seed_link(pool, 200, table=db.CLEANING_LINKS_TABLE, real_lead_id=5200)
    amo = FakeAmo()
    amo.add_lead(5200, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS)
    notify = NotifyLog()
    handler = DeletionHandler(own_pool=pool, amo=amo, on_notify=notify, dry_run=False)

    await handler.run()

    assert await _seen_outcome(pool, "cleaning", 200) == "reopened"
    link = await PgCleaningLinkStore(pool).get(200)
    assert link.status == "cancelled"
    outcome = next(o for o in notify.outcomes if o.record.kind == "cleaning" and o.record.order_id == 200)
    assert outcome.outcome == "reopened"


async def test_already_seen_records_are_not_reprocessed(pool):
    """Отметка о разборе — не повод писать в амо ещё раз."""
    await _seed_link(pool, 106, real_lead_id=5006)
    amo = FakeAmo()
    amo.add_lead(5006, ids.PIPELINE_REALIZATION, ids.STATUS_SUCCESS)
    handler = DeletionHandler(own_pool=pool, amo=amo, dry_run=False)

    first = await handler.run()
    second = await handler.run()

    assert first >= 1
    assert second == 0
    assert amo.calls_of("move_lead") == [(5006, ids.PIPELINE_REALIZATION, ids.REAL_STAGE_CONFIRMED)]


async def test_one_broken_record_does_not_stop_the_rest(pool):
    """Сбой у амо на одной записи (№104, ходит в CRM) не должен мешать записи,
    которая туда вообще не ходит (№100 — ветка 1, связки нет)."""
    await _seed_link(pool, 104, real_lead_id=5004)
    amo = FakeAmo()
    amo.fail_on = "get_lead"
    handler = DeletionHandler(own_pool=pool, amo=amo, dry_run=False)

    await handler.run()

    assert await _seen_outcome(pool, "order", 100) == "no_link"    # разобрана, несмотря на сбой рядом
    assert await _seen_outcome(pool, "order", 104) is None         # сбой — не отмечена, попробуем снова

    amo.fail_on = None
    await handler.run()
    assert await _seen_outcome(pool, "order", 104) == "lead_gone"  # лид 5004 роботу не знаком
