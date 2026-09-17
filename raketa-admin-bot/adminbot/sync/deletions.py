"""Источник неразобранных удалений: заказ снят в рабочем боте, связка про это
ещё не в курсе.

У удаления два разных следа (факт 1, ТЗ 2026-09-17 «удаление заказа освобождает
сделку»): заказ химчистки при удалении пропадает из БД физически, и рабочий
бот кладёт след в свой регистр `public.deleted_orders`; уборка остаётся строкой
`public.cleaning_orders` с заполненным `deleted_at`. Разбор (задача 5, отдельным
этапом) для обоих один и тот же — не важно, каким путём заказ пропал, поэтому
источник сводит оба следа к одному списку `DeletionRecord`.

Разобранные записи не возвращаются повторно — критерий не сам факт удаления,
а отметка `adminbot.order_deletions_seen` (задача 3), ключ (`kind`, `order_id`):
номера заказов и уборок пересекаются, поэтому голого `order_id` недостаточно.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import asyncpg

from adminbot import db
from adminbot.amo import ids
from adminbot.amo.fields import as_msk
from adminbot.models import AmoLink, DeletionRecord
from adminbot.sync.store import PgCleaningLinkStore, PgLinkStore

log = logging.getLogger(__name__)


async def fetch_pending_deletions(own_pool: asyncpg.Pool) -> list[DeletionRecord]:
    """Все неразобранные удаления — заказы и уборки вместе, старые первыми.

    Сортировка по времени удаления, а не по виду работы: решение владельца 4
    (ТЗ 2026-09-17) разбирает удаления первым шагом прохода, и внутри этого шага
    порядок должен быть предсказуемым, а не «сначала все заказы, потом уборки».
    """
    orders = await db.fetch_pending_order_deletions(own_pool)
    cleanings = await db.fetch_pending_cleaning_deletions(own_pool)
    return sorted(orders + cleanings, key=lambda r: (r.deleted_at, r.kind, r.order_id))


# --- обработчик (задача 5): что делать с каждой неразобранной записью ---
#
# Пять веток разбора (решения владельца 1, 3, 5-7 из ТЗ 2026-09-17), строго
# в этом порядке:
#   1. связки нет                                  → отметить, молчим (решение 7)
#   2. сделка занята связкой ДРУГОГО живого заказа  → статус не трогать, примечание,
#      сообщить (решение 5) — это и делает разбор безопасным при любом порядке
#      событий: гонку «удаление разобрали позже, чем новый заказ уже перехватил
#      сделку» закрывает именно эта ветка, а не порядок вызова.
#   3. сделки в CRM больше нет (ответ без id)       → пометить связку, сообщить
#      (решение 3)
#   4. сделка открыта (статус не финальный)         → статус не трогать, примечание,
#      сообщить (решение 6)
#   5. сделка закрыта                               → вернуть на «Заказ подтвержден,
#      Мастер назначен», примечание, сообщить
#
# Отдельный, прямо в ТЗ не описанный случай: связка есть, но ни одной сделки ей
# ещё не назначено (`real_lead_id` и `primary_lead_id` оба пусты — робот успел
# завести строку связки, но до похода в CRM не дошёл: статус `new`, либо застрял
# на `waiting_owner`/`error` до выбора сделки). Трогать в CRM нечего, поэтому по
# духу решения 7 робот молчит; связку, в отличие от ветки 1, всё же помечаем
# отменённой — она принадлежит удалённому заказу и не должна вечно считаться
# «активной» (см. `fetch_taken_lead_ids`, задача 6).

# Пометка связки после разбора (кроме ветки 1, где связки нет вовсе): статус
# `cancelled`, а не удаление строки (решение 1 — «через год опять упрёмся в то,
# что сейчас обходим»), и заглушить напоминание про адрес — иначе оно всплывёт
# по мёртвой связке.
_CANCEL_FIELDS = {"status": "cancelled", "address_reminder_muted": True}


@dataclass(frozen=True)
class DeletionOutcome:
    """Что стало со сделкой после разбора одной записи — сырьё для письма
    владельцу. Само письмо собирает `main.py` (текст — дело представления;
    этот модуль — про решения и запись, как `sync/address_reminder.py`, который
    тоже не строит карточку сам, а зовёт `on_reminder`)."""

    record: DeletionRecord
    link: AmoLink
    outcome: str                              # held_by_other | lead_gone | left_open | reopened
    lead_id: Optional[int] = None
    other_kind: Optional[str] = None
    other_order_id: Optional[int] = None


# Кто ищет сделку, занятую другой, живой связкой (ветка 2). Один запрос сразу
# по обеим таблицам связок — «живой» заказ проверяется тут же, JOIN'ом в
# `public` (факт 3 ТЗ: обе схемы в одной базе, роль `adminbot` читает `public`).
_OTHER_LIVE_HOLDER_SQL = """
    SELECT 'order' AS kind, o.order_id
    FROM adminbot.amo_links o
    WHERE (o.real_lead_id = $1 OR o.primary_lead_id = $1)
      AND NOT ($3 = 'order' AND o.order_id = $2)
      AND NOT EXISTS (SELECT 1 FROM public.deleted_orders d WHERE d.order_id = o.order_id)
    UNION ALL
    SELECT 'cleaning' AS kind, c.order_id
    FROM adminbot.cleaning_links c
    WHERE (c.real_lead_id = $1 OR c.primary_lead_id = $1)
      AND NOT ($3 = 'cleaning' AND c.order_id = $2)
      AND EXISTS (
          SELECT 1 FROM public.cleaning_orders co
          WHERE co.id = c.order_id AND co.deleted_at IS NULL
      )
    LIMIT 1
"""


def _label(kind: str) -> str:
    return "Уборка" if kind == "cleaning" else "Заказ"


def _note_text(record: DeletionRecord, outcome: str, *,
               other_kind: Optional[str] = None, other_order_id: Optional[int] = None) -> str:
    """Примечание в сделку амо (решение 2: бюджет и поля не трогаем — они
    перезапишутся при повторном проведении)."""
    when = as_msk(record.deleted_at).strftime("%d.%m.%Y %H:%M")
    head = f"🤖 {_label(record.kind)} №{record.order_id} удалён из бота {when} по Москве."
    if outcome == "held_by_other":
        other_label = "уборкой" if other_kind == "cleaning" else "заказом"
        return f"{head} Сделка осталась закреплена за {other_label} №{other_order_id} — этап не меняю."
    if outcome == "left_open":
        return f"{head} Сделка сейчас открыта — этап не меняю, разберитесь сами."
    if outcome == "reopened":
        return (f"{head} Сделка возвращена на этап «Заказ подтвержден, мастер назначен». "
                f"Бюджет и поля оставлены как есть — перезапишутся, если заказ проведут заново.")
    return head


class DeletionHandler:
    """Разбор пачки неразобранных удалений (задача 5).

    Не отдельный цикл: у него нет ни `run_forever`, ни своего `sleep` — только
    один проход `run()`, который наблюдатель (`sync/watcher.py`) вызывает первым
    шагом своего тика (решение владельца 4).

    Репетиция (`dry_run=True`) не должна оставлять следов, которые заберут
    работу у последующего боя, — это правило проекта, и здесь оно особенно
    важно: отметка «разобрано» (`adminbot.order_deletions_seen`) окончательна,
    второго шанса у записи нет (в отличие, например, от счётчика напоминаний
    про адрес, где потерянная попытка ничего не портит). Поэтому в репетиции
    обработчик ЧИТАЕТ записи, принимает решение по каждой и пишет его только
    в журнал действий (`store.log`, `dry_run=True` — как это делает движок) и
    в лог службы, — но НЕ ставит отметку о разборе, НЕ гасит связку
    (`status='cancelled'`) и НЕ зовёт `on_notify`. Иначе, включив функцию по-
    настоящему, владелец обнаружил бы, что все накопленные удаления уже
    «разобраны» репетицией — сделки, которые нужно было вернуть на этап,
    остались бы закрытыми навсегда. Одна и та же запись в репетиции
    возвращается в список неразобранных на каждом проходе — это ожидаемо,
    а не сбой; повторные записи в журнал действий безвредны, дублей писем
    владельцу при этом не будет вовсе (`on_notify` не вызывается).

    Что до самих действий в CRM (`move_lead`, `add_note`): их выполняет `amo`,
    и настоящую сеть трогает только боевой клиент — какой из двух передать
    сюда (`rehearsal_amo` или `live_amo`), решает `main.py` по тому же
    `dry_run`. Так что писать в амо в репетиции и без того нечем; `dry_run`
    этого класса нужен ИМЕННО для того, чтобы не закрепить эффект такой
    несостоявшейся записи в собственной базе.
    """

    def __init__(
        self,
        *,
        own_pool: asyncpg.Pool,
        amo: Any,
        dry_run: bool = True,
        on_notify: Optional[Callable[[DeletionOutcome], Awaitable[Any]]] = None,
    ) -> None:
        self.own_pool = own_pool
        self.amo = amo
        self.dry_run = dry_run
        self.on_notify = on_notify

    async def run(self) -> int:
        """Разобрать всё неразобранное. Возвращает, сколько записей разобрано.

        Сбой на одной записи (например, амо не ответила) не должен останавливать
        разбор остальных — запись просто не отмечается и вернётся в список на
        следующем проходе.
        """
        records = await fetch_pending_deletions(self.own_pool)
        handled = 0
        for record in records:
            try:
                await self._handle(record)
            except Exception:                          # noqa: BLE001
                log.exception("%s №%s: разбор удаления не удался",
                              _label(record.kind), record.order_id)
                continue
            handled += 1
        return handled

    # --- одна запись ---

    async def _handle(self, record: DeletionRecord) -> None:
        store = self._store(record.kind)
        link = await store.get(record.order_id)

        if link is None:                                            # ветка 1
            await self._settle(store, record, "no_link", cancel=False)
            return

        lead_id = link.real_lead_id or link.primary_lead_id
        if lead_id is None:                                          # см. докстринг класса
            await self._settle(store, record, "no_lead", cancel=True)
            return

        other = await self._find_other_live_holder(lead_id, record.kind, record.order_id)
        if other is not None:                                         # ветка 2
            other_kind, other_order_id = other
            await self._note(store, record, lead_id,
                              _note_text(record, "held_by_other",
                                         other_kind=other_kind, other_order_id=other_order_id))
            await self._settle(store, record, "held_by_other", cancel=True, link=link,
                                lead_id=lead_id, other_kind=other_kind,
                                other_order_id=other_order_id)
            return

        lead = await self.amo.get_lead(lead_id)
        if lead is None or lead.get("id") is None:                    # ветка 3 (факт 11)
            await self._settle(store, record, "lead_gone", cancel=True, link=link, lead_id=lead_id)
            return

        if int(lead.get("status_id") or 0) not in ids.STATUSES_FINAL:  # ветка 4
            await self._note(store, record, lead_id, _note_text(record, "left_open"))
            await self._settle(store, record, "left_open", cancel=True, link=link, lead_id=lead_id)
            return

        # ветка 5 — сделка закрыта, возвращаем на этап, снова делая её кандидатом
        await self._write(store, record.order_id, "move_lead", lead_id,
                           self.amo.move_lead(lead_id, ids.PIPELINE_REALIZATION,
                                              ids.REAL_STAGE_CONFIRMED))
        await self._note(store, record, lead_id, _note_text(record, "reopened"))
        await self._settle(store, record, "reopened", cancel=True, link=link, lead_id=lead_id)

    # --- внутреннее ---

    def _store(self, kind: str):
        return PgCleaningLinkStore(self.own_pool) if kind == "cleaning" else PgLinkStore(self.own_pool)

    async def _write(self, store: Any, order_id: int, action: str,
                      amo_id: Optional[int], coro: Awaitable[Any]) -> Any:
        """Выполнить действие в амо и записать его в журнал — как это делает
        движок (`sync/engine.py`, метод `_write`): в репетиции `intent.performed`
        будет False, а журнал (`amo_actions`/`cleaning_actions`) пишется всё равно."""
        intent = await coro
        if intent is not None:
            await store.log(order_id, action, dry_run=not intent.performed,
                             entity=intent.entity, amo_id=intent.entity_id or amo_id,
                             payload=intent.payload)
        return intent

    async def _note(self, store: Any, record: DeletionRecord, lead_id: int, text: str) -> None:
        await self._write(store, record.order_id, "add_note", lead_id,
                           self.amo.add_note(lead_id, text))

    async def _find_other_live_holder(self, lead_id: int, exclude_kind: str,
                                       exclude_order_id: int) -> Optional[tuple[str, int]]:
        async with self.own_pool.acquire() as conn:
            row = await conn.fetchrow(_OTHER_LIVE_HOLDER_SQL, lead_id, exclude_order_id,
                                       exclude_kind)
        return None if row is None else (row["kind"], row["order_id"])

    async def _settle(self, store: Any, record: DeletionRecord, outcome: str, *, cancel: bool,
                       link: Optional[AmoLink] = None, lead_id: Optional[int] = None,
                       other_kind: Optional[str] = None,
                       other_order_id: Optional[int] = None) -> None:
        """Общий хвост каждой ветки: погасить связку (решение 1), поставить
        отметку о разборе и сообщить владельцу — либо, в репетиции, не делать
        из этого ничего (см. докстринг класса про необратимость отметки).
        Действия в CRM и запись в журнал действий (`_write`/`_note`) к этому
        методу не относятся — они уже случились до его вызова, как положено
        и в бою, и в репетиции."""
        if self.dry_run:
            log.info("%s №%s: репетиция — решил бы %r, связку и отметку не трогаю, "
                     "владельцу не пишу", _label(record.kind), record.order_id, outcome)
            return
        if cancel:
            await store.update(record.order_id, **_CANCEL_FIELDS)
        await self._mark_seen(record, outcome)
        if link is not None:
            await self._notify(record, link, outcome, lead_id=lead_id,
                               other_kind=other_kind, other_order_id=other_order_id)

    async def _mark_seen(self, record: DeletionRecord, outcome: str) -> None:
        """Отметка о разборе — своя таблица (`adminbot.order_deletions_seen`,
        миграция 014): админ-бот не пишет в `public`, поэтому регистр удалений
        рабочего бота (`public.deleted_orders`) остаётся нетронутым."""
        async with self.own_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO adminbot.order_deletions_seen (kind, order_id, outcome)
                VALUES ($1, $2, $3)
                ON CONFLICT (kind, order_id) DO NOTHING
                """,
                record.kind, record.order_id, outcome,
            )

    async def _notify(self, record: DeletionRecord, link: AmoLink, outcome: str, *,
                       lead_id: Optional[int] = None, other_kind: Optional[str] = None,
                       other_order_id: Optional[int] = None) -> None:
        """Сообщение владельцу не должно ронять разбор — Telegram бывает недоступен,
        и `OwnerMail` сама превращает неудачу в долг (см. `tg/outbox.py`)."""
        if self.on_notify is None:
            return
        try:
            await self.on_notify(DeletionOutcome(
                record=record, link=link, outcome=outcome, lead_id=lead_id,
                other_kind=other_kind, other_order_id=other_order_id))
        except Exception:                                  # noqa: BLE001
            log.exception("%s №%s: сообщение о разборе удаления не ушло",
                          _label(record.kind), record.order_id)
