"""Хранилище ковров в памяти: заказы письма для итога (ТЗ 2026-10-12)."""

from adminbot.carpets.store import MemoryCarpetStore


async def test_memory_store_tracks_letter_report_once():
    store = MemoryCarpetStore()
    await store.track_letter("1", "отчёт", [10, 11], [11])
    await store.track_letter("1", "отчёт", [99], [])        # репетиция разбирает письмо каждый проход

    assert await store.letters_to_report() == [
        {"uid": "1", "subject": "отчёт", "partner_ids": [10, 11], "refused_ids": [11]}]

    await store.mark_letter_reported("1")
    await store.track_letter("1", "отчёт", [10, 11], [11])  # после итога письмо не воскресает
    assert await store.letters_to_report() == []


async def test_memory_store_links_by_ids():
    store = MemoryCarpetStore()
    await store.create(10, "9601945325")
    await store.create(11, "9601945325")

    links = await store.links_by_ids([11, 12])

    assert [link.partner_id for link in links] == [11]
