# autocall Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Автозвонок по заявке с сайта — одобренный дизайн `docs/plans/2026-08-31-site-lead-autocall-design.md`: робот видит новую сделку с тегом «Заявка с сайта» и командует onlinePBX соединить менеджера с клиентом, с повторами и переносом сделки по правилам владельца.

**Architecture:** Новая функция `adminbot/autocall/` по анатомии gcal: наблюдатель (опрос амо) → чистая машина попыток → исполнитель (АТС + амо + сообщения). Чистая логика (окно звонков, переходы цепочки) не знает о сети; АТС и Telegram — за протоколами с фейками. В dry-run всё состояние в памяти, включая курсор опроса (правило «репетиция не оставляет следов»).

**Tech Stack:** тот же: Python 3.11, aiogram 3, asyncpg, aiohttp, pytest + pytest-asyncio. Образцы: `adminbot/gcal/watcher.py` (наблюдатель), `adminbot/amo/client.py` (Intent/dry-run), `migrations/005_gcal.sql` (хранилище), `deploy/update.sh` (выключатели).

**Решения владельца** — §2 дизайна. Технические решения этапа планирования (владельца не требуют):
- Транспорт сообщений менеджеру: **send-only через токен рабочего бота** (`WORKER_TG_TOKEN` + `MANAGER_TG_CHAT_ID` в `.env`). Отправка сообщений не конфликтует с поллингом рабочего бота (конфликтует только getUpdates). Переменные пусты → сообщения менеджеру уходят владельцу с пометкой — мягкая деградация.
- Неизвестный исход звонка (история АТС не нашлась за 5 минут) = засчитанная неудачная попытка по ветке «менеджер не взял» — робот никогда не звонит бесконтрольно.
- Опрос амо раз в 30 сек; предохранитель — не больше 4 попыток на сделку (§4.5 дизайна).

**Готовые константы амо** (`adminbot/amo/ids.py`, менять не нужно):
`PIPELINE_PRIMARY = 4482751`, `PRIM_STAGE_NEW_LEAD = 41463535` («Новый лид»),
`PRIM_STAGE_NO_CONTACT = 41463541` («Не было 1-го касания» — этап уже существует).
Тег «Заявка с сайта» — строковый тег сделки, id прочитать экзаменом (Задача 6).

**Вне этого плана:** сообщение менеджеру о новой заявке с сайта (ссылка + описание) делается в `tgbot-v1` (вебхук и уведомления уже там, см. `notifications/amocrm.py`) — отдельная задача в том репозитории, в его сессии.

---

## Фаза 1 — чистая логика (не требует ни АТС, ни владельца)

### Задача 1: Конфиг autocall

**Files:** Modify: `adminbot/config.py`, `.env.example`; Test: `tests/test_config.py`

Новые поля `Settings` (по образцу gcal_*): `autocall_enabled=False`, `autocall_dry_run=True`,
`autocall_poll_interval_sec=30`, `autocall_window_from_hour=10`, `autocall_window_to_hour=20`,
`pbx_base_url=""`, `pbx_api_key=""`, `pbx_manager_dial=""` (внутренний номер/цепочка менеджера в АТС),
`worker_tg_token=""`, `manager_tg_chat_id=0`.
Env-имена: `AUTOCALL_ENABLED`, `AUTOCALL_DRY_RUN`, `AUTOCALL_POLL_INTERVAL_SEC`,
`AUTOCALL_WINDOW_FROM`, `AUTOCALL_WINDOW_TO`, `PBX_BASE_URL`, `PBX_API_KEY`,
`PBX_MANAGER_DIAL`, `WORKER_TG_TOKEN`, `MANAGER_TG_CHAT_ID`.
PBX-переменные НЕ в `REQUIRED_ENV`: обязательны только при включённой функции
(проверка при сборке в Задаче 11, как gcal с ключом Google). Телефоны и токены — только в `.env`, не в git.

1. Тесты `test_autocall_defaults` (всё выключено, dry-run включён) и `test_autocall_env_overrides` → FAIL.
2. Поля + чтение в `from_env()` → PASS. 3. Commit `feat(autocall): конфиг и выключатели`.

### Задача 2: Миграция 007 и хранилище

**Files:** Create: `migrations/007_autocall.sql`, `adminbot/autocall/__init__.py`, `adminbot/autocall/store.py`; Test: `tests/test_autocall_store.py`

```sql
-- Цепочка попыток: одна строка на сделку. PK = lead_id, поэтому повторный
-- проход наблюдателя не заведёт вторую цепочку по той же заявке.
CREATE TABLE IF NOT EXISTS adminbot.autocall_leads (
    lead_id          bigint PRIMARY KEY,
    phone10          text,
    status           text NOT NULL DEFAULT 'queued',
    -- queued|calling|done|no_contact|gave_up|error
    attempts_total   int NOT NULL DEFAULT 0,
    manager_failures int NOT NULL DEFAULT 0,
    client_failures  int NOT NULL DEFAULT 0,
    next_action_at   timestamptz,              -- когда пора действовать
    call_id          text,                     -- идентификатор звонка в АТС
    called_at        timestamptz,              -- когда отдали команду АТС
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_autocall_due ON adminbot.autocall_leads (status, next_action_at);

-- Журнал действий — тот же смысл, что gcal_actions.
CREATE TABLE IF NOT EXISTS adminbot.autocall_actions (
    id bigserial PRIMARY KEY, lead_id bigint NOT NULL, action text NOT NULL,
    dry_run boolean NOT NULL, payload jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_autocall_actions_lead ON adminbot.autocall_actions (lead_id, created_at);

-- Курсор опроса амо: с какого created_at читать. В репетиции НЕ пишется
-- (память): боевой запуск должен начать со своего момента включения.
CREATE TABLE IF NOT EXISTS adminbot.autocall_cursor (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    created_from timestamptz NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
```

`store.py`: `MemoryAutocallStore` + `PgAutocallStore` с одинаковым интерфейсом
(`get/create/update/due(now)/cursor()/save_cursor/log_action/actions_for`) —
по образцу `adminbot/gcal/store.py`.

1. Тесты хранилища (Memory — всегда; Pg — skip без Postgres, образец `tests/test_gcal_store.py`) → FAIL.
2. Реализация → PASS. 3. Прогнать миграцию на локальном Postgres. 4. Commit.

### Задача 3: Окно звонков

**Files:** Create: `adminbot/autocall/window.py`; Test: `tests/test_autocall_window.py`

```python
def next_call_moment(created_at: datetime, *, now: datetime,
                     from_hour: int = 10, to_hour: int = 20) -> datetime:
    """Когда можно звонить по заявке. Всё в МСК (MOSCOW_TZ из amo/fields.py).

    В окне 10:00–20:00 — прямо сейчас; ночью/вечером — ближайшие 10:00
    (решение владельца №5: заявка в 21:30 ждёт до утра).
    """
```

Тесты: заявка днём → сейчас; в 21:30 → завтра 10:00; в 02:00 → сегодня 10:00;
в 09:59 → сегодня 10:00; ровно в 20:00 → завтра 10:00; повтор, назначенный на
19:58 + 10 минут → завтра 10:00 (повторы тоже уважают окно). Commit.

### Задача 4: Машина попыток (ядро)

**Files:** Create: `adminbot/autocall/chain.py`; Test: `tests/test_autocall_chain.py`

Чистая функция переходов; никакой сети. Исходы и эффекты:

```python
class Outcome(str, Enum):
    CONNECTED = "connected"            # менеджер и клиент поговорили
    MANAGER_NO_ANSWER = "manager_no_answer"
    CLIENT_NO_ANSWER = "client_no_answer"
    UNKNOWN = "unknown"                # история АТС не нашлась — считаем как менеджера

MAX_ATTEMPTS = 4                       # предохранитель §4.5 дизайна
RETRY_MANAGER = timedelta(minutes=5)   # решение владельца №3
RETRY_CLIENT = timedelta(minutes=10)   # решение владельца №4

def advance(chain: Chain, outcome: Outcome, now: datetime) -> tuple[Chain, list[Effect]]: ...
```

Эффекты (исполняет движок): `Retry(at)`, `NotifyManager(kind)`, `MoveLeadNoContact()`, `Done()`, `GaveUp(reason)`.
Правила: CONNECTED → done. MANAGER_NO_ANSWER/UNKNOWN → manager_failures+1;
вторая → `NotifyManager("manager_unreachable")` + gave_up; иначе Retry(+5 мин).
CLIENT_NO_ANSWER → client_failures+1; первая → `NotifyManager("client_retry_10")`
(«Попытка звонка не удалась, повтор через 10 минут») + Retry(+10 мин);
вторая → `MoveLeadNoContact` + `NotifyManager("no_contact_final")` + no_contact.
Перед любым Retry: attempts_total ≥ MAX_ATTEMPTS → gave_up + уведомление.

Тесты — все ветки по отдельности, смешанные исходы (менеджер не взял → взял,
клиент не взял → соединились), лимит 4 попытки, идемпотентность dataclass'а. Commit.

---

## Фаза 2 — чтение амо и экзамен на истории

### Задача 5: Новые сделки с сайта из амо

**Files:** Modify: `adminbot/amo/client.py`, `adminbot/amo/fields.py`; Create: `adminbot/autocall/leads.py`; Tests: `tests/test_amo_client.py`, `tests/test_autocall_leads.py`

`AmoClient.find_leads_created_since(pipeline_id, status_id, created_from_ts)`:

```python
params = [
    ("filter[statuses][0][pipeline_id]", pipeline_id),
    ("filter[statuses][0][status_id]", status_id),
    ("filter[created_at][from]", created_from_ts),
    ("order[created_at]", "asc"),
    ("with", "contacts"),
]
return await self.get_all("/api/v4/leads", "leads", params=params)
```

`fields.lead_tag_names(lead) -> tuple[str, ...]` из `_embedded.tags`.
`autocall/leads.py`: `is_site_lead(lead)` — тег «Заявка с сайта» (сравнение без
регистра); `lead_phone10(lead, contact)` — телефон через существующие
`lead_contact_ids` + `contact_phones` + `phone.last10`. Тесты на JSON-фикстурах
(без ПД: телефоны вымышленные). Commit.

### Задача 6: Экзамен на истории — CHECKPOINT владельца

**Files:** Create: `scripts/autocall_exam.py`; Test: `tests/test_autocall_exam.py` (разборная часть)

Скрипт (чтение, запускается с VPS — амо доступна оттуда): выбрать сделки этапа
«Новый лид» + все сделки с тегом «Заявка с сайта» за последние 60 дней; отчёт:
сколько заявок с сайта, у скольких извлёкся телефон (цель 100%), сколько сделок
БЕЗ тега попали бы под фильтр (цель 0), реальный вид `_embedded.tags` в списочном
ответе (подтвердить, что тег виден без отдельного запроса). Замерить задержку
«письмо → сделка»: `created_at` свежей заявки против времени письма в ленте.

**Checkpoint:** результаты — владельцу (метрики + примеры БЕЗ полных телефонов в
тексте отчёта в git; в Telegram владельцу телефоны полные — его правило). Commit.

---

## Фаза 3 — onlinePBX (нужен ключ от владельца)

### Задача 7: Клиент АТС и probe — CHECKPOINT владельца

**Files:** Create: `adminbot/autocall/pbx.py`, `scripts/pbx_probe.py`; Test: `tests/test_autocall_pbx.py`

**Сначала от владельца:** ключ API onlinePBX (личный кабинет → Настройки → API;
положить на VPS в `~/.pbx.env`, не в git) и `PBX_MANAGER_DIAL` — как в АТС
адресуется цепочка менеджера (внутренний номер; смотрим вместе в кабинете).

`pbx.py` — протокол + реализация:

```python
class Pbx(Protocol):
    async def call_now(self, *, to_dial: str, client_phone: str) -> str: ...
    async def call_outcome(self, call_id: str, *, called_at: datetime) -> Outcome | None: ...
    # None — звонок ещё идёт / истории пока нет

class OnlinePbx:  # api2.onlinepbx.ru; MemoryPbx — фейк для тестов и репетиции
    ...
```

Порядок: `scripts/pbx_probe.py` руками против боевой АТС — аутентификация,
тестовый `call_now`, где «клиент» = номер владельца, чтение истории этого звонка.
Реальные JSON-ответы (обезличенные) сохранить в `tests/fixtures/pbx/`; парсер
исходов писать по ним: менеджер не взял / клиент не взял / соединились различаются
по полям истории (длительности/статусы плеч) — точные поля даст probe.
Голосовую отбивку настроить в кабинете АТС на этом же шаге (владелец записывает).

**Checkpoint:** тестовый звонок при владельце; исходы в фикстурах. Тесты парсера → PASS. Commit.

---

## Фаза 4 — движок, сообщения, сборка

### Задача 8: Движок

**Files:** Create: `adminbot/autocall/engine.py`; Test: `tests/test_autocall_engine.py`

`AutocallEngine(pbx, amo, store, notify_manager, notify_owner, dry_run)`.
`process_due(chain, now)`:
- `queued` и пора → **сначала** `store.update(status="calling", called_at=now, attempts_total+1)`,
  **потом** `pbx.call_now(...)` (намерение до звонка: упали между — после рестарта
  не позвоним дважды, цепочка со статусом calling без исхода уйдёт в UNKNOWN по таймауту);
- `calling` → `pbx.call_outcome(...)`; None и прошло <5 мин → ждём; ≥5 мин → UNKNOWN;
- исход → `chain.advance(...)` → исполнить эффекты: Retry → `next_action_at`
  (через `window.next_call_moment` — повторы уважают окно), `MoveLeadNoContact` →
  `amo.move_lead(lead_id, PIPELINE_PRIMARY, PRIM_STAGE_NO_CONTACT)`,
  уведомления через колбэки; каждый шаг — в `autocall_actions`.
- dry-run: АТС не трогаем (MemoryPbx), в амо не пишем (dry_run-клиент), владельцу —
  «позвонил бы». Ошибка АТС/амо → status='error', last_error, повтор следующим тиком.

Тесты с фейками: полный счастливый путь; обе ветки неудач до конца; UNKNOWN по
таймауту; падение между "calling" и call_now не даёт второго звонка; dry-run
не зовёт ни АТС, ни амо. Commit.

### Задача 9: Сообщения — CHECKPOINT владельца (тексты)

**Files:** Create: `adminbot/tg/autocall_cards.py`; Modify: `adminbot/main.py` (фабрики-отправители); Test: `tests/test_autocall_cards.py`

Тексты (черновики — согласовать с владельцем ДО боевого запуска):
- менеджеру `client_retry_10`: «Попытка звонка не удалась, повтор через 10 минут» + ссылка на сделку;
- менеджеру `no_contact_final`: «Клиент не ответил дважды. Сделка … перенесена в „Не было 1-го касания“» + ссылка;
- менеджеру `manager_unreachable`: «Не дозвонились до менеджера по заявке …» + ссылка;
- владельцу: репетиция («позвонил бы…»), отчёт о состоявшемся соединении со ссылкой
  (стиль недели наблюдения), алерт при 30 мин недоступности АТС.
Полный телефон и дата — в сообщениях, маска — в логах (правила проекта).
Отправка менеджеру: отдельный aiogram `Bot(worker_tg_token)` **только для отправки**
(сессия из `adminbot/tg/session.py` — те же прямые IP Telegram); токен пуст →
сообщения владельцу с пометкой «(менеджеру не отправлено: транспорт не настроен)».
Отметка об отправке — по образцу `done_msg_id` gcal: сообщение «отправлено» только
после того, как Telegram его принял. Commit.

### Задача 10: Наблюдатель

**Files:** Create: `adminbot/autocall/watcher.py`; Test: `tests/test_autocall_watcher.py`

По образцу `CalendarWatcher.tick`: (1) курсор из store (первый боевой запуск —
`now`: заявки до включения — владельца); (2) `find_leads_created_since` →
`is_site_lead` → новые цепочки (`create` идемпотентен по PK, телефон нашёлся —
`queued` c `next_call_moment`, нет телефона → сразу вопрос владельцу);
(3) `store.due(now)` → `engine.process_due` — одна сбойная цепочка не роняет
проход (try/except, как `_handle` календаря); (4) курсор сохранить в конце
прохода. Активные цепочки (`calling`, близкий `next_action_at`) → быстрый
следующий тик (аналог QUICK_RETRY_SEC). `run_forever` — как у всех. Тесты:
идемпотентность по PK; сделка без тега не берётся; курсор двигается только после
прохода; одна ошибка не роняет тик. Commit.

### Задача 11: Сборка в main + выключатели

**Files:** Modify: `adminbot/main.py` (`_build_autocall` по образцу `_build_calendar` + задача в фоне), `adminbot/tg/bot.py` (/status — строка autocall), `deploy/update.sh` (флаги `--autocall-on/--autocall-off/--autocall-live/--autocall-rehearsal/--autocall-check`), `.env.example`; Tests: `tests/test_main_resilience.py`, `tests/test_tg_commands.py`

Функция выключена / PBX-переменные пусты → autocall просто не поднимается,
остальные фичи работают (образец — деградация gcal без ключа Google). dry-run →
MemoryStore + MemoryPbx + dry-run-клиент амо. `--autocall-check` — диагностика
без рестарта сервиса (правило: диагностика не рестартует). Commit.

### Задача 12: Деплой-раннбук, стенд, репетиция, боевой старт

**Files:** Modify: `docs/deploy.md` (раздел autocall: `~/.pbx.env`, `WORKER_TG_TOKEN`/`MANAGER_TG_CHAT_ID`, порядок включения, §9 — двухшаговый деплой rsync!)

Порядок (по образцу gcal):
1. Деплой кода выключенным; миграция 007 (проверить в выводе шага 4 — ловушка §9).
2. **Стенд**: `--autocall-check` + тестовая цепочка, где клиент = номер владельца:
   отбивка звучит, соединение происходит, исходы определяются.
3. **Репетиция 1–2 дня**: `--autocall-on` (dry-run по умолчанию): по каждой живой
   заявке владельцу — «позвонил бы в HH:MM, менеджеру цепочка X»; сверка руками.
4. Согласовать финальные тексты сообщений (Задача 9) — решение владельца.
5. **Боевой старт**: `--autocall-live`; неделя наблюдения — каждая цепочка
   отчитывается владельцу. Выключение одной командой `--autocall-off`.

**Checkpoint:** каждый шаг 2–5 — только с владельцем.
