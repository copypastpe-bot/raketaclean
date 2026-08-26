"""Проведение ковровой сделки по строке отчёта партнёра.

Устройство то же, что у движка уборки: после каждого выполненного шага прогресс
записывается в хранилище, поэтому сбой посреди цепочки означает «продолжим
отсюда», а не «начнём заново» и не «сделаем дважды».

Что робот пишет в сделку и чего не пишет:

- бюджет — «Взято денег у клиента» (решение владельца: в CRM идут полученные
  деньги, а не расчётная стоимость);
- даты забора и возврата ковров — в одноимённые поля сделки;
- район города — только если название совпало со списком амо; «Опалихи» в списке
  нет, и выдумывать её робот не станет;
- адрес, услугу и специалиста — только если поля пустые: там могла быть правка
  владельца, и затирать её нельзя.

Отказ партнёра закрывает сделку как нереализованную и оставляет причину
комментарием. Бюджет и поля при этом не трогаются вовсе: работы не было.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Optional

from adminbot.amo import ids
from adminbot.amo.client import AmoError
from adminbot.amo.fields import MOSCOW_TZ, date_field, enum_field, field_value, text_field
from adminbot.carpets.matcher import CarpetLead, match_carpet
from adminbot.carpets.report import CarpetRow
from adminbot.carpets.store import CarpetStore
from adminbot.models import CarpetLink
from adminbot.phone import mask

log = logging.getLogger(__name__)

# Способы оплаты партнёра → «Вариант оплаты» в амо.
PAYMENT_ENUM_BY_METHOD = {
    "наличные": ids.PAYMENT_ENUM_CASH,
    "карта": ids.PAYMENT_ENUM_CARD,
    "перевод": ids.PAYMENT_ENUM_CARD,
    "эквайринг": ids.PAYMENT_ENUM_ACQUIRING,
}

# Шаги проведённого заказа. Порядок важен: сначала заполняем, потом двигаем этап
# (переход в финал порождает задачи сейлзбота), и только потом отмечаемся.
STEPS_DELIVERED = ("fill_carpet_lead", "move_carpet_delivered", "note_robot_done")
STEPS_REFUSED = ("move_carpet_refused", "note_refusal")
STEPS_LINK_ONLY: tuple[str, ...] = ()          # владелец провёл сам — только привязка


@dataclass
class StepResult:
    done: bool = True
    wait: bool = False
    ask: Optional[str] = None


class CarpetEngine:
    def __init__(
        self,
        *,
        amo: Any,
        store: CarpetStore,
        dry_run: bool = True,
        now: Callable[[], datetime] = lambda: datetime.now(MOSCOW_TZ),
    ) -> None:
        self.amo = amo
        self.store = store
        self.dry_run = dry_run
        self.now = now

    async def process_row(self, row: CarpetRow,
                          source_file: Optional[str] = None) -> CarpetLink:
        """Продвинуть одну строку отчёта настолько, насколько это возможно сейчас."""
        link = await self.store.get(row.partner_id)
        if link is None:
            link = await self.store.create(row.partner_id, row.phone10, source_file)

        if link.status in ("done", "waiting_owner"):
            return link                            # уже разобрано или ждём ответа

        if not row.phone10:
            return await self._ask_owner(row, "в строке отчёта не разобрал телефон")

        try:
            if not link.checklist and link.lead_id is None and link.status == "new":
                link = await self._decide(row, link)
                if link.status in ("waiting_owner", "done"):
                    return link
            return await self._run_steps(row, link)
        except AmoError as exc:
            log.warning("Ковры, заказ партнёра №%s: ошибка амо — %s", row.partner_id, exc)
            return await self.store.update(row.partner_id, status="error",
                                           last_error=f"{type(exc).__name__}: {exc}")

    # --- выбор пути ---

    async def _decide(self, row: CarpetRow, link: CarpetLink) -> CarpetLink:
        raw_leads = await self.amo.find_leads_by_phone(row.phone10)
        candidates = [self._to_carpet_lead(lead) for lead in raw_leads]
        taken = await self.store.taken_leads(row.phone10, row.partner_id)

        decision = match_carpet(row, candidates, taken_lead_ids=taken)
        log.info("Ковры, заказ партнёра №%s (%s): решение — %s",
                 row.partner_id, mask(row.phone10), decision.kind)

        if decision.kind == "ask_owner":
            options = [self._option(lead) for lead in candidates
                       if lead.lead_id in decision.options]
            return await self._ask_owner(row, "какая сделка про этот заказ", options)

        if decision.kind == "already_done":
            return await self.store.update(row.partner_id, status="done",
                                           lead_id=decision.lead_id)

        if decision.kind == "use_carpet":
            return await self.store.update(row.partner_id, status="in_progress",
                                           lead_id=decision.lead_id)

        # Отказ без сделки закрывать нечего: работы не было, новую цепочку не заводим.
        if row.is_refusal:
            await self.store.log(row.partner_id, "refusal_without_lead",
                                 dry_run=self.dry_run)
            return await self.store.update(row.partner_id, status="done")

        # use_primary и create_new — цепочка с лида, это следующая задача плана.
        return await self._ask_owner(
            row, "сделки в CRM нет — нужна новая цепочка",
            [self._option(lead) for lead in candidates if lead.lead_id in decision.options])

    # --- исполнение шагов ---

    async def _run_steps(self, row: CarpetRow, link: CarpetLink) -> CarpetLink:
        for step in self._steps_for(row):
            if step in (link.checklist or {}):
                continue

            result = await getattr(self, f"_step_{step}")(row, link)
            link = await self.store.get(row.partner_id)

            if result.ask:
                return await self._ask_owner(row, result.ask)
            await self.store.mark_step(row.partner_id, step)
            link = await self.store.get(row.partner_id)

        return await self.store.update(row.partner_id, status="done", last_error=None)

    def _steps_for(self, row: CarpetRow) -> tuple[str, ...]:
        return STEPS_REFUSED if row.is_refusal else STEPS_DELIVERED

    # --- шаги ---

    async def _step_fill_carpet_lead(self, row: CarpetRow, link: CarpetLink) -> StepResult:
        existing = await self._get_lead(link.lead_id)
        await self._write("update_lead", row, link.lead_id,
                          self.amo.update_lead(link.lead_id, price=row.amount,
                                               custom_fields=self._lead_fields(row, existing)))
        return StepResult()

    async def _step_move_carpet_delivered(self, row: CarpetRow, link: CarpetLink) -> StepResult:
        await self._write("move_lead", row, link.lead_id,
                          self.amo.move_lead(link.lead_id, ids.PIPELINE_CARPETS,
                                             ids.CARPET_STAGE_DELIVERED))
        return StepResult()

    async def _step_move_carpet_refused(self, row: CarpetRow, link: CarpetLink) -> StepResult:
        await self._write("move_lead", row, link.lead_id,
                          self.amo.move_lead(link.lead_id, ids.PIPELINE_CARPETS,
                                             ids.CARPET_STAGE_REFUSED))
        return StepResult()

    async def _step_note_robot_done(self, row: CarpetRow, link: CarpetLink) -> StepResult:
        lines = [
            f"🤖 Проведено роботом по отчёту партнёра, заказ №{row.partner_id}.",
            f"Взято денег у клиента: {row.amount} ₽. Оплата: {row.payment_method or 'не указана'}.",
        ]
        if row.pickup_date and row.return_date:
            lines.append(f"Ковры забрали {row.pickup_date:%d.%m.%Y}, "
                         f"сдали {row.return_date:%d.%m.%Y}.")
        if row.price and row.price != row.amount:
            lines.append(f"Расчётная стоимость в отчёте: {row.price} ₽.")
        await self._write("add_note", row, link.lead_id,
                          self.amo.add_note(link.lead_id, "\n".join(lines)))
        return StepResult()

    async def _step_note_refusal(self, row: CarpetRow, link: CarpetLink) -> StepResult:
        text = (f"🤖 Партнёр не забрал ковры по заказу №{row.partner_id}. "
                f"Причина: {row.refusal_reason or 'в отчёте не указана'}.")
        await self._write("add_note", row, link.lead_id, self.amo.add_note(link.lead_id, text))
        return StepResult()

    # --- поля сделки ---

    def _lead_fields(self, row: CarpetRow, existing: Optional[dict]) -> list[dict]:
        fields: list[dict] = []

        if row.pickup_date:
            fields.append(date_field(ids.FIELD_CARPET_PICKUP, row.pickup_date))
        if row.return_date:
            fields.append(date_field(ids.FIELD_CARPET_RETURN, row.return_date))
            # Деньги партнёр берёт при сдаче ковров.
            fields.append(date_field(ids.FIELD_PAYMENT_DATE, row.return_date))

        district = ids.DISTRICT_ENUMS.get((row.district or "").strip().lower())
        if district:
            fields.append(enum_field(ids.FIELD_DISTRICT, district))

        payment = PAYMENT_ENUM_BY_METHOD.get((row.payment_method or "").strip().lower())
        if payment:
            fields.append(enum_field(ids.FIELD_PAYMENT_TYPE, payment))

        # Ниже — поля, которые владелец мог заполнить сам. Заполненное не трогаем.
        if row.address and not field_value(existing, ids.FIELD_ADDRESS):
            fields.append(text_field(ids.FIELD_ADDRESS, row.address))
        if not field_value(existing, ids.FIELD_SERVICE):
            fields.append(enum_field(ids.FIELD_SERVICE, ids.SERVICE_ENUM_CARPETS))
        if not field_value(existing, ids.FIELD_SPECIALIST):
            fields.append(enum_field(ids.FIELD_SPECIALIST, ids.SPECIALIST_ENUM_CARPETS))
        return fields

    # --- общее ---

    async def _write(self, action: str, row: CarpetRow, amo_id: Optional[int], coro) -> Any:
        intent = await coro
        if intent is not None:
            await self.store.log(row.partner_id, action, dry_run=not intent.performed,
                                 entity=intent.entity, amo_id=intent.entity_id or amo_id,
                                 payload=_jsonable(intent.payload))
        return intent

    async def _get_lead(self, lead_id: Optional[int]) -> Optional[dict]:
        if lead_id is None:
            return None
        getter = getattr(self.amo, "get_lead", None)
        return await getter(lead_id) if getter else None

    async def _ask_owner(self, row: CarpetRow, reason: str,
                         options: Optional[list[dict]] = None) -> CarpetLink:
        await self.store.log(row.partner_id, "ask_owner", dry_run=self.dry_run,
                             payload={"reason": reason})
        return await self.store.update(
            row.partner_id, status="waiting_owner", last_error=None,
            question={"reason": reason, "options": options or []})

    def _to_carpet_lead(self, lead: dict) -> CarpetLead:
        return CarpetLead(
            lead_id=int(lead["id"]),
            pipeline_id=int(lead.get("pipeline_id") or 0),
            status_id=int(lead.get("status_id") or 0),
            created_date=_stamp_to_date(lead.get("created_at")),
            order_date=_order_date(lead),
            price=None if lead.get("price") is None else Decimal(str(lead["price"])),
            name=lead.get("name"),
        )

    @staticmethod
    def _option(lead: CarpetLead) -> dict:
        when = lead.order_date or lead.created_date
        return {"lead_id": lead.lead_id, "pipeline_id": lead.pipeline_id,
                "date": when.isoformat() if when else None,
                "price": None if lead.price is None else int(lead.price),
                "name": lead.name}


def _order_date(lead: dict) -> Optional[date]:
    from adminbot.amo.fields import order_date_msk

    return order_date_msk(lead)


def _stamp_to_date(stamp: Any) -> Optional[date]:
    if not stamp:
        return None
    return datetime.fromtimestamp(int(stamp), tz=MOSCOW_TZ).date()


def _jsonable(payload: Any) -> Any:
    if isinstance(payload, Decimal):
        return str(payload)
    if isinstance(payload, (datetime, date)):
        return payload.isoformat()
    if isinstance(payload, dict):
        return {key: _jsonable(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_jsonable(item) for item in payload]
    return payload
