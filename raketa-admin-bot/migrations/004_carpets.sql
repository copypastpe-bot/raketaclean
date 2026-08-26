-- Ковры от партнёра: что робот знает о заказах из отчётов.
-- Применяется вручную:
--   psql "$ADMINBOT_DB_DSN" -f migrations/004_carpets.sql
--
-- Ключ — номер заказа в CRM партнёра (колонка «#» в отчёте). Он сквозной
-- и приходит в каждом отчёте, поэтому месячный свод, где те же заказы идут
-- второй раз, не приведёт к повторной обработке.

CREATE TABLE IF NOT EXISTS adminbot.carpet_links (
    partner_id      bigint PRIMARY KEY,            -- номер заказа у партнёра
    phone10         text NOT NULL,
    status          text NOT NULL DEFAULT 'new',   -- new|in_progress|waiting_owner|waiting_salesbot|done|error
    path            text,                          -- primary|scratch (как ведём заказ)
    lead_id         bigint,                        -- сделка воронки «Ковры Кристал»
    primary_lead_id bigint,                        -- лид первичной, если цепочку вели с него
    checklist       jsonb NOT NULL DEFAULT '{}'::jsonb,
    question        jsonb,                         -- вопрос владельцу и варианты сделок
    question_msg_id bigint,
    last_error      text,
    source_file     text,                          -- из какого вложения пришла строка
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_carpet_links_status ON adminbot.carpet_links (status);

-- Журнал действий по коврам: тот же смысл, что у amo_actions, но свой ключ.
CREATE TABLE IF NOT EXISTS adminbot.carpet_actions (
    id          bigserial PRIMARY KEY,
    partner_id  bigint NOT NULL,
    action      text NOT NULL,
    amo_entity  text,
    amo_id      bigint,
    dry_run     boolean NOT NULL,
    payload     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_carpet_actions_order ON adminbot.carpet_actions (partner_id, created_at);

-- Разобранные письма: чтобы не перечитывать одно и то же после перезапуска,
-- даже если пометка в почтовом ящике не поставилась.
CREATE TABLE IF NOT EXISTS adminbot.carpet_letters (
    uid         text PRIMARY KEY,                  -- идентификатор письма в папке
    subject     text,
    files       text[],
    rows_total  integer NOT NULL DEFAULT 0,
    processed_at timestamptz NOT NULL DEFAULT now()
);
