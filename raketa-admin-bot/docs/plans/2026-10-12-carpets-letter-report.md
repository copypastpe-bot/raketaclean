# Ковры: итог письма после доведения и «сделай руками» — ТЗ

> **Исполнителям:** одна задача — один исполнитель. Первым шагом сверить
> `git rev-parse --short HEAD` с SHA из промпта; не совпало — стоп и доклад.
> Запрещены `git stash`, `git worktree`, `git merge`, `git push`. После задачи —
> коммит своих файлов (никогда `git add -A`) и строка «Выполнено: <SHA>» под
> заголовком задачи в этом файле. Шаги отмечаются `- [x]`.

**Цель:** владелец получает по письму партнёра одно итоговое сообщение, когда
у каждой строки есть судьба, а заказ, который робот три часа не может сдвинуть,
приходит ему отдельным сообщением «сделайте руками».

**Устройство:** письмо при разборе запоминает свои заказы (`carpet_letters.partner_ids`)
и отказы среди них (`refused_ids`); в конце каждого прохода наблюдатель проверяет
незакрытые письма и шлёт итог, когда все их строки в статусах `done`/`waiting_owner`/
`manual`. Строка с ошибкой помнит, с какого момента стоит (`carpet_links.stuck_since`);
три часа без пройденного шага — статус `manual`, сообщение владельцу, робот строку
больше не трогает.

**Стек:** Python 3.11, asyncpg, aiogram, pytest (`asyncio_mode = auto`).

**Решения владельца:** разговор 12.10.2026 в сессии координатора (ниже — дословно,
что решено). Это ТЗ и есть спецификация.

## Решения владельца (12.10.2026)

1. Сообщение сразу после разбора письма («Проведено: 0 из 2 / В работе (ждут
   автосделку)») убрать. По письму — одно итоговое сообщение.
2. Итог уходит, когда каждая строка письма доведена (доставлено или отказ), ждёт
   ответа на карточку или отдана «сделать руками». Ответов на карточки итог не ждёт.
3. Отказы — отдельной строкой: «✖️ Отказ: N (закрыл как нереализованную)».
4. Повторы из месячного свода (заказ уже проведён по недельному отчёту) считаются
   вместе со всеми в «✅ Доставлено». Отдельной строки нет.
5. «Сделай руками»: строка три часа подряд не сдвинулась ни на шаг — отдельное
   сообщение владельцу, робот этот заказ больше не трогает. Любой пройденный шаг
   начинает отсчёт заново.
6. Без изменений: ожидание сейлзбота (40 минут → карточка-вопрос), карточки-вопросы,
   «письмо отложено», порог 100 строк.
7. Напоминания «отчёт не пришёл» нет — пропуски добивает месячный свод партнёра.
8. Отдельного выключателя нет: это правка уже работающих ковров (`CARPETS_ENABLED`).
9. Сообщение «сделай руками» — по образцу карточки-вопроса: полный номер, сумма, даты,
   ссылка на сделку. Обращение — на «вы», как во всех сообщениях бота.

## Global Constraints

- Только админ-бот (`raketa-admin-bot/`). Схема `public` — только чтение; своё
  состояние — схема `adminbot`.
- Тексты сообщений — дословно из задачи 2 (символы, кавычки «», тире —).
- Порог «сделай руками»: `MANUAL_AFTER_SEC = 3 * 3600`.
- Телефон: полный — только в сообщениях владельцу (`adminbot.phone.for_owner`);
  в журнале — номер заказа партнёра, телефон не писать.
- Миграция идемпотентна: обёртка обновления гоняет все миграции на каждом выкате.
- Тесты во время работы — точечно; полный прогон один раз перед коммитом задачи:
  `TEST_DB_DSN=postgresql://postgres@127.0.0.1:5432/adminbot_test rtk proxy .venv/bin/python -m pytest -q`
  (из `raketa-admin-bot/`). Через `rtk` — только `rtk proxy …`, иначе вывод врёт.
- Карты функций: кто менял файл из списка в `raketa-admin-bot/CLAUDE.md`, тот
  обновляет его `*.map.md` (номера строк и новые функции).

## Факты (источник — проверено координатором 12.10.2026)

- Итог уходит из `CarpetWatcher._handle_letter` сразу после первого прохода по строкам
  (`adminbot/carpets/watcher.py`, вызов `self.on_report(...)` в конце `_handle_letter`).
- Письмо «отчёт с 05.10 по 11.10»: заказы 45199 и 45257 — `create_lead` 16:57:30,
  итог 16:57:32 со строками в `waiting_salesbot`, оба `done` в 16:57:38
  (`adminbot.carpet_actions`, `adminbot.carpet_links`). Все строки с 31.08 — `done`.
- Ошибка амо даёт строке статус `error` (`CarpetEngine.process_row`, ветка `AmoError`);
  наблюдатель повторяет такие строки раз в минуту (`QUICK_RETRY_SEC`) и владельцу
  не пишет. Исключение не-`AmoError` оставляет письмо непрочитанным; повтор каждый
  проход, владельцу — ничего (`_handle_letter`, ветка `failures`).
- Строки письма не связаны с письмом: в `carpet_links` есть только `source_file`
  (миграция 004), в `carpet_letters` — `uid, subject, files, rows_total` и три колонки
  отложения (миграция 011). Последняя миграция — `020_feedback_kind.sql`.
- В репетиции (`dry_run`) наблюдатель письмо не помечает прочитанным и
  `remember_letter` не зовёт (тест `test_rehearsal_leaves_the_letter_unread`); хранилище
  в репетиции — `MemoryCarpetStore` (`main._build_carpets`).
- `OwnerMail.send` при сбое Telegram кладёт сообщение в долг и возвращает `None`,
  исключения не бросает (`adminbot/tg/outbox.py`).
- `/carpets` вызывает `watcher.tick()` и показывает снимок прохода через
  `carpet_report_text` (`adminbot/tg/bot.py`, `Commands.carpets`) — его не трогаем.
- Баг, найден при подготовке ТЗ (проба на `FakeAmo`): сбой амо в `find_leads_by_phone`
  ставит строке `error`; на повторе `process_row` пропускает выбор сделки
  (условие требует `status == "new"`) и зовёт `update_lead(None, …)`. Настоящий
  клиент шлёт `PATCH /api/v4/leads/None` → ошибка амо → строка застревает навсегда.
  Исправление — задача 4, только по решению владельца.

---

### Задача 1: память робота — миграция, хранилища, движок не трогает `manual`

Выполнено: bd43f57

**Файлы:**
- Создать: `raketa-admin-bot/migrations/021_carpet_letter_report.sql`
- Изменить: `raketa-admin-bot/adminbot/models.py` (`CarpetLink`)
- Изменить: `raketa-admin-bot/adminbot/db.py` (+ `db.py.map.md`)
- Изменить: `raketa-admin-bot/adminbot/carpets/store.py`
- Изменить: `raketa-admin-bot/adminbot/carpets/engine.py` (`process_row`)
- Тесты: `tests/test_db_schema.py`, `tests/test_carpet_engine.py`,
  создать `tests/test_carpet_letter_store.py`

**Интерфейсы (их ждут задачи 2–3):**
- `CarpetLink.stuck_since: Optional[datetime] = None`; новый статус строки `"manual"`.
- Хранилища (`MemoryCarpetStore`, `PgCarpetStore`):
  - `async track_letter(uid: str, subject: Optional[str], partner_ids: Sequence[int], refused_ids: Sequence[int]) -> None`
    — запомнить заказы письма; повторный вызов список не меняет;
  - `async letters_to_report() -> list[dict]` — словари с ключами
    `uid, subject, partner_ids: list[int], refused_ids: list[int]`, итог по которым ещё не ушёл;
  - `async mark_letter_reported(uid: str) -> None`;
  - `async links_by_ids(partner_ids: Sequence[int]) -> list[CarpetLink]`.
- `db`: `track_carpet_letter`, `carpet_letters_to_report`, `mark_carpet_letter_reported`,
  `fetch_carpet_links_by_ids`; `stuck_since` — в `_UPDATABLE_CARPET_FIELDS`.

- [x] **Шаг 1: падающие тесты базы** — в конец ковровых тестов `tests/test_db_schema.py`
  (импорты `datetime, timezone` там уже есть):

```python
async def test_letter_report_tracking(pool):
    """Итог письма: заказы запоминаются один раз, итог отмечается (ТЗ 2026-10-12)."""
    await db.track_carpet_letter(pool, "51", "отчёт с 05.10 по 11.10",
                                 [45199, 45257], [45257])
    await db.remember_letter(pool, "51", "отчёт с 05.10 по 11.10",
                             ["Договоры.xlsx"], rows_total=2)
    await db.track_carpet_letter(pool, "51", "другая тема", [1], [])   # повтор не меняет

    assert await db.letter_state(pool, "51") == "processed"
    assert await db.carpet_letters_to_report(pool) == [
        {"uid": "51", "subject": "отчёт с 05.10 по 11.10",
         "partner_ids": [45199, 45257], "refused_ids": [45257]}]

    await db.mark_carpet_letter_reported(pool, "51")
    assert await db.carpet_letters_to_report(pool) == []


async def test_remember_after_track_keeps_letter_details(pool):
    """`track_letter` идёт первым — `remember_letter` дописывает файлы и число строк."""
    await db.track_carpet_letter(pool, "52", "отчёт", [1], [])
    await db.remember_letter(pool, "52", "отчёт", ["Договоры.xlsx"], rows_total=1)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT files, rows_total FROM adminbot.carpet_letters WHERE uid = '52'")
    assert list(row["files"]) == ["Договоры.xlsx"] and row["rows_total"] == 1


async def test_letters_without_partner_ids_get_no_report(pool):
    """Письма до миграции 021 (без списка заказов) итога не получают."""
    await db.remember_letter(pool, "17", "отчёт с 17.08 по 23.08",
                             ["Договоры (11).xlsx"], rows_total=4)
    assert await db.carpet_letters_to_report(pool) == []


async def test_carpet_links_by_ids_and_stuck_since(pool):
    await db.create_carpet_link(pool, 1, "9601945325")
    await db.create_carpet_link(pool, 2, "9601945325")
    moment = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    await db.update_carpet_link(pool, 2, status="manual", stuck_since=moment)

    links = await db.fetch_carpet_links_by_ids(pool, [2, 3])
    assert [(link.partner_id, link.status, link.stuck_since) for link in links] == [
        (2, "manual", moment)]
    assert await db.fetch_carpet_links_by_ids(pool, []) == []
```

- [x] **Шаг 2: падающие тесты хранилища в памяти** — создать `tests/test_carpet_letter_store.py`:

```python
"""Хранилище ковров в памяти: заказы письма для итога (ТЗ 2026-10-12)."""

from adminbot.carpets.store import MemoryCarpetStore


async def test_memory_store_tracks_letter_report_once():
    store = MemoryCarpetStore()
    await store.track_letter("1", "отчёт", [10, 11], [11])
    await store.track_letter("1", "отчёт", [99], [])        # репетиция разбирает письмо каждый проход

    assert await store.letters_to_report() == [
        {"uid": "1", "subject": "отчёт", "partner_ids": [10, 11], "refused_ids": [11]}]

    await store.mark_letter_reported("1")
    await store.track_letter("1", "отчёт", [10, 11], [11])  # после итога письмо не воскресает
    assert await store.letters_to_report() == []


async def test_memory_store_links_by_ids():
    store = MemoryCarpetStore()
    await store.create(10, "9601945325")
    await store.create(11, "9601945325")

    links = await store.links_by_ids([11, 12])

    assert [link.partner_id for link in links] == [11]
```

- [x] **Шаг 3: падающий тест движка** — в `tests/test_carpet_engine.py`, после
  `test_second_run_of_the_same_row_does_nothing`:

```python
async def test_row_handed_to_owner_is_left_alone():
    """«Сделай руками»: заказ отдан владельцу, робот его больше не трогает (ТЗ 2026-10-12)."""
    amo, store = FakeAmo(), MemoryCarpetStore()
    open_carpet_lead(amo)
    await store.create(44426, "9601945325")
    await store.update(44426, status="manual")

    link = await make_engine(amo, store).process_row(row())

    assert link.status == "manual"
    assert amo.calls == []
```

- [x] **Шаг 4: прогнать — должны упасть**

  `TEST_DB_DSN=… rtk proxy .venv/bin/python -m pytest -q tests/test_db_schema.py tests/test_carpet_letter_store.py tests/test_carpet_engine.py 2>&1 | tail -15`
  Ожидание: новые тесты падают (нет функций/колонок/метода, движок трогает `manual`).

- [x] **Шаг 5: миграция** `migrations/021_carpet_letter_report.sql`:

```sql
-- Ковры: итог письма — когда строки доведены; «сделайте руками» после трёх часов.
-- Применяется обёрткой обновления сама (шаг «4. Миграции» в deploy/update.sh),
-- а она гоняет ВСЕ миграции на каждом выкате — поэтому каждый шаг здесь
-- безопасен для повторного запуска.
--
-- Зачем (решение владельца 2026-10-12). Итог письма уходил сразу после первого
-- прохода, пока сейлзбот ещё заводил сделки, и показывал «Проведено: 0 из 2»,
-- хотя через секунды всё было проведено. Теперь итог уходит, когда у каждой
-- строки письма есть судьба, — для этого письмо помнит свои заказы и отметку
-- «итог отправлен». Заказ, который три часа не может сдвинуться, робот отдаёт
-- владельцу — для этого строка помнит, с какого момента она стоит.
--
--   carpet_letters.partner_ids IS NULL       — письмо до этой миграции: итога не будет;
--   partner_ids NOT NULL, reported_at NULL   — итог ещё не отправлен;
--   reported_at NOT NULL                     — итог отправлен.
--   carpet_links.status = 'manual'           — отдан владельцу, робот не трогает.

ALTER TABLE adminbot.carpet_letters
    ADD COLUMN IF NOT EXISTS partner_ids bigint[],     -- заказы письма (номера партнёра)
    ADD COLUMN IF NOT EXISTS refused_ids bigint[],     -- из них отказы — своя строка итога
    ADD COLUMN IF NOT EXISTS reported_at timestamptz;  -- когда итог ушёл владельцу

ALTER TABLE adminbot.carpet_links
    ADD COLUMN IF NOT EXISTS stuck_since timestamptz;  -- с какого момента строка не сдвигается
```

- [x] **Шаг 6: модель** — `adminbot/models.py`, `CarpetLink`: в комментарии к `status`
  дописать `|manual`; после `source_file` добавить:

```python
    # С какого момента строка не может сдвинуться (ошибка CRM, сбой разбора). Три часа
    # подряд — робот отдаёт заказ владельцу, статус `manual` (ТЗ 2026-10-12).
    stuck_since: Optional[datetime] = None
```

  Найди все места, где `CarpetLink(...)` создаётся позиционными аргументами (поиском
  по репозиторию админ-бота); если такие есть — порядок полей после `source_file`
  им важен, проверь каждое.

- [x] **Шаг 7: база** — `adminbot/db.py`:
  - `_UPDATABLE_CARPET_FIELDS`: добавить `"stuck_since"`;
  - `_carpet_from_row`: `stuck_since=row["stuck_since"],` (перед `created_at`);
  - `remember_letter`: в `ON CONFLICT (uid) DO UPDATE SET` дописать
    `subject = EXCLUDED.subject, files = EXCLUDED.files, rows_total = EXCLUDED.rows_total,`
    — строку письма теперь первым заводит `track_carpet_letter` (порядок — задача 3);
    в докстринг одну фразу об этом;
  - после `held_letters` добавить:

```python
async def track_carpet_letter(own_pool: asyncpg.Pool, uid: str, subject: Optional[str],
                              partner_ids: Sequence[int], refused_ids: Sequence[int]) -> None:
    """Запомнить заказы письма для итога владельцу (ТЗ 2026-10-12).

    Зовётся до `remember_letter`: сбой между ними оставит письмо неразобранным,
    и следующий проход повторит оба шага, а не потеряет итог. Повторный вызов
    список не меняет.
    """
    async with own_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO adminbot.carpet_letters (uid, subject, partner_ids, refused_ids)
            VALUES ($1, $2, $3::bigint[], $4::bigint[])
            ON CONFLICT (uid) DO UPDATE
                SET partner_ids = EXCLUDED.partner_ids, refused_ids = EXCLUDED.refused_ids
                WHERE adminbot.carpet_letters.partner_ids IS NULL
            """,
            uid, subject, list(partner_ids), list(refused_ids),
        )


async def carpet_letters_to_report(own_pool: asyncpg.Pool) -> list[dict]:
    """Письма, итог по которым ещё не ушёл, — старые первыми."""
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT uid, subject, partner_ids, refused_ids FROM adminbot.carpet_letters
            WHERE partner_ids IS NOT NULL AND reported_at IS NULL
            ORDER BY processed_at, uid
            """)
    return [{"uid": row["uid"], "subject": row["subject"],
             "partner_ids": list(row["partner_ids"]),
             "refused_ids": list(row["refused_ids"] or [])} for row in rows]


async def mark_carpet_letter_reported(own_pool: asyncpg.Pool, uid: str) -> None:
    async with own_pool.acquire() as conn:
        await conn.execute(
            "UPDATE adminbot.carpet_letters SET reported_at = now() WHERE uid = $1", uid)


async def fetch_carpet_links_by_ids(own_pool: asyncpg.Pool,
                                    partner_ids: Sequence[int]) -> list[CarpetLink]:
    """Заказы партнёра по номерам — для итога письма."""
    if not partner_ids:
        return []
    async with own_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM adminbot.carpet_links WHERE partner_id = ANY($1::bigint[]) "
            "ORDER BY partner_id",
            list(partner_ids),
        )
    return [_carpet_from_row(row) for row in rows]
```

- [x] **Шаг 8: хранилища** — `adminbot/carpets/store.py`.
  `MemoryCarpetStore.__init__`: `self.reports: dict[str, dict] = {}   # итог письма: uid → заказы и отметка`.
  В раздел «что нужно наблюдателю почты» обоих классов:

```python
    # MemoryCarpetStore
    async def track_letter(self, uid: str, subject: Optional[str],
                           partner_ids, refused_ids) -> None:
        self.reports.setdefault(uid, {"uid": uid, "subject": subject,
                                      "partner_ids": list(partner_ids),
                                      "refused_ids": list(refused_ids), "reported": False})

    async def letters_to_report(self) -> list[dict]:
        return [{key: value for key, value in item.items() if key != "reported"}
                for item in self.reports.values() if not item["reported"]]

    async def mark_letter_reported(self, uid: str) -> None:
        if uid in self.reports:
            self.reports[uid]["reported"] = True

    async def links_by_ids(self, partner_ids) -> list[CarpetLink]:
        return [self.links[pid] for pid in partner_ids if pid in self.links]
```

```python
    # PgCarpetStore
    async def track_letter(self, uid: str, subject: Optional[str],
                           partner_ids, refused_ids) -> None:
        await db.track_carpet_letter(self._pool, uid, subject, partner_ids, refused_ids)

    async def letters_to_report(self) -> list[dict]:
        return await db.carpet_letters_to_report(self._pool)

    async def mark_letter_reported(self, uid: str) -> None:
        await db.mark_carpet_letter_reported(self._pool, uid)

    async def links_by_ids(self, partner_ids) -> list[CarpetLink]:
        return await db.fetch_carpet_links_by_ids(self._pool, partner_ids)
```

- [x] **Шаг 9: движок** — `adminbot/carpets/engine.py`, `process_row`:

```python
        if link.status in ("done", "waiting_owner", "manual"):
            return link                    # разобрано, ждём ответа или отдано владельцу
```

- [x] **Шаг 10: прогнать точечно** — тесты шага 4 зелёные.
- [x] **Шаг 11: карта** `adminbot/db.py.map.md` — четыре новые функции, сдвиг номеров.
- [x] **Шаг 12: полный прогон и коммит** — полный прогон админ-бота зелёный; коммит:
  миграция, `models.py`, `db.py`, `db.py.map.md`, `store.py`, `engine.py`, три файла тестов.
  Сообщение: `Ковры: письмо помнит свои заказы для итога, строка — когда застряла (ТЗ 2026-10-12, задача 1)`.

---

### Задача 2: тексты итога и «сделайте руками»

Выполнено: 3c84f9b

**Файлы:**
- Изменить: `raketa-admin-bot/adminbot/carpets/watcher.py` (константа и класс итога, без логики)
- Изменить: `raketa-admin-bot/adminbot/tg/cards.py` (+ `cards.py.map.md`)
- Тесты: `tests/test_cards.py`

**Интерфейсы:**
- Производит: `watcher.MANUAL_AFTER_SEC = 3 * 3600`; `watcher.CarpetLetterSummary`
  (поля `uid, subject, delivered, refused, waiting_owner, manual`, свойство `clean`);
  `cards.carpet_letter_text(summary, *, dry_run=False) -> str`;
  `cards.carpet_manual_text(row, link, *, hours: int, base_url: Optional[str] = None, dry_run=False) -> str`.
- `carpet_report_text` остаётся как есть — им пользуется `/carpets` (снимок прохода);
  в докстринг: «Снимок одного прохода — для команды /carpets».

- [x] **Шаг 1: падающие тесты** — в `tests/test_cards.py` рядом с ковровыми:

```python
def test_carpet_letter_text_when_all_is_done():
    """Итог письма, когда делать ничего не надо (решение владельца 12.10)."""
    from adminbot.carpets.watcher import CarpetLetterSummary
    from adminbot.tg.cards import carpet_letter_text

    text = carpet_letter_text(CarpetLetterSummary(
        uid="1", subject="отчёт с 05.10 по 11.10", delivered=2, refused=1))

    assert text == ("🧶 Отчёт партнёра «отчёт с 05.10 по 11.10» проведён.\n"
                    "\n"
                    "✅ Доставлено: 2\n"
                    "✖️ Отказ: 1 (закрыл как нереализованную)\n"
                    "\n"
                    "Делать ничего не надо.")


def test_carpet_letter_text_when_owner_has_work():
    from adminbot.carpets.watcher import CarpetLetterSummary
    from adminbot.tg.cards import carpet_letter_text

    text = carpet_letter_text(CarpetLetterSummary(
        uid="1", subject="отчёт с 05.10 по 11.10", delivered=1, waiting_owner=1, manual=1))

    assert text == ("🧶 Отчёт партнёра «отчёт с 05.10 по 11.10» разобран.\n"
                    "\n"
                    "✅ Доставлено: 1\n"
                    "❓ Ждут вашего ответа: 1 (карточка выше)\n"
                    "⚠️ Сделать руками: 1 (сообщение выше)")


def test_carpet_letter_text_without_subject():
    from adminbot.carpets.watcher import CarpetLetterSummary
    from adminbot.tg.cards import carpet_letter_text

    text = carpet_letter_text(CarpetLetterSummary(uid="1", subject=None, delivered=1))

    assert text.startswith("🧶 Отчёт партнёра «без темы» проведён.")


def test_carpet_manual_text_tells_what_and_why():
    """«Сделайте руками»: что за заказ, почему, где сделка (решение владельца 12.10)."""
    from decimal import Decimal
    from adminbot.models import CarpetLink
    from adminbot.phone import for_owner
    from adminbot.tg.cards import carpet_manual_text, money

    link = CarpetLink(partner_id=44426, phone10="9601945325", status="manual",
                      lead_id=31723663, last_error="AmoError: amoCRM 500: сбой")
    text = carpet_manual_text(carpet_row(), link, hours=3,
                              base_url="https://raketa.amocrm.ru")

    lines = text.split("\n")
    assert lines[0] == ("⚠️ Ковры: не смог провести заказ партнёра №44426 за 3 часа — "
                        "сделайте руками.")
    # Полный номер — правило владельца; шапка — та же, что у карточки-вопроса.
    assert lines[1] == (f"Толстая Светлана · {for_owner('9601945325')} · "
                        f"{money(Decimal('3995'))} ₽ · Советский · "
                        "заказ у партнёра 12.08.2026 · сдано 23.08.2026")
    assert lines[2] == "Причина: AmoError: amoCRM 500: сбой."
    assert lines[3] == "Сделка: https://raketa.amocrm.ru/leads/detail/31723663"
    assert lines[4] == "Этот заказ робот больше не трогает."


def test_carpet_manual_text_without_deal_and_error():
    from adminbot.models import CarpetLink
    from adminbot.tg.cards import carpet_manual_text

    link = CarpetLink(partner_id=44426, phone10="9601945325", status="manual")
    text = carpet_manual_text(carpet_row(), link, hours=3, base_url="https://raketa.amocrm.ru")

    assert "Причина: не записана." in text
    assert "Сделка:" not in text
```

  В `test_carpet_cards_mark_rehearsal` дописать: `carpet_letter_text(CarpetLetterSummary(uid="1", subject="тема"), dry_run=True)`
  и `carpet_manual_text(carpet_row(), CarpetLink(partner_id=1, phone10="1", status="manual"), hours=3, dry_run=True)`
  начинаются с `"🎭 РЕПЕТИЦИЯ"`.

- [x] **Шаг 2: прогнать — должны упасть** (`ImportError`).

- [x] **Шаг 3: константа и класс итога** — `adminbot/carpets/watcher.py`, после `ACTIVE_STATUSES`:

```python
# Сколько строка может стоять без пройденного шага (ошибка CRM, сбой разбора), прежде
# чем робот отдаст заказ владельцу «сделать руками». Решение владельца 2026-10-12:
# двухчасовой обрыв связи с амо (24.09) робот переживает молча.
MANUAL_AFTER_SEC = 3 * 3600


@dataclass(frozen=True)
class CarpetLetterSummary:
    """Итог одного письма партнёра: что стало с каждой его строкой."""

    uid: str
    subject: Optional[str]
    delivered: int = 0          # доведены (вместе с проведёнными раньше — решение 4)
    refused: int = 0            # отказы партнёра
    waiting_owner: int = 0      # ждут ответа на карточку
    manual: int = 0             # отданы владельцу «сделать руками»

    @property
    def clean(self) -> bool:
        """Владельцу делать нечего."""
        return not (self.waiting_owner or self.manual)
```

- [x] **Шаг 4: тексты** — `adminbot/tg/cards.py`. Вынести общую часть шапки
  карточки-вопроса в помощник и использовать его в `carpet_question_card`
  (поведение карточки не меняется — её тесты это сторожат):

```python
def _carpet_details(row: Any) -> list[str]:
    """Клиент, деньги, даты — общая часть ковровых сообщений владельцу."""
    details = []
    if getattr(row, "client_name", None):
        details.append(row.client_name)
    details.append(for_owner(row.phone10))
    if row.is_refusal:
        details.append("ОТКАЗ")
    else:
        details.append(f"{money(row.amount)} ₽")
    if getattr(row, "district", None):
        details.append(row.district)
    if row.added_date:
        details.append(f"заказ у партнёра {row.added_date:%d.%m.%Y}")
    if row.return_date:
        details.append(f"сдано {row.return_date:%d.%m.%Y}")
    return details
```

  В `carpet_question_card`: `head = [f"🧶 Ковры · заказ партнёра №{row.partner_id}", *_carpet_details(row)]`.
  После `carpet_report_text` добавить:

```python
def carpet_letter_text(summary: Any, *, dry_run: bool = False) -> str:
    """Итог письма партнёра — один раз, когда у каждой строки есть судьба (ТЗ 2026-10-12)."""
    verb = "проведён" if summary.clean else "разобран"
    lines = [f"🧶 Отчёт партнёра «{summary.subject or 'без темы'}» {verb}.", ""]
    lines.append(f"✅ Доставлено: {summary.delivered}")
    if summary.refused:
        lines.append(f"✖️ Отказ: {summary.refused} (закрыл как нереализованную)")
    if summary.waiting_owner:
        lines.append(f"❓ Ждут вашего ответа: {summary.waiting_owner} (карточка выше)")
    if summary.manual:
        lines.append(f"⚠️ Сделать руками: {summary.manual} (сообщение выше)")
    if summary.clean:
        lines += ["", "Делать ничего не надо."]
    return mark_rehearsal("\n".join(lines), dry_run)


def carpet_manual_text(row: Any, link: Any, *, hours: int, base_url: Optional[str] = None,
                       dry_run: bool = False) -> str:
    """Заказ отдан владельцу: робот не смог его провести и больше не трогает."""
    lines = [
        f"⚠️ Ковры: не смог провести заказ партнёра №{row.partner_id} за {hours} часа — "
        "сделайте руками.",
        " · ".join(_carpet_details(row)),
        f"Причина: {link.last_error or 'не записана'}.",
    ]
    url = _deal_url(base_url, link.lead_id or link.primary_lead_id)
    if url:
        lines.append(f"Сделка: {url}")
    lines.append("Этот заказ робот больше не трогает.")
    return mark_rehearsal("\n".join(lines), dry_run)
```

- [x] **Шаг 5: прогнать точечно** — `tests/test_cards.py` зелёный целиком.
- [x] **Шаг 6: карта** `adminbot/tg/cards.py.map.md`.
- [x] **Шаг 7: полный прогон и коммит** — `watcher.py`, `cards.py`, `cards.py.map.md`,
  `tests/test_cards.py`. Сообщение: `Ковры: тексты итога письма и «сделайте руками» (ТЗ 2026-10-12, задача 2)`.

---

### Задача 3: наблюдатель — итог после доведения, отсчёт трёх часов, подключение

**Файлы:**
- Изменить: `raketa-admin-bot/adminbot/carpets/watcher.py`
- Изменить: `raketa-admin-bot/adminbot/main.py` (+ `main.py.map.md`)
- Тесты: `tests/test_carpet_watcher.py`

**Интерфейсы:**
- Потребляет: всё из задач 1 и 2.
- Меняет контракт: `on_report(summary: CarpetLetterSummary) -> Awaitable[None]` (был
  `on_report(letter, CarpetTickReport)`); новый `on_manual(row: CarpetRow, link: CarpetLink) -> Awaitable[Any]`.
- Новые параметры `CarpetWatcher.__init__`: `on_manual=None`,
  `manual_after_sec: int = MANUAL_AFTER_SEC`, `now: Callable[[], datetime]` (по умолчанию
  `datetime.now(timezone.utc)`).
- Найди все вызовы `on_report` и все тесты, которые его передают (поиском по
  `raketa-admin-bot/`), и приведи к новому контракту.

**Поведение (что должно получиться):**
1. `_handle_letter`: после успешного разбора всех строк — сначала
   `store.track_letter(uid, subject, partner_ids, refused_ids)` (в обоих режимах), потом,
   только в боевом, `remember_letter` и `mark_seen`. Вызов `on_report` отсюда убрать.
   `partner_ids` — номера всех строк письма в порядке файла без повторов;
   `refused_ids` — из них строки с `is_refusal`.
2. В конце прохода (`tick`, после незавершённых строк) — `_report_letters()`: для каждого
   письма из `letters_to_report()` берутся `links_by_ids`; если все строки в
   `SETTLED_STATUSES = ("done", "waiting_owner", "manual")` — `on_report(summary)` и
   `mark_letter_reported(uid)`; иначе письмо ждёт. Строки, которой нет в базе, в итоге
   нет (предупреждение в журнал). Сбой по одному письму не мешает остальным.
   Счёт: `done` и номер в `refused_ids` → `refused`; `done` иначе → `delivered`;
   `waiting_owner` → `waiting_owner`; `manual` → `manual`.
3. `_handle_row`: до `process_row` — `before = await store.get(partner_id)` (внутри того же
   `try`). После:
   - статус `error` → отсчёт: `stuck_since` пуст или шаг пройден (в `checklist` стало
     больше записей, чем в `before`) → `stuck_since = now`; иначе, если прошло
     `>= manual_after_sec` от `stuck_since` → передача владельцу;
   - любой другой статус при непустом `stuck_since` → `stuck_since = None`.
   - Исключение из `process_row`: `last_error = "<Тип>: <текст>"` и тот же отсчёт по
     строке из `store.get` (строки нет — ничего, как раньше в `failures`). Если строку
     тут же передали владельцу — в `failures` её не класть (письмо больше не держит),
     посчитать статус `manual`.
   - Передача владельцу: `store.update_and_log(partner_id, action="manual_handoff",
     dry_run=self.dry_run, payload={"error": …, "stuck_since": iso}, status="manual")`,
     предупреждение в журнал (номер заказа, без телефона), `on_manual(row, link)` в
     `try/except` с записью в журнал.
4. `tick()` под `asyncio.Lock`: `/carpets` и фоновый цикл не идут одновременно, итог и
   «сделайте руками» не уходят дважды.

- [ ] **Шаг 1: двойники** — в `tests/test_carpet_watcher.py`:
  - `FakeStore`: методы `get`, `update_and_log` (пишет в `self.logged` кортеж
    `(partner_id, action, payload)` и зовёт `update`), `track_letter`, `letters_to_report`,
    `mark_letter_reported`, `links_by_ids` — с той же логикой, что у `MemoryCarpetStore`
    из задачи 1; `pending_rows` отдаёт актуальную строку из `self.links` (если есть) и
    только в `ACTIVE_STATUSES` — как боевое хранилище;
  - `FakeEngine`: словарь `self.next_run: dict[int, dict] = {}` — поля, которые строка
    получит на ближайшем `process_row` (так движок-двойник «проходит шаг» или «доводит
    строку» во время прохода, а не до него). В `process_row` после проверки `raise_on`:

```python
        known = self.store.links.get(row.partner_id)
        if known is not None:
            if row.partner_id in self.next_run:
                known = replace(known, **self.next_run.pop(row.partner_id))
                self.store.links[row.partner_id] = known
            return known
```

  - часы:

```python
class Clock:
    def __init__(self):
        self.moment = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.moment

    def advance(self, **delta):
        self.moment += timedelta(**delta)
```

- [ ] **Шаг 2: падающие тесты** — тест `test_report_counts_outcomes` удалить (контракт
  `on_report` поменялся) и добавить:

```python
async def test_report_waits_until_every_row_is_settled():
    """Итог не уходит, пока сейлзбот не завёл сделку (письмо 11.10: «0 из 2»)."""
    mailbox = FakeMailBox([letter(subject="отчёт с 05.10 по 11.10")])
    engine = FakeEngine({
        45199: CarpetLink(partner_id=45199, phone10="1", status="done"),
        45257: CarpetLink(partner_id=45257, phone10="2", status="waiting_salesbot"),
    })
    reports = []

    async def on_report(summary):
        reports.append(summary)

    watcher = make_watcher(mailbox, engine, on_report=on_report,
                           rows=[row(45199), row(45257)])
    await watcher.tick()
    assert reports == []                                   # сейлзбот ещё думает

    engine.next_run[45257] = {"status": "done"}           # сейлзбот завёл, робот довёл
    engine.store.pending = [(row(45257), engine.store.links[45257])]
    await watcher.tick()
    await watcher.tick()                                   # и второй раз не повторяет

    assert reports == [CarpetLetterSummary(uid="1", subject="отчёт с 05.10 по 11.10",
                                           delivered=2)]


async def test_report_does_not_wait_for_owner_answers():
    """Карточка без ответа — не повод держать итог (решение 2), отказ — своей строкой."""
    mailbox = FakeMailBox([letter()])
    engine = FakeEngine({
        44426: CarpetLink(partner_id=44426, phone10="1", status="done"),
        44535: CarpetLink(partner_id=44535, phone10="2", status="waiting_owner"),
        43986: CarpetLink(partner_id=43986, phone10="3", status="done"),
    })
    reports = []

    async def on_report(summary):
        reports.append(summary)

    rows = [row(44426), row(44535), row(43986, is_refusal=True)]
    await make_watcher(mailbox, engine, on_report=on_report, rows=rows).tick()

    assert reports == [CarpetLetterSummary(uid="1", subject="отчёт с 17.08 по 23.08",
                                           delivered=1, refused=1, waiting_owner=1)]


async def test_stuck_row_is_handed_to_owner_after_three_hours():
    clock = Clock()
    mailbox = FakeMailBox([letter()])
    engine = FakeEngine({44426: CarpetLink(partner_id=44426, phone10="9601945325",
                                           status="error", last_error="AmoError: 500")})
    reports, manual = [], []

    async def on_report(summary):
        reports.append(summary)

    async def on_manual(row_, link):
        manual.append((row_.partner_id, link.status))

    watcher = make_watcher(mailbox, engine, on_report=on_report, on_manual=on_manual,
                           now=clock)
    await watcher.tick()
    assert engine.store.links[44426].stuck_since == clock.moment

    engine.store.pending = [(row(), engine.store.links[44426])]
    clock.advance(hours=2, minutes=59)
    await watcher.tick()
    assert manual == [] and reports == []

    clock.advance(minutes=1)
    await watcher.tick()
    await watcher.tick()                                   # повторно не пишет

    assert manual == [(44426, "manual")]
    assert engine.store.links[44426].status == "manual"
    assert engine.store.logged[0][:2] == (44426, "manual_handoff")
    assert reports == [CarpetLetterSummary(uid="1", subject="отчёт с 17.08 по 23.08",
                                           manual=1)]


async def test_passed_step_restarts_the_clock():
    """Любой пройденный шаг — отсчёт заново (решение 5)."""
    clock = Clock()
    engine = FakeEngine({44426: CarpetLink(partner_id=44426, phone10="1", status="error",
                                           checklist={"fill_carpet_lead": "t"})})
    watcher = make_watcher(FakeMailBox([letter()]), engine, now=clock)
    await watcher.tick()

    clock.advance(hours=2)
    engine.next_run[44426] = {"checklist": {"fill_carpet_lead": "t",
                                            "move_carpet_delivered": "t"}}
    engine.store.pending = [(row(), engine.store.links[44426])]
    await watcher.tick()
    assert engine.store.links[44426].stuck_since == clock.moment

    clock.advance(hours=2)
    await watcher.tick()
    assert engine.store.links[44426].status == "error"     # с перезапуска прошло два часа


async def test_recovered_row_forgets_the_clock():
    clock = Clock()
    engine = FakeEngine({44426: CarpetLink(partner_id=44426, phone10="1", status="error")})
    watcher = make_watcher(FakeMailBox([letter()]), engine, now=clock)
    await watcher.tick()

    engine.next_run[44426] = {"status": "done"}           # амо ожила, строка доведена
    engine.store.pending = [(row(), engine.store.links[44426])]
    await watcher.tick()

    assert engine.store.links[44426].stuck_since is None


async def test_crashing_row_is_handed_over_and_the_letter_completes():
    """Исключение не-амо: раньше письмо крутилось молча вечно, теперь — три часа."""
    clock = Clock()
    mailbox = FakeMailBox([letter()])
    engine = FakeEngine()
    engine.store.links[44426] = CarpetLink(partner_id=44426, phone10="1",
                                           status="in_progress")
    engine.raise_on = {44426}
    reports, manual = [], []

    async def on_report(summary):
        reports.append(summary)

    async def on_manual(row_, link):
        manual.append(row_.partner_id)

    watcher = make_watcher(mailbox, engine, on_report=on_report, on_manual=on_manual,
                           now=clock)
    first = await watcher.tick()
    assert first.failures and mailbox.seen == []
    assert engine.store.links[44426].last_error == "RuntimeError: амо не отвечает"

    clock.advance(hours=3)
    await watcher.tick()

    assert manual == [44426]
    assert mailbox.seen == ["1"]                           # письмо больше не держит
    assert reports == [CarpetLetterSummary(uid="1", subject="отчёт с 17.08 по 23.08",
                                           manual=1)]


async def test_rehearsal_reports_once_and_leaves_the_letter_unread():
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()
    reports = []

    async def on_report(summary):
        reports.append(summary)

    watcher = make_watcher(mailbox, engine, on_report=on_report, dry_run=True)
    await watcher.tick()
    await watcher.tick()

    assert len(reports) == 1
    assert mailbox.seen == [] and engine.store.remembered == []


async def test_concurrent_ticks_send_one_report():
    """/carpets во время фонового прохода не удваивает итог."""
    mailbox, engine = FakeMailBox([letter()]), FakeEngine()
    reports = []

    async def on_report(summary):
        reports.append(summary)

    watcher = make_watcher(mailbox, engine, on_report=on_report)
    await asyncio.gather(watcher.tick(), watcher.tick())

    assert len(reports) == 1
```

  Импорты в начало файла: `from dataclasses import replace`, `from datetime import date, datetime, timedelta, timezone`,
  `from adminbot.carpets.watcher import CarpetLetterSummary, CarpetWatcher`.
  Строка-отказ `row(43986, is_refusal=True)` проходит `rows_to_process`: `is_ours`
  по умолчанию `True` (`adminbot/carpets/report.py`).

- [ ] **Шаг 3: прогнать — должны упасть.**

- [ ] **Шаг 4: наблюдатель** — `adminbot/carpets/watcher.py` по разделу «Поведение».
  Опорный код (подогнать к файлу; имена — как здесь, их ждут тесты):

```python
SETTLED_STATUSES = frozenset(("done", "waiting_owner", "manual"))


def summarize_letter(letter: dict, links: list[CarpetLink]) -> Optional[CarpetLetterSummary]:
    """Итог письма или None, если хоть одна строка ещё в работе.

    Строки, которой нет в базе, в итоге нет: считать по ней нечего.
    """
    by_id = {link.partner_id: link for link in links}
    refused_ids = set(letter.get("refused_ids") or ())
    counts: Counter[str] = Counter()
    for partner_id in dict.fromkeys(letter["partner_ids"]):
        link = by_id.get(partner_id)
        if link is None:
            continue
        if link.status not in SETTLED_STATUSES:
            return None
        if link.status == "done":
            counts["refused" if partner_id in refused_ids else "delivered"] += 1
        else:
            counts[link.status] += 1
    return CarpetLetterSummary(uid=letter["uid"], subject=letter.get("subject"),
                               delivered=counts["delivered"], refused=counts["refused"],
                               waiting_owner=counts["waiting_owner"], manual=counts["manual"])
```

```python
    async def _note_trouble(self, row: CarpetRow, before: Optional[CarpetLink],
                            link: Optional[CarpetLink], error: Optional[str] = None
                            ) -> Optional[CarpetLink]:
        """Строка не сдвинулась: завести или продолжить отсчёт; три часа — владельцу."""
        if link is None:
            return None                     # строки нет: сбой случился до её заведения
        moment = self.now()
        fields: dict[str, Any] = {"last_error": error} if error else {}
        progressed = (before is not None
                      and len(link.checklist or {}) > len(before.checklist or {}))
        if link.stuck_since is None or progressed:
            return await self.store.update(row.partner_id, stuck_since=moment, **fields) or link
        if (moment - link.stuck_since).total_seconds() < self.manual_after_sec:
            return (await self.store.update(row.partner_id, **fields) or link) if fields else link

        link = await self.store.update_and_log(
            row.partner_id, action="manual_handoff", dry_run=self.dry_run,
            payload={"error": error or link.last_error,
                     "stuck_since": link.stuck_since.isoformat()},
            status="manual", **fields) or link
        log.warning("Ковры, заказ партнёра №%s: %s ч без продвижения — отдаю владельцу",
                    row.partner_id, self.manual_after_sec // 3600)
        await self._send_manual(row, link)
        return link
```

  В ветке исключения `link` для `_note_trouble` — из `await self.store.get(partner_id)`
  в своём `try/except` (база недоступна — записать в журнал и вернуть `None`).

- [ ] **Шаг 5: подключение** — `adminbot/main.py`:
  - `MAIL_CARPET_MANUAL = "carpet_manual"` рядом с `MAIL_CARPET_*`;
  - импорт `carpet_letter_text`, `carpet_manual_text` из `adminbot.tg.cards` и
    `MANUAL_AFTER_SEC` из `adminbot.carpets.watcher`;
  - `_make_carpet_report_sender` — новый контракт и верный докстринг (сейчас там по
    ошибке «Письмо отложено»):

```python
def _make_carpet_report_sender(mail: OwnerMail, dry_run: bool = False):
    """Итог письма партнёра — один раз, когда у каждой строки есть судьба."""
    async def send(summary) -> None:
        await mail.send(carpet_letter_text(summary, dry_run=dry_run), kind=MAIL_CARPET_REPORT)

    return send


def _make_carpet_manual_sender(mail: OwnerMail, amo_base_url: str, dry_run: bool = False):
    """Заказ отдан владельцу «сделать руками»: робот три часа не мог его сдвинуть."""
    async def send(row, link) -> None:
        await mail.send(carpet_manual_text(row, link, hours=MANUAL_AFTER_SEC // 3600,
                                           base_url=amo_base_url, dry_run=dry_run),
                        kind=MAIL_CARPET_MANUAL, ref=row.partner_id)

    return send
```

  - `_build_carpets`: `on_manual=_make_carpet_manual_sender(mail, settings.amo_base_url, dry_run=settings.carpets_dry_run)`.

- [ ] **Шаг 6: прогнать точечно** — `tests/test_carpet_watcher.py` целиком зелёный
  (старые тесты наблюдателя тоже).
- [ ] **Шаг 7: докстринг модуля** `watcher.py` — правило 3 дополнить: итог письма уходит,
  когда у всех его строк есть судьба; строка без продвижения три часа уходит владельцу.
- [ ] **Шаг 8: карта** `adminbot/main.py.map.md` (новый отправитель, сдвиг номеров).
- [ ] **Шаг 9: полный прогон и коммит** — `watcher.py`, `main.py`, `main.py.map.md`,
  `tests/test_carpet_watcher.py`. Сообщение: `Ковры: итог письма после доведения, «сделайте руками» через 3 часа (ТЗ 2026-10-12, задача 3)`.

---

### Задача 4 (только по «да» владельца): выбор сделки после сбоя амо

**Файлы:** `raketa-admin-bot/adminbot/carpets/engine.py` (`process_row`), `tests/test_carpet_engine.py`.

- [ ] **Шаг 1: падающий тест** — рядом с `test_interrupted_chain_continues_from_where_it_stopped`:

```python
async def test_crm_failure_while_choosing_a_deal_retries_the_choice():
    """Сбой амо на поиске сделок: повтор снова выбирает сделку, а не пишет в пустоту
    (найдено 12.10.2026 при подготовке ТЗ итогов)."""
    amo, store = FakeAmo(), MemoryCarpetStore()
    lead_id = open_carpet_lead(amo)
    engine = make_engine(amo, store)

    amo.fail_on = "find_leads_by_phone"
    assert (await engine.process_row(row())).status == "error"

    amo.fail_on = None
    link = await engine.process_row(row())

    assert link.status == "done" and link.lead_id == lead_id
    assert [lead for lead, _ in amo.calls_of("update_lead")] == [lead_id]
```

- [ ] **Шаг 2: прогнать — падает** (`update_lead` зовётся с `None`).
- [ ] **Шаг 3: правка** — условие выбора пути в `process_row`:

```python
            # Решение ещё не принято — принимаем. `error` без пути и сделок — сбой амо
            # случился на самом выборе (поиск по телефону): без этой ветки повтор
            # шёл заполнять несуществующую сделку (найдено 12.10.2026).
            undecided = (link.lead_id is None and link.primary_lead_id is None
                         and link.path is None)
            if not link.checklist and link.lead_id is None and (
                    link.status == "new" or (link.status == "error" and undecided)):
                link = await self._decide(row, link)
```

- [ ] **Шаг 4: прогнать** `tests/test_carpet_engine.py` целиком — зелёный.
- [ ] **Шаг 5: полный прогон и коммит** — `engine.py`, `tests/test_carpet_engine.py`.
  Сообщение: `Ковры: сбой амо при выборе сделки — повтор выбирает заново (ТЗ 2026-10-12, задача 4)`.

---

## Проверка и выкат (координатор)

1. После каждой задачи — сверка с ТЗ по диффу (координатор). После задачи 3 — одно
   ревью качества на всю ветку (`reviewer`, Opus; без запуска тестов).
2. Пуш — решение владельца. Выкат админ-бота — `raketa-admin-bot/docs/deploy.md` §9:
   `rsync` из `~/Projects/raketaclean/raketa-admin-bot/` в `/home/admin/raketa-admin-bot/`,
   затем `sudo raketa-admin-bot-update` (миграция 021 накатится сама). `rsync` — с
   согласия владельца.
3. После выката: `\d adminbot.carpet_letters` — есть `partner_ids, refused_ids, reported_at`;
   старые письма итога не получат (`partner_ids IS NULL`). Приёмка — ближайшее письмо
   партнёра: одно сообщение «🧶 Отчёт партнёра «…» проведён», без «0 из N».
