"""Заявка на звонок по отклику на промо → контакт → сделка с тегом (ТЗ 2026-09-23, задача 5).

Рабочий бот пишет строку в `public.promo_callbacks`, когда клиент или лид
ответил чистой «1» на промо (миграция рабочего бота 0015). Этот цикл раз в
минуту берёт новые строки после своей закладки и по каждой:

1. разбирает номер (`phone.last10`); номера нет — `failed` и письмо владельцу;
2. находит контакт по телефону или заводит новый;
3. заводит сделку «Отклик на промо — <имя>» в «Новом лиде» воронки 1 с тегом
   «Отклик на промо» и сразу запоминает её номер — повтор после сбоя
   продолжает с места и вторую сделку не заводит;
4. пишет в сделку примечание, что ответил клиент и когда.

Дальше сделку ведёт автозвонок сам — он берёт сделки с этим тегом наравне с
заявками с сайта (решение владельца 7). Об автозвонке модуль ничего не знает.

Устройство — как у `sync/wire_payment.py`: источник, цикл, отчёт. Что уже
обработано, помнит своё хранилище (схема `adminbot`, миграция 018): у
репетиции и у боя свои строки и своя закладка.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence

from adminbot.amo import ids
from adminbot.amo.fields import MOSCOW_TZ, contact_phones
from adminbot.autocall.leads import PROMO_TAG
from adminbot.phone import last10, mask

log = logging.getLogger(__name__)

# Раз в минуту: отклик ждёт звонка, но автозвонок и так опрашивает CRM раз в
# 30 секунд — чаще читать заявки смысла нет.
DEFAULT_POLL_INTERVAL_SEC = 60
# Сколько проходов подряд может упасть одна заявка, прежде чем робот сдастся
# и попросит владельца позвонить руками.
MAX_ATTEMPTS = 3
# Сколько новых заявок берём за один проход.
BATCH_LIMIT = 100

MODE_LIVE = "live"
MODE_REHEARSAL = "rehearsal"

# Статусы строки состояния (см. шапку migrations/018_promo_callback.sql).
STATUS_NEW = "new"
STATUS_LEAD_CREATED = "lead_created"
STATUS_QUEUED = "queued"
STATUS_DRY_RUN = "dry_run"
STATUS_FAILED = "failed"
OPEN_STATUSES = (STATUS_NEW, STATUS_LEAD_CREATED)

# Имя контакта, если в карточке бота имени нет, — как у остальных движков.
FALLBACK_NAME = "Клиент"


@dataclass(frozen=True)
class PromoCallback:
    """Строка `public.promo_callbacks` — заявка рабочего бота."""

    id: int
    source: str                        # 'client' | 'lead'
    phone: str
    name: Optional[str] = None
    response_text: Optional[str] = None
    created_at: Optional[datetime] = None
    client_id: Optional[int] = None
    lead_id: Optional[int] = None


@dataclass(frozen=True)
class PromoCallbackState:
    """Строка `adminbot.promo_callback_state` — что робот сделал по заявке."""

    callback_id: int
    mode: str
    status: str = STATUS_NEW
    contact_id: Optional[int] = None
    lead_id: Optional[int] = None
    attempts: int = 0
    last_error: Optional[str] = None


class PromoCallbackSource(Protocol):
    """Откуда цикл берёт заявки (схема `public`, только чтение)."""

    async def max_id(self) -> int: ...

    async def after(self, last_id: int, limit: int) -> list[PromoCallback]: ...

    async def by_ids(self, callback_ids: Sequence[int]) -> list[PromoCallback]: ...


class PromoCallbackStore(Protocol):
    """Что робот помнит о заявках (схема `adminbot`). Всё — в пределах режима."""

    async def cursor(self, mode: str) -> Optional[int]: ...

    async def save_cursor(self, mode: str, last_id: int) -> None: ...

    async def register(self, mode: str, callback_ids: Sequence[int]) -> None: ...

    async def pending(self, mode: str) -> list[PromoCallbackState]: ...

    async def update(self, callback_id: int, mode: str, **fields: Any) -> None: ...


def deal_name(callback: PromoCallback) -> str:
    """Название сделки — «Отклик на промо — <имя>» (принято координатором, п.1)."""
    return f"Отклик на промо — {_name(callback)}"


def note_text(callback: PromoCallback) -> str:
    """Примечание в сделку: что и когда ответил человек и кто он для бота."""
    answer = (callback.response_text or "").strip() or "1"
    when = (callback.created_at.astimezone(MOSCOW_TZ).strftime("%d.%m %H:%M")
            if callback.created_at is not None else "")
    who = "Клиент" if callback.source == "client" else "Лид"
    head = f"🤖 Клиент ответил «{answer}» на промо"
    return f"{head} {when}. {who} из бота." if when else f"{head}. {who} из бота."


class PromoCallbackSync:
    """Раз в `poll_interval_sec` — новые заявки после закладки и незаконченные."""

    def __init__(
        self,
        *,
        source: PromoCallbackSource,
        store: PromoCallbackStore,
        amo: Any,
        dry_run: bool = True,
        on_rehearsal: Optional[Callable[[PromoCallback, Optional[int]], Awaitable[Any]]] = None,
        on_failure: Optional[Callable[[PromoCallback, Optional[int], str], Awaitable[Any]]] = None,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.source = source
        self.store = store
        # Репетиция получает `rehearsal_amo`: читает CRM по-настоящему, писать не
        # может. Сам цикл в репетиции методы записи и не зовёт.
        self.amo = amo
        self.dry_run = dry_run
        self.mode = MODE_REHEARSAL if dry_run else MODE_LIVE
        # Репетиция: письмо владельцу по каждой заявке. Бой: писем по успешным
        # заявкам нет (как у заявок с сайта), только по сбоям — on_failure.
        self.on_rehearsal = on_rehearsal
        self.on_failure = on_failure
        self.poll_interval_sec = poll_interval_sec
        self.sleep = sleep

    async def tick(self) -> int:
        """Один проход. Возвращает, сколько заявок доведено до конца."""
        cursor = await self.store.cursor(self.mode)
        if cursor is None:
            # Первый проход в этом режиме: отклики до включения уже обработали
            # люди по старому сообщению админам — встаём на текущий максимум.
            top = await self.source.max_id()
            await self.store.save_cursor(self.mode, top)
            log.info("Отклики на промо (%s): закладка поставлена на заявку №%s, "
                     "старые заявки не трогаю", self.mode, top)
            return 0

        fresh = await self.source.after(cursor, BATCH_LIMIT)
        if fresh:
            # Сначала строки состояния, потом закладка: упадём между ними —
            # следующий проход увидит те же заявки, а повторная регистрация
            # ничего не меняет.
            await self.store.register(self.mode, [item.id for item in fresh])
            await self.store.save_cursor(self.mode, max(item.id for item in fresh))

        pending = await self.store.pending(self.mode)
        if not pending:
            return 0
        found = await self.source.by_ids([state.callback_id for state in pending])
        by_id = {item.id: item for item in found}

        done = 0
        for state in pending:
            callback = by_id.get(state.callback_id)
            if callback is None:
                # Рабочий бот строки не удаляет (договорённость миграции 0015);
                # пропала — значит, удалили руками, и делать по ней нечего.
                log.warning("Отклик на промо №%s: заявки нет в рабочем боте, пропускаю",
                            state.callback_id)
                await self.store.update(state.callback_id, self.mode, status=STATUS_FAILED,
                                        last_error="заявки нет в public.promo_callbacks")
                continue
            if await self._handle(callback, state):
                done += 1
        return done

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except Exception:                          # noqa: BLE001
                log.exception("Отклики на промо: проход не удался")
            await self.sleep(self.poll_interval_sec)

    # --- одна заявка ---

    async def _handle(self, callback: PromoCallback, state: PromoCallbackState) -> bool:
        """Довести заявку до конца. True — доведена; False — сбой или отказ."""
        phone10 = last10(callback.phone)
        if phone10 is None:
            error = f"номер «{callback.phone}» не распознан"
            log.warning("Отклик на промо №%s: номер не распознан", callback.id)
            await self._save(state, status=STATUS_FAILED, last_error=error)
            await self._report_failure(callback, None, error)
            return False

        current = state
        try:
            if self.dry_run:
                contact_id = await self._find_contact(phone10)
                await self._report_rehearsal(callback, contact_id)
                current = await self._save(current, status=STATUS_DRY_RUN,
                                           contact_id=contact_id)
                return True

            if current.lead_id is None:
                contact_id = current.contact_id
                if contact_id is None:
                    contact_id = await self._find_contact(phone10)
                if contact_id is None:
                    created = await self.amo.create_contact(name=_name(callback),
                                                            phone="+7" + phone10)
                    contact_id = created.entity_id
                    if contact_id is None:
                        raise RuntimeError("amoCRM не вернула номер нового контакта")
                    # Сразу запоминаем: повтор после сбоя не заведёт второй контакт.
                    current = await self._save(current, contact_id=contact_id)
                elif current.contact_id is None:
                    current = await self._save(current, contact_id=contact_id)

                intent = await self.amo.create_lead(
                    name=deal_name(callback), pipeline_id=ids.PIPELINE_PRIMARY,
                    status_id=ids.PRIM_STAGE_NEW_LEAD, contact_id=contact_id,
                    tags=[PROMO_TAG])
                if intent.entity_id is None:
                    raise RuntimeError("amoCRM не вернула номер новой сделки")
                # Номер сделки — в базу сразу после создания (ТЗ, задача 5, п.4).
                current = await self._save(current, lead_id=intent.entity_id,
                                           status=STATUS_LEAD_CREATED)

            await self.amo.add_note(current.lead_id, note_text(callback))
            current = await self._save(current, status=STATUS_QUEUED, last_error=None)
            log.info("Отклик на промо №%s (%s): сделка #%s в «Новом лиде»",
                     callback.id, mask(callback.phone), current.lead_id)
            return True
        except Exception as exc:                       # noqa: BLE001 — сбой CRM или базы
            await self._fail(callback, current, exc)
            return False

    async def _find_contact(self, phone10: str) -> Optional[int]:
        """Контакт, у которого среди телефонов есть этот номер.

        Амо ищет подстрокой, поэтому найденных проверяем сами: берём первый,
        у кого номер совпадает целиком (10 цифр).
        """
        for contact in await self.amo.find_contacts_by_phone(phone10):
            if phone10 in contact_phones(contact) and contact.get("id") is not None:
                return int(contact["id"])
        return None

    async def _fail(self, callback: PromoCallback, state: PromoCallbackState,
                    exc: Exception) -> None:
        """Сбой: следующий проход повторит; третий подряд — `failed` и письмо."""
        attempts = state.attempts + 1
        error = str(exc) or type(exc).__name__
        log.warning("Отклик на промо №%s (%s): попытка %s/%s не удалась: %s",
                    callback.id, mask(callback.phone), attempts, MAX_ATTEMPTS, error)
        if attempts < MAX_ATTEMPTS:
            await self._save_quietly(state, attempts=attempts, last_error=error)
            return
        await self._save_quietly(state, attempts=attempts, last_error=error,
                                 status=STATUS_FAILED)
        await self._report_failure(callback, state.lead_id, error)

    async def _save(self, state: PromoCallbackState, **fields: Any) -> PromoCallbackState:
        await self.store.update(state.callback_id, state.mode, **fields)
        return replace(state, **fields)

    async def _save_quietly(self, state: PromoCallbackState, **fields: Any) -> None:
        """Отметка сбоя сама не должна ронять проход: база бывает недоступна."""
        try:
            await self.store.update(state.callback_id, state.mode, **fields)
        except Exception:                              # noqa: BLE001
            log.exception("Отклик на промо №%s: не смог записать отметку о сбое",
                          state.callback_id)

    async def _report_rehearsal(self, callback: PromoCallback,
                                contact_id: Optional[int]) -> None:
        if self.on_rehearsal is None:
            return
        try:
            await self.on_rehearsal(callback, contact_id)
        except Exception:                              # noqa: BLE001
            log.exception("Отклик на промо №%s: отчёт репетиции не ушёл", callback.id)

    async def _report_failure(self, callback: PromoCallback, lead_id: Optional[int],
                              error: str) -> None:
        if self.on_failure is None:
            return
        try:
            await self.on_failure(callback, lead_id, error)
        except Exception:                              # noqa: BLE001
            log.exception("Отклик на промо №%s: письмо о сбое не ушло", callback.id)


def _name(callback: PromoCallback) -> str:
    return (callback.name or "").strip() or FALLBACK_NAME
