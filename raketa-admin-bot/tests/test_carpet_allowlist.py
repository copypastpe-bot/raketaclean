"""Защита боевого прогона из файла: белый список сделок.

Прогон из файла делают руками, когда обычный путь через почту не годится, —
и ошибиться файлом здесь легко: 15.09.2026 отчёт за два года дал 30 лишних
сделок за три минуты. Поэтому боевой прогон из файла разрешён только со списком
сделок, и клиент амо проверяет каждый вызов сам, а не полагается на движок.
"""

import pytest

from adminbot.amo import ids
from scripts.run_carpets import AllowListAmo, parse_allow


def client(allowed=(31587353,)) -> AllowListAmo:
    # dry_run=True: до сети дело не доходит, проверяем именно запрет.
    return AllowListAmo(base_url="https://example.amocrm.ru", token="секрет",
                        dry_run=True, allowed=allowed)


def test_allow_list_is_read_from_a_comma_separated_string():
    assert parse_allow("31587353, 31613297") == {31587353, 31613297}
    assert parse_allow("") == set()


def test_allow_list_rejects_a_non_numeric_token_with_a_clear_message():
    """Опечатка в --allow — понятная ошибка, а не падение стеком (задача 4, 16.09)."""
    with pytest.raises(ValueError, match="abc"):
        parse_allow("31587353,abc")


async def test_creating_anything_is_forbidden():
    guarded = client()

    for call in (
        guarded.create_lead(name="Ковры", pipeline_id=ids.PIPELINE_CARPETS,
                            status_id=ids.CARPET_STAGE_DELIVERED),
        guarded.create_contact(name="Ирина", phone="+79601861067"),
        guarded.update_contact(555, name="Ирина"),
        guarded.complete_task(777),
    ):
        with pytest.raises(RuntimeError, match="прогон из файла"):
            await call


async def test_writing_to_a_lead_outside_the_list_is_forbidden():
    guarded = client()

    with pytest.raises(RuntimeError, match="нет в белом списке"):
        await guarded.update_lead(41463832, price=3995)
    with pytest.raises(RuntimeError, match="нет в белом списке"):
        await guarded.add_note(41463832, "комментарий")
    with pytest.raises(RuntimeError, match="нет в белом списке"):
        await guarded.move_lead(41463832, ids.PIPELINE_CARPETS,
                                ids.CARPET_STAGE_DELIVERED)


async def test_lead_from_the_list_is_written_as_usual():
    guarded = client()

    updated = await guarded.update_lead(31587353, price=3995)
    noted = await guarded.add_note(31587353, "комментарий")
    moved = await guarded.move_lead(31587353, ids.PIPELINE_CARPETS,
                                    ids.CARPET_STAGE_DELIVERED)

    assert (updated.action, updated.entity_id) == ("update_lead", 31587353)
    assert noted.action == "add_note"
    assert moved.payload["pipeline_id"] == ids.PIPELINE_CARPETS


async def test_lead_from_the_list_cannot_leave_the_carpets_pipeline():
    """Сделку из списка двигаем только в воронке ковров — чужая воронка не наше дело."""
    guarded = client()

    with pytest.raises(RuntimeError, match="воронке ковров"):
        await guarded.move_lead(31587353, ids.PIPELINE_PRIMARY, ids.STATUS_SUCCESS)
