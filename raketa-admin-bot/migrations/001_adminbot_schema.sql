-- Собственное хранилище админ-бота. Применяется вручную:
--   psql "$ADMINBOT_DB_DSN" -f migrations/001_adminbot_schema.sql
-- В схему public БД рабочего бота этот сервис НИКОГДА не пишет.

CREATE SCHEMA IF NOT EXISTS adminbot;

-- привязка заказа бота к сделкам амо + чек-лист шагов (идемпотентность, дизайн §5.4)
CREATE TABLE IF NOT EXISTS adminbot.amo_links (
    order_id        bigint PRIMARY KEY,            -- orders.id рабочего бота (не FK: чужая схема)
    phone10         text NOT NULL,
    status          text NOT NULL DEFAULT 'new',   -- new|in_progress|waiting_owner|waiting_salesbot|done|error
    path            text,                          -- A|B|C|D (путь из дизайна §4)
    primary_lead_id bigint,
    real_lead_id    bigint,
    checklist       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {"step_name": "2026-08-25T10:00:00Z", ...}
    question_msg_id bigint,                        -- id карточки-вопроса в TG, если путь Г
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_amo_links_status ON adminbot.amo_links (status);

-- журнал действий в амо (прозрачность: что робот сделал и когда)
CREATE TABLE IF NOT EXISTS adminbot.amo_actions (
    id          bigserial PRIMARY KEY,
    order_id    bigint NOT NULL,
    action      text NOT NULL,                     -- update_lead|move_stage|create_lead|close_task|...
    amo_entity  text,
    amo_id      bigint,
    dry_run     boolean NOT NULL,
    payload     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_amo_actions_order ON adminbot.amo_actions (order_id, created_at);
