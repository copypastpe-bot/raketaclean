-- Почта владельца: сообщение не теряется при обрыве связи с Telegram.
-- Применяется вручную только при первой установке; скрипт обновления
-- прогоняет все файлы migrations/*.sql сам:
--   psql "$ADMINBOT_DB_DSN" -f migrations/009_owner_outbox.sql
--
-- Зачем нужна. Telegram с этого VPS отвечает нестабильно, и сообщение, не
-- ушедшее с первой попытки, пропадало навсегда: робот писал ошибку в журнал
-- и шёл работать дальше. Так 2026-09-02 потерялся отчёт о сделке по записи
-- календаря, а 2026-09-01 — вечерняя сводка. Теперь недоставленное становится
-- долгом: он лежит здесь и досылается, пока не уйдёт или не протухнет.
--
-- Почему в базе, а не в памяти процесса: долг должен пережить перезапуск
-- службы. Обрыв связи и перезапуск часто ходят парой — деплой посреди
-- недоступного Telegram стоил бы владельцу всех накопленных сообщений.

CREATE TABLE IF NOT EXISTS adminbot.owner_outbox (
    id           bigserial PRIMARY KEY,
    chat_id      bigint      NOT NULL,
    kind         text        NOT NULL,
    ref          text,
    text         text        NOT NULL,
    reply_markup jsonb,
    attempts     int         NOT NULL DEFAULT 0,
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    next_try_at  timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    sent_at      timestamptz,
    message_id   bigint,
    dropped_at   timestamptz,
    drop_reason  text
);

-- Досылка спрашивает ровно об одном: какие долги созрели. Частичный индекс
-- держит в себе только их — доставленное и протухшее в него не попадает,
-- поэтому таблица может расти, а очередь остаётся короткой.
CREATE INDEX IF NOT EXISTS owner_outbox_due_idx
    ON adminbot.owner_outbox (next_try_at)
    WHERE sent_at IS NULL AND dropped_at IS NULL;

COMMENT ON TABLE adminbot.owner_outbox IS
    'Сообщения владельцу, которые не ушли с первой попытки. Досылаются фоновой задачей.';
COMMENT ON COLUMN adminbot.owner_outbox.kind IS
    'Назначение сообщения: по нему почта знает, нужен ли долг ещё и что отметить после доставки.';
COMMENT ON COLUMN adminbot.owner_outbox.ref IS
    'К чему относится сообщение: id записи календаря, номер заказа партнёра. NULL — общее (сводка).';
COMMENT ON COLUMN adminbot.owner_outbox.expires_at IS
    'После этого момента сообщение бессмысленно. Сводка живёт часы, карточки и отчёты — сутки.';
