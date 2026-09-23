-- Сверка «номер + имя» записи с контактом сделки и напоминание владельцу при
-- расхождении. Применяется вручную:
--   psql "$ADMINBOT_DB_DSN" -f migrations/016_contact_mismatch.sql
--
-- Зачем. Чужой номер или смена номера в записи робот не блокирует и не
-- переносит (решение владельца 22.09, п.6): он сверяет пару «номер + имя»
-- записи с контактом сделки и, если не сходится, напоминает владельцу в
-- личку — раз в сутки, не больше семи раз, тем же приёмом, что и напоминание
-- про адрес (миграция 013). Четыре поля хранят состояние этого разговора:
--
--   contact_mismatch          — что разошлось, человеческим языком, без ПД
--                                в логах (сам текст — в базу можно)
--   contact_reminder_count    — сколько напоминаний уже ушло
--   contact_reminder_sent_at  — когда ушло последнее: отсюда считаются сутки
--   contact_reminder_muted    — «не напоминать»: по кнопке «Я разобрался»
--                                владельца или сам робот, дойдя до потолка в 7
--
-- ТЗ 2026-09-22 «цепочка заказа», задача 5.

ALTER TABLE adminbot.gcal_events
    ADD COLUMN IF NOT EXISTS contact_mismatch          text,
    ADD COLUMN IF NOT EXISTS contact_reminder_count     integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS contact_reminder_sent_at   timestamptz,
    ADD COLUMN IF NOT EXISTS contact_reminder_muted     boolean NOT NULL DEFAULT false;
