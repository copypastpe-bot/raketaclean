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
class AmoLink:
    """Строка adminbot.amo_links: что робот знает и уже сделал по заказу."""

    order_id: int
    phone10: str
    status: str                       # new|in_progress|waiting_owner|waiting_salesbot|done|error
    path: Optional[str] = None        # A|B|C|D — путь заказа из дизайна §4
    primary_lead_id: Optional[int] = None
    real_lead_id: Optional[int] = None
    checklist: dict[str, Any] = field(default_factory=dict)   # шаг → время выполнения
    question_msg_id: Optional[int] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
