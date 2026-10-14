# Карта функций `notifications/outbox.py`

Дата: 2026-10-14
Файл: `notifications/outbox.py`, строк: 803
Объявлений верхнего уровня (`grep -cE "^(async def|def|class) " notifications/outbox.py`): 22.

Очередь писем клиентам `notification_outbox`: схема, постановка, выборка работником
(`notifications/worker.py`), отметки «отправлено / сбой / отменено / отложено» и приём статусов
доставки от Wahelp (вебхук `notifications/webhook.py`). Решение «слать ли» здесь не принимается —
это работник и проверка перед отправкой в `bot.py`.

| Строка | Функция | Назначение |
|---|---|---|
| 31 | `NotificationOutboxEntry` | Письмо из очереди вместе с данными клиента (телефон, каналы Wahelp, флаги) — то, что получает работник |
| 55 | `PreSendVerdict` | Ответ проверки перед отправкой: `send` / `cancel` / `retry` и причина |
| 75 | `_now_utc` | Текущее время в UTC (точка подмены в тестах) |
| 79 | `_serialize_value` | Значение для JSON: `Decimal` — строкой, даты — ISO (наивное время считается UTC) |
| 94 | `serialize_payload` | Переменные письма в JSON-совместимый словарь |
| 101 | `render_template` | Подставить `{{переменные}}` в шаблон; пустое — пустой строкой |
| 112 | `ensure_notification_schema` | Схема при запуске: поля каналов Wahelp у `clients`, таблицы `notification_outbox`, `notification_messages`, `wahelp_delivery_issues`, индексы |
| 223 | `enqueue_notification` | Поставить письмо события в очередь: время — `scheduled_at` или задержка из правил; возвращает номер |
| 253 | `pick_ready_batch` | Взять пачку наступивших `pending` (`FOR UPDATE SKIP LOCKED`), перевести в `sending`, +1 попытка |
| 353 | `mark_outbox_sent` | Отметить письмо отправленным и записать сообщение в `notification_messages` |
| 397 | `mark_outbox_failure` | Сбой отправки: повтор через N минут или `failed` после последней попытки |
| 426 | `cancel_outbox_entry` | Отменить письмо в руках работника (любой статус) |
| 430 | `cancel_pending_outbox` | Отменить письмо, только если оно ещё `pending`; отвечает, удалось ли |
| 444 | `_cancel_outbox_row` | Общая отмена: статус `cancelled`, причина в `last_error` |
| 462 | `defer_outbox_entry` | Отложить письмо до момента (ночная тишина), не тратя попытку; ушедшие и отменённые не трогает |
| 505 | `_extract_id_from_mapping` | Найти номер сообщения провайдера в словаре по известным ключам |
| 525 | `extract_provider_message_id` | Номер сообщения Wahelp из ответа или вебхука (в том числе во вложенных данных) |
| 550 | `_normalize_status` | Статус доставки Wahelp → `sent` / `delivered` / `read` / `failed` (в том числе по имени события) |
| 574 | `_parse_timestamp` | Время из вебхука (datetime, число секунд или строка) в UTC |
| 600 | `_status_priority` | Старшинство статуса: более ранний статус не перезаписывает более поздний |
| 606 | `_map_channel_to_key` | Канал Wahelp (`clients_wa/tg/max`) → ключ `wa/tg/max` |
| 616 | `apply_provider_status_update` | Вебхук статуса: обновить `notification_messages` или `lead_logs` (время доставки/прочтения/сбоя), канал клиента |
