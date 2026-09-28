# ТЗ — задача «Повторный заказ» по оценке клиента (админ-бот)

> Исполнителям: одна задача за раз, по номеру из промпта. Читать это ТЗ целиком, делать только свою задачу.
> После каждого законченного куска — коммит; в конце задачи — отметка «Выполнено» под ней (коммит + строка итога).

**Цель.** Когда клиент ставит оценку заказу химчистки в рабочем боте, админ-бот сам разбирает задачу amoCRM
«Повторный заказ» в сделке: пятёрка — закрывает её; ниже пятёрки — пишет комментарий, ставит задачу «Связаться»
и закрывает «Повторный заказ». Нет оценки — ничего.

**Архитектура.** Новый фоновый цикл админ-бота `adminbot/feedback/` по образцу откликов на промо
(`adminbot/promo_callback/`): источник (читает `public.orders` и связки `adminbot.amo_links`), своё состояние
(миграция 019, у репетиции и боя свои строки), цикл раз в 15 минут, свой выключатель и репетиция. Старое разовое
правило «закрыть при проведении, если оценка уже есть» (решение №9 дизайна 2026-08-24) убирается.

**Стек.** Python 3.11, asyncio, asyncpg, aiohttp; тесты pytest (async). Всё в `raketa-admin-bot/`.

## Решения владельца 28.09

1. Запрос оценки остаётся как есть (рабочий бот, только химчистка). Задачу «Повторный заказ» ставит сама amoCRM при
   переводе сделки в «Успешно реализовано» — так и остаётся.
2. **Оценка 5** → робот закрывает «Повторный заказ».
3. **Оценка 1–4** → админов уведомляет рабочий бот (уже работает, не трогать); робот пишет комментарий в сделку,
   ставит задачу «Связаться» и закрывает «Повторный заказ».
4. **Нет оценки** → робот ничего не делает.
5. «Связаться» — на ответственного по сделке; срок — плюс сутки от момента постановки; ставится при первом проходе,
   увидевшем оценку (срочности нет, админ видит оценку в боте сразу, задача — для контроля).
6. Если «Повторный заказ» уже закрыт руками: при 5 — ничего; при 1–4 — всё равно комментарий и «Связаться».
7. Владельцу в личку по работе — ничего.
8. Какие оценки берутся: пришедшие после включения режима **плюс** старые, по которым «Повторный заказ» ещё открыт.
   Старые, где задача уже закрыта (или её нет), — не трогать.
9. Тексты (утверждены на примере оценки 3, заказ №605):
   - комментарий в сделке:
     ```
     🤖 Клиент оценил заказ №605 на 3 в боте (04.09 12:46).
     Ответ клиента: «3».
     Поставил задачу «Связаться», «Повторный заказ» закрыл.
     ```
   - задача «Связаться»: `Клиент оценил заказ №605 на 3 — узнать, что не так.`
   - результат закрытия «Повторного заказа»: при 5 — `🤖 Клиент оценил заказ в боте на 5.`;
     при 1–4 — `🤖 Клиент оценил заказ в боте на 3, поставлена задача «Связаться».`

## Принято координатором, владелец утвердил дизайном 28.09

1. Проход раз в 15 минут.
2. «Повторного заказа» в сделке ещё нет (amoCRM ставит его при переходе в «Успешно реализовано», при оплате по счёту
   это позже) — робот ждёт и смотрит на каждом проходе до 30 дней после оценки, потом молча отступается.
3. Сбой CRM — повтор на следующих проходах; три неудачи подряд — одно письмо владельцу «не смог, сделай руками».
4. Правило №9 (разовая проверка при проведении) убирается.
5. Уборки не входят (у `cleaning_orders` нет оценок).
6. Свой выключатель, по умолчанию выключен; сначала репетиция (в CRM не пишет, письмо владельцу «Репетиция: …»),
   потом бой.

## Принято координатором при написании ТЗ (доложить владельцу)

- **Третья строка комментария зависит от положения задачи.** Решение 5 требует ставить «Связаться» сразу, не дожидаясь
  «Повторного заказа», поэтому к моменту комментария задача бывает в трёх положениях:
  открыта → `Поставил задачу «Связаться», «Повторный заказ» закрыл.` (утверждённый текст);
  закрыта руками → `Поставил задачу «Связаться»; «Повторный заказ» уже был закрыт.`;
  ещё не появилась → `Поставил задачу «Связаться»; «Повторный заказ» закрою, когда он появится.`
- Письмо репетиции — только когда робот сделал бы хоть одну запись в CRM; ожидание и пропуск — без писем.
- За проход — не больше 20 заказов (первый проход разбирает накопленное частями, не нагружая CRM).

## Факты (источник — вывод команд 28.09)

- В CRM было 39 открытых задач типа «Повторный заказ» (id 2270746), все на сделках в «Успешно реализовано»;
  у 36 нашлась связка с заказом, и **все 36 — на сделке реализации** (`adminbot.amo_links.real_lead_id`), ни одной
  на первичной (запрос к `amo_links` по номерам сделок из выгрузки задач).
- По 11 задачам клиент уже оценил заказ; робот не закрыл ни одной: у 9 из 11 оценка пришла позже, чем связка стала
  `done` (запрос `amo_links.updated_at` против `orders.rating_replied_at`); в чек-листах нет шага `close_feedback_task`.
- Разовое правило: `adminbot/sync/checklist.py:66` (`"close_feedback_task": lambda ctx: ctx.has_rating`),
  `adminbot/sync/engine.py` (`StepContext(has_rating=order.rating_score is not None)` в `_run_checklist`,
  `_step_close_feedback_task`), `adminbot/amo/ids.py:100` (`TASK_TYPE_FEEDBACK = 2270746`).
- Типы задач amoCRM (`recon/data/amocrm_account.json` в корне монорепо): `1` — «Связаться», `2270746` — «Повторный заказ».
- Оценка в базе рабочего бота: `public.orders.rating_score` (smallint), `rating_comment` (сырой текст ответа,
  например «3» или «5 спасибо»), `rating_replied_at` (timestamptz), `rating_requested_at`. Пишет
  `bot.py:_process_rating_response`; ответ засчитывается в пределах 30 дней от заказа (`_select_pending_rating_order`).
- Клиент amoCRM админ-бота (`adminbot/amo/client.py`): есть `get_lead` (в ответе `responsible_user_id`),
  `get_lead_tasks` (только **открытые** задачи сделки), `complete_task(task_id, result_text)`, `add_note`;
  **нет** постановки задачи и нет чтения задач с закрытыми. Запись — через `_perform` (в репетиции не пишет).
- Образец цикла: `adminbot/promo_callback/sync.py` + `store.py`, миграция `migrations/018_promo_callback.sql`,
  сборка `adminbot/main.py:_build_promo_callback`, поле `App.promo_callback`, запуск в `App.run`.
- Образец выключателя в обёртке: `deploy/update.sh` — переменные (стр. ~78–79), ключи (~136–139), `set_flag`
  (~325–326), отчёт (~379–381), подсказка ключей в шапке (~24–26). Настройки: `adminbot/config.py`
  (`promo_callback_enabled`, `promo_callback_dry_run`, `_flag(...)`).
- Следующий номер миграции админ-бота — **019** (`ls migrations`: последняя `018_promo_callback.sql`).
- Пулы: `bot_pool` — чтение `public` (только SELECT), `own_pool` — схема `adminbot`.

## Правила для исполнителей

- Рабочая папка `raketa-admin-bot/`. Тесты: `TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/adminbot_test
  rtk proxy .venv/bin/python -m pytest <файл> -q --tb=short` — по своим файлам во время работы; полный прогон
  (`... -m pytest -q`) один раз перед финальным коммитом задачи. Без `TEST_DB_DSN` тесты с базой молча пропускаются.
- TDD: сначала тест, увидеть падение, потом код.
- **Запрещено:** `git stash`, `git worktree`, `git add -A`/`git add .`, правка чужих файлов вне своей задачи,
  `ssh`, выкат, запись в схему `public`, реальные телефоны и имена в тестах и коммитах.
- Телефоны в журнале — только через `adminbot.phone.mask`.
- Коммит после каждого законченного куска; `git status --short` перед коммитом; добавлять только свои файлы.
- Файлы длиннее 500 строк, которые правишь (`adminbot/main.py`, `adminbot/sync/engine.py`, `adminbot/db.py`), —
  обнови их карту `<файл>.map.md` (номера строк заново, новые функции — строкой).
- Место правки ищи сам по всему проекту (`grep -rn`), а не по списку из ТЗ: список в ТЗ — отправная точка, не полный.

---

### Задача 1. Клиент amoCRM: задачи сделки по типу и постановка задачи

**Файлы:** `adminbot/amo/client.py`, `adminbot/amo/ids.py`, `tests/fakes.py`, тесты клиента (найди файл, где
проверяются `complete_task`/`add_note`; нет — создай `tests/test_amo_client_tasks.py`), двойник в `tests/fakes.py`.

**Производит (сигнатуры для задач 3–4):**
```python
# adminbot/amo/ids.py
TASK_TYPE_CONTACT = 1            # «Связаться» — стандартный тип amoCRM

# adminbot/amo/client.py (методы AmoClient)
async def get_lead_tasks_of_type(self, lead_id: int, task_type_id: int) -> list[dict]:
    """Все задачи сделки этого типа — и открытые, и закрытые (поле is_completed)."""

async def create_task(self, lead_id: int, *, task_type_id: int, text: str,
                      complete_till: int, responsible_user_id: Optional[int] = None) -> Intent:
    """Поставить задачу на сделку. complete_till — unix-время. Intent(action="create_task",
    entity="task"); в бою entity_id — номер созданной задачи (из ответа, ключ "tasks")."""
```

- `get_lead_tasks_of_type`: `GET /api/v4/tasks`, фильтры `filter[entity_type]=leads`, `filter[entity_id]=<lead>`,
  `filter[task_type]=<type>`, **без** `filter[is_completed]`; через `get_all`, как `get_lead_tasks`.
- `create_task`: `POST /api/v4/tasks`, тело — список из одной задачи
  `{"task_type_id", "text", "complete_till", "entity_id", "entity_type": "leads"[, "responsible_user_id"]}`;
  через `_perform(..., result_key="tasks")` — как `create_lead`/`add_note` (посмотри, как они передают список).
- `FakeAmo`: `get_lead_tasks_of_type` отдаёт задачи сделки нужного типа **вместе с закрытыми** (у закрытой
  `is_completed=True`; как двойник сейчас помечает закрытие — разберись в `complete_task`/`task_results`);
  `create_task` пишет вызов в `calls`, заводит задачу в `self.tasks[lead_id]` (новый id из `_next_id`), уважает
  `fail_on` и `dry_run` так же, как соседние методы записи.
- Тесты: фильтры запроса (закрытые не отсекаются), тело `create_task` (тип, срок, сделка, ответственный; без
  ответственного — ключа нет), номер созданной задачи из ответа, репетиция не делает запроса.

- [x] Выполнено. Коммиты: 8a5f22d (клиент: `get_lead_tasks_of_type`, `create_task`,
  `ids.TASK_TYPE_CONTACT`, тесты `test_amo_client.py`/`test_amo_write.py`), 041a076
  (двойник `FakeAmo` + `tests/test_fakes_amo_tasks.py`). Тесты: TDD (падение до кода
  подтверждено), полный прогон `pytest -q` — `1126 passed, 9 skipped` (пропуски —
  отчёты по коврам без `CARPET_FIXTURES_DIR`, к задаче не относятся).

---

### Задача 2. Миграция 019, источник оценок и хранилище состояния

**Файлы:** создать `migrations/019_feedback_tasks.sql`, `adminbot/feedback/__init__.py`,
`adminbot/feedback/models.py`, `adminbot/feedback/store.py`; тест `tests/test_feedback_store.py` (на настоящей
базе, как `tests/test_promo_callback_store.py` — возьми оттуда приём с миграцией и `public.orders`).

**Производит:**
```python
# adminbot/feedback/models.py
MODE_LIVE = "live"; MODE_REHEARSAL = "rehearsal"
STATUS_NEW = "new"                # увидели, ещё не закончили (ждём задачу или повторяем после сбоя)
STATUS_CONTACT_SET = "contact_set"  # 1–4: комментарий и «Связаться» есть, «Повторный заказ» ещё не закрыт
STATUS_DONE = "done"              # всё сделано
STATUS_SKIPPED = "skipped"        # делать нечего (решения 6 и 8, 30 дней без задачи)
STATUS_DRY_RUN = "dry_run"        # репетиция отчиталась
STATUS_FAILED = "failed"          # 3 сбоя подряд, владельцу ушло письмо
OPEN_STATUSES = (STATUS_NEW, STATUS_CONTACT_SET)

@dataclass(frozen=True)
class RatedOrder:
    order_id: int
    lead_id: int                  # сделка реализации: adminbot.amo_links.real_lead_id
    score: int                    # orders.rating_score
    comment: Optional[str]        # orders.rating_comment — сырой ответ клиента
    replied_at: datetime          # orders.rating_replied_at (aware)

@dataclass(frozen=True)
class FeedbackState:
    order_id: int
    mode: str
    status: str = STATUS_NEW
    contact_task_id: Optional[int] = None   # «Связаться» поставлена (номер; в репетиции — 0)
    note_added: bool = False                # комментарий записан
    attempts: int = 0
    last_error: Optional[str] = None

# adminbot/feedback/store.py
class PgFeedbackSource:           # __init__(self, bot_pool, own_pool)
    async def rated_orders(self) -> list[RatedOrder]: ...
class PgFeedbackStore:            # __init__(self, own_pool)
    async def started_at(self, mode: str) -> Optional[datetime]: ...
    async def save_started_at(self, mode: str, when: datetime) -> None: ...   # только первый раз
    async def states(self, mode: str) -> dict[int, FeedbackState]: ...
    async def register(self, mode: str, order_ids: Sequence[int]) -> None: ...  # уже заведённые не трогать
    async def update(self, order_id: int, mode: str, **fields: Any) -> None: ... # только поля из белого списка
```

- Миграция 019 (идемпотентна, стиль и шапка — как 018): `adminbot.feedback_state` — ключ `(order_id, mode)`,
  `mode` CHECK `('rehearsal','live')`, `status` CHECK по списку выше, `contact_task_id bigint`,
  `note_added boolean NOT NULL DEFAULT false`, `attempts int NOT NULL DEFAULT 0`, `last_error text`,
  `created_at`/`updated_at`; индекс `(mode, status)`. `adminbot.feedback_cursor` — `mode` PK, `started_at timestamptz
  NOT NULL`, `updated_at`: момент первого прохода режима (решение 8, «после включения»).
- `rated_orders`: связки `adminbot.amo_links` со `status = 'done'` и непустым `real_lead_id` (через `own_pool`) +
  заказы `public.orders` с непустыми `rating_score` и `rating_replied_at` (через `bot_pool`, только SELECT),
  соединение в Python по `order_id`. Уборки (`adminbot.cleaning_links`) не брать.
- `update`: белый список полей `status, contact_task_id, note_added, attempts, last_error` (как в
  `promo_callback/store.py`).
- Тесты: связка не `done` / без `real_lead_id` / без оценки — не попадает; уборка не попадает; `started_at`
  ставится один раз и второй вызов его не двигает; режимы не видят строк друг друга; `update` не пускает чужое поле;
  миграция применяется дважды без ошибки.

- [x] Выполнено. Коммиты: 18217f1 (миграция 019: `adminbot.feedback_state`,
  `adminbot.feedback_cursor`), 9146d59 (`adminbot/feedback/models.py`,
  `adminbot/feedback/store.py`, тест `tests/test_feedback_store.py`). Тесты: TDD
  (падение `ModuleNotFoundError` до кода подтверждено), файл — 5 passed на
  настоящей базе (не skipped); полный прогон `pytest -q` — `1131 passed, 9 skipped`
  (пропуски — отчёты по коврам без `CARPET_FIXTURES_DIR`, к задаче не относятся).

---

### Задача 3. Цикл «оценка → задачи в CRM» и тексты

**Файлы:** создать `adminbot/feedback/sync.py`, `adminbot/tg/feedback_cards.py`; тест `tests/test_feedback_sync.py`
(источник и хранилище в памяти, CRM — `FakeAmo`, как в `tests/test_promo_callback.py`).

**Потребляет:** задачу 1 (`get_lead_tasks_of_type`, `create_task`, `ids.TASK_TYPE_CONTACT`,
`ids.TASK_TYPE_FEEDBACK`, существующие `get_lead`, `complete_task`, `add_note`) и задачу 2 (модели, протоколы
источника и хранилища — объяви их `Protocol` в `sync.py`, как в `promo_callback/sync.py`).

**Производит:**
```python
class FeedbackSync:
    def __init__(self, *, source, store, amo, dry_run: bool = True,
                 on_rehearsal: Optional[Callable[[RatedOrder, list[str]], Awaitable[Any]]] = None,
                 on_failure: Optional[Callable[[RatedOrder, str], Awaitable[Any]]] = None,
                 poll_interval_sec: int = 900, now: Callable[[], datetime] = ...,
                 sleep=asyncio.sleep) -> None: ...
    async def tick(self) -> int: ...            # сколько заказов доведено до конечного статуса
    async def run_forever(self, stop=None) -> None: ...

# adminbot/tg/feedback_cards.py — только строители текста
def note_text(order: RatedOrder, *, feedback_task: str) -> str   # feedback_task: "closed_now" | "closed_by_hand" | "not_yet"
def contact_task_text(order: RatedOrder) -> str
def feedback_result_text(score: int) -> str
def rehearsal_text(order: RatedOrder, actions: list[str], *, base_url: str) -> str
def failure_text(order: RatedOrder, error: str, *, base_url: str) -> str
```

**Проход `tick()`** (режим = `rehearsal` при `dry_run`, иначе `live`):
1. `started = store.started_at(mode)`; нет — `save_started_at(mode, now)` и взять `now`.
2. `orders = source.rated_orders()`, `states = store.states(mode)`; в работу — заказы без строки или в
   `OPEN_STATUSES`; новые — `register`. Не больше **20** за проход (по возрастанию `replied_at`).
3. Для каждого: `tasks = amo.get_lead_tasks_of_type(order.lead_id, TASK_TYPE_FEEDBACK)`;
   `open_ = [t for t in tasks if not t.get("is_completed")]`; `closed_by_hand = bool(tasks) and not open_`;
   `is_new = order.replied_at >= started`; `expired = now > order.replied_at + 30 дней`.
4. **Старая оценка** (`not is_new`) без открытой задачи → `skipped` (решение 8), без записей.
5. **Оценка 5:** есть `open_` → закрыть каждую с `feedback_result_text(5)` → `done`;
   `closed_by_hand` → `skipped`; задачи нет → ждать (статус не менять), при `expired` → `skipped`.
6. **Оценка 1–4:** если `contact_task_id is None` — `get_lead` → `responsible_user_id`; `create_task(lead_id,
   task_type_id=TASK_TYPE_CONTACT, text=contact_task_text(order), complete_till=int(now + 24 ч),
   responsible_user_id=…)`; **сразу** сохранить `contact_task_id`. Если `not note_added` — `add_note(lead_id,
   note_text(order, feedback_task=…))` (положение задачи на этот момент: `open_` → `closed_now`,
   `closed_by_hand` → `closed_by_hand`, нет → `not_yet`); сразу `note_added=True`. Затем: есть `open_` → закрыть с
   `feedback_result_text(score)` → `done`; `closed_by_hand` → `done`; нет задачи → `contact_set` (дальше ждём,
   при `expired` → `done` без закрытия).
7. **Репетиция:** та же логика, CRM — клиент репетиции (записи — пустые `Intent`). Вместо смены статусов на
   `done/contact_set` — `dry_run` и одно письмо `on_rehearsal(order, actions)`, если была хоть одна запись
   (`actions` — человеческие строки: «закрыл бы «Повторный заказ»», «поставил бы «Связаться» на …»,
   «написал бы комментарий»). Ожидание и `skipped` — без писем. `contact_task_id` в репетиции — `0`.
8. **Сбой** (исключение CRM/базы на заказе): `attempts + 1`, `last_error`; третий подряд → `failed` +
   `on_failure(order, error)`; успех обнуляет `attempts`. Сбой одного заказа не роняет проход; сбой письма — тоже
   (как `_report_*` в `promo_callback/sync.py`).
9. Журнал: по одной строке `log.info` на итог заказа (номер заказа, сделка, оценка, итог), без телефонов.

**Тексты** — ровно решения 9 и «Принято при написании ТЗ»; время ответа — `replied_at` в МСК `%d.%m %H:%M`;
«Ответ клиента» — `comment.strip()`, пустой → цифра оценки. Письмо сбоя:
`⚠️ Не смог обработать оценку {score} по заказу №{order_id}, сделай руками.` / `Ошибка: {error}` / ссылка
`{base_url}/leads/detail/{lead_id}`. Письмо репетиции — через `adminbot.tg.cards.mark_rehearsal`, со ссылкой.

**Тесты (минимум):** 5 + открытая задача → закрыта с нужным текстом, `done`; 5 + закрыта руками → `skipped`, записей
нет; 5 + задачи нет → ждёт, после 30 дней → `skipped`; 3 + открытая → задача «Связаться» (тип 1, срок +24 ч,
ответственный из сделки), комментарий с «закрыл», закрытие с текстом «…на 3, поставлена…», `done`; 3 + закрыта руками
→ «Связаться» и комментарий «уже был закрыт», `done`; 3 + задачи нет → «Связаться», комментарий «закрою, когда
появится», `contact_set`; следующий проход с появившейся задачей → закрыта, `done`, второй «Связаться» и второго
комментария нет; сбой после постановки «Связаться» → повтор не ставит вторую; старая оценка (до `started_at`) с
открытой задачей — обрабатывается, без открытой — `skipped`; три сбоя → `failed` и одно письмо; репетиция: записей
в CRM нет, одно письмо, `dry_run`, у боя своя очередь; лимит 20 за проход; первый проход ставит `started_at`.

- [x] Выполнено. Коммит: 388045b (`adminbot/feedback/sync.py` — `FeedbackSync`,
  протоколы `FeedbackSource`/`FeedbackStore`; `adminbot/tg/feedback_cards.py` — тексты;
  `tests/test_feedback_sync.py` — источник/хранилище в памяти, CRM — `FakeAmo`). Тесты:
  TDD (падения из-за ошибок в самих тестах — `replied_at` по умолчанию раньше
  `started_at`, перепутанные переменные — исправлены, не код цикла); файл — 21 passed;
  полный прогон `pytest -q` — `1152 passed, 9 skipped` (пропуски — отчёты по коврам без
  `CARPET_FIXTURES_DIR`, к задаче не относятся).

---

### Задача 4. Подключение: настройки, сборка, обёртка, письма

**Файлы:** `adminbot/config.py`, `adminbot/main.py` (+ `main.py.map.md`), `deploy/update.sh`,
`tests/test_main_resilience.py` (и тест настроек, если он есть — найди, где проверяются `PROMO_CALLBACK_*`),
документ ключей обёртки (найди, где перечислены ключи `--promo-callback-*`, — `docs/deploy.md` админ-бота и/или
шапка `deploy/update.sh`).

- Настройки: `feedback_tasks_enabled: bool = False` ← `FEEDBACK_TASKS_ENABLED` (по умолчанию 0),
  `feedback_tasks_dry_run: bool = True` ← `FEEDBACK_TASKS_DRY_RUN` (по умолчанию 1) — рядом с `promo_callback_*`.
- `main.py`: `MAIL_FEEDBACK_REHEARSAL = "feedback_rehearsal"`, `MAIL_FEEDBACK_FAILED = "feedback_failed"`;
  `_build_feedback(settings, bot_pool, own_pool, mail, live_amo, rehearsal_amo)` по образцу
  `_build_promo_callback` (выключен → `None` и строка в журнал; `dry_run` выбирает клиента CRM; строка журнала о
  режиме); отправители `_make_feedback_rehearsal_sender(mail, amo_base_url)` и
  `_make_feedback_failure_sender(mail, amo_base_url)`; поле `App.feedback`, запуск в `App.run`
  (`name="feedback"`), передача в `App(...)` в `build_app`.
- `deploy/update.sh`: ключи `--feedback-on`, `--feedback-off`, `--feedback-live`, `--feedback-rehearsal` →
  `FEEDBACK_TASKS_ENABLED` / `FEEDBACK_TASKS_DRY_RUN` тем же `set_flag`; строка в отчёте «5. Выключатели»
  («задача «Повторный заказ» по оценке: ВКЛЮЧЕНА/выключена, режим …»); строки в подсказке ключей в шапке.
  Проверка синтаксиса: `bash -n deploy/update.sh`.
- Тесты: выключено → `None`; репетиция берёт клиента репетиции, бой — боевого; письма уходят в почту владельца
  нужного вида и с номером заказа.

- [x] Выполнено. Коммиты: `35e021c` (настройки `feedback_tasks_enabled`/`feedback_tasks_dry_run` +
  тесты), `094ac39` (`main.py` — `_build_feedback`, `MAIL_FEEDBACK_*`, отправители, `App.feedback`,
  запуск в `App.run`, сборка в `build_app`, тесты), `8d5a5f2` (карта `main.py.map.md`), `e66ad23`
  (`deploy/update.sh` — ключи `--feedback-on/off/live/rehearsal`, строка в отчёте, подсказка в
  шапке), `8f04166` (`docs/deploy.md` — раздел про включение). Решение координатора: письмо о
  сбое в репетиции помечается `mark_rehearsal` в самом отправителе (тест
  `test_feedback_failure_letter_marked_in_rehearsal`/`_not_marked_when_live`). Тесты: TDD
  (падения проверены на импорте несуществующих имён и `AttributeError` настроек до правки кода);
  полный прогон `pytest -q` — `1161 passed, 9 skipped` (пропуски — те же фикстуры ковров без
  `CARPET_FIXTURES_DIR`, к задаче не относятся).

---

### Задача 5. Убрать разовое правило №9

**Файлы:** `adminbot/sync/checklist.py`, `adminbot/sync/engine.py` (+ `engine.py.map.md`), тесты, которые его
проверяют (найди все: `grep -rn "close_feedback_task\|has_rating\|TASK_TYPE_FEEDBACK" adminbot tests`), и
`docs/plans/2026-08-24-amo-sync-design.md` (одна строка у решения №9: «заменено циклом `adminbot/feedback/`,
ТЗ docs/plans/2026-09-28-feedback-tasks.md в корне монорепо»).

- Убрать шаг `close_feedback_task`, поле `StepContext.has_rating` и `_step_close_feedback_task`. Проверить и
  закрепить тестом: связка, в сохранённом чек-листе которой уже есть ключ `close_feedback_task` (такие могли
  остаться в базе), проходит дальше без ошибки.
- `TASK_TYPE_FEEDBACK` остаётся (им пользуется задача 3). Проведение заказа **не** должно закрывать «Повторный
  заказ» ни при какой оценке — тест. Уборки (`tests/test_cleaning_sync.py`) — поведение не меняется, тесты зелёные.

- [x] Выполнено. Коммиты: `8d3f4eb` (код, карта, тесты), `35c8d15` (дизайн-документ). Полный прогон
  `raketa-admin-bot/`: `1152 passed, 9 skipped` (пропуски — отсутствие `CARPET_FIXTURES_DIR`, к задаче не относятся).

---

## Порядок выката (делает координатор, не исполнители)

1. Полный прогон тестов админ-бота; `rsync` → `sudo raketa-admin-bot-update` (миграция 019 применится сама; обёртка
   обновит себя — новые ключи со **второго** запуска) → `sudo raketa-admin-bot-update --report`.
2. `sudo raketa-admin-bot-update --feedback-on --feedback-rehearsal` → письма «Репетиция: …» владельцу по
   текущим оценкам; владелец сверяет.
3. `sudo raketa-admin-bot-update --feedback-live` — **по слову владельца**.
   Откат: `--feedback-off`.

## Чего НЕ делать

- Рабочий бот не трогать (запрос оценки, уведомления админам, `_process_rating_response`).
- Не писать в схему `public`; в CRM — только клиентом админ-бота.
- Не закрывать задачи других типов; не трогать сделки уборок и ковров.
- Не менять остальные функции админ-бота и их выключатели.

## Приёмка владельцем

1. Клиент ставит 5 → в течение ~15 мин «Повторный заказ» в сделке закрыт с результатом «🤖 Клиент оценил заказ в
   боте на 5.».
2. Клиент ставит 3 → комментарий в сделке, задача «Связаться» на ответственного со сроком +сутки, «Повторный заказ»
   закрыт с «…на 3, поставлена задача «Связаться».».
3. Клиент не ответил → задача висит, робот не трогает.
4. В личку по работе ничего не приходит; письмо — только при сбое.
