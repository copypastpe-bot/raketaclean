-- Уборки клининг-контура: своя таблица связок и свой журнал.
-- Применяется вручную:
--   psql "$ADMINBOT_DB_DSN" -f migrations/010_cleaning_links.sql
--
-- Зачем отдельные таблицы, а не общие с химчисткой. Уборки живут в своей таблице
-- рабочего бота (`public.cleaning_orders`), и её номера пересекаются с номерами
-- `public.orders`: заказ №5 и уборка №5 существуют одновременно. Общая таблица
-- связок с ключом `order_id` считала бы их одной работой и провела бы в амо одну
-- вместо двух.
--
-- Строение — копия `amo_links` / `amo_actions` из 001 (плюс колонка `question`
-- из 003): движок один и тот же, он просто получает другое хранилище.

CREATE TABLE IF NOT EXISTS adminbot.cleaning_links (
    order_id        bigint PRIMARY KEY,            -- cleaning_orders.id рабочего бота (не FK: чужая схема)
    phone10         text NOT NULL,
    status          text NOT NULL DEFAULT 'new',   -- new|in_progress|waiting_owner|waiting_salesbot|done|error
    path            text,                          -- A|B|C|D (путь из дизайна §4)
    primary_lead_id bigint,
    real_lead_id    bigint,
    checklist       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {"step_name": "2026-09-10T10:00:00Z", ...}
    question        jsonb,                         -- варианты, между которыми робот не выбрал сам
    question_msg_id bigint,                        -- id карточки-вопроса в TG, если путь Г
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cleaning_links_status ON adminbot.cleaning_links (status);
-- Поиск «занятых» сделок идёт по телефону клиента сразу в двух таблицах связок.
CREATE INDEX IF NOT EXISTS idx_cleaning_links_phone ON adminbot.cleaning_links (phone10);

CREATE TABLE IF NOT EXISTS adminbot.cleaning_actions (
    id          bigserial PRIMARY KEY,
    order_id    bigint NOT NULL,
    action      text NOT NULL,                     -- update_lead|move_stage|create_lead|close_task|...
    amo_entity  text,
    amo_id      bigint,
    dry_run     boolean NOT NULL,
    payload     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cleaning_actions_order ON adminbot.cleaning_actions (order_id, created_at);
