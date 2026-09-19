"""Управление справочником notify.routes ключами скрипта (ТЗ, задача 4)."""

from __future__ import annotations

from notifyd import db, routes_cli
from notifyd.watchdog import KEY_DATABASE


async def test_set_address_creates_route_with_defaults(pool):
    code = await routes_cli.cmd_set_address(pool, "kind.new", "ops_feed")
    assert code == 0

    route = await db.get_route(pool, "kind.new")
    assert route["address"] == "ops_feed"
    assert route["level"] == "grey"     # значение по умолчанию из миграции
    assert route["enabled"] is True      # значение по умолчанию из миграции
    assert route["tag"] is None


async def test_set_address_updates_existing_route(pool):
    await routes_cli.cmd_set_address(pool, "kind.move", "ops_feed")
    code = await routes_cli.cmd_set_address(pool, "kind.move", "my_admin")
    assert code == 0

    route = await db.get_route(pool, "kind.move")
    assert route["address"] == "my_admin"


async def test_set_address_rejects_unknown_address(pool):
    code = await routes_cli.cmd_set_address(pool, "kind.bad", "куда-то")
    assert code == 2
    assert await db.get_route(pool, "kind.bad") is None   # ничего не завелось


async def test_set_level_requires_existing_route(pool):
    code = await routes_cli.cmd_set_level(pool, "kind.absent", "red")
    assert code == 1
    assert await db.get_route(pool, "kind.absent") is None


async def test_set_level_updates_existing_route(pool):
    await routes_cli.cmd_set_address(pool, "kind.level", "ops_feed")
    code = await routes_cli.cmd_set_level(pool, "kind.level", "red")
    assert code == 0
    assert (await db.get_route(pool, "kind.level"))["level"] == "red"


async def test_set_level_rejects_unknown_level(pool):
    await routes_cli.cmd_set_address(pool, "kind.badlevel", "ops_feed")
    code = await routes_cli.cmd_set_level(pool, "kind.badlevel", "синий")
    assert code == 2
    assert (await db.get_route(pool, "kind.badlevel"))["level"] == "grey"  # не тронут


async def test_set_tag_sets_and_clears(pool):
    await routes_cli.cmd_set_address(pool, "kind.tag", "tech_journal")
    await routes_cli.cmd_set_tag(pool, "kind.tag", "мой-тег")
    assert (await db.get_route(pool, "kind.tag"))["tag"] == "мой-тег"

    await routes_cli.cmd_set_tag(pool, "kind.tag", "")   # пустая строка снимает тег
    assert (await db.get_route(pool, "kind.tag"))["tag"] is None


async def test_enable_disable_toggle(pool):
    await routes_cli.cmd_set_address(pool, "kind.toggle", "ops_feed")

    await routes_cli.cmd_set_enabled(pool, "kind.toggle", False)
    assert (await db.get_route(pool, "kind.toggle"))["enabled"] is False

    await routes_cli.cmd_set_enabled(pool, "kind.toggle", True)
    assert (await db.get_route(pool, "kind.toggle"))["enabled"] is True


async def test_list_shows_all_routes_sorted_by_kind(pool, capsys):
    await routes_cli.cmd_set_address(pool, "kind.b", "ops_feed")
    await routes_cli.cmd_set_address(pool, "kind.a", "tech_journal")
    capsys.readouterr()   # смыть подтверждения set-address — интересен только вывод list

    code = await routes_cli.cmd_list(pool)
    assert code == 0
    out = capsys.readouterr().out
    assert out.index("kind.a") < out.index("kind.b")   # сортировка по kind


async def test_cli_dispatch_end_to_end_through_argparse(pool, capsys):
    """Хотя бы одна проверка через настоящий разбор аргументов, а не напрямую cmd_*."""
    parser = routes_cli.build_parser()

    args = parser.parse_args(["set-address", "kind.cli", "ops_feed"])
    assert await routes_cli.dispatch(pool, args) == 0

    args = parser.parse_args(["enable", "kind.cli"])
    assert await routes_cli.dispatch(pool, args) == 0

    args = parser.parse_args(["list"])
    assert await routes_cli.dispatch(pool, args) == 0
    assert "kind.cli" in capsys.readouterr().out


async def test_set_level_refuses_grey_for_watchdog_check(pool):
    """Серый на поломке бессмыслен: инцидент серым быть не может (ограничение
    в миграции 001), а «замолчать совсем» делает выключатель маршрута."""
    await routes_cli.cmd_set_address(pool, KEY_DATABASE, "my_admin")

    code = await routes_cli.cmd_set_level(pool, KEY_DATABASE, "grey")

    assert code == 2
    # и уровень остался тем, что положен по табличке владельца
    assert (await db.get_route(pool, KEY_DATABASE))["level"] == "red"


async def test_set_level_allows_grey_for_ordinary_kind(pool):
    """Обычный вид события серым быть обязан — технический журнал именно такой."""
    await routes_cli.cmd_set_address(pool, "notify.journal", "tech_journal")

    code = await routes_cli.cmd_set_level(pool, "notify.journal", "grey")

    assert code == 0
    assert (await db.get_route(pool, "notify.journal"))["level"] == "grey"
