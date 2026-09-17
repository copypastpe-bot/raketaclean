# Карта функций: adminbot/db.py

Обновлено 2026-09-17 (задача 4 ТЗ «удаление заказа освобождает сделку»: источник
неразобранных удалений заказов и уборок). Строк в файле: 1383.

Правило файла: в схему `public` (таблицы рабочего бота) не пишем никогда —
для неё здесь только SELECT. Всё собственное состояние живёт в схеме `adminbot`.

Работ у робота две разновидности, и лежат они в разных таблицах бота: заказы
химчистки (`public.orders`) и уборки (`public.cleaning_orders`). Номера в них
пересекаются, поэтому и таблицы связок разные — имя таблицы передаётся
параметром `table` (`LINKS_TABLE` / `CLEANING_LINKS_TABLE`, строки 30–38).

## Общее

| Строка | Функция | Назначение |
|---|---|---|
| 131 | `_init_connection` | jsonb ↔ dict без ручного json.dumps на каждом вызове |
| 138 | `create_pool` | пул соединений с нашим кодеком jsonb |
| 763 | `apply_migration` | применить SQL-файл миграции (тесты и развёртывание) |
| 746 | `get_setting` / 752 `set_setting` | настройки владельца (пауза) |

## Заказы химчистки и уборки

| Строка | Функция | Назначение |
|---|---|---|
| 41 | `_SELECT_ORDERS` (константа SQL, не функция) | заказы бота за период: мастера, адрес из карточки клиента |
| 90 | `_SELECT_CLEANING_ORDERS` (константа SQL, не функция) | уборки за период: свой адрес, бригадир, первая строка оплат, удалённые пропущены |
| 142 | `_order_from_row` | строка → `Order` (kind="order") |
| 175 | `_cleaning_order_from_row` | строка → `Order` (kind="cleaning", «Услуга»=Уборка, «Специалист»=Ольга) |
| 159 | `fetch_orders_since` | все заказы с даты |
| 166 | `fetch_orders_by_ids` | заказы по номерам — вернуться к незавершённым |
| 203 | `fetch_cleaning_orders_since` | все уборки с даты |
| 210 | `fetch_cleaning_orders_by_ids` | уборки по номерам |
| 221 | `fetch_linked_order_ids` | какие работы уже взяты в работу (`table`) |
| 233 | `fetch_unprocessed_orders` | заказы, по которым робот ещё ничего не начинал |
| 242 | `fetch_unprocessed_cleaning_orders` | то же по уборкам |

## Связки работа → сделка (`amo_links` / `cleaning_links`)

Все принимают `table=` — по умолчанию таблица заказов химчистки. С миграции 012
у связки есть `deal_address` — адрес сделки амо на момент, когда робот её
заполнял (движок `sync/engine.py`, `_fill_lead` и путь `already_done`); ПД,
в логи не идёт. С миграции 013 (задача 7) у связки есть ещё три поля
напоминания «сделка без адреса»: `address_reminder_count`,
`address_reminder_sent_at`, `address_reminder_muted` — их читает и пишет
`sync/address_reminder.py`, а не движок.

| Строка | Функция | Назначение |
|---|---|---|
| 252 | `_link_from_row` | строка → `AmoLink` (включая `deal_address` и поля напоминания) |
| 281 | `create_link` | взять работу в работу; повтор ничего не портит |
| 299 | `get_link` | привязка по номеру работы |
| 306 | `update_link` | обновить поля из белого списка (`_UPDATABLE_LINK_FIELDS`) |
| 326 | `mark_checklist_step` | отметить выполненный шаг: после сбоя продолжим отсюда |
| 341 | `log_action` | журнал действий в амо (и в репетиции тоже) |
| 363 | `fetch_actions` | журнал по одной работе |
| 372 | `fetch_taken_lead_ids` | занятые сделки клиента — UNION по ОБЕИМ таблицам связок |
| 405 | `fetch_link_ids_by_status` | номера незаконченных работ |
| 419 | `fetch_links_for_orders` | сырьё для вечерней сводки |
| 433 | `count_links_by_status` | очередь для /status |
| 443 | `fetch_links_needing_address_reminder` | кандидаты на напоминание: `done`+путь C, адреса нет, не muted, счётчик и сутки позволяют |

## Ковры от партнёра (`carpet_links`, `carpet_letters`)

| Строка | Функция | Назначение |
|---|---|---|
| 483 | `_carpet_from_row` | строка → `CarpetLink` |
| 509 | `create_carpet_link` | взять строку отчёта в работу |
| 531 | `remember_carpet_link` | заказ из архива партнёра сразу как сделанный (`done`/`remembered`), одной записью; существующую не трогает |
| 556 | `fetch_pending_carpet_rows` | незавершённые заказы партнёра со строками отчёта |
| 576–630 | `get_/update_/mark_carpet_step/log_carpet_action` | то же, что у заказов |
| 631 | `fetch_carpet_taken_leads` | занятые ковровые сделки клиента |
| 645 | `fetch_carpet_links_by_status` | незаконченные заказы партнёра |
| 659 | `count_carpet_links_by_status` | очередь ковров |
| 666 | `remember_letter` | письмо разобрано: страховка от повторного разбора; снимает отметки об отложении |
| 685 | `letter_state` | что робот помнит о письме: `processed` / `held` / `released` / ничего |
| 703 | `hold_letter` | отложить письмо: робот его не проводит и ждёт владельца |
| 720 | `release_letter` | владелец разрешил провести отложенное письмо |
| 733 | `held_letters` | что сейчас отложено — для `--carpets-held` |

## Календарь (`gcal_events`, `gcal_cursor`)

| Строка | Функция | Назначение |
|---|---|---|
| 781 | `_as_dict` / 877 `_gcal_value` | jsonb и text[] в виде, который принимает asyncpg |
| 788 | `_calendar_from_row` | строка → `CalendarLink` |
| 822–912 | `get_/create_/update_calendar_link`, `mark_calendar_step`, `log_calendar_action` | ход работы по записи |
| 913 | `fetch_calendar_taken_leads` | сделки, занятые ДРУГИМИ записями того же клиента |
| 934 | `fetch_pending_calendar_links` | записи, работа по которым не закончена |
| 948 | `fetch_calendar_links_without_report` | сделано, а владельцу не отчитались |
| 971 | `count_calendar_links_by_status` | очередь календаря |
| 978 | `get_calendar_cursor` / 1007 `save_calendar_cursor` | закладка обмена, своя у каждого календаря |
| 1024 | `find_calendar_link_by_question_msg` | запись по номеру карточки в Telegram |
| 1040 | `delete_calendar_link` | забыть запись (ручной разбор последствий) |
| 1046 | `fetch_calendar_actions` | что робот делал по записи |

## Автозвонок (`autocall_leads`, курсор опроса)

| Строка | Функция | Назначение |
|---|---|---|
| 1070 | `_autocall_from_row` | строка → `AutocallLead` |
| 1089–1138 | `get_/create_/update_autocall_lead` | цепочка попыток дозвона |
| 1139 | `fetch_due_autocall_leads` | созревшие цепочки, просроченные первыми |
| 1160 | `log_autocall_action` / 1172 `fetch_autocall_actions` | журнал по заявке |
| 1301 | `get_autocall_cursor` / 1309 `save_autocall_cursor` | с какого `created_at` читать амо |

## Почта владельца (`owner_outbox`)

| Строка | Функция | Назначение |
|---|---|---|
| 1187 | `add_owner_letter` | положить недоставленное сообщение в долг |
| 1207 | `fetch_due_owner_letters` | созревшие долги, старые первыми |
| 1229 | `fetch_owner_letters_for` | незаконченные долги по одной записи |
| 1250 | `mark_owner_letter_sent` / 1263 `postpone_owner_letter` / 1277 `drop_owner_letter` | исход попытки |
| 1291 | `count_owner_letters_waiting` | строка «жду отправки» для /status |

## Удаления заказов и уборок (задача 4 ТЗ 2026-09-17 «удаление заказа освобождает сделку»)

Один запрос на вид работы, оба — прямой JOIN между `public` (регистр рабочего
бота) и `adminbot.order_deletions_seen` (свои отметки, миграция 014): обе схемы
живут в одной базе под одной ролью, диф считается в Postgres, а не в Python.
Разбор неразобранных записей (задача 5) в этот файл не входит.

| Строка | Функция | Назначение |
|---|---|---|
| 1335 | `fetch_pending_order_deletions` | удалённые заказы химчистки (`public.deleted_orders`) без отметки |
| 1361 | `fetch_pending_cleaning_deletions` | уборки с `deleted_at` без отметки |
