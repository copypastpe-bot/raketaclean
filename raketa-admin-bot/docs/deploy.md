# Установка админ-бота на сервер

Что это. Отдельная служба на том же сервере, где работает бот мастеров. Она читает
базу рабочего бота (только читает) и оформляет сделки в amoCRM. Своё состояние
держит в отдельной схеме `adminbot` той же базы.

Куда ставим:

```text
сервер:        admin@91.200.150.68
служба:        raketa-admin-bot.service
команда:       python -m adminbot.main
пользователь:  adminbot (системный, без входа в систему)
база:          postgresql://adminbot@127.0.0.1:5432/clients_db

/opt/raketa-admin-bot/          дом службы
├── app/                        код из git
├── .venv/                      окружение python
├── .env                        настройки и токены (права 600)
└── .ssh/                       ключ доступа к репозиторию
```

Порядок ниже рассчитан на первый запуск. Шаги с `sudo` без NOPASSWD выполняет
владелец: у робота пароля нет и быть не должно.

## Короткий путь: один скрипт

Шаги 1-7 собраны в `deploy/install.sh`. Повторный запуск безопасен. Перед ним
на сервере должны лежать два файла с доступами (права 600, владелец `admin`):

```text
~/.adminbot.env    ADMINBOT_TG_TOKEN=…  и  ADMINBOT_OWNER_TG_ID=…
~/.amo_write.env   AMOCRM_API_TOKEN=…   (токен интеграции «Робот amo_sync»)
```

Запуск (флаг `-t` нужен, чтобы `sudo` мог спросить пароль):

```bash
scp deploy/install.sh admin@91.200.150.68:/tmp/adminbot_install.sh
ssh -t admin@91.200.150.68 'sudo bash /tmp/adminbot_install.sh 2>&1 | tee /tmp/install.log'
```

Пароль пользователя базы скрипт генерирует сам и кладёт в `.env` — набирать
и пересылать его не нужно. Ниже те же шаги подробно, на случай ручной установки
или разбора, если что-то пойдёт не так.

---

## 0. Что нужно приготовить заранее

1. **Токен бота в Telegram.** В @BotFather: `/newbot` → имя и адрес бота →
   вы получите строку вида `8123456789:AA...`. Это `ADMINBOT_TG_TOKEN`.
2. **Свой Telegram id.** Напишите @userinfobot — он ответит числом.
   Это `ADMINBOT_OWNER_TG_ID`, только этот человек сможет управлять роботом.
3. **Токен amoCRM с правом записи.** Уже есть: интеграция «Робот amo_sync»,
   токен лежит на сервере в `~/.amo_write.env`.
4. **Пароль для пользователя базы** — любой длинный, придумайте и сохраните.

---

## 1. Пользователь в системе

```bash
sudo useradd --system --shell /usr/sbin/nologin --home /opt/raketa-admin-bot adminbot
sudo mkdir -p /opt/raketa-admin-bot
sudo chown adminbot:adminbot /opt/raketa-admin-bot
```

Отдельный пользователь нужен по одной причине: служба ходит в боевую CRM с правом
записи, и ей незачем иметь права на самом сервере.

## 2. Код на сервере

Репозиторий приватный, поэтому серверу нужен свой ключ доступа (только чтение).

```bash
sudo -u adminbot mkdir -p /opt/raketa-admin-bot/.ssh
sudo -u adminbot ssh-keygen -t ed25519 -N "" -f /opt/raketa-admin-bot/.ssh/id_ed25519
sudo cat /opt/raketa-admin-bot/.ssh/id_ed25519.pub
```

Показанную строку добавьте на GitHub: репозиторий `raketa-admin-bot` →
Settings → Deploy keys → Add deploy key (галочку «Allow write access» НЕ ставить).

```bash
sudo -u adminbot git clone git@github.com:copypastpe-bot/raketa-admin-bot.git /opt/raketa-admin-bot/app
```

Запасной путь, если с ключом не сложилось, — скопировать код с ноутбука:

```bash
rsync -a --exclude '.venv' --exclude '.git' ~/Projects/raketa-admin-bot/ admin@91.200.150.68:/tmp/adminbot-src/
sudo rsync -a /tmp/adminbot-src/ /opt/raketa-admin-bot/app/
sudo chown -R adminbot:adminbot /opt/raketa-admin-bot
```

## 3. Своё окружение Python

```bash
sudo -u adminbot python3 -m venv /opt/raketa-admin-bot/.venv
sudo -u adminbot /opt/raketa-admin-bot/.venv/bin/pip install -q -r /opt/raketa-admin-bot/app/requirements.txt
sudo -u adminbot /opt/raketa-admin-bot/.venv/bin/python -c "import aiogram, asyncpg; print('зависимости на месте')"
```

Проверено на Python 3.10.12 — том же, что стоит на сервере.

## 4. Пользователь базы и схема

Выполняется от суперпользователя Postgres. Читать чужие таблицы можно, писать в них — нет.

```sql
-- 1) пользователь службы
CREATE ROLE adminbot LOGIN PASSWORD 'ПРИДУМАННЫЙ_ПАРОЛЬ';

-- 2) чтение таблиц рабочего бота, и только чтение
GRANT USAGE ON SCHEMA public TO adminbot;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO adminbot;
ALTER DEFAULT PRIVILEGES FOR ROLE bot IN SCHEMA public GRANT SELECT ON TABLES TO adminbot;

-- 3) собственная схема робота
CREATE SCHEMA IF NOT EXISTS adminbot AUTHORIZATION adminbot;

-- 4) право создавать схемы: миграция 001 начинается с CREATE SCHEMA IF NOT EXISTS,
--    а Postgres проверяет право раньше, чем существование схемы.
--    На таблицы рабочего бота это не влияет — там остаётся только SELECT.
GRANT CREATE ON DATABASE clients_db TO adminbot;
```

Затем миграции — по порядку, он важен. Скрипт обновления применяет их сам
(`for sql in migrations/*.sql`), вручную это нужно только при первой установке:

```bash
export ADMINBOT_DB_DSN='postgresql://adminbot:ПАРОЛЬ@127.0.0.1:5432/clients_db'
cd /opt/raketa-admin-bot/app
for sql in migrations/*.sql; do psql "$ADMINBOT_DB_DSN" -v ON_ERROR_STOP=1 -f "$sql"; done
psql "$ADMINBOT_DB_DSN" -tAc "select tablename from pg_tables where schemaname='adminbot'"
# ожидаемо: amo_links, amo_actions, settings, carpet_links, carpet_actions,
#           carpet_letters, gcal_events, gcal_actions, gcal_cursor,
#           autocall_leads, autocall_actions, autocall_cursor, owner_outbox
```

Проверка, что правило «в чужие таблицы не пишем» держится не на честном слове:

```bash
psql "$ADMINBOT_DB_DSN" -c "UPDATE public.orders SET amount_total = amount_total WHERE false"
# ожидаемый ответ: ERROR: permission denied for table orders
```

## 5. Настройки

```bash
sudo -u adminbot cp /opt/raketa-admin-bot/app/.env.example /opt/raketa-admin-bot/.env
sudo -u adminbot nano /opt/raketa-admin-bot/.env
sudo chmod 600 /opt/raketa-admin-bot/.env
```

Заполнить обязательно:

```text
ADMINBOT_TG_TOKEN=<из BotFather>
ADMINBOT_OWNER_TG_ID=<ваш id>
BOT_DB_DSN=postgresql://adminbot:ПАРОЛЬ@127.0.0.1:5432/clients_db
ADMINBOT_DB_DSN=postgresql://adminbot:ПАРОЛЬ@127.0.0.1:5432/clients_db
AMO_BASE_URL=https://raketacleancrm.amocrm.ru
AMO_TOKEN=<токен интеграции «Робот amo_sync», см. ~/.amo_write.env>
AMO_SYNC_ENABLED=0      # первый запуск — с выключенной функцией
AMO_SYNC_DRY_RUN=1      # и в режиме репетиции
CARPETS_ENABLED=0       # ковры от партнёра — свой выключатель
GCAL_ENABLED=0          # календарь — тоже свой
GCAL_DRY_RUN=1
```

Файл `.env` в git не попадает и не должен: в нём токены и пароль.

## 6. Служба

```bash
sudo cp /opt/raketa-admin-bot/app/deploy/adminbot.service /etc/systemd/system/raketa-admin-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now raketa-admin-bot.service
sudo systemctl status raketa-admin-bot.service --no-pager
```

Чтобы перезапуск не требовал пароля каждый раз, добавьте строку в sudoers
(`sudo visudo -f /etc/sudoers.d/raketa-admin-bot`):

```text
admin ALL=(root) NOPASSWD: /usr/bin/systemctl restart raketa-admin-bot.service, /usr/bin/systemctl status raketa-admin-bot.service, /usr/bin/systemctl stop raketa-admin-bot.service, /usr/bin/systemctl start raketa-admin-bot.service
```

## 7. Проверка после запуска

1. Логи чистые:

   ```bash
   journalctl -u raketa-admin-bot.service -n 50 --no-pager
   ```

2. В Telegram напишите своему боту `/status`. Ожидаемый ответ: режим «репетиция»,
   состояние «выключен настройками сервиса», очередь пуста.
3. Попросите кого-нибудь ещё написать боту — он должен получить отказ.

## 8. Включение (по шагам, каждый подтверждает владелец)

```text
шаг 1: AMO_SYNC_ENABLED=1, AMO_SYNC_DRY_RUN=1 → перезапуск
       день-два робот шлёт «что я сделал БЫ», в CRM ничего не меняется
шаг 2: команда /backlog → предпросмотр хвоста → кнопка «🚀 Поехали»
       первый боевой прогон, сверка глазами в amoCRM
шаг 3: AMO_SYNC_DRY_RUN=0 → перезапуск
       неделя ежевечерних сводок под присмотром
```

Кнопка «Поехали» проводит хвост по-настоящему даже в режиме репетиции — это
осознанное разрешение владельца, а не сбой.

Календарь (этап 2) включается отдельно и тем же порядком. Доступ к нему настраивается
один раз по инструкции `docs/gcal_access.md`, после чего:

```text
шаг 1: sudo raketa-admin-bot-update --gcal-check       проверка доступа, в CRM не пишем
шаг 2: sudo raketa-admin-bot-update --gcal-on --gcal-rehearsal   день-два репетиции
шаг 3: sudo raketa-admin-bot-update --gcal-live        боевой режим
```

Записи, лежавшие в календаре до включения, робот в работу не берёт — он их только
запоминает. Это решение владельца: будущие заказы постоянных клиентов он ведёт сам.

Календарей может быть несколько (уборки бригадир ведёт в своём). Список задаётся
командой, а не правкой настроек:

```bash
sudo raketa-admin-bot-update --gcal-calendars=raketaclean52@gmail.com,ВТОРОЙ@group.calendar.google.com
```

Через запятую **без пробелов**, первым — основной календарь. Как выдать доступ и
что проверить — `docs/gcal_access.md`, раздел «Как добавить ещё один календарь».
У каждого календаря своя закладка обмена (миграция 008), поэтому один не стирает
изменения другого.

### Автозвонок по заявке с сайта (autocall)

Что делает фича: заявка с сайта попадает в CRM → в окне 10:00–20:00 по Москве АТС
соединяет с ней менеджера и клиента. Менеджер не взял трубку — повтор через 5 минут.
Клиент не взял — повтор через 10 минут. Не больше 4 попыток, после чего сделка уезжает
в этап «Не было 1-го касания», и владелец получает отчёт по каждой завершённой цепочке.

Что владелец кладёт на сервер один раз, до первого включения — файл
`/home/admin/.pbx.env` (права 600):

```text
PBX_BASE_URL=       адрес API АТС onlinePBX
PBX_API_KEY=        ключ API из личного кабинета onlinePBX
PBX_MANAGER_DIAL=   внутренний номер (цепочка) менеджера в АТС
WORKER_TG_TOKEN=    токен рабочего бота — только чтобы написать менеджеру, других прав не даёт
MANAGER_TG_CHAT_ID= куда менеджеру приходит сообщение о звонке
```

Скрипт обновления переносит эти строки в настройки службы сам, один раз при
следующем `sudo raketa-admin-bot-update` (как и ключ календаря). В git файл не
попадает и не должен.

Порядок включения — тот же принцип, что и у календаря, шаг за шагом, каждый
подтверждает владелец:

```text
шаг 0: sudo raketa-admin-bot-update --autocall-exam
       экзамен фильтра на истории заявок с сайта; в CRM не пишем, службу не трогаем

шаг 1: sudo raketa-admin-bot-update --autocall-on --autocall-rehearsal
       репетиция: робот пишет владельцу «позвонил бы», клиенту и менеджеру не звонит,
       менеджеру не пишет, следов в базе не оставляет; ключ АТС для репетиции не нужен

шаг 2: стенд с АТС (после появления боевого ключа — Задача 7 плана autocall):
       тестовая цепочка, где «клиент» — номер владельца; проверить
       соединение менеджер↔клиент и то, как робот определяет исход звонка.
       Факты стенда 2026-09-02: отбивка НЕВОЗМОЖНА — call/now.json не умеет
       проигрывать аудио, а модуль «Приветствие» ломает переадресацию (ответ
       поддержки onlinePBX); замена — TG-сообщение менеджеру до звонка + номер
       клиента в приложении onlinePBX. Таймаут HTTP-ответа call/now НЕ значит,
       что звонок не ушёл — исход искать в истории. Открытый вопрос: в двух
       прогонах переадресация 100→мобильные не сработала (звонило только
       приложение), причина не найдена, вопрос в поддержке.

шаг 3: согласовать с владельцем тексты сообщений менеджеру
       (черновики — adminbot/tg/autocall_cards.py)

шаг 4: sudo raketa-admin-bot-update --autocall-live
       боевой режим + неделя наблюдения: каждое соединение — отчёт владельцу
```

Выключить в любой момент одной командой:

```bash
sudo raketa-admin-bot-update --autocall-off
```

## 9. Обновление кода

Код едет `локальная машина → /home/admin/raketa-admin-bot → служба`. Git на сервере
не участвует: в `/opt/raketa-admin-bot/app` лежит копия без `.git`, и `git pull`
там выполнить нельзя. Два шага, оба обязательны:

```bash
# 1. Перенести код в рабочую копию на сервере (с локальной машины, из корня репозитория)
rsync -a --delete --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
      --exclude '.pytest_cache' --exclude '.env' --exclude '.DS_Store' \
      ./ admin@91.200.150.68:/home/admin/raketa-admin-bot/

# 2. Обновить службу: код → зависимости → миграции → перезапуск
ssh admin@91.200.150.68 'sudo raketa-admin-bot-update'
```

Пропустить первый шаг — значит перезапустить службу на прежней версии: скрипт
обновления берёт код из `/home/admin/raketa-admin-bot`, а не из GitHub. Признак,
что код не доехал: в шаге «4. Миграции» нет файла, который вы только что добавили.

Перед переносом полезно посмотреть, что именно поедет: тот же `rsync` с ключами
`--dry-run --itemize-changes`. Миграции скрипт применяет сам (все файлы
`migrations/*.sql` по порядку) — и делает это до перезапуска, поэтому новая
колонка появляется раньше, чем её начинает читать новый код.

## 10. Если что-то пошло не так

| Что видно | Что делать |
|---|---|
| Бот молчит на команды | `systemctl status raketa-admin-bot.service`, затем `journalctl -u raketa-admin-bot.service -n 100` |
| Робот делает лишнее в CRM | напишите боту `/pause` — он остановится сразу и переживёт перезапуск |
| Нужен полный стоп | `sudo systemctl stop raketa-admin-bot.service` |
| Ошибки amoCRM в логах | проверьте токен в `.env`; амо бывает доступна только из России, сервер в РФ |
| `permission denied for table` | значит правило read-only работает; смотрите, какой запрос это вызвал |
| Робот звонит лишнее или не вовремя | `sudo raketa-admin-bot-update --autocall-off` — служба перезапустится без фичи |
| АТС не отвечает (алерт владельцу) | проверьте `PBX_API_KEY` в `.env` службы и личный кабинет onlinePBX |
| Сообщения от робота не приходят | `/status` покажет строку «Жду отправки: N» — значит Telegram недоступен, а сообщения целы и уйдут сами |
| Робот спрашивает про старые сделки | `sudo raketa-admin-bot-update --stale` — покажет все незакрытые сделки по возрасту и этапам, со ссылками на самые древние |

### Почта владельца: что где смотреть

Сообщение, не ушедшее с первой попытки, становится долгом и досылается фоном
(раз в минуту, с растущей паузой). Очередь видна в базе:

```bash
psql "$ADMINBOT_DB_DSN" -c "
  SELECT id, kind, ref, attempts, next_try_at, last_error
  FROM adminbot.owner_outbox
  WHERE sent_at IS NULL AND dropped_at IS NULL ORDER BY id"
```

Пусто — всё доставлено. Строки с растущим `attempts` означают, что Telegram
недоступен дольше обычного; работа с CRM при этом идёт как ни в чём не бывало.
Протухшие и отменённые видны отдельно — у них заполнены `dropped_at` и
`drop_reason` («протухло» или «нужда отпала»).
