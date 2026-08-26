#!/usr/bin/env bash
# Обновление админ-бота: код из рабочей копии → служба. Выполняется от root:
#   sudo bash /tmp/adminbot_update.sh          обновить код и перезапустить
#   sudo raketa-admin-bot-update --enable      включить функцию amo_sync
#   sudo raketa-admin-bot-update --disable     выключить функцию
#   sudo raketa-admin-bot-update --live        боевой режим: писать в amoCRM
#   sudo raketa-admin-bot-update --rehearsal   репетиция: решать, но не писать
#
# Токены и пароль базы в /opt/raketa-admin-bot/.env не трогаются никогда —
# меняются только два выключателя, и каждый раз печатается итоговое состояние.

set -euo pipefail

ENABLED=""
DRY_RUN=""
for arg in "$@"; do
    case "$arg" in
        --enable)    ENABLED=1 ;;
        --disable)   ENABLED=0 ;;
        --live)      DRY_RUN=0 ;;
        --rehearsal) DRY_RUN=1 ;;
        *) echo "Неизвестный ключ: $arg" >&2; exit 2 ;;
    esac
done

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

say "4. Выключатели"
set_flag() {                                  # имя переменной, новое значение
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i "s/^$1=.*/$1=$2/" "$ENV_FILE"
    else
        echo "$1=$2" >> "$ENV_FILE"
    fi
}
[ -n "$ENABLED" ] && set_flag AMO_SYNC_ENABLED "$ENABLED"
[ -n "$DRY_RUN" ] && set_flag AMO_SYNC_DRY_RUN "$DRY_RUN"

now_enabled=$(grep '^AMO_SYNC_ENABLED=' "$ENV_FILE" | cut -d= -f2-)
now_dry=$(grep '^AMO_SYNC_DRY_RUN=' "$ENV_FILE" | cut -d= -f2-)
echo "функция: $([ "$now_enabled" = 1 ] && echo 'ВКЛЮЧЕНА' || echo 'выключена')"
echo "режим:   $([ "$now_dry" = 1 ] && echo 'репетиция (в amoCRM не пишем)' || echo 'БОЕВОЙ (пишем в amoCRM)')"

say "5. Перезапуск"
systemctl restart raketa-admin-bot.service
sleep 4
systemctl is-active raketa-admin-bot.service

say "6. Журнал"
journalctl -u raketa-admin-bot.service -n 15 --no-pager | tail -15
