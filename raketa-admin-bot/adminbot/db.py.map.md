# Карта функций: adminbot/db.py

Обновлено 2026-09-23 (задача 10 ТЗ `2026-09-22-order-chain.md`: заказ с
известной сделкой — `_SELECT_ORDERS` читает `o.calendar_event_id`,
`o.deal_lead_id` (мастер выбрал запись календаря в сценарии рабочего бота,
задача 9), `_order_from_row` кладёт их в `Order`; `get_calendar_link`/
`update_calendar_link` без изменений — `order_id` уже был в
`_UPDATABLE_GCAL_FIELDS` (задача 8), движок и наблюдатель читают/пишут его
через `sync/store.py`, не напрямую; карта пересчитана целиком по факту
`grep -nE '^(async )?def '`, а не по памяти — тот самый урок ревью 23.09,
что и ниже).
Раньше — задача 11 ТЗ `2026-09-22-order-chain.md`: доводка сделки
после оплаты по счёту — `AmoLink` (`models.py`) получил `payment_pending`/
`payment_synced_at`, `_link_from_row` их читает, `_UPDATABLE_LINK_FIELDS` их
разрешает; добавлены `fetch_wire_paid_order_ids` (узкий запрос по
`public.orders`: способ оплаты безнал, `awaiting_wire_payment=false`) и
`fetch_links_needing_wire_payment_sync` (связки `amo_links`, ещё не доведённые).
Ещё раньше — задача 5 ТЗ `2026-09-22-order-chain.md`: сверка «номер + имя» записи
календаря с контактом сделки — `_calendar_from_row` читает четыре новых поля
(`contact_mismatch`, `contact_reminder_count/sent_at/muted`),
`_UPDATABLE_GCAL_FIELDS` их разрешает, добавлена `fetch_calendar_links_needing_contact_reminder`
по образцу `fetch_links_needing_address_reminder`; ревью 23.09 — карта
пересчитана целиком по факту `grep -nE '^(async )?def '`, а не по памяти:
секции «Автозвонок», «Почта владельца», «Удаления заказов» ошибочно не
сдвинулись на +36 строк вместе с календарной, и не хватало строки
`fetch_calendar_actions`).
Ещё раньше — задача 6 ТЗ `2026-09-22-order-chain.md`: «занято» считается
по номеру сделки-кандидата, а не по телефону — `fetch_taken_lead_ids`,
`fetch_carpet_taken_leads`, `fetch_calendar_taken_leads` принимают список
кандидатов `lead_ids` вместо `phone10`; круг правок по ревью — `fetch_carpet_taken_leads`
теперь смотрит обе колонки, `lead_id` и `primary_lead_id`, как и «зеркальные»
функции заказов и календаря).
Строк в файле: 1772.

Правило файла: в схему `public` (таблицы рабочего бота) не пишем никогда —
для неё здесь только SELECT. Всё собственное состояние живёт в схеме `adminbot`.

Работ у робота две разновидности, и лежат они в разных таблицах бота: заказы
химчистки (`public.orders`) и уборки (`public.cleaning_orders`). Номера в них
пересекаются, поэтому и таблицы связок разные — имя таблицы передаётся
параметром `table` (`LINKS_TABLE` / `CLEANING_LINKS_TABLE`, строки 35–38).

## Общее

| Строка | Функция | Назначение |
|---|---|---|
| 134 | `_init_connection` | jsonb ↔ dict без ручного json.dumps на каждом вызове |
| 141 | `create_pool` | пул соединений с нашим кодеком jsonb |
| 1040 | `apply_migration` | применить SQL-файл миграции (тесты и развёртывание) |
| 1023 | `get_setting` / 1029 `set_setting` | настройки владельца (пауза) |

## Заказы химчистки и уборки

| Строка | Функция | Назначение |
|---|---|---|
| 42 | `_SELECT_ORDERS` (константа SQL, не функция) | заказы бота за период: мастера, адрес из карточки клиента; с задачи 10 (ТЗ 2026-09-22) — ещё `calendar_event_id`, `deal_lead_id` |
| 93 | `_SELECT_CLEANING_ORDERS` (константа SQL, не функция) | уборки за период: свой адрес, бригадир, первая строка оплат, удалённые пропущены; этих двух колонок не знает — уборки календарной связки не имеют |
| 145 | `_order_from_row` | строка → `Order` (kind="order"); с задачи 10 — плюс `calendar_event_id`, `deal_lead_id` |
| 180 | `_cleaning_order_from_row` | строка → `Order` (kind="cleaning", «Услуга»=Уборка, «Специалист»=Ольга) |
| 164 | `fetch_orders_since` | все заказы с даты |
| 171 | `fetch_orders_by_ids` | заказы по номерам — вернуться к незавершённым |
| 208 | `fetch_cleaning_orders_since` | все уборки с даты |
| 215 | `fetch_cleaning_orders_by_ids` | уборки по номерам |
| 226 | `fetch_linked_order_ids` | какие работы уже взяты в работу (`table`); задача 6 — фильтр по живому заказу не нужен, `order_ids` уже без мёртвых (см. докстринг) |
| 245 | `fetch_unprocessed_orders` | заказы, по которым робот ещё ничего не начинал |
| 254 | `fetch_unprocessed_cleaning_orders` | то же по уборкам |

## Связки работа → сделка (`amo_links` / `cleaning_links`)

Все принимают `table=` — по умолчанию таблица заказов химчистки. С миграции 012
у связки есть `deal_address` — адрес сделки амо на момент, когда робот её
заполнял (движок `sync/engine.py`, `_fill_lead` и путь `already_done`); ПД,
в логи не идёт. С миграции 013 (задача 7) у связки есть ещё три поля
напоминания «сделка без адреса»: `address_reminder_count`,
`address_reminder_sent_at`, `address_reminder_muted` — их читает и пишет
`sync/address_reminder.py`, а не движок.

С миграции 014 «мёртвый заказ» проверяется в SQL, а не в Python (задача 6 ТЗ
2026-09-17 «удаление заказа освобождает сделку»): `order_alive_clause` даёт
EXISTS-условие против `public.orders` (химчистка — физическое удаление) или
`public.cleaning_orders WHERE deleted_at IS NULL` (уборка — мягкое). Событие об
удалении может не дойти, поэтому это не дубль обработчика удаления (задача 5),
а единственная проверка, которая работает и без события. С ревью 17.09
(замечание 2) функция публичная и единственная на весь проект: тот же вызов
собирает `_OTHER_LIVE_HOLDER_SQL` в `sync/deletions.py` — раньше там было своё
определение («нет в регистре `deleted_orders`»), расходившееся с этим на
сироте без записи в регистре.

С задачи 8 (ТЗ 2026-09-21, кнопки-ответы владельца) `update_link_and_log`
объединяет `update_link`+`log_action` в одной транзакции: если связка не
обновилась, строки в журнале тоже не будет. `count_owner_handled` считает по
этому журналу, сколько раз владелец нажал «Сам разберусь» (`payload->>'choice'
= 'manual'`) — связка сама этого не показывает, «Сам разберусь» и
автоматическое `already_done` пишут её одинаково.

С задачи 6 (ТЗ 2026-09-22 «цепочка заказа») `fetch_taken_lead_ids` принимает
`lead_ids` — уже найденных по телефону кандидатов (результат
`find_leads_by_phone`), а не телефон: занятой считается любая из ЭТИХ сделок,
закреплённая за другой работой, независимо от того, каким телефоном та работа
привязана. Один и тот же человек с двумя номерами больше не выглядит для
робота двумя разными клиентами. Тот же приём — у `fetch_carpet_taken_leads` и
`fetch_calendar_taken_leads` ниже.

С задачи 10 (та же ТЗ) заказ с `deal_lead_id`/`calendar_event_id` эти функции
вообще не проходит — движок (`sync/engine.py`, `_decide_from_calendar`) минует
матчер и «занято» целиком, сделку называет мастер или уже знает запись
календаря (`gcal_events.real_lead_id`).

С задачи 11 (та же ТЗ) у связки есть ещё два поля доводки оплаты по счёту:
`payment_pending` (ставит движок в `_step_move_realization_done`, когда
переводит сделку в «Заказ выполнен» из-за неоплаченного счёта) и
`payment_synced_at` (когда робот довёл сделку до конца после прихода денег;
второй раз связка с непустым значением не берётся). `fetch_wire_paid_order_ids`
смотрит только `public.orders` (способ оплаты, `awaiting_wire_payment`) —
номера оттуда сужают `fetch_links_needing_wire_payment_sync`, которая уже
смотрит только `amo_links`; join двух баз здесь, как и везде в файле, идёт в
Python, а не в SQL (базы бота и админ-бота не обязаны быть одним Postgres).

| Строка | Функция | Назначение |
|---|---|---|
| 264 | `_link_from_row` | строка → `AmoLink` (включая `deal_address`, поля напоминания и доводки оплаты) |
| 295 | `create_link` | взять работу в работу; повтор ничего не портит |
| 313 | `get_link` | привязка по номеру работы |
| 320 | `update_link` | обновить поля из белого списка (`_UPDATABLE_LINK_FIELDS`) |
| 340 | `mark_checklist_step` | отметить выполненный шаг: после сбоя продолжим отсюда |
| 355 | `log_action` | журнал действий в амо (и в репетиции тоже) |
| 377 | `update_link_and_log` | задача 8: `update_link`+`log_action` одной транзакцией — для кнопок-ответов владельца |
| 418 | `fetch_actions` | журнал по одной работе |
| 427 | `count_owner_handled` | задача 8: сколько раз владелец нажал «Сам разберусь» (`answer_owner`/`choice=manual`) за окно суток — источник «Передано администратору» |
| 448 | `fetch_touched_order_ids` | задача 2 (ТЗ 2026-09-21): номера работ из журнала за окно суток, свёрнутые в множество — источник «Провёл из бота» (пути A/B/C вместе), не `updated_at`; `address_reminder_capped` исключено — не работа над заказом |
| 494 | `order_alive_clause` | задача 6 (ТЗ 2026-09-17), публичная с ревью 17.09: EXISTS-предикат «заказ ещё жив» под нужную таблицу связок — общая с `sync/deletions.py` |
| 501 | `fetch_taken_lead_ids` | из переданных `lead_ids` — занятые другими работами; UNION по ОБЕИМ таблицам связок, мёртвые связки исключены (`order_alive_clause` в обеих ветках); задача 6 ТЗ 2026-09-22 — занятость по номеру сделки, не по телефону |
| 549 | `fetch_link_ids_by_status` | номера незаконченных работ; задача 6 (ТЗ 2026-09-17) — фильтр сознательно не добавлен, оба вызова из `watcher.py` уже упираются в `fetch_orders_by_ids`/`fetch_cleaning_orders_by_ids`, которые мёртвый заказ не вернут (см. докстринг) |
| 572 | `fetch_links_for_orders` | сырьё для вечерней сводки; задача 6 (ТЗ 2026-09-17) — не нужен фильтр, `order_ids` уже без мёртвых заказов (см. докстринг) |
| 592 | `count_links_by_status` | очередь для /status; задача 6 (ТЗ 2026-09-17) — сознательно без фильтра, это счётчик для владельца, а не действие (см. докстринг) |
| 610 | `fetch_links_needing_address_reminder` | кандидаты на напоминание: `done`+путь C, адреса нет, не muted, счётчик и сутки позволяют, заказ жив (`order_alive_clause`, задача 6 ТЗ 2026-09-17) |
| 645 | `fetch_wire_paid_order_ids` | задача 11 (ТЗ 2026-09-22): номера заказов бота по счёту, уже оплаченных (`awaiting_wire_payment=false`) — только `public.orders`, без уборок (у них ожидания оплаты нет) |
| 667 | `fetch_links_needing_wire_payment_sync` | задача 11: из переданных номеров — связки `done`, ещё не доведённые (`payment_synced_at IS NULL`, `COALESCE(payment_pending, true)`) |

## Ковры от партнёра (`carpet_links`, `carpet_letters`)

| Строка | Функция | Назначение |
|---|---|---|
| 706 | `_carpet_from_row` | строка → `CarpetLink` |
| 732 | `create_carpet_link` | взять строку отчёта в работу |
| 754 | `remember_carpet_link` | заказ из архива партнёра сразу как сделанный (`done`/`remembered`), одной записью; существующую не трогает |
| 779 | `fetch_pending_carpet_rows` | незавершённые заказы партнёра со строками отчёта |
| 799–838 | `get_/update_/mark_carpet_step/log_carpet_action` | то же, что у заказов |
| 854 | `update_carpet_link_and_log` | задача 8: то же самое, что `update_link_and_log`, но для ковровой связки |
| 887 | `fetch_carpet_taken_leads` | из переданных `lead_ids` — ковровые сделки, занятые другими заказами (задача 6 ТЗ 2026-09-22 — по номеру сделки, не по телефону); круг правок по ревью — смотрит обе колонки, `lead_id` и `primary_lead_id` (путь `use_primary`), как и «зеркальные» `fetch_taken_lead_ids`/`fetch_calendar_taken_leads` |
| 922 | `fetch_carpet_links_by_status` | незаконченные заказы партнёра |
| 936 | `count_carpet_links_by_status` | очередь ковров |
| 943 | `remember_letter` | письмо разобрано: страховка от повторного разбора; снимает отметки об отложении |
| 962 | `letter_state` | что робот помнит о письме: `processed` / `held` / `released` / ничего |
| 980 | `hold_letter` | отложить письмо: робот его не проводит и ждёт владельца |
| 997 | `release_letter` | владелец разрешил провести отложенное письмо |
| 1010 | `held_letters` | что сейчас отложено — для `--carpets-held` |

Ковры, календарь и автозвонок работают по своим сделкам (`carpet_links`,
`gcal_events`, `autocall_leads`), не по `public.orders`/`public.cleaning_orders`
рабочего бота — задача 6 (ТЗ 2026-09-17, мёртвый заказ) их не касается: понятия
«заказ удалён» там нет.

## Календарь (`gcal_events`, `gcal_cursor`)

С задачи 10 (ТЗ 2026-09-22) у записи есть встречное использование: заказ бота
приходит с `calendar_event_id`/`deal_lead_id`, а после того как заказ проведён,
наблюдатель (`sync/watcher.py`, `_link_calendar_order`) пишет номер заказа
обратно через `update_calendar_link(..., order_id=...)` — поле `order_id` в
`_UPDATABLE_GCAL_FIELDS` уже было (задача 8), новых функций здесь не
понадобилось.

| Строка | Функция | Назначение |
|---|---|---|
| 1061 | `_as_dict` / 1161 `_gcal_value` | jsonb и text[] в виде, который принимает asyncpg |
| 1068 | `_calendar_from_row` | строка → `CalendarLink`; с задачи 5 (ТЗ 2026-09-22) читает и четыре поля сверки контакта |
| 1106–1181 | `get_/create_/update_calendar_link`, `mark_calendar_step`, `log_calendar_action` | ход работы по записи; `get_/update_calendar_link` — с задачи 10 их зовёт ещё и `sync/store.py` (движок и наблюдатель заказов) |
| 1197 | `update_calendar_link_and_log` | задача 8: то же самое, что `update_link_and_log`, но для записи календаря |
| 1230 | `count_calendar_created` | задача 2 (ТЗ 2026-09-21): сколько событий получили `create_lead` в журнале за окно суток — источник «Завёл из календаря» |
| 1247 | `count_calendar_owner_handled` | задача 8: то же самое, что `count_owner_handled`, но по журналу календаря — подмешивается снаружи в «Передано администратору», как и `count_calendar_created` в «Завёл из календаря» |
| 1263 | `fetch_calendar_taken_leads` | из переданных `lead_ids` — сделки, занятые ДРУГИМИ записями (задача 6 ТЗ 2026-09-22 — по номеру сделки, не по телефону записи) |
| 1294 | `fetch_pending_calendar_links` | записи, работа по которым не закончена |
| 1308 | `fetch_calendar_links_without_report` | сделано, а владельцу не отчитались |
| 1331 | `fetch_calendar_links_needing_contact_reminder` | задача 5 (ТЗ 2026-09-22): записи с расхождением «номер + имя», которым пора напомнить владельцу — по образцу `fetch_links_needing_address_reminder`; самоостановка не флагом, а снятым `contact_mismatch` |
| 1360 | `count_calendar_links_by_status` | очередь календаря |
| 1367 | `get_calendar_cursor` / 1396 `save_calendar_cursor` | закладка обмена, своя у каждого календаря |
| 1413 | `find_calendar_link_by_question_msg` | запись по номеру карточки в Telegram |
| 1429 | `delete_calendar_link` | забыть запись (ручной разбор последствий) |
| 1435 | `fetch_calendar_actions` | что робот делал по записи |

## Автозвонок (`autocall_leads`, курсор опроса)

| Строка | Функция | Назначение |
|---|---|---|
| 1459 | `_autocall_from_row` | строка → `AutocallLead` |
| 1478–1509 | `get_/create_/update_autocall_lead` | цепочка попыток дозвона |
| 1528 | `fetch_due_autocall_leads` | созревшие цепочки, просроченные первыми |
| 1549 | `log_autocall_action` / 1561 `fetch_autocall_actions` | журнал по заявке |
| 1690 | `get_autocall_cursor` / 1698 `save_autocall_cursor` | с какого `created_at` читать амо |

## Почта владельца (`owner_outbox`)

| Строка | Функция | Назначение |
|---|---|---|
| 1576 | `add_owner_letter` | положить недоставленное сообщение в долг |
| 1596 | `fetch_due_owner_letters` | созревшие долги, старые первыми |
| 1618 | `fetch_owner_letters_for` | незаконченные долги по одной записи |
| 1639 | `mark_owner_letter_sent` / 1652 `postpone_owner_letter` / 1666 `drop_owner_letter` | исход попытки |
| 1680 | `count_owner_letters_waiting` | строка «жду отправки» для /status |

## Удаления заказов и уборок (задача 4 ТЗ 2026-09-17 «удаление заказа освобождает сделку»)

Один запрос на вид работы, оба — прямой JOIN между `public` (регистр рабочего
бота) и `adminbot.order_deletions_seen` (свои отметки, миграция 014): обе схемы
живут в одной базе под одной ролью, диф считается в Postgres, а не в Python.
Разбор неразобранных записей (задача 5) в этот файл не входит. Страховку от
сирот (задача 6 ТЗ 2026-09-17 — мёртвая связка не считается действующей) см. в
разделе «Связки работа → сделка» выше: `order_alive_clause` и её применение.

| Строка | Функция | Назначение |
|---|---|---|
| 1724 | `fetch_pending_order_deletions` | удалённые заказы химчистки (`public.deleted_orders`) без отметки |
| 1750 | `fetch_pending_cleaning_deletions` | уборки с `deleted_at` без отметки |
