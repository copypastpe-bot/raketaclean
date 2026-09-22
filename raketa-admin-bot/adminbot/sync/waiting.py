"""Общий счётчик ожидания автосделки: секунды с отметки шага чек-листа.

Раньше все три движка (календарь, заказы, ковры) мерили ожидание сейлзбота
от `link.updated_at` — а его тем же проходом двигает сам шаг `wait_salesbot`:
`_run_checklist` перед `StepResult(wait=True)` всегда зовёт `store.update(...,
status="waiting_salesbot")`, и это обновляет `updated_at`, даже когда статус
не менялся. Таймер обнулялся на каждом опросе, и вопрос владельцу «сейлзбот
не создал автосделку» не приходил никогда (дефект найден 22.09, задача 2 ТЗ
`docs/plans/2026-09-22-order-chain.md`).

Считаем вместо этого от отметки самого шага передачи в работу —
`checklist["move_primary_success"]`. В базе это ISO-строка с зоной
(`to_jsonb(now())`, `db.py:1076`); в памяти (`MemoryCalendarStore` и её
аналоги для заказов и ковров) кладут `datetime.isoformat()` — тоже строка,
но функция терпима и к `datetime` напрямую, если где-то положат его.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from adminbot.amo.fields import MOSCOW_TZ


def waited_since(checklist: Optional[dict[str, Any]], step: str,
                 now: datetime) -> Optional[float]:
    """Секунды с отметки шага `step` в `checklist` до `now`.

    Отметки нет — `None`: вызывающий код сам решает, что считать началом
    отсчёта (обычно `created_at` связки).
    """
    stamp = (checklist or {}).get(step)
    if stamp is None:
        return None
    if isinstance(stamp, datetime):
        moment = stamp
    elif isinstance(stamp, str):
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MOSCOW_TZ)
    return (now - moment.astimezone(MOSCOW_TZ)).total_seconds()
