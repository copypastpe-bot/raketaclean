# Деплой рабочего бота

Короткий runbook. Админ-бот едет по-другому — см. `raketa-admin-bot/docs/deploy.md` §9
(два шага: `rsync` плюс `sudo raketa-admin-bot-update`). Пути разные, шаги разные,
перепутать нельзя.

## Что где

- Репозиторий: `copypastpe-bot/raketaclean`, боевая ветка `main`.
- Сервер: `admin@91.200.150.68`, рабочая копия `/opt/telegram-bot`, служба
  `telegram-bot.service` (перезапуск разрешён без пароля).

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

## Чего делать нельзя

- Деплоить с грязного дерева: сначала `git status --short` пуст, потом выкатка.
- Деплоить неотправленное: на сервер едет только то, что уже в `origin/main`.
- Деплоить админ-бота этим runbook: у него `rsync` и свой скрипт обновления.
