#!/usr/bin/env bash
# Обновление админ-бота: код из рабочей копии → служба. Выполняется от root:
#   sudo bash /tmp/adminbot_update.sh          обновить код и перезапустить
#   sudo raketa-admin-bot-update --enable      включить функцию amo_sync
#   sudo raketa-admin-bot-update --disable     выключить функцию
#   sudo raketa-admin-bot-update --live        боевой режим: писать в amoCRM
#   sudo raketa-admin-bot-update --rehearsal   репетиция: решать, но не писать
#   sudo raketa-admin-bot-update --backlog-from=2026-08-14   с какой даты разбирать заказы
#   sudo raketa-admin-bot-update --report      что робот записал (ничего не меняет)
#   sudo raketa-admin-bot-update --address-reminder-on|--address-reminder-off
#                                             напоминания «сделка без адреса»
#   sudo raketa-admin-bot-update --deletions-on --deletions-rehearsal   удаления заказов: репетиция
#   sudo raketa-admin-bot-update --deletions-live                      удаления заказов: боевой режим
#   sudo raketa-admin-bot-update --deletions-off                       удаления заказов: выключить
#   sudo raketa-admin-bot-update --carpets-on --carpets-rehearsal   ковры: репетиция
#   sudo raketa-admin-bot-update --carpets-live                     ковры: боевой режим
#   sudo raketa-admin-bot-update --carpets-held                     ковры: какие письма отложены и почему
#   sudo raketa-admin-bot-update --carpets-release=UID              ковры: провести отложенное письмо
#   sudo raketa-admin-bot-update --carpets-remember=ФАЙЛ             ковры: посчитать заказы в архивном файле партнёра
#   sudo raketa-admin-bot-update --carpets-remember=ФАЙЛ --carpets-remember-live   ковры: запомнить их как сделанные
#   sudo raketa-admin-bot-update --cleaning-on --cleaning-rehearsal уборки: репетиция
#   sudo raketa-admin-bot-update --cleaning-live                    уборки: боевой режим
#   sudo raketa-admin-bot-update --cleaning-off                     уборки: выключить
#   sudo raketa-admin-bot-update --cleaning-from=2026-09-11         с какой даты проводить уборки
#   sudo raketa-admin-bot-update --gcal-on --gcal-rehearsal         календарь: репетиция
#   sudo raketa-admin-bot-update --gcal-live                        календарь: боевой режим
#   sudo raketa-admin-bot-update --gcal-check                       проверить доступ к календарям
#   sudo raketa-admin-bot-update --gcal-calendars=A,B               какие календари читать (через запятую, без пробелов)
#   sudo raketa-admin-bot-update --gcal-run=ID[,ID]                 провести эти записи календаря
#   sudo raketa-admin-bot-update --gcal-run=ID --gcal-preview       то же, но без записи в CRM
#   sudo raketa-admin-bot-update --gcal-forget=ID[,ID]              показать, что робот помнит об этих записях
#   sudo raketa-admin-bot-update --gcal-forget=ID --gcal-forget-live  забыть их (сделки снова свободны)
#   sudo raketa-admin-bot-update --autocall-exam   экзамен фильтра заявок с сайта (читает CRM, ничего не меняет)
#   sudo raketa-admin-bot-update --autocall-on --autocall-rehearsal   автозвонок: репетиция
#   sudo raketa-admin-bot-update --autocall-live                      автозвонок: боевой режим
#   sudo raketa-admin-bot-update --autocall-off                       автозвонок: выключить
#   sudo raketa-admin-bot-update --manager-dials=8930...,8986...      телефоны менеджера: рабочий, потом личный (через запятую, без пробелов)
#   sudo raketa-admin-bot-update --telegram-proxy=http://user:pass@host:port  трафик до Telegram через прокси вне РФ
#   sudo raketa-admin-bot-update --telegram-proxy-off                 вернуть прямой путь до Telegram
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
CARPETS_HELD=""
CARPETS_RELEASE=""
CARPETS_RELEASE_SET=""
CARPETS_REMEMBER=""
CARPETS_REMEMBER_SET=""
CARPETS_REMEMBER_LIVE=""
CLEANING=""
ADDRESS_REMINDER=""
DELETIONS=""
DELETIONS_DRY=""
CLEANING_DRY=""
CLEANING_FROM=""
GCAL=""
GCAL_DRY=""
GCAL_CHECK=""
GCAL_CALENDARS=""
GCAL_RUN=""
GCAL_PREVIEW=""
GCAL_RESET=""
GCAL_FORGET=""
GCAL_FORGET_LIVE=""
SHOW_LEADS=""
SHOW_STALE=""
CLOSE_STALE=""
CLOSE_STALE_LIVE=""
CLOSE_STALE_DAYS=""
CLOSE_STALE_LIMIT=""
CLOSE_STALE_SUCCESS_FROM=""
SHOW_LEAD_IDS=""
AUTOCALL_EXAM=""
AUTOCALL=""
AUTOCALL_DRY=""
MANAGER_DIALS=""
TELEGRAM_PROXY=""
TELEGRAM_PROXY_SET=""
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
        --carpets-held)      CARPETS_HELD=1 ;;
        --carpets-release=*) CARPETS_RELEASE="${arg#*=}"; CARPETS_RELEASE_SET=1 ;;
        --carpets-remember=*) CARPETS_REMEMBER="${arg#*=}"; CARPETS_REMEMBER_SET=1 ;;
        --carpets-remember-live) CARPETS_REMEMBER_LIVE=1 ;;
        --cleaning-on)        CLEANING=1 ;;
        --address-reminder-on)  ADDRESS_REMINDER=1 ;;
        --address-reminder-off) ADDRESS_REMINDER=0 ;;
        --deletions-on)        DELETIONS=1 ;;
        --deletions-off)       DELETIONS=0 ;;
        --deletions-live)      DELETIONS_DRY=0 ;;
        --deletions-rehearsal) DELETIONS_DRY=1 ;;
        --cleaning-off)       CLEANING=0 ;;
        --cleaning-live)      CLEANING_DRY=0 ;;
        --cleaning-rehearsal) CLEANING_DRY=1 ;;
        --cleaning-from=*)    CLEANING_FROM="${arg#*=}" ;;
        --gcal-on)        GCAL=1 ;;
        --gcal-off)       GCAL=0 ;;
        --gcal-live)      GCAL_DRY=0 ;;
        --gcal-rehearsal) GCAL_DRY=1 ;;
        --gcal-check)     GCAL_CHECK=1 ;;
        --gcal-calendars=*) GCAL_CALENDARS="${arg#*=}" ;;
        --gcal-run=*)     GCAL_RUN="${arg#*=}" ;;
        --gcal-preview)   GCAL_PREVIEW=1 ;;
        --gcal-reset)     GCAL_RESET=1 ;;
        --gcal-forget=*)  GCAL_FORGET="${arg#*=}" ;;
        --gcal-forget-live) GCAL_FORGET_LIVE=1 ;;
        --leads=*)        SHOW_LEADS="${arg#*=}" ;;
        --stale)          SHOW_STALE=1 ;;
        --close-stale)       CLOSE_STALE=1 ;;
        --close-stale-live)  CLOSE_STALE=1; CLOSE_STALE_LIVE=1 ;;
        --close-stale-days=*)  CLOSE_STALE_DAYS="${arg#*=}" ;;
        --close-stale-limit=*) CLOSE_STALE_LIMIT="${arg#*=}" ;;
        --close-stale-success-from=*) CLOSE_STALE_SUCCESS_FROM="${arg#*=}" ;;
        --lead-ids=*)     SHOW_LEAD_IDS="${arg#*=}" ;;
        --autocall-exam)  AUTOCALL_EXAM=1 ;;
        --autocall-on)        AUTOCALL=1 ;;
        --autocall-off)       AUTOCALL=0 ;;
        --autocall-live)      AUTOCALL_DRY=0 ;;
        --autocall-rehearsal) AUTOCALL_DRY=1 ;;
        --manager-dials=*)    MANAGER_DIALS="${arg#*=}" ;;
        --telegram-proxy=*)   TELEGRAM_PROXY="${arg#*=}"; TELEGRAM_PROXY_SET=1 ;;
        --telegram-proxy-off) TELEGRAM_PROXY=""; TELEGRAM_PROXY_SET=1 ;;
        *) echo "Неизвестный ключ: $arg" >&2; exit 2 ;;
    esac
done

# Ключ без своей пары — это опечатка, а не команда. Без проверки такой запуск
# доходил бы до полного обновления кода и перезапуска боевой службы: владелец
# просил показать письмо, а получил бы деплой.
if [ -n "$CARPETS_REMEMBER_LIVE" ] && [ -z "$CARPETS_REMEMBER" ]; then
    echo "--carpets-remember-live без --carpets-remember=<файл>: запоминать нечего." >&2
    exit 2
fi
if [ -n "$CARPETS_REMEMBER_SET" ] && [ -z "$CARPETS_REMEMBER" ]; then
    echo "--carpets-remember= без пути к файлу: укажите архивный файл партнёра." >&2
    exit 2
fi
if [ -n "$CARPETS_RELEASE_SET" ] && [ -z "$CARPETS_RELEASE" ]; then
    echo "--carpets-release= без UID письма: UID берут из --carpets-held." >&2
    exit 2
fi

# --report завершает скрипт сразу после отчёта (exit 0 в блоке ниже) — второй
# ключ вроде --carpets-held молча проглатывался бы. Ключи несовместимы: говорим
# об этом прямо, а не выполняем один и делаем вид, что второго не было.
if [ -n "$REPORT" ] && { [ -n "$CARPETS_HELD" ] || [ -n "$CARPETS_RELEASE_SET" ]; }; then
    echo "--report нельзя совмещать с --carpets-held/--carpets-release=: " \
         "--report сам печатает состояние ковров и сразу завершает скрипт. " \
         "Запустите ключи по отдельности." >&2
    exit 2
fi

# Пробел внутри списка календарей позже разорвал бы строку при переносе настроек
# в окружение диагностики (там xargs), и робот пошёл бы читать несуществующий
# календарь. Ловим сразу, а не через сутки молчания.
case "$GCAL_CALENDARS" in
    *[[:space:]]*)
        echo "В списке календарей есть пробел: перечисляйте через запятую без пробелов." >&2
        exit 2 ;;
esac

# Та же беда с телефонами менеджера: строка уезжает в настройки службы как есть,
# а systemd на пробеле внутри значения обрывает строку — робот остался бы с одним
# номером и молча звонил бы только на него.
case "$MANAGER_DIALS" in
    *[[:space:]]*)
        echo "В списке телефонов есть пробел: перечисляйте через запятую без пробелов." >&2
        exit 2 ;;
esac

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
    # Заказы из архивных файлов партнёра робот не проводил, он их просто помнит,
    # и их сотни. В таблицу они не идут — иначе за ними не видно настоящей работы.
    REMEMBERED=$(sudo -u adminbot psql "$DSN" -tAc \
        "SELECT count(*) FROM adminbot.carpet_links WHERE path = 'remembered'")
    echo "Запомнено из файлов партнёра: $REMEMBERED (в таблицу ниже не входят)"
    sudo -u adminbot psql "$DSN" -P pager=off -c "
        SELECT partner_id AS \"заказ партнёра\", status AS состояние, path AS путь,
               lead_id AS \"ковровая сделка\", primary_lead_id AS лид,
               left(coalesce(last_error, ''), 30) AS ошибка,
               to_char(updated_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS обновлено
        FROM adminbot.carpet_links
        WHERE path IS DISTINCT FROM 'remembered' ORDER BY partner_id"
    echo "=== Записи календаря ==="
    sudo -u adminbot psql "$DSN" -P pager=off -c "
        SELECT event_id AS запись, kind AS вид, status AS состояние,
               order_date AS \"дата заказа\", real_lead_id AS сделка,
               left(coalesce(skip_reason, last_error, ''), 35) AS примечание,
               to_char(updated_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS обновлено
        FROM adminbot.gcal_events ORDER BY updated_at DESC LIMIT 25"
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
    # Разделитель sed — вертикальная черта, а не косая: адрес прокси
    # (http://user:pass@host:port) косыми чертами разорвал бы саму команду.
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
    else
        echo "$1=$2" >> "$ENV_FILE"
    fi
}
[ -n "$ENABLED" ] && set_flag AMO_SYNC_ENABLED "$ENABLED"
[ -n "$DRY_RUN" ] && set_flag AMO_SYNC_DRY_RUN "$DRY_RUN"
[ -n "$BACKLOG_FROM" ] && set_flag AMO_SYNC_BACKLOG_FROM "$BACKLOG_FROM"
[ -n "$CARPETS" ] && set_flag CARPETS_ENABLED "$CARPETS"
[ -n "$CARPETS_DRY" ] && set_flag CARPETS_DRY_RUN "$CARPETS_DRY"
[ -n "$CLEANING" ] && set_flag CLEANING_SYNC_ENABLED "$CLEANING"
[ -n "$ADDRESS_REMINDER" ] && set_flag ADDRESS_REMINDER_ENABLED "$ADDRESS_REMINDER"
[ -n "$DELETIONS" ] && set_flag ORDER_DELETIONS_ENABLED "$DELETIONS"
[ -n "$DELETIONS_DRY" ] && set_flag ORDER_DELETIONS_DRY_RUN "$DELETIONS_DRY"
[ -n "$CLEANING_DRY" ] && set_flag CLEANING_SYNC_DRY_RUN "$CLEANING_DRY"
[ -n "$CLEANING_FROM" ] && set_flag CLEANING_BACKLOG_FROM "$CLEANING_FROM"
[ -n "$TELEGRAM_PROXY_SET" ] && set_flag TELEGRAM_PROXY_URL "$TELEGRAM_PROXY"
[ -n "$GCAL" ] && set_flag GCAL_ENABLED "$GCAL"
[ -n "$GCAL_DRY" ] && set_flag GCAL_DRY_RUN "$GCAL_DRY"
[ -n "$GCAL_CALENDARS" ] && set_flag GCAL_CALENDAR_ID "$GCAL_CALENDARS"
[ -n "$AUTOCALL" ] && set_flag AUTOCALL_ENABLED "$AUTOCALL"
[ -n "$AUTOCALL_DRY" ] && set_flag AUTOCALL_DRY_RUN "$AUTOCALL_DRY"
[ -n "$MANAGER_DIALS" ] && set_flag PBX_MANAGER_DIAL "$MANAGER_DIALS"

# Доступы к почте робота лежат отдельным файлом у admin. Переносим их в настройки
# службы один раз: сама служба читает только свой .env.
if ! grep -q "^MAIL_USER=" "$ENV_FILE" && [ -f /home/admin/.mail_robot.env ]; then
    grep -E "^MAIL_[A-Z_]+=" /home/admin/.mail_robot.env >> "$ENV_FILE"
    echo "доступы к почте перенесены в настройки службы"
fi

# Ключ служебного аккаунта Google владелец кладёт себе в /home/admin/.gcal.json.
# Служба работает от другого пользователя, поэтому ключ переносим ей один раз.
GCAL_KEY=$HOME_DIR/.gcal.json
if [ ! -f "$GCAL_KEY" ] && [ -f /home/admin/.gcal.json ]; then
    install -o adminbot -g adminbot -m 0600 /home/admin/.gcal.json "$GCAL_KEY"
    set_flag GCAL_SERVICE_ACCOUNT_FILE "$GCAL_KEY"
    echo "ключ служебного аккаунта Google перенесён в настройки службы"
fi

# Ключ АТС и токен рабочего бота владелец кладёт себе в /home/admin/.pbx.env.
# Служба работает от другого пользователя, поэтому переносим их один раз.
if ! grep -q "^PBX_API_KEY=" "$ENV_FILE" && [ -f /home/admin/.pbx.env ]; then
    grep -E "^(PBX_|WORKER_TG_|MANAGER_TG_)" /home/admin/.pbx.env >> "$ENV_FILE"
    echo "доступы к АТС и токен рабочего бота перенесены в настройки службы"
fi

flag_of() { grep "^$1=" "$ENV_FILE" | cut -d= -f2- || true; }
now_enabled=$(flag_of AMO_SYNC_ENABLED)
now_dry=$(flag_of AMO_SYNC_DRY_RUN)
echo "функция: $([ "$now_enabled" = 1 ] && echo 'ВКЛЮЧЕНА' || echo 'выключена')"
echo "режим:   $([ "$now_dry" = 1 ] && echo 'репетиция (в amoCRM не пишем)' || echo 'БОЕВОЙ (пишем в amoCRM)')"
echo "хвост с: $(flag_of AMO_SYNC_BACKLOG_FROM)"
now_carpets=$(flag_of CARPETS_ENABLED)
now_carpets_dry=$(flag_of CARPETS_DRY_RUN)
echo "ковры:   $([ "$now_carpets" = 1 ] && echo "ВКЛЮЧЕНЫ, $([ "$now_carpets_dry" = 1 ] && echo 'репетиция' || echo 'БОЕВОЙ режим')" || echo 'выключены')"
now_cleaning=$(flag_of CLEANING_SYNC_ENABLED)
now_cleaning_dry=$(flag_of CLEANING_SYNC_DRY_RUN)
echo "уборки:  $([ "$now_cleaning" = 1 ] && echo "ВКЛЮЧЕНЫ, $([ "$now_cleaning_dry" = 1 ] && echo 'репетиция' || echo 'БОЕВОЙ режим')" || echo 'выключены')"
echo "уборки с: $(flag_of CLEANING_BACKLOG_FROM)"
now_deletions=$(flag_of ORDER_DELETIONS_ENABLED)
now_deletions_dry=$(flag_of ORDER_DELETIONS_DRY_RUN)
echo "удаления: $([ "$now_deletions" = 1 ] && echo "ВКЛЮЧЕНЫ, $([ "$now_deletions_dry" = 1 ] && echo 'репетиция' || echo 'БОЕВОЙ режим')" || echo 'выключены')"
now_gcal=$(flag_of GCAL_ENABLED)
now_gcal_dry=$(flag_of GCAL_DRY_RUN)
echo "календарь: $([ "$now_gcal" = 1 ] && echo "ВКЛЮЧЁН, $([ "$now_gcal_dry" = 1 ] && echo 'репетиция' || echo 'БОЕВОЙ режим')" || echo 'выключен')"
echo "календари: $(flag_of GCAL_CALENDAR_ID)"
now_autocall=$(flag_of AUTOCALL_ENABLED)
now_autocall_dry=$(flag_of AUTOCALL_DRY_RUN)
echo "автозвонок: $([ "$now_autocall" = 1 ] && echo "ВКЛЮЧЁН, $([ "$now_autocall_dry" = 1 ] && echo 'репетиция' || echo 'БОЕВОЙ режим')" || echo 'выключен')"
# Телефоны менеджера печатаем целиком: их видит только владелец у себя в консоли,
# а проверить порядок «рабочий, потом личный» иначе нечем.
echo "телефоны менеджера: $(flag_of PBX_MANAGER_DIAL)"
# Адрес прокси НЕ печатаем: в нём пароль, а вывод скрипта уходит в терминал
# и в историю. Достаточно знать, каким путём бот идёт до Telegram.
now_proxy=$(flag_of TELEGRAM_PROXY_URL)
echo "Telegram: $([ -n "$now_proxy" ] && echo 'через прокси' || echo 'напрямую')"

# Проверка доступа к календарю: читает три ближайшие записи и ничего не меняет.
if [ -n "$GCAL_CHECK" ]; then
    say "Проверка доступа к календарю"
    cd "$APP_DIR"
    # Настроек может не быть вовсе — тогда проверка сама скажет, чего не хватает.
    GCAL_VARS=$(grep -E "^GCAL_" "$ENV_FILE" | xargs || true)
    sudo -u adminbot env $GCAL_VARS \
        "$HOME_DIR/.venv/bin/python" -m scripts.check_calendar || true
fi

# Разовый прогон по названным записям календаря: заводит сделки по-настоящему
# (или показывает, что сделал бы, если добавлен --gcal-preview).
if [ -n "$GCAL_RUN" ]; then
    say "Прогон записей календаря"
    cd "$APP_DIR"
    ENV_VARS=$(grep -E "^(GCAL_|AMO_|ADMINBOT_|BOT_DB_)" "$ENV_FILE" | xargs || true)
    sudo -u adminbot env $ENV_VARS GCAL_RUN_IDS="$GCAL_RUN" \
        GCAL_RUN_PREVIEW="${GCAL_PREVIEW:-0}" GCAL_RUN_RESET="${GCAL_RESET:-0}" \
        "$HOME_DIR/.venv/bin/python" -m scripts.run_calendar || true
fi

# Забыть запись календаря: нужно, когда запись удалена из календаря, а сделку
# она за собой держит — вернувшийся заказ того же клиента иначе считается новым.
# Без --gcal-forget-live только показывает, что будет забыто.
if [ -n "$GCAL_FORGET" ]; then
    say "Память о записях календаря: $([ -n "$GCAL_FORGET_LIVE" ] && echo 'ЗАБЫВАЮ' || echo 'просмотр')"
    cd "$APP_DIR"
    ENV_VARS=$(grep -E "^(GCAL_|AMO_|ADMINBOT_|BOT_DB_)" "$ENV_FILE" | xargs || true)
    sudo -u adminbot env $ENV_VARS GCAL_FORGET_IDS="$GCAL_FORGET" \
        GCAL_FORGET_LIVE="${GCAL_FORGET_LIVE:-0}" \
        "$HOME_DIR/.venv/bin/python" -m scripts.forget_calendar || true
fi

# Отложенные письма партнёра: показать список или снять отложение с одного
# письма. Робот откладывает письмо, где наших строк больше порога или чьё
# вложение не разобралось; после снятия ближайший проход проведёт его как обычное.
if [ -n "$CARPETS_HELD" ] || [ -n "$CARPETS_RELEASE" ]; then
    say "Отложенные письма партнёра"
    cd "$APP_DIR"
    ENV_VARS=$(grep -E "^(GCAL_|AMO_|ADMINBOT_|BOT_DB_)" "$ENV_FILE" | xargs || true)
    sudo -u adminbot env $ENV_VARS CARPETS_RELEASE_UID="$CARPETS_RELEASE" \
        "$HOME_DIR/.venv/bin/python" -m scripts.held_letters || true
fi

# Архивный файл партнёра: пометить его заказы как уже сделанные, чтобы повтор
# старых строк в новых отчётах робот пропускал. В амо не ходит.
# Файл лежит у admin и пользователю adminbot недоступен, поэтому работаем
# с временной копией и убираем её в любом исходе: в файле телефоны клиентов.
if [ -n "$CARPETS_REMEMBER" ]; then
    say "Архивный файл партнёра: $([ -n "$CARPETS_REMEMBER_LIVE" ] && echo 'ЗАПОМИНАЮ' || echo 'просмотр')"
    if [ ! -f "$CARPETS_REMEMBER" ]; then
        echo "Файла нет: $CARPETS_REMEMBER" >&2
        exit 2
    fi
    CARPETS_TMP=$(mktemp /tmp/carpets_remember_XXXXXX.xlsx)
    trap 'rm -f "$CARPETS_TMP"' EXIT
    cp "$CARPETS_REMEMBER" "$CARPETS_TMP"
    chown adminbot "$CARPETS_TMP"
    chmod 600 "$CARPETS_TMP"
    cd "$APP_DIR"
    ENV_VARS=$(grep -E "^(GCAL_|AMO_|ADMINBOT_|BOT_DB_)" "$ENV_FILE" | xargs || true)
    # В базе должно остаться имя файла партнёра, а не временной копии:
    # по нему потом видно, откуда взялась запись.
    sudo -u adminbot env $ENV_VARS CARPETS_REMEMBER_FILE="$CARPETS_TMP" \
        CARPETS_REMEMBER_NAME="$(basename "$CARPETS_REMEMBER")" \
        CARPETS_REMEMBER_LIVE="${CARPETS_REMEMBER_LIVE:-0}" \
        "$HOME_DIR/.venv/bin/python" -m scripts.remember_carpets || true
    rm -f "$CARPETS_TMP"
    trap - EXIT
fi

# Диагностика: какие сделки у клиента в CRM. Ничего не меняет.
if [ -n "$SHOW_LEADS" ] || [ -n "$SHOW_LEAD_IDS" ]; then
    say "Сделки клиента"
    cd "$APP_DIR"
    ENV_VARS=$(grep -E "^(GCAL_|AMO_|ADMINBOT_|BOT_DB_)" "$ENV_FILE" | xargs || true)
    sudo -u adminbot env $ENV_VARS SHOW_LEADS_PHONE="$SHOW_LEADS" \
        SHOW_LEADS_IDS="$SHOW_LEAD_IDS" \
        "$HOME_DIR/.venv/bin/python" -m scripts.show_leads || true
fi

# Сколько в CRM висит незакрытых сделок и какого они возраста. Только чтение.
if [ -n "$SHOW_STALE" ]; then
    say "Незакрытые сделки в CRM"
    cd "$APP_DIR"
    ENV_VARS=$(grep -E "^(GCAL_|AMO_|ADMINBOT_|BOT_DB_)" "$ENV_FILE" | xargs || true)
    sudo -u adminbot env $ENV_VARS \
        "$HOME_DIR/.venv/bin/python" -m scripts.show_stale || true
fi

# Чистка забытых сделок. Без --close-stale-live только показывает список.
if [ -n "$CLOSE_STALE" ]; then
    say "Забытые сделки: $([ -n "$CLOSE_STALE_LIVE" ] && echo 'ЗАКРЫТИЕ' || echo 'просмотр')"
    cd "$APP_DIR"
    ENV_VARS=$(grep -E "^(GCAL_|AMO_|ADMINBOT_|BOT_DB_)" "$ENV_FILE" | xargs || true)
    sudo -u adminbot env $ENV_VARS \
        CLOSE_STALE_LIVE="${CLOSE_STALE_LIVE:-0}" \
        CLOSE_STALE_DAYS="$CLOSE_STALE_DAYS" \
        CLOSE_STALE_LIMIT="$CLOSE_STALE_LIMIT" \
        CLOSE_STALE_SUCCESS_FROM="$CLOSE_STALE_SUCCESS_FROM" \
        "$HOME_DIR/.venv/bin/python" -m scripts.close_stale || true
fi

# Экзамен фильтра заявок с сайта: читает CRM, ничего не меняет.
if [ -n "$AUTOCALL_EXAM" ]; then
    say "Экзамен заявок с сайта"
    cd "$APP_DIR"
    ENV_VARS=$(grep -E "^(GCAL_|AMO_|ADMINBOT_|BOT_DB_)" "$ENV_FILE" | xargs || true)
    sudo -u adminbot env $ENV_VARS \
        "$HOME_DIR/.venv/bin/python" -m scripts.autocall_exam || true
fi

# Диагностика ничего не меняет — перезапускать из-за неё боевую службу незачем.
if [ -n "$GCAL_CHECK$SHOW_LEADS$SHOW_LEAD_IDS$AUTOCALL_EXAM$SHOW_STALE$CLOSE_STALE$GCAL_FORGET$CARPETS_HELD$CARPETS_RELEASE$CARPETS_REMEMBER$CARPETS_REMEMBER_LIVE" ] && [ -z "$GCAL_RUN" ] \
        && [ -z "$ENABLED$DRY_RUN$CARPETS$CARPETS_DRY$GCAL$GCAL_DRY$BACKLOG_FROM$AUTOCALL$AUTOCALL_DRY" ]; then
    echo
    echo "Служба не перезапускалась: это была только проверка."
    exit 0
fi

say "6. Перезапуск"
systemctl restart raketa-admin-bot.service
sleep 4
systemctl is-active raketa-admin-bot.service

say "7. Журнал"
journalctl -u raketa-admin-bot.service -n 15 --no-pager | tail -15
