"""Наблюдатель автозвонка: обмен с amoCRM → новые заявки с сайта → движок.

Устройство прохода — по образцу `adminbot.gcal.watcher.CalendarWatcher`, три
тех же правила:

1. **Первый проход только запоминает момент.** На этапе «Новый лид» уже могут
   лежать заявки, заведённые до включения робота, — они дело владельца, а не
   робота (тот же принцип решения 8 из дизайна календаря). Поэтому первый
   проход не читает ни одной сделки, а лишь ставит закладку на «сейчас».
2. **Закладка двигается только вперёд и только по факту.** Пустой ответ амо
   закладку не трогает; непустой — двигает её на самую позднюю `created_at`
   среди пришедших сделок. Граница снова попадёт в следующий опрос (амо
   фильтрует по `>=`), но повторный лид не заведёт вторую цепочку: PK
   хранилища (`lead_id`) и явная проверка `store.get` гасят повтор.
3. **Одна сделка не роняет проход.** Заявки без телефона получают вежливый
   финал (`gave_up`) вместо падения, а незавершённые цепочки, отданные
   движку, обрабатываются в собственном try/except: сбой одной не мешает
   остальным.

Окно звонков (10:00–20:00) наблюдатель не считает и не трогает: это решает
движок (`adminbot.autocall.engine.AutocallEngine`) на своём шаге. Новая
цепочка заводится в `queued` с `next_action_at = NULL`, поэтому `store.due`
отдаёт её немедленно — движок сам решит, звонить сейчас или отложить до
открытия окна.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from adminbot.amo import ids
from adminbot.amo.fields import lead_contact_ids
from adminbot.autocall.chain import STATUS_GAVE_UP
from adminbot.autocall.leads import is_site_lead, lead_phone10

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutocallTickReport:
    """Итог одного прохода — для журнала и команды /status."""

    paused: bool = False
    first_run: bool = False                        # первый проход: только поставили закладку
    seen: int = 0                                  # сделок пришло из амо
    new_chains: int = 0                            # заведено новых цепочек
    no_phone: int = 0                              # сайтовых заявок без телефона
    processed: int = 0                             # цепочек отдано движку
    failures: tuple[tuple[int, str], ...] = ()      # (lead_id, ошибка)


class AutocallWatcher:
    def __init__(
        self,
        *,
        amo: Any,
        engine: Any,
        store: Any,
        is_enabled: Optional[Callable[[], Any]] = None,
        poll_interval_sec: int = 30,
        on_no_phone: Optional[Callable[[int], Awaitable[None]]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.amo = amo
        self.engine = engine
        self.store = store
        self.is_enabled = is_enabled
        self.poll_interval_sec = poll_interval_sec
        # Заявка без телефона — тупик: звонить некому. Владелец должен узнать
        # об этом сам, а не заметить постфактум в списке «gave_up».
        self.on_no_phone = on_no_phone
        self.sleep = sleep
        self.last_report: Optional[AutocallTickReport] = None

    async def tick(self) -> AutocallTickReport:
        """Один проход: забрать новые заявки с сайта и доделать незавершённое."""
        if not await self._enabled():
            return self._remember(AutocallTickReport(paused=True))

        now = datetime.now(timezone.utc)
        created_from = await self.store.cursor()

        if created_from is None:
            # Первый проход: лежащие на этапе заявки — дело владельца, робот
            # берёт в работу только то, что появится после включения.
            await self.store.save_cursor(now)
            return self._remember(AutocallTickReport(first_run=True))

        leads = await self.amo.find_leads_created_since(
            ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NEW_LEAD, int(created_from.timestamp()),
        )

        new_chains = 0
        no_phone = 0
        latest_created_at: Optional[datetime] = None
        # Контакты сделок за один проход не меняются — кэш экономит повторные
        # походы в амо за одним и тем же контактом (общий главный контакт
        # у двух заявок — редкость, но возможна).
        contacts_cache: dict[int, Optional[dict]] = {}

        for lead in leads:
            lead_id = int(lead["id"])
            latest_created_at = _max_created_at(latest_created_at, lead)

            if not is_site_lead(lead):
                continue                            # не заявка сайта — не наша
            if await self.store.get(lead_id) is not None:
                continue                            # уже заведена прошлым проходом

            contacts = await self._contacts_of(lead, contacts_cache)
            phone10 = lead_phone10(lead, contacts)

            if phone10:
                await self.store.create(lead_id, phone10=phone10)
                new_chains += 1
            else:
                await self._give_up_without_phone(lead_id)
                no_phone += 1

        # Закладка — только по факту пришедших сделок: пустой ответ её не двигает,
        # иначе следующий проход перечитал бы тот же (уже пустой) кусок амо.
        if leads and latest_created_at is not None:
            await self.store.save_cursor(latest_created_at)

        processed = 0
        failures: list[tuple[int, str]] = []
        for link in await self.store.due(now):
            processed += 1
            try:
                await self.engine.process_due(link, now)
            except Exception as exc:                # noqa: BLE001 — сделка не роняет проход
                log.exception("Автозвонок, сделка %s: проход прерван", link.lead_id)
                failures.append((link.lead_id, f"{type(exc).__name__}: {exc}"))

        return self._remember(AutocallTickReport(
            seen=len(leads), new_chains=new_chains, no_phone=no_phone,
            processed=processed, failures=tuple(failures),
        ))

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except Exception:                        # noqa: BLE001
                log.exception("Проход наблюдателя заявок с сайта не удался")
            await self.sleep(self.poll_interval_sec)

    # --- внутреннее ---

    async def _contacts_of(self, lead: Any, cache: dict[int, Optional[dict]]) -> list[dict]:
        """Контакты сделки целиком: в _embedded лежат только id и is_main."""
        contacts: list[dict] = []
        for contact_id in lead_contact_ids(lead):
            if contact_id not in cache:
                cache[contact_id] = await self.amo.get_contact(contact_id)
            contact = cache[contact_id]
            if contact is not None:
                contacts.append(contact)
        return contacts

    async def _give_up_without_phone(self, lead_id: int) -> None:
        """Телефон не извлёкся — звонить некому: цепочка закрывается сразу.

        Финал именно "gave_up", а не тихое отсутствие цепочки: заявка была,
        робот её видел и не смог обработать — это должно остаться в журнале,
        а не выглядеть так, будто заявки не было вовсе.
        """
        await self.store.create(lead_id, phone10=None)
        await self.store.update(lead_id, status=STATUS_GAVE_UP,
                                last_error="телефон из сделки не извлёкся")
        await self.store.log_action(lead_id, "no_phone", dry_run=False, payload=None)
        await self._notify_no_phone(lead_id)

    async def _notify_no_phone(self, lead_id: int) -> None:
        """Сообщение владельцу не должно ронять проход: Telegram бывает недоступен."""
        if self.on_no_phone is None:
            return
        try:
            await self.on_no_phone(lead_id)
        except Exception:                            # noqa: BLE001
            log.exception(
                "Автозвонок, сделка %s: уведомление о заявке без телефона не ушло", lead_id,
            )

    def _remember(self, report: AutocallTickReport) -> AutocallTickReport:
        self.last_report = report
        return report

    async def _enabled(self) -> bool:
        if self.is_enabled is None:
            return True
        result = self.is_enabled()
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)


def _max_created_at(current: Optional[datetime], lead: Any) -> Optional[datetime]:
    """Самая поздняя created_at среди пришедших сделок — новая граница закладки."""
    raw = lead.get("created_at") if isinstance(lead, dict) else None
    if raw is None:
        return current
    moment = datetime.fromtimestamp(int(raw), tz=timezone.utc)
    if current is None or moment > current:
        return moment
    return current
