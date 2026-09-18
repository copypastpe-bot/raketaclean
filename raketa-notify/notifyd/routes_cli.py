"""Управление справочником notify.routes ключами скрипта (ТЗ, задача 4).

    python -m notifyd.routes_cli list
    python -m notifyd.routes_cli set-address <вид> <адрес>
    python -m notifyd.routes_cli set-level <вид> <red|yellow|grey>
    python -m notifyd.routes_cli set-tag <вид> [тег]
    python -m notifyd.routes_cli enable <вид>
    python -m notifyd.routes_cli disable <вид>

Команда в боте владельца — отдельная задача (9), не эта; здесь только ключи
скрипта, которые та команда впоследствии дёргает.

Подключается той же ролью, что и сама служба (полный доступ к схеме notify),
поэтому правка видна почтальону на следующем проходе цикла без перезапуска:
`notify.routes` читается заново на каждое событие (postman._resolve_route).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from notifyd import db
from notifyd.config import ADDRESSES, LEVELS, Settings


async def cmd_list(pool: Any) -> int:
    routes = await db.list_routes(pool)
    if not routes:
        print("Маршрутов нет.")
        return 0
    for route in routes:
        state = "включён" if route["enabled"] else "выключен"
        tag = f" #{route['tag']}" if route["tag"] else ""
        print(f"{route['kind']:<30} -> {route['address']:<12} "
              f"уровень={route['level']:<6} {state}{tag}")
    return 0


async def cmd_set_address(pool: Any, kind: str, address: str) -> int:
    if address not in ADDRESSES:
        print(f"Неизвестный адрес {address!r}. Допустимо: {', '.join(ADDRESSES)}",
              file=sys.stderr)
        return 2
    await db.upsert_route_address(pool, kind, address)
    print(f"{kind}: адрес -> {address}")
    return 0


async def cmd_set_level(pool: Any, kind: str, level: str) -> int:
    if level not in LEVELS:
        print(f"Неизвестный уровень {level!r}. Допустимо: {', '.join(LEVELS)}",
              file=sys.stderr)
        return 2
    if not await db.update_route_level(pool, kind, level):
        print(_no_route_message(kind), file=sys.stderr)
        return 1
    print(f"{kind}: уровень -> {level}")
    return 0


async def cmd_set_tag(pool: Any, kind: str, tag: str) -> int:
    clean = tag.strip() or None
    if not await db.update_route_tag(pool, kind, clean):
        print(_no_route_message(kind), file=sys.stderr)
        return 1
    print(f"{kind}: тег -> {clean or '(снят)'}")
    return 0


async def cmd_set_enabled(pool: Any, kind: str, enabled: bool) -> int:
    if not await db.set_route_enabled(pool, kind, enabled):
        print(_no_route_message(kind), file=sys.stderr)
        return 1
    print(f"{kind}: {'включён' if enabled else 'выключен'}")
    return 0


def _no_route_message(kind: str) -> str:
    return (f"Маршрута для {kind!r} нет. Сначала задайте адрес: "
            f"set-address {kind} <адрес>")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="routes_cli", description="Справочник маршрутов notify.routes")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="показать все маршруты")

    p = sub.add_parser("set-address", help="завести или поменять адрес маршрута")
    p.add_argument("kind")
    p.add_argument("address")

    p = sub.add_parser("set-level", help="поменять уровень маршрута")
    p.add_argument("kind")
    p.add_argument("level")

    p = sub.add_parser("set-tag", help="поменять тег маршрута (без тега — снять)")
    p.add_argument("kind")
    p.add_argument("tag", nargs="?", default="")

    p = sub.add_parser("enable", help="включить маршрут")
    p.add_argument("kind")

    p = sub.add_parser("disable", help="выключить маршрут")
    p.add_argument("kind")

    return parser


async def dispatch(pool: Any, args: argparse.Namespace) -> int:
    if args.command == "list":
        return await cmd_list(pool)
    if args.command == "set-address":
        return await cmd_set_address(pool, args.kind, args.address)
    if args.command == "set-level":
        return await cmd_set_level(pool, args.kind, args.level)
    if args.command == "set-tag":
        return await cmd_set_tag(pool, args.kind, args.tag)
    if args.command == "enable":
        return await cmd_set_enabled(pool, args.kind, True)
    if args.command == "disable":
        return await cmd_set_enabled(pool, args.kind, False)
    raise AssertionError(f"неизвестная команда: {args.command}")  # argparse не пропустит


async def _main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    pool = await db.create_pool(settings.db_dsn)
    try:
        return await dispatch(pool, args)
    finally:
        await pool.close()


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
