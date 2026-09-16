# Карта функций: adminbot/db.py

Обновлено 2026-09-16 (колонка `deal_address` в связках, миграция 012). Строк в файле: 1290.

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
| 732 | `apply_migration` | применить SQL-файл миграции (тесты и развёртывание) |
| 715 | `get_setting` / 721 `set_setting` | настройки владельца (пауза) |

## Заказы химчистки и уборки

| Строка | Функция | Назначение |
|---|---|---|
| 40 | `_SELECT_ORDERS` (константа SQL, не функция) | заказы бота за период: мастера, адрес из карточки клиента |
| 89 | `_SELECT_CLEANING_ORDERS` (константа SQL, не функция) | уборки за период: свой адрес, бригадир, первая строка оплат, удалённые пропущены |
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

Все принимают `table=` — по умолчанию таблица заказов химчистки. С миграции 012
у связки есть `deal_address` — адрес сделки амо на момент, когда робот её
заполнял (движок `sync/engine.py`, `_fill_lead`); ПД, в логи не идёт.

| Строка | Функция | Назначение |
|---|---|---|
| 251 | `_link_from_row` | строка → `AmoLink` (включая `deal_address`) |
| 277 | `create_link` | взять работу в работу; повтор ничего не портит |
| 295 | `get_link` | привязка по номеру работы |
| 302 | `update_link` | обновить поля из белого списка (`_UPDATABLE_LINK_FIELDS`) |
| 322 | `mark_checklist_step` | отметить выполненный шаг: после сбоя продолжим отсюда |
| 337 | `log_action` | журнал действий в амо (и в репетиции тоже) |
| 359 | `fetch_actions` | журнал по одной работе |
| 368 | `fetch_taken_lead_ids` | занятые сделки клиента — UNION по ОБЕИМ таблицам связок |
| 401 | `fetch_link_ids_by_status` | номера незаконченных работ |
| 415 | `fetch_links_for_orders` | сырьё для вечерней сводки |
| 429 | `count_links_by_status` | очередь для /status |

## Ковры от партнёра (`carpet_links`, `carpet_letters`)

| Строка | Функция | Назначение |
|---|---|---|
| 452 | `_carpet_from_row` | строка → `CarpetLink` |
| 478 | `create_carpet_link` | взять строку отчёта в работу |
| 500 | `remember_carpet_link` | заказ из архива партнёра сразу как сделанный (`done`/`remembered`), одной записью; существующую не трогает |
| 525 | `fetch_pending_carpet_rows` | незавершённые заказы партнёра со строками отчёта |
| 545–584 | `get_/update_/mark_carpet_step/log_carpet_action` | то же, что у заказов |
| 600 | `fetch_carpet_taken_leads` | занятые ковровые сделки клиента |
| 614 | `fetch_carpet_links_by_status` | незаконченные заказы партнёра |
| 628 | `count_carpet_links_by_status` | очередь ковров |
| 635 | `remember_letter` | письмо разобрано: страховка от повторного разбора; снимает отметки об отложении |
| 654 | `letter_state` | что робот помнит о письме: `processed` / `held` / `released` / ничего |
| 672 | `hold_letter` | отложить письмо: робот его не проводит и ждёт владельца |
| 689 | `release_letter` | владелец разрешил провести отложенное письмо |
| 702 | `held_letters` | что сейчас отложено — для `--carpets-held` |

## Календарь (`gcal_events`, `gcal_cursor`)

| Строка | Функция | Назначение |
|---|---|---|
| 750 | `_as_dict` / 846 `_gcal_value` | jsonb и text[] в виде, который принимает asyncpg |
| 757 | `_calendar_from_row` | строка → `CalendarLink` |
| 791–866 | `get_/create_/update_calendar_link`, `mark_calendar_step`, `log_calendar_action` | ход работы по записи |
| 882 | `fetch_calendar_taken_leads` | сделки, занятые ДРУГИМИ записями того же клиента |
| 903 | `fetch_pending_calendar_links` | записи, работа по которым не закончена |
| 917 | `fetch_calendar_links_without_report` | сделано, а владельцу не отчитались |
| 940 | `count_calendar_links_by_status` | очередь календаря |
| 947 | `get_calendar_cursor` / 976 `save_calendar_cursor` | закладка обмена, своя у каждого календаря |
| 993 | `find_calendar_link_by_question_msg` | запись по номеру карточки в Telegram |
| 1009 | `delete_calendar_link` | забыть запись (ручной разбор последствий) |
| 1015 | `fetch_calendar_actions` | что робот делал по записи |

## Автозвонок (`autocall_leads`, курсор опроса)

| Строка | Функция | Назначение |
|---|---|---|
| 1039 | `_autocall_from_row` | строка → `AutocallLead` |
| 1058–1089 | `get_/create_/update_autocall_lead` | цепочка попыток дозвона |
| 1108 | `fetch_due_autocall_leads` | созревшие цепочки, просроченные первыми |
| 1129 | `log_autocall_action` / 1141 `fetch_autocall_actions` | журнал по заявке |
| 1270 | `get_autocall_cursor` / 1278 `save_autocall_cursor` | с какого `created_at` читать амо |

## Почта владельца (`owner_outbox`)

| Строка | Функция | Назначение |
|---|---|---|
| 1156 | `add_owner_letter` | положить недоставленное сообщение в долг |
| 1176 | `fetch_due_owner_letters` | созревшие долги, старые первыми |
| 1198 | `fetch_owner_letters_for` | незаконченные долги по одной записи |
| 1219 | `mark_owner_letter_sent` / 1232 `postpone_owner_letter` / 1246 `drop_owner_letter` | исход попытки |
| 1260 | `count_owner_letters_waiting` | строка «жду отправки» для /status |
