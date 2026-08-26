#!/usr/bin/env bash
# Обновление админ-бота: код из рабочей копии → служба. Выполняется от root:
#   sudo bash /tmp/adminbot_update.sh
#
# Настройки (/opt/raketa-admin-bot/.env) не трогаются: там токены и пароль базы.
# Миграции применяются все подряд — они написаны так, что повтор безопасен.

set -euo pipefail

HOME_DIR=/opt/raketa-admin-bot
APP_DIR=$HOME_DIR/app
SRC_DIR=/home/admin/raketa-admin-bot
ENV_FILE=$HOME_DIR/.env

say() { printf '\n=== %s ===\n' "$1"; }

say "0. Право обновлять без пароля"
# Кладём этот же скрипт в системный каталог (менять его сможет только root)
# и разрешаем запускать именно его. Так обновления идут без пароля владельца,
# а список разрешённого остаётся коротким и понятным.
INSTALLED=/usr/local/sbin/raketa-admin-bot-update
install -o root -g root -m 0755 "${BASH_SOURCE[0]}" "$INSTALLED"
cat > /etc/sudoers.d/raketa-admin-bot <<'SUDO'
admin ALL=(root) NOPASSWD: /usr/local/sbin/raketa-admin-bot-update
admin ALL=(root) NOPASSWD: /usr/bin/systemctl restart raketa-admin-bot.service, /usr/bin/systemctl start raketa-admin-bot.service, /usr/bin/systemctl stop raketa-admin-bot.service, /usr/bin/systemctl status raketa-admin-bot.service
SUDO
chmod 0440 /etc/sudoers.d/raketa-admin-bot
visudo -c -q -f /etc/sudoers.d/raketa-admin-bot && echo "правило установлено и проверено"

say "1. Код"
rsync -a --delete --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
      --exclude '.pytest_cache' "$SRC_DIR/" "$APP_DIR/"
chown -R adminbot:adminbot "$APP_DIR"
echo "обновлено из $SRC_DIR"

say "2. Зависимости"
sudo -u adminbot "$HOME_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
echo "проверены"

say "3. Миграции"
DSN=$(grep '^ADMINBOT_DB_DSN=' "$ENV_FILE" | cut -d= -f2-)
cd /tmp
for sql in "$APP_DIR"/migrations/*.sql; do
    sudo -u adminbot psql "$DSN" -v ON_ERROR_STOP=1 -q -f "$sql"
    echo "  $(basename "$sql")"
done

say "4. Перезапуск"
systemctl restart raketa-admin-bot.service
sleep 4
systemctl is-active raketa-admin-bot.service

say "5. Журнал"
journalctl -u raketa-admin-bot.service -n 15 --no-pager | tail -15
