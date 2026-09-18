"""Почтальон: доставка, повтор, протухание, выключенный маршрут, неизвестный
вид события, репетиция без следов, двойной цикл без двойной отправки.

Проверки задачи 3 и задачи 4 ТЗ 2026-09-18. Инциденты (задача 8) сюда не входят.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from notifyd import routes_cli
from notifyd.postman import BACKOFF_SEC, Postman, Target

from conftest import NOW, FakeSender, fetch_outbox, insert_outbox


def _postman(pool, targets, *, enabled=True, dry_run=False, **kwargs) -> Postman:
    return Postman(pool=pool, targets=targets, enabled=enabled, dry_run=dry_run,
                   now=lambda: NOW, **kwargs)


# --------------------------------------------------------------------------
# Задача 3: выключатель, репетиция, доставка/повтор/протухание, двойной цикл
# --------------------------------------------------------------------------

async def test_disabled_service_never_touches_outbox(pool):
    """Выключатель в положении «выключено» — служба поднимается и молчит."""
    outbox_id = await insert_outbox(pool, kind="test.disabled")
    sender = FakeSender()

    calls = {"n": 0}
    stop = asyncio.Event()

    async def fake_sleep(_seconds: float) -> None:
        # Управляемый «сон»: считаем заходы и останавливаем цикл сами —
        # настоящий asyncio.sleep(60) в тесте не нужен и повесил бы его.
        calls["n"] += 1
        stop.set()

    postman = _postman(pool, {"ops_feed": Target(sender, chat_id=1)}, enabled=False,
                       sleep=fake_sleep)
    await postman.run_forever(stop)

    assert calls["n"] == 1          # цикл заглянул поспать ровно раз и вышел
    assert sender.sent == []
    row = await fetch_outbox(pool, outbox_id)
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["sent_at"] is None
    assert row["error"] is None


async def test_rehearsal_leaves_outbox_untouched(pool):
    """Репетиция логирует, что бы отправила, и не оставляет следов в ящике."""
    outbox_id = await insert_outbox(pool, kind="test.rehearsal")
    await routes_cli.cmd_set_address(pool, "test.rehearsal", "ops_feed")

    before = await fetch_outbox(pool, outbox_id)

    sender = FakeSender()
    postman = _postman(pool, {"ops_feed": Target(sender, chat_id=1)}, dry_run=True)
    delivered = await postman.rehearse_once()

    after = await fetch_outbox(pool, outbox_id)
    assert delivered == 1
    assert sender.sent == []                 # ни одного настоящего обращения к Telegram
    assert after == before                    # строка не тронута совсем, включая next_try_at


async def test_rehearsal_does_not_claim_from_combat(pool):
    """Репетиция не должна отбирать работу у боевого прохода (комментарий миграции)."""
    outbox_id = await insert_outbox(pool, kind="test.rehearsal2")
    await routes_cli.cmd_set_address(pool, "test.rehearsal2", "ops_feed")

    sender_dry = FakeSender()
    dry_postman = _postman(pool, {"ops_feed": Target(sender_dry, chat_id=1)}, dry_run=True)
    await dry_postman.rehearse_once()

    sender_real = FakeSender()
    real_postman = _postman(pool, {"ops_feed": Target(sender_real, chat_id=1)}, dry_run=False)
    delivered = await real_postman.deliver_due()

    assert delivered == 1
    assert sender_real.sent[0][1] == "текст события"
    row = await fetch_outbox(pool, outbox_id)
    assert row["status"] == "sent"


async def test_successful_delivery_marks_sent(pool):
    outbox_id = await insert_outbox(pool, kind="test.ok", text="привет")
    await routes_cli.cmd_set_address(pool, "test.ok", "ops_feed")

    sender = FakeSender()
    postman = _postman(pool, {"ops_feed": Target(sender, chat_id=777)})
    delivered = await postman.deliver_due()

    assert delivered == 1
    assert sender.sent == [(777, "привет", None)]
    row = await fetch_outbox(pool, outbox_id)
    assert row["status"] == "sent"
    assert row["message_id"] is not None
    assert row["sent_at"] is not None


async def test_failed_delivery_backs_off_and_keeps_pending(pool):
    outbox_id = await insert_outbox(pool, kind="test.fail")
    await routes_cli.cmd_set_address(pool, "test.fail", "ops_feed")

    sender = FakeSender(always_fail=True)
    postman = _postman(pool, {"ops_feed": Target(sender, chat_id=1)})
    delivered = await postman.deliver_due()

    assert delivered == 0
    row = await fetch_outbox(pool, outbox_id)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["sent_at"] is None
    assert "недоступна" in row["error"]
    assert row["next_try_at"] == NOW + timedelta(seconds=BACKOFF_SEC[0])


async def test_expired_event_is_dropped_without_sending(pool):
    outbox_id = await insert_outbox(pool, kind="test.expired",
                                    expires_at=NOW - timedelta(minutes=1))
    await routes_cli.cmd_set_address(pool, "test.expired", "ops_feed")

    sender = FakeSender()
    postman = _postman(pool, {"ops_feed": Target(sender, chat_id=1)})
    delivered = await postman.deliver_due()

    assert delivered == 0
    assert sender.sent == []
    row = await fetch_outbox(pool, outbox_id)
    assert row["status"] == "dropped"
    assert row["error"] == "протухло"
    assert row["sent_at"] is None


async def test_disabled_route_drops_event_without_sending(pool):
    """Задача 4: выключенный маршрут — событие не отправляется и гасится."""
    outbox_id = await insert_outbox(pool, kind="test.route-off")
    await routes_cli.cmd_set_address(pool, "test.route-off", "ops_feed")
    await routes_cli.cmd_set_enabled(pool, "test.route-off", False)

    sender = FakeSender()
    postman = _postman(pool, {"ops_feed": Target(sender, chat_id=1)})
    delivered = await postman.deliver_due()

    assert delivered == 0
    assert sender.sent == []
    row = await fetch_outbox(pool, outbox_id)
    assert row["status"] == "dropped"
    assert "выключен" in row["error"]


async def test_unknown_kind_goes_to_tech_journal_with_tag(pool, caplog):
    """Задача 4: вид события без маршрута не теряется — уходит в tech_journal
    с тегом #неизвестный-вид и строкой в журнал службы."""
    outbox_id = await insert_outbox(pool, kind="test.unknown-kind", text="что-то новое")
    sender = FakeSender()
    postman = _postman(pool, {"tech_journal": Target(sender, chat_id=42)})

    caplog.set_level("WARNING")
    delivered = await postman.deliver_due()

    assert delivered == 1
    assert sender.sent == [(42, "что-то новое\n\n#неизвестный-вид", None)]
    row = await fetch_outbox(pool, outbox_id)
    assert row["status"] == "sent"
    assert any("test.unknown-kind" in record.message for record in caplog.records)


async def test_route_address_change_takes_effect_without_restart(pool):
    """Задача 4: изменение адреса вступает в силу без перезапуска службы —
    один и тот же объект Postman должен увидеть новый адрес на следующем проходе."""
    await routes_cli.cmd_set_address(pool, "test.moves", "ops_feed")

    ops_sender = FakeSender()
    admin_sender = FakeSender()
    postman = _postman(pool, {
        "ops_feed": Target(ops_sender, chat_id=1),
        "my_admin": Target(admin_sender, chat_id=2),
    })

    first_id = await insert_outbox(pool, kind="test.moves", text="первое")
    assert await postman.deliver_due() == 1
    assert ops_sender.sent == [(1, "первое", None)]
    assert admin_sender.sent == []

    # Меняем маршрут той же командой, которой владелец/координатор будет
    # пользоваться в бою (routes_cli) — без пересборки Postman.
    await routes_cli.cmd_set_address(pool, "test.moves", "my_admin")

    second_id = await insert_outbox(pool, kind="test.moves", text="второе")
    assert await postman.deliver_due() == 1
    assert admin_sender.sent == [(2, "второе", None)]
    assert ops_sender.sent == [(1, "первое", None)]   # первый адрес больше не тронут

    assert (await fetch_outbox(pool, first_id))["status"] == "sent"
    assert (await fetch_outbox(pool, second_id))["status"] == "sent"


async def test_two_concurrent_postmen_do_not_double_send(pool):
    """Задача 3: два одновременно работающих цикла — сообщение уходит один раз."""
    await routes_cli.cmd_set_address(pool, "test.concurrent", "ops_feed")

    total = 20
    ids = [await insert_outbox(pool, kind="test.concurrent", text=f"событие {i}")
           for i in range(total)]

    sender1, sender2 = FakeSender(), FakeSender()
    postman1 = _postman(pool, {"ops_feed": Target(sender1, chat_id=1)}, batch_limit=1)
    postman2 = _postman(pool, {"ops_feed": Target(sender2, chat_id=1)}, batch_limit=1)

    async def drain(postman: Postman, rounds: int) -> int:
        delivered = 0
        for _ in range(rounds):
            delivered += await postman.deliver_due()
        return delivered

    delivered1, delivered2 = await asyncio.gather(drain(postman1, total),
                                                   drain(postman2, total))

    assert delivered1 + delivered2 == total

    texts_1 = {text for _, text, _ in sender1.sent}
    texts_2 = {text for _, text, _ in sender2.sent}
    assert not (texts_1 & texts_2), "одно и то же событие отправлено обоими циклами"
    assert len(texts_1) + len(texts_2) == total

    for outbox_id in ids:
        row = await fetch_outbox(pool, outbox_id)
        assert row["status"] == "sent"
