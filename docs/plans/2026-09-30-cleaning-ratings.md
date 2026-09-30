# ТЗ: оценка по уборкам как у химчистки (п.9, 30.09)

> Исполнителю: задачи по одной, TDD (сначала падающий тест, потом код), коммит после
> каждой задачи и отметка «Выполнено: <дата>, <SHA>» под ней (отметка — следующим коммитом).

**Цель:** ответ клиента на «оцените уборку» сохраняется у уборки, обрабатывается как у
химчистки, и цикл «Повторный заказ» админ-бота ведёт по нему задачи в сделке уборки.

**Устройство:** рабочий бот получает у уборок те же четыре поля оценки, что у заказов, и
относит ответ-цифру к работе, по которой оценку просили последней (уборка или химчистка).
Админ-бот читает оценки уборок через свою связку `adminbot.cleaning_links`, а состояние
цикла различает уборку и заказ с одним номером (миграция 020).

**Стек:** Python 3.11 (локально), asyncpg, aiogram; Postgres; pytest.

## Решения владельца 2026-09-30 (источник — разговор в сессии)

1. Всё 1 в 1 как у химчистки, кроме подписи работы: «уборка №N» вместо «заказ №N».
2. Только новые уборки: у уборок, проведённых до выката, оценку не ловим (7 штук с 28.08).
3. Уборки в цикле «Повторный заказ» — под тем же выключателем, что химчистка
   (`FEEDBACK_TASKS_ENABLED` / `FEEDBACK_TASKS_DRY_RUN`), своего нет.
4. Изменение таблицы `cleaning_orders` в бою (4 новых поля) — разрешено.

## Факты (источник — код на HEAD `85b0829`)

- Химчистка: поля `orders.rating_score smallint, rating_comment text, rating_requested_at
  timestamptz, rating_replied_at timestamptz` — `bot.py:3206` `ensure_orders_rating_schema`.
- Просьба оценить химчистку и отметка «попросили» — `bot.py:3988` `_enqueue_order_completed_notification`
  (enqueue `order_rating_reminder` и `UPDATE orders SET rating_requested_at = NOW(), …`).
- Просьба оценить уборку — `cleaning/handlers.py:217` `_enqueue_cleaning_completed_notifications`
  (enqueue `cleaning_order_rating_reminder`), отметки «попросили» нет; вызов — `handlers.py:746`.
- Схема уборок: файл миграции `app/migrations/0006_cleaning.sql` и `0010_…` плюс зеркало при
  запуске `cleaning/schema.py` `ensure_cleaning_schema` (докстринг перечисляет зеркалируемые файлы).
  Последний файл миграций рабочего бота — `0015_promo_callbacks.sql`.
- Ответ-цифра: `bot.py:3629` разбор, `bot.py:3761–3772` маршрут, `_select_pending_rating_order`
  (`bot.py:3862`, только `orders`, за 30 дней), `_process_rating_response` (`bot.py:3877`),
  `_notify_rating_admins` (`bot.py:3925`, текст «Оценка N по заказу #id»).
- Тексты клиенту (`docs/notification_rules.json`) общие, про услугу не говорят — не меняются.
- Образец теста входящего на настоящей базе — `tests/test_promo_inbound.py` (своя схема
  с `orders.rating_*`, пул с `search_path`).
- Админ-бот: цикл — `adminbot/feedback/` (`models.py`, `store.py`, `sync.py`), тексты —
  `adminbot/tg/feedback_cards.py`, миграция `019_feedback_tasks.sql`: `feedback_state`
  с ключом `PRIMARY KEY (order_id, mode)`. Связка уборки со сделкой — `adminbot.cleaning_links`
  (`010_cleaning_links.sql`: `order_id` = `cleaning_orders.id`, `status`, `real_lead_id`).
  Последняя миграция админ-бота — `019`. Обёртка обновления прогоняет ВСЕ миграции на каждом
  выкате (`deploy/update.sh`), поэтому миграция обязана быть идемпотентной.
- Роль `adminbot` видит таблицы `public` на чтение на уровне таблицы — новые поля
  `cleaning_orders` доступны ей без новых прав (предположение, проверить в задаче 4 тестом
  не выйдет — проверяет координатор на сервере после выката).

## Общие правила

- Первым шагом: `git rev-parse --short HEAD` и сверка с SHA из промпта. Не совпало — стоп и доклад.
- `git stash`, `git worktree`, `git merge`, `git push` — запрещены. `git status --short` до начала
  и перед каждым коммитом, в индекс — только свои файлы поимённо.
- Поведение химчистки не меняется: её тексты, выбор, поля — как были.
- Хард-правила админ-бота: в схему `public` не пишет; телефоны в журналах — маской.
- Файл длиннее 500 строк — сначала карта `<файл>.map.md`; число строк изменилось — обновить
  номера в карте после места правки; новые функции — строкой в карту.
- Тесты во время работы — только затронутые файлы, через `tail`; полный прогон бота — перед
  последним коммитом задачи:
  рабочий бот — `TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/raketaclean_test rtk proxy .venv-wahelp/bin/python -m pytest tests/ -q`;
  админ-бот — `cd raketa-admin-bot && TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/adminbot_test rtk proxy .venv/bin/python -m pytest -q`.
- Места правки в ТЗ названы как «найди все»: список в ТЗ может быть неполным.

---

## Задача 1. Рабочий бот: поля оценки у уборок и отметка «попросили»

**Файлы:** создать `app/migrations/0016_cleaning_orders_rating.sql`; изменить
`cleaning/schema.py`, `cleaning/handlers.py`; тест — рядом с существующими тестами уборок
(найди, где тестируется `_enqueue_cleaning_completed_notifications`, например
`tests/test_cleaning_helpers.py`).

**Даёт следующим задачам:** поля `cleaning_orders.rating_score smallint, rating_comment text,
rating_requested_at timestamptz, rating_replied_at timestamptz` (типы — как у `orders`).

1. Тест: после `_enqueue_cleaning_completed_notifications(...)` с правилами у уборки стоит
   `rating_requested_at`, а `rating_score/rating_comment/rating_replied_at` — пустые. Без правил
   (`rules is None`, функция выходит сразу) — отметки нет. Прогнать — падает.
2. Миграция `0016`: `ALTER TABLE cleaning_orders ADD COLUMN IF NOT EXISTS …` — четыре поля.
   Шапка — по образцу `0010` (зачем, ТЗ, решение владельца 30.09).
3. `ensure_cleaning_schema`: те же четыре `ADD COLUMN IF NOT EXISTS`; в докстринг — `0016`.
4. `_enqueue_cleaning_completed_notifications`: после enqueue `cleaning_order_rating_reminder` —
   `UPDATE cleaning_orders SET rating_requested_at = NOW(), rating_replied_at = NULL,
   rating_score = NULL, rating_comment = NULL WHERE id = $1` (зеркало химчистки).
5. Тесты зелёные, коммит.

Выполнено: 2026-09-30, eda3a98

## Задача 2. Рабочий бот: ответ-цифра — к уборке или к химчистке

**Файлы:** `bot.py` (`_select_pending_rating_order`, `_process_rating_response`,
`_notify_rating_admins`, маршрут входящего — найди все вызовы этих трёх функций),
`bot.py.map.md`; тест — новый `tests/test_cleaning_rating_inbound.py` по образцу
`tests/test_promo_inbound.py` (своя схема: `clients`, `orders` с `rating_*`, `cleaning_orders`
с `rating_*` и `deleted_at`, что ещё нужно маршруту).

**Потребляет:** поля задачи 1.

Поведение:
- Кандидаты — работы клиента за 30 дней без оценки: заказы — как сейчас
  (`created_at >= NOW() - 30 days`, `rating_score IS NULL`); уборки — `rating_score IS NULL`,
  `rating_requested_at IS NOT NULL` (решение 2: только новые), `deleted_at IS NULL`,
  `created_at >= NOW() - 30 days`.
- Берётся одна — с самой свежей просьбой: `COALESCE(rating_requested_at, created_at) DESC`
  по обеим вместе (для уборки это всегда `rating_requested_at`).
- Выбор возвращает вид работы (`'order'` | `'cleaning'`) и номер.
- `_process_rating_response` пишет оценку в таблицу своего вида; тексты клиенту и ветки 5 / 4 / 1–3 —
  те же; в `payload` добавить вид работы.
- Админам: «Оценка N по уборке №M» для уборки; для химчистки текст прежний («по заказу #id»).

Тесты (каждый — сначала падает):
1. Уборка с отметкой «попросили», ответ «5» → оценка у уборки, `rating_replied_at` стоит,
   ушёл `order_rating_response_high_client`.
2. У клиента ждут оценки и уборка, и химчистка; просьба по уборке свежее → оценка у уборки,
   заказ не тронут. И наоборот — просьба по химчистке свежее → у заказа.
3. Уборка без отметки «попросили» (старая) → не кандидат: при отсутствии заказа поведение —
   как сейчас без кандидата.
4. Удалённая уборка (`deleted_at`) → не кандидат.
5. Текст админам для уборки содержит «по уборке №».

Полный прогон рабочего бота, коммит.

## Задача 3. Админ-бот: состояние цикла различает уборку и заказ (миграция 020)

**Файлы:** создать `raketa-admin-bot/migrations/020_feedback_kind.sql`; изменить
`adminbot/feedback/models.py`, `adminbot/feedback/store.py`, `adminbot/feedback/sync.py`;
тесты — `tests/test_feedback_store.py`, `tests/test_feedback_sync.py`.

**Даёт задаче 4:** `KIND_ORDER = "order"`, `KIND_CLEANING = "cleaning"` в `models.py`;
`RatedOrder.kind: str = KIND_ORDER`, `FeedbackState.kind: str = KIND_ORDER`; ключ состояния —
`(kind, order_id)`; методы хранилища получают вид работы (сигнатуры — на усмотрение исполнителя,
записать в отметку «Выполнено»).

1. Тест хранилища: заказ №12 и уборка №12 в одном режиме — две независимые строки состояния.
   Тест миграции: прогон `020` дважды подряд — без ошибок, ключ `(kind, order_id, mode)`,
   старые строки получили `kind = 'order'`. Прогнать — падают.
2. Миграция `020`: колонка `kind text NOT NULL DEFAULT 'order' CHECK (kind IN ('order','cleaning'))`,
   первичный ключ `(kind, order_id, mode)`. Идемпотентно: повторный прогон ничего не ломает
   (обёртка гоняет все миграции на каждом выкате).
3. Модели, хранилище, цикл — ключ по `(kind, order_id)` везде, где сейчас ключ `order_id`
   (найди все места в `adminbot/feedback/`).
4. Поведение для химчистки не меняется: существующие тесты цикла — зелёные без правки смысла.
5. Полный прогон админ-бота, коммит.

## Задача 4. Админ-бот: оценки уборок в цикле и тексты по виду работы

**Файлы:** `adminbot/feedback/store.py` (`PgFeedbackSource.rated_orders`),
`adminbot/tg/feedback_cards.py`, `adminbot/feedback/sync.py` (журнал), `docs/deploy.md`;
тесты — `tests/test_feedback_store.py`, `tests/test_feedback_sync.py`, тесты текстов
(найди, где тестируется `feedback_cards`).

**Потребляет:** задачу 3; поля задачи 1 (в тестовой схеме `public.cleaning_orders` с `rating_*`).

1. Тесты (падают): источник отдаёт оценённую уборку — связка `adminbot.cleaning_links`
   (`status = 'done'`, `real_lead_id` не пусто) + `public.cleaning_orders` (`rating_score` и
   `rating_replied_at` не пусты) → `RatedOrder(kind='cleaning', lead_id=real_lead_id, …)`;
   химчистка — как раньше. Тексты уборки: «Клиент оценил уборку №12 на 4 …».
2. `rated_orders`: вторая выборка по уборкам — тем же приёмом, что заказы (связки из
   `own_pool`, оценки из `bot_pool`, только чтение).
3. Тексты `feedback_cards.py`: все, где работа называется «заказ» (найди все), — по виду работы:
   «уборку №N» / «заказ №N», «уборка №N» / «заказ №N». Журнал `sync.py` («Заказ №%s») — так же.
4. `docs/deploy.md`: раздел «Повторный заказ» — что уборки идут тем же циклом и тем же
   выключателем; строка сброса `failed` — с `kind` (`DELETE … WHERE kind = '<order|cleaning>'
   AND order_id = <ID> AND mode = 'live'`).
5. Полный прогон админ-бота, коммит.

---

## Выкат (делает координатор, не исполнитель)

Порядок обязателен: админ-бот читает поля, которые заводит рабочий бот.

1. Пуш `main`.
2. Рабочий бот: `git pull --ff-only` в `/opt/telegram-bot`, перезапуск `telegram-bot` — паролем
   владельца. Поля `cleaning_orders.rating_*` появятся при запуске (`ensure_cleaning_schema`).
   Проверка: поля есть; `SELECT` от роли `adminbot` по новым полям работает.
3. Админ-бот: `rsync` → `sudo raketa-admin-bot-update` (миграция 020 применится сама) →
   `--report`. Проверка: в `feedback_state` колонка `kind`, старые строки — `order`.
4. Приёмка — первая новая уборка после выката: у неё стоит «попросили», ответ клиента
   записан у уборки, задача «Повторный заказ» в сделке уборки обработана.
