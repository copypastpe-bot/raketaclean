# ТЗ: защита ковров от «не того файла», память по файлу партнёра, прогон из файла

Дата: 2026-09-15. Автор ТЗ: Fable (координатор). Исполнитель: Opus. Ветка: `feature/carpets-letter-guard` от `main`.

## Зачем

15.09.2026 партнёр прислал на ящик робота отчёт за два года (536 строк). Фильтр почты
переложил письмо в `robot_amo`, часовой проход робота взял его как обычный недельный
отчёт и за три минуты создал 30 лишних сделок в amoCRM, пока службу не остановили.
Разбор инцидента: `SESSION_LOG.md` в корне репозитория, запись от 2026-09-15.

Решения владельца (15.09.2026):

1. Письмо, в котором больше 100 наших строк, робот не проводит: откладывает и один раз
   сообщает владельцу. Проверку разброса дат не делаем.
2. Все номера заказов из двухлетнего файла партнёра загружаются в память робота как
   проведённые, чтобы любой повтор старых строк (в любом файле) пропускался.
3. Боевой прогон из файла возможен только с белым списком сделок.
4. Ключ `--carpets-forget` в это ТЗ не входит (владелец не подтвердил).

Механика партнёра, которую робот должен переживать без изменений: недельный отчёт
(понедельник, 5–15 строк), месячный отчёт (повторяет недельные, до 40–50 строк,
даты «Добавление» растянуты на 30–45 дней), файл отказов за месяц (повторяет недельные
отказы). Повторы отсекаются по номеру заказа партнёра, это уже работает: см.
`adminbot/carpets/engine.py:96` (`process_row`: строка со статусом `done` или
`waiting_owner` пропускается без обращения к амо).

## Что уже есть в коде (читать перед работой)

- `adminbot/mail.py` — чтение папки: `_fetch_new` (строка 97), `_read_letter` (130),
  `_mark_seen` (116). Письма адресуются **порядковыми номерами** IMAP (`box.search`,
  `box.fetch`, `box.store` без `uid`). Порядковый номер сдвигается при удалении
  любого письма в папке: проверено 15.09 (`5 (UID 6 …)`).
- `adminbot/carpets/watcher.py` — `CarpetStateStore` (протокол, строка 43),
  `CarpetTickReport` (56), `CarpetWatcher.__init__` (67), `_handle_letter` (138).
  Сейчас: письмо, которое уже в базе, помечается прочитанным; файл, который
  не разобрался, оставляет письмо «в работе» и пробуется каждый час, владельцу
  об этом не сообщается.
- `adminbot/carpets/store.py` — `MemoryCarpetStore` (репетиция) и `PgCarpetStore`
  (боевой, строка 113): `letter_processed`, `remember_letter`.
- `adminbot/db.py:605` `remember_letter`, `:619` `letter_was_processed`.
- `migrations/004_carpets.sql:43` — таблица `adminbot.carpet_letters(uid, subject,
  files, rows_total, processed_at)`. Последняя миграция — `010_cleaning_links.sql`,
  следующая свободная — **011**. Скрипт обновления применяет `migrations/*.sql`
  по порядку сам.
- `adminbot/config.py:204` — настройки ковров (`carpets_enabled`, `carpets_dry_run`,
  `carpets_poll_interval_sec`), чтение из env на строке 291.
- `adminbot/main.py:399` `_build_carpets` — сборка наблюдателя; `:707`
  `_make_carpet_report_sender` — как уходит отчёт владельцу (`mail.send(...,
  kind=MAIL_CARPET_REPORT)`); тексты карточек в `adminbot/tg/cards.py`
  (`carpet_report_text`).
- `deploy/update.sh` — разбор ключей (строка 70), образец служебного ключа с доступом
  к базе: блок `--gcal-forget` (строка 312): `sudo -u adminbot env $ENV_VARS …
  python -m scripts.forget_calendar`, параметры передаются через переменные окружения.
  Образец скрипта: `scripts/forget_calendar.py`.
- `scripts/run_carpets.py` — репетиция и боевое проведение по письмам папки
  (`MemoryCarpetStore`, `dry_run=not args.live`).
- `scripts/verify_carpets.py` — `AllLettersBox` переопределяет `_fetch_new` и тоже
  использует порядковые номера.
- Тесты: `tests/test_carpet_watcher.py` (двойники `FakeMailBox`, `FakeEngine`,
  `FakeStore`), `tests/test_mail.py` (`FakeImap` со `search/fetch/store`).

## Стенд

- Тесты: из папки `raketa-admin-bot/`: `.venv/bin/python -m pytest -q --tb=short`
  с `TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/adminbot_test`; Postgres
  поднимается `brew services start postgresql@14`. Ожидается 811 зелёных, 9 пропущено
  до начала работы. Точечно: `.venv/bin/python -m pytest tests/test_carpet_watcher.py -q`.
- amoCRM с локальной машины недоступна: живой прогон делает координатор на VPS.
- Деплой не входит в задачи исполнителя (`docs/deploy.md` §9, делает координатор).

## Задачи

### Задача 1. Письма адресуются постоянным UID, а не порядковым номером

**Выполнено** 9907f98

Файлы: `adminbot/mail.py`, `scripts/verify_carpets.py`, `tests/test_mail.py`.

- В `_fetch_new`, `_read_letter`, `_mark_seen` перейти на команды `box.uid("search", …)`,
  `box.uid("fetch", uid, …)`, `box.uid("store", uid, …)`. `Letter.uid` становится
  постоянным UID папки (строка).
- `AllLettersBox._fetch_new` в `scripts/verify_carpets.py` — то же самое.
- `FakeImap` в тестах получает метод `uid(command, *args)`, тесты на чтение и пометку
  обновляются. Добавить тест: после удаления письма из папки-двойника UID остальных
  не меняются, и `mark_seen` попадает в нужное письмо.
- Старые записи `carpet_letters` с порядковыми номерами (4 штуки на проде) остаются:
  эти письма уже прочитаны и в выборку `UNSEEN` не попадают.

Почему: отложенное письмо (задача 3) живёт в папке непрочитанным неделями; по
порядковому номеру чужое письмо могло бы сойти за отложенное и молча пропасть.

### Задача 2. Память робота: отложенные письма

**Выполнено** 8648779

Файлы: `migrations/011_carpet_letters_held.sql`, `adminbot/db.py`,
`adminbot/carpets/store.py`, протокол в `adminbot/carpets/watcher.py`, тесты.

- Миграция 011: в `adminbot.carpet_letters` добавить `held_reason text`,
  `held_at timestamptz`, `released_at timestamptz`. Шапка миграции по образцу 010
  (зачем, как применяется вручную).
- `db.py`:
  - `letter_state(pool, uid) -> Optional[str]`: `"processed"` — запись есть и
    `held_reason IS NULL`; `"held"` — `held_reason IS NOT NULL AND released_at IS NULL`;
    `None` — записи нет **или** письмо отложено и снято (`released_at IS NOT NULL`):
    такое письмо обрабатывается заново.
  - `hold_letter(pool, uid, subject, files, rows_total, reason)`: upsert с
    `held_reason=reason, held_at=now(), released_at=NULL`.
  - `release_letter(pool, uid) -> bool`: `released_at=now()` там, где
    `held_reason IS NOT NULL AND released_at IS NULL`; вернуть, была ли строка.
  - `held_letters(pool) -> list[dict]`: uid, subject, rows_total, held_reason, held_at.
  - `remember_letter` при конфликте по uid сбрасывает `held_reason`, `held_at`,
    `released_at` в NULL (письмо проведено).
  - `letter_was_processed` оставить как обёртку над `letter_state` или удалить,
    если больше не используется.
- `store.py`: обе реализации получают `letter_state` и `hold_letter`
  (`MemoryCarpetStore` держит отложенные в памяти; в репетиции ничего в базу не
  пишется, как и сейчас). Протокол `CarpetStateStore` обновить.
- Тесты на `db.py` — по образцу существующих тестов базы (если для `carpet_letters`
  их нет, добавить минимальный: hold → state held → release → state None →
  remember → state processed).

### Задача 3. Проверка письма целиком до первой строки

**Выполнено** 442caf4 (с отступлением: `letter_state` отдаёт четвёртое состояние
`released` вместо `None`, иначе снятие отложения ничего не меняет — см. отчёт)

Файлы: `adminbot/carpets/watcher.py`, `adminbot/config.py`, `adminbot/main.py`,
`adminbot/tg/cards.py`, `.env.example`, `docs/deploy.md` (раздел про ковры, если
есть; иначе короткий абзац в §5 «Настройки»), тесты.

Поведение `_handle_letter`:

1. `state = await store.letter_state(uid)`. `"processed"` — как сейчас: пометить
   прочитанным и выйти. `"held"` — выйти молча (лог уровня debug), письмо остаётся
   непрочитанным, в отчёт прохода не попадает.
2. Разобрать **все** вложения до обработки первой строки. Любое вложение не
   разобралось — письмо целиком откладывается с причиной
   `файл <имя>: <тип ошибки>: <текст>`; остальные вложения не проводятся.
   (Это меняет текущее поведение «сбойный файл не мешает остальным» — решение
   владельца: неразобранный файл требует взгляда человека, а не тихих повторов.)
3. Посчитать наши строки после `rows_to_process` (выполненные плюс отказы). Если их
   больше `max_rows` — отложить с причиной `строк <N>, порог <max_rows>`.
4. Отложить = `store.hold_letter(...)` + один вызов `on_held(letter, reason,
   rows_total, refused_total)` + `log.warning`. Письмо **не** помечается прочитанным
   ни в боевом режиме, ни в репетиции. Повторное сообщение владельцу по тому же
   письму не уходит (состояние `held` отсекает на шаге 1).
5. Иначе — как сейчас.

`CarpetWatcher.__init__` получает `max_rows: int = 100` и
`on_held: Optional[Callable[..., Awaitable[None]]] = None`.
`CarpetTickReport` получает поле `held: int` (сколько писем отложено за проход).

Настройка: `carpets_max_rows` в `Settings`, env `CARPETS_MAX_ROWS`, по умолчанию 100;
пример в `.env.example` с комментарием «недельный отчёт 5–15 строк, месячный до 50».

`main.py`: передать `max_rows=settings.carpets_max_rows` и
`on_held=_make_carpet_hold_sender(mail)`; новый вид письма `MAIL_CARPET_HELD =
"carpet_held"`, текст `carpet_held_text(subject, reason, rows_total, refused_total,
uid)` в `adminbot/tg/cards.py`. Текст для владельца, без жаргона, например:

> Ковры: письмо «<тема>» отложено, не проведено.
> Причина: строк 536, порог 100.
> В файле: 475 выполненных, 61 отказ.
> Если это нормальный отчёт: `sudo raketa-admin-bot-update --carpets-release=<uid>`.
> Если архив или чужой файл: удалите письмо из папки robot_amo.

Тесты (`tests/test_carpet_watcher.py`): письмо больше порога откладывается, строки не
трогаются, `on_held` вызван один раз, письмо не помечено прочитанным; на втором
проходе то же письмо пропускается молча без повторного `on_held`; неразобранное
вложение откладывает письмо целиком; письмо ровно на пороге проводится; после
`release` письмо проводится и попадает в `remember_letter`. Существующие тесты
поправить под новое поведение (в частности
`test_broken_attachment_does_not_stop_the_others`).

### Задача 4. Снятие отложенного письма с сервера

**Выполнено** 3fa5a4a

Файлы: `scripts/held_letters.py` (новый), `deploy/update.sh`, шапка `update.sh`
(список ключей), `docs/deploy.md` §10 (таблица «если что-то пошло не так»: строка
«Робот сообщил, что письмо отложено»).

- `scripts/held_letters.py`: без параметров печатает отложенные письма (uid, тема,
  строк, причина, когда); с `CARPETS_RELEASE_UID=<uid>` снимает отложение
  (`db.release_letter`) и печатает результат. Доступ к базе и параметры через
  переменные окружения — как в `scripts/forget_calendar.py`.
- `update.sh`: ключи `--carpets-held` (показать) и `--carpets-release=<uid>` (снять);
  блок по образцу `--gcal-forget`; служба **не** перезапускается (как у `--report`).
  После снятия следующий часовой проход проведёт письмо как обычное.

### Задача 5. Загрузить номера заказов из файла партнёра в память робота

Файлы: `scripts/remember_carpets.py` (новый), `deploy/update.sh`, шапка `update.sh`,
`docs/deploy.md` (§10 или отдельный абзац «Архивный файл партнёра»).

- Скрипт читает xlsx по пути из `CARPETS_REMEMBER_FILE`, разбирает `parse_report`,
  оставляет наши строки (`rows_to_process`). Для каждого номера, которого нет в
  `carpet_links`, создаёт запись через `PgCarpetStore.create(...)` и переводит
  в `status="done", path="remembered"`, `source_file` — имя файла. Существующие
  номера не трогает. В амо не ходит. Печатает: всего строк, новых, уже известных.
- По умолчанию только показывает счётчики; `CARPETS_REMEMBER_LIVE=1` — записывает.
- `update.sh`: `--carpets-remember=<путь к файлу>` (показать) и
  `--carpets-remember-live` (записать). Файл с телефонами клиентов лежит у `admin`;
  скрипт обновления копирует его во временный файл, читаемый пользователем `adminbot`,
  запускает скрипт и удаляет копию в любом исходе. Служба не перезапускается.
- Тест на скрипт не обязателен; логика отбора строк уже покрыта тестами
  `parse_report`/`rows_to_process`. Проверить руками на локальной базе
  `adminbot_test` с любым xlsx нужного формата нельзя (файла партнёра в репозитории
  нет и быть не должно) — достаточно прогона на пустом файле с заголовком, если
  такой есть в `tests/fixtures`; иначе только статическая проверка.

### Задача 6. Прогон из файла с белым списком сделок

Файлы: `scripts/run_carpets.py`, шапка скрипта, `docs/deploy.md`.

- Ключи `--file <xlsx>` и `--allow <id,id,...>`. С `--file` письмо строится из файла
  (`Letter(uid="file:<имя>", subject=<имя>, sender="владелец", attachments={...})`),
  почта не нужна: доступы к почте не проверяются и не читаются.
- `--live` вместе с `--file` **обязательно** требует `--allow`; без него скрипт
  отказывается: «боевой прогон из файла только с белым списком сделок».
- При `--allow` клиент амо оборачивается защитой: `create_contact`, `update_contact`,
  `create_lead`, `complete_task` бросают `RuntimeError`; `update_lead`, `move_lead`,
  `add_note` разрешены только для сделок из списка, `move_lead` — только в воронку
  ковров (`ids.PIPELINE_CARPETS`). Ошибка защиты роняет строку в `error`
  (это уже делает движок), в амо ничего не уходит.
- Образец такой обёртки применялся 15.09 (`live_rows.py`, удалён): класс-наследник
  `AmoClient` с проверкой `lead_id` в белом списке.
- Тест: защита отказывает в создании и в записи вне списка, пропускает запись
  в сделку из списка (на двойнике `AmoClient` или через `dry_run=True`).

### Задача 7. Полный прогон тестов, документация, отметки

- Полный `pytest -q` зелёный. Обновить `adminbot/db.py.map.md` (файл длиннее
  500 строк, карта обязательна) новыми функциями.
- Шапка `deploy/update.sh` перечисляет новые ключи. `docs/deploy.md` содержит:
  настройку `CARPETS_MAX_ROWS`, что делать с отложенным письмом, как загрузить
  архивный файл партнёра, как гонять из файла с белым списком.
- Под каждой задачей в этом файле поставить `**Выполнено** <sha коммита>` после
  коммита. Коммит на каждую задачу, сообщения на русском, ветка
  `feature/carpets-letter-guard`. В `main` не сливать, не пушить.

## Не делаем

- Кнопки в Telegram для снятия отложенного письма.
- Проверку разброса дат.
- Напоминание о пропавшем недельном отчёте.
- Ключ `--carpets-forget` (не подтверждён владельцем).
- Деплой, слияние в `main`, пуш — координатор и владелец.

## Приёмка (координатор, после ревью)

1. Ревью на соответствие ТЗ, замечания в `docs/plans/2026-09-15-carpets-letter-guard-review.md`.
2. Слияние в `main`, пуш владельцем, деплой по `docs/deploy.md` §9 (rsync плюс
   `sudo raketa-admin-bot-update`; миграция 011 применится сама).
3. На VPS: `--carpets-held` пуст; `--carpets-remember=<двухлетний файл>` показывает
   536 строк, затем `--carpets-remember-live`; `--report` показывает записи
   с путём `remembered`.
4. Следующий недельный отчёт партнёра проходит как обычно (строк меньше порога).
