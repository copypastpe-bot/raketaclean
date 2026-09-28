-- Цикл «Повторный заказ»: оценка клиента → задача «Связаться» → закрытие
-- «Повторного заказа». Применяется обёрткой обновления сама (шаг «4. Миграции»
-- в deploy/update.sh); повторный запуск безопасен.
--
-- Зачем. Рабочий бот пишет оценку клиента в `public.orders` (rating_score,
-- rating_comment, rating_replied_at). У 39 открытых задач «Повторный заказ»
-- (amoCRM id 2270746) в сделке реализации 11 уже дождались оценки, но робот
-- их не закрывал: разовое правило `close_feedback_task` в чек-листе только
-- закрывает задачу, когда оценка уже есть, и не ставит следующий шаг для
-- заказов, у которых оценки ещё не было на момент прохода чек-листа. Этот
-- цикл читает оценённые заказы отдельно от чек-листа и сам ведёт разговор:
-- по низкой оценке — примечание и задача «Связаться» (1–4), по счастливому
-- случаю — сразу к закрытию «Повторного заказа». Как обычно, режим (mode) —
-- часть ключа: репетиция и бой ведут свои строки и свою закладку.
--
-- ТЗ docs/plans/2026-09-28-feedback-tasks.md, задача 2.

-- Одна строка на заказ в каждом режиме.
--   order_id         — public.orders.id рабочего бота (не FK: чужая схема);
--   mode              — 'rehearsal' | 'live';
--   status            — new          увидели оценённый заказ, цикл ещё не
--                                     закончен (ждём задачу или повторяем
--                                     после сбоя);
--                        contact_set  комментарий и задача «Связаться»
--                                     поставлены (оценка 1–4), «Повторный
--                                     заказ» ещё не закрыт;
--                        done         всё сделано;
--                        skipped      делать нечего (решения 6 и 8 ТЗ,
--                                     30 дней без задачи «Повторный заказ»);
--                        dry_run      репетиция отчиталась, в CRM не писала;
--                        failed       3 сбоя подряд, владельцу ушло письмо;
--   contact_task_id   — номер поставленной задачи «Связаться»; в репетиции — 0;
--   note_added        — комментарий с текстом оценки уже записан в сделку;
--   attempts          — сколько проходов подряд упали на этом заказе.
CREATE TABLE IF NOT EXISTS adminbot.feedback_state (
    order_id         bigint NOT NULL,
    mode             text NOT NULL CHECK (mode IN ('rehearsal', 'live')),
    status           text NOT NULL DEFAULT 'new'
                     CHECK (status IN ('new', 'contact_set', 'done', 'skipped',
                                       'dry_run', 'failed')),
    contact_task_id  bigint,
    note_added       boolean NOT NULL DEFAULT false,
    attempts         int NOT NULL DEFAULT 0,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (order_id, mode)
);
CREATE INDEX IF NOT EXISTS idx_feedback_state_open
    ON adminbot.feedback_state (mode, status);

-- Момент первого прохода режима — своя закладка у каждого. Решение владельца 8:
-- цикл берёт в работу только то, что оценено после включения, а не всю
-- историю разом; закладка ставится один раз и назад не двигается.
--   started_at — когда режим впервые прошёл цикл.
CREATE TABLE IF NOT EXISTS adminbot.feedback_cursor (
    mode        text PRIMARY KEY CHECK (mode IN ('rehearsal', 'live')),
    started_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
