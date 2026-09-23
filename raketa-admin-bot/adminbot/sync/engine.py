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
from adminbot.sync.matcher import PLACEHOLDER_PRICE, Decision, LeadInfo, match
from adminbot.sync.specialists import SpecialistIndex
from adminbot.sync.store import LinkStore
from adminbot.sync.waiting import waited_since

log = logging.getLogger(__name__)

# Сколько ждём автосделку сейлзбота, прежде чем спросить владельца (дизайн §5.3).
# 40 минут: обычно она появляется за секунды, но амо подтормаживает — владелец
# видел задержки до 20 минут (2026-08-26).
DEFAULT_SALESBOT_WAIT_SEC = 2400

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
    # Цепочка закончена прямо здесь, без вопроса владельцу и без следующих шагов
    # чек-листа: например, дочка из примечания уже закрыта в CRM руками
    # (задача 1, ревью 22.09) — правка владельца важнее догадки робота.
    stop: bool = False


@dataclass(frozen=True)
class WirePaymentResult:
    """Итог доводки одной сделки после оплаты по счёту — сырьё для отчёта владельцу."""

    lead_id: Optional[int]
    tasks_closed: int
    stage_moved: bool   # True — сделка переведена в «выполнено и оплата получена»
    # Что-то реально поменялось в амо в этом проходе (сумма, стадия или хотя бы
    # одна задача) — по этому флагу цикл решает, слать ли отчёт владельцу
    # (ревью 23.09): молчать, когда менять было нечего.
    changed: bool


class Engine:
    def __init__(
        self,
        *,
        amo: Any,
        store: LinkStore,
        specialists: SpecialistIndex,
        dry_run: bool = True,
        salesbot_wait_sec: int = DEFAULT_SALESBOT_WAIT_SEC,
        child_by_note: bool = False,
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
        # Дочку воронки 2 берём по служебному примечанию сейлзбота, а не как
        # «первую свободную» сделку по телефону (задача 1 ТЗ 2026-09-22).
        # Выключено — старое поведение (AMO_CHILD_BY_NOTE, откат одной командой).
        self.child_by_note = child_by_note
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
                # Путь ещё не выбран — заказ с известной записью календаря ждёт,
                # пока движок календаря узнает её сделку (задача 10, ТЗ 2026-09-22):
                # чек-листу тут пока нечего исполнять, отдаём link как есть.
                if link.path is None or link.status in ("waiting_owner", "done"):
                    return link
            return await self._run_checklist(order, link)
        except AmoError as exc:
            log.warning("%s №%s: ошибка амо — %s", order.label, order.order_id, exc)
            return await self.store.update(
                order.order_id, status="error", last_error=f"{type(exc).__name__}: {exc}")

    async def process_payment(self, order: Order, link: AmoLink) -> Optional[WirePaymentResult]:
        """Довести сделку до конца после того, как оплата по счёту пришла (задача 11).

        Сбой амо (сделка недоступна, сеть моргнула) сюда не ловится — долетает
        до вызывающего цикла как есть: `payment_synced_at` ниже не поставится,
        и на следующем проходе связка снова окажется «due» (тот же приём, что
        у `sync/address_reminder.py`).

        Закрытие задач вынесено из веток и идёт всегда, независимо от того,
        переводили стадию сейчас или она уже была финальной (ревью 23.09):
        частичный сбой основной ветки — стадия переведена, а следующий шаг
        (закрытие задач или примечание) упал — иначе на повторном проходе
        сделка уже видна как «финальная», и открытые задачи не закрылись бы
        никогда. Чтение задач идемпотентно: закрытую амо второй раз не отдаст.
        """
        lead_id = link.real_lead_id
        lead = await self._get_lead(lead_id)
        stage = int(lead["status_id"]) if lead is not None and lead.get("status_id") else None
        stage_moved = False
        price_fixed = False

        if stage == ids.REAL_STAGE_DONE:
            await self._write("update_lead", order, lead_id,
                              self.amo.update_lead(lead_id, price=order.amount_total))
            price_fixed = True
            await self._write("move_lead", order, lead_id,
                              self.amo.move_lead(lead_id, ids.PIPELINE_REALIZATION,
                                                 ids.STATUS_SUCCESS))
            stage_moved = True
        else:
            # Стадия уже финальная (вопреки ожиданию — п. «Принято координатором»
            # 22.09, или это повторный проход после частичного сбоя) или сделки
            # не нашлось: стадию не трогаем. Сумму правим, только если в сделке
            # стоит заглушка.
            price = None if lead is None else lead.get("price")
            price_dec = None if price is None else Decimal(str(price))
            if price_dec is not None and price_dec < PLACEHOLDER_PRICE:
                await self._write("update_lead", order, lead_id,
                                  self.amo.update_lead(lead_id, price=order.amount_total))
                price_fixed = True

        tasks_closed = (await self._close_tasks(order, lead_id, ids.TASK_TYPES_TO_CLOSE)
                        if lead_id is not None else 0)

        # Примечание — только когда стадия переведена в ЭТОМ проходе: иначе
        # повторный (или уже финальный) проход плодил бы дубли в сделке.
        if stage_moved:
            note = (f"🤖 Оплата по счёту получена: {order.amount_total} ₽. Сделка проведена "
                   "в «выполнено и оплата получена».")
            await self._write("add_note", order, lead_id, self.amo.add_note(lead_id, note))

        await self.store.update(order.order_id, payment_synced_at=self.now())
        changed = stage_moved or price_fixed or tasks_closed > 0
        await self.store.log(order.order_id, "wire_payment_synced", dry_run=self.dry_run,
                             payload={"lead_id": lead_id, "stage": stage,
                                      "tasks_closed": tasks_closed, "stage_moved": stage_moved,
                                      "changed": changed})
        return WirePaymentResult(lead_id=lead_id, tasks_closed=tasks_closed,
                                 stage_moved=stage_moved, changed=changed)

    # --- выбор пути ---

    async def _decide(self, order: Order, link: AmoLink) -> AmoLink:
        """Спросить матчер и записать выбранный путь.

        Заказ с известной записью календаря (задача 10, ТЗ 2026-09-22) матчер
        не спрашивает вовсе: сделку назвал мастер, а не гадание по телефону.
        """
        if order.deal_lead_id is not None or order.calendar_event_id is not None:
            return await self._decide_from_calendar(order, link)

        raw_leads = await self.amo.find_leads_by_phone(order.phone10)
        candidates = [self._to_lead_info(lead) for lead in raw_leads]
        taken = await self.store.taken_leads(
            [info.lead_id for info in candidates], exclude_order_id=order.order_id)

        decision = match(
            order_date=order.order_date,
            candidates=candidates,
            master_specialist_ids=self._master_enums(order),
            taken_lead_ids=taken,
            order_amount=order.amount_total,
        )
        log.info("%s №%s (%s): решение — %s",
                 order.label, order.order_id, mask(order.phone10), decision.kind)

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
            # Этот путь в чек-лист не заходит (process_order отдаёт результат сразу
            # по status="done"), поэтому _fill_lead сюда не попадает и адрес
            # сделки нужно прочитать здесь же. Берём его из уже полученных raw_leads —
            # лишнего похода в амо не делаем, в сделку ничего не пишем (задача 9,
            # ТЗ 2026-09-16).
            matched = next((raw for raw in raw_leads if int(raw["id"]) == decision.lead_id), None)
            fields["deal_address"] = field_value(matched, ids.FIELD_ADDRESS)

        link = await self.store.update(order.order_id, **fields)
        self._duplicates[order.order_id] = tuple(decision.duplicates)
        return link

    async def _decide_from_calendar(self, order: Order, link: AmoLink) -> AmoLink:
        """Заказ с известной сделкой — мастер выбрал запись календаря (задача 10).

        `deal_lead_id` заказа называет сделку прямо: её поставил мастер, выбирая
        запись в сценарии рабочего бота (задача 9). Матчер и проверка «занято»
        здесь не нужны — гадать нечего, сделка уже названа. Известен только
        `calendar_event_id` (мастер выбрал запись, но на момент заказа движок
        календаря (`gcal/engine.py`) ещё не завёл её сделку) — читаем запись
        заново и берём её `real_lead_id`. Не готова и она — ждём, пока движок
        календаря её заведёт, тем же таймером, что и путь Б (задача 2), но от
        `created_at` связки: чек-листа здесь ещё нет, отметки шага — тоже.

        Названная сделка могла измениться между выбором мастера и этим проходом
        (ревью 23.09): читаем её заново, прежде чем назначать путь A. Удалена —
        спрашиваем владельца, матчер не запускаем (гадать по телефону здесь так
        же не нужно, как и раньше). Закрыта руками — привязываем как
        `already_done` обычного матчера: в CRM ничего не пишем, чек-лист не
        исполняем.
        """
        real_lead_id = order.deal_lead_id
        if real_lead_id is None and order.calendar_event_id is not None:
            calendar_link = await self.store.get_calendar_link(order.calendar_event_id)
            real_lead_id = calendar_link.real_lead_id if calendar_link is not None else None

        if real_lead_id is not None:
            lead = await self._get_lead(real_lead_id)
            if lead is None:
                return await self._ask_owner(order, link, "сделка из заказа не найдена в CRM")

            if int(lead.get("status_id") or 0) in ids.STATUSES_FINAL:
                link = await self.store.update(
                    order.order_id, path="done", status="done", real_lead_id=real_lead_id,
                    deal_address=field_value(lead, ids.FIELD_ADDRESS))
                await self.store.log(order.order_id, "linked_by_master", dry_run=self.dry_run,
                                     payload={"lead_id": real_lead_id, "closed": True,
                                              "calendar_event_id": order.calendar_event_id})
                return link

            link = await self.store.update(order.order_id, path="A", status="in_progress",
                                           real_lead_id=real_lead_id)
            await self.store.log(order.order_id, "linked_by_master", dry_run=self.dry_run,
                                 payload={"lead_id": real_lead_id,
                                          "calendar_event_id": order.calendar_event_id})
            return link

        waited = (self.now() - _as_msk(link.created_at)).total_seconds()
        if waited > self.salesbot_wait_sec:
            return await self._ask_owner(order, link, "сейлзбот не создал автосделку")
        return await self.store.update(order.order_id, status="waiting_salesbot")

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
            if result.stop:
                return link           # шаг сам завершил цепочку (см. StepResult.stop)

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
        if self._payment_pending(order):
            # Остановились на «Заказ выполнен» из-за неоплаченного счёта —
            # отметка для доводки оплаты (задача 11, ТЗ 2026-09-22): когда
            # админ привяжет платёж, `process_payment` найдёт эту связку сама.
            await self.store.update(order.order_id, payment_pending=True)
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
            f"🤖 Проведено роботом amo_sync: {order.label} №{order.order_id} из бота.",
            f"Дата работы: {_as_msk(order.created_at):%d.%m.%Y %H:%M}. Чек: {order.amount_total} ₽.",
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
        text = (f"{order.label} №{order.order_id} — работа проведена в сделке "
                f"#{link.primary_lead_id}. Похоже на повторное обращение того же клиента.")
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
        """Ждём автосделку, которую создаёт сейлзбот после «Передано в работу».

        `child_by_note` включён: дочка — та, что названа в служебном примечании
        сейлзбота у лида воронки 1 (задача 1 ТЗ 2026-09-22), а не «первая
        свободная» сделка по телефону.
        """
        if self.child_by_note:
            child_id = await self.amo.get_child_lead_id(link.primary_lead_id)
            if child_id is not None:
                # Дочку привязываем всегда — примечание называет факт цепочки.
                # Но прежде чем писать в неё дальше, проверяем, не закрыта ли
                # она уже руками: правка владельца важнее догадки робота (тот
                # же принцип, что у already_done). Сделка удалена (get_lead
                # вернул None) — тоже считаем закрытой.
                await self.store.update(order.order_id, real_lead_id=child_id,
                                        status="in_progress")
                await self.store.log(order.order_id, "child_by_note", dry_run=self.dry_run,
                                     payload={"parent": link.primary_lead_id,
                                              "child": child_id})
                child_lead = await self._get_lead(child_id)
                status_id = (int(child_lead.get("status_id") or 0)
                            if child_lead is not None else None)
                if child_lead is None or status_id in ids.STATUSES_FINAL:
                    await self.store.log(order.order_id, "child_closed", dry_run=self.dry_run,
                                         payload={"child": child_id, "status_id": status_id})
                    await self.store.update(
                        order.order_id, status="done",
                        last_error="сделка реализации уже закрыта в CRM, не трогал")
                    return StepResult(stop=True)
                return StepResult()
        else:
            for lead in await self.amo.find_leads_by_phone(order.phone10):
                info = self._to_lead_info(lead)
                if info.pipeline_id == ids.PIPELINE_REALIZATION and info.is_open:
                    await self.store.update(order.order_id, real_lead_id=info.lead_id,
                                            status="in_progress")
                    return StepResult()

        # Считаем от отметки «передано в работу», а не от updated_at: его
        # двигает сам этот шаг на каждом проходе (задача 2 ТЗ 22.09).
        waited = waited_since(link.checklist, "move_primary_success", self.now())
        if waited is None:
            waited = (self.now() - _as_msk(link.created_at)).total_seconds()
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
                name=f"{order.label} №{order.order_id}",
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
        # Адрес сделки — своей связке, рядом с real_lead_id/primary_lead_id: то,
        # что реально стоит в амо сейчас, а не то, что бот только собрался туда
        # дописать. Пусто в сделке — пусто и в связке, выдумывать нечего (ПД,
        # в лог не идёт; задача 1, ТЗ 2026-09-16).
        await self.store.update(order.order_id,
                                deal_address=field_value(existing, ids.FIELD_ADDRESS))
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
        # «Адрес»: заполненный не трогаем. В заказе адреса нет вовсе — он
        # подставляется из карточки клиента, а та заполнялась импортом из CRM и
        # с тех пор живёт своей жизнью. Фактический адрес работы владелец вносит
        # в календарь, оттуда его переносит в сделку календарный движок, и он
        # точнее. 2026-09-06: проведение заказа затёрло бы «п 3, эт 4, со стороны
        # двора» адресом «Менделеева 15 а» из карточки.
        if order.address and not field_value(existing, ids.FIELD_ADDRESS):
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
        # Вид работы, заданный самим источником, побеждает вывод по мастеру:
        # у уборки «Услуга» всегда «Уборка», кто бы ни был бригадиром.
        if order.service_kind:
            return SERVICE_ENUM_BY_KIND.get(order.service_kind)
        for name, _phone in order.masters:
            for key, enum_id in self.service_by_master.items():
                if key in (name or "").lower():
                    return enum_id
        return None                       # мастер неизвестен — поле не выдумываем

    def _master_enums(self, order: Order) -> tuple[int, ...]:
        # То же и со «Специалистом»: у уборки это всегда Ольга, а бригадир идёт
        # в примечание сделки (решение владельца 2026-09-10).
        if order.specialist_enums is not None:
            return tuple(order.specialist_enums)
        return self.specialists.resolve_many(order.masters)

    async def _close_tasks(self, order: Order, lead_id: Optional[int],
                           types: Sequence[int] | set[int]) -> int:
        """Закрыть автозадачи нужных типов. Возвращает, сколько закрыто."""
        closed = 0
        for task in await self.amo.get_lead_tasks(lead_id):
            if task.get("task_type_id") in types:
                await self._write("complete_task", order, task["id"],
                                  self.amo.complete_task(task["id"]))
                closed += 1
        return closed

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
            address=field_value(lead, ids.FIELD_ADDRESS),
        )


# Способы оплаты бота → значения поля «Вариант оплаты» в амо
# (маппинг подтверждён владельцем 2026-08-25, дополнен клинингом 2026-09-10).
# Ключи пишутся без «ё»: в боте способ называется «Расчётный», а в переписке и
# в старых записях встречается «Расчетный» — на выборе значения это сказываться
# не должно (см. `_payment_key`).
PAYMENT_ENUM_BY_METHOD = {
    # химчистка
    "наличные": ids.PAYMENT_ENUM_CASH,
    "карта дима": ids.PAYMENT_ENUM_CARD,
    "карта женя": ids.PAYMENT_ENUM_CARD,
    "р/с": ids.PAYMENT_ENUM_WIRE,
    # клининг: свои названия кнопок в боте бригадира
    "карта": ids.PAYMENT_ENUM_CARD,
    "расчетный": ids.PAYMENT_ENUM_WIRE,
    "подарочный сертификат": ids.PAYMENT_ENUM_CERTIFICATE,
}

# Способы, которые в амо означают безнал: тип клиента — «Юр лицо», а у химчистки
# ещё и «ждём оплату по счёту». У уборки такого ожидания нет: деньги вносят
# при проведении, поэтому сделка идёт до конца.
WIRE_METHODS = frozenset({"р/с", "расчетный"})

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
        "address": info.address,
    }


def _payment_key(method: Optional[str]) -> str:
    """Способ оплаты в виде, пригодном для сравнения: без регистра и без «ё»."""
    return (method or "").strip().lower().replace("ё", "е")


def _payment_enum(method: Optional[str]) -> Optional[int]:
    return PAYMENT_ENUM_BY_METHOD.get(_payment_key(method))


def _is_wire(order: Order) -> bool:
    return _payment_key(order.payment_method) in WIRE_METHODS


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
