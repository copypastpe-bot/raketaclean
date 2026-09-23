-- Дела по записям «Неразобранного» amoCRM: карточка админам с напоминанием.
-- ТЗ docs/plans/2026-09-23-amo-unsorted-cards.md (задачи 2–4, 2026-09-23).
-- Опрос «Неразобранного» заводит дело на каждую новую запись (пропущенный
-- звонок или сообщение); отдельный цикл рассылает карточку обоим админам и
-- напоминает раз в час в окне 9–20 МСК, пока кто-то не нажмёт кнопку.
--
-- id — короткий номер дела для callback_data (uid записи 60 символов, в лимит
-- 64 байта с префиксом не влезает); uid — запись amocrm_unsorted_seen;
-- messages — текущие сообщения карточки: {"<tg id админа>": <message_id>};
-- done_by / done_at — кто и когда нажал (журнал решения).
-- При переезде на шину оповещений таблица остаётся: меняется только доставка.

CREATE TABLE IF NOT EXISTS amocrm_unsorted_cards (
    id           bigserial PRIMARY KEY,
    uid          text NOT NULL UNIQUE,
    kind         text NOT NULL CHECK (kind IN ('call', 'message')),
    lead_id      bigint,
    contact_name text,
    phone        text,
    event_at     timestamptz NOT NULL,
    status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    next_send_at timestamptz NOT NULL,
    messages     jsonb NOT NULL DEFAULT '{}'::jsonb,
    done_by      bigint,
    done_at      timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_amocrm_unsorted_cards_due
    ON amocrm_unsorted_cards (next_send_at)
    WHERE status = 'open';
