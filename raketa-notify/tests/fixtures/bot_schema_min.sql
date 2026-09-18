-- Урезанная копия трёх таблиц рабочего бота (схема public) ТОЛЬКО для
-- тестов сторожа (задача 7). Колонки — из bot.py (ensure_service_heartbeat_schema
-- :717, ensure_amocrm_api_schema:990, notifications/outbox.py:156 — тот же
-- приём, что raketa-admin-bot/tests/fixtures/bot_schema_min.sql.
-- В бою эти таблицы уже существуют (бутстрап bot.py при старте), сторож их
-- только читает (права — migrations/002_watchdog_schema.sql).

DROP TABLE IF EXISTS public.notification_outbox;
DROP TABLE IF EXISTS public.amocrm_api_state;
DROP TABLE IF EXISTS public.service_heartbeats;

CREATE TABLE public.service_heartbeats (
    service_key       text        PRIMARY KEY,
    display_name      text        NOT NULL,
    status            text        NOT NULL DEFAULT 'starting',
    last_seen_at      timestamptz NOT NULL DEFAULT NOW(),
    last_ok_at        timestamptz,
    last_error        text,
    alert_open        boolean     NOT NULL DEFAULT FALSE,
    last_alerted_at   timestamptz,
    last_recovered_at timestamptz,
    created_at        timestamptz NOT NULL DEFAULT NOW(),
    updated_at        timestamptz NOT NULL DEFAULT NOW()
);

CREATE TABLE public.amocrm_api_state (
    stream            text PRIMARY KEY,
    cursor_created_at integer NOT NULL DEFAULT 0,
    updated_at        timestamptz NOT NULL DEFAULT NOW()
);

CREATE TABLE public.notification_outbox (
    id              bigserial PRIMARY KEY,
    event_key       text NOT NULL,
    recipient_kind  text NOT NULL,
    client_id       integer,
    template        text NOT NULL,
    payload         jsonb NOT NULL,
    locale          text NOT NULL DEFAULT 'ru-RU',
    status          text NOT NULL DEFAULT 'pending',
    scheduled_at    timestamptz NOT NULL DEFAULT NOW(),
    last_attempt_at timestamptz,
    attempts        integer NOT NULL DEFAULT 0,
    last_error      text,
    sent_at         timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW()
);
