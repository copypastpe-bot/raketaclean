-- Настройки, которые владелец меняет из Telegram (сейчас — только пауза).
-- Применяется вручную:
--   psql "$ADMINBOT_DB_DSN" -f migrations/002_settings.sql
-- Хранить их в базе, а не в памяти, нужно затем, чтобы пауза пережила
-- перезапуск сервиса: иначе робот после рестарта молча возобновил бы работу.

CREATE TABLE IF NOT EXISTS adminbot.settings (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
