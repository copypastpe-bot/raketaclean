-- Оценка по уборкам — как у химчистки: те же четыре поля, что у orders
-- (bot.py ensure_orders_rating_schema), те же типы. Когда клиенту ставится
-- просьба оценить уборку, бот отмечает «попросили» (rating_requested_at) и
-- стирает прошлый ответ; ответ-цифру клиента бот пишет сюда же, а цикл
-- «Повторный заказ» админ-бота читает её и ведёт задачи в сделке уборки.
-- Только новые уборки: у проведённых до выката поля пустые, оценку по ним
-- не ловим (решение владельца 30.09). Изменение cleaning_orders в бою
-- разрешено владельцем 30.09.
--
-- ТЗ docs/plans/2026-09-30-cleaning-ratings.md, задача 1.

ALTER TABLE cleaning_orders
    ADD COLUMN IF NOT EXISTS rating_score smallint,
    ADD COLUMN IF NOT EXISTS rating_comment text,
    ADD COLUMN IF NOT EXISTS rating_requested_at timestamptz,
    ADD COLUMN IF NOT EXISTS rating_replied_at timestamptz;
