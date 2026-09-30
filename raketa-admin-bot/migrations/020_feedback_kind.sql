-- Цикл «Повторный заказ»: вид работы в состоянии — химчистка или уборка.
-- Применяется обёрткой обновления сама (шаг «4. Миграции» в deploy/update.sh),
-- а она гоняет ВСЕ миграции на каждом выкате — поэтому каждый шаг здесь
-- безопасен для повторного запуска.
--
-- Зачем. Уборки получают оценку клиента так же, как химчистка (решение
-- владельца 2026-09-30: «всё 1 в 1 как у химчистки»), и идут тем же циклом.
-- Но номера у них независимые: `public.orders.id` (химчистка) и
-- `public.cleaning_orders.id` (уборка) — разные последовательности, заказ №12
-- и уборка №12 — разные работы. Ключ состояния `(order_id, mode)` из миграции
-- 019 склеил бы их в одну строку: доведённый до конца заказ №12 молча закрыл
-- бы уборку №12. Поэтому вид работы становится частью ключа.
--
--   kind — 'order'    химчистка (public.orders, связка adminbot.amo_links);
--          'cleaning' уборка    (public.cleaning_orders, связка adminbot.cleaning_links).
-- Все строки, заведённые до этой миграции, — химчистка: другого источника у
-- цикла тогда не было, поэтому им достаётся значение по умолчанию 'order'.
--
-- ТЗ docs/plans/2026-09-30-cleaning-ratings.md, задача 3.

ALTER TABLE adminbot.feedback_state
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'order';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'adminbot.feedback_state'::regclass
          AND conname = 'feedback_state_kind_check'
    ) THEN
        ALTER TABLE adminbot.feedback_state
            ADD CONSTRAINT feedback_state_kind_check CHECK (kind IN ('order', 'cleaning'));
    END IF;
END$$;

-- Первичный ключ (order_id, mode) → (kind, order_id, mode). Меняем, только если
-- ключ ещё старый: повторный прогон видит новый ключ и ничего не делает.
DO $$
DECLARE
    pkey_name text;
    pkey_def  text;
BEGIN
    SELECT conname, pg_get_constraintdef(oid) INTO pkey_name, pkey_def
    FROM pg_constraint
    WHERE conrelid = 'adminbot.feedback_state'::regclass AND contype = 'p';

    IF pkey_def IS DISTINCT FROM 'PRIMARY KEY (kind, order_id, mode)' THEN
        IF pkey_name IS NOT NULL THEN
            EXECUTE format('ALTER TABLE adminbot.feedback_state DROP CONSTRAINT %I', pkey_name);
        END IF;
        ALTER TABLE adminbot.feedback_state ADD PRIMARY KEY (kind, order_id, mode);
    END IF;
END$$;
