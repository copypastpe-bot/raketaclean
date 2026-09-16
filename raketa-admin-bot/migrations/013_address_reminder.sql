-- Напоминание владельцу про сделку, заведённую с нуля без адреса. Применяется
-- вручную:
--   psql "$ADMINBOT_DB_DSN" -f migrations/013_address_reminder.sql
--
-- Зачем. Когда робот заводит сделку с нуля (create_new) и адреса в ней нет,
-- владелец получает карточку с кнопками «Я заполнил» / «Не напоминать» и,
-- пока не ответит, — напоминание раз в сутки. Три поля хранят состояние этого
-- разговора отдельно от самой связки, чтобы наблюдатель заказов их не видел:
--
--   address_reminder_count    — сколько напоминаний уже ушло (счётчик, не журнал)
--   address_reminder_sent_at  — когда ушло последнее: отсюда считаются сутки
--   address_reminder_muted    — «не напоминать»: по решению владельца или сам
--                                робот выставляет, дойдя до потолка в 7 штук
--
-- Колонка — в обеих таблицах связок: химчистка и уборки идут одним движком
-- (ТЗ 2026-09-16 «адреса до конца», задача 7).

ALTER TABLE adminbot.amo_links
    ADD COLUMN IF NOT EXISTS address_reminder_count   integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS address_reminder_sent_at  timestamptz,
    ADD COLUMN IF NOT EXISTS address_reminder_muted    boolean NOT NULL DEFAULT false;

ALTER TABLE adminbot.cleaning_links
    ADD COLUMN IF NOT EXISTS address_reminder_count   integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS address_reminder_sent_at  timestamptz,
    ADD COLUMN IF NOT EXISTS address_reminder_muted    boolean NOT NULL DEFAULT false;
