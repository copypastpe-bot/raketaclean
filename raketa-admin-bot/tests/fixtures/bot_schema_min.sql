-- Урезанная копия таблиц рабочего бота (схема public) ТОЛЬКО для тестов.
-- Колонки взяты из боевого дампа `tgbot-v1/recon/data/bot_db_schema.sql`.
-- В бою эти таблицы уже существуют, и сервис их только читает.

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
    rating_score    smallint,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.order_masters (
    order_id        bigint NOT NULL,
    master_id       bigint NOT NULL,
    share_fraction  numeric(10,4) NOT NULL DEFAULT 1.0,
    created_at      timestamptz NOT NULL DEFAULT now()
);
