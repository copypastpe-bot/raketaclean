# Деплой рабочего бота

Короткий runbook. Админ-бот едет по-другому — см. `raketa-admin-bot/docs/deploy.md` §9
(два шага: `rsync` плюс `sudo raketa-admin-bot-update`). Пути разные, шаги разные,
перепутать нельзя.

## Что где

- Репозиторий: `copypastpe-bot/raketaclean`, боевая ветка `main`.
- Сервер: `admin@91.200.150.68`, рабочая копия `/opt/telegram-bot`, служба
  `telegram-bot.service` (перезапуск разрешён без пароля).

## Права на схему adminbot (разовая установка)

Рабочий бот (роль `bot`) читает две таблицы связок админ-бота — оттуда он
дозаполняет у себя адрес заказа (задача 4 ТЗ «адреса до конца», ещё не сделана
на момент этой записи). Выполняется один раз от суперпользователя Postgres,
после того как в `raketa-admin-bot` накатана миграция 012 (см.
`raketa-admin-bot/docs/deploy.md` §4.1 — там же и эти же команды):

```sql
GRANT USAGE ON SCHEMA adminbot TO bot;
GRANT SELECT ON adminbot.amo_links, adminbot.cleaning_links TO bot;
```

Чтение только в эту сторону: правило «админ-бот в схему `public` не пишет»
эти команды не затрагивают и не ослабляют.

Проверка:

```bash
psql "$DSN_AS_BOT" -c "SELECT count(*) FROM adminbot.amo_links"
# ожидаемо: работает
psql "$DSN_AS_BOT" -c "UPDATE adminbot.amo_links SET status = 'x' WHERE false"
# ожидаемый ответ: ERROR: permission denied for table amo_links
```

## Обычный выезд

```bash
cd /opt/telegram-bot
git pull --ff-only origin main
git rev-parse --short HEAD          # запомнить: это новый боевой SHA
sudo systemctl restart telegram-bot.service
systemctl is-active telegram-bot.service
journalctl -u telegram-bot.service -n 20 --no-pager
```

В журнале должен быть чистый старт без трассировок. `git rev-parse --short HEAD`
на сервере обязан совпасть с локальным `main`.

Если на сервере ещё прописан старый адрес репозитория, перевести его один раз:

```bash
git remote set-url origin https://github.com/copypastpe-bot/raketaclean.git
```

## Выключатели (switches.env)

Динамические флаги в `/opt/telegram-bot/switches.env` (синтаксис: `КЛЮЧ=значение`, без кавычек):

- `AMOCRM_UNSORTED_CARDS`: `1` — карточки «Неразобранного» админам включены, `0` — выключены; рестарт рабочего бота по слову владельца.

Другие выключатели и детали их установки — см. комментарии в самом файле.

## Откат

Вернуться на прежний SHA и перезапустить:

```bash
cd /opt/telegram-bot
git checkout <прежний SHA>
sudo systemctl restart telegram-bot.service
journalctl -u telegram-bot.service -n 20 --no-pager
```

Прежний SHA — тот, что был напечатан перед выкаткой. Ветка `feature/amo-exchange`
держится в репозитории как запасной путь отката.

## Сторож прокси

Телеграм с этого сервера доступен только через прокси Contabo. Сторож проверяет
канал по cron и путь свой не меняет:

```
cd /opt/telegram-bot && .venv/bin/python scripts/telegram_proxy_watchdog.py
```

Он живёт в той же рабочей копии, поэтому отдельной выкатки не требует: приехал код
рабочего бота — приехал и сторож. После деплоя убедиться, что ближайший проход
сторожа отработал.

## Резервные серверы имён

Служба имён (DNS) на сервере — `systemd-resolved`, серверы приходят от провайдера
по DHCP (85.193.93.193 и .194). 15.09.2026 они молчали 44 минуты, и оба бота писали
«нет связи с amoCRM» и «календарь недоступен», хотя сеть работала. Резервные серверы
(Яндекс 77.88.8.8 и Cloudflare 1.1.1.1) добавляются отдельным файлом netplan
`/etc/netplan/60-dns-backup.yaml`: провайдерские остаются, cloud-init этот файл не трогает.

Нужен sudo, поэтому с локальной машины, в своём терминале:

```bash
scp scripts/server_dns_backup.sh admin@91.200.150.68:/tmp/
ssh -t admin@91.200.150.68 'sudo bash /tmp/server_dns_backup.sh'
```

Скрипт применяет настройку через `netplan try`: если связь с сервером оборвётся,
через 120 секунд всё откатится само; если всё хорошо, нажать Enter. Посмотреть
состояние без изменений: `bash /tmp/server_dns_backup.sh --check` (sudo не нужен).
Откат: удалить файл и выполнить `sudo netplan apply`.

## Чего делать нельзя

- Деплоить с грязного дерева: сначала `git status --short` пуст, потом выкатка.
- Деплоить неотправленное: на сервер едет только то, что уже в `origin/main`.
- Деплоить админ-бота этим runbook: у него `rsync` и свой скрипт обновления.
