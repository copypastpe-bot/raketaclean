-- 001_notify_schema.sql — фундамент службы оповещений (ТЗ 2026-09-18, задача 1).
--
-- Схема `notify` — общий почтовый ящик двух ботов и состояние службы-почтальона.
-- Живёт в той же базе `clients_db`, но отдельной схемой: правило «админ-бот не пишет
-- в public» остаётся нетронутым, а рабочий бот не лезет в хозяйство админ-бота.
--
-- Применяется ролью `bot` (она суперпользователь Postgres — только так новые таблицы
-- получают права для остальных ролей):
--     cd /opt/telegram-bot
--     psql "$DSN_AS_BOT" -f /путь/raketa-notify/migrations/001_notify_schema.sql
--
-- До применения роль службы должна существовать (её заводит владелец, агент за паролями
-- не ходит):
--     psql "$DSN_AS_BOT" -c "CREATE ROLE notify LOGIN PASSWORD 'пароль';"
--
-- Миграция идемпотентна: повторный прогон ничего не ломает.

BEGIN;

CREATE SCHEMA IF NOT EXISTS notify;

COMMENT ON SCHEMA notify IS
    'Служба оповещений: почтовый ящик обоих ботов, справочник маршрутов, инциденты.';

-- --------------------------------------------------------------------------
-- Почтовый ящик: сюда боты кладут событие, отсюда почтальон его забирает.
-- Форма полей повторяет `adminbot.owner_outbox` (миграция 009) — она проверена
-- месяцем работы книги долгов и умеет переживать обрыв связи.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS notify.outbox (
    id           bigserial   PRIMARY KEY,
    kind         text        NOT NULL,
    text         text        NOT NULL,
    reply_markup jsonb,
    ref          text,
    source       text        NOT NULL,
    dry_run      boolean     NOT NULL DEFAULT false,
    status       text        NOT NULL DEFAULT 'pending',
    attempts     int         NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    next_try_at  timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    sent_at      timestamptz,
    message_id   bigint,
    error        text,
    CONSTRAINT notify_outbox_status_check
        CHECK (status IN ('pending', 'sent', 'dropped')),
    CONSTRAINT notify_outbox_source_check
        CHECK (source IN ('worker', 'adminbot', 'notify'))
);

COMMENT ON COLUMN notify.outbox.kind IS
    'Вид события. Куда его нести и каким уровнем — решает notify.routes, не отправитель.';
COMMENT ON COLUMN notify.outbox.ref IS
    'На что событие ссылается: номер заказа, сделки, записи календаря. Нужен, чтобы понять,
     не отпала ли нужда в сообщении, пока оно лежало.';
COMMENT ON COLUMN notify.outbox.source IS
    'Кто положил событие: рабочий бот, админ-бот или сама служба (сторож, инциденты).';
COMMENT ON COLUMN notify.outbox.dry_run IS
    'Событие репетиции. Боевой режим такие строки не трогает, и наоборот: репетиция не
     должна красть работу у боевого режима.';
COMMENT ON COLUMN notify.outbox.expires_at IS
    'Срок годности. Протухшее не отправляем: вечерняя сводка, пролежав ночь, уже врёт.';

-- Почтальон каждый проход спрашивает «что созрело» — индекс ровно под этот вопрос.
CREATE INDEX IF NOT EXISTS notify_outbox_due_idx
    ON notify.outbox (next_try_at)
    WHERE status = 'pending';

-- --------------------------------------------------------------------------
-- Справочник маршрутов: единственное место, где решается «куда и каким уровнем».
-- Правится строкой, без правки кода — ради этого вся затея.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS notify.routes (
    kind       text        PRIMARY KEY,
    address    text        NOT NULL,
    level      text        NOT NULL DEFAULT 'grey',
    tag        text,
    enabled    boolean     NOT NULL DEFAULT true,
    comment    text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT notify_routes_address_check
        CHECK (address IN ('work_chat', 'ops_feed', 'tech_journal',
                           'my_assistant', 'my_admin', 'manager')),
    CONSTRAINT notify_routes_level_check
        CHECK (level IN ('red', 'yellow', 'grey'))
);

COMMENT ON TABLE notify.routes IS
    'Вид события → адрес, уровень, тег. Выключенный маршрут гасит событие, не теряя его.';
COMMENT ON COLUMN notify.routes.address IS
    'work_chat — рабочие чаты (не трогаем), ops_feed — операционная лента (нынешний чат
     логов), tech_journal — технический журнал без звука, my_assistant — тревоги по делам,
     my_admin — тревоги по железу, manager — чат менеджера.';
COMMENT ON COLUMN notify.routes.level IS
    'red — повтор каждые 10 минут до «сел разбираться», ночью тоже; yellow — редкое
     напоминание с потолком; grey — не дёргает вовсе.';

-- --------------------------------------------------------------------------
-- Инциденты: поломка это состояние, а не сообщение. Только так есть куда вешать
-- повторы, кнопку «сел разбираться» и автоматический отбой.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS notify.incidents (
    id               bigserial   PRIMARY KEY,
    key              text        NOT NULL,
    level            text        NOT NULL,
    address          text        NOT NULL,
    state            text        NOT NULL DEFAULT 'open',
    detail           text,
    opened_at        timestamptz NOT NULL DEFAULT now(),
    acked_at         timestamptz,
    ack_until        timestamptz,
    closed_at        timestamptz,
    escalated_at     timestamptz,
    last_notified_at timestamptz,
    notify_count     int         NOT NULL DEFAULT 0,
    CONSTRAINT notify_incidents_state_check
        CHECK (state IN ('open', 'acked', 'closed')),
    CONSTRAINT notify_incidents_level_check
        CHECK (level IN ('red', 'yellow'))
);

COMMENT ON COLUMN notify.incidents.key IS
    'Что именно сломано: «пульс рабочего бота», «опрос amoCRM», «прокси». Ключ один на
     поломку, поэтому вторая проверка не заводит второй инцидент.';
COMMENT ON COLUMN notify.incidents.ack_until IS
    'До какого времени молчим после «сел разбираться». Истекло, а поломка жива —
     напоминания возвращаются.';
COMMENT ON COLUMN notify.incidents.escalated_at IS
    'Когда красная техническая поломка была продублирована владельцу словами последствия.';

-- Одна открытая запись на поломку: «уже сообщили» проверяется этим ограничением,
-- а не памятью процесса — иначе перезапуск службы заведёт дубль.
CREATE UNIQUE INDEX IF NOT EXISTS notify_incidents_open_key_idx
    ON notify.incidents (key)
    WHERE state <> 'closed';

-- --------------------------------------------------------------------------
-- Права. Боты только кладут события; всё остальное — дело службы.
-- --------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'notify') THEN
        RAISE EXCEPTION
            'Нет роли notify. Заведите её до применения миграции: '
            'CREATE ROLE notify LOGIN PASSWORD ''...'';';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA notify TO notify;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA notify TO notify;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA notify TO notify;

-- Рабочий бот кладёт события своей ролью, админ-бот — своей. Читать чужую почту и
-- менять маршруты им незачем.
GRANT USAGE ON SCHEMA notify TO bot, adminbot;
GRANT INSERT, SELECT ON notify.outbox TO bot, adminbot;
GRANT USAGE, SELECT ON SEQUENCE notify.outbox_id_seq TO bot, adminbot;
GRANT SELECT ON notify.routes TO bot, adminbot;

COMMIT;
