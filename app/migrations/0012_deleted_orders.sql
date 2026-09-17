-- Регистр удалённых заказов химчистки. Заказ удаляется физически
-- (DELETE FROM orders, bot.py: обработчик order_remove_confirm) и следов не
-- оставляет; без отдельного регистра память о нём пропадает вместе со строкой.
-- Из-за этого сделка в CRM, которую заказ уже провёл, остаётся закрытой как
-- успешная, хотя работы не было, а при повторном проведении того же клиента
-- заводится вторая сделка. Номер заказа переиспользован не будет —
-- `orders.id` выдаёт счётчик `orders_id_seq`, который назад не отматывается
-- (см. ТЗ docs/plans/2026-09-17-order-deletions.md, факт 2), поэтому
-- `order_id` годится в первичный ключ уже удалённой строки.
--
-- Уборок клининга это не касается: они не удаляются физически, а помечаются
-- `cleaning_orders.deleted_at` (cleaning/admin_ops.py) — строка остаётся, и
-- отдельный регистр ей не нужен.
--
-- Читает регистр админ-бот (роль `adminbot`, схема `public` только на
-- чтение) — он и разбирает удаления, отвязывая сделку в CRM. Право SELECT
-- приходит из `ALTER DEFAULT PRIVILEGES ... FOR ROLE bot` на проде, поэтому
-- миграцию обязана применять роль `bot`: от `postgres` привилегия для
-- `adminbot` не появится (факт 4 того же ТЗ).
--
-- ТЗ 2026-09-17 «удаление заказа освобождает сделку в amoCRM», задача 1.

CREATE TABLE IF NOT EXISTS deleted_orders (
    order_id     bigint PRIMARY KEY,
    phone_digits text,
    client_id    bigint,
    amount_total numeric(12,2),
    deleted_at   timestamptz NOT NULL DEFAULT now(),
    deleted_by   bigint
);

-- Админ-бот выбирает неразобранные удаления самого свежего периода.
CREATE INDEX IF NOT EXISTS idx_deleted_orders_deleted_at
    ON deleted_orders (deleted_at);
