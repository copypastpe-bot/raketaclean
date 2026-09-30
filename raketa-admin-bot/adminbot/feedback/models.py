"""Модели цикла «Повторный заказ»: оценка клиента и состояние обработки.

Оценённый заказ (`RatedOrder`) и то, что цикл уже сделал по нему
(`FeedbackState`) — раздельно: первое читается заново из `public.orders` и
`adminbot.amo_links` каждый проход, второе живёт в `adminbot.feedback_state`
(миграция 019) и меняется только через `PgFeedbackStore.update`.

Вид работы (`kind`, миграция 020): химчистка (`public.orders`) и уборка
(`public.cleaning_orders`) нумеруются независимо, поэтому заказ №12 и уборка
№12 — разные работы. Ключ состояния — `(kind, order_id)` в пределах режима.

ТЗ docs/plans/2026-09-28-feedback-tasks.md, задача 2;
ТЗ docs/plans/2026-09-30-cleaning-ratings.md, задача 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

MODE_LIVE = "live"
MODE_REHEARSAL = "rehearsal"

KIND_ORDER = "order"          # химчистка: public.orders, связка adminbot.amo_links
KIND_CLEANING = "cleaning"    # уборка: public.cleaning_orders, связка adminbot.cleaning_links

# Ключ строки состояния в пределах режима: (вид работы, номер).
FeedbackKey = tuple[str, int]

STATUS_NEW = "new"                  # увидели, ещё не закончили (ждём задачу или повторяем после сбоя)
STATUS_CONTACT_SET = "contact_set"  # 1–4: комментарий и «Связаться» есть, «Повторный заказ» ещё не закрыт
STATUS_DONE = "done"                # всё сделано
STATUS_SKIPPED = "skipped"          # делать нечего (решения 6 и 8, 30 дней без задачи)
STATUS_DRY_RUN = "dry_run"          # репетиция отчиталась
STATUS_FAILED = "failed"            # 3 сбоя подряд, владельцу ушло письмо

OPEN_STATUSES = (STATUS_NEW, STATUS_CONTACT_SET)


@dataclass(frozen=True)
class RatedOrder:
    """Заказ, который клиент оценил, со сделкой реализации, где ведём разговор."""

    order_id: int
    lead_id: int                  # сделка реализации: adminbot.amo_links.real_lead_id
    score: int                    # orders.rating_score
    comment: Optional[str]        # orders.rating_comment — сырой ответ клиента
    replied_at: datetime          # orders.rating_replied_at (aware)
    kind: str = KIND_ORDER        # вид работы: KIND_ORDER | KIND_CLEANING

    @property
    def key(self) -> FeedbackKey:
        return (self.kind, self.order_id)


@dataclass(frozen=True)
class FeedbackState:
    """Строка `adminbot.feedback_state`: что цикл уже сделал по заказу в режиме."""

    order_id: int
    mode: str
    status: str = STATUS_NEW
    contact_task_id: Optional[int] = None   # «Связаться» поставлена (номер; в репетиции — 0)
    note_added: bool = False                # комментарий записан
    attempts: int = 0
    last_error: Optional[str] = None
    kind: str = KIND_ORDER                  # вид работы: KIND_ORDER | KIND_CLEANING

    @property
    def key(self) -> FeedbackKey:
        return (self.kind, self.order_id)
