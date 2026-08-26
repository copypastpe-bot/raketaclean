-- Календарь (этап 2): что робот знает о записях календаря владельца.
-- Применяется вручную:
--   psql "$ADMINBOT_DB_DSN" -f migrations/005_gcal.sql
--
-- Ключ — идентификатор записи в Google. Он вечный и не меняется при правках,
-- поэтому повторный обмен (а он случается каждый раз, когда протухает закладка)
-- не заводит вторую сделку по той же записи.

CREATE TABLE IF NOT EXISTS adminbot.gcal_events (
    event_id        text PRIMARY KEY,             -- идентификатор записи в Google
    kind            text NOT NULL,                -- order|block|boat|rewash|unsettled|skip
    order_date      date,                         -- дата заказа, московская
    phone10         text,
    client_name     text,
    district        text,                         -- канон района; NULL — приставка непонятна
    services        text[],
    event_data      jsonb,                        -- разобранная запись: нужна, чтобы
                                                  -- продолжить цепочку после ожидания
    status          text NOT NULL DEFAULT 'new',  -- new|in_progress|waiting_salesbot|
                                                  -- waiting_owner|done|skipped|cancelled|error
    skip_reason     text,                         -- почему запись не в работе
    path            text,                         -- A|B|C — как ведём запись
    primary_lead_id bigint,                       -- лид первичной воронки
    real_lead_id    bigint,                       -- сделка воронки реализации
    order_id        integer,                      -- заказ бота, если он уже пришёл
    checklist       jsonb NOT NULL DEFAULT '{}'::jsonb,
    question        jsonb,                        -- вопрос владельцу и варианты
    question_msg_id bigint,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gcal_events_status ON adminbot.gcal_events (status);
CREATE INDEX IF NOT EXISTS idx_gcal_events_phone ON adminbot.gcal_events (phone10, order_date);

-- Журнал действий по календарю: тот же смысл, что у amo_actions и carpet_actions.
CREATE TABLE IF NOT EXISTS adminbot.gcal_actions (
    id          bigserial PRIMARY KEY,
    event_id    text NOT NULL,
    action      text NOT NULL,
    amo_entity  text,
    amo_id      bigint,
    dry_run     boolean NOT NULL,
    payload     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gcal_actions_event ON adminbot.gcal_actions (event_id, created_at);

-- Закладка обмена с Google: одна строка на весь сервис.
-- В репетиции сюда НЕ пишем: боевой запуск начал бы с «изменений нет»
-- и пропустил бы всё, что робот уже посмотрел вхолостую.
CREATE TABLE IF NOT EXISTS adminbot.gcal_cursor (
    id          smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    sync_token  text,
    sync_from   date NOT NULL,                    -- дата включения: глубже не читаем
    updated_at  timestamptz NOT NULL DEFAULT now()
);
