-- Очередь отложенного отчёта в чат и сообщения о деньгах: обе рассылки ждут,
-- пока по заказу не появится связка со сделкой CRM (адрес приезжает туда же —
-- см. 0008_orders_address.sql и adminbot.amo_links/cleaning_links, колонка
-- deal_address), и уходят с адресом или без него, если связка не появилась
-- за 30 минут. Живёт в базе, а не в памяти процесса: перезапуск службы не
-- должен терять неотправленные отчёты (тот же довод, что у
-- raketa-admin-bot/migrations/009_owner_outbox.sql).
--
-- ТЗ 2026-09-16 «адреса до конца», задача 5.

CREATE TABLE IF NOT EXISTS pending_order_reports (
    order_id    integer PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
    payload     jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz
);

-- Фоновый проход спрашивает ровно об одном: какие отчёты ещё не ушли.
CREATE INDEX IF NOT EXISTS idx_pending_order_reports_pending
    ON pending_order_reports (created_at)
    WHERE sent_at IS NULL;
