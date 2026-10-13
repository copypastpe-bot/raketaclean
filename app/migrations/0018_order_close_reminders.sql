-- Сторож проведения заказов: запись-заказ в календаре есть, заказа в боте нет.
-- ТЗ docs/plans/2026-10-13-order-close-reminder.md (задача 1, 2026-10-13).
-- Рассылка (bot.py) берёт кандидатов из adminbot.calendar_jobs, по каждой
-- непроведённой записи заводит строку здесь и напоминает в рабочие чаты:
-- первое — на следующий день после даты записи с 10:00 МСК, повтор — каждые
-- 30 минут до 20:00, пока заказ не проведут или запись не уберут из календаря.
--
-- event_id    — ключ: id записи adminbot.gcal_events (текст Google, не наш serial);
-- id          — короткий номер для callback_data кнопки «Напомнить завтра»:
--               id записи Google бывает длиннее, чем влезает в лимит 64 байта
--               вместе с префиксом (тот же довод, что у amocrm_unsorted_cards);
-- order_date  — дата записи на момент заведения: нужна, когда запись пропала
--               из календаря и из представления её уже не взять;
-- status      — open (напоминаем) / closed (заказ проведён) / gone (запись
--               пропала из календаря: удалили, отменили);
-- messages    — последние сообщения по чатам: {"<chat_id>": <message_id>};
-- snoozed_*   — кто (id и имя в Telegram) и когда нажал «Напомнить завтра»
--               в последний раз (журнал решения; имя — для подписи «Отложено
--               до завтра — <кто>»);
-- closed_at   — когда строка ушла из open (проведён или пропал).

CREATE TABLE IF NOT EXISTS order_close_reminders (
    event_id        text PRIMARY KEY,
    id              bigserial NOT NULL UNIQUE,
    order_date      date NOT NULL,
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'gone')),
    next_send_at    timestamptz NOT NULL,
    messages        jsonb NOT NULL DEFAULT '{}'::jsonb,
    snoozed_by      bigint,
    snoozed_by_name text,
    snoozed_at      timestamptz,
    closed_at       timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_order_close_reminders_due
    ON order_close_reminders (next_send_at)
    WHERE status = 'open';
