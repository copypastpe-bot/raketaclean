"""Движок календаря: от записи в блокноте владельца до заполненной сделки.

Устройство то же, что у движка заказов: движок не знает ни про Postgres, ни про
сеть, у него два подручных — хранилище и клиент amoCRM. После каждого шага
прогресс пишется в чек-лист, поэтому сбой означает «продолжим отсюда», а не
«начнём заново».

Чем работа по календарю отличается от работы по заказу из бота:

- **Заказ ещё не выполнен.** Робот готовит сделку, но не проводит её: доводит до
  «Заказ оформлен» и останавливается (решение владельца 13). Мастера он не знает,
  автозадачу «Назначь мастера» не закрывает — это работа владельца.
- **Бюджет не трогаем.** Сумма чека появится только когда мастер закроет заказ
  в боте; движок amo_sync тогда её и проставит.
- **Заполненное не переписываем.** Правки владельца в CRM важнее догадок робота,
  а перенос даты в сделке робот не правит намеренно (решение 5): фактическую дату
  впишет заказ из бота.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any, Callable, Optional

from adminbot.amo import ids
from adminbot.amo.client import AmoError
from adminbot.amo.fields import (
    MOSCOW_TZ, datetime_field, enums_field, field_value, lead_contact_ids,
    order_date_msk, text_field,
)
from adminbot.gcal.event import EventKind, ParsedEvent
from adminbot.gcal.matcher import match_event
from adminbot.gcal.store import CalendarStore
from adminbot.models import CalendarLink
from adminbot.phone import mask, normalize_phone
from adminbot.sync.matcher import LeadInfo

log = logging.getLogger(__name__)

# Сколько ждём автосделку сейлзбота, прежде чем спросить владельца. Столько же,
# сколько в amo_sync: владелец видел задержки амо до 20 минут, берём запас вдвое.
DEFAULT_SALESBOT_WAIT_SEC = 2400

# Вид работ из записи → значение списка «Услуга» в амо (решение владельца 11).
SERVICE_ENUMS: dict[str, int] = {
    "mattress": ids.SERVICE_ENUM_MATTRESS,
    "furniture": ids.SERVICE_ENUM_FURNITURE,
    "carpeting": ids.SERVICE_ENUM_CARPETING,
    "rug_home": ids.SERVICE_ENUM_RUG_HOME,
    "cleaning": ids.SERVICE_ENUM_CLEANING,
    "windows": ids.SERVICE_ENUM_WINDOWS,
}

# Пометка записи, которая лежала в календаре ДО включения функции. Такие заказы
# владелец ведёт сам (его решение 8), и правка записи не возвращает её в работу:
# иначе первое же изменение состава завело бы сделку по чужой работе.
KEPT_OUT_REASON = "была в календаре до включения"


def _kept_out_of_work(link: CalendarLink) -> bool:
    return (link.skip_reason or "").startswith(KEPT_OUT_REASON)


# Виды записей, по которым робот молчит, и почему.
SILENT_KINDS: dict[EventKind, str] = {
    EventKind.BLOCK: "выходной мастера",
    EventKind.REWASH: "перемыв по гарантии — сделка не нужна",
    EventKind.SKIP: "телефон не найден",
    EventKind.UNSETTLED: "дата не подтверждена — ждём, пока снимут пометку",
}

# Путь А: сделка в воронке реализации уже есть — дозаполнить и оставить.
PATH_A: tuple[str, ...] = ("fill_realization", "fix_contact_name", "note_from_calendar")
# Путь Б: есть лид первичной — заполнить, передать в работу, дождаться автосделки.
PATH_B: tuple[str, ...] = (
    "note_duplicates", "fill_primary", "move_primary_success", "wait_salesbot") + PATH_A
# Путь В: сделки нет вовсе — контакт, лид, дальше как в пути Б.
PATH_C: tuple[str, ...] = (
    "ensure_contact", "create_primary_lead", "move_primary_success",
    "wait_salesbot") + PATH_A

# Путь теплохода: заводим только сделку на юрлицо, и то по кнопке владельца.
# Ни телефона, ни цены в записи нет, передавать такое в работу нечему.
PATH_BOAT: tuple[str, ...] = ("create_boat_lead",)

PATHS: dict[str, tuple[str, ...]] = {"A": PATH_A, "B": PATH_B, "C": PATH_C,
                                     "BOAT": PATH_BOAT}

_PATH_BY_KIND = {"use_realization": "A", "use_primary": "B", "create_new": "C"}
_ASK_KINDS = ("ask_owner", "ask_owner_stale")

# Шаги, которые делаются не всегда.
_CONDITIONAL = {"note_duplicates": lambda link, scratch: bool(scratch.duplicates)}


@dataclass
class _Scratch:
    """Черновые заметки в пределах одной записи: они не переживают проход."""

    duplicates: tuple[int, ...] = ()
    contact_id: Optional[int] = None
    is_new_client: bool = True


@dataclass
class StepResult:
    done: bool = True
    wait: bool = False
    ask: Optional[str] = None


class CalendarEngine:
    def __init__(
        self,
        *,
        amo: Any,
        store: CalendarStore,
        dry_run: bool = True,
        salesbot_wait_sec: int = DEFAULT_SALESBOT_WAIT_SEC,
        now: Callable[[], datetime] = lambda: datetime.now(MOSCOW_TZ),
    ) -> None:
        self.amo = amo
        self.store = store
        self.dry_run = dry_run
        self.salesbot_wait_sec = salesbot_wait_sec
        self.now = now
        # У каждого движка свои черновики: в сервисе их работает несколько сразу
        # (наблюдатель, репетиция), и путать их расчёты нельзя.
        self._scratch: dict[str, _Scratch] = {}

    # --- основной ход ---

    async def process(self, event: ParsedEvent) -> CalendarLink:
        """Продвинуть запись календаря настолько, насколько возможно сейчас."""
        link = await self.store.get(event.event_id)
        if link is None:
            link = await self.store.create(
                event.event_id, kind=event.kind.value, phone10=event.phone10)

        # Владелец нажал «Закрыть сделку»: закрываем её здесь, а не из Telegram —
        # так решение не потеряется, даже если робота перезапустят сразу после
        # нажатия, и все обращения к амо идут одним путём.
        if link.status == "closing":
            return await self._close_deal(link)

        if event.kind is EventKind.CANCELLED:
            return await self._handle_cancelled(link)

        # Сведения записи держим свежими всегда: владелец правит календарь и после
        # того, как робот отработал, а телефон и дата нужны ему позже — в карточке
        # отмены и в проверке, не занята ли сделка другой записью.
        if event.kind not in SILENT_KINDS and event.kind is not EventKind.BOAT:
            link = await self._refresh(event, link)

        if link.status in ("done", "waiting_owner", "cancelled"):
            return link                        # закончили или ждём ответа владельца

        if _kept_out_of_work(link):
            # Запись лежала в календаре до включения: этот заказ владелец ведёт
            # сам, и правка записи не делает его нашей работой (решение 8).
            return link

        if event.kind in SILENT_KINDS:
            return await self._skip(event, link)

        if event.kind is EventKind.BOAT and link.path != "BOAT":
            # Сам теплоход робот не заводит: ни телефона, ни цены в записи нет.
            return await self._ask_owner(link, "теплоход — завести сделку?",
                                         payload=_boat_option(event))

        try:
            if link.path is None:
                link = await self._decide(event, link)
                if link.status == "waiting_owner":
                    return link
            return await self._run_checklist(event, link)
        except AmoError as exc:
            log.warning("Календарь, запись %s: ошибка амо — %s", event.event_id, exc)
            return await self.store.update(
                event.event_id, status="error", last_error=f"{type(exc).__name__}: {exc}")

    # --- отдельные виды записей ---

    async def _skip(self, event: ParsedEvent, link: CalendarLink) -> CalendarLink:
        reason = event.skip_reason or SILENT_KINDS[event.kind]
        return await self.store.update(event.event_id, kind=event.kind.value,
                                       status="skipped", skip_reason=reason)

    async def _close_deal(self, link: CalendarLink) -> CalendarLink:
        """Закрыть сделку как несостоявшуюся — по прямому подтверждению владельца."""
        lead_id = link.real_lead_id or link.primary_lead_id
        lead = await self._get_lead(lead_id)
        pipeline_id = int((lead or {}).get("pipeline_id") or ids.PIPELINE_REALIZATION)

        if lead is not None and int(lead.get("status_id") or 0) in ids.STATUSES_FINAL:
            # Пока карточка висела, сделку закрыли руками — второй раз не трогаем.
            return await self.store.update(link.event_id, status="cancelled",
                                           skip_reason="сделка уже закрыта")

        await self.store.log(link.event_id, "move_lead", dry_run=self.dry_run,
                             entity="lead", amo_id=lead_id,
                             payload={"status_id": ids.STATUS_CLOSED})
        await self.amo.move_lead(lead_id, pipeline_id, ids.STATUS_CLOSED)
        await self.amo.add_note(lead_id, "🤖 Заказ отменён: запись удалена из календаря. "
                                         "Закрыто по подтверждению владельца.")
        return await self.store.update(link.event_id, status="cancelled",
                                       skip_reason="сделка закрыта по вашему подтверждению")

    async def _handle_cancelled(self, link: CalendarLink) -> CalendarLink:
        """Запись удалена = заказ отменён.

        Сделку робот сам не закрывает (решение владельца 2): удаление бывает и
        переносом, и случайностью, а закрытая сделка портит статистику. Спрашиваем
        только если есть что закрывать — иначе просто отмечаем отмену.
        """
        lead_id = link.real_lead_id or link.primary_lead_id
        if lead_id is None:
            return await self.store.update(link.event_id, status="cancelled",
                                           skip_reason="запись удалена, сделки не было")

        lead = await self._get_lead(lead_id)
        if lead is not None and int(lead.get("status_id") or 0) in ids.STATUSES_FINAL:
            # Сделка уже проведена: работа сделана, запись убрали для порядка.
            return await self.store.update(link.event_id, status="cancelled",
                                           skip_reason="запись удалена, сделка уже закрыта")

        return await self._ask_owner(link, "заказ отменён — закрыть сделку?",
                                     payload={"lead_id": lead_id})

    # --- выбор пути ---

    async def _decide(self, event: ParsedEvent, link: CalendarLink) -> CalendarLink:
        raw_leads = await self.amo.find_leads_by_phone(event.phone10)
        candidates = [_to_lead_info(lead) for lead in raw_leads]
        taken = await self.store.taken_leads(event.phone10, exclude_event_id=event.event_id)

        decision = match_event(order_date=event.order_date, candidates=candidates,
                               taken_lead_ids=taken)
        log.info("Календарь, запись %s (%s): решение — %s",
                 event.event_id, mask(event.phone10), decision.kind)

        scratch = self._scratch.setdefault(event.event_id, _Scratch())
        scratch.is_new_client = not candidates
        scratch.duplicates = tuple(decision.duplicates)

        if decision.kind in _ASK_KINDS:
            # Варианты кладём рядом с записью вместе с воронкой: карточку владельцу
            # может отправить уже другой проход, а по ответу надо знать, лид это
            # первичной воронки или готовая сделка реализации.
            options = [{"lead_id": info.lead_id, "pipeline_id": info.pipeline_id,
                        "date": info.order_date.isoformat() if info.order_date else None,
                        "name": info.name}
                       for info in candidates if info.lead_id in decision.options]
            return await self._ask_owner(link, decision.kind, payload={"options": options})

        fields: dict[str, Any] = {"path": _PATH_BY_KIND[decision.kind],
                                  "status": "in_progress"}
        if decision.kind == "use_realization":
            fields["real_lead_id"] = decision.lead_id
        elif decision.kind == "use_primary":
            fields["primary_lead_id"] = decision.lead_id

        return await self.store.update(event.event_id, **fields)

    # --- исполнение чек-листа ---

    async def _run_checklist(self, event: ParsedEvent, link: CalendarLink) -> CalendarLink:
        scratch = self._scratch.setdefault(event.event_id, _Scratch())

        while True:
            step = _next_step(link.path, link.checklist, link, scratch)
            if step is None:
                return await self.store.update(event.event_id, status="done",
                                               last_error=None)

            result = await getattr(self, f"_step_{step}")(event, link)
            link = await self.store.get(event.event_id)

            if result.ask:
                return await self._ask_owner(link, result.ask)
            if result.wait:
                return await self.store.update(event.event_id, status="waiting_salesbot")

            await self.store.mark_step(event.event_id, step)
            link = await self.store.get(event.event_id)

    # --- шаги ---

    async def _step_fill_realization(self, event: ParsedEvent, link: CalendarLink) -> StepResult:
        await self._fill_lead(event, link.real_lead_id)
        return StepResult()

    async def _step_fill_primary(self, event: ParsedEvent, link: CalendarLink) -> StepResult:
        await self._fill_lead(event, link.primary_lead_id)
        return StepResult()

    async def _step_move_primary_success(self, event: ParsedEvent,
                                         link: CalendarLink) -> StepResult:
        """«Передано в работу»: после этого сейлзбот создаёт сделку реализации."""
        await self._write("move_lead", event, link.primary_lead_id,
                          self.amo.move_lead(link.primary_lead_id, ids.PIPELINE_PRIMARY,
                                             ids.STATUS_SUCCESS))
        return StepResult()

    async def _step_wait_salesbot(self, event: ParsedEvent, link: CalendarLink) -> StepResult:
        """Дождаться автосделки, которую сейлзбот создаёт после «Передано в работу».

        Берём только СВОБОДНУЮ сделку. У клиента бывает два заказа подряд, и
        сделка предыдущей записи открыта и видна по тому же телефону: без этой
        проверки второй заказ прицепился бы к чужой сделке, а при отмене робот
        предложил бы закрыть не ту.
        """
        taken = await self.store.taken_leads(event.phone10,
                                             exclude_event_id=event.event_id)
        for lead in await self.amo.find_leads_by_phone(event.phone10):
            info = _to_lead_info(lead)
            if (info.pipeline_id == ids.PIPELINE_REALIZATION and info.is_open
                    and info.lead_id not in taken):
                await self.store.update(event.event_id, real_lead_id=info.lead_id,
                                        status="in_progress")
                return StepResult()

        waited = (self.now() - _as_msk(link.updated_at)).total_seconds()
        if waited > self.salesbot_wait_sec:
            await self.store.log(event.event_id, "salesbot_timeout", dry_run=self.dry_run,
                                 payload={"waited_sec": int(waited)})
            return StepResult(ask="сейлзбот не создал автосделку")
        return StepResult(wait=True)

    async def _step_ensure_contact(self, event: ParsedEvent, link: CalendarLink) -> StepResult:
        scratch = self._scratch.setdefault(event.event_id, _Scratch())
        contacts = await self.amo.find_contacts_by_phone(event.phone10)
        if contacts:
            scratch.contact_id = int(contacts[0]["id"])
            scratch.is_new_client = False
            return StepResult()

        intent = await self._write(
            "create_contact", event, None,
            self.amo.create_contact(name=event.client_name or "Клиент",
                                    phone=normalize_phone(event.phone10)))
        if intent and intent.entity_id:
            scratch.contact_id = intent.entity_id
        return StepResult()

    async def _step_create_primary_lead(self, event: ParsedEvent,
                                        link: CalendarLink) -> StepResult:
        scratch = self._scratch.setdefault(event.event_id, _Scratch())
        intent = await self._write(
            "create_lead", event, None,
            self.amo.create_lead(
                name=_lead_name(event),
                pipeline_id=ids.PIPELINE_PRIMARY,
                status_id=ids.PRIM_STAGE_NEW_LEAD,
                contact_id=scratch.contact_id,
                custom_fields=self._lead_fields(event, existing=None, creating=True),
            ))
        if intent and intent.entity_id:
            await self.store.update(event.event_id, primary_lead_id=intent.entity_id)
        return StepResult()

    async def _step_create_boat_lead(self, event: ParsedEvent,
                                     link: CalendarLink) -> StepResult:
        """Сделка по рейсу теплохода: юрлицо, дата, название судна.

        Телефона и цены в записи нет, поэтому контакт не заводим и в работу
        не передаём — остальное владелец заполняет сам (решение 7).
        """
        when = event.order_date.strftime("%d.%m") if event.order_date else ""
        fields = [
            enums_field(ids.FIELD_CLIENT_TYPE, [ids.CLIENT_TYPE_COMPANY]),
            enums_field(ids.FIELD_DISTRICT, [ids.DISTRICT_ENUMS["юридическое лицо"]]),
        ]
        if event.order_date:
            moment = event.start_at or datetime.combine(
                event.order_date, time(hour=9), tzinfo=MOSCOW_TZ)
            fields.append(datetime_field(ids.FIELD_ORDER_DATETIME, moment))
        if event.summary:
            fields.append(text_field(ids.FIELD_COMMENT, f"Из календаря: «{event.summary}»"))

        intent = await self._write(
            "create_lead", event, None,
            self.amo.create_lead(
                name=f"Теплоход «{event.client_name or 'без названия'}» {when}".strip(),
                pipeline_id=ids.PIPELINE_PRIMARY,
                status_id=ids.PRIM_STAGE_NEW_LEAD,
                custom_fields=fields,
            ))
        if intent and intent.entity_id:
            await self.store.update(event.event_id, primary_lead_id=intent.entity_id)
        return StepResult()

    async def _step_fix_contact_name(self, event: ParsedEvent, link: CalendarLink) -> StepResult:
        """Заменить автоматическое название контакта на имя из записи календаря."""
        if not event.client_name:
            return StepResult()

        lead = await self._get_lead(link.real_lead_id or link.primary_lead_id)
        for contact_id in lead_contact_ids(lead):
            contact = await self._get_contact(contact_id)
            current = (contact or {}).get("name") or ""
            if _is_autogenerated_name(current) and current.strip() != event.client_name:
                await self._write("update_contact", event, contact_id,
                                  self.amo.update_contact(contact_id, name=event.client_name))
        return StepResult()

    async def _step_note_from_calendar(self, event: ParsedEvent,
                                       link: CalendarLink) -> StepResult:
        """След в сделке: что робот взял из календаря и чего он НЕ делал."""
        when = event.start_at.strftime("%d.%m.%Y %H:%M") if event.start_at else (
            event.order_date.strftime("%d.%m.%Y") if event.order_date else "дата не указана")
        lines = [
            f"🤖 Заполнено роботом по записи календаря: «{event.summary}».",
            f"Работа назначена на {when}.",
            "Мастер и сумма чека появятся, когда заказ закроют в боте.",
        ]
        await self._write("add_note", event, link.real_lead_id,
                          self.amo.add_note(link.real_lead_id, "\n".join(lines)))
        return StepResult()

    async def _step_note_duplicates(self, event: ParsedEvent, link: CalendarLink) -> StepResult:
        scratch = self._scratch.setdefault(event.event_id, _Scratch())
        text = (f"Заказ на {event.order_date:%d.%m.%Y} ведётся в сделке "
                f"#{link.primary_lead_id}. Похоже на повторное обращение того же клиента.")
        for lead_id in scratch.duplicates:
            await self._write("add_note", event, lead_id, self.amo.add_note(lead_id, text))
        return StepResult()

    # --- общее ---

    async def _fill_lead(self, event: ParsedEvent, lead_id: Optional[int]) -> None:
        existing = await self._get_lead(lead_id)
        fields = self._lead_fields(event, existing)
        if not fields:
            return                             # всё уже заполнено — в амо не ходим
        await self._write("update_lead", event, lead_id,
                          self.amo.update_lead(lead_id, custom_fields=fields))

    def _lead_fields(self, event: ParsedEvent, existing: Optional[dict],
                     *, creating: bool = False) -> list[dict]:
        """Что робот проставляет в сделке по записи календаря.

        Только пустые поля: заполненное — это либо правка владельца, либо данные
        сейлзбота, и то и другое важнее догадки робота. Бюджет и «Специалист»
        не трогаются вовсе: суммы чека и мастера в записи календаря нет.
        """
        fields: list[dict] = []

        # «Дата и время заказа». При переносе записи НЕ правим (решение 5):
        # фактическую дату впишет заказ из бота после выполнения.
        if event.order_date and not field_value(existing, ids.FIELD_ORDER_DATETIME):
            moment = event.start_at or datetime.combine(
                event.order_date, time(hour=12), tzinfo=MOSCOW_TZ)
            fields.append(datetime_field(ids.FIELD_ORDER_DATETIME, moment))

        if event.address and not field_value(existing, ids.FIELD_ADDRESS):
            fields.append(text_field(ids.FIELD_ADDRESS, event.address))

        if event.comment and not field_value(existing, ids.FIELD_COMMENT):
            fields.append(text_field(ids.FIELD_COMMENT, event.comment))

        district_enum = ids.DISTRICT_ENUMS.get(event.district or "")
        if district_enum and not field_value(existing, ids.FIELD_DISTRICT):
            fields.append(enums_field(ids.FIELD_DISTRICT, [district_enum]))

        service_enums = [SERVICE_ENUMS[kind] for kind in event.services
                         if kind in SERVICE_ENUMS]
        if service_enums and not field_value(existing, ids.FIELD_SERVICE):
            fields.append(enums_field(ids.FIELD_SERVICE, service_enums))

        if creating:
            scratch = self._scratch.get(event.event_id) or _Scratch()
            fields.append(enums_field(
                ids.FIELD_SOURCE,
                [ids.SOURCE_ENUM_WORD_OF_MOUTH if scratch.is_new_client
                 else ids.SOURCE_ENUM_REPEAT]))
        return fields

    async def _write(self, action: str, event: ParsedEvent, amo_id: Optional[int],
                     coro) -> Any:
        """Выполнить действие в амо и записать его в журнал — и в бою, и в репетиции."""
        intent = await coro
        if intent is not None:
            await self.store.log(event.event_id, action, dry_run=not intent.performed,
                                 entity=intent.entity, amo_id=intent.entity_id or amo_id,
                                 payload=_jsonable(intent.payload))
        return intent

    async def _refresh(self, event: ParsedEvent, link: CalendarLink) -> CalendarLink:
        """Подтянуть свежий разбор записи: 18% записей правятся после создания."""
        updates = {
            "kind": event.kind.value,
            "phone10": event.phone10,
            "order_date": event.order_date,
            "client_name": event.client_name,
            "district": event.district,
            "services": list(event.services),
        }
        changed = {name: value for name, value in updates.items()
                   if getattr(link, name, None) != value}
        if link.status == "skipped" and not _kept_out_of_work(link):
            # Владелец снял пометку «⁉️» — запись снова в работе (решение 9).
            changed["status"] = "new"
            changed["skip_reason"] = None
        if not changed:
            return link
        return await self.store.update(event.event_id, **changed) or link

    async def _ask_owner(self, link: CalendarLink, reason: str,
                         payload: Optional[dict] = None) -> CalendarLink:
        await self.store.log(link.event_id, "ask_owner", dry_run=self.dry_run,
                             payload={"reason": reason, **(payload or {})})
        return await self.store.update(link.event_id, status="waiting_owner",
                                       question={"reason": reason, **(payload or {})},
                                       last_error=None)

    async def _get_lead(self, lead_id: Optional[int]) -> Optional[dict]:
        if lead_id is None:
            return None
        getter = getattr(self.amo, "get_lead", None)
        return await getter(lead_id) if getter else None

    async def _get_contact(self, contact_id: int) -> Optional[dict]:
        getter = getattr(self.amo, "get_contact", None)
        return await getter(contact_id) if getter else None


# --- вспомогательное ---


def _next_step(path: Optional[str], checklist: dict, link: CalendarLink,
               scratch: _Scratch) -> Optional[str]:
    """Первый невыполненный шаг пути. None — работа по записи закончена."""
    for step in PATHS.get(path or "", ()):
        if step in _CONDITIONAL and not _CONDITIONAL[step](link, scratch):
            continue
        if step not in (checklist or {}):
            return step
    return None


def _to_lead_info(lead: dict) -> LeadInfo:
    return LeadInfo(
        lead_id=int(lead["id"]),
        pipeline_id=int(lead.get("pipeline_id") or 0),
        status_id=int(lead.get("status_id") or 0),
        order_date=order_date_msk(lead),
        created_date=_stamp_to_date(lead.get("created_at")),
        closed_date=_stamp_to_date(lead.get("closed_at")),
        name=lead.get("name"),
    )


def _lead_name(event: ParsedEvent) -> str:
    when = event.order_date.strftime("%d.%m") if event.order_date else "дата не указана"
    return f"Заказ {when} — {event.client_name or 'клиент'}"


def _boat_option(event: ParsedEvent) -> dict:
    when = event.start_at.strftime("%d.%m %H:%M") if event.start_at else (
        event.order_date.strftime("%d.%m") if event.order_date else "")
    return {"boat": event.client_name, "when": when, "summary": event.summary}


_AUTO_NAME_PREFIXES = ("входящий", "пропущенный", "заявка", "сделка", "автосделка", "звонок")


def _is_autogenerated_name(name: str) -> bool:
    cleaned = (name or "").strip().lower()
    if not cleaned:
        return True
    if any(cleaned.startswith(prefix) for prefix in _AUTO_NAME_PREFIXES):
        return True
    has_letters = any(ch.isalpha() for ch in cleaned)
    digits = sum(1 for ch in cleaned if ch.isdigit())
    return not has_letters and digits >= 10


def _stamp_to_date(stamp: Any):
    if not stamp:
        return None
    return datetime.fromtimestamp(int(stamp), tz=timezone.utc).astimezone(MOSCOW_TZ).date()


def _as_msk(moment: Optional[datetime]) -> datetime:
    if moment is None:
        return datetime.now(MOSCOW_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MOSCOW_TZ)
    return moment.astimezone(MOSCOW_TZ)


def _jsonable(payload: Any) -> Any:
    if isinstance(payload, datetime):
        return payload.isoformat()
    if isinstance(payload, dict):
        return {key: _jsonable(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_jsonable(item) for item in payload]
    return payload
