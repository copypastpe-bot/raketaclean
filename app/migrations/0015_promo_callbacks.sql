-- Заявки на обратный звонок по откликам на промо — договорённость рабочего бота
-- с админ-ботом. ТЗ docs/plans/2026-09-23-promo-autocall.md (2026-09-23).
--
-- Рабочий бот пишет строку, когда клиент или лид ответил чистой «1» на промо,
-- которое ему приходило и на которое он ещё не отвечал. Админ-бот читает новые
-- строки (у роли adminbot есть SELECT на новые таблицы public по умолчанию)
-- и заводит сделку в «Новом лиде» с тегом «Отклик на промо»; автозвонок
-- подхватывает её сам, как заявку с сайта. Что уже обработано, админ-бот
-- помнит у себя (схема adminbot); рабочий бот строки не меняет и не удаляет.
--
-- source    — 'client' (напоминание клиенту, promo_reengagements) или
--             'lead' (промо лиду, lead_logs);
-- client_id / lead_id — ровно одно из двух, по source;
-- phone     — телефон из карточки как есть (clients.phone / leads.phone);
--             админ-бот нормализует сам;
-- response_text — что прислал человек (для примечания в сделке).

CREATE TABLE IF NOT EXISTS promo_callbacks (
    id            bigserial PRIMARY KEY,
    source        text NOT NULL CHECK (source IN ('client', 'lead')),
    client_id     integer,
    lead_id       bigint,
    phone         text NOT NULL,
    name          text,
    response_text text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK ((source = 'client' AND client_id IS NOT NULL AND lead_id IS NULL)
        OR (source = 'lead' AND lead_id IS NOT NULL AND client_id IS NULL))
);
