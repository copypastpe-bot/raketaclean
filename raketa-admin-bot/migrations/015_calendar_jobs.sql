-- Представление для рабочего бота: запись календаря и связная с ней сделка.
-- Рабочий бот читает это представление вместо прямого доступа к таблицам админ-бота.
--
-- Формат: одна строка на запись календаря, с раскрытыми всеми телефонами записи
-- и адресом из разобранного `event_data`, вместе с известной сделкой и всеми её состояниями.
-- Единственная точка зависимости рабочего бота от админ-бота; колонки менять нельзя.
--
-- ТЗ 2026-09-22 «цепочка заказа», задача 8.

CREATE OR REPLACE VIEW adminbot.calendar_jobs AS
SELECT event_id,
       phone10,
       COALESCE(
         (SELECT array_agg(value) FROM jsonb_array_elements_text(event_data->'phones')),
         ARRAY[phone10]) AS phones,
       order_date,
       client_name,
       event_data->>'address' AS address,
       services,
       primary_lead_id,
       real_lead_id,
       status,
       order_id
FROM adminbot.gcal_events
WHERE kind = 'order' AND status IN ('done', 'in_progress', 'waiting_salesbot');

-- Выдать доступ рабочему боту, если роль существует (может не быть на тестовой базе)
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bot') THEN
    GRANT SELECT ON adminbot.calendar_jobs TO bot;
  END IF;
END $$;
