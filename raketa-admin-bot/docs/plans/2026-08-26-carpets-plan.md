# Ковры от партнёра (этап 3) — план реализации

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Робот сам забирает недельный отчёт партнёра «Кристалл» из почты и доводит ковровые сделки в amoCRM до конца — вместо ручной сверки владельцем.

**Architecture:** Новая функция `carpets` в том же сервисе, рядом с `amo_sync`. Своя цепочка: почта (IMAP, только чтение) → разбор Excel → сопоставление строки со сделкой ковровой воронки → заполнение и проведение. Переиспользуем готовое ядро: клиент amoCRM с режимом репетиции, нормализацию телефонов, хранилище `adminbot`, карточки-вопросы владельцу и вечернюю сводку. Своя таблица привязок (ключ — номер заказа партнёра), поэтому повторная обработка того же файла ничего не портит.

**Tech Stack:** Python 3.10+ (на сервере 3.10.12), `openpyxl` для Excel, встроенный `imaplib` для почты, остальное — как в v1 (aiogram 3, asyncpg, aiohttp).

**Что уже проверено на боевых данных (2026-08-26):**

- Почта работает: ящик `amoraketaclean@yandex.ru`, папка `robot_amo`, в ней два письма «Fwd: отчёт с 10.08 по 16.08» и «Fwd: отчёт с 17.08 по 23.08» с вложениями `Договоры (N).xlsx`. Доступ — пароль приложения, лежит на сервере в `~/.mail_robot.env` (chmod 600).
- Формат отчёта стабилен: один лист `Worksheet`, 26 колонок с заголовками. Разобрано 5 файлов, 51 заказ (`tgbot-v1/recon/05-partner-excel.md`).
- Ковровая воронка и её этапы прочитаны из боевой CRM (см. константы ниже).
- Метрика сопоставления: 90% строк находят сделку в ковровой воронке по телефону, 6% — только сделку в воронках уборки, 4% не находят ничего.

**Константы (проверены в боевой amoCRM 2026-08-26), кладём в `adminbot/amo/ids.py`:**

```python
PIPELINE_CARPETS = 4645519            # «Ковры Кристал» — уже есть в ids.py
CARPET_STAGE_UNSORTED = 42638824      # Неразобранное
CARPET_STAGE_HANDED_OVER = 42638827   # «Передано в работу (ВПО)» — здесь ждут результата партнёра
CARPET_STAGE_IN_WORK = 71292730       # Заказ в работе
CARPET_STAGE_DELIVERED = 142          # «Заказ доставлен» = успех воронки
CARPET_STAGE_REFUSED = 143            # «Закрыто и не реализовано»

FIELD_CARPET_PICKUP = 1414101         # «Дата забора ковра»   ← колонка «Факт забор»
FIELD_CARPET_RETURN = 1414103         # «Дата возврата ковра» ← колонка «Факт сдача»
FIELD_DISTRICT = 1453415              # «Район города»        ← колонка «Город»
SERVICE_ENUM_CARPETS = 947369         # «Ковры КРИСТАЛ» в списке «Услуга»
SPECIALIST_ENUM_CARPETS = 947245      # «Кристал» в списке «Специалист»

# «Район города»: название из отчёта → значение списка амо
DISTRICT_ENUMS = {
    "автозаводский": 946951, "советский": 946953, "сормовский": 946955,
    "ленинский": 946957, "нижегородский": 946959, "приокский": 946961,
    "канавинский": 946963, "московский": 946965, "анкудиновка": 946967,
    "новинки": 946969, "кстовский": 946971, "борский": 946973,
    "богородский": 946975, "дзержинский": 947035,
}
```

**Решения владельца (2026-08-26):**

1. Бюджет сделки = колонка **«Взято денег у клиента»** (фактически полученные деньги), а не «Стоимость».
2. Отказы (`Статус = «Забор отказ»`) — закрывать как нереализованные, причину писать комментарием.
3. Если сделки в CRM нет вовсе — заводить лид с услугой «Ковры КРИСТАЛ», сейлзбот создаст сделку в ковровой воронке, робот её доводит (та же цепочка, что путь В в v1).
4. Неоднозначное — карточкой владельцу, а не догадками.

---

## Фаза 0 — данные партнёра (без сети)

### Task 1: Разбор отчёта Excel

**Files:**
- Create: `adminbot/carpets/__init__.py`, `adminbot/carpets/report.py`
- Test: `tests/test_carpet_report.py`
- Test fixtures: реальные файлы уже лежат в `~/Projects/tgbot-v1/recon/data/` (в git не попадают, содержат ПД). Тест берёт их через переменную `CARPET_FIXTURES_DIR`, а без неё пропускается — как сделано для БД в `tests/test_db_schema.py`.

**Step 1: Write the failing test**

```python
# tests/test_carpet_report.py
"""Разбор недельного отчёта партнёра по коврам.

Файлы содержат персональные данные клиентов, поэтому в репозиторий не попадают:
путь к ним задаётся переменной CARPET_FIXTURES_DIR, без неё тесты пропускаются.
Проверяем на настоящих файлах — формат партнёра нам не подчиняется.
"""

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from adminbot.carpets.report import CarpetRow, parse_report

FIXTURES = os.environ.get("CARPET_FIXTURES_DIR")
pytestmark = pytest.mark.skipif(not FIXTURES, reason="CARPET_FIXTURES_DIR не задан")


def rows_of(name: str) -> list[CarpetRow]:
    return parse_report((Path(FIXTURES) / name).read_bytes())


def test_completed_report_is_parsed():
    rows = rows_of("Договоры (11).xlsx")

    assert len(rows) == 4
    row = next(r for r in rows if r.partner_id == 44426)
    assert row.phone10 == "9601945325"            # в файле число 89601945325
    assert row.amount == Decimal("3995")          # «Взято денег у клиента»
    assert row.district == "Советский"
    assert row.address == "Ивлеева 18-101 п6 э1"
    assert row.payment_method == "Наличные"
    assert row.pickup_date == date(2026, 8, 16)   # «Факт забор» 16.08.2026 11:45
    assert row.return_date == date(2026, 8, 23)   # «Факт сдача» 23.08.2026 11:24
    assert row.is_refusal is False
    assert row.is_ours is True                    # «Рекламный источник» = РАКЕТА


def test_refusal_report_is_parsed():
    rows = rows_of("ракета забор отказ.xlsx")

    assert all(r.is_refusal for r in rows)
    row = next(r for r in rows if r.partner_id == 43986)
    assert row.amount == Decimal(0)
    assert row.refusal_reason == "Не взяли трубку"
    assert row.pickup_date is None                # забора не было


def test_single_row_file_is_parsed():
    rows = rows_of("Договоры (10).xlsx")

    assert len(rows) == 1
    assert rows[0].partner_id == 44345
    assert rows[0].phone10 == "9108970195"        # телефон без 8/7 впереди
```

**Step 2:** Run: `CARPET_FIXTURES_DIR=~/Projects/tgbot-v1/recon/data pytest tests/test_carpet_report.py -v` → FAIL (`ModuleNotFoundError`).

**Step 3: Implement** — `adminbot/carpets/report.py`:

- `@dataclass(frozen=True) class CarpetRow`: `partner_id:int`, `phone10:Optional[str]`, `client_name`, `address`, `district`, `amount:Decimal`, `payment_method`, `pickup_date`, `return_date`, `added_date`, `status`, `refusal_reason`, `comment`, `is_ours:bool`, `is_refusal:bool`.
- `parse_report(data: bytes) -> list[CarpetRow]` — `openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)`, первая строка — заголовки, колонки ищем **по названию**, а не по номеру (партнёр может добавить колонку).
- Ловушки настоящих файлов, каждая — отдельная строка в коде с комментарием:
  - телефон приходит **числом** (`79108970195`, `89601945325`) → `str()` перед `last10()`;
  - даты — строки двух видов: `«13.08.2026 13:09:37»` и `«08.08.2026 12:44»`;
  - «Стоимость» — строка `«1 200.00»` с пробелом-разделителем, «Взято денег у клиента» — число;
  - пустые значения приходят как `«-»`, `None` или пустая строка;
  - `is_ours` = «Рекламный источник» содержит `РАКЕТА` (без учёта регистра);
  - `is_refusal` = статус содержит `отказ`.
- Добавить `openpyxl==3.1.5` в `requirements.txt`.

**Step 4:** Тесты проходят.

**Step 5:** `git commit -m "feat: разбор недельного отчёта партнёра по коврам"`

### Task 2: Отбор строк к обработке

**Files:**
- Modify: `adminbot/carpets/report.py`
- Test: `tests/test_carpet_report.py`

Функция `rows_to_process(rows) -> tuple[list[CarpetRow], list[CarpetRow]]` — («провести», «закрыть как отказ»). Правила: не наши строки (`is_ours=False`) отбрасываем; выполненные без телефона — в вопросы владельцу (не молча); отказы отделяем.

Тест: на «ракета забор отказ.xlsx» первый список пуст, второй — все 4; на «Договоры (11).xlsx» — наоборот.

Commit: `feat: отбор строк отчёта к обработке`.

---

## Фаза 1 — почта

### Task 3: Чтение писем партнёра

**Files:**
- Create: `adminbot/mail.py`
- Test: `tests/test_mail.py`

`imaplib` — синхронный, поэтому работу с ним выносим в поток (`asyncio.to_thread`), чтобы не блокировать бота.

**Step 1: Write the failing test** — на двойнике IMAP (класс с методами `login/select/search/fetch/store/logout`), живой почты в тестах нет:

```python
async def test_fetches_only_unread_letters_with_xlsx():
    box = FakeImap(letters=[
        Letter(uid=1, subject="отчёт с 10.08 по 16.08", attachments={"Договоры (10).xlsx": b"PK..."}),
        Letter(uid=2, subject="реклама", attachments={}),
        Letter(uid=3, subject="отчёт", attachments={"отчёт.pdf": b"%PDF"}, seen=True),
    ])
    letters = await MailBox(connect=lambda: box, folder="robot_amo").fetch_new()

    assert [l.uid for l in letters] == [1]
    assert letters[0].attachments["Договоры (10).xlsx"].startswith(b"PK")


async def test_letter_is_marked_read_only_after_success():
    """Пометка прочитанным — признак «разобрано». Ставим её последней."""
```

**Step 3: Implement** — `MailBox`: `fetch_new()` (непрочитанные письма папки, вложения `.xlsx`), `mark_seen(uid)`, `settings_from_env()`. Пароль и адрес — из окружения (`MAIL_*`), в логи не попадают никогда.

Commit: `feat: чтение писем партнёра по IMAP`.

---

## Фаза 2 — сопоставление и проведение

### Task 4: Матчер ковровых сделок

**Files:**
- Create: `adminbot/carpets/matcher.py`
- Test: `tests/test_carpet_matcher.py`

Вход: строка отчёта + сделки клиента (по телефону). Правила по убыванию надёжности:

1. открытая сделка ковровой воронки (этапы «Передано в работу (ВПО)», «Заказ в работе», «Неразобранное») → `use_carpet_lead`;
2. таких несколько → выбрать по дате: «Добавление» из отчёта против даты создания сделки (±7 дней, цикл заказа 1–2 недели); не различили → `ask_owner`;
3. проведённая ковровая сделка с датой возврата ≈ «Факт сдача» → `already_done` (владелец провёл сам);
4. ковровых сделок нет вовсе, но есть сделки уборки → `ask_owner_unrelated` (6% случаев из разведки: заказ завели не в ту воронку);
5. ничего → `create_new`.

Тесты — на каждый пункт, плюс случай B2B-клиента с 33 сделками (`recon/07-open-questions.md`, п. 9) как худший случай.

Commit: `feat: матчер ковровых сделок`.

### Task 5: Проведение ковровой сделки

**Files:**
- Create: `adminbot/carpets/engine.py`
- Test: `tests/test_carpet_engine.py`

Чек-лист «выполнено»: `fill_carpet_lead` → `move_carpet_done` → `note_robot_done`. Поля:

| Поле амо | Откуда |
|---|---|
| бюджет | «Взято денег у клиента» |
| «Вариант оплаты» (464605) | «Форма оплаты»: Карта → 235851, Наличные → 235849, Перевод → 235851 |
| «Дата забора ковра» (1414101) | «Факт забор» |
| «Дата возврата ковра» (1414103) | «Факт сдача» |
| «Дата оплаты» (18643) | «Факт сдача» |
| «Район города» (1453415) | «Город» через `DISTRICT_ENUMS`; неизвестный район — **не заполняем** |
| «Адрес» (18639) | «Адрес», только если поле пустое |
| «Услуга» (271915) | «Ковры КРИСТАЛ», только если поле пустое |
| «Специалист» (39243) | «Кристал», только если поле пустое |

Этап: `CARPET_STAGE_DELIVERED` (142). Отказ: `CARPET_STAGE_REFUSED` (143) + комментарий «Партнёр не забрал ковры: <причина>».

Тесты: заполненные поля не перезаписываются; отказ не трогает бюджет; повторный прогон той же строки не делает ничего (идемпотентность).

Commit: `feat: проведение ковровой сделки по отчёту партнёра`.

### Task 6: Сделки нет — заводим цепочку

**Files:**
- Modify: `adminbot/carpets/engine.py`
- Test: `tests/test_carpet_engine.py`

Решение владельца №3: найти/создать контакт по телефону → создать лид в первичной воронке с услугой «Ковры КРИСТАЛ» → перевести в «Передано в работу» → дождаться автосделки сейлзбота в ковровой воронке (до 10 минут, как в v1) → довести её по Task 5.

Тест: полная цепочка на двойниках, включая случай «сейлзбот молчит» → вопрос владельцу.

Commit: `feat: цепочка с нуля для ковровых заказов`.

---

## Фаза 3 — интеграция и запуск

### Task 7: Своё хранилище

**Files:**
- Create: `migrations/004_carpets.sql`
- Modify: `adminbot/db.py`, `adminbot/sync/store.py`
- Test: `tests/test_db_schema.py`

```sql
CREATE TABLE IF NOT EXISTS adminbot.carpet_links (
    partner_id  bigint PRIMARY KEY,          -- номер заказа в CRM партнёра (колонка «#»)
    phone10     text NOT NULL,
    status      text NOT NULL DEFAULT 'new', -- new|in_progress|waiting_owner|waiting_salesbot|done|error
    lead_id     bigint,
    checklist   jsonb NOT NULL DEFAULT '{}'::jsonb,
    question    jsonb,
    question_msg_id bigint,
    last_error  text,
    source_file text,                        -- имя вложения, из которого пришла строка
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
```

Ключ — номер заказа партнёра: месячный свод содержит те же заказы, что и недельные отчёты, и обработать их второй раз нельзя.

Тест: повторная запись той же строки не создаёт дубль; обработанные строки не берутся снова.

Commit: `feat: хранилище ковровых привязок`.

### Task 8: Наблюдатель почты и Telegram

**Files:**
- Create: `adminbot/carpets/watcher.py`
- Modify: `adminbot/main.py`, `adminbot/tg/bot.py`, `adminbot/tg/cards.py`
- Test: `tests/test_carpet_watcher.py`, `tests/test_cards.py`

- Свой выключатель `CARPETS_ENABLED` (по умолчанию выключено) и свой режим репетиции — правило проекта: у каждой функции свой стоп-кран.
- Опрос почты раз в час (`CARPETS_POLL_INTERVAL_SEC=3600`): письма приходят раз в неделю, чаще смысла нет.
- Команда `/carpets` — показать, что лежит в почте и что робот сделает; кнопки «🚀 Поехали» / «✋ Отложить», как у хвоста.
- Карточка-вопрос по строке отчёта: «Ковры · Светлана …5303 · 3 995 ₽ · сдано 23.08» + кнопки со сделками.
- Итог обработки файла отдельным сообщением: проведено / закрыто отказов / вопросов / ошибок. В вечернюю сводку добавить строку про ковры.

Commit: `feat: наблюдатель почты и телеграм-интерфейс ковров`.

### Task 9: Репетиция и боевой запуск

Порядок (каждый шаг подтверждает владелец):

1. `CARPETS_ENABLED=1`, репетиция — прогон по двум письмам, что уже лежат в папке. Владелец смотрит план глазами.
2. Сверка выборочно: 2–3 сделки открыть в amoCRM и сравнить с отчётом.
3. Боевой режим, обработка обоих писем.
4. Неделя наблюдения: следующий отчёт партнёра робот разбирает сам.

---

## Definition of Done

- [ ] На пяти реальных файлах партнёра разбор даёт 51 строку без ошибок.
- [ ] Все тесты зелёные: `pytest -q`.
- [ ] Два письма из папки `robot_amo` обработаны, владелец сверил сделки в CRM.
- [ ] Доля вопросов владельцу ≤ 10% строк (по разведке ожидается ~10%: 6% чужая воронка + 4% нет сделки).
- [ ] Повторная обработка того же файла не делает в CRM ничего.
- [ ] Свой выключатель проверен: `CARPETS_ENABLED=0` останавливает функцию, не задевая amo_sync.
- [ ] `docs/deploy.md` дополнен: переменные `MAIL_*`, миграция 004.
