-- Заявки на звонок по откликам «1» на промо: что админ-бот уже обработал.
-- Применяется обёрткой обновления сама (шаг «4. Миграции» в deploy/update.sh);
-- повторный запуск безопасен.
--
-- Зачем. Рабочий бот пишет строку в `public.promo_callbacks` (миграция рабочего
-- бота 0015), когда клиент или лид ответил чистой «1» на промо. Админ-бот
-- читает новые строки, находит или заводит контакт и заводит сделку в «Новом
-- лиде» воронки 1 с тегом «Отклик на промо»; дальше сделку сам берёт
-- автозвонок, как заявку с сайта. Рабочий бот свои строки не меняет, а писать
-- в схему public админ-боту нельзя — поэтому всё, что админ-бот помнит о
-- заявке, живёт здесь.
--
-- Режим (mode) — часть ключа: репетиция и бой ведут свои строки и свою
-- закладку. Репетиция в CRM не пишет, и её отметки не должны отнимать работу
-- у боя: заявка, которую репетиция «обработала», в бою ещё не обработана.
--
-- ТЗ docs/plans/2026-09-23-promo-autocall.md, задача 4.

-- Одна строка на заявку в каждом режиме.
--   callback_id — номер заявки (public.promo_callbacks.id);
--   mode        — 'rehearsal' | 'live';
--   status      — new          заявку увидели, до сделки ещё не дошли;
--                 lead_created сделка заведена (номер ниже), примечания ещё нет;
--                 queued       сделка с тегом и примечанием стоит в «Новом лиде» —
--                              автозвонок берёт её сам (решение владельца 7);
--                 dry_run      репетиция отчиталась владельцу, в CRM не писала;
--                 failed       не вышло (номер не распознан или 3 сбоя подряд),
--                              владельцу ушло письмо;
--   contact_id  — контакт amoCRM (найденный или заведённый роботом);
--   lead_id     — сделка amoCRM; пишется сразу после создания, чтобы повтор
--                 после сбоя продолжил с места и не завёл вторую сделку;
--   attempts    — сколько проходов подряд упали на этой заявке.
CREATE TABLE IF NOT EXISTS adminbot.promo_callback_state (
    callback_id  bigint NOT NULL,
    mode         text NOT NULL CHECK (mode IN ('rehearsal', 'live')),
    status       text NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new', 'lead_created', 'queued', 'dry_run', 'failed')),
    contact_id   bigint,
    lead_id      bigint,
    attempts     int NOT NULL DEFAULT 0,
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (callback_id, mode)
);
CREATE INDEX IF NOT EXISTS idx_promo_callback_state_open
    ON adminbot.promo_callback_state (mode, status);

-- Закладка «с какой заявки начинать» — своя у каждого режима. Первый проход в
-- режиме ставит её на текущий максимум номера заявки и ничего не обрабатывает:
-- отклики до включения уже обработали люди по старому сообщению админам.
--   last_id — последний номер заявки, который робот уже взял в работу.
CREATE TABLE IF NOT EXISTS adminbot.promo_callback_cursor (
    mode        text PRIMARY KEY CHECK (mode IN ('rehearsal', 'live')),
    last_id     bigint NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
