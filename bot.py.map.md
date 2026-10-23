# Карта функций `bot.py` (tgbot-v1)

Дата: 2026-10-23
Файл: `bot.py`, строк: 14955
Объявлений верхнего уровня (`grep -c "^def \|^class \|^async def " bot.py`): 488.

Обновлено 2026-10-23 (механическая правка М1, ТЗ `docs/plans/2026-10-23-autocall-manager-button.md`, фильтр ревью): в `autocall_called_cb` пустой текст нажатого сообщения не затирается — только убирается кнопка (+15 строк). Новых функций нет; номера после неё пересчитаны по всему файлу.

Обновлено 2026-10-23 (автозвонок, кнопка «Я уже перезвонил», ТЗ `docs/plans/2026-10-23-autocall-manager-button.md`, задача 1): константа `AUTOCALL_CALLED_CB_PREFIX` после `ORDER_CLOSE_SNOOZE_CB_PREFIX` (+5 строк); после `order_close_snooze_cb` — константы `AUTOCALL_LEADS_SOURCE`, `AUTOCALL_LIVE_STATUSES`, `AUTOCALL_CALLED_ACCEPTED_LINE` и новый `autocall_called_cb` (таблица нажатий — миграция `0019_autocall_manager_callbacks.sql`). Номера пересчитаны по всему файлу.

Обновлено 2026-10-23 (ложное «да» менеджера 21.10): перед `handle_wahelp_inbound` — константа `WAHELP_CLIENT_DESTINATIONS` (клиентом считаются только метки `from_client` и `from`, чужая метка — в журнал без текста и телефона); `decide_on_answer` получает время работы. Номера пересчитаны по всему файлу.

Обновлено 2026-10-13 (сторож проведения заказов, ТЗ `docs/plans/2026-10-13-order-close-reminder.md`, задача 2): импорт из `notifications/order_close_reminder.py` расширен (под другими именами, +11 строк); выключатели `ORDER_CLOSE_REMINDER_ENABLED`, `STAFF_CHAT_ID`, `ORDER_CLOSE_REMINDER_INTERVAL_SEC`, `ORDER_CLOSE_SNOOZE_CB_PREFIX` после `UNSORTED_DONE_CB_PREFIX`; в `IgnoreNonPrivateMiddleware` — пропуск кнопки «Напомнить завтра» из групп; глобальная `order_close_task`; после `_order_close_candidates` — `_order_close_now`, `_order_close_chats`, `_order_close_fields`, `_order_close_delete`, `_order_close_edit`, `_order_close_refresh`, `_order_close_send_due`, `run_order_close_reminders`, `order_close_snooze_cb`; запуск и остановка задачи `order_close_reminders` в `main`. Номера пересчитаны по всему файлу.

Обновлено 2026-10-13 (сторож проведения заказов, ТЗ `docs/plans/2026-10-13-order-close-reminder.md`, задача 1): импорт `LOOKBACK_DAYS` из `notifications/order_close_reminder.py` (+1 строка после импорта `amocrm_unsorted`); после `unsorted_card_done_cb` — константа `ORDER_CLOSE_JOBS_SOURCE` и новая `_order_close_candidates`. Номера пересчитаны по всему файлу.

Обновлено 2026-10-13 (правки по ревью, `docs/plans/2026-10-13-reschedule-review-fixes.md`): правка 1 — `_confirmation_reschedule` берёт блокировки как работник и возвращает `busy` по письму в отправке; `_confirmation_check_moved` на `busy` — `retry`; `_confirmation_before_send` отменяет письмо-вопрос без ожидания. Правка 2 — первый проход обхода переносов засчитывается только чистым (константа `RESCHEDULE_UNFINISHED`). Правка 3 — `rescheduled_at` ставит только `reask`. Правка 4 — колонка `order_confirmations.reschedule_dropped_at` в `ensure_client_messaging_schema` (закрыто пересчётом), обход берёт такие `dropped` и смотрит на 7 дней назад (`RESCHEDULE_DEPTH`). Правка 5 — исход `drop` в `_confirmation_reschedule`, `_confirmation_check_moved`, `_confirmation_watch_deal`; правило получает статус ожидания. Новых функций нет; номера пересчитаны по всему файлу.

Обновлено 2026-10-13 (ночная тишина, ТЗ `docs/plans/2026-10-13-quiet-hours.md`, задача 1): выключатель `CLIENT_QUIET_HOURS_ENABLED` (после `CLIENT_MESSAGING_INTERVAL_SEC`, +4 строки) и его передача в `NotificationWorker` внутри `main` (+1 строка). Новых функций нет; номера пересчитаны по всему файлу.

Обновлено 2026-10-13 (перенос заказа, ТЗ `docs/plans/2026-10-13-reschedule-confirmation.md`, задача 2): новые `_confirmation_watch_deal`, `run_confirmation_reschedule_watch` (обход переносов раз в 10 минут; константы `RESCHEDULE_FIRST_PASS_DEPTH`, `RESCHEDULE_WATCH_LIMIT` и признак первого прохода `_reschedule_watch_first_pass_done` — перед ними); глобальная `reschedule_watch_task` и её запуск в `on_startup` задачей `confirmation_reschedule`. Номера пересчитаны по всему файлу.

Обновлено 2026-10-13 (перенос заказа, ТЗ `docs/plans/2026-10-13-reschedule-confirmation.md`, задача 1): колонка `order_confirmations.rescheduled_at` в `ensure_client_messaging_schema`; новые `_confirmation_moved_to`, `_confirmation_iso`, `_confirmation_reschedule`, `_confirmation_check_moved`; `_confirmation_before_send` — `may_ask_question` с признаком переноса и последней проверкой даты. Номера пересчитаны по всему файлу.

Обновлено 2026-10-05: удалена сломанная с 02.12.2025 `/my_daily` (`my_daily_report`, решение владельца), `/help` админов — без `/mysalary`, `/myincome`, Диме — отмены уборки и выплаты; номера пересчитаны.

Обновлено 2026-10-05 (меню админа, задача 5): `set_commands` — синее меню по ролям (решение владельца 3): у всех `/start` «Открыть меню» и `/help` «Помощь», у `admin` и `superadmin` свои списки, «Кто я» убрана, у клинера и мастера — как без роли. `help_cmd` — админам команды, которых нет ни на кнопках, ни в синем меню, группами «Химчистка» и «Клининг»; клинеру, мастеру и без роли — одна строка с контактом. Номера пересчитаны по всему файлу.

Обновлено 2026-10-05 (меню админа, задача 4): удалены команды `/import_amocrm`, `/upload_clients`, `/import_leads_dryrun`, `/import_leads`, `/db_apply_cash_trigger` и код, нужный только им: их обработчики, группы `UploadFSM` и `AmoImportFSM`, разбор CSV `process_amocrm_csv` с помощниками `_amo_get_cell`, `_amo_parse_decimal`, `_amo_parse_datetime`, `_ensure_dt_aware`, `_amo_split_services`, `_amo_merge_services`, `_format_amocrm_counters`; пункт `/import_amocrm` в `set_commands` и `/help`, `UploadFSM` в `cancel_any`. `_amo_normalize_phone` (обмен с amoCRM) и `_last_birthday_date` (бонусы ко дню рождения) оставлены. Номера пересчитаны по всему файлу.

Обновлено 2026-10-05 (меню админа, задача 3): главное меню админа `[Операции, Отчёты]`, `[Клиенты, Клининг]`; подменю «Операции» и «Клининг» (`admin_operations_kb`, `admin_cleaning_kb`, состояния `AdminMenuFSM.operations`/`cleaning`), их кнопки и «Выберите раздел:» (`admin_pick_section`); `admin_menu_return` для возврата админа в главное меню после клининговых операций; удалены кнопка «Мастера» и всё, что достижимо только через неё (`AdminMastersFSM`, `admin_masters_*`). Номера пересчитаны по всему файлу.

Обновлено 2026-10-05 (меню админа, задача 2): удалена команда `/cleaning_cash_expense` (расход кассы клининга у всех один — `/cleaning_expense` в `cleaning/handlers.py`): её пункт из синего меню админов (`set_commands`) и группа `CleaningCashExpenseFSM` из `cleaning_prefixes` в `cancel_any`. Номера после `set_commands` сдвинуты на 3, после `cancel_any` — ещё на 1.

Обновлено 2026-10-05 (меню админа, задача 1): фильтр `admin_menu_idle` — кнопки главного меню админа (Приход, Расход, Изъятие, Привязать, Мастера, Клиенты, Рассчитать ЗП) срабатывают без `/start`; `cancel_any` и `unknown` ставят админу `AdminMenuFSM.root`. Номера после `admin_root_kb` сдвинуты на 15, после `cancel_any` — ещё на 1, после `unknown` — ещё на 1.

Обновлено 2026-10-05: удалена команда `/wipe_test_data` (`wipe_test_data`, стирала базу химчистки без подтверждения — решение владельца), номера после неё сдвинуты на 24.

Обновлено 2026-10-05 (перемещение денег клининга, задача 4): в `cancel_any` в `cleaning_prefixes` добавлена группа `CleaningCashMoveDeleteFSM` (`/cleaning_move_delete` в `cleaning/handlers.py`), номера после `cancel_any` сдвинуты на 1.

Обновлено 2026-10-05 (перемещение денег клининга, задача 3): в `cancel_any` в `cleaning_prefixes` добавлена группа `CleaningCashMoveFSM` (`/cleaning_move` в `cleaning/handlers.py`), номера после `cancel_any` сдвинуты на 1.

Обновлено 2026-10-05 (реестр денег Оли, задача 4): в синее меню админов (`set_commands`) — `/cleaning_olya` «Клининг: деньги Оли» (сам обработчик в `cleaning/handlers.py`), номера после `set_commands` сдвинуты на 2.

Обновлено 2026-10-05 (реестр денег Оли, задача 2): `_dispatch_cleaning_order_report` передаёт остаток денег Оли из payload очереди в сообщение об уборке, номера после него сдвинуты на 1.

Обновлено 2026-10-05: `_unlink_wire_entry` — удаление привязанного перевода откатывает заказ (решение владельца), номера пересчитаны по всему файлу. Тогда же у клинера убрана «💸 Выплата» (мост и `ROLE_MATRIX`), админам в синее меню — «Клининг: расход».

Обновлено 2026-09-30: ответ-цифра клиента относится к уборке или к химчистке (задача 2 ТЗ `docs/plans/2026-09-30-cleaning-ratings.md`), номера после `_select_pending_rating_order` сдвинуты.

Обновлено 2026-09-28: удалено старое сообщение админам «откликнулся на промо» (`_notify_admins_about_promo_interest`, решение владельца), номера пересчитаны заново по всему файлу.

| Строка | Функция/класс | Назначение |
|---|---|---|
| 33 | `AdminMenuFSM` | Состояния FSM меню админа: главное меню (`root`), подменю «Операции» (`operations`) и «Клининг» (`cleaning`), `clients` |
| 40 | `AdminClientsFSM` | Состояния FSM раздела «Клиенты» в админке |
| 48 | `AdminPayrollFSM` | Состояния FSM расчёта зарплаты мастера |
| 54 | `IncomeFSM` | Состояния FSM ввода дохода |
| 62 | `WireLinkFSM` | Состояния FSM привязки банковского перевода к заказу |
| 70 | `ExpenseFSM` | Состояния FSM ввода расхода |
| 78 | `WithdrawFSM` | Состояния FSM вывода средств |
| 84 | `TxDeleteFSM` | Состояния FSM удаления транзакции |
| 89 | `DividendFSM` | Состояния FSM выплаты дивидендов |
| 95 | `InvestmentFSM` | Состояния FSM ввода инвестиции |
| 102 | `OrderDeleteFSM` | Состояния FSM удаления заказа |
| 108 | `AddMasterFSM` | Состояния FSM добавления мастера |
| 114 | `ReportsFSM` | Состояния FSM раздела отчётов |
| 240 | `is_valid_phone_format` | Проверка формата телефона |
| 246 | `_env_int` | Чтение целочисленной переменной окружения |
| 426 | `_parse_telegram_api_ips` | Разбор списка IP Telegram API из настроек |
| 444 | `_build_telegram_session` | Сборка aiohttp-сессии для Telegram с нужными IP |
| 456 | `_make_telegram_bot` | Создание объекта бота по токену |
| 468 | `_google_access_token` | Получение токена доступа Google (для Sheets) |
| 480 | `_fetch_sheet_rows` | Чтение строк из Google Sheets |
| 496 | `_parse_sheet_texts` | Разбор строк таблицы в тексты рассылок |
| 517 | `get_promo_texts` | Получение текстов промо-рассылки |
| 529 | `get_birthday_texts` | Получение текстов поздравлений с днём рождения |
| 541 | `_log_missing_messenger` | Лог отсутствия мессенджера у контакта |
| 576 | `IgnoreNonPrivateMiddleware` | Middleware, игнорирующее не-приватные чаты |
| 602 | `_try_enqueue_notification` | Постановка уведомления в очередь отправки |
| 630 | `_format_expire_label` | Форматирование даты сгорания бонусов для текста |
| 642 | `_get_next_bonus_expire_date` | Получение ближайшей даты сгорания бонусов клиента |
| 658 | `ensure_promo_schema` | Создание/проверка таблиц схемы промо-рассылок |
| 749 | `ensure_daily_job_schema` | Создание/проверка таблицы учёта суточных джобов |
| 760 | `ensure_service_heartbeat_schema` | Создание/проверка таблицы heartbeat сервисов |
| 851 | `ensure_jenya_card_schema` | Создание/проверка таблицы карты «Женя» |
| 897 | `ensure_client_channels_schema` | Создание/проверка таблицы каналов связи с клиентом |
| 965 | `ensure_amocrm_api_schema` | Создание/проверка таблиц API-обмена с amoCRM |
| 1006 | `ensure_client_messaging_schema` | Создание/проверка таблиц переписки с клиентами |
| 1127 | `_amocrm_get_cursor` | Чтение курсора последней обработки потока amoCRM |
| 1146 | `_amocrm_set_cursor` | Сохранение курсора обработки потока amoCRM |
| 1160 | `_to_int` | Безопасное приведение значения к int |
| 1169 | `_amocrm_api_enabled` | Проверка, включена ли интеграция API amoCRM |
| 1173 | `_amocrm_default_cursor` | Значение курсора amoCRM по умолчанию |
| 1178 | `_amocrm_created_at` | Извлечение даты создания из payload amoCRM |
| 1184 | `_amocrm_payload_json` | Сериализация payload amoCRM в JSON-строку |
| 1188 | `_amocrm_payload_from_db` | Десериализация payload amoCRM из БД |
| 1201 | `_amocrm_mark_event_action` | Отметка выполненного действия по событию amoCRM |
| 1224 | `_amocrm_fetch_first_contact` | Получение первого контакта сделки amoCRM |
| 1241 | `_exchange_fetch_deal` | Получение сделки amoCRM для модуля «Обмен» |
| 1255 | `_exchange_handle_lead` | Обработка сделки в рамках сценария «Обмен» |
| 1290 | `_amocrm_poll_exchange_once` | Один цикл опроса сделок «Обмен» |
| 1403 | `run_exchange_cycle` | Запуск цикла обработки «Обмен» с обработкой ошибок |
| 1420 | `run_weekly_leads_exchange` | Еженедельный прогон обмена/рассылки по лидам |
| 1502 | `_confirmation_find_deal` | Поиск сделки amoCRM для подтверждения заказа |
| 1540 | `_confirmation_apply` | Применение результата подтверждения к заказу в БД |
| 1617 | `_confirmation_handle_lead` | Обработка сделки в сценарии подтверждения заказа |
| 1670 | `_amocrm_poll_confirmations_once` | Один цикл опроса сделок на подтверждение |
| 1764 | `_confirmation_poll_cancellations` | Опрос сделок на предмет отмены подтверждения |
| 1850 | `_confirmation_enqueue_cancel` | Постановка в очередь уведомления об отмене |
| 1912 | `run_client_messaging_cycle` | Цикл обработки очереди переписки с клиентами |
| 1935 | `_confirmation_moved_to` | Причина отмены старого вопроса «заказ перенесён на ДД.ММ.ГГГГ ЧЧ:ММ» (по Москве) (ТЗ 2026-10-13 перенос заказа, задача 1) |
| 1943 | `_confirmation_iso` | Дата в ISO-строку для журнала, `None` — как есть |
| 1947 | `_confirmation_reschedule` | Пересчёт вопроса после переноса заказа: блокировки как у работника (письмо, потом ожидание), под ними решает `decide_on_reschedule`; старое письмо в отправке не в руках зовущего — `busy`, ничего не трогает; `replan`/`reask` — отмена старого письма (кроме письма в руках работника), новое за сутки до новой даты, сброс ответа, `rescheduled_at` — только при `reask`; `update_time` — только `order_at`; `drop` (новая дата прошла) — снять ждущий вопрос, закрыть ожидание с `reschedule_dropped_at`, без ждущего письма — `none`; журнал `confirmation_reschedule` |
| 2124 | `_confirmation_check_moved` | Последняя проверка перед вопросом: дата в сделке другая (`replan`) или уже прошла (`drop`) → пересчёт и вердикт `cancel` «заказ перенесён на …»; репетиция — только лог; сбой пересчёта или `busy` — `retry` |
| 2178 | `_confirmation_before_send` | Проверка перед отправкой уведомления подтверждения (PreSendVerdict); письмо-вопрос без ожидания — `cancel` «вопрос больше не связан с заказом»; `may_ask_question` с признаком переноса, последней — дата сделки (`_confirmation_check_moved`) |
| 2290 | `_confirmation_after_send` | Действия после отправки уведомления подтверждения |
| 2313 | `run_unasked_question_watch` | Слежение за незаданными вопросами клиентов |
| 2386 | `run_confirmation_deferred_retry` | Повторная попытка отложенного подтверждения |
| 2443 | `run_confirmation_silence_watch` | Слежение за молчанием клиента после подтверждения |
| 2528 | `_confirmation_watch_deal` | Один заказ обхода переносов: читает сделку; нет / не воронка реализации / «ведёт владелец» — `skip`; сбой CRM — `crm_silent`; правило `decide_on_reschedule` (со статусом ожидания), репетиция — только лог; адрес из лида, если в сделке пусто (кроме `drop`); исход `none` пересчёта в лог не пишется; пересчёт `_confirmation_reschedule` (ТЗ 2026-10-13 перенос заказа, задача 2) |
| 2599 | `run_confirmation_reschedule_watch` | Обход переносов раз в 10 минут: живые ожидания со сделкой (`planned`/`confirmed`/`refused`/`owner_notified`) и закрытые пересчётом (`dropped` с `reschedule_dropped_at`), сохранённая дата не старше 7 дней (`RESCHEDULE_DEPTH`); первый проход после старта — на 30 дней назад (`RESCHEDULE_FIRST_PASS_DEPTH`), засчитывается только чистый (без исходов `RESCHEDULE_UNFINISHED` и без упора в лимит), лимит `RESCHEDULE_WATCH_LIMIT` = 200; один клиент amoCRM на проход |
| 2663 | `_amocrm_mark_unsorted_action` | Отметка действия и причины по записи в `amocrm_unsorted_seen` (ТЗ 2026-09-23 карточки «Неразобранного», задача 3) |
| 2677 | `_amocrm_poll_unsorted_once` | Один цикл опроса «Неразобранного»: заводит дело в `amocrm_unsorted_cards` (или отмечает пропуск), в Telegram не пишет; курсор `unsorted` — на каждом проходе |
| 2774 | `_unsorted_card_from_row` | Карточка «Неразобранного» (`UnsortedCard`) из строки `amocrm_unsorted_cards`, ссылка — `build_lead_link` |
| 2786 | `_unsorted_card_messages` | Сообщения карточки из колонки `messages` парами (чат, сообщение) |
| 2796 | `_unsorted_card_keyboard` | Кнопки карточки: «Перейти в сделку» (если есть сделка) и «Я перезвонил»/«Ответил» (`unsorted_done:<id>`) |
| 2809 | `_unsorted_card_deliver` | Единственная точка доставки карточки админам: отправка (удалить прежнюю, прислать заново) или закрытый вид; при переезде на шину меняется только она |
| 2877 | `_unsorted_card_send_due` | Отправка одного наступившего дела под блокировкой строки (`FOR UPDATE`) — гонка с нажатием кнопки |
| 2916 | `run_unsorted_cards_dispatch` | Проход рассылки карточек «Неразобранного» (раз в минуту, выключатель `AMOCRM_UNSORTED_CARDS`): первая отправка и напоминания раз в час |
| 2951 | `unsorted_card_done_cb` | Хендлер «Я перезвонил»/«Ответил» (`unsorted_done:<id>`): только `ADMIN_TG_IDS`, закрывает дело «только если открыто», у обоих админов закрытый вид; уже закрыто — «Уже отмечено». Зарегистрирован раньше хендлеров по состоянию сценария |
| 3012 | `_order_close_candidates` | Сторож проведения заказов: записи календаря (`ORDER_CLOSE_JOBS_SOURCE` = `adminbot.calendar_jobs`) за «сегодня − 14 … вчера» без проведённого заказа плюс все с открытым напоминанием (`order_close_reminders`); признаки `conducted` (заказ по `calendar_event_id` или по номеру записи, не удалённая уборка клининга — с дня накануне записи) и `gone` (запись пропала из календаря) |
| 3091 | `_order_close_now` | Текущее время для кнопки «Напомнить завтра» (тесты подменяют часы) |
| 3096 | `_order_close_chats` | Чаты напоминаний сторожа проведения: `ORDERS_CONFIRM_CHAT_ID` и `STAFF_CHAT_ID`, нулевые пропускаются |
| 3105 | `_order_close_fields` | Поля текста напоминания из записи календаря; записи нет — только дата |
| 3117 | `_order_close_delete` | Удалить сообщения напоминания, ошибки Telegram — в журнал и дальше |
| 3129 | `_order_close_edit` | Переписать сообщения напоминания текстом без кнопки (`parse_mode=None`), ошибки — в журнал и дальше |
| 3151 | `_order_close_refresh` | Строка напоминания по свежему кандидату: завести; переоткрыть `closed`/`gone`; открытую — закрыть «✅ Заказ проведён», удалить при пропаже записи (`gone`), при переносе на сегодня и позже — удалить сообщения и ждать новой даты |
| 3249 | `_order_close_send_due` | Отправка одного наступившего напоминания под `FOR UPDATE`: удалить прежние сообщения, в каждый чат — новое с кнопкой, `next_send_at` +30 минут в окне даже при сбое |
| 3311 | `run_order_close_reminders` | Проход сторожа проведения заказов (раз в 5 минут, выключатель `ORDER_CLOSE_REMINDER_ENABLED`): обновить кандидатов, в окне 10–20 МСК разослать наступившие |
| 3370 | `order_close_snooze_cb` | Хендлер «Напомнить завтра» (`order_close_snooze:<id>`): любой в чате; «только если открыто» — тишина до 10:00 завтра, в обоих чатах «Отложено до завтра — <кто>»; закрыто — короткий ответ и кнопку убрать. Зарегистрирован раньше хендлеров по состоянию; из групп его пропускает `IgnoreNonPrivateMiddleware` |
| 3466 | `autocall_called_cb` | Хендлер «Я уже перезвонил» робота автозвонка (`autocall_called:<lead_id>`, сообщение шлёт админ-бот ключом рабочего бота): статус цепочки из `AUTOCALL_LEADS_SOURCE` (= `adminbot.autocall_leads`); живая (`queued`/`calling`/`error`) — строка в `autocall_manager_callbacks` (`ON CONFLICT DO NOTHING`), к сообщению «✅ Принято…», кнопку убрать; цепочки нет или финал — без записи, «Робот по этой заявке уже не звонит», кнопку убрать. Зарегистрирован раньше хендлеров по состоянию |
| 3557 | `amocrm_api_polling_loop` | Основной фоновый цикл опроса amoCRM API |
| 3616 | `_compact_text_for_matching` | Нормализация текста для сравнения/поиска |
| 3620 | `_resolve_channel_kind` | Определение канала связи (whatsapp/telegram и т.п.) |
| 3634 | `_extract_wahelp_message_id` | Извлечение id сообщения из payload WAHelp |
| 3657 | `_log_lead_send` | Лог отправки сообщения по лиду |
| 3681 | `_update_lead_log_status` | Обновление статуса записи лога по лиду |
| 3720 | `_log_lead_response` | Лог ответа клиента по лиду |
| 3740 | `_lead_promo_awaiting_answer` | Было ли лиду промо из `LEADS_PROMO_CAMPAIGNS` без ответа interest/stop после самой поздней рассылки (ТЗ 2026-09-23 «автозвонок по промо», задача 1) |
| 3776 | `_record_promo_callback` | Строка заявки на звонок в `promo_callbacks` по засчитанному отклику; сбой — в журнал, не роняет автоответ (то же ТЗ, задача 3) |
| 3814 | `_send_lead_auto_reply` | Отправка автоответа по лиду |
| 3854 | `_get_leads_campaign_text` | Получение текста рассылки по лидам (кампания/вариант) |
| 3860 | `_random_leads_interval` | Случайный интервал между отправками рассылки лидам |
| 3864 | `_sleep_between_leads` | Пауза между отправками в рассылке лидам |
| 3870 | `_select_leads_for_campaign` | Выборка лидов для рассылочной кампании |
| 3889 | `_send_leads_campaign_batch` | Рассылка очередной пачки сообщений лидам |
| 4075 | `ensure_order_masters_schema` | Создание/проверка таблицы мастеров заказа |
| 4096 | `ensure_orders_wire_schema` | Создание/проверка полей привязки перевода к заказу |
| 4111 | `ensure_orders_address_schema` | Создание/проверка поля адреса заказа |
| 4120 | `ensure_pending_order_reports_schema` | Создание/проверка таблицы очереди отложенного отчёта и сообщения о деньгах |
| 4155 | `ensure_cashbook_wire_schema` | Создание/проверка полей перевода в кассовой книге |
| 4184 | `ensure_order_payments_schema` | Создание/проверка таблицы способов оплаты заказа |
| 4214 | `ensure_orders_rating_schema` | Создание/проверка поля оценки заказа |
| 4226 | `_add_months` | Прибавление месяцев к дате |
| 4236 | `_format_bonus_amount` | Форматирование суммы бонусов для текста |
| 4244 | `_ensure_min_bonus_for_promo` | Проверка минимального бонуса для промо-условия |
| 4277 | `_schedule_promo_notification` | Планирование промо-уведомления клиенту |
| 4300 | `_split_amount` | Разбиение суммы на равные части |
| 4318 | `_format_staff_name` | Форматирование имени сотрудника для текста |
| 4326 | `_schedule_birthday_congrats` | Планирование поздравления с днём рождения |
| 4362 | `_enqueue_wire_payment_received` | Постановка в очередь уведомления о получении перевода |
| 4376 | `_process_promo_stage` | Обработка очередного этапа промо-рассылки |
| 4427 | `_confirmation_notify_owner` | Уведомление владельца о подтверждении заказа |
| 4437 | `_confirmation_owner_phone` | Получение телефона владельца для звонка подтверждения |
| 4447 | `_confirmation_link` | Формирование ссылки подтверждения заказа |
| 4454 | `_confirmation_order_at_msk` | Время заказа в московской зоне для подтверждения |
| 4461 | `_confirmation_pending_for` | Поиск ожидающего подтверждения по цифрам телефона |
| 4495 | `_confirmation_accept` | Принятие подтверждения заказа |
| 4577 | `_confirmation_call_owner` | Звонок/уведомление владельцу по подтверждению |
| 4615 | `handle_wahelp_inbound` | Обработка входящего сообщения WAHelp: только метки клиента (`WAHELP_CLIENT_DESTINATIONS`), дальше подтверждение заказа, оценка, отписка, промо |
| 4891 | `_select_pending_rating_order` | Выбор работы клиента, ожидающей оценки: заказ химчистки или уборка с отметкой «попросили» (самая свежая просьба), возвращает `kind` и `id` (задача 2 ТЗ 2026-09-30-cleaning-ratings) |
| 4926 | `_process_rating_response` | Обработка ответа клиента с оценкой: пишет в таблицу своего вида (`orders`/`cleaning_orders`), в `payload` — `kind` |
| 4977 | `_notify_rating_admins` | Уведомление админов об оценке: «по заказу #id» или «по уборке №id» |
| 5008 | `_enqueue_bonus_change` | Постановка в очередь уведомления об изменении бонусов |
| 5042 | `_enqueue_order_completed_notification` | Постановка в очередь уведомления о завершении заказа |
| 5097 | `_load_notification_rules` | Загрузка правил уведомлений из конфига |
| 5107 | `ensure_staff_role_schema` | Создание/проверка таблицы ролей сотрудников |
| 5123 | `get_user_role` | Получение роли пользователя по id |
| 5130 | `has_permission` | Проверка права доступа пользователя |
| 5229 | `init_permissions` | Инициализация справочника прав доступа (вставка канона PERMISSIONS_CANON) |
| 5252 | `only_digits` | Извлечение только цифр из строки |
| 5255 | `normalize_phone_for_db` | Нормализация телефона для хранения в БД |
| 5303 | `_escape_html` | Экранирование HTML-спецсимволов |
| 5307 | `_bold_html` | Обёртка текста в HTML bold |
| 5311 | `_format_money_signed` | Форматирование суммы со знаком |
| 5317 | `mask_phone_last4` | Маскирование телефона (видны последние 4 цифры) |
| 5323 | `extract_street` | Извлечение улицы из адреса |
| 5343 | `is_bad_name` | Проверка «плохого»/подозрительного имени клиента |
| 5356 | `qround_ruble` | Округление суммы до рубля |
| 5361 | `parse_birthday_str` | Разбор строки даты рождения |
| 5389 | `_find_client_by_phone` | Поиск клиента по телефону |
| 5417 | `_fmt_client_row` | Форматирование строки клиента для вывода |
| 5432 | `payment_method_kb` | Клавиатура выбора способа оплаты |
| 5443 | `admin_payment_method_kb` | Клавиатура выбора способа оплаты (админ) |
| 5453 | `reports_root_kb` | Клавиатура корневого меню отчётов |
| 5464 | `reports_period_kb` | Клавиатура выбора периода отчёта |
| 5472 | `period_input_kb` | Клавиатура ввода произвольного периода |
| 5479 | `_parse_user_date` | Разбор даты, введённой пользователем |
| 5510 | `_parse_range_tokens` | Разбор диапазона дат из введённых токенов |
| 5525 | `_format_period_label` | Форматирование подписи периода отчёта |
| 5535 | `_dates_to_utc_bounds` | Преобразование дат периода в границы UTC |
| 5541 | `_resolve_period_dates` | Определение дат периода по ключевому слову (сегодня/месяц и т.п.) |
| 5560 | `_build_report_text` | Сборка текста отчёта по типу и периоду |
| 5576 | `_build_interval_report_text` | Сборка текста отчёта за произвольный интервал |
| 5606 | `build_report_masters_kb` | Клавиатура выбора мастера для отчёта |
| 5636 | `admin_root_kb` | Клавиатура главного меню админа: `[Операции, Отчёты]`, `[Клиенты, Клининг]` |
| 5644 | `admin_operations_kb` | Клавиатура подменю «Операции»: `[Приход, Расход, Изъятие]`, `[Привязать, Рассчитать ЗП]`, `[Назад]` |
| 5654 | `admin_cleaning_kb` | Клавиатура подменю «Клининг»: `[Приход, Расход, Баланс]`, `[Перемещение, Выплата прибыли]`, `[Назад]` |
| 5664 | `admin_menu_idle` | Фильтр кнопок меню админа: пропускает в состоянии `AdminMenuFSM` или без состояния у админа (право `view_orders_reports`) |
| 5679 | `admin_menu_return` | Админ закончил клининговую операцию: состояние `AdminMenuFSM.root` и главное меню; роутер клининга получает её через `dp["admin_menu_return"]` |
| 5692 | `build_salary_master_kb` | Клавиатура выбора мастера для расчёта зарплаты |
| 5724 | `build_salary_summary_text` | Сборка текста сводки по зарплате мастера |
| 5819 | `admin_clients_kb` | Клавиатура раздела «Клиенты» в админке |
| 5827 | `admin_cancel_kb` | Клавиатура отмены в админке |
| 5834 | `client_edit_fields_kb` | Клавиатура выбора поля клиента для редактирования |
| 5844 | `client_view_kb` | Клавиатура просмотра карточки клиента |
| 5852 | `client_find_phone_kb` | Клавиатура поиска клиента по телефону |
| 5857 | `tx_last_kb` | Клавиатура последних транзакций |
| 5865 | `_send_tx_last` | Отправка списка последних транзакций |
| 5914 | `get_master_cash_on_orders` | Сумма наличных мастера по заказам |
| 5932 | `_ensure_bonus_posted_column` | Проверка/добавление колонки признака начисления бонуса |
| 5952 | `bonus_baseline_init` | Инициализация базового значения бонусов |
| 5988 | `post_order_bonus_delta` | Начисление изменения бонусов по заказу |
| 6031 | `format_money` | Форматирование денежной суммы |
| 6047 | `_format_payment_label` | Подпись способа оплаты |
| 6053 | `_format_payment_parts` | Форматирование частей разбитого платежа |
| 6072 | `_format_payment_summary` | Сводка по способам оплаты для текста |
| 6100 | `_format_dividend_comment` | Форматирование комментария к дивиденду |
| 6107 | `_withdrawal_filter_sql` | SQL-условие фильтра выводов средств |
| 6116 | `_non_profit_expense_filter` | SQL-условие фильтра непрофильных расходов |
| 6125 | `_cashbook_active_filter` | SQL-условие активных записей кассовой книги |
| 6134 | `_cashbook_daily_aggregates_sql` | SQL для суточных агрегатов кассовой книги |
| 6148 | `get_cash_balance_excluding_withdrawals` | Остаток кассы без учёта выводов |
| 6168 | `build_masters_kb` | Клавиатура списка мастеров |
| 6205 | `_amo_normalize_phone` | Нормализация телефона из amoCRM (обмен с amoCRM, `incoming_from_amo`) |
| 6223 | `_last_birthday_date` | Последняя дата дня рождения относительно сегодня |
| 6241 | `_exchange_find_existing` | Поиск существующей записи «Обмен» в БД |
| 6270 | `_exchange_apply` | Применение решения по сделке «Обмен» в БД |
| 6313 | `_accrue_birthday_bonuses` | Начисление бонусов ко дню рождения |
| 6457 | `_expire_old_bonuses` | Сгорание просроченных бонусов |
| 6519 | `_should_run_daily_job` | Проверка, нужно ли запускать суточный джоб |
| 6528 | `_mark_daily_job_run` | Отметка о выполнении суточного джоба |
| 6556 | `build_exchange_daily_summary_text` | Сводка за сутки по модулю «Обмен» |
| 6615 | `build_client_messaging_daily_summary_text` | Сводка за сутки по переписке с клиентами |
| 6661 | `send_daily_reports` | Отправка суточных отчётов админам |
| 6700 | `retry_pending_sent_messages` | Повторная отправка зависших сообщений |
| 6904 | `wire_pending_reminder_job` | Напоминание о непривязанных переводах |
| 6964 | `run_birthday_jobs` | Джоб обработки дней рождения (бонусы + поздравления) |
| 7147 | `run_promo_reminders` | Джоб промо-напоминаний |
| 7161 | `check_rewash_master_counter` | Проверка счётчика перемывов мастера, уведомление при 5 в месяц |
| 7227 | `_backfill_address_for_table` | Один проход дозаполнения адреса для пары заказы+связки (химчистка или уборки) |
| 7269 | `run_address_backfill` | Фоновый цикл: дозаполняет адрес заказа и клиента из связки админ-бота с CRM |
| 7294 | `_order_report_payload_from_db` | Разбор payload очереди отчётов из БД (jsonb/строка) в словарь |
| 7307 | `_dispatch_order_report` | Отправка отчёта в чат и сообщения о деньгах по одному заказу из очереди |
| 7333 | `_dispatch_cleaning_order_report` | Отправка сообщения о проведённой уборке в кассу клининга (kind='cleaning' очереди) с адресом, прочитанным заново перед отправкой; строка «Деньги Оли», если в payload есть `olya_balance` |
| 7362 | `_alert_deal_created_without_address` | Сигнал в LOGS_CHAT_ID: сделка создана с нуля и без адреса (возможно, неверный телефон) |
| 7388 | `_alert_admin_bot_silent` | Сигнал в LOGS_CHAT_ID: связка с CRM не появилась за 30 минут (авария) |
| 7407 | `run_pending_order_reports` | Фоновый цикл: досылает отчёт и сообщение о деньгах по связке с CRM, шлёт сигналы задачи 6 |
| 7476 | `schedule_daily_job` | Планировщик ежедневного запуска джоба в заданное время (МСК) |
| 7504 | `schedule_periodic_job` | Планировщик периодического запуска джоба |
| 7514 | `_format_health_dt` | Форматирование времени для отчёта о здоровье сервиса |
| 7522 | `_send_client_bot_health_alert` | Отправка алерта о состоянии клиентского бота |
| 7533 | `check_client_bot_health` | Проверка heartbeat клиентского бота |
| 7644 | `write_own_heartbeat` | Пульс рабочего бота в service_heartbeats (оповещения, задача 6) |
| 7677 | `clear_dead_channels_weekly` | Еженедельная очистка неактивных каналов связи |
| 7704 | `withdraw_nav_kb` | Клавиатура навигации вывода средств |
| 7712 | `withdraw_confirm_kb` | Клавиатура подтверждения вывода средств |
| 7720 | `confirm_inline_kb` | Универсальная инлайн-клавиатура подтверждения |
| 7728 | `expense_category_suggestion_kb` | Клавиатура подсказки категории расхода |
| 7737 | `expense_category_pick_kb` | Клавиатура выбора категории расхода |
| 7746 | `_is_withdraw_entry` | Проверка, является ли запись выводом средств |
| 7759 | `_tx_type_label` | Подпись типа транзакции |
| 7768 | `reports_root` | Хендлер открытия корневого меню отчётов |
| 7778 | `reports_shortcut_cash` | Хендлер быстрого отчёта по кассе |
| 7792 | `reports_shortcut_profit` | Хендлер быстрого отчёта по прибыли |
| 7806 | `reports_shortcut_payment_types` | Хендлер быстрого отчёта по способам оплаты |
| 7818 | `reports_run_period_day` | Хендлер отчёта за день |
| 7825 | `reports_run_period_month` | Хендлер отчёта за месяц |
| 7832 | `reports_run_period_year` | Хендлер отчёта за год |
| 7839 | `reports_period_cancel` | Хендлер отмены выбора периода отчёта |
| 7846 | `reports_period_back` | Хендлер возврата на шаг назад в выборе периода |
| 7865 | `reports_period_start_input` | Хендлер ввода начала произвольного периода |
| 7875 | `reports_period_end_cancel` | Хендлер отмены на шаге ввода конца периода |
| 7882 | `reports_period_end_back` | Хендлер возврата на шаге ввода конца периода |
| 7888 | `reports_period_end_input` | Хендлер ввода конца произвольного периода |
| 7919 | `_record_income` | Запись дохода в кассовую книгу |
| 7961 | `_record_expense` | Запись расхода в кассовую книгу |
| 7990 | `_notify_order_income` | Уведомление о доходе по заказу |
| 8009 | `_record_order_income` | Запись дохода по заказу в кассу |
| 8050 | `_build_order_report_lines` | Сборка текста отчёта о заказе (адрес подставляется в момент отправки) |
| 8098 | `_enqueue_order_report` | Постановка заказа в очередь pending_order_reports (отчёт и сообщение о деньгах) |
| 8151 | `_record_withdrawal` | Запись вывода средств в кассу |
| 8180 | `_get_jenya_card_balance` | Остаток на карте «Женя» |
| 8195 | `_record_jenya_card_entry` | Запись операции по карте «Женя» |
| 8260 | `_remove_jenya_card_entry` | Удаление операции по карте «Женя» |
| 8297 | `norm_pay_method_py` | Приведение способа оплаты к канону (PAYMENT_METHODS/подарочный сертификат) |
| 8322 | `set_commands` | Регистрация списка команд бота в Telegram |
| 8383 | `list_masters` | Хендлер списка мастеров |
| 8410 | `masters_all` | Хендлер полного списка мастеров |
| 8438 | `add_master` | Хендлер начала добавления мастера |
| 8458 | `add_master_tg` | Хендлер ввода Telegram-данных нового мастера |
| 8476 | `add_master_phone` | Хендлер ввода телефона нового мастера |
| 8491 | `add_master_name` | Хендлер ввода имени нового мастера |
| 8531 | `add_master_cancel` | Хендлер отмены добавления мастера |
| 8538 | `remove_master` | Хендлер запроса на удаление мастера |
| 8554 | `admin_menu_start` | Хендлер входа в админ-меню |
| 8563 | `admin_withdraw_entry` | Хендлер входа в раздел вывода средств |
| 8589 | `admin_clients_root` | Хендлер входа в раздел «Клиенты» |
| 8598 | `client_find_start` | Хендлер начала поиска клиента |
| 8604 | `client_edit_start` | Хендлер начала редактирования клиента |
| 8610 | `admin_clients_back` | Хендлер «назад» в разделе «Клиенты» |
| 8616 | `admin_clients_cancel` | Хендлер отмены в разделе «Клиенты» |
| 8621 | `get_master_wallet` | Остатки кошелька мастера (нал/безнал) |
| 8643 | `parse_amount_ru` | Разбор денежной суммы, введённой в русском формате |
| 8679 | `withdraw_amount_cancel` | Хендлер отмены на шаге ввода суммы вывода |
| 8687 | `withdraw_amount_back` | Хендлер «назад» на шаге ввода суммы вывода |
| 8707 | `withdraw_amount_got` | Хендлер получения суммы вывода |
| 8777 | `withdraw_master_cancel` | Хендлер отмены на шаге выбора мастера для вывода |
| 8785 | `withdraw_master_pick` | Хендлер выбора мастера для вывода средств |
| 8856 | `withdraw_confirm_handler` | Хендлер подтверждения вывода средств |
| 8951 | `admin_clients_states_back` | Хендлер «назад» в состояниях раздела «Клиенты» |
| 9002 | `admin_clients_states_cancel` | Хендлер отмены в состояниях раздела «Клиенты» |
| 9009 | `client_find_got_phone` | Хендлер получения телефона при поиске клиента |
| 9020 | `client_view_edit` | Хендлер перехода к редактированию из просмотра клиента |
| 9035 | `client_edit_got_phone` | Хендлер получения телефона клиента для редактирования |
| 9048 | `client_edit_pick_field` | Хендлер выбора поля клиента для редактирования |
| 9062 | `client_edit_apply` | Хендлер применения изменений карточки клиента |
| 9139 | `admin_panel_alias` | Хендлер алиаса входа в админ-панель |
| 9144 | `help_cmd` | Хендлер команды /help |
| 9192 | `order_open_master_flow` | Хендлер открытия сценария заказа для мастера |
| 9201 | `whoami` | Хендлер команды «кто я» (роль/права) |
| 9238 | `client_info` | Хендлер вывода информации о клиенте |
| 9252 | `client_set_name` | Хендлер установки имени клиента |
| 9269 | `client_set_birthday` | Хендлер установки даты рождения клиента |
| 9308 | `client_set_bonus` | Хендлер установки бонусов клиента |
| 9328 | `client_add_bonus` | Хендлер добавления бонусов клиенту |
| 9351 | `client_set_phone` | Хендлер установки телефона клиента |
| 9382 | `payroll_report` | Хендлер отчёта по зарплате |
| 9416 | `build_master_orders_report_text` | Сборка текста отчёта по заказам мастера |
| 9520 | `build_cash_report_text_for_period` | Сборка текста отчёта по кассе за период |
| 9622 | `build_profit_report_text_for_period` | Сборка текста отчёта по прибыли за период |
| 9650 | `build_payment_methods_report_text` | Сборка текста отчёта по способам оплаты |
| 9682 | `_today_period_bounds` | Границы периода «сегодня» |
| 9692 | `build_daily_cash_summary_text` | Сводка по кассе за сегодня |
| 9697 | `build_profit_summary_text` | Сводка по прибыли за сегодня |
| 9702 | `build_daily_orders_admin_summary_text` | Сводка по заказам за сегодня для админов |
| 9762 | `cash_report` | Хендлер отчёта по кассе |
| 9781 | `cash_balance_report` | Хендлер отчёта остатка кассы |
| 9789 | `profit_report` | Хендлер отчёта по прибыли |
| 9808 | `payments_report` | Хендлер отчёта по способам оплаты |
| 9827 | `daily_cash_report` | Хендлер суточного отчёта по кассе |
| 9835 | `daily_profit_report` | Хендлер суточного отчёта по прибыли |
| 9843 | `daily_orders_report` | Хендлер суточного отчёта по заказам |
| 9851 | `orders_report` | Хендлер отчёта по заказам |
| 10014 | `reports_start` | Хендлер входа в раздел отчётов |
| 10021 | `rep_master_orders_entry` | Хендлер входа в отчёт «заказы мастера» |
| 10036 | `rep_master_salary_entry` | Хендлер входа в отчёт «зарплата мастера» |
| 10051 | `reports_pick_period` | Хендлер выбора периода отчёта |
| 10062 | `reports_payment_methods` | Хендлер отчёта по способам оплаты из меню отчётов |
| 10071 | `rep_period_back` | Хендлер «назад» на шаге выбора периода отчёта |
| 10090 | `reports_exit_to_admin` | Хендлер выхода из отчётов в админ-меню |
| 10097 | `reports_pick_master_cancel_or_back` | Хендлер отмены/назад при выборе мастера для отчёта |
| 10104 | `rep_master_back` | Хендлер «назад» при выборе мастера отчёта |
| 10110 | `reports_root_back` | Хендлер «назад» в корне отчётов |
| 10118 | `reports_cancel` | Хендлер отмены раздела отчётов |
| 10125 | `admin_salary_start` | Хендлер начала расчёта зарплаты мастера (админ) |
| 10134 | `admin_salary_pick_master` | Хендлер выбора мастера для расчёта зарплаты |
| 10172 | `admin_salary_pick_start` | Хендлер выбора начала периода зарплаты |
| 10194 | `admin_salary_pick_end` | Хендлер выбора конца периода зарплаты |
| 10236 | `adm_root_reports` | Хендлер быстрого перехода к отчётам из корня админки |
| 10244 | `adm_root_cash` | Хендлер быстрого перехода к кассе из корня админки |
| 10249 | `adm_root_profit` | Хендлер быстрого перехода к прибыли из корня админки |
| 10254 | `adm_root_tx_last` | Хендлер быстрого перехода к последним транзакциям |
| 10262 | `admin_root_back` | Хендлер «Назад» в главном меню и в подменю «Операции»/«Клининг» — главное меню админа |
| 10270 | `admin_root_exit` | Хендлер выхода из админки |
| 10278 | `adm_root_whoami` | Хендлер команды «кто я» из корня админки |
| 10283 | `income_wizard_start` | Хендлер начала мастера ввода дохода (кнопка «Приход» подменю «Операции») |
| 10293 | `income_cancel_any` | Хендлер отмены на любом шаге ввода дохода |
| 10300 | `income_back_to_method` | Хендлер возврата к выбору способа оплаты дохода |
| 10306 | `income_back_to_amount` | Хендлер возврата к вводу суммы дохода |
| 10319 | `income_wizard_pick_method` | Хендлер выбора способа оплаты дохода |
| 10337 | `income_wizard_amount` | Хендлер ввода суммы дохода |
| 10358 | `_fetch_pending_wire_entries` | Выборка записей кассы, ожидающих привязки перевода |
| 10381 | `_format_pending_wire_entry_line` | Форматирование строки записи для привязки перевода |
| 10388 | `_format_pending_wire_entry_label` | Короткая подпись записи для привязки перевода |
| 10394 | `_format_pending_order_label` | Подпись заказа для привязки перевода |
| 10400 | `_link_mode_kb` | Клавиатура выбора режима привязки перевода |
| 10409 | `_link_confirm_kb` | Клавиатура подтверждения привязки перевода |
| 10417 | `_link_list_kb` | Клавиатура списка для выбора при привязке перевода |
| 10445 | `_link_store_message` | Сохранение сообщения в состоянии сценария привязки |
| 10449 | `_link_edit` | Редактирование сообщения сценария привязки перевода |
| 10465 | `_link_cancel_flow` | Отмена сценария привязки перевода |
| 10476 | `_link_show_mode` | Показ экрана выбора режима привязки |
| 10481 | `_link_show_payments` | Показ списка платежей для привязки |
| 10490 | `_link_show_orders` | Показ списка заказов для привязки |
| 10499 | `_link_prompt_master` | Запрос выбора мастера в сценарии привязки |
| 10516 | `_link_build_summary` | Сборка сводки перед подтверждением привязки |
| 10537 | `_link_show_summary` | Показ сводки перед подтверждением привязки |
| 10544 | `_begin_wire_entry_selection` | Запуск выбора записи перевода для привязки |
| 10567 | `link_payment_cmd` | Хендлер команды привязки платежа |
| 10579 | `link_payment_menu` | Хендлер меню привязки платежа |
| 10591 | `link_pick_mode` | Хендлер выбора режима привязки (callback) |
| 10605 | `link_back_mode` | Хендлер «назад» к выбору режима привязки |
| 10612 | `link_back_payments` | Хендлер «назад» к списку платежей |
| 10619 | `link_back_orders` | Хендлер «назад» к списку заказов |
| 10626 | `link_refresh_payments` | Хендлер обновления списка платежей |
| 10637 | `link_refresh_orders` | Хендлер обновления списка заказов |
| 10648 | `link_cancel` | Хендлер отмены привязки перевода (callback) |
| 10655 | `link_pick_payment` | Хендлер выбора платежа для привязки |
| 10692 | `link_pick_order` | Хендлер выбора заказа для привязки |
| 10737 | `_link_prepare_masters` | Подготовка списка мастеров для привязки |
| 10754 | `link_master_amount_input` | Хендлер ввода суммы по мастеру при привязке |
| 10785 | `link_confirm` | Хендлер подтверждения привязки перевода |
| 10824 | `income_wizard_comment` | Хендлер ввода комментария к доходу |
| 10835 | `_send_income_confirm` | Отправка экрана подтверждения дохода |
| 10851 | `wire_link_pick_entry` | Хендлер выбора записи перевода для привязки к заказу |
| 10896 | `expense_wizard_start` | Хендлер начала мастера ввода расхода (кнопка «Расход» подменю «Операции») |
| 10909 | `expense_wizard_amount` | Хендлер ввода суммы расхода |
| 10935 | `investment_start` | Хендлер начала ввода инвестиции |
| 10948 | `investment_amount_input` | Хендлер ввода суммы инвестиции |
| 10964 | `investment_owner_input` | Хендлер ввода владельца инвестиции |
| 10984 | `investment_comment_input` | Хендлер ввода комментария к инвестиции |
| 11007 | `expense_wizard_comment` | Хендлер ввода комментария к расходу |
| 11025 | `expense_category_handler` | Хендлер выбора категории расхода |
| 11065 | `expense_wizard_owner` | Хендлер выбора владельца/ответственного за расход |
| 11099 | `rep_master_pick` | Хендлер выбора мастера в отчёте |
| 11136 | `tx_remove_start` | Хендлер начала удаления транзакции |
| 11151 | `tx_remove_pick_date` | Хендлер выбора даты для удаления транзакции |
| 11214 | `tx_remove_choose` | Хендлер выбора транзакции для удаления |
| 11270 | `tx_remove_confirm` | Хендлер подтверждения удаления транзакции |
| 11363 | `order_remove_start` | Хендлер начала удаления заказа |
| 11378 | `order_remove_pick_date` | Хендлер выбора даты для удаления заказа |
| 11453 | `order_remove_choose` | Хендлер выбора заказа для удаления |
| 11546 | `_record_deleted_order` | Пишет строку в `deleted_orders` внутри уже открытой транзакции (INSERT ... ON CONFLICT DO NOTHING) |
| 11578 | `order_remove_confirm` | Хендлер подтверждения удаления заказа |
| 11795 | `bonus_backfill` | Хендлер разового пересчёта/бэкфилла бонусов |
| 11896 | `income_confirm_handler` | Хендлер подтверждения записи дохода (callback) |
| 11965 | `expense_confirm_handler` | Хендлер подтверждения записи расхода (callback) |
| 12027 | `investment_confirm_handler` | Хендлер подтверждения записи инвестиции (callback) |
| 12092 | `income_wire_choice` | Хендлер выбора «привязать к переводу» при вводе дохода |
| 12134 | `wire_nudge_link_cb` | Хендлер напоминания привязать перевод (callback) |
| 12147 | `wire_nudge_later_cb` | Хендлер «напомнить позже» о привязке перевода |
| 12156 | `add_income` | Хендлер команды добавления дохода |
| 12206 | `add_expense` | Хендлер команды добавления расхода |
| 12270 | `tx_last_cmd` | Хендлер команды «последние транзакции» |
| 12283 | `tx_last_presets` | Хендлер быстрых пресетов последних транзакций |
| 12292 | `tx_delete` | Хендлер команды удаления транзакции |
| 12318 | `withdraw_start` | Хендлер команды начала вывода средств |
| 12323 | `dividend_start` | Хендлер начала выплаты дивидендов |
| 12337 | `dividend_amount_input` | Хендлер ввода суммы дивидендов |
| 12360 | `dividend_owner_input` | Хендлер ввода получателя дивидендов |
| 12380 | `dividend_comment_input` | Хендлер ввода комментария к дивидендам |
| 12403 | `dividend_confirm_handler` | Хендлер подтверждения выплаты дивидендов |
| 12478 | `jenya_card_status` | Хендлер запроса статуса карты «Женя» |
| 12509 | `my_salary` | Хендлер запроса своей зарплаты (мастер) |
| 12559 | `my_income` | Хендлер запроса своего дохода (мастер) |
| 12597 | `master_main_kb` | Главная клавиатура мастера |
| 12638 | `cancel_any` | Универсальный хендлер отмены текущего сценария |
| 12688 | `admin_operations_root` | Кнопка «Операции» (`admin_menu_idle`): подменю «Операции», состояние `AdminMenuFSM.operations` |
| 12696 | `admin_cleaning_root` | Кнопка «Клининг» (`admin_menu_idle`): подменю «Клининг», состояние `AdminMenuFSM.cleaning` |
| 12706 | `admin_cleaning_income` | «Приход» в подменю «Клининг» → `/cleaning_cash_add` (`start_cash_add`) |
| 12711 | `admin_cleaning_expense` | «Расход» в подменю «Клининг» → `/cleaning_expense` (`foreman_expense_start`) |
| 12716 | `admin_cleaning_balance` | Кнопка «Баланс» (`admin_menu_idle`): баланс клининга (`cleaning_balance_cmd`) с главным меню админа, состояние `root` |
| 12723 | `admin_cleaning_move` | Кнопка «Перемещение» (`admin_menu_idle`) → `/cleaning_move` (`start_cash_move`) |
| 12728 | `admin_cleaning_dividend` | Кнопка «Выплата прибыли» (`admin_menu_idle`) → `/cleaning_dividend` (`start_cleaning_dividend`) |
| 12733 | `admin_pick_section` | «Приход»/«Расход» вне подменю (без состояния или в главном меню): «Выберите раздел:», главное меню, `root` |
| 12744 | `admin_root_fallback` | Хендлер прочего текста в меню админа (главное меню и подменю): «Выберите действие…» с клавиатурой текущего меню |
| 12755 | `is_admin` | Проверка, является ли пользователь админом |
| 12758 | `ensure_master` | Проверка права пользователя создавать заказы/клиентов (доступ мастера) |
| 12763 | `keyboard_for_user` | Подбор клавиатуры под роль пользователя |
| 12776 | `start_handler` | Хендлер команды /start |
| 12806 | `find_cmd` | Хендлер команды поиска (клиента/заказа) |
| 12833 | `OrderFSM` | Состояния FSM оформления заказа |
| 12858 | `MasterFSM` | Состояния FSM сценариев мастера |
| 12865 | `start_order` | Хендлер начала оформления заказа |
| 12875 | `_order_continue_after_phone` | Продолжение сценария после телефона (имя/правка имени/сумма) — общее для обычного пути и для пути после выбора записи календаря (задача 9 ТЗ «цепочка заказа», 2026-09-22) |
| 12905 | `_order_pick_calendar_job` | Запрос к `adminbot.calendar_jobs` по телефону и веткам 0/1/2+ записей; выключатель `ORDER_CALENDAR_PICK`, сбой запроса не роняет сценарий (задача 9 того же ТЗ) |
| 12976 | `got_calendar_job_pick` | Хендлер выбора записи календаря (кнопки «1»…«N»/«Не из списка») в состоянии `OrderFSM.pick_job` |
| 12994 | `got_phone` | Хендлер получения телефона клиента при заказе |
| 13024 | `fix_name` | Хендлер исправления имени клиента |
| 13035 | `parse_money` | Разбор денежной суммы из текста |
| 13045 | `got_name` | Хендлер получения имени клиента |
| 13051 | `got_amount` | Хендлер получения суммы заказа |
| 13068 | `got_upsell_flag` | Хендлер флага доп. продажи (апсейл) |
| 13077 | `got_upsell_amount` | Хендлер суммы доп. продажи |
| 13087 | `ask_bonus` | Хендлер запроса списания бонусов |
| 13121 | `got_bonus_spend` | Хендлер суммы списания бонусов |
| 13153 | `bonus_custom_amount` | Хендлер произвольной суммы списания бонусов |
| 13180 | `order_pick_method` | Хендлер выбора способа оплаты заказа |
| 13217 | `_prompt_payment_split` | Запрос разбивки оплаты по способам |
| 13243 | `_payment_parts_from_state` | Сборка частей оплаты из состояния FSM |
| 13257 | `_fetch_order_payment_parts` | Выборка частей оплаты по заказам из БД |
| 13278 | `order_payment_split_prompt` | Хендлер экрана разбивки оплаты заказа |
| 13315 | `order_payment_split_amount` | Хендлер ввода суммы части разбивки оплаты |
| 13345 | `order_payment_split_method` | Хендлер выбора способа для части разбивки оплаты |
| 13392 | `ensure_primary_master_info` | Определение основного мастера заказа |
| 13411 | `ask_extra_master` | Хендлер вопроса о доп. мастере |
| 13437 | `_prompt_pick_extra_master` | Запрос выбора доп. мастера |
| 13468 | `handle_add_more_masters` | Хендлер добавления ещё одного мастера к заказу |
| 13481 | `pick_extra_master` | Хендлер выбора доп. мастера |
| 13516 | `_format_pending_wire_comment` | Форматирование комментария ожидающей записи перевода |
| 13524 | `_mark_wire_entry_pending` | Пометка записи как ожидающей привязки перевода |
| 13539 | `_ensure_pending_wire_on_abort` | Сохранение записи в ожидании при прерывании сценария |
| 13547 | `_exit_wire_link_pending` | Выход из сценария привязки перевода с сохранением ожидания |
| 13559 | `_fetch_orders_waiting_wire` | Выборка заказов, ожидающих перевода |
| 13582 | `_format_wire_order_line` | Форматирование строки заказа, ожидающего перевода |
| 13597 | `_prompt_wire_order_selection` | Запрос выбора заказа для привязки перевода |
| 13618 | `_load_order_masters` | Загрузка списка мастеров заказа |
| 13647 | `wire_link_pick_order` | Хендлер выбора заказа для привязки перевода |
| 13713 | `wire_link_master_amount` | Хендлер ввода суммы перевода по мастеру |
| 13756 | `_prompt_next_wire_master` | Запрос суммы по следующему мастеру перевода |
| 13775 | `_finalize_wire_link_flow` | Завершение сценария привязки перевода к заказу |
| 13806 | `_apply_wire_link` | Применение привязки перевода к заказу в БД |
| 13878 | `_unlink_wire_entry` | Откат привязки перевода при удалении её транзакции: заказ снова ждёт оплату по счёту, сумма и зарплата — как до привязки |
| 13943 | `_ensure_address_before_confirm` | Проверка наличия адреса перед подтверждением заказа |
| 13948 | `proceed_order_finalize` | Переход к финализации заказа |
| 13964 | `got_bday` | Хендлер получения даты рождения клиента при заказе |
| 13974 | `show_confirm` | Хендлер показа экрана подтверждения заказа; строка «📍 из календаря» при выбранной записи (задача 9 того же ТЗ) |
| 14058 | `cancel_order` | Хендлер отмены оформления заказа |
| 14064 | `commit_order` | Фиксация заказа в БД — основная логика оформления; INSERT кладёт `calendar_event_id`/`deal_lead_id` (задача 9 того же ТЗ) |
| 14375 | `master_find_start` | Хендлер начала поиска мастера |
| 14386 | `master_find_phone` | Хендлер поиска мастера по телефону |
| 14415 | `master_salary_prompt` | Хендлер запроса периода для расчёта зарплаты мастера |
| 14425 | `master_salary_calc` | Хендлер расчёта зарплаты мастера |
| 14471 | `master_income` | Хендлер запроса дохода мастера |
| 14499 | `master_rewash_start` | Хендлер начала оформления перемыва |
| 14509 | `master_rewash_date` | Хендлер выбора даты исходного заказа для перемыва |
| 14602 | `master_rewash_order` | Хендлер выбора заказа для перемыва |
| 14705 | `cleaner_button_bridge` | Мост: пять кнопок клавиатуры клинера идут до cleaning-хендлеров в обход заглушки `unknown` |
| 14724 | `unknown` | Хендлер нераспознанных сообщений |
| 14748 | `main` | Точка входа: инициализация пула БД, прав, фоновых задач и запуск polling |
