-- Автозвонок по заявке с сайта (autocall). Применяется вручную:
--   psql "$ADMINBOT_DB_DSN" -f migrations/007_autocall.sql
--
-- Цепочка попыток: одна строка на сделку. PK = lead_id, поэтому повторный
-- проход наблюдателя не заведёт вторую цепочку по той же заявке.

CREATE TABLE IF NOT EXISTS adminbot.autocall_leads (
    lead_id          bigint PRIMARY KEY,
    phone10          text,
    status           text NOT NULL DEFAULT 'queued',
    -- queued|calling|done|no_contact|gave_up|error
    attempts_total   int NOT NULL DEFAULT 0,
    manager_failures int NOT NULL DEFAULT 0,
    client_failures  int NOT NULL DEFAULT 0,
    next_action_at   timestamptz,              -- когда пора действовать
    call_id          text,                     -- идентификатор звонка в АТС
    called_at        timestamptz,              -- когда отдали команду АТС
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_autocall_due ON adminbot.autocall_leads (status, next_action_at);

-- Журнал действий — тот же смысл, что gcal_actions.
CREATE TABLE IF NOT EXISTS adminbot.autocall_actions (
    id         bigserial PRIMARY KEY,
    lead_id    bigint NOT NULL,
    action     text NOT NULL,
    dry_run    boolean NOT NULL,
    payload    jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_autocall_actions_lead ON adminbot.autocall_actions (lead_id, created_at);

-- Курсор опроса амо: с какого created_at читать. В репетиции НЕ пишется
-- (память): боевой запуск должен начать со своего момента включения.
CREATE TABLE IF NOT EXISTS adminbot.autocall_cursor (
    id           smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    created_from timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
