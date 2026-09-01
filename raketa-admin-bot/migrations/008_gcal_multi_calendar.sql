-- Календарей стало несколько (решение владельца 2026-09-01): мебель и ковры
-- ведут мастера в основном календаре, уборки — бригадир в своём.
--
-- До этой миграции закладка обмена была ровно одна: таблица объявлена как
-- `id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1)`. Второй календарь затирал
-- бы закладку первого, и каждый обмен начинался бы с полной перезагрузки.
--
-- Существующая строка остаётся, но без имени календаря: её усыновит первый
-- календарь из настроек при первом же чтении (см. db.get_calendar_cursor,
-- параметр inherit_legacy). Так закладку не приходится хардкодить в SQL —
-- адрес календаря живёт в настройках службы, а не в миграции.
--
-- Применяется вручную:
--   psql "$ADMINBOT_DB_DSN" -f migrations/008_gcal_multi_calendar.sql
-- Скрипт обновления прогоняет все миграции подряд при каждом деплое, поэтому
-- каждый шаг здесь безопасен для повторного запуска.

ALTER TABLE adminbot.gcal_cursor
    ADD COLUMN IF NOT EXISTS calendar_id text NOT NULL DEFAULT '';

-- Вместе с колонкой уходят и первичный ключ, и ограничение «строка только одна».
ALTER TABLE adminbot.gcal_cursor DROP COLUMN IF EXISTS id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'adminbot.gcal_cursor'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE adminbot.gcal_cursor ADD PRIMARY KEY (calendar_id);
    END IF;
END$$;

COMMENT ON COLUMN adminbot.gcal_cursor.calendar_id IS
    'Адрес календаря в Google. Пустая строка — закладка, снятая до появления второго календаря: её забирает первый календарь из настроек.';
