# Переходник журнала — заметка для выката (задача 5)

Это не полный runbook службы (тот — задача 11 того же ТЗ, `docs/plans/
2026-09-18-notifications-bus.md`), а узкая заметка ровно по тому, что просила
задача 5: права на чтение journalctl и как проверить на стенде поведение
«100 одинаковых ошибок -> одна строка со счётчиком».

## Что это

`notifyd.journal_adapter.JournalAdapter` — свой цикл в процессе
`raketa-notify.service` (см. `notifyd/main.py`). Читает `journalctl` служб
`telegram-bot.service` и `raketa-admin-bot.service`, берёт записи уровня
warning/error/critical, маскирует телефон/адрес клиента (`notifyd.redact`),
схлопывает одинаковые повторы в окне 10 минут, ограничивает число строк за
проход и кладёт результат в `notify.outbox` (`kind=notify.journal`,
`source=notify`) — дальше доставляет обычный почтальон (`notifyd.postman`),
этот модуль сам в Telegram не ходит.

Свой выключатель `NOTIFY_JOURNAL_ENABLED` (по умолчанию `0`) — отдельно от
`NOTIFY_ENABLED` почтальона. Пока стоит `0`, служба journalctl вообще не
трогает.

## Права на чтение журнала

Юнит `deploy/raketa-notify.service` содержит:

```
SupplementaryGroups=systemd-journal
```

Членство в группе `systemd-journal` — штатный способ читать чужой журнал
без root (`journalctl(1)`, раздел «Access Control»): группа существует на
любом systemd-хосте сама по себе, заводить её не нужно. Право не зависит от
того, под каким пользователем запущены `telegram-bot.service` и
`raketa-admin-bot.service` — это и есть смысл группы.

**Проверить после выката:**

```bash
# служба запущена под своим unit-файлом (с SupplementaryGroups) —
# смотрим, что она сама не жалуется на доступ к журналу:
sudo systemctl restart raketa-notify.service
journalctl -u raketa-notify.service -n 50 --no-pager
```

Строки вида «notify: journalctl ... завершился с кодом ...» или «не удалось
получить курсор журнала» в собственном журнале `raketa-notify.service` —
знак, что прав не хватает (или что `systemd-journal` на этом хосте называется
иначе — проверить `getent group systemd-journal`).

Отдельно, для ручной проверки **без** запуска самой службы (интерактивная
сессия из-под пользователя `notify` НЕ получает `SupplementaryGroups` юнита
— это относится только к процессу, запущенному systemd):

```bash
sudo usermod -aG systemd-journal notify   # только для ручных проверок такого рода
sudo -u notify journalctl -u telegram-bot.service -n 1 -o json
```

## Проверка «100 одинаковых ошибок -> одна строка со счётчиком» на стенде

Настоящие боты трогать не нужно (и нельзя — правило ТЗ). Проверка идёт на
одноразовом transient-юните, который создаёт сам `systemd-run` — он не
конфликтует с именами `telegram-bot.service`/`raketa-admin-bot.service` и
ничего не меняет в проде:

Важен порядок: курсор нужно зафиксировать **до** того, как строки попали в
журнал (`SystemdJournalSource` не читает историю раньше своего первого
вызова — см. docstring модуля), поэтому весь сценарий — один скрипт, а не
две отдельные команды в разном порядке:

```bash
cd /opt/raketa-notify/app
.venv/bin/python3 -c "
import asyncio
import subprocess
from notifyd.journal_adapter import parse_level_and_module
from notifyd.journal_source import SystemdJournalSource

async def main():
    src = SystemdJournalSource('notify-test-burst.service')
    await src.read_new()          # первый вызов — только фиксирует курсор

    # только после этого заливаем 100 одинаковых строк под отдельным
    # юнитом — он не конфликтует с telegram-bot.service/raketa-admin-bot.service
    # и ничего не меняет в проде; в notify.outbox ничего не попадёт, юнит не
    # из WATCHED_UNITS, это чисто ручная проверка механики чтения/парсинга:
    subprocess.run(['sudo', 'systemd-run', '--unit=notify-test-burst',
                    '--collect', '--wait', '/bin/bash', '-c',
                    'for i in \$(seq 100); do echo \"ERROR:test:искусственный сбой\"; done'],
                   check=True)

    entries = await src.read_new()
    print('строк прочитано:', len(entries))
    parsed = [parse_level_and_module(e.message) for e in entries]
    print('уровень первой строки:', parsed[0].level if parsed and parsed[0] else None)

asyncio.run(main())
"
```

Ожидаемо: `строк прочитано: 100`, `уровень первой строки: ERROR`. Схлопывание
в «одна строка ×100» проверено модульными тестами на живой базе
(`tests/test_journal_adapter.py::test_hundred_identical_errors_become_one_line_with_counter`)
— здесь на стенде подтверждается только то, что не проверить локально:
настоящий `journalctl` действительно отдаёт то, что ожидает
`SystemdJournalSource`.

**Сквозная проверка в бою** (после того как включили `NOTIFY_JOURNAL_ENABLED=1`
и `NOTIFY_ENABLED=1`): временно понизить уровень amoCRM-опроса на бою нельзя
— проще подождать первого настоящего предупреждения любого из двух ботов и
проверить, что в техническом чате появилась строка с тегами `#telegram-bot`
или `#raketa-admin-bot` и тегом модуля.
