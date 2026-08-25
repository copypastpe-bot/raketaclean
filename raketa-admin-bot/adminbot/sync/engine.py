"""Движок: проводит один заказ от решения матчера до проведённой сделки.

Устройство: движок ничего не знает ни про Postgres, ни про сеть. У него два
подручных — хранилище состояния (`LinkStore`) и клиент amoCRM. Благодаря этому
редкие сценарии (амо упала посреди цепочки, сейлзбот молчит, робота перезапустили)
проверяются в тестах на двойниках, а не ловятся в бою.

Главное правило: после КАЖДОГО выполненного шага прогресс записывается в хранилище.
Сбой на любом шаге означает «продолжим отсюда», а не «начнём заново».
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from adminbot.amo import ids
from adminbot.amo.client import AmoError
from adminbot.amo.fields import (
    MOSCOW_TZ, date_field, datetime_field, enum_field, field_value, lead_contact_ids,
    order_date_msk, specialist_ids, text_field,
)
from adminbot.config import DEFAULT_SERVICE_BY_MASTER
from adminbot.models import AmoLink, Order
from adminbot.phone import mask, normalize_phone
from adminbot.sync.checklist import StepContext, next_step
from adminbot.sync.matcher import Decision, LeadInfo, match
from adminbot.sync.specialists import SpecialistIndex
from adminbot.sync.store import LinkStore

log = logging.getLogger(__name__)

# Сколько ждём автосделку сейлзбота, прежде чем спросить владельца (дизайн §5.3).
DEFAULT_SALESBOT_WAIT_SEC = 600

# Виды работ → значения списка «Услуга» в амо (проверены по справочнику 2026-08-25).
SERVICE_ENUM_BY_KIND: dict[str, int] = {
    "furniture": ids.SERVICE_ENUM_FURNITURE,     # «Чистка мебели»
    "cleaning": ids.SERVICE_ENUM_CLEANING,       # «Уборка»
}


def service_enums(by_master: dict[str, str]) -> dict[str, int]:
    """Настройка «мастер → вид работ» превращается в «мастер → значение списка амо»."""
    return {master.lower(): SERVICE_ENUM_BY_KIND[kind]
            for master, kind in by_master.items() if kind in SERVICE_ENUM_BY_KIND}


# Какую «Услугу» ставит робот, если поле пустое (решение владельца №7).
# Ключ — часть имени мастера в нижнем регистре. Значение по умолчанию берётся
# из настроек сервиса (SERVICE_BY_MASTER), здесь — запасной вариант для тестов
# и разовых прогонов.
SERVICE_BY_MASTER: dict[str, int] = service_enums(DEFAULT_SERVICE_BY_MASTER)

# Решения матчера, требующие вмешательства владельца.
_ASK_KINDS = ("ask_owner", "ask_owner_stale", "ask_owner_unrelated")

# Как решение матчера превращается в путь заказа.
_PATH_BY_KIND = {
    "use_realization": "A",
    "use_primary": "B",
    "create_new": "C",
    "already_done": "done",
}


@dataclass
class StepResult:
    """Что случилось на шаге: выполнен, или надо подождать, или спросить владельца."""

    done: bool = True
    wait: bool = False
    ask: Optional[str] = None            # причина вопроса владельцу


class Engine:
    def __init__(
        self,
        *,
        amo: Any,
        store: LinkStore,
        specialists: SpecialistIndex,
        dry_run: bool = True,
        salesbot_wait_sec: int = DEFAULT_SALESBOT_WAIT_SEC,
        service_by_master: Optional[dict[str, int]] = None,
        now: Callable[[], datetime] = lambda: datetime.now(MOSCOW_TZ),
    ) -> None:
        self.amo = amo
        self.store = store
        self.specialists = specialists
        self.service_by_master = (SERVICE_BY_MASTER if service_by_master is None
                                  else service_by_master)
        self.dry_run = dry_run
        self.salesbot_wait_sec = salesbot_wait_sec
        self.now = now
        # Черновые заметки в пределах одного заказа: лиды-дубли и найденный контакт.
        # У каждого движка свои — в сервисе их работает несколько сразу (наблюдатель,
        # репетиция предпросмотра, боевой прогон хвоста), и путать их расчёты нельзя.
        self._duplicates: dict[int, tuple[int, ...]] = {}
        self._contacts: dict[int, int] = {}

    # --- основной ход ---

    async def process_order(self, order: Order) -> AmoLink:
        """Продвинуть заказ настолько, насколько это возможно сейчас."""
        link = await self.store.get(order.order_id)
        if link is None:
            link = await self.store.create(order.order_id, order.phone10)

        if link.status in ("done", "waiting_owner"):
            return link                       # уже закончили или ждём ответа владельца

        if not order.phone10:
            return await self._ask_owner(order, link, "телефон заказа не распознан")

        try:
            if link.path is None:
                link = await self._decide(order, link)
                if link.status in ("waiting_owner", "done"):
                    return link
            return await self._run_checklist(order, link)
        except AmoError as exc:
            log.warning("Заказ №%s: ошибка амо — %s", order.order_id, exc)
            return await self.store.update(
                order.order_id, status="error", last_error=f"{type(exc).__name__}: {exc}")

    # --- выбор пути ---

    async def _decide(self, order: Order, link: AmoLink) -> AmoLink:
        """Спросить матчер и записать выбранный путь."""
        raw_leads = await self.amo.find_leads_by_phone(order.phone10)
        candidates = [self._to_lead_info(lead) for lead in raw_leads]
        taken = await self.store.taken_leads(order.phone10, order.order_id)

        decision = match(
            order_date=order.order_date,
            candidates=candidates,
            master_specialist_ids=self._master_enums(order),
            taken_lead_ids=taken,
            order_amount=order.amount_total,
        )
        log.info("Заказ №%s (%s): решение — %s", order.order_id, mask(order.phone10), decision.kind)

        if decision.kind in _ASK_KINDS:
            await self.store.log(order.order_id, "ask_owner", dry_run=self.dry_run,
                                 payload={"reason": decision.kind,
                                          "options": list(decision.options)})
            # Варианты кладём рядом с заказом: карточку владельцу может отправить
            # уже другой проход, а сделки к тому времени надо чем-то подписать.
            question = {
                "reason": decision.kind,
                "options": [_option(info) for info in candidates
                            if info.lead_id in decision.options],
            }
            return await self.store.update(order.order_id, status="waiting_owner",
                                           question=question)

        path = _PATH_BY_KIND[decision.kind]
        fields: dict[str, Any] = {"path": path, "status": "in_progress"}
        if decision.kind == "use_realization":
            fields["real_lead_id"] = decision.lead_id
        elif decision.kind == "use_primary":
            fields["primary_lead_id"] = decision.lead_id
        elif decision.kind == "already_done":
            fields["real_lead_id"] = decision.lead_id
            fields["status"] = "done"

        link = await self.store.update(order.order_id, **fields)
        self._duplicates[order.order_id] = tuple(decision.duplicates)
        return link

    # --- исполнение чек-листа ---

    async def _run_checklist(self, order: Order, link: AmoLink) -> AmoLink:
        context = StepContext(
            has_rating=order.rating_score is not None,
            has_duplicates=bool(self._duplicates.get(order.order_id)),
        )

        while True:
            step = next_step(link.path, link.checklist, context)
            if step is None:
                return await self.store.update(order.order_id, status="done", last_error=None)

            result = await self._run_step(step, order, link)
            link = await self.store.get(order.order_id)

            if result.ask:
                return await self._ask_owner(order, link, result.ask)
            if result.wait:
                return await self.store.update(order.order_id, status="waiting_salesbot")

            await self.store.mark_step(order.order_id, step)
            link = await self.store.get(order.order_id)

    async def _run_step(self, step: str, order: Order, link: AmoLink) -> StepResult:
        handler = getattr(self, f"_step_{step}")
        return await handler(order, link)

    # --- шаги ---

    async def _step_fill_realization(self, order: Order, link: AmoLink) -> StepResult:
        await self._fill_lead(order, link.real_lead_id)
        return StepResult()

    async def _step_move_realization_done(self, order: Order, link: AmoLink) -> StepResult:
        await self._write("move_lead", order, link.real_lead_id,
                          self.amo.move_lead(link.real_lead_id, ids.PIPELINE_REALIZATION,
                                             self._final_stage(order)))
        return StepResult()

    async def _step_fix_contact_name(self, order: Order, link: AmoLink) -> StepResult:
        """Заменить автоматическое название контакта на имя клиента из бота.

        Трогаем только названия вида «Входящий 79…», «Пропущенный 79…», «Заявка…»
        и голые номера: имя, вписанное владельцем руками, не перезаписываем.
        """
        if not order.client_name:
            return StepResult()

        lead = await self._get_lead(link.real_lead_id or link.primary_lead_id)
        for contact_id in lead_contact_ids(lead):
            contact = await self._get_contact(contact_id)
            current = (contact or {}).get("name") or ""
            if _is_autogenerated_name(current) and current.strip() != order.client_name.strip():
                await self._write("update_contact", order, contact_id,
                                  self.amo.update_contact(contact_id, name=order.client_name))
        return StepResult()

    async def _step_note_robot_done(self, order: Order, link: AmoLink) -> StepResult:
        """Оставить в сделке след: провёл робот, вот по каким данным."""
        masters = ", ".join(order.master_names) or "не указан"
        lines = [
            f"🤖 Проведено роботом amo_sync по заказу №{order.order_id} из бота.",
            f"Дата работы: {order.created_at:%d.%m.%Y %H:%M}. Чек: {order.amount_total} ₽.",
            f"Мастер: {masters}. Оплата: {order.payment_method or 'не указана'}.",
        ]
        if self._payment_pending(order):
            lines.append("Оплата по счёту ещё не поступила — сделка оставлена "
                         "на этапе «Заказ выполнен».")
        await self._write("add_note", order, link.real_lead_id,
                          self.amo.add_note(link.real_lead_id, "\n".join(lines)))
        return StepResult()

    async def _step_close_autotasks(self, order: Order, link: AmoLink) -> StepResult:
        await self._close_tasks(order, link.real_lead_id, ids.TASK_TYPES_TO_CLOSE)
        return StepResult()

    async def _step_close_feedback_task(self, order: Order, link: AmoLink) -> StepResult:
        await self._close_tasks(order, link.real_lead_id, {ids.TASK_TYPE_FEEDBACK})
        return StepResult()

    async def _step_note_duplicates(self, order: Order, link: AmoLink) -> StepResult:
        text = (f"Заказ №{order.order_id} проведён в сделке #{link.primary_lead_id}. "
                f"Похоже на повторное обращение того же клиента.")
        for lead_id in self._duplicates.get(order.order_id, ()):
            await self._write("add_note", order, lead_id, self.amo.add_note(lead_id, text))
        return StepResult()

    async def _step_fill_primary(self, order: Order, link: AmoLink) -> StepResult:
        await self._fill_lead(order, link.primary_lead_id)
        return StepResult()

    async def _step_move_primary_success(self, order: Order, link: AmoLink) -> StepResult:
        await self._write("move_lead", order, link.primary_lead_id,
                          self.amo.move_lead(link.primary_lead_id, ids.PIPELINE_PRIMARY,
                                             ids.STATUS_SUCCESS))
        return StepResult()

    async def _step_wait_salesbot(self, order: Order, link: AmoLink) -> StepResult:
        """Ждём автосделку, которую создаёт сейлзбот после «Передано в работу»."""
        for lead in await self.amo.find_leads_by_phone(order.phone10):
            info = self._to_lead_info(lead)
            if info.pipeline_id == ids.PIPELINE_REALIZATION and info.is_open:
                await self.store.update(order.order_id, real_lead_id=info.lead_id,
                                        status="in_progress")
                return StepResult()

        waited = (self.now() - _as_msk(link.updated_at)).total_seconds()
        if waited > self.salesbot_wait_sec:
            await self.store.log(order.order_id, "salesbot_timeout", dry_run=self.dry_run,
                                 payload={"waited_sec": int(waited)})
            return StepResult(ask="сейлзбот не создал автосделку")
        return StepResult(wait=True)

    async def _step_ensure_contact(self, order: Order, link: AmoLink) -> StepResult:
        contacts = await self.amo.find_contacts_by_phone(order.phone10)
        if contacts:
            self._contacts[order.order_id] = int(contacts[0]["id"])
            return StepResult()

        phone = normalize_phone(order.phone10)
        intent = await self._write("create_contact", order, None,
                                   self.amo.create_contact(name=order.client_name or "Клиент",
                                                           phone=phone))
        if intent and intent.entity_id:
            self._contacts[order.order_id] = intent.entity_id
        return StepResult()

    async def _step_create_primary_lead(self, order: Order, link: AmoLink) -> StepResult:
        intent = await self._write(
            "create_lead", order, None,
            self.amo.create_lead(
                name=f"Заказ №{order.order_id}",
                pipeline_id=ids.PIPELINE_PRIMARY,
                status_id=ids.PRIM_STAGE_NEW_LEAD,
                price=order.amount_total,
                contact_id=self._contacts.get(order.order_id),
                custom_fields=self._lead_fields(order, existing=None, creating=True),
            ))
        if intent and intent.entity_id:
            await self.store.update(order.order_id, primary_lead_id=intent.entity_id)
        return StepResult()

    # --- общее ---

    async def _fill_lead(self, order: Order, lead_id: Optional[int]) -> None:
        existing = await self._get_lead(lead_id)
        await self._write("update_lead", order, lead_id,
                          self.amo.update_lead(lead_id, price=order.amount_total,
                                               custom_fields=self._lead_fields(order, existing)))

    def _lead_fields(self, order: Order, existing: Optional[dict],
                     *, creating: bool = False) -> list[dict]:
        """Что робот проставляет в сделке.

        Всё это можно писать в лид первичной воронки: сейлзбот переносит поля
        в автосделку сам (проверено на заказе №585).
        """
        fields = [datetime_field(ids.FIELD_ORDER_DATETIME, order.created_at)]
        if order.address:
            fields.append(text_field(ids.FIELD_ADDRESS, order.address))

        # «Услуга»: заполненную не трогаем (решение владельца №7).
        if not field_value(existing, ids.FIELD_SERVICE):
            service = self._service_enum(order)
            if service:
                fields.append(enum_field(ids.FIELD_SERVICE, service))

        # «Специалист» — мастер, выполнивший заказ. Заполненное не трогаем:
        # там может стоять кто-то ещё с этапа планирования.
        if not field_value(existing, ids.FIELD_SPECIALIST):
            for enum_id in self._master_enums(order):
                fields.append(enum_field(ids.FIELD_SPECIALIST, enum_id))
                break                       # одного мастера достаточно

        # «Источник сделки». Заполняем в двух случаях: лид застрял в «Неразобранном»
        # без источника и сделку робот заводит сам. И там, и там клиент пришёл мимо
        # рекламы и звонков. Первый заказ клиента — сарафан, дальше — повторный.
        unsorted_without_source = (
            existing is not None
            and existing.get("status_id") in ids.STATUSES_UNSORTED
            and not field_value(existing, ids.FIELD_SOURCE)
        )
        if creating or unsorted_without_source:
            fields.append(enum_field(
                ids.FIELD_SOURCE,
                ids.SOURCE_ENUM_REPEAT if order.is_repeat_client
                else ids.SOURCE_ENUM_WORD_OF_MOUTH))

        payment = _payment_enum(order.payment_method)
        if payment:
            fields.append(enum_field(ids.FIELD_PAYMENT_TYPE, payment))

        # «Тип клиента»: расчёт по счёту — практически всегда юрлицо.
        fields.append(enum_field(
            ids.FIELD_CLIENT_TYPE,
            ids.CLIENT_TYPE_COMPANY if _is_wire(order) else ids.CLIENT_TYPE_PERSON))

        # «Дата оплаты» = дате заказа. Кроме неоплаченного счёта: денег ещё нет.
        if not self._payment_pending(order):
            fields.append(date_field(ids.FIELD_PAYMENT_DATE, order.order_date))
        return fields

    def _payment_pending(self, order: Order) -> bool:
        """Заказ по счёту, оплата по которому ещё не поступила."""
        return _is_wire(order) and order.awaiting_wire_payment

    def _final_stage(self, order: Order) -> int:
        """Куда ведём сделку в конце.

        Обычно — «ЗАКАЗ ВЫПОЛНЕН и Оплата получена». Но если работа сделана,
        а оплата по счёту ещё не пришла, останавливаемся на «Заказ выполнен»:
        сейлзбот сам поставит задачу получить оплату, и владелец её отследит
        (решение владельца 2026-08-25).
        """
        return ids.REAL_STAGE_DONE if self._payment_pending(order) else ids.STATUS_SUCCESS

    def _service_enum(self, order: Order) -> Optional[int]:
        for name, _phone in order.masters:
            for key, enum_id in self.service_by_master.items():
                if key in (name or "").lower():
                    return enum_id
        return None                       # мастер неизвестен — поле не выдумываем

    def _master_enums(self, order: Order) -> tuple[int, ...]:
        return self.specialists.resolve_many(order.masters)

    async def _close_tasks(self, order: Order, lead_id: Optional[int],
                           types: Sequence[int] | set[int]) -> None:
        for task in await self.amo.get_lead_tasks(lead_id):
            if task.get("task_type_id") in types:
                await self._write("complete_task", order, task["id"],
                                  self.amo.complete_task(task["id"]))

    async def _write(self, action: str, order: Order, amo_id: Optional[int], coro) -> Any:
        """Выполнить действие в амо и записать его в журнал — и в бою, и в репетиции."""
        intent = await coro
        if intent is not None:
            await self.store.log(order.order_id, action, dry_run=not intent.performed,
                                 entity=intent.entity, amo_id=intent.entity_id or amo_id,
                                 payload=_jsonable(intent.payload))
        return intent

    async def _get_lead(self, lead_id: Optional[int]) -> Optional[dict]:
        if lead_id is None:
            return None
        getter = getattr(self.amo, "get_lead", None)
        if getter is None:
            return None
        return await getter(lead_id)

    async def _get_contact(self, contact_id: int) -> Optional[dict]:
        getter = getattr(self.amo, "get_contact", None)
        return await getter(contact_id) if getter else None

    async def _ask_owner(self, order: Order, link: AmoLink, reason: str) -> AmoLink:
        await self.store.log(order.order_id, "ask_owner", dry_run=self.dry_run,
                             payload={"reason": reason})
        return await self.store.update(order.order_id, status="waiting_owner",
                                       question={"reason": reason, "options": []},
                                       last_error=None)

    def _to_lead_info(self, lead: dict) -> LeadInfo:
        return LeadInfo(
            lead_id=int(lead["id"]),
            pipeline_id=int(lead.get("pipeline_id") or 0),
            status_id=int(lead.get("status_id") or 0),
            order_date=order_date_msk(lead),
            closed_date=_stamp_to_date(lead.get("closed_at")),
            created_date=_stamp_to_date(lead.get("created_at")),
            specialist_ids=specialist_ids(lead),
            price=None if lead.get("price") is None else Decimal(str(lead["price"])),
            name=lead.get("name"),
        )


# Способы оплаты бота → значения поля «Вариант оплаты» в амо
# (маппинг подтверждён владельцем 2026-08-25).
PAYMENT_ENUM_BY_METHOD = {
    "наличные": ids.PAYMENT_ENUM_CASH,
    "карта дима": ids.PAYMENT_ENUM_CARD,
    "карта женя": ids.PAYMENT_ENUM_CARD,
    "р/с": ids.PAYMENT_ENUM_WIRE,
}

# Названия контактов, которые амо ставит сама и которые не жалко заменить.
_AUTO_NAME_PREFIXES = ("входящий", "пропущенный", "заявка", "сделка", "автосделка", "звонок")


def _option(info: LeadInfo) -> dict:
    """Сделка-кандидат в том виде, в каком её увидит владелец на кнопке."""
    # Дата сделки: сначала заявленная дата заказа, иначе когда её завели или закрыли.
    when = info.order_date or info.created_date or info.closed_date
    return {
        "lead_id": info.lead_id,
        "pipeline_id": info.pipeline_id,
        "date": when.isoformat() if when else None,
        "price": None if info.price is None else int(info.price),
        "name": info.name,
    }


def _payment_enum(method: Optional[str]) -> Optional[int]:
    return PAYMENT_ENUM_BY_METHOD.get((method or "").strip().lower())


def _is_wire(order: Order) -> bool:
    return (order.payment_method or "").strip().lower() == "р/с"


def _is_autogenerated_name(name: str) -> bool:
    """Название контакта поставлено автоматом, а не человеком."""
    cleaned = (name or "").strip().lower()
    if not cleaned:
        return True
    if any(cleaned.startswith(prefix) for prefix in _AUTO_NAME_PREFIXES):
        return True
    # Голый номер вместо имени: букв нет вовсе, а цифр хватает на телефон.
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
    """Decimal и datetime в журнал попадают в человекочитаемом виде."""
    if isinstance(payload, Decimal):
        return str(payload)
    if isinstance(payload, datetime):
        return payload.isoformat()
    if isinstance(payload, dict):
        return {key: _jsonable(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_jsonable(item) for item in payload]
    return payload
