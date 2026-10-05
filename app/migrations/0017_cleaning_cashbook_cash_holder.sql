-- Реестр денег Оли: у строки кассы клининга поле «чьи деньги».
-- 'olya' — деньги компании на руках у Оли (бригадир клининга), 'dima' —
-- обычная касса. NULL — строки до запуска: реестр стартует с нуля, историю
-- не переносим и не пересчитываем (решение владельца 05.10, п.5).
-- Остаток Оли считается по строкам с cash_holder = 'olya' и deleted_at IS NULL
-- (cleaning/cashbook.py, get_olya_balance), откаты уборок и выплат работают
-- через deleted_at сами.
--
-- Зеркало при запуске — cleaning/schema.py, ensure_cleaning_schema.
-- ТЗ docs/plans/2026-10-05-olya-money-register.md, задача 1.

ALTER TABLE cleaning_cashbook
    ADD COLUMN IF NOT EXISTS cash_holder text;

-- Ограничение по имени, чтобы повторный прогон его не дублировал.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'cleaning_cashbook_cash_holder_check'
          AND conrelid = 'cleaning_cashbook'::regclass
    ) THEN
        ALTER TABLE cleaning_cashbook
            ADD CONSTRAINT cleaning_cashbook_cash_holder_check
            CHECK (cash_holder IN ('olya', 'dima'));
    END IF;
END
$$;
