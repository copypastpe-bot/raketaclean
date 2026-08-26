"""Хвост непроведённых заказов: сначала показать план, потом провести.

Предпросмотр обязан быть безопасным: он читает amoCRM и считает, что робот
сделал бы, но не меняет ни CRM, ни собственное состояние робота. Иначе кнопка
«Поехали» теряет смысл — работа окажется уже сделанной.
"""

from datetime import datetime
from decimal import Decimal

from adminbot.amo.fields import MOSCOW_TZ
from adminbot.models import Order
from adminbot.sync.backlog import BacklogRunner
from adminbot.sync.engine import Engine
from adminbot.sync.specialists import SpecialistIndex
from adminbot.sync.store import MemoryLinkStore
from tests.fakes import FakeAmo, FakeStore
from tests.test_engine import SPECIALISTS, open_realization_lead

ORDER_MOMENT = datetime(2026, 8, 24, 17, 53, tzinfo=MOSCOW_TZ)


def make_order(order_id):
    return Order(order_id=order_id, phone10="9601861067", created_at=ORDER_MOMENT,
                 amount_total=Decimal("5950"), client_name="Ирина",
                 masters=[("Дмитрий Козлов", "79306858534")])


def make_runner(orders, rehearsal_amo, live_amo, live_store):
    def rehearsal_engine(store):
        return Engine(amo=rehearsal_amo, store=store, specialists=SPECIALISTS, dry_run=True)

    async def fetch():
        return list(orders)

    return BacklogRunner(
        fetch_orders=fetch,
        rehearsal_engine=rehearsal_engine,
        live_engine=Engine(amo=live_amo, store=live_store, specialists=SPECIALISTS,
                           dry_run=False),
    )


async def test_preview_shows_the_plan_without_changing_anything():
    rehearsal_amo = FakeAmo(dry_run=True)
    open_realization_lead(rehearsal_amo, 41400001)
    live_amo, live_store = FakeAmo(), FakeStore()

    plan = await make_runner([make_order(582)], rehearsal_amo, live_amo, live_store).preview()

    assert [item.order_id for item in plan] == [582]
    assert plan[0].path == "A"
    assert ("update_lead", 41400001) in plan[0].actions
    assert live_store.links == {}                  # состояние робота не тронуто
    assert live_amo.calls == []                    # боевой клиент не вызывался


async def test_preview_titles_are_readable_and_show_the_phone():
    rehearsal_amo = FakeAmo(dry_run=True)
    open_realization_lead(rehearsal_amo, 41400001)

    plan = await make_runner([make_order(582)], rehearsal_amo, FakeAmo(), FakeStore()).preview()

    assert "Заказ №582" in plan[0].title
    assert "Ирина" in plan[0].title
    assert "5 950" in plan[0].title
    assert "+79601861067" in plan[0].title       # владельцу — номер целиком
    assert "24.08.2026" in plan[0].title        # и дата заказа с годом


async def test_go_processes_the_same_orders_for_real():
    live_amo, live_store = FakeAmo(), FakeStore()
    open_realization_lead(live_amo, 41400001)
    runner = make_runner([make_order(582), make_order(583)],
                         FakeAmo(dry_run=True), live_amo, live_store)

    done = await runner.run_live()

    assert [item.order_id for item in done] == [582, 583]
    assert live_store.links[582].status == "done"
    assert live_amo.calls_of("move_lead")           # сделка действительно переведена


async def test_preview_keeps_its_own_scratch_state():
    """Два предпросмотра подряд дают одинаковый результат: черновик не копится."""
    rehearsal_amo = FakeAmo(dry_run=True)
    open_realization_lead(rehearsal_amo, 41400001)
    runner = make_runner([make_order(582)], rehearsal_amo, FakeAmo(), FakeStore())

    first = await runner.preview()
    second = await runner.preview()

    assert first[0].actions == second[0].actions


async def test_empty_backlog_gives_an_empty_plan():
    runner = make_runner([], FakeAmo(dry_run=True), FakeAmo(), FakeStore())

    assert await runner.preview() == []
    assert await runner.run_live() == []


async def test_memory_store_is_used_for_the_rehearsal():
    """Черновик предпросмотра живёт в памяти и в базу не попадает."""
    rehearsal_amo = FakeAmo(dry_run=True)
    open_realization_lead(rehearsal_amo, 41400001)
    runner = make_runner([make_order(582)], rehearsal_amo, FakeAmo(), FakeStore())

    await runner.preview()

    assert isinstance(runner.last_rehearsal_store, MemoryLinkStore)
