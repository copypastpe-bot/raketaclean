# Карта функций: adminbot/db.py

Обновлено 2026-09-10 (добавлено чтение уборок). Строк в файле: 1200.

Правило файла: в схему `public` (таблицы рабочего бота) не пишем никогда —
для неё здесь только SELECT. Всё собственное состояние живёт в схеме `adminbot`.

Работ у робота две разновидности, и лежат они в разных таблицах бота: заказы
химчистки (`public.orders`) и уборки (`public.cleaning_orders`). Номера в них
пересекаются, поэтому и таблицы связок разные — имя таблицы передаётся
параметром `table` (`LINKS_TABLE` / `CLEANING_LINKS_TABLE`, строки 30–38).

## Общее

| Строка | Функция | Назначение |
|---|---|---|
| 130 | `_init_connection` | jsonb ↔ dict без ручного json.dumps на каждом вызове |
| 137 | `create_pool` | пул соединений с нашим кодеком jsonb |
| 642 | `apply_migration` | применить SQL-файл миграции (тесты и развёртывание) |
| 625 | `get_setting` / 631 `set_setting` | настройки владельца (пауза) |

## Заказы химчистки и уборки

| Строка | Функция | Назначение |
|---|---|---|
| 40 | `_SELECT_ORDERS` | заказы бота за период: мастера, адрес из карточки клиента |
| 89 | `_SELECT_CLEANING_ORDERS` | уборки за период: свой адрес, бригадир, первая строка оплат, удалённые пропущены |
| 141 | `_order_from_row` | строка → `Order` (kind="order") |
| 174 | `_cleaning_order_from_row` | строка → `Order` (kind="cleaning", «Услуга»=Уборка, «Специалист»=Ольга) |
| 158 | `fetch_orders_since` | все заказы с даты |
| 165 | `fetch_orders_by_ids` | заказы по номерам — вернуться к незавершённым |
| 202 | `fetch_cleaning_orders_since` | все уборки с даты |
| 209 | `fetch_cleaning_orders_by_ids` | уборки по номерам |
| 220 | `fetch_linked_order_ids` | какие работы уже взяты в работу (`table`) |
| 232 | `fetch_unprocessed_orders` | заказы, по которым робот ещё ничего не начинал |
| 241 | `fetch_unprocessed_cleaning_orders` | то же по уборкам |

## Связки работа → сделка (`amo_links` / `cleaning_links`)

Все принимают `table=` — по умолчанию таблица заказов химчистки.

| Строка | Функция | Назначение |
|---|---|---|
| 251 | `_link_from_row` | строка → `AmoLink` |
| 276 | `create_link` | взять работу в работу; повтор ничего не портит |
| 294 | `get_link` | привязка по номеру работы |
| 301 | `update_link` | обновить поля из белого списка (`_UPDATABLE_LINK_FIELDS`) |
| 321 | `mark_checklist_step` | отметить выполненный шаг: после сбоя продолжим отсюда |
| 336 | `log_action` | журнал действий в амо (и в репетиции тоже) |
| 358 | `fetch_actions` | журнал по одной работе |
| 367 | `fetch_taken_lead_ids` | занятые сделки клиента — UNION по ОБЕИМ таблицам связок |
| 400 | `fetch_link_ids_by_status` | номера незаконченных работ |
| 414 | `fetch_links_for_orders` | сырьё для вечерней сводки |
| 428 | `count_links_by_status` | очередь для /status |

## Ковры от партнёра (`carpet_links`)

| Строка | Функция | Назначение |
|---|---|---|
| 447 | `_carpet_from_row` | строка → `CarpetLink` |
| 473 | `create_carpet_link` | взять строку отчёта в работу |
| 495 | `fetch_pending_carpet_rows` | незавершённые заказы партнёра со строками отчёта |
| 515–554 | `get_/update_/mark_carpet_step/log_carpet_action` | то же, что у заказов |
| 570 | `fetch_carpet_taken_leads` | занятые ковровые сделки клиента |
| 584 | `fetch_carpet_links_by_status` | незаконченные заказы партнёра |
| 598 | `count_carpet_links_by_status` | очередь ковров |
| 605 | `remember_letter` / 619 `letter_was_processed` | письмо разобрано: страховка от повторного разбора |

## Календарь (`gcal_events`, `gcal_cursor`)

| Строка | Функция | Назначение |
|---|---|---|
| 660 | `_as_dict` / 756 `_gcal_value` | jsonb и text[] в виде, который принимает asyncpg |
| 667 | `_calendar_from_row` | строка → `CalendarLink` |
| 701–776 | `get_/create_/update_calendar_link`, `mark_calendar_step`, `log_calendar_action` | ход работы по записи |
| 792 | `fetch_calendar_taken_leads` | сделки, занятые ДРУГИМИ записями того же клиента |
| 813 | `fetch_pending_calendar_links` | записи, работа по которым не закончена |
| 827 | `fetch_calendar_links_without_report` | сделано, а владельцу не отчитались |
| 850 | `count_calendar_links_by_status` | очередь календаря |
| 857 | `get_calendar_cursor` / 886 `save_calendar_cursor` | закладка обмена, своя у каждого календаря |
| 903 | `find_calendar_link_by_question_msg` | запись по номеру карточки в Telegram |
| 919 | `delete_calendar_link` | забыть запись (ручной разбор последствий) |
| 925 | `fetch_calendar_actions` | что робот делал по записи |

## Автозвонок (`autocall_leads`, курсор опроса)

| Строка | Функция | Назначение |
|---|---|---|
| 949 | `_autocall_from_row` | строка → `AutocallLead` |
| 968–999 | `get_/create_/update_autocall_lead` | цепочка попыток дозвона |
| 1018 | `fetch_due_autocall_leads` | созревшие цепочки, просроченные первыми |
| 1039 | `log_autocall_action` / 1051 `fetch_autocall_actions` | журнал по заявке |
| 1180 | `get_autocall_cursor` / 1188 `save_autocall_cursor` | с какого `created_at` читать амо |

## Почта владельца (`owner_outbox`)

| Строка | Функция | Назначение |
|---|---|---|
| 1066 | `add_owner_letter` | положить недоставленное сообщение в долг |
| 1086 | `fetch_due_owner_letters` | созревшие долги, старые первыми |
| 1108 | `fetch_owner_letters_for` | незаконченные долги по одной записи |
| 1129 | `mark_owner_letter_sent` / 1142 `postpone_owner_letter` / 1156 `drop_owner_letter` | исход попытки |
| 1170 | `count_owner_letters_waiting` | строка «жду отправки» для /status |
