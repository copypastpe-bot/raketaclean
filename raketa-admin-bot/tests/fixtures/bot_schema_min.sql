-- Урезанная копия таблиц рабочего бота (схема public) ТОЛЬКО для тестов.
-- Колонки взяты из боевого дампа `tgbot-v1/recon/data/bot_db_schema.sql`.
-- В бою эти таблицы уже существуют, и сервис их только читает.

DROP TABLE IF EXISTS public.cleaning_order_payments;
DROP TABLE IF EXISTS public.cleaning_orders;
DROP TABLE IF EXISTS public.cleaning_foremen;
DROP TABLE IF EXISTS public.order_masters;
DROP TABLE IF EXISTS public.orders;
DROP TABLE IF EXISTS public.clients;
DROP TABLE IF EXISTS public.staff;

CREATE TABLE public.clients (
    id          bigint PRIMARY KEY,
    full_name   text,
    phone       text,
    phone_digits text,
    address     text,
    last_order_addr text
);

CREATE TABLE public.staff (
    id          bigint PRIMARY KEY,
    full_name   text,
    first_name  text,
    last_name   text,
    phone       text,
    role        text NOT NULL DEFAULT 'master',
    is_active   boolean NOT NULL DEFAULT true
);

CREATE TABLE public.orders (
    id              bigint PRIMARY KEY,
    phone           text,
    phone_digits    text,
    customer_name   text,
    client_id       bigint,
    master_id       bigint,
    amount_total    numeric(12,2) NOT NULL DEFAULT 0,
    amount_upsell   numeric(12,2) DEFAULT 0,
    upsale_amount   numeric(12,2) NOT NULL DEFAULT 0,
    payment_method  text,
    awaiting_wire_payment boolean NOT NULL DEFAULT false,
    rating_score    smallint,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.order_masters (
    order_id        bigint NOT NULL,
    master_id       bigint NOT NULL,
    share_fraction  numeric(10,4) NOT NULL DEFAULT 1.0,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Клининг-контур рабочего бота (с 2026-05-21): уборки живут в СВОИХ таблицах,
-- а не в public.orders. Колонки — из `tgbot-v1/cleaning/schema.py`.

CREATE TABLE public.cleaning_foremen (
    id          bigint PRIMARY KEY,
    tg_user_id  bigint,
    fn          text NOT NULL,
    ln          text,
    phone       text,
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.cleaning_orders (
    id             bigint PRIMARY KEY,
    client_id      bigint NOT NULL,
    foreman_id     bigint NOT NULL,
    address        text NOT NULL,
    total_amount   numeric(12,2) NOT NULL,
    bonuses_used   numeric(12,2) NOT NULL DEFAULT 0,
    bonuses_earned numeric(12,2) NOT NULL DEFAULT 0,
    client_op_id   text,
    happened_at    timestamptz NOT NULL DEFAULT now(),
    created_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz
);

CREATE TABLE public.cleaning_order_payments (
    id         bigint PRIMARY KEY,
    order_id   bigint NOT NULL,
    method     text NOT NULL,
    amount     numeric(12,2) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
