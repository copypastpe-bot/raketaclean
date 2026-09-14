"""Движок автозвонка: связывает машину попыток (chain.py) с АТС и амо.

Устройство то же, что у движков заказов и календаря: движок не знает ни про
Postgres, ни про сеть напрямую — у него есть хранилище (store), телефония
(pbx) и клиент amoCRM (amo). Собственно решения «что делать дальше» уже
приняты в chain.py; здесь их только исполняют.

Три вещи, которые определяют устройство `process_due`:

1. **Намерение фиксируется до звонка.** Прежде чем попросить АТС набрать
   номер, движок сначала переводит цепочку в статус "calling" и уже
   увеличивает счётчик попыток. Если процесс упадёт между этой записью и
   ответом АТС (обрыв сети, рестарт сервиса, что угодно), к попытке никогда
   не вернутся дважды: при следующем проходе цепочка обнаружится в
   "calling" без call_id, исход взять неоткуда, и через
   CALL_OUTCOME_TIMEOUT_SEC она сама уйдёт в Outcome.UNKNOWN — как и
   положено пропавшей попытке, а не тихо забудется и не позвонит второй раз
   за ту же попытку.
2. **Репетиция не звонит и не оставляет следов в амо.** Она проговаривает
   «позвонил бы» владельцу один раз и закрывает цепочку — иначе тот же тик
   повторял бы «позвонил бы» бесконечно.
3. **Неизвестный исход = неудача менеджера.** Робот никогда не звонит
   бесконтрольно: если история АТС не отвечает дольше CALL_OUTCOME_TIMEOUT_SEC,
   движок считает попытку неудачной для менеджера и отдаёт её машине
   переходов как Outcome.UNKNOWN — chain.py дальше обращается с ней так же,
   как с любой другой неудачей менеджера.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional, Sequence

import aiohttp

from adminbot.amo import ids
from adminbot.amo.client import AmoError
from adminbot.autocall.chain import (
    FINAL_STATUSES,
    STATUS_CALLING,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_GAVE_UP,
    STATUS_QUEUED,
    Chain,
    Done,
    GaveUp,
    MoveLeadNoContact,
    NotifyManager,
    Outcome,
    Retry,
    advance,
)
from adminbot.autocall.notes import deal_note_text
from adminbot.autocall.pbx import Pbx
from adminbot.autocall.store import AutocallStore
from adminbot.autocall.window import next_call_moment
from adminbot.models import AutocallLead

log = logging.getLogger(__name__)

#: Сколько ждём исход попытки из истории АТС, прежде чем считать её
#: пропавшей (Outcome.UNKNOWN). Решение плана: молчание АТС — тоже
#: неудача менеджера, а не повод звонить клиенту ещё раз наугад.
CALL_OUTCOME_TIMEOUT_SEC = 300

#: Сетевые сбои внутри process_due не роняют движок: цепочка уходит в
#: STATUS_ERROR и вернётся к работе через минуту (наблюдатель её найдёт
#: снова через store.due). Список — конкретные типы, а не голый Exception:
#: программный баг должен падать и быть заметным, а не тихо превращаться
#: в "error" со следующей попыткой через 60 секунд.
_TRANSIENT_ERRORS = (AmoError, aiohttp.ClientError, asyncio.TimeoutError, OSError)

#: Пауза перед повторной попыткой после сетевого сбоя.
_ERROR_RETRY = timedelta(seconds=60)

NotifyManagerFn = Callable[[int, str], Awaitable[None]]
NotifyOwnerRehearsalFn = Callable[[int, Optional[str]], Awaitable[None]]
NotifyOwnerConnectedFn = Callable[[int], Awaitable[None]]


class AutocallEngine:
    """Продвигает одну цепочку автозвонка на один шаг за вызов `process_due`."""

    def __init__(
        self,
        *,
        pbx: Pbx,
        amo: Any,
        store: AutocallStore,
        manager_dials: Sequence[str],
        window_from_hour: int = 10,
        window_to_hour: int = 20,
        notify_manager: Optional[NotifyManagerFn] = None,
        notify_owner_rehearsal: Optional[NotifyOwnerRehearsalFn] = None,
        notify_owner_connected: Optional[NotifyOwnerConnectedFn] = None,
        dry_run: bool = True,
    ) -> None:
        self.pbx = pbx
        self.amo = amo
        self.store = store
        # Телефоны менеджера по порядку: первый — рабочий, следующий — личный.
        # Решение владельца 2026-09-02: не взял рабочий — повтор идёт на личный.
        self.manager_dials = tuple(manager_dials)
        self.window_from_hour = window_from_hour
        self.window_to_hour = window_to_hour
        self.notify_manager = notify_manager
        self.notify_owner_rehearsal = notify_owner_rehearsal
        # Отчёт владельцу о состоявшемся соединении — неделя наблюдения
        # (дизайн §6.5): по каждой доведённой до конца цепочке он видит одно
        # сообщение со ссылкой на сделку, как и в уборке/календаре.
        self.notify_owner_connected = notify_owner_connected
        self.dry_run = dry_run

    async def process_due(self, link: AutocallLead, now: datetime) -> None:
        """Продвинуть одну цепочку (см. модульную докстрину — три инварианта выше).

        `link` — запись из `store.due(now)`: наблюдатель находит её сам, движок
        только исполняет один шаг. Финальные статусы (done|no_contact|gave_up)
        сюда попадать не должны — `due` их не отдаёт; если всё же пришли (баг
        вызывающего), выходим тихо с предупреждением в лог, а не падаем.
        """
        if link.status in FINAL_STATUSES:
            log.warning(
                "Автозвонок, сделка %s: process_due вызван по закрытой цепочке "
                "(статус %s) — выхожу без изменений", link.lead_id, link.status,
            )
            return

        try:
            if link.status == STATUS_ERROR:
                await self._recover_error(link, now)
            elif link.status == STATUS_QUEUED:
                await self._process_queued(link, now)
            elif link.status == STATUS_CALLING:
                await self._process_calling(link, now)
            else:
                log.warning(
                    "Автозвонок, сделка %s: незнакомый статус %s — выхожу без изменений",
                    link.lead_id, link.status,
                )
        except _TRANSIENT_ERRORS as exc:
            await self.store.update(
                link.lead_id, status=STATUS_ERROR, last_error=str(exc),
                next_action_at=now + _ERROR_RETRY,
            )
            await self.store.log_action(
                link.lead_id, "error", dry_run=self.dry_run, payload={"error": str(exc)},
            )

    # --- восстановление после сбоя ---

    async def _recover_error(self, link: AutocallLead, now: datetime) -> None:
        """Статус "error" не знает сам, на каком шаге упал — судим по фактам.

        called_at заполнен → команда АТС уже отдавалась (или отдача сорвалась
        прямо на ней) — довести как "calling". Иначе сбой был раньше самого
        звонка — довести как "queued". last_error чистим сразу: если
        восстановление снова упадёт, ошибку запишет заново тот же обработчик.
        """
        cleared = await self.store.update(link.lead_id, last_error=None)
        if cleared is None:
            # Симметрично с другими аномалиями (финальный/незнакомый статус):
            # запись пропала из хранилища — не падаем, но не молчим об этом.
            log.warning(
                "Автозвонок, сделка %s: не удалось снять last_error — "
                "записи нет в хранилище", link.lead_id,
            )
        recovered = cleared if cleared is not None else link
        if recovered.called_at is not None:
            await self._process_calling(recovered, now)
        else:
            await self._process_queued(recovered, now)

    # --- статус "queued" ---

    async def _process_queued(self, link: AutocallLead, now: datetime) -> None:
        moment = next_call_moment(
            now, now=now, from_hour=self.window_from_hour, to_hour=self.window_to_hour,
        )
        if moment > now:
            # Ночь или вечер — звонить нельзя, заявка ждёт открытия окна.
            await self.store.update(link.lead_id, next_action_at=moment)
            return

        if self.dry_run:
            # Репетиция не звонит и не пишет в амо (см. модульную докстрину,
            # пункт 2): один раз «позвонил бы» — и цепочка закрыта, иначе
            # каждый следующий тик повторял бы то же самое владельцу.
            await self.store.log_action(
                link.lead_id, "would_call", dry_run=True, payload={"phone10": link.phone10},
            )
            if self.notify_owner_rehearsal is not None:
                await self.notify_owner_rehearsal(link.lead_id, link.phone10)
            await self.store.update(link.lead_id, status=STATUS_DONE)
            return

        # Бой. Последний рубеж перед командой АТС: что бы ни лежало в
        # хранилище (ручная правка записи, сбой ровно между шагами создания
        # цепочки без телефона), звонить нечем — движок не должен передавать
        # АТС пустой client_phone. Наблюдатель уже не заводит такую цепочку
        # живой ("queued"), но полагаться только на источник неправильно:
        # опечатка в другом месте не должна приводить к звонку в никуда.
        if not link.phone10:
            await self.store.update(
                link.lead_id, status=STATUS_GAVE_UP,
                last_error="звонок без телефона невозможен",
            )
            await self.store.log_action(
                link.lead_id, "no_phone", dry_run=self.dry_run, payload=None,
            )
            return

        # Намерение — ДО команды АТС (инвариант №1 из модульной докстрины):
        # упади мы между этой записью и звонком, второй звонок по той же
        # попытке не уйдёт — цепочка обнаружится в "calling" без call_id и
        # уйдёт в Outcome.UNKNOWN по таймауту, а не позвонит ещё раз.
        await self.store.update(
            link.lead_id, status=STATUS_CALLING, called_at=now,
            attempts_total=link.attempts_total + 1, next_action_at=None, call_id=None,
        )
        call_id = await self.pbx.call_now(
            to_dial=self._dial_for(link), client_phone=link.phone10,
        )
        await self.store.update(link.lead_id, call_id=call_id)
        await self.store.log_action(
            link.lead_id, "call_started", dry_run=self.dry_run, payload={"call_id": call_id},
        )

    def _dial_for(self, link: AutocallLead) -> str:
        """На какой телефон менеджера звонить в этой попытке.

        Счёт идёт по НЕУДАЧАМ менеджера, а не по попыткам вообще: если
        трубку не взял клиент, менеджер ни при чём — повтор снова идёт на
        его рабочий номер. Список короче числа неудач (например, телефон
        задан один) — остаёмся на последнем.

        Пустой список проверяется здесь, а не в конструкторе: репетиция
        никому не звонит и обязана подниматься даже с незаполненными
        настройками АТС — иначе правка уронила бы работающую службу.
        """
        if not self.manager_dials:
            raise ValueError(
                "звонок невозможен: не задан ни один телефон менеджера "
                "(PBX_MANAGER_DIAL)"
            )
        index = min(link.manager_failures, len(self.manager_dials) - 1)
        return self.manager_dials[index]

    # --- статус "calling" ---

    async def _process_calling(self, link: AutocallLead, now: datetime) -> None:
        outcome: Optional[Outcome] = None
        if link.call_id is not None:
            outcome = await self.pbx.call_outcome(link.call_id, called_at=link.called_at)

        if outcome is None:
            elapsed = (now - link.called_at).total_seconds() if link.called_at else None
            if elapsed is not None and elapsed < CALL_OUTCOME_TIMEOUT_SEC:
                return  # исхода ещё нет — спросим на следующем тике
            outcome = Outcome.UNKNOWN  # история АТС молчит: неудача менеджера

        await self.store.log_action(
            link.lead_id, "outcome", dry_run=self.dry_run, payload={"outcome": outcome.value},
        )

        gave_up_reason: Optional[str] = None
        chain = Chain(
            lead_id=link.lead_id, status=STATUS_CALLING,
            attempts_total=link.attempts_total, manager_failures=link.manager_failures,
            client_failures=link.client_failures, next_action_at=link.next_action_at,
        )
        new_chain, effects = advance(chain, outcome, now)

        # Один store.update на весь исход — не по одному на эффект. next_action_at
        # берём из new_chain (для Done/GaveUp/переноса он уже None); Retry —
        # единственный эффект, который его переопределяет через окно звонков.
        update_fields: dict[str, Any] = {
            "status": new_chain.status,
            "attempts_total": new_chain.attempts_total,
            "manager_failures": new_chain.manager_failures,
            "client_failures": new_chain.client_failures,
            "next_action_at": new_chain.next_action_at,
        }

        for effect in effects:
            if isinstance(effect, Retry):
                # Повтор уважает окно звонков: желаемый момент из chain.py —
                # ещё не то, что можно записать в next_action_at напрямую.
                update_fields["next_action_at"] = next_call_moment(
                    effect.at, now=now,
                    from_hour=self.window_from_hour, to_hour=self.window_to_hour,
                )
                # called_at/call_id от только что разобранной попытки не должны
                # пережить возврат в очередь (по образцу боевой ветки
                # _process_queued, которая всегда сбрасывает их перед новым
                # звонком). Иначе сбой ровно на записи намерения следующей
                # попытки оставит в строке status=error со СТАРЫМ call_id —
                # и _recover_error примет его за «попытка ещё идёт», переспросит
                # АТС по этому call_id и засчитает один и тот же исход дважды,
                # не сделав ни одного нового звонка.
                update_fields["called_at"] = None
                update_fields["call_id"] = None
            elif isinstance(effect, NotifyManager):
                await self._notify_manager(link.lead_id, effect.kind)
            elif isinstance(effect, MoveLeadNoContact):
                await self.amo.move_lead(
                    link.lead_id, ids.PIPELINE_PRIMARY, ids.PRIM_STAGE_NO_CONTACT,
                )
                await self.store.log_action(
                    link.lead_id, "move_no_contact", dry_run=self.dry_run, payload=None,
                )
            elif isinstance(effect, GaveUp):
                gave_up_reason = effect.reason
                await self.store.log_action(
                    link.lead_id, "gave_up", dry_run=self.dry_run,
                    payload={"reason": effect.reason},
                )
            elif isinstance(effect, Done):
                await self._notify_connected(link.lead_id)

        await self.store.update(link.lead_id, **update_fields)

        if new_chain.status in FINAL_STATUSES:
            await self._leave_deal_note(link, new_chain.status, gave_up_reason)

    # --- вспомогательное ---

    async def _leave_deal_note(
        self, link: AutocallLead, status: str, reason: Optional[str],
    ) -> None:
        """Написать в сделку, чем кончилась цепочка: один раз, при финале.

        Зачем: звонок робота не попадает в карточку сделки — связка АТС с амо
        раскладывает звонки по внутренним номерам и знает только 100 «Амо», а
        робот звонит менеджеру на мобильный. На живой заявке 2026-09-02 вызов
        в АТС записан, а в амо его нет. Пока это не решено на стороне
        onlinePBX, след в сделке оставляет сам робот (решение владельца).

        Упавшее примечание не роняет цепочку — по образцу `_notify_manager`:
        след для человека не должен отменять уже принятое решение робота.
        """
        try:
            text = deal_note_text(status, called_at=link.called_at, reason=reason)
            await self.amo.add_note(link.lead_id, text)
        except Exception:  # noqa: BLE001 — примечание не важнее самой цепочки
            log.exception(
                "Автозвонок, сделка %s: примечание об итоге (%s) не записано",
                link.lead_id, status,
            )
            return
        await self.store.log_action(
            link.lead_id, "deal_note", dry_run=self.dry_run, payload={"status": status},
        )

    async def _notify_manager(self, lead_id: int, kind: str) -> None:
        """Сообщение менеджеру не должно ронять цепочку: транспорт бывает недоступен.

        Недоставленное сообщение — не повод останавливать автозвонок или
        уводить сделку в "error": владелец узнает о сбое из журнала, а
        цепочка продолжит жить по своим правилам как ни в чём не бывало.
        """
        if self.notify_manager is None:
            return
        try:
            await self.notify_manager(lead_id, kind)
        except Exception as exc:  # noqa: BLE001 — недоставленное сообщение не роняет цепочку
            log.exception(
                "Автозвонок, сделка %s: сообщение менеджеру (%s) не ушло", lead_id, kind,
            )
            await self.store.log_action(
                lead_id, "notify_failed", dry_run=self.dry_run,
                payload={"kind": kind, "error": str(exc)},
            )
            return
        await self.store.log_action(
            lead_id, "notify", dry_run=self.dry_run, payload={"kind": kind},
        )

    async def _notify_connected(self, lead_id: int) -> None:
        """Отчёт владельцу о соединении — неделя наблюдения (дизайн §6.5).

        Тот же принцип, что и у сообщения менеджеру: недоставленный отчёт не
        должен ронять цепочку. Цепочка к этому моменту уже закрыта (done),
        поэтому сбой отчёта не мешает самой работе — только журналу.
        """
        if self.notify_owner_connected is None:
            return
        try:
            await self.notify_owner_connected(lead_id)
        except Exception as exc:  # noqa: BLE001 — недоставленный отчёт не роняет цепочку
            log.exception(
                "Автозвонок, сделка %s: отчёт владельцу о соединении не ушёл", lead_id,
            )
            await self.store.log_action(
                lead_id, "notify_failed", dry_run=self.dry_run,
                payload={"kind": "connected", "error": str(exc)},
            )
            return
        await self.store.log_action(
            lead_id, "notify", dry_run=self.dry_run, payload={"kind": "connected"},
        )
