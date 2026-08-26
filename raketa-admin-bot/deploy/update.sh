#!/usr/bin/env bash
# Обновление админ-бота: код из рабочей копии → служба. Выполняется от root:
#   sudo bash /tmp/adminbot_update.sh          обновить код и перезапустить
#   sudo raketa-admin-bot-update --enable      включить функцию amo_sync
#   sudo raketa-admin-bot-update --disable     выключить функцию
#   sudo raketa-admin-bot-update --live        боевой режим: писать в amoCRM
#   sudo raketa-admin-bot-update --rehearsal   репетиция: решать, но не писать
#   sudo raketa-admin-bot-update --backlog-from=2026-08-14   с какой даты разбирать заказы
#   sudo raketa-admin-bot-update --report      что робот записал (ничего не меняет)
#   sudo raketa-admin-bot-update --carpets-on --carpets-rehearsal   ковры: репетиция
#   sudo raketa-admin-bot-update --carpets-live                     ковры: боевой режим
#
# Токены и пароль базы в /opt/raketa-admin-bot/.env не трогаются никогда —
# меняются только два выключателя, и каждый раз печатается итоговое состояние.

set -euo pipefail

ENABLED=""
DRY_RUN=""
REPORT=""
BACKLOG_FROM=""
CARPETS=""
CARPETS_DRY=""
for arg in "$@"; do
    case "$arg" in
        --enable)    ENABLED=1 ;;
        --disable)   ENABLED=0 ;;
        --live)      DRY_RUN=0 ;;
        --rehearsal) DRY_RUN=1 ;;
        --report)    REPORT=1 ;;
        --backlog-from=*) BACKLOG_FROM="${arg#*=}" ;;
        --carpets-on)        CARPETS=1 ;;
        --carpets-off)       CARPETS=0 ;;
        --carpets-live)      CARPETS_DRY=0 ;;
        --carpets-rehearsal) CARPETS_DRY=1 ;;
        *) echo "Неизвестный ключ: $arg" >&2; exit 2 ;;
    esac
done

# --report: только показать, что робот записал. Ничего не меняем и не перезапускаем.
if [ -n "$REPORT" ]; then
    DSN=$(grep '^ADMINBOT_DB_DSN=' /opt/raketa-admin-bot/.env | cut -d= -f2-)
    cd /tmp
    echo "=== Заказы в работе робота ==="
    sudo -u adminbot psql "$DSN" -P pager=off -c "
        SELECT order_id AS заказ, status AS состояние, path AS путь,
               real_lead_id AS сделка, primary_lead_id AS лид,
               left(coalesce(last_error, ''), 40) AS ошибка,
               to_char(updated_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS обновлено
        FROM adminbot.amo_links ORDER BY order_id"
    echo "=== Ковры от партнёра ==="
    sudo -u adminbot psql "$DSN" -P pager=off -c "
        SELECT partner_id AS \"заказ партнёра\", status AS состояние, path AS путь,
               lead_id AS \"ковровая сделка\", primary_lead_id AS лид,
               left(coalesce(last_error, ''), 30) AS ошибка,
               to_char(updated_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS обновлено
        FROM adminbot.carpet_links ORDER BY partner_id"
    echo "=== Последние действия в amoCRM ==="
    sudo -u adminbot psql "$DSN" -P pager=off -c "
        SELECT order_id AS заказ, action AS действие, amo_id AS объект,
               CASE WHEN dry_run THEN 'репетиция' ELSE 'боевое' END AS режим,
               to_char(created_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS когда
        FROM adminbot.amo_actions ORDER BY id DESC LIMIT 25"
    exit 0
fi

HOME_DIR=/opt/raketa-admin-bot
APP_DIR=$HOME_DIR/app
SRC_DIR=/home/admin/raketa-admin-bot
ENV_FILE=$HOME_DIR/.env

say() { printf '\n=== %s ===\n' "$1"; }

say "1. Код"
rsync -a --delete --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
      --exclude '.pytest_cache' "$SRC_DIR/" "$APP_DIR/"
chown -R adminbot:adminbot "$APP_DIR"
echo "обновлено из $SRC_DIR"

say "2. Право обновлять без пароля"
# Рабочая копия скрипта лежит в системном каталоге: менять её может только root,
# а запускать разрешено пользователю admin. Ставим её из свежескопированного кода,
# поэтому скрипт обновляет сам себя и правило не приходится трогать руками.
INSTALLED=/usr/local/sbin/raketa-admin-bot-update
if ! cmp -s "$APP_DIR/deploy/update.sh" "$INSTALLED"; then
    install -o root -g root -m 0755 "$APP_DIR/deploy/update.sh" "$INSTALLED"
    echo "скрипт обновлён"
else
    echo "скрипт уже свежий"
fi
cat > /etc/sudoers.d/raketa-admin-bot <<'SUDO'
admin ALL=(root) NOPASSWD: /usr/local/sbin/raketa-admin-bot-update
admin ALL=(root) NOPASSWD: /usr/bin/systemctl restart raketa-admin-bot.service, /usr/bin/systemctl start raketa-admin-bot.service, /usr/bin/systemctl stop raketa-admin-bot.service, /usr/bin/systemctl status raketa-admin-bot.service
SUDO
chmod 0440 /etc/sudoers.d/raketa-admin-bot
visudo -c -q -f /etc/sudoers.d/raketa-admin-bot && echo "правило установлено и проверено"

say "3. Зависимости"
sudo -u adminbot "$HOME_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
echo "проверены"

say "4. Миграции"
DSN=$(grep '^ADMINBOT_DB_DSN=' "$ENV_FILE" | cut -d= -f2-)
cd /tmp
for sql in "$APP_DIR"/migrations/*.sql; do
    sudo -u adminbot psql "$DSN" -v ON_ERROR_STOP=1 -q -f "$sql"
    echo "  $(basename "$sql")"
done

say "5. Выключатели"
set_flag() {                                  # имя переменной, новое значение
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i "s/^$1=.*/$1=$2/" "$ENV_FILE"
    else
        echo "$1=$2" >> "$ENV_FILE"
    fi
}
[ -n "$ENABLED" ] && set_flag AMO_SYNC_ENABLED "$ENABLED"
[ -n "$DRY_RUN" ] && set_flag AMO_SYNC_DRY_RUN "$DRY_RUN"
[ -n "$BACKLOG_FROM" ] && set_flag AMO_SYNC_BACKLOG_FROM "$BACKLOG_FROM"
[ -n "$CARPETS" ] && set_flag CARPETS_ENABLED "$CARPETS"
[ -n "$CARPETS_DRY" ] && set_flag CARPETS_DRY_RUN "$CARPETS_DRY"

# Доступы к почте робота лежат отдельным файлом у admin. Переносим их в настройки
# службы один раз: сама служба читает только свой .env.
if ! grep -q "^MAIL_USER=" "$ENV_FILE" && [ -f /home/admin/.mail_robot.env ]; then
    grep -E "^MAIL_[A-Z_]+=" /home/admin/.mail_robot.env >> "$ENV_FILE"
    echo "доступы к почте перенесены в настройки службы"
fi

now_enabled=$(grep '^AMO_SYNC_ENABLED=' "$ENV_FILE" | cut -d= -f2-)
now_dry=$(grep '^AMO_SYNC_DRY_RUN=' "$ENV_FILE" | cut -d= -f2-)
echo "функция: $([ "$now_enabled" = 1 ] && echo 'ВКЛЮЧЕНА' || echo 'выключена')"
echo "режим:   $([ "$now_dry" = 1 ] && echo 'репетиция (в amoCRM не пишем)' || echo 'БОЕВОЙ (пишем в amoCRM)')"
echo "хвост с: $(grep "^AMO_SYNC_BACKLOG_FROM=" "$ENV_FILE" | cut -d= -f2-)"
now_carpets=$(grep "^CARPETS_ENABLED=" "$ENV_FILE" | cut -d= -f2-)
now_carpets_dry=$(grep "^CARPETS_DRY_RUN=" "$ENV_FILE" | cut -d= -f2-)
echo "ковры:   $([ "$now_carpets" = 1 ] && echo "ВКЛЮЧЕНЫ, $([ "$now_carpets_dry" = 1 ] && echo 'репетиция' || echo 'БОЕВОЙ режим')" || echo 'выключены')"

say "6. Перезапуск"
systemctl restart raketa-admin-bot.service
sleep 4
systemctl is-active raketa-admin-bot.service

say "7. Журнал"
journalctl -u raketa-admin-bot.service -n 15 --no-pager | tail -15
