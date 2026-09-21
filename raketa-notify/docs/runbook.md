# Служба оповещений: установка, включение, разбор полётов

ТЗ: `docs/plans/2026-09-18-notifications-bus.md` (задача 11).
Устройство журнального переходника подробнее — `journal-adapter.md` рядом.

Правило этого файла: **служба ставится выключенной**. Установка и включение —
два разных действия, и второе делается отдельным решением владельца.

---

## 1. Два файла настроек

Настройки службы разделены (решение владельца 21.09):

- `/opt/raketa-notify/.env` — **секреты**: токены, пароль базы. `root:notify`,
  права `640`, правит только владелец. Агент их не ищет, не печатает и не вводит.
- `/opt/raketa-notify/switches.env` — **выключатели** шагов выката. Секретов нет,
  `admin:admin`, права `644`, правит агент. Образец — `switches.env.example`.

Юнит читает оба файла, выключатели вторым — поэтому значение из `switches.env`
выигрывает, даже если такое же осталось в `.env`.

Эти значения владелец вносит в `.env` сам:

| Переменная | Что это |
|---|---|
| `NOTIFY_DB_DSN` | строка подключения роли `notify` (выдана при заведении роли) |
| `MY_ADMIN_TG_TOKEN` | токен бота My_admin |
| `TECH_JOURNAL_CHAT_ID` | номер технического чата (вида `-100…`) |
| `TELEGRAM_API_IPS`, `TELEGRAM_PROXY_URL` | копируются из `.env` рабочего бота как есть |
| `MY_ADMIN_OWNER_TG_ID` | Telegram id владельца — кому разрешено нажимать кнопки инцидентов |

Остальное — `WORKER_TG_TOKEN`, `ADMINBOT_TG_TOKEN`, `OPS_FEED_CHAT_ID`,
`MY_ASSISTANT_CHAT_ID`, `MANAGER_CHAT_ID` — тоже копируются из настроек ботов.
Образец со всеми именами: `.env.example`.

---

## 2. Установка

Своего репозитория у службы нет: она лежит в том же монорепозитории, в папке
`raketa-notify/`. Значит код едет на сервер тем же путём, что код рабочего
бота, — через `git pull` в его рабочую копию, а оттуда копируется к себе.
Ничего пересылать с ноутбука не нужно.

```bash
# 0. Свежий код в рабочей копии рабочего бота — там же лежит и служба.
#    Код самого бота при этом не меняется, перезапускать его не нужно.
cd /opt/telegram-bot
git pull --ff-only origin main

# 1. Пользователь, под которым работает служба (без него она не запустится).
#    Тот же приём, что у админ-бота: системный, без входа в систему.
sudo useradd --system --shell /usr/sbin/nologin --home /opt/raketa-notify notify || true

# 2. Код службы
sudo mkdir -p /opt/raketa-notify
sudo rsync -a --delete /opt/telegram-bot/raketa-notify/ /opt/raketa-notify/app/

# 3. Окружение. Каталог принадлежит root, поэтому обе команды под sudo.
sudo python3 -m venv /opt/raketa-notify/.venv
sudo /opt/raketa-notify/.venv/bin/pip install -r /opt/raketa-notify/app/requirements.txt

# 4. Настройки (заполняются по разделу 1). В файле токены и пароль базы,
#    поэтому читать его может только служба.
sudo cp /opt/raketa-notify/app/.env.example /opt/raketa-notify/.env
sudo chown root:notify /opt/raketa-notify/.env
sudo chmod 640 /opt/raketa-notify/.env
sudo nano /opt/raketa-notify/.env

# 5. Выключатели — отдельным файлом, без секретов, правится без sudo
sudo cp /opt/raketa-notify/app/switches.env.example /opt/raketa-notify/switches.env
sudo chown admin:admin /opt/raketa-notify/switches.env
sudo chmod 644 /opt/raketa-notify/switches.env

# 6. Служба
sudo cp /opt/raketa-notify/app/deploy/raketa-notify.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable raketa-notify.service
```

**Если `git pull` ругается на неотслеживаемый файл** — значит на сервере лежит
что-то, положенное руками (так было 17.09 со скриптом разведки). Сдвинуть его
в сторону, не удаляя, и повторить.

## 3. Миграции — до запуска кода

Применяются **ролью `bot`** (она суперпользователь; только так новые таблицы
получают права для остальных ролей), обе по очереди:

```bash
export DSN_AS_BOT='postgresql://bot:ПАРОЛЬ@127.0.0.1:5432/clients_db'
psql "$DSN_AS_BOT" -f /opt/raketa-notify/app/migrations/001_notify_schema.sql
psql "$DSN_AS_BOT" -f /opt/raketa-notify/app/migrations/002_watchdog_schema.sql
```

Обе идемпотентны: повторный прогон ничего не ломает. Роль `notify` должна
существовать до первой миграции, иначе она остановится с понятным сообщением.

## 4. Первый запуск — выключенной

```bash
sudo systemctl start raketa-notify.service
journalctl -u raketa-notify.service -n 30 --no-pager
```

Ожидаемое в журнале: служба поднялась и говорит, что выключена и ничего не шлёт.
**Это правильный результат первого запуска.** Если она вместо этого ругается на
настройки — в сообщении названа конкретная недостающая переменная.

---

## 5. Включение — по шагам, не разом

Каждый шаг: поправить **`switches.env`** (не `.env` — там только секреты),
`sudo systemctl restart raketa-notify.service`, посмотреть журнал. Между шагами —
пауза хотя бы в сутки, чтобы увидеть, как ведёт себя предыдущий.

Исключение — шаг 4: пульс живёт в настройках самих ботов, см. ниже.

| Шаг | Что включаем | Переменные | Что должно произойти |
|---|---|---|---|
| 1 | репетиция почтальона | `NOTIFY_ENABLED=1`, `NOTIFY_DRY_RUN=1` | в журнале «отправила бы…», ни одного сообщения в чатах |
| 2 | боевая доставка | `NOTIFY_DRY_RUN=0` | события из ящика доходят до чатов |
| 3 | технический журнал | `NOTIFY_JOURNAL_ENABLED=1` | в технический чат идут записи с `#тегами`, без звука |
| 4 | пульс ботов — **в настройках самих ботов, не службы** (см. ниже) | `SERVICE_HEARTBEAT_ENABLED=1` (рабочий бот), `HEARTBEAT_ENABLED=1` (админ-бот) | отметки «жив» обновляются раз в минуту |
| 5 | сторож | `NOTIFY_WATCHDOG_ENABLED=1` | в журнале службы результаты семи проверок |
| 6 | инциденты и приём кнопок | `NOTIFY_INCIDENTS_ENABLED=1`, `NOTIFY_MY_ADMIN_ENABLED=1`, `MY_ADMIN_OWNER_TG_ID=<ваш id>` | поломка доходит до My_admin с кнопкой, и **кнопка работает** |

Шаги 4 и 5 **нельзя менять местами**: сторож, включённый раньше пульса, увидит
молчащие отметки и заведёт ложные поломки.

**Шаг 4 делается не так, как остальные.** Эти две переменные живут в настройках самих
ботов, а не службы, и перезапуск `raketa-notify.service` на них никак не влияет:

```bash
# рабочий бот: правим /opt/telegram-bot/.env, затем
sudo systemctl restart telegram-bot.service

# админ-бот: правим его .env, затем ДВА шага (простой restart поднимет старую версию)
rsync -a ~/Projects/raketaclean/raketa-admin-bot/ /home/admin/raketa-admin-bot/
sudo raketa-admin-bot-update
```

**Шаг 6 без `NOTIFY_MY_ADMIN_ENABLED` и `MY_ADMIN_OWNER_TG_ID` включать нельзя.** Кнопка
нарисуется, но нажимать её будет некому: красная тревога продолжит приходить каждые
10 минут (ночью, с 00:00 до 08:00 МСК, — одним сообщением), а `/status` не ответит.

Выключить любой шаг — вернуть переменную в `0` в `switches.env` и перезапустить
службу.

---

## 6. Маршруты: куда что идёт

Справочник правится без программиста, изменения подхватываются без перезапуска:

```bash
cd /opt/raketa-notify/app
../.venv/bin/python -m notifyd.routes_cli list
../.venv/bin/python -m notifyd.routes_cli set-address <вид события> ops_feed
../.venv/bin/python -m notifyd.routes_cli set-level   <вид события> yellow
../.venv/bin/python -m notifyd.routes_cli disable     <вид события>
```

Адреса: `work_chat`, `ops_feed`, `tech_journal`, `my_assistant`, `my_admin`,
`manager`. Уровни: `red`, `yellow`, `grey`.

Вид события без маршрута не теряется: уходит в технический журнал с тегом
`#неизвестный-вид` и строкой в журнал службы.

Маршруты служба заводит сама при первой надобности и **больше их не трогает**:
адрес, уровень и тег, поставленные руками, переживают перезапуск.

Уровни сторожевых проверок по умолчанию (решение владельца 19.09): красные —
`база данных`, `прокси`, `пульс рабочего бота`, `пульс клиентского бота`,
`рассыльщик клиентам`, `опрос amoCRM`; жёлтый — `пульс админ-бота`. Серый им
поставить нельзя: поломка бывает красной или жёлтой, а чтобы сигнал замолчал
совсем, есть `disable`. Смена уровня действует сразу, в том числе на уже
идущую поломку.

Свои виды есть и у самой службы: `notify.journal` — технический журнал,
`notify.incident.escalation` — дубль поломки, не закрытой за час,
`notify.incident.blip` — «мигнуло»: проверка упала и поднялась сама, не
продержавшись трёх проходов. Тревоги по такому не будет, только строка
в журнале, и не чаще раза в десять минут по одному виду поломки.

---

## 7. Если служба молчит

Смотреть по порядку — сверху вниз, первое совпадение и есть причина:

1. **Жива ли она вообще:** `systemctl is-active raketa-notify.service`.
2. **Что говорит сама:** `journalctl -u raketa-notify.service -n 50 --no-pager`.
   Собственные логи службы идут только сюда — самонаблюдения у неё нет
   (решение владельца): если сообщения перестали приходить, смотреть надо здесь.
3. **Копится ли ящик:** `psql "$DSN_AS_BOT" -c "SELECT status, count(*) FROM notify.outbox GROUP BY 1"`.
   Растущий `pending` — служба не забирает; пусто — события никто не кладёт.
4. **Не выключена ли:** `grep ENABLED /opt/raketa-notify/.env`.
5. **Есть ли маршрут** у вида события: `routes_cli list`.
6. **Дорога до Telegram:** если лёг прокси, служба не отправит ничего — и о самом
   прокси сообщить тоже не сможет: запасного пути в обход у неё нет. Это
   известное ограничение, проверяется со стороны сервера.

## 8. Обновление

```bash
cd /opt/telegram-bot && git pull --ff-only origin main
sudo rsync -a --delete /opt/telegram-bot/raketa-notify/ /opt/raketa-notify/app/
sudo /opt/raketa-notify/.venv/bin/pip install -r /opt/raketa-notify/app/requirements.txt
sudo systemctl restart raketa-notify.service
```

Строка с `pip` нужна, только если менялся `requirements.txt`; лишней она
не будет — уже установленное переставлять не станет.

Обновление не трогает ни `.env`, ни `switches.env`: оба лежат уровнем выше
каталога `app`, куда идёт `rsync`.

Появились новые миграции — применять по разделу 3 **до** перезапуска.
