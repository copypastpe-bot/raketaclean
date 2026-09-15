-- Отложенные письма партнёра: письмо, которое робот не стал проводить.
-- Применяется вручную:
--   psql "$ADMINBOT_DB_DSN" -f migrations/011_carpet_letters_held.sql
--
-- Зачем. 15.09.2026 партнёр прислал на ящик робота отчёт за два года (536 строк).
-- Робот принял его за обычный недельный отчёт и за три минуты завёл 30 лишних
-- сделок в amoCRM. Теперь письмо, где наших строк больше порога, и письмо
-- с неразобранным вложением робот откладывает: не проводит, один раз сообщает
-- владельцу и ждёт его решения.
--
-- Отдельной таблицы не завожу: `carpet_letters` и так помнит письма, а разница
-- между «проведено» и «отложено» — это три колонки. Так состояние письма живёт
-- в одном месте, и «проведено» не может разойтись с «отложено».
--
--   held_reason IS NULL                         — письмо проведено (как раньше);
--   held_reason IS NOT NULL, released_at IS NULL — отложено, робот его не трогает;
--   released_at IS NOT NULL                     — владелец снял отложение,
--                                                 следующий проход проведёт письмо.

ALTER TABLE adminbot.carpet_letters
    ADD COLUMN IF NOT EXISTS held_reason text,          -- почему отложено (текстом, для владельца)
    ADD COLUMN IF NOT EXISTS held_at     timestamptz,   -- когда отложено
    ADD COLUMN IF NOT EXISTS released_at timestamptz;   -- когда владелец снял отложение

-- Список отложенных писем нужен скрипту `--carpets-held`; писем немного,
-- но частичный индекс держит выборку дешёвой и после сотен проведённых писем.
CREATE INDEX IF NOT EXISTS idx_carpet_letters_held ON adminbot.carpet_letters (held_at)
    WHERE held_reason IS NOT NULL AND released_at IS NULL;
