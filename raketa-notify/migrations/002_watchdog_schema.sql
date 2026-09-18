-- 002_watchdog_schema.sql — фундамент для сторожа (ТЗ 2026-09-18, задачи 6-7).
--
-- Две вещи в одной миграции, обе — служебные, не бизнес-данные:
--
-- 1. `notify.service_heartbeats` — пульс админ-бота. Та же роль, что у
--    `public.service_heartbeats` (клиентский и рабочий боты, bot.py:717),
--    но в схеме `notify`, а не `public`: у роли `adminbot` на схему `public`
--    только SELECT (хард-правило проекта, raketa-admin-bot/CLAUDE.md,
--    технически закреплено с 2026-08-26), и писать пульс в чужую таблицу
--    ей нельзя. Схема `notify` — нейтральная территория, где `adminbot` уже
--    имеет право писать (миграция 001, notify.outbox), поэтому пульс лёг сюда,
--    а не в новую таблицу схемы `adminbot` — тогда сторожу не нужен был бы
--    ещё один cross-schema грант в придачу к тому, что ниже.
--    Форма колонок — подмножество `public.service_heartbeats`: только то,
--    что нужно проверке «жив/не жив» (задача 7). Колонки алертинга
--    (alert_open, last_alerted_at, last_recovered_at) не переносятся — это
--    машинерия check_client_bot_health для ЕГО собственной таблицы, а не
--    общий формат; сторож это состояние не хранит вовсе (задача 8, не эта).
--
-- 2. Права роли `notify` на ЧТЕНИЕ существующих таблиц ботов (схема `public`,
--    владелец — роль `bot`) — сторожу (задача 7) нужно проверить пульс
--    рабочего и клиентского ботов, идёт ли опрос amoCRM и жив ли рассыльщик
--    клиентам. Только SELECT — симметрично тому, что `adminbot` имеет на
--    public (тоже только SELECT); сторож в чужие таблицы не пишет.
--
-- Применяется той же командой, что и 001 (роль bot — суперпользователь, только
-- так можно выдать права другим ролям):
--     cd /opt/telegram-bot
--     psql "$DSN_AS_BOT" -f /путь/raketa-notify/migrations/002_watchdog_schema.sql
--
-- Миграция идемпотентна: повторный прогон ничего не ломает.

BEGIN;

CREATE TABLE IF NOT EXISTS notify.service_heartbeats (
    service_key  text        PRIMARY KEY,
    display_name text        NOT NULL,
    status       text        NOT NULL DEFAULT 'starting',
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_ok_at   timestamptz,
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT notify_service_heartbeats_status_check
        CHECK (status IN ('starting', 'ok', 'error'))
);

COMMENT ON TABLE notify.service_heartbeats IS
    'Пульс админ-бота (раз в минуту, задача 6). Аналог public.service_heartbeats
     для бота, которому нельзя писать в схему public. Сторож (задача 7) читает обе
     таблицы и решает «жив/не жив» по возрасту last_seen_at.';

GRANT INSERT, UPDATE, SELECT ON notify.service_heartbeats TO adminbot;
GRANT ALL PRIVILEGES ON notify.service_heartbeats TO notify;

-- Права на чтение существующих таблиц ботов (схема public, роль bot).
-- Только SELECT — сторож смотрит, но не пишет.
--
-- Каждый грант — только если таблица уже есть. В бою это всегда так (bot.py
-- бутстрапит их при каждом своём старте, задолго до этой миграции), а вот
-- тестовая база службы (notify_test) поднимается пустой: часть тестов эти
-- таблицы поднимает сама (fixtures/bot_schema_min.sql, задача 7), остальные —
-- нет, и без этой защиты миграция валила бы их всех "relation does not exist".
GRANT USAGE ON SCHEMA public TO notify;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables
               WHERE schemaname = 'public' AND tablename = 'service_heartbeats') THEN
        GRANT SELECT ON public.service_heartbeats TO notify;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_tables
               WHERE schemaname = 'public' AND tablename = 'amocrm_api_state') THEN
        GRANT SELECT ON public.amocrm_api_state TO notify;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_tables
               WHERE schemaname = 'public' AND tablename = 'notification_outbox') THEN
        GRANT SELECT ON public.notification_outbox TO notify;
    END IF;
END
$$;

COMMIT;
