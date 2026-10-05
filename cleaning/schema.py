"""Idempotent bootstrap of cleaning tables (mirrors 0006_cleaning.sql, 0010_cleaning_orders_comment.sql,
0016_cleaning_orders_rating.sql, 0017_cleaning_cashbook_cash_holder.sql)."""

from __future__ import annotations

import asyncpg


async def ensure_cleaning_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cleaning_foremen (
            id         serial PRIMARY KEY,
            tg_user_id bigint UNIQUE,
            fn         text NOT NULL,
            ln         text,
            phone      text,
            is_active  boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT NOW()
        );
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cleaning_orders (
            id             serial PRIMARY KEY,
            client_id      integer NOT NULL REFERENCES clients(id),
            foreman_id     integer NOT NULL REFERENCES cleaning_foremen(id),
            address        text,
            comment        text,
            total_amount   numeric(12,2) NOT NULL,
            bonuses_used   numeric(12,2) NOT NULL DEFAULT 0,
            bonuses_earned numeric(12,2) NOT NULL DEFAULT 0,
            client_op_id   text UNIQUE,
            happened_at    timestamptz NOT NULL DEFAULT NOW(),
            created_at     timestamptz NOT NULL DEFAULT NOW(),
            deleted_at     timestamptz
        );
        """
    )
    # Оценка по уборкам — как у orders (0016, ТЗ 2026-09-30 «оценка по уборкам»).
    await conn.execute(
        """
        ALTER TABLE cleaning_orders
        ADD COLUMN IF NOT EXISTS rating_score smallint,
        ADD COLUMN IF NOT EXISTS rating_comment text,
        ADD COLUMN IF NOT EXISTS rating_requested_at timestamptz,
        ADD COLUMN IF NOT EXISTS rating_replied_at timestamptz;
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cleaning_orders_client   ON cleaning_orders(client_id);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cleaning_orders_happened ON cleaning_orders(happened_at);"
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cleaning_order_payments (
            id         serial PRIMARY KEY,
            order_id   integer NOT NULL REFERENCES cleaning_orders(id) ON DELETE CASCADE,
            method     text NOT NULL,
            amount     numeric(12,2) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT NOW()
        );
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cleaning_op_order ON cleaning_order_payments(order_id);"
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cleaning_cashbook (
            id          serial PRIMARY KEY,
            kind        text NOT NULL,
            method      text NOT NULL,
            amount      numeric(12,2) NOT NULL,
            comment     text,
            order_id    integer REFERENCES cleaning_orders(id),
            happened_at timestamptz NOT NULL DEFAULT NOW(),
            created_at  timestamptz NOT NULL DEFAULT NOW(),
            deleted_at  timestamptz
        );
        """
    )
    # Реестр денег Оли — «чьи деньги» у строки кассы (0017, ТЗ 2026-10-05).
    await conn.execute(
        """
        ALTER TABLE cleaning_cashbook
        ADD COLUMN IF NOT EXISTS cash_holder text;
        """
    )
    await conn.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'cleaning_cashbook_cash_holder_check'
                  AND conrelid = 'cleaning_cashbook'::regclass
            ) THEN
                ALTER TABLE cleaning_cashbook
                    ADD CONSTRAINT cleaning_cashbook_cash_holder_check
                    CHECK (cash_holder IN ('olya', 'dima'));
            END IF;
        END
        $$;
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cleaning_cb_kind_method ON cleaning_cashbook(kind, method);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cleaning_cb_order       ON cleaning_cashbook(order_id);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cleaning_cb_happened    ON cleaning_cashbook(happened_at);"
    )
