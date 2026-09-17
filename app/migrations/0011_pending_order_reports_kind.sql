-- Очередь pending_order_reports обслуживает два контура: заказы химчистки
-- (public.orders + adminbot.amo_links) и уборки клининга (public.cleaning_orders
-- + adminbot.cleaning_links). Номера уборок и заказов пересекаются — заказ №5
-- и уборка №5 существуют одновременно, — поэтому контур различает явная
-- колонка `kind` ('order' | 'cleaning'), а не догадка по order_id. Ключ
-- поэтому становится составным: одна и та же пара (kind, order_id) уникальна,
-- но order_id сам по себе больше не может быть первичным ключом.
--
-- Жёсткий FK на orders(id) снимаем: он не может одновременно указывать то на
-- orders, то на cleaning_orders. То же решение уже применено к
-- adminbot.amo_links / adminbot.cleaning_links — там order_id тоже не
-- внешний ключ (0010_cleaning_links.sql, раздел «Зачем отдельные таблицы»).
--
-- ТЗ 2026-09-17 «адреса в клининге (уборки)», задача 3.

ALTER TABLE pending_order_reports
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'order';

ALTER TABLE pending_order_reports DROP CONSTRAINT IF EXISTS pending_order_reports_pkey;
ALTER TABLE pending_order_reports DROP CONSTRAINT IF EXISTS pending_order_reports_order_id_fkey;
ALTER TABLE pending_order_reports ADD PRIMARY KEY (kind, order_id);
