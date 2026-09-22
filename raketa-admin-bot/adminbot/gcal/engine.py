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
- **Заполненное не переписываем — кроме адреса, комментария и дня работы.** Правки
  владельца в CRM важнее догадок робота, но эти поля живут в календаре: амо
  подставляет в сделку адрес из карточки клиента (адрес прошлого заказа), а в
  комментарии лида часто стоит огрызок от заявки. Состав заказа и куда ехать
  мастеру владелец пишет в записи (его решение 2026-08-27), день работы — тоже
  (решение 2026-09-02, отменяет прежнее решение 5). Переносом считается смена
  дня: время внутри дня владелец ставит в CRM сам, и робот его не трогает.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Callable, Optional

from adminbot.amo import ids
from adminbot.amo.client import AmoError
from adminbot.amo.fields import (
    MOSCOW_TZ, checkbox_field, datetime_field, enum_field, enums_field, field_value,
    lead_contact_ids, order_date_msk, text_field,
)
from adminbot.gcal.event import EventKind, ParsedEvent
from adminbot.gcal.matcher import match_event
from adminbot.gcal.store import CalendarStore
from adminbot.models import CalendarLink
from adminbot.phone import mask, normalize_phone
from adminbot.sync.matcher import LeadInfo
from adminbot.sync.waiting import waited_since

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

# Решения владельца «дальше веду сам»: кнопки «✋ Сам разберусь» на карточке-
# вопросе и «✋ Оставить как есть» на карточке отмены. До 2026-09-04 они не
# держались: запись оставалась в статусе `skipped`, а следующий проход
# принимал это за снятую пометку «⁉️» и возвращал её в работу — робот шёл
# и переписывал поля сделки поверх того, что владелец правил руками.
OWNER_HANDLES_REASON = "владелец разбирается сам"
OWNER_KEEPS_REASON = "владелец оставил сделку как есть"

# Причины, по которым робот к записи больше не подходит.
HANDS_OFF_REASONS: tuple[str, ...] = (
    KEPT_OUT_REASON, OWNER_HANDLES_REASON, OWNER_KEEPS_REASON,
)

# Чем кончается пометка сделки: «оставить как есть» — отменённой записью,
# «сам разберусь» — пропущенной. Разница только в отчётности, работать робот
# не станет ни с той, ни с другой.
FINAL_AFTER_MARK: dict[str, str] = {
    OWNER_KEEPS_REASON: "cancelled",
    OWNER_HANDLES_REASON: "skipped",
}

# Чем робот закрывает задачи отменённого заказа (решение владельца 2026-09-10):
# висящая задача по отменённому заказу требует внимания зря.
CANCEL_TASK_RESULT = "🤖 Заказ отменён: запись удалена из календаря"

# Удаление записи, а дочки воронки 2 нет и по примечанию не нашлась (задача 4
# ТЗ 2026-09-22): закрывать нечего, а лид воронки 1 робот не закрывает никогда —
# это решение владельца 5, а не выбор робота. Owner получает короткий отчёт.
NO_REALIZATION_REASON = "сделки реализации нет, в CRM ничего не трогал"


def _hands_off(link: CalendarLink) -> bool:
    """Робот в эту запись не лезет — так решил владелец или так вышло само."""
    return (link.skip_reason or "").startswith(HANDS_OFF_REASONS)


def _kept_out_of_work(link: CalendarLink) -> bool:
    return (link.skip_reason or "").startswith(KEPT_OUT_REASON)


# Что робот подтягивает в уже заведённую сделку, если запись поправили
# (решение владельца 2026-08-27). Даты здесь нет намеренно: её при переносе
# робот не правит, фактическую впишет заказ из бота.
CHANGEABLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("address", "адрес"),
    ("comment", "комментарий"),
    ("services", "услуга"),
    ("district", "район"),
    # Перенос заказа на другой день — тоже правка записи (решение владельца
    # 2026-09-02). Раньше его здесь не было, и сделка молча оставалась со старым
    # днём: ни поля не менялись, ни владельцу об этом не говорили.
    ("order_date", "дата"),
)

# Отчёт владельцу о правке (задача 3 ТЗ 2026-09-22, правка по ревью 22.09):
# сбой на любой из двух сделок не должен ронять весь проход и не должен
# отменять правку другой — обе строки добавляются к перечню того, что
# поменялось, через тот же `self.last_edits`, других путей уведомления не
# заводим. Раньше `AmoError` на дочке улетала наружу из `_apply_edits`
# необработанной, а `_refresh` к этому моменту уже переписал `event_data` —
# следующий проход не видел бы правку вовсе, и дочка молча оставалась со
# старым адресом.
PRIMARY_LEAD_EDIT_FAILED_NOTE = "лид воронки 1 не обновился — ошибка амо"
CHILD_LEAD_EDIT_FAILED_NOTE = "сделка реализации не обновилась — ошибка амо"


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
_ASK_KINDS = ("ask_owner", "ask_owner_stale", "ask_owner_closed")

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
    # Цепочка закончена прямо здесь, без вопроса владельцу и без следующих шагов
    # чек-листа: например, дочка из примечания уже закрыта в CRM руками
    # (задача 1, ревью 22.09) — правка владельца важнее догадки робота.
    stop: bool = False


class CalendarEngine:
    def __init__(
        self,
        *,
        amo: Any,
        store: CalendarStore,
        dry_run: bool = True,
        salesbot_wait_sec: int = DEFAULT_SALESBOT_WAIT_SEC,
        child_by_note: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(MOSCOW_TZ),
    ) -> None:
        self.amo = amo
        self.store = store
        self.dry_run = dry_run
        self.salesbot_wait_sec = salesbot_wait_sec
        # Дочку воронки 2 берём по служебному примечанию сейлзбота, а не как
        # «первую свободную» сделку по телефону (задача 1 ТЗ 2026-09-22).
        # Выключено — старое поведение (AMO_CHILD_BY_NOTE, откат одной командой).
        self.child_by_note = child_by_note
        self.now = now
        # У каждого движка свои черновики: в сервисе их работает несколько сразу
        # (наблюдатель, репетиция), и путать их расчёты нельзя.
        self._scratch: dict[str, _Scratch] = {}
        # Что подтянулось в сделку при последней правке записи: наблюдатель
        # берёт это, чтобы сказать владельцу, что именно поменялось.
        self.last_edits: tuple[str, ...] = ()

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
        #
        # Сбой амо здесь не переводит запись в `error` (ревью 22.09, задача 4):
        # решение владельца — не робота, и следующий проход обязан повторить
        # именно `closing`/`marking`, а не спросить заново или промолчать.
        if link.status == "closing":
            try:
                return await self._close_deal(link)
            except AmoError as exc:
                log.warning("Календарь, запись %s: закрытие сделки не задалось — %s",
                            event.event_id, exc)
                return await self.store.update(
                    event.event_id, last_error=f"{type(exc).__name__}: {exc}")

        # Владелец нажал «✋ Сам разберусь» или «✋ Оставить как есть»: ставим
        # галочку в самой сделке. Отсюда, а не из Telegram, по той же причине,
        # что и закрытие: решение переживёт перезапуск робота.
        if link.status == "marking":
            try:
                return await self._mark_owner_handles(link)
            except AmoError as exc:
                log.warning("Календарь, запись %s: пометка «ведёт владелец» не задалась — %s",
                            event.event_id, exc)
                return await self.store.update(
                    event.event_id, last_error=f"{type(exc).__name__}: {exc}")

        if event.kind is EventKind.CANCELLED:
            return await self._handle_cancelled(link)

        # Сведения записи держим свежими всегда: владелец правит календарь и после
        # того, как робот отработал, а телефон и дата нужны ему позже — в карточке
        # отмены и в проверке, не занята ли сделка другой записью.
        if event.kind not in SILENT_KINDS and event.kind is not EventKind.BOAT:
            before = link
            link = await self._refresh(event, link)
            if link.status == "done" and not _hands_off(link):
                # Запись поправили после проведения — доносим правку до сделки.
                await self._apply_edits(event, link, before)
                # Сбой на любой из сделок внутри `_apply_edits` пишет `last_error`
                # напрямую в хранилище — перечитываем, иначе вызывающий код
                # его не увидит (задача 3 ТЗ 2026-09-22).
                link = await self.store.get(event.event_id) or link

        if link.status in ("done", "waiting_owner", "cancelled"):
            return link                        # закончили или ждём ответа владельца

        if _hands_off(link):
            # Запись лежала в календаре до включения либо владелец сказал
            # «веду сам»: правка записи не делает этот заказ нашей работой
            # (решение 8 и решение владельца 2026-09-04).
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

    async def _resolve_child(self, link: CalendarLink) -> CalendarLink:
        """Дочка воронки 2 для вопроса и закрытия (задача 4 ТЗ 2026-09-22).

        `real_lead_id` уже известен — он и есть ответ, второй раз не ищем.
        Иначе ищем по примечанию сейлзбота **всегда**, независимо от
        выключателя `child_by_note`: здесь это чтение, а не решение, и старое
        поведение — закрыть лид воронки 1 вместо неизвестной дочки — владелец
        запретил прямо (решение владельца 5). Найденное запоминаем в связке и
        в журнале действий (`child_by_note`), чтобы закрытие уже не искало
        заново, даже если пройдёт отдельным вызовом.

        `AmoError` наружу не гасим: вызывающая сторона (`_handle_cancelled`,
        `_close_deal`) сама решает, что делать со сбоем амо — падать здесь
        значило бы то же самое молчаливое зависание записи, из-за которого
        эту функцию и выделили (ревью 22.09).
        """
        if link.real_lead_id is not None or link.primary_lead_id is None:
            return link
        found = await self.amo.get_child_lead_id(link.primary_lead_id)
        if found is None:
            return link
        link = await self.store.update(link.event_id, real_lead_id=found) or link
        await self.store.log(link.event_id, "child_by_note", dry_run=self.dry_run,
                             payload={"parent": link.primary_lead_id, "child": found})
        return link

    async def _close_deal(self, link: CalendarLink) -> CalendarLink:
        """Закрыть сделку как несостоявшуюся — по прямому подтверждению владельца.

        Закрываем только дочку воронки 2 — никогда лид воронки 1 (задача 4 ТЗ
        2026-09-22, решение владельца 5). Дочку ищем тем же помощником, что и
        вопрос владельцу: старая запись, застрявшая в `waiting_owner` только
        с `primary_lead_id` (до задачи 4, либо после сбоя амо на вопросе —
        см. `_handle_cancelled`), находит дочку здесь же, а не проваливается
        в лид воронки 1.
        """
        link = await self._resolve_child(link)
        lead_id = link.real_lead_id
        if lead_id is None:
            return await self.store.update(link.event_id, status="cancelled",
                                           skip_reason=NO_REALIZATION_REASON)
        lead = await self._get_lead(lead_id)
        pipeline_id = int((lead or {}).get("pipeline_id") or ids.PIPELINE_REALIZATION)

        if lead is not None and int(lead.get("status_id") or 0) in ids.STATUSES_FINAL:
            # Пока карточка висела, сделку закрыли руками — второй раз не трогаем.
            return await self.store.update(link.event_id, status="cancelled",
                                           skip_reason="сделка уже закрыта")

        # Сначала задачи и причина, закрытие — последним: это единственный
        # необратимый шаг, и порядок совпадает с тем, как закрывает человек.
        # Сбой посередине не страшен: следующий проход начнёт заново, а уже
        # закрытые задачи амо открытыми больше не отдаст.
        closed_tasks = 0
        for task in await self.amo.get_lead_tasks(lead_id):
            await self.store.log(link.event_id, "complete_task", dry_run=self.dry_run,
                                 entity="task", amo_id=task["id"],
                                 payload={"result": CANCEL_TASK_RESULT})
            await self.amo.complete_task(task["id"], CANCEL_TASK_RESULT)
            closed_tasks += 1

        # «Причина закрытия» — как «Услуга»: заполненное человеком не трогаем.
        # Само значение читает рабочий бот: по нему он пишет клиенту об отмене.
        if field_value(lead, ids.FIELD_CLOSE_REASON) is None:
            await self.store.log(link.event_id, "update_lead", dry_run=self.dry_run,
                                 entity="lead", amo_id=lead_id,
                                 payload={"field": ids.FIELD_CLOSE_REASON})
            await self.amo.update_lead(lead_id, custom_fields=[
                enum_field(ids.FIELD_CLOSE_REASON, ids.CLOSE_REASON_ENUM_NO_NEED)])
            reason_note = "Причина: «Пропала потребность»."
        else:
            reason_note = "Причина закрытия оставлена как была."

        await self.store.log(link.event_id, "move_lead", dry_run=self.dry_run,
                             entity="lead", amo_id=lead_id,
                             payload={"status_id": ids.STATUS_CLOSED})
        await self.amo.move_lead(lead_id, pipeline_id, ids.STATUS_CLOSED)
        # Дата заказа — из записи (задача 4 ТЗ 2026-09-22): без неё в сделке не
        # видно, о каком именно дне речь, если у клиента заказов было несколько.
        order_line = f" Заказ был на {link.order_date:%d.%m.%Y}." if link.order_date else ""
        await self.amo.add_note(
            lead_id,
            f"🤖 Заказ отменён: запись удалена из календаря.{order_line} "
            "Закрыто по подтверждению владельца. "
            f"Закрыто задач: {closed_tasks}. {reason_note}")
        return await self.store.update(link.event_id, status="cancelled",
                                       skip_reason="сделка закрыта по вашему подтверждению")

    async def _mark_owner_handles(self, link: CalendarLink) -> CalendarLink:
        """Пометить сделку галочкой «заказ ведёт владелец».

        Галочка нужна не нам, а рабочему боту (tgbot-v1): он пишет клиенту
        письмо «Ваш заказ принят» и вопрос-подтверждение за сутки, а про наши
        кнопки не знает и знать не может — общий язык у роботов только амо.
        2026-09-04 клиентка отменила работу, владелец нажал «Оставить как есть»
        и договорился созвониться через неделю, а рабочий бот всё равно
        собирался спросить её про заказ.

        Сделки может и не быть — тогда помечать нечего, и это нормальный исход,
        а не ошибка: робот просто отступает.
        """
        reason = link.skip_reason or OWNER_HANDLES_REASON
        final = FINAL_AFTER_MARK.get(reason, "skipped")
        lead_id = link.real_lead_id or link.primary_lead_id
        if lead_id is None:
            return await self.store.update(link.event_id, status=final,
                                           skip_reason=reason)

        await self.store.log(link.event_id, "update_lead", dry_run=self.dry_run,
                             entity="lead", amo_id=lead_id,
                             payload={"field": ids.FIELD_OWNER_HANDLES})
        await self.amo.update_lead(
            lead_id, custom_fields=[checkbox_field(ids.FIELD_OWNER_HANDLES, True)])
        await self.amo.add_note(lead_id, "🤖 Заказ ведёт владелец: роботы эту "
                                         "сделку не трогают.")
        return await self.store.update(link.event_id, status=final,
                                       skip_reason=reason)

    async def _handle_cancelled(self, link: CalendarLink) -> CalendarLink:
        """Запись удалена = заказ отменён.

        Сделку робот сам не закрывает (решение владельца 2): удаление бывает и
        переносом, и случайностью, а закрытая сделка портит статистику. Спрашиваем
        только если есть что закрывать — иначе просто отмечаем отмену.

        Сделка для вопроса — только дочка воронки 2, лид воронки 1 робот не
        трогает никогда, даже когда дочки нет (задача 4 ТЗ 2026-09-22, решение
        владельца 5). Дочку ищет общий помощник `_resolve_child`.

        Сбой амо в любой точке этой подготовки — на поиске дочки или на
        проверке её статуса — не должен ронять проход и морозить запись
        (ревью 22.09): раньше исключение гасило наблюдатель, статус записи не
        менялся, курсор синка всё равно уходил вперёд, и Google второй раз про
        удаление уже не сообщит — запись зависла бы навсегда без ответа
        владельцу. Поэтому всё тело — под одним перехватом: сбой на любом шаге
        задаёт владельцу тот же вопрос вслепую, без подтверждённого статуса
        дочки; точный статус уточнит `_close_deal`, когда владелец подтвердит
        закрытие. Ветка «дочки нет вовсе» в амо не ходит и под перехват не
        попадает — ей нечему падать.
        """
        try:
            link = await self._resolve_child(link)
            lead_id = link.real_lead_id
            if lead_id is None:
                return await self.store.update(link.event_id, status="cancelled",
                                               skip_reason=NO_REALIZATION_REASON)

            lead = await self._get_lead(lead_id)
            if lead is not None and int(lead.get("status_id") or 0) in ids.STATUSES_FINAL:
                # Сделка уже проведена: работа сделана, запись убрали для порядка.
                return await self.store.update(link.event_id, status="cancelled",
                                               skip_reason="запись удалена, сделка уже закрыта")

            return await self._ask_owner(link, "заказ отменён — закрыть сделку?",
                                         payload={"lead_id": lead_id})
        except AmoError as exc:
            log.warning("Календарь, запись %s: подготовка вопроса об отмене не удалась — %s",
                        link.event_id, exc)
            link = await self._ask_owner(
                link, "заказ отменён — закрыть сделку?",
                payload={"lead_id": link.real_lead_id, "child_lookup_failed": True})
            return await self.store.update(
                link.event_id, last_error=f"{type(exc).__name__}: {exc}") or link

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

        if decision.forgotten:
            # Работе такие сделки не мешают, но владельцу о них надо сказать:
            # это забытый в CRM мусор, и он копится (решение владельца 2026-09-02).
            await self.store.log(
                event.event_id, "note_forgotten", dry_run=self.dry_run,
                payload={"lead_ids": list(decision.forgotten),
                         "dates": _forgotten_dates(candidates, decision.forgotten)})

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
            if result.stop:
                return link           # шаг сам завершил цепочку (см. StepResult.stop)

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

        `child_by_note` включён: дочка — та, что названа в служебном примечании
        сейлзбота у лида воронки 1 (задача 1 ТЗ 2026-09-22). Примечание не может
        быть чужим, поэтому отсев «занята другой записью» здесь не нужен.

        Выключено (старое поведение): берём первую СВОБОДНУЮ сделку по телефону.
        У клиента бывает два заказа подряд, и сделка предыдущей записи открыта
        и видна по тому же телефону: без отсева второй заказ прицепился бы
        к чужой сделке, а при отмене робот предложил бы закрыть не ту.
        """
        if self.child_by_note:
            child_id = await self.amo.get_child_lead_id(link.primary_lead_id)
            if child_id is not None:
                # Дочку привязываем всегда — так гласит примечание, это факт
                # цепочки. Но прежде чем писать в неё дальше (fill_realization
                # и т.д.), проверяем, не закрыта ли она уже руками: правка
                # владельца в CRM важнее догадки робота (тот же принцип, что
                # у already_done). Сделка удалена (get_lead вернул None) —
                # тоже считаем закрытой.
                await self.store.update(event.event_id, real_lead_id=child_id,
                                        status="in_progress")
                await self.store.log(event.event_id, "child_by_note", dry_run=self.dry_run,
                                     payload={"parent": link.primary_lead_id,
                                              "child": child_id})
                child_lead = await self._get_lead(child_id)
                status_id = (int(child_lead.get("status_id") or 0)
                            if child_lead is not None else None)
                if child_lead is None or status_id in ids.STATUSES_FINAL:
                    await self.store.log(event.event_id, "child_closed", dry_run=self.dry_run,
                                         payload={"child": child_id, "status_id": status_id})
                    await self.store.update(
                        event.event_id, status="done",
                        skip_reason="сделка реализации уже закрыта в CRM, не трогал")
                    return StepResult(stop=True)
                return StepResult()
        else:
            taken = await self.store.taken_leads(event.phone10,
                                                 exclude_event_id=event.event_id)
            for lead in await self.amo.find_leads_by_phone(event.phone10):
                info = _to_lead_info(lead)
                if (info.pipeline_id == ids.PIPELINE_REALIZATION and info.is_open
                        and info.lead_id not in taken):
                    await self.store.update(event.event_id, real_lead_id=info.lead_id,
                                            status="in_progress")
                    return StepResult()

        # Считаем от отметки «передано в работу», а не от updated_at: его
        # двигает сам этот шаг на каждом проходе (задача 2 ТЗ 22.09).
        waited = waited_since(link.checklist, "move_primary_success", self.now())
        if waited is None:
            waited = (self.now() - _as_msk(link.created_at)).total_seconds()
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

        # «Дата и время заказа» — третье исключение из правила «заполненное не
        # трогаем» (решение владельца 2026-09-02, отменяет прежнее решение 5).
        # Клиент переносит и отменяет заказы, и сделка должна показывать тот день,
        # на который он записан сейчас, а не тот, на который записывались сперва.
        # Сравниваем по ДНЮ: время внутри дня владелец ставит в CRM сам, в записи
        # календаря стоит начало работы — переписывать его нечем и незачем.
        if event.order_date and order_date_msk(existing) != event.order_date:
            moment = event.start_at or datetime.combine(
                event.order_date, time(hour=12), tzinfo=MOSCOW_TZ)
            fields.append(datetime_field(ids.FIELD_ORDER_DATETIME, moment))

        # Адрес — исключение из правила «заполненное не трогаем» (решение владельца
        # 2026-08-27). В сделку амо подставляет адрес из карточки клиента, то есть
        # адрес прошлого заказа; куда ехать мастеру сегодня, написано в календаре.
        # В карточку клиента робот адрес не пишет: заказ бывает и не по его адресу.
        if event.address and field_value(existing, ids.FIELD_ADDRESS) != event.address:
            fields.append(text_field(ids.FIELD_ADDRESS, event.address))

        # Комментарий — второе исключение (решение владельца 2026-08-27). В лиде
        # часто стоит огрызок от заявки («Хим чи»), а состав заказа с ценами
        # владелец пишет в календарь: он и должен оказаться в сделке.
        if event.comment and field_value(existing, ids.FIELD_COMMENT) != event.comment:
            fields.append(text_field(ids.FIELD_COMMENT, event.comment))

        district_enum = ids.DISTRICT_ENUMS.get(event.district or "")
        if district_enum and not field_value(existing, ids.FIELD_DISTRICT):
            fields.append(enums_field(ids.FIELD_DISTRICT, [district_enum]))

        service_enums = [SERVICE_ENUMS[kind] for kind in event.services
                         if kind in SERVICE_ENUMS]
        if service_enums and not field_value(existing, ids.FIELD_SERVICE):
            fields.append(enums_field(ids.FIELD_SERVICE, service_enums))

        # «Источник сделки» — правило владельца 2026-08-27:
        #   лид есть, источник в нём не указан      → «Сарафанное радио»;
        #   ни лида, ни контакта (заводим с нуля)   → «Сарафанное радио»;
        #   лида нет, а контакт в CRM есть          → «Повторный заказ».
        # Указанный источник не трогаем никогда: там правда о том, откуда клиент.
        if not field_value(existing, ids.FIELD_SOURCE):
            scratch = self._scratch.get(event.event_id) or _Scratch()
            repeat = creating and not scratch.is_new_client
            fields.append(enums_field(
                ids.FIELD_SOURCE,
                [ids.SOURCE_ENUM_REPEAT if repeat else ids.SOURCE_ENUM_WORD_OF_MOUTH]))
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
            # Разбор целиком: по нему видно, что именно поправил владелец в записи,
            # и им же продолжают незаконченную цепочку после перезапуска.
            "event_data": event.to_dict(),
        }
        changed = {name: value for name, value in updates.items()
                   if getattr(link, name, None) != value}
        if link.status == "skipped" and not _hands_off(link):
            # Владелец снял пометку «⁉️» — запись снова в работе (решение 9).
            # Его собственное «веду сам» сюда не попадает: это не снятая
            # пометка, а прямое указание не трогать заказ.
            changed["status"] = "new"
            changed["skip_reason"] = None
        if not changed:
            return link
        return await self.store.update(event.event_id, **changed) or link

    async def _apply_edits(self, event: ParsedEvent, link: CalendarLink,
                           before: CalendarLink) -> None:
        """Подтянуть правку записи в уже заведённые сделки.

        Сравниваем с тем, что робот помнил о записи: так правка видна, даже
        если в самой сделке владелец успел что-то поменять.

        Правим сначала лид воронки 1 (если есть), потом дочку воронки 2
        (если есть) — оба одним и тем же расчётом полей, но каждый по
        своему свежему состоянию в амо (решение владельца 22.09, задача 3
        ТЗ: «менеджер изменил запись → робот меняет сначала лид воронки 1,
        потом дочку»). К этому моменту лид воронки 1 обычно уже закрыт как
        успешный; принимает ли амо правку поля закрытого лида — предположение,
        проверить на репетиции.

        Сбой на одной сделке не отменяет правку другой и не должен вылетать
        наружу необработанным (правка по ревью 22.09): обе сделки
        независимы, и упавшая — не повод молчать о второй или ронять весь
        проход. Причины всех сбоев складываются в `last_error` одной
        строкой, а в отчёт владельцу — по строке на каждую упавшую сделку.
        """
        lead_ids = [lead_id for lead_id in (link.primary_lead_id, link.real_lead_id)
                   if lead_id is not None]
        if not lead_ids:
            return

        previous = before.event_data or {}
        if not previous:
            return                             # прошлого разбора нет — сравнить не с чем

        changed = [title for name, title in CHANGEABLE_FIELDS
                   if _value_of(event, name) != _value_of_dict(previous, name)]
        if not changed:
            return

        reported = list(changed)
        attempted = False
        errors: list[str] = []
        for lead_id in lead_ids:
            existing = await self._get_lead(lead_id)
            fields = self._lead_fields(event, existing)
            if not fields:
                continue
            attempted = True
            note = (PRIMARY_LEAD_EDIT_FAILED_NOTE if lead_id == link.primary_lead_id
                   else CHILD_LEAD_EDIT_FAILED_NOTE)
            try:
                log.info("Календарь, запись %s: запись изменилась (%s) — обновляю сделку %s",
                         event.event_id, ", ".join(changed), lead_id)
                await self._write("update_lead", event, lead_id,
                                  self.amo.update_lead(lead_id, custom_fields=fields))
            except AmoError as exc:
                log.warning("Календарь, запись %s: правка сделки %s не задалась — %s",
                            event.event_id, lead_id, exc)
                errors.append(f"{type(exc).__name__}: {exc}")
                reported.append(note)

        if not attempted:
            return
        if errors:
            await self.store.update(event.event_id, last_error="; ".join(errors))
        self.last_edits = tuple(reported)       # наблюдателю — о чём сказать владельцу

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


def _forgotten_dates(candidates: list[LeadInfo], lead_ids: tuple[int, ...]) -> dict:
    """Когда завели забытые сделки — чтобы владелец видел возраст, а не только номер."""
    dates: dict[str, str] = {}
    for lead in candidates:
        if lead.lead_id not in lead_ids:
            continue
        when = lead.created_date or lead.order_date
        if when is not None:
            dates[str(lead.lead_id)] = when.isoformat()
    return dates


def _value_of(event: ParsedEvent, name: str) -> Any:
    """Значение поля записи в том виде, в каком его помнит хранилище."""
    return _comparable(getattr(event, name, None))


def _value_of_dict(data: dict, name: str) -> Any:
    return _comparable(data.get(name))


def _comparable(value: Any) -> Any:
    """Привести к виду, в котором значения записи сравниваются между собой.

    Хранилище держит разбор в JSON: дата там уже строка, а у свежей записи это
    `date`. Без приведения любая проверка «дата изменилась» отвечала бы «да»
    каждый проход и гоняла бы в амо одно и то же.
    """
    if isinstance(value, (list, tuple)):
        return tuple(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value
