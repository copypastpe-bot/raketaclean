#!/usr/bin/env bash
# Установка админ-бота (amo_sync) на сервер. Выполняется от root: sudo bash этот-файл.
# Повторный запуск безопасен: всё, что уже сделано, пропускается.
#
# Что делает:
#   1) системный пользователь adminbot (без входа в систему)
#   2) код в /opt/raketa-admin-bot/app и своё окружение Python
#   3) пользователь базы с правом ЧИТАТЬ таблицы бота и своей схемой adminbot
#   4) три миграции
#   5) файл настроек с токенами (права 600)
#   6) служба systemd, запуск с ВЫКЛЮЧЕННОЙ функцией и в режиме репетиции

set -euo pipefail

HOME_DIR=/opt/raketa-admin-bot
APP_DIR=$HOME_DIR/app
SRC_DIR=/home/admin/raketa-admin-bot
ENV_FILE=$HOME_DIR/.env
UNIT=/etc/systemd/system/raketa-admin-bot.service
OWNER_TG_ID=$(grep "^ADMINBOT_OWNER_TG_ID=" /home/admin/.adminbot.env | cut -d= -f2-)

say() { printf '\n=== %s ===\n' "$1"; }

say "1. Пользователь adminbot"
if id adminbot >/dev/null 2>&1; then
    echo "уже есть"
else
    useradd --system --shell /usr/sbin/nologin --home "$HOME_DIR" adminbot
    echo "создан"
fi
mkdir -p "$APP_DIR"
chown -R adminbot:adminbot "$HOME_DIR"

say "2. Код"
rsync -a --delete --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
      --exclude '.pytest_cache' "$SRC_DIR/" "$APP_DIR/"
chown -R adminbot:adminbot "$APP_DIR"
echo "скопировано из $SRC_DIR"

say "3. Окружение Python"
if [ ! -x "$HOME_DIR/.venv/bin/python" ]; then
    sudo -u adminbot python3 -m venv "$HOME_DIR/.venv"
fi
sudo -u adminbot "$HOME_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u adminbot "$HOME_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
sudo -u adminbot "$HOME_DIR/.venv/bin/python" -c "import aiogram, asyncpg, aiohttp; print('зависимости на месте')"

say "4. Пользователь базы и схема"
# Пароль генерируем здесь: его никто не набирает руками и он не проходит через переписку.
if [ -f "$ENV_FILE" ] && grep -q '^ADMINBOT_DB_PASSWORD=' "$ENV_FILE"; then
    DB_PASS=$(grep '^ADMINBOT_DB_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
    echo "пароль базы взят из прежней установки"
else
    DB_PASS=$(openssl rand -hex 24)
    echo "пароль базы сгенерирован"
fi

# cd /tmp: иначе postgres ругается, что не может войти в домашний каталог admin.
cd /tmp
sudo -u postgres psql -v ON_ERROR_STOP=1 -d clients_db <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adminbot') THEN
        CREATE ROLE adminbot LOGIN;
    END IF;
END
\$\$;
ALTER ROLE adminbot PASSWORD '$DB_PASS';

-- таблицы рабочего бота: только чтение, никогда не запись
GRANT USAGE ON SCHEMA public TO adminbot;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO adminbot;
ALTER DEFAULT PRIVILEGES FOR ROLE bot IN SCHEMA public GRANT SELECT ON TABLES TO adminbot;

-- собственная схема робота
CREATE SCHEMA IF NOT EXISTS adminbot AUTHORIZATION adminbot;

-- Право создавать схемы в этой базе. Нужно потому, что миграция 001 начинается
-- с CREATE SCHEMA IF NOT EXISTS, а Postgres проверяет право до проверки
-- существования. На таблицы рабочего бота это никак не влияет: там по-прежнему
-- только SELECT.
GRANT CREATE ON DATABASE clients_db TO adminbot;
SQL
echo "роль и схема готовы"

DSN="postgresql://adminbot:$DB_PASS@127.0.0.1:5432/clients_db"

say "5. Миграции"
for sql in "$APP_DIR"/migrations/*.sql; do
    echo "  применяю $(basename "$sql")"
    sudo -u adminbot psql "$DSN" -v ON_ERROR_STOP=1 -q -f "$sql"
done
echo "таблицы схемы adminbot:"
sudo -u adminbot psql "$DSN" -tAc "select tablename from pg_tables where schemaname='adminbot' order by 1" | sed 's/^/  /'

say "5a. Проверка: в таблицы бота писать нельзя"
if sudo -u adminbot psql "$DSN" -q -c "UPDATE public.orders SET amount_total = amount_total WHERE false" 2>/dev/null; then
    echo "ВНИМАНИЕ: запись в public прошла — права выданы слишком широко!"
    exit 1
else
    echo "запись отклонена — правило read-only работает"
fi

say "6. Настройки"
TG_TOKEN=$(grep '^ADMINBOT_TG_TOKEN=' /home/admin/.adminbot.env | cut -d= -f2-)
AMO_TOKEN=$(grep '^AMOCRM_API_TOKEN=' /home/admin/.amo_write.env | cut -d= -f2-)
AMO_BASE=$(grep '^AMOCRM_API_BASE=' /opt/telegram-bot/.env | cut -d= -f2- | tr -d '"')
TG_IPS=$(grep '^TELEGRAM_API_IPS=' /opt/telegram-bot/.env | cut -d= -f2- | tr -d '"')

umask 077
cat > "$ENV_FILE" <<ENV
# Настройки админ-бота. Создано скриптом установки. В git не попадает.

ADMINBOT_TG_TOKEN=$TG_TOKEN
ADMINBOT_OWNER_TG_ID=$OWNER_TG_ID
TELEGRAM_API_IPS=$TG_IPS

BOT_DB_DSN=$DSN
ADMINBOT_DB_DSN=$DSN
ADMINBOT_DB_PASSWORD=$DB_PASS

AMO_BASE_URL=$AMO_BASE
AMO_TOKEN=$AMO_TOKEN

# Первый запуск: функция выключена, режим репетиции.
AMO_SYNC_ENABLED=0
AMO_SYNC_DRY_RUN=1
AMO_SYNC_BACKLOG_FROM=2026-08-21

SERVICE_BY_MASTER=Никита:furniture,Дмитрий:furniture,Дима:furniture,Ольга:cleaning,Оля:cleaning
ENV
chown adminbot:adminbot "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "записан $ENV_FILE (права 600)"
for name in ADMINBOT_TG_TOKEN AMO_TOKEN AMO_BASE_URL TELEGRAM_API_IPS BOT_DB_DSN; do
    value=$(grep "^$name=" "$ENV_FILE" | cut -d= -f2-)
    [ -n "$value" ] && echo "  $name: заполнено" || echo "  $name: ПУСТО — проверьте источник"
done

say "7. Служба"
cp "$APP_DIR/deploy/adminbot.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now raketa-admin-bot.service
sleep 4
systemctl status raketa-admin-bot.service --no-pager | head -12

say "8. Последние строки журнала"
journalctl -u raketa-admin-bot.service -n 20 --no-pager | tail -20

say "Готово"
echo "Проверьте в Telegram: напишите боту /status"
