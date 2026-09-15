"""Загрузка архивного файла партнёра в память робота.

Строки из архива робот не проводил — он просто знает, что они сделаны.
Проверяем два свойства, ради которых загрузка и переписана: строка появляется
сразу готовой (одна операция, промежуточного `new` не бывает), а строка,
оставшаяся от оборванной загрузки, при повторном запуске доводится до конца,
а не считается «роботу уже известной».
"""

from adminbot.carpets.report import CarpetRow
from adminbot.carpets.store import MemoryCarpetStore
from adminbot.db import CARPET_REMEMBERED_PATH
from scripts.remember_carpets import is_unfinished, remember_rows


def row(partner_id: int, phone10: str = "9161234567") -> CarpetRow:
    return CarpetRow(partner_id=partner_id, phone10=phone10)


async def test_new_row_is_written_ready_in_one_step():
    store = MemoryCarpetStore()

    counts = await remember_rows([row(101)], store=store, live=True,
                                 source_file="архив.xlsx")

    link = await store.get(101)
    assert (link.status, link.path) == ("done", CARPET_REMEMBERED_PATH)
    assert link.source_file == "архив.xlsx"
    assert counts == (0, 1, 0)


async def test_preview_changes_nothing():
    store = MemoryCarpetStore()

    counts = await remember_rows([row(101)], store=store, live=False)

    assert await store.get(101) is None
    assert counts.fresh == 1


async def test_row_the_robot_really_worked_is_left_alone():
    store = MemoryCarpetStore()
    await store.create(101, "9161234567")
    await store.update(101, status="done", path="primary", lead_id=31587353)

    counts = await remember_rows([row(101)], store=store, live=True)

    link = await store.get(101)
    assert (link.path, link.lead_id) == ("primary", 31587353)
    assert counts == (1, 0, 0)


async def test_unfinished_row_from_a_broken_run_is_repaired():
    """Строка осталась в `new` после обрыва: движок принял бы её за работу."""
    store = MemoryCarpetStore()
    await store.create(101, "9161234567")

    counts = await remember_rows([row(101)], store=store, live=True)

    link = await store.get(101)
    assert (link.status, link.path) == ("done", CARPET_REMEMBERED_PATH)
    assert counts == (0, 0, 1)


async def test_unfinished_row_is_only_counted_in_preview():
    store = MemoryCarpetStore()
    await store.create(101, "9161234567")

    counts = await remember_rows([row(101)], store=store, live=False)

    assert (await store.get(101)).status == "new"
    assert counts.fixed == 1


async def test_row_in_the_middle_of_real_work_is_not_repaired():
    """У строки в работе есть отметки в чек-листе — её трогать нельзя."""
    store = MemoryCarpetStore()
    await store.create(101, "9161234567")
    await store.update(101, status="waiting_salesbot")
    await store.mark_step(101, "matched")

    counts = await remember_rows([row(101)], store=store, live=True)

    link = await store.get(101)
    assert link.status == "waiting_salesbot"
    assert counts == (1, 0, 0)
    assert not is_unfinished(link)
