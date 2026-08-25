# amo_sync v1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Автоматическое проведение сделок amoCRM по данным БД рабочего бота — пути А–Г из одобренного дизайна `docs/plans/2026-08-24-amo-sync-design.md`.

**Architecture:** Отдельный сервис (этот репозиторий): ядро (config, две БД-связи, Telegram-бот владельца) + функция amo_sync (наблюдатель → матчер → чек-лист исполнителя → вечерняя сверка). БД рабочего бота — только SELECT; своё состояние — в схеме `adminbot` того же Postgres; запись в амо — отдельной интеграцией.

**Tech Stack:** Python 3.11, aiogram 3.22, asyncpg 0.30, aiohttp 3.12, pytest + pytest-asyncio. Референсы для порта: `~/Projects/tgbot-v1/notifications/amocrm_api.py` (клиент амо), `~/Projects/tgbot-v1/bot.py:3251` (нормализация телефона).

**Константы амо из разведки** (кладём в `adminbot/amo/ids.py`, значения проверены 2026-08-24, см. `tgbot-v1/recon/01-amocrm.md`):

```python
PIPELINE_PRIMARY = 4482751          # Воронка первичной обработки
PIPELINE_REALIZATION = 4482787      # Воронка для реализации
PIPELINE_CARPETS = 4645519          # Ковры Кристал (v1 их НЕ трогает)
STATUS_SUCCESS = 142                # успешный этап любой воронки
STATUS_CLOSED = 143
REAL_STAGE_CREATED = 41463832       # Заказ оформлен
REAL_STAGE_CONFIRMED = 41463838     # Заказ подтвержден, Мастер назначен
REAL_STAGE_DONE = 41463964          # Заказ выполнен
FIELD_SERVICE = 271915              # Услуга (multiselect)
FIELD_ORDER_DATETIME = 18701        # Дата и время заказа
FIELD_ADDRESS = 18639               # Адрес
TASK_TYPES_TO_CLOSE = {2270716, 2270737, 2270740, 2270743, 2301196}
TASK_TYPE_FEEDBACK = 2270746        # «Получить ОС» — закрываем только если есть оценка в боте
```

---

## Фаза 0 — скелет проекта

### Task 1: Каркас пакета и конфиг

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `pytest.ini`
- Create: `adminbot/__init__.py`, `adminbot/config.py`
- Create: `.env.example`
- Test: `tests/test_config.py`

**Step 1: Write the failing test**

```python
# tests/test_config.py
from adminbot.config import Settings

def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("ADMINBOT_TG_TOKEN", "123:abc")
    monkeypatch.setenv("ADMINBOT_OWNER_TG_ID", "42")
    monkeypatch.setenv("BOT_DB_DSN", "postgresql://ro@localhost/clients_db")
    monkeypatch.setenv("AMO_BASE_URL", "https://raketacleancrm.amocrm.ru")
    monkeypatch.setenv("AMO_TOKEN", "tok")
    s = Settings.from_env()
    assert s.owner_tg_id == 42
    assert s.amo_sync_enabled is False      # по умолчанию ВЫКЛЮЧЕНО (дизайн: kill switch)
    assert s.amo_sync_dry_run is True       # по умолчанию репетиция, не запись

def test_settings_missing_required(monkeypatch):
    monkeypatch.delenv("ADMINBOT_TG_TOKEN", raising=False)
    import pytest
    with pytest.raises(RuntimeError, match="ADMINBOT_TG_TOKEN"):
        Settings.from_env()
```

**Step 2:** Run: `pytest tests/test_config.py -v` → FAIL (`ModuleNotFoundError: adminbot`).

**Step 3: Implement** — `adminbot/config.py`: `@dataclass(frozen=True) class Settings` с полями `tg_token, owner_tg_id:int, bot_db_dsn, amo_base_url, amo_token, amo_sync_enabled:bool=False, amo_sync_dry_run:bool=True, backlog_from:date=date(2026,8,21), reconcile_hour_msk:int=21, salesbot_wait_sec:int=600, poll_interval_sec:int=60`; classmethod `from_env()` — обязательные переменные без значения → `RuntimeError` с именем переменной. Флаги: `"1"/"true"` → True.

`requirements.txt`: `aiogram==3.22.0`, `asyncpg==0.30.0`, `aiohttp==3.12.15`, `python-dotenv==1.1.1`. `requirements-dev.txt`: `pytest`, `pytest-asyncio`. `pytest.ini`: `asyncio_mode = auto`.

`.env.example` — все переменные с комментариями, без значений секретов.

**Step 4:** `pytest tests/test_config.py -v` → PASS.

**Step 5:** `git add -A && git commit -m "feat: project skeleton and Settings config"`

### Task 2: Нормализация телефона (канон из bot.py:3251)

**Files:**
- Create: `adminbot/phone.py`
- Test: `tests/test_phone.py`

**Step 1: Write the failing test** — кейсы из разведки (реальные форматы календаря/бота, `tgbot-v1/recon/02-calendar.md`):

```python
# tests/test_phone.py
from adminbot.phone import normalize_phone, last10

CASES = [
    ("89601861067", "9601861067"),
    ("+7 922 088‑82‑08", "9220888208"),          # юникод-дефисы
    ("Ирина\xa089159496642", "9159496642"),                  # nbsp перед номером
    ("Диван 3300₽\nСушка 40\n89960663965 Фания", "9960663965"),  # цены перед номером
    ("9202572757", "9202572757"),                            # 10 цифр с 9
    ("Матрас 160/1 2000₽ + 700₽", None),                     # телефона нет
    ("", None),
]

def test_last10():
    for raw, expected in CASES:
        assert last10(raw) == expected, raw

def test_normalize_plus7():
    assert normalize_phone("89601861067") == "+79601861067"
    assert normalize_phone("нет номера") is None              # НЕ возвращаем мусор (отличие от bot.py)
```

**Step 2:** Run: `pytest tests/test_phone.py -v` → FAIL.

**Step 3: Implement** — `adminbot/phone.py`: посимвольный скан как в `bot.py:3251` (старт с цифры 7/8/9; разделители `" -()\xa0‑  \t"` и `+` в начале; 7/8 → ровно 11 цифр, 9 → ровно 10), НО при неудаче возвращаем `None`, не исходную строку. `last10(raw)` → 10 цифр без кода страны; `normalize_phone(raw)` → `+7XXXXXXXXXX` или `None`.

**Step 4:** PASS. **Step 5:** `git commit -m "feat: canonical phone normalization (ported char-scan)"`

### Task 3: Схема собственного хранилища и доступ к БД

**Files:**
- Create: `migrations/001_adminbot_schema.sql`
- Create: `adminbot/db.py`
- Test: `tests/test_db_schema.py` (проверка SQL на локальном Postgres; если недоступен — `pytest.skip`)

**Step 1: SQL** (это и тест-фикстура, и прод-миграция; применяется вручную `psql -f`, как принято в tgbot-v1):

```sql
CREATE SCHEMA IF NOT EXISTS adminbot;

-- привязка заказа бота к сделкам амо + чек-лист шагов (идемпотентность, дизайн §5.4)
CREATE TABLE IF NOT EXISTS adminbot.amo_links (
    order_id        bigint PRIMARY KEY,            -- orders.id рабочего бота (не FK: чужая схема)
    phone10         text NOT NULL,
    status          text NOT NULL DEFAULT 'new',   -- new|in_progress|waiting_owner|waiting_salesbot|done|error
    path            text,                          -- A|B|C|D (путь из дизайна §4)
    primary_lead_id bigint,
    real_lead_id    bigint,
    checklist       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {"step_name": "2026-08-25T10:00:00Z", ...}
    question_msg_id bigint,                        -- id карточки-вопроса в TG, если путь Г
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_amo_links_status ON adminbot.amo_links (status);

-- журнал действий в амо (прозрачность: что робот сделал и когда)
CREATE TABLE IF NOT EXISTS adminbot.amo_actions (
    id          bigserial PRIMARY KEY,
    order_id    bigint NOT NULL,
    action      text NOT NULL,                     -- update_lead|move_stage|create_lead|close_task|...
    amo_entity  text,
    amo_id      bigint,
    dry_run     boolean NOT NULL,
    payload     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
```

**Step 2:** `tests/test_db_schema.py` — применить SQL к тестовой БД (env `TEST_DB_DSN`, иначе skip), вставить/прочитать строку в `amo_links`.

**Step 3:** `adminbot/db.py` — два пула: `bot_pool` (DSN read-only пользователя) и `own_pool` (тот же Postgres, права на схему `adminbot`); функции `fetch_unprocessed_orders(bot_pool, own_pool, since: date) -> list[Order]` (заказы `orders` без строки в `amo_links`; поля: id, phone_digits, created_at, amount_total, master имена через `order_masters`+`staff`, rating_score, адрес/имя клиента через `clients`) и CRUD для `amo_links`/`amo_actions`. `Order` — frozen dataclass в `adminbot/models.py`.

ВАЖНО (правило CLAUDE.md): ни одного INSERT/UPDATE в схему `public` — только SELECT. Проверить глазами каждый SQL в `db.py` перед коммитом.

**Step 4:** PASS (или skip без Postgres). **Step 5:** `git commit -m "feat: adminbot schema and db access layer"`

---

## Фаза 1 — матчер и экзамен на истории

### Task 4: Клиент amoCRM — чтение

**Files:**
- Create: `adminbot/amo/__init__.py`, `adminbot/amo/ids.py` (константы из шапки), `adminbot/amo/client.py`
- Test: `tests/test_amo_client.py` (aiohttp-мок через `aioresponses` ИЛИ ручной fake-сервер `aiohttp.test_utils`; НЕ живое амо)

Порт из `tgbot-v1/notifications/amocrm_api.py` (класс `AmoCRMAPIClient`): `get`, пагинация, ошибки `AmoAuthError/AmoRateLimitError/AmoError`, ретраи (3 попытки, пауза 2^n сек). Методы чтения: `find_contacts_by_phone(last10) -> list[Contact]` (амо ищет по `query`), `get_contact_leads(contact_id) -> list[Lead]`, `get_lead(lead_id)`, `get_lead_tasks(lead_id)`. Всегда экранировать телефон в логах (`phone[-4:]`).

Шаги: тест на пагинацию и на 401 → исключение; реализация; PASS; `git commit -m "feat: amo read client"`.

### Task 5: Матчер («сваха») — чистая функция, ядро системы

**Files:**
- Create: `adminbot/sync/__init__.py`, `adminbot/sync/matcher.py`
- Test: `tests/test_matcher.py`

Матчер НЕ ходит в сеть: вход — заказ + список кандидатов-сделок (уже загруженных), выход — решение.

**Step 1: Write the failing tests** — все хитрые кейсы разведки (`tgbot-v1/recon/06-matching-metrics.md`) как таблица:

```python
# tests/test_matcher.py
from adminbot.sync.matcher import match, Decision, LeadInfo
from datetime import date

def L(id, pipeline, status, order_date=None):
    return LeadInfo(lead_id=id, pipeline_id=pipeline, status_id=status, order_date=order_date)

REAL, PRIM, SUCCESS, CLOSED = 4482787, 4482751, 142, 143
CREATED = 41463832

def test_path_a_single_open_realization():
    d = match(order_date=date(2026,8,20), candidates=[L(1, REAL, CREATED)])
    assert d == Decision(kind="use_realization", lead_id=1)

def test_path_b_only_primary_lead():
    d = match(order_date=date(2026,8,20), candidates=[L(2, PRIM, 41463535)])
    assert d == Decision(kind="use_primary", lead_id=2)

def test_path_c_no_candidates():
    assert match(order_date=date(2026,8,20), candidates=[]).kind == "create_new"

def test_pair_primary_plus_realization_same_client_is_not_ambiguous():
    # пара «первичная(успех) + автосделка» — это ОДИН заказ; приоритет реализации
    d = match(order_date=date(2026,7,23), candidates=[
        L(10, PRIM, SUCCESS, date(2026,7,23)), L(11, REAL, CREATED, date(2026,7,23))])
    assert d == Decision(kind="use_realization", lead_id=11)

def test_stale_dates_resolved_by_open_stage():
    # кейс «Лен!Ковролин, Анна»: даты 2024 года, но открытая сделка одна
    d = match(order_date=date(2026,6,16), candidates=[
        L(20, REAL, SUCCESS, date(2024,5,14)), L(21, REAL, CREATED, date(2026,6,8))])
    assert d == Decision(kind="use_realization", lead_id=21)

def test_two_open_same_date_is_ambiguous():
    # кейс «Ниж! Матрас, Юлия» с ДВУМЯ открытыми: вопрос владельцу
    d = match(order_date=date(2026,7,13), candidates=[
        L(30, REAL, CREATED, date(2026,7,13)), L(31, REAL, CREATED, date(2026,7,13))])
    assert d.kind == "ask_owner" and set(d.options) == {30, 31}

def test_already_completed_deal_binds_without_touching():
    # вы провели руками раньше робота (дизайн §5.1)
    d = match(order_date=date(2026,8,20), candidates=[L(40, REAL, SUCCESS, date(2026,8,20))])
    assert d == Decision(kind="already_done", lead_id=40)

def test_carpets_pipeline_ignored():
    # заказ из бота — никогда не ковры (решение владельца №7-контекст)
    d = match(order_date=date(2026,8,20), candidates=[L(50, 4645519, 42638827)])
    assert d.kind == "create_new"
```

**Step 2:** FAIL. **Step 3: Implement** `match()` — порядок правил (из дизайна §4 и метрик):
1. отбросить ковровые воронки и архивные;
2. открытые сделки реализации: одна → `use_realization`; несколько → фильтр по дате ±2 дня; осталась одна → она; иначе `ask_owner(options)`;
3. нет открытых в реализации, но есть завершённая (142) с датой ±2 дня → `already_done`;
4. открытый лид в первичной: один → `use_primary`; несколько → дата ±2, иначе `ask_owner`;
5. ничего → `create_new`.

**Step 4:** PASS. **Step 5:** `git commit -m "feat: matcher core with recon edge cases"`

### Task 6: Экзамен на истории (скрипт, режим чтения)

**Files:**
- Create: `scripts/history_exam.py`
- Modify: `adminbot/db.py` (запрос заказов за произвольный период, включая уже проведённые вручную)

Скрипт: взять заказы бота за 90 дней → для каждого: контакты/сделки из амо (только чтение) → решение матчера → сравнить с фактом (какая сделка реально стоит на 142 в реализации с той датой). Вывод: `совпало / вопрос / ошибка` + список расхождений (телефоны маскировать). Порог годности из дизайна: ≥92% авто, ≤8% вопросов, 0 «уверенно, но неверно».

Шаги: запуск `python -m scripts.history_exam --days 90` на реальном амо-токене ЧТЕНИЯ (тот же, что в разведке); результат сохранить в `docs/plans/history-exam-result.md`; `git commit -m "feat: history exam script + baseline result"`. Это ворота фазы: **дальше не идти, пока экзамен не сдан** — расхождения сначала чинятся в правилах матчера.

---

## Фаза 2 — запись в амо

### Task 7: Клиент amoCRM — запись (с dry-run)

**Files:**
- Modify: `adminbot/amo/client.py`
- Test: `tests/test_amo_write.py` (мок-сервер; проверять СФОРМИРОВАННЫЕ запросы, не живое амо)

Методы: `update_lead(lead_id, *, price=None, custom_fields=None)`, `move_lead(lead_id, pipeline_id, status_id)`, `create_lead(pipeline_id, status_id, contact_id, name, price, custom_fields)`, `create_contact(name, phone_plus7)`, `complete_task(task_id, result_text="Закрыто роботом amo_sync")`. Каждый метод: при `dry_run=True` НЕ шлёт запрос, а возвращает описание намерения; вызывающий слой пишет и намерение, и факт в `adminbot.amo_actions`.

Тесты: PATCH `/api/v4/leads/{id}` с нужным телом; complex-создание сделки с привязкой контакта (`_embedded.contacts`); dry-run не делает HTTP-вызовов. Commit: `feat: amo write client with dry-run`.

---

## Фаза 3 — исполнитель

### Task 8: Чек-лист исполнителя (state machine, чистая логика)

**Files:**
- Create: `adminbot/sync/checklist.py`
- Test: `tests/test_checklist.py`

Шаги по путям (дизайн §4): `fill_realization → move_realization_done → close_autotasks → close_feedback_task?` (А); `fill_primary → move_primary_success → wait_salesbot → …путь А` (Б); `ensure_contact → create_primary_lead → …путь Б` (В). Функция `next_step(path, checklist) -> str|None` — по записанному прогрессу выдаёт следующий шаг; тесты: продолжение с места остановки, повторный вызов завершённого чек-листа → `None` (идемпотентность, дизайн §5.4). Commit: `feat: executor checklist state machine`.

### Task 9: Движок синхронизации

**Files:**
- Create: `adminbot/sync/engine.py`
- Test: `tests/test_engine.py` (fake amo-клиент + fake db — протоколы/duck typing)

`process_order(order, deps)` — оркестрация: матчер → путь → шаги чек-листа через amo-клиент → запись прогресса в `amo_links` после КАЖДОГО шага. Правила: `ask_owner` → статус `waiting_owner` + карточка в TG; `wait_salesbot` → статус `waiting_salesbot`, повторная проверка при следующем тике, после `salesbot_wait_sec` без автосделки → `waiting_owner` (дизайн §5.3); ошибка амо → статус `error`, `last_error`, ретрай следующим тиком; «Услуга» по мастеру: Никита/Дима → мебель, Оля → клининг (маппинг в конфиге `SERVICE_BY_MASTER`, точные enum-значения подставить в Task 13-примечании ниже). Commit: `feat: sync engine orchestration`.

### Task 10: Наблюдатель и вечерняя сверка

**Files:**
- Create: `adminbot/sync/watcher.py`, `adminbot/sync/reconcile.py`
- Test: `tests/test_reconcile.py`

Watcher: вечный цикл `poll_interval_sec`; guard: `if not settings.amo_sync_enabled: sleep; continue` (kill switch). Reconcile 21:00 МСК (расчёт следующего запуска как `schedule_daily_job` в tgbot-v1): пересканировать с `backlog_from`, найти пропуски/зависшие (`error`, `waiting_salesbot` старше часа, `waiting_owner`), собрать сводку-объект (тест: содержимое сводки по подготовленным строкам `amo_links`). Commit: `feat: watcher loop and evening reconciliation`.

---

## Фаза 4 — Telegram-интерфейс владельца

### Task 11: Бот-скелет и команды управления

**Files:**
- Create: `adminbot/tg/__init__.py`, `adminbot/tg/bot.py`, `adminbot/main.py`
- Test: `tests/test_tg_guard.py`

aiogram 3: единственный разрешённый пользователь — `owner_tg_id`, все прочие получают отказ (тест guard-фильтра). Команды: `/status` (включено? dry-run? очередь: new/waiting/error), `/pause` и `/resume` (пишут флаг в `adminbot`-таблицу настроек — добавить `adminbot.settings(key,value)` в миграцию 002), `/help`. `main.py`: запуск бота + watcher + reconcile задач (образец композиции — `tgbot-v1/bot.py:13275 main()`, но без глобальных переменных: контейнер `App` с зависимостями). Commit: `feat: owner telegram bot with status/pause/resume`.

### Task 12: Карточки-вопросы и сводка

**Files:**
- Create: `adminbot/tg/cards.py`
- Test: `tests/test_cards.py` (формат текста и callback-данных, маскирование телефонов)

Карточка пути Г: «Заказ №596 · Ирина …1881 · чек 5 950₽ · 24.08» + inline-кнопки `[Сделка 22.07] [Сделка 24.07] [Создать новую]`; callback `amosync:{order_id}:{lead_id|new}`; обработчик пишет выбор в `amo_links` (статус → `new`, путь по выбору) — движок доделает следующим тиком. Вечерняя сводка: «Проведено N (№…→#…), создано M, ждут ответа K, ошибок E». Предпросмотр хвоста: список планируемых действий + кнопка «🚀 Поехали» (снимает dry-run для перечисленных заказов) и «✋ Отложить». Commit: `feat: question cards, summary and backlog preview`.

---

## Фаза 5 — ввод в бой

### Task 13: Enum «Услуга» и финальный конфиг

Разово запросить (чтение) `leads/custom_fields` → выписать точные enum id для «мебель» и «клининг» (человеческие названия уточнены владельцем: Никита/Дима → мебель, Оля → клининг; выбрать ближайшие значения списка, показать владельцу на подтверждение в TG/чате). Занести в `adminbot/amo/ids.py` (`SERVICE_ENUM_FURNITURE`, `SERVICE_ENUM_CLEANING`) + маппинг мастеров в `.env.example` (`SERVICE_BY_MASTER=Никита:furniture,Дима:furniture,Оля:cleaning`). Commit: `feat: service enum mapping`.

### Task 14: Деплой-раннбук и systemd

**Files:**
- Create: `docs/deploy.md`, `deploy/adminbot.service`

По образцу `tgbot-v1/docs/analytics_deploy.md`: путь `/opt/raketa-admin-bot`, отдельный venv, systemd unit (Restart=always, EnvironmentFile=/opt/raketa-admin-bot/.env), NOPASSWD-строка для restart — задокументировать, попросить владельца добавить. Чек-лист первого запуска: (1) владелец создаёт бота в BotFather → токен; (2) владелец создаёт интеграцию амо с записью → токен; (3) создать БД-пользователя: `GRANT SELECT ON ALL TABLES IN SCHEMA public`, `ALL ON SCHEMA adminbot`; (4) применить миграции; (5) запуск с `AMO_SYNC_ENABLED=0`. Commit: `docs: deploy runbook`.

### Task 15: Репетиция → экзамен хвоста → боевой запуск

Последовательность (каждый шаг подтверждает владелец):
1. Деплой, `AMO_SYNC_ENABLED=1`, `AMO_SYNC_DRY_RUN=1` — 1–2 дня решения сыплются владельцу «что я сделал БЫ».
2. Владелец жмёт предпросмотр хвоста (с 2026-08-21) → «🚀 Поехали» → первый боевой прогон, сверка глазами в амо.
3. `AMO_SYNC_DRY_RUN=0` навсегда; неделя ежевечерних сводок под присмотром.
4. Ретроспектива: доля вопросов, ложные срабатывания; при необходимости — правки правил матчера (тесты сначала!).

---

## Definition of Done (v1)

- [ ] Экзамен на истории: ≥92% авто, 0 «уверенно-неверно» (`docs/plans/history-exam-result.md`).
- [ ] Все тесты зелёные: `pytest -q`.
- [ ] Хвост с 21.08 разобран, владелец подтвердил корректность в амо глазами.
- [ ] Неделя боевой работы: сводки приходят, вопросов ≤ ~2/нед, ручного проведения нет.
- [ ] `docs/deploy.md` актуален; kill switch проверен (пауза/возобновление).
- [ ] Ни одной записи в схему `public` БД бота (проверка: `grep -rn "INSERT INTO\|UPDATE " adminbot/db.py` — только `adminbot.`).
