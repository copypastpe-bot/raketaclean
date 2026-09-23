-- Связка заказа мастера с записью календаря и сделкой воронки 2.
-- Задача 9 ТЗ «цепочка заказа» (docs/plans/2026-09-22-order-chain.md,
-- 2026-09-22): при включённом ORDER_CALENDAR_PICK мастер, вводя телефон
-- клиента, выбирает (или получает автоматически, если запись одна) запись
-- adminbot.calendar_jobs, которую проводит. Заказ уезжает в базу уже со
-- ссылкой на эту запись и на сделку воронки 2 — админ-бот (задача 10 того
-- же ТЗ) читает эти колонки и ведёт сделку напрямую, без матчера.
--
-- calendar_event_id — event_id записи adminbot.gcal_events (текстовый id
-- события Google Calendar, не наш serial); deal_lead_id — real_lead_id
-- сделки, если он уже был известен на момент выбора.

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS calendar_event_id text,
    ADD COLUMN IF NOT EXISTS deal_lead_id bigint;
