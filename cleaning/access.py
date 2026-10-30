"""Доступ бригадиров к клининг-сценариям."""

from __future__ import annotations

import asyncpg


async def is_cleaning_foreman(conn: asyncpg.Connection, tg_user_id: int) -> bool:
    row = await conn.fetchval(
        "SELECT 1 FROM cleaning_foremen WHERE tg_user_id = $1 AND is_active = true",
        tg_user_id,
    )
    return bool(row)


async def get_user_role(conn: asyncpg.Connection, tg_user_id: int) -> str | None:
    return await conn.fetchval(
        "SELECT role FROM staff WHERE tg_user_id = $1 AND is_active LIMIT 1",
        tg_user_id,
    )


async def has_permission(
    conn: asyncpg.Connection, tg_user_id: int, permission_name: str
) -> bool:
    role = await get_user_role(conn, tg_user_id)
    if role is None:
        return False
    if role == "superadmin":
        return True
    row = await conn.fetchval(
        """
        SELECT 1
        FROM role_permissions rp
        JOIN permissions p ON p.id = rp.permission_id
        WHERE rp.role = $1 AND p.name = $2
        LIMIT 1
        """,
        role,
        permission_name,
    )
    return bool(row)


async def is_cleaning_admin(conn: asyncpg.Connection, tg_user_id: int) -> bool:
    role = await get_user_role(conn, tg_user_id)
    return role in {"admin", "superadmin"}


async def can_create_cleaning_order(conn: asyncpg.Connection, tg_user_id: int) -> bool:
    return await has_permission(conn, tg_user_id, "cleaning_create_orders") and await is_cleaning_foreman(conn, tg_user_id)


async def get_foreman(conn: asyncpg.Connection, tg_user_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT id, tg_user_id, fn, ln, phone FROM cleaning_foremen "
        "WHERE tg_user_id = $1 AND is_active = true",
        tg_user_id,
    )


async def get_active_foreman_by_id(
    conn: asyncpg.Connection, foreman_id: int
) -> asyncpg.Record | None:
    """Запись Оли для уборки, которую проводит админ (CLEANING_OLYA_FOREMAN_ID)."""
    return await conn.fetchrow(
        "SELECT id, fn, ln FROM cleaning_foremen WHERE id = $1 AND is_active = true",
        foreman_id,
    )


async def admin_foreman_name(
    conn: asyncpg.Connection, tg_user_id: int, fallback: str | None
) -> tuple[str, str | None]:
    """Имя админа в уборке «Дима»: его запись бригадира, если уже есть; иначе
    карточка сотрудника (имя, фамилия, потом полное имя); иначе имя Telegram."""
    row = await conn.fetchrow(
        "SELECT fn, ln FROM cleaning_foremen WHERE tg_user_id = $1", tg_user_id
    )
    if row and (row["fn"] or "").strip():
        return row["fn"].strip(), (row["ln"] or "").strip() or None
    staff = await conn.fetchrow(
        "SELECT first_name, last_name, full_name FROM staff "
        "WHERE tg_user_id = $1 AND is_active LIMIT 1",
        tg_user_id,
    )
    if staff:
        first = (staff["first_name"] or "").strip()
        last = (staff["last_name"] or "").strip() or None
        if first:
            return first, last
        full = (staff["full_name"] or "").strip()
        if full:
            return full, None
        if last:
            return last, None
    return (fallback or "").strip() or "Администратор", None


async def ensure_admin_foreman(
    conn: asyncpg.Connection, tg_user_id: int, fallback_name: str | None
) -> asyncpg.Record:
    """Запись бригадира для админа (уборка «Дима»): есть — она, нет — заводится.

    Структура базы не меняется: админ становится строкой cleaning_foremen,
    чтобы у уборки был foreman_id (ТЗ 2026-10-30-admin-cleaning-order, задача 3).
    """
    row = await conn.fetchrow(
        "SELECT id, fn, ln FROM cleaning_foremen WHERE tg_user_id = $1", tg_user_id
    )
    if row:
        return row
    fn, ln = await admin_foreman_name(conn, tg_user_id, fallback_name)
    return await conn.fetchrow(
        """
        INSERT INTO cleaning_foremen (tg_user_id, fn, ln)
        VALUES ($1, $2, $3)
        ON CONFLICT (tg_user_id) DO UPDATE SET tg_user_id = EXCLUDED.tg_user_id
        RETURNING id, fn, ln
        """,
        tg_user_id,
        fn,
        ln,
    )


async def list_active_foremen(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT id, tg_user_id, fn, ln, phone FROM cleaning_foremen "
        "WHERE is_active = true ORDER BY id"
    )
