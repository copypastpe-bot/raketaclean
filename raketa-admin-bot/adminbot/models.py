"""Объекты предметной области: заказ бота и привязка заказа к сделкам амо."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional


@dataclass(frozen=True)
class Order:
    """Закрытый мастером заказ из БД рабочего бота (только чтение)."""

    order_id: int                     # «Заказ №N» = orders.id, отдельного счётчика нет
    phone10: Optional[str]            # 10 цифр номера клиента; None — номер не распознан
    created_at: datetime              # момент закрытия заказа мастером
    # Финальная сумма чека = бюджет сделки в амо (решение владельца №8, уточнено 2026-08-25).
    # Доп. продажу (orders.upsale_amount) сюда НЕ подмешиваем: в амо её нет,
    # она нужна только для расчёта зарплаты мастера внутри бота.
    amount_total: Decimal
    # Мастера заказа, основной первым: (имя, телефон). Телефон нужен, чтобы связать
    # мастера со «Специалистом» в амо — так различаются уборка и химчистка
    # одному клиенту в один день.
    masters: list[tuple[str, Optional[str]]] = field(default_factory=list)
    rating_score: Optional[int] = None   # оценка клиента: есть → задачу «Получить ОС» закрываем
    client_name: Optional[str] = None
    address: Optional[str] = None
    payment_method: Optional[str] = None   # «Наличные» | «Карта Дима» | «Карта Женя» | «р/с»
    # У клиента уже были заказы в боте. Нужно для «Источника сделки»:
    # первый раз — сарафан, дальше — «Повторный заказ».
    is_repeat_client: bool = False
    # Оплата по счёту ещё не поступила. Тогда сделку не проводим до конца, а оставляем
    # на «Заказ выполнен»: сейлзбот поставит задачу получить оплату (решение владельца).
    awaiting_wire_payment: bool = False

    @property
    def order_date(self):
        """Дата заказа — по ней матчер сверяется со сделками амо."""
        return self.created_at.date()

    @property
    def master_names(self) -> list[str]:
        return [name for name, _ in self.masters]


@dataclass(frozen=True)
class CarpetLink:
    """Строка adminbot.carpet_links: что робот знает о заказе партнёра по коврам.

    Ключ — номер заказа в CRM партнёра: он сквозной и приходит в каждом отчёте,
    поэтому месячный свод не приведёт к повторной обработке.
    """

    partner_id: int
    phone10: str
    status: str                       # new|in_progress|waiting_owner|waiting_salesbot|done|error
    # Как ведём заказ: сделка уже есть (None), через лид первичной («primary»)
    # или цепочкой с нуля («scratch»).
    path: Optional[str] = None
    lead_id: Optional[int] = None     # сделка ковровой воронки
    primary_lead_id: Optional[int] = None   # лид первичной, если цепочку вели с него
    checklist: dict[str, Any] = field(default_factory=dict)
    question: Optional[dict[str, Any]] = None
    question_msg_id: Optional[int] = None
    last_error: Optional[str] = None
    source_file: Optional[str] = None       # из какого вложения пришла строка
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class CalendarLink:
    """Строка adminbot.gcal_events: что робот знает о записи календаря.

    Ключ — идентификатор записи в Google: он вечный и переживает правки, поэтому
    повторный обмен по той же записи не заводит вторую сделку.
    """

    event_id: str
    kind: str                         # order|block|boat|rewash|unsettled|skip
    status: str                       # new|in_progress|waiting_salesbot|waiting_owner|
                                      # done|skipped|cancelled|error
    phone10: Optional[str] = None
    order_date: Optional[Any] = None  # date; заказ ведём по московской дате
    client_name: Optional[str] = None
    district: Optional[str] = None    # None — приставка заголовка непонятна
    services: tuple[str, ...] = ()
    event_data: Optional[dict[str, Any]] = None   # разбор записи: им продолжают цепочку
    skip_reason: Optional[str] = None
    path: Optional[str] = None        # A|B|C — как ведём запись
    primary_lead_id: Optional[int] = None
    real_lead_id: Optional[int] = None
    order_id: Optional[int] = None    # заказ бота, если он уже пришёл
    checklist: dict[str, Any] = field(default_factory=dict)
    question: Optional[dict[str, Any]] = None
    question_msg_id: Optional[int] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class AmoLink:
    """Строка adminbot.amo_links: что робот знает и уже сделал по заказу."""

    order_id: int
    phone10: str
    status: str                       # new|in_progress|waiting_owner|waiting_salesbot|done|error
    path: Optional[str] = None        # A|B|C|D — путь заказа из дизайна §4
    primary_lead_id: Optional[int] = None
    real_lead_id: Optional[int] = None
    checklist: dict[str, Any] = field(default_factory=dict)   # шаг → время выполнения
    # Вопрос владельцу: причина и варианты сделок на выбор. Хранится рядом с заказом,
    # чтобы карточку можно было отправить (или переотправить) в любой момент.
    question: Optional[dict[str, Any]] = None
    question_msg_id: Optional[int] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
