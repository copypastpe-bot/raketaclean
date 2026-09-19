"""Сторож: определяет «сломано / работает» для технических зависимостей
обоих ботов (ТЗ 2026-09-18, задача 7).

Инциденты (задача 8, `notify.incidents`) — не эта задача и не этот модуль:
`Watchdog.check_once()` только вычисляет состояние и отдаёт список
`CheckResult` наружу. Следующий исполнитель подключит его к инцидентам сам
(вызывая `check_once()` из своего кода или подписавшись на него иначе) —
таблицу `notify.incidents` этот модуль не читает и не пишет.

Пять проверок ТЗ, первая — по числу ботов, отсюда семь ключей. Ключи —
русские фразы: комментарий к `notify.incidents.key` в миграции 001 называет
примером именно такие («пульс рабочего бота», «опрос amoCRM», «прокси») —
формат сохранён, чтобы задаче 8 не пришлось придумывать перевод:

* пульс рабочего бота     — `public.service_heartbeats` (задача 6, bot.py)
* пульс клиентского бота  — та же таблица, пишет чужой код (уже работает,
                             факт 7 ТЗ — эта служба его не меняет)
* пульс админ-бота        — `notify.service_heartbeats` (задача 6,
                             adminbot/heartbeat.py; своя таблица, потому что
                             админ-боту нельзя писать в схему public —
                             хард-правило проекта, миграция 002)
* опрос amoCRM            — `public.amocrm_api_state.updated_at`, закрывает
                             дыру факта 6 ТЗ (цикл `amocrm_api_polling_loop`
                             при ошибке авторизации выходит навсегда и молча)
* база данных             — SELECT 1 через пул самой службы
* прокси                  — Telegram getMe() тем же путём, что доставка
* рассыльщик клиентам     — `public.notification_outbox`: возраст последней
                             успешной отправки и не растёт ли очередь
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Sequence

from notifyd import db

log = logging.getLogger(__name__)

# service_key пульса — те же строки, что пишут сами боты (bot.py и
# adminbot/heartbeat.py). Общего модуля нет: три разных деплоя (уточнение
# координатора 2 ТЗ), поэтому значения совпадают буквально, а не по импорту.
WORKER_BOT_SERVICE_KEY = "telegram-bot-worker"
CLIENT_BOT_SERVICE_KEY = "telegram-bot-client"      # уже существует, факт 7 ТЗ
ADMIN_BOT_SERVICE_KEY = "raketa-admin-bot"

PUBLIC_HEARTBEATS_TABLE = "public.service_heartbeats"
NOTIFY_HEARTBEATS_TABLE = "notify.service_heartbeats"

KEY_WORKER_HEARTBEAT = "пульс рабочего бота"
KEY_CLIENT_HEARTBEAT = "пульс клиентского бота"
KEY_ADMIN_HEARTBEAT = "пульс админ-бота"
KEY_AMOCRM_POLL = "опрос amoCRM"
KEY_DATABASE = "база данных"
KEY_PROXY = "прокси"
KEY_DISPATCH = "рассыльщик клиентам"

# Уровень по умолчанию для каждой проверки — табличка владельца 19.09
# (ТЗ 2026-09-19, «Решения владельца»): техника, которую видят клиенты, —
# красная; личный инструмент владельца (админ-бот) ждёт до утра. Это только
# значение для ПЕРВОГО засева маршрута: последнее слово за справочником
# notify.routes, его читает notifyd.incidents.
DEFAULT_LEVELS = {
    KEY_WORKER_HEARTBEAT: "red",
    KEY_CLIENT_HEARTBEAT: "red",
    KEY_ADMIN_HEARTBEAT: "yellow",
    KEY_AMOCRM_POLL: "red",
    KEY_DATABASE: "red",
    KEY_PROXY: "red",
    KEY_DISPATCH: "red",
}

DEFAULT_POLL_INTERVAL_SEC = 60

# Куда цикл отдаёт готовые результаты — инцидентам (notifyd.incidents).
# Своего цикла у инцидентов нет намеренно, см. run_forever.
ResultsHandler = Callable[[Sequence["CheckResult"]], Awaitable[Any]]


@dataclass(frozen=True)
class CheckResult:
    """Результат одной проверки одного прохода сторожа."""

    key: str
    ok: bool
    detail: str
    # Уровень по умолчанию — из DEFAULT_LEVELS выше (табличка владельца 19.09).
    # Это рекомендация кода на случай, когда маршрута в справочнике ещё нет:
    # у заведённого маршрута уровень решает владелец, а не код.
    level: str = "red"


ProxyProbe = Callable[[], Awaitable[bool]]


class Watchdog:
    """Один цикл всех проверок. Свой выключатель, по умолчанию выключен —
    как у Postman и JournalAdapter: выключено — служба поднимается и молчит."""

    def __init__(self, *, pool: Any, proxy_probe: ProxyProbe, enabled: bool,
                 now: Optional[Callable[[], datetime]] = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
                 heartbeat_max_age_sec: int = 300,
                 amocrm_max_age_sec: int = 300,
                 db_timeout_sec: float = 5.0,
                 proxy_timeout_sec: float = 5.0,
                 dispatch_max_age_sec: int = 1800) -> None:
        self.pool = pool
        self.proxy_probe = proxy_probe
        self.enabled = enabled
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.poll_interval_sec = poll_interval_sec
        self.heartbeat_max_age_sec = heartbeat_max_age_sec
        self.amocrm_max_age_sec = amocrm_max_age_sec
        self.db_timeout_sec = db_timeout_sec
        self.proxy_timeout_sec = proxy_timeout_sec
        self.dispatch_max_age_sec = dispatch_max_age_sec
        # Межпроходная память нужна только «растёт ли очередь» (задача 7,
        # второй сигнал рассыльщика) — не переживает перезапуск службы,
        # сознательный выбор ради простоты, тем же приёмом, что курсор
        # переходника журнала (notifyd/journal_source.py). Обновляет её
        # только цикл: разовые вызовы (`check_once(remember=False)`) читают,
        # но не пишут, иначе замер достаётся не тому, кто его ждёт.
        self._prev_dispatch_pending: Optional[int] = None

    async def run_forever(self, stop: Optional[asyncio.Event] = None,
                          on_results: Optional[ResultsHandler] = None) -> None:
        """Единственный цикл проверок в службе.

        `on_results` — инциденты (задача 8). Отдельного цикла у них нет
        намеренно: два независимых цикла звали `check_once()` на одном и том
        же объекте и затирали друг другу межпроходную память «сколько было
        в очереди минуту назад», из-за чего рост очереди мог систематически
        не замечаться (замечание 2 ревью 18.09). Свой выключатель у инцидентов
        остался — он внутри обработчика, а не здесь: сторож должен уметь
        работать и без них, так устроен порядок выката.
        """
        if not self.enabled:
            log.info("notify: сторож выключен (выключатель в настройках), проверок не делаю")
            while stop is None or not stop.is_set():
                await self.sleep(self.poll_interval_sec)
            return

        while stop is None or not stop.is_set():
            try:
                results = await self.check_once()
                for result in results:
                    if result.ok:
                        log.info("notify: сторож — %s: работает", result.key)
                    else:
                        log.warning("notify: сторож — %s: сломано (%s)",
                                   result.key, result.detail)
                if on_results is not None:
                    await on_results(results)
            except Exception:                             # noqa: BLE001
                log.exception("notify: проход сторожа не удался")
            await self.sleep(self.poll_interval_sec)

    async def check_once(self, *, remember: bool = True) -> list[CheckResult]:
        """Один проход всех семи проверок. Каждая гасит собственное
        исключение (`_safe`) — падение одной (например, база недоступна)
        не должно скрыть остальные результаты: прокси проверяется вообще
        без похода в базу.

        `remember=False` — разовый вызов со стороны (команда «что сейчас
        сломано»): результаты отдаются, но межпроходная память не трогается.
        Иначе нажатие команды съедает у цикла замер очереди и рост
        рассыльщика теряется."""
        now = self._now()
        results = [
            await self._safe(KEY_WORKER_HEARTBEAT, self._check_heartbeat(
                key=KEY_WORKER_HEARTBEAT, table=PUBLIC_HEARTBEATS_TABLE,
                service_key=WORKER_BOT_SERVICE_KEY, label="рабочего бота", now=now)),
            await self._safe(KEY_CLIENT_HEARTBEAT, self._check_heartbeat(
                key=KEY_CLIENT_HEARTBEAT, table=PUBLIC_HEARTBEATS_TABLE,
                service_key=CLIENT_BOT_SERVICE_KEY, label="клиентского бота", now=now)),
            await self._safe(KEY_ADMIN_HEARTBEAT, self._check_heartbeat(
                key=KEY_ADMIN_HEARTBEAT, table=NOTIFY_HEARTBEATS_TABLE,
                service_key=ADMIN_BOT_SERVICE_KEY, label="админ-бота", now=now)),
            await self._safe(KEY_AMOCRM_POLL, self._check_amocrm(now)),
            await self._safe(KEY_DATABASE, self._check_database()),
            await self._safe(KEY_PROXY, self._check_proxy()),
            await self._safe(KEY_DISPATCH,
                             self._check_dispatch(now, remember=remember)),
        ]
        # Уровень проставляем в одном месте, чтобы каждая проверка не помнила
        # про табличку владельца — включая ту, что упала с исключением (_safe).
        return [replace(r, level=DEFAULT_LEVELS.get(r.key, r.level))
                for r in results]

    async def _safe(self, key: str, coro: Awaitable[CheckResult]) -> CheckResult:
        try:
            return await coro
        except Exception as exc:                         # noqa: BLE001
            return CheckResult(key=key, ok=False, detail=f"проверка упала: {exc}")

    async def _check_heartbeat(self, *, key: str, table: str, service_key: str,
                               label: str, now: datetime) -> CheckResult:
        row = await db.fetch_heartbeat(self.pool, table=table, service_key=service_key)
        if row is None:
            return CheckResult(key=key, ok=False,
                               detail=f"пульса {label} нет вовсе (строки {service_key!r} "
                                      f"в {table} нет)")
        last_seen_at = row["last_seen_at"]
        if last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
        age_sec = (now - last_seen_at).total_seconds()
        if age_sec > self.heartbeat_max_age_sec:
            return CheckResult(key=key, ok=False,
                               detail=f"пульс {label} устарел на {int(age_sec)} сек "
                                      f"(порог {self.heartbeat_max_age_sec})")
        return CheckResult(key=key, ok=True, detail="")

    async def _check_amocrm(self, now: datetime) -> CheckResult:
        updated_at = await db.fetch_amocrm_last_poll(self.pool)
        if updated_at is None:
            return CheckResult(key=KEY_AMOCRM_POLL, ok=False,
                               detail="ещё ни одного успешного цикла опроса "
                                      "(amocrm_api_state пуст)")
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_sec = (now - updated_at).total_seconds()
        if age_sec > self.amocrm_max_age_sec:
            return CheckResult(key=KEY_AMOCRM_POLL, ok=False,
                               detail=f"опрос стоит {int(age_sec)} сек "
                                      f"(порог {self.amocrm_max_age_sec})")
        return CheckResult(key=KEY_AMOCRM_POLL, ok=True, detail="")

    async def _check_database(self) -> CheckResult:
        try:
            ok = await asyncio.wait_for(db.ping_database(self.pool),
                                        timeout=self.db_timeout_sec)
        except Exception as exc:                         # noqa: BLE001
            return CheckResult(key=KEY_DATABASE, ok=False,
                               detail=f"{type(exc).__name__}: {exc}")
        return CheckResult(key=KEY_DATABASE, ok=bool(ok),
                           detail="" if ok else "SELECT 1 вернул не то, что ожидалось")

    async def _check_proxy(self) -> CheckResult:
        try:
            ok = await asyncio.wait_for(self.proxy_probe(), timeout=self.proxy_timeout_sec)
        except Exception as exc:                         # noqa: BLE001
            return CheckResult(key=KEY_PROXY, ok=False, detail=f"{type(exc).__name__}: {exc}")
        return CheckResult(key=KEY_PROXY, ok=bool(ok), detail="" if ok else "getMe не ответил")

    async def _check_dispatch(self, now: datetime, *,
                              remember: bool = True) -> CheckResult:
        stats = await db.fetch_dispatch_stats(self.pool, now=now)
        pending = stats["pending_due"]
        last_sent_at = stats["last_sent_at"]
        prev = self._prev_dispatch_pending
        if remember:
            self._prev_dispatch_pending = pending

        if pending == 0:
            # Ничего не ждёт отправки — застарелость last_sent_at не значит
            # поломку, это может значить «клиентам сегодня просто нечего писать».
            return CheckResult(key=KEY_DISPATCH, ok=True, detail="")

        growing = prev is not None and pending > prev
        if last_sent_at is not None and last_sent_at.tzinfo is None:
            last_sent_at = last_sent_at.replace(tzinfo=timezone.utc)
        age_sec = (now - last_sent_at).total_seconds() if last_sent_at is not None else None
        stalled = last_sent_at is None or (age_sec is not None
                                           and age_sec > self.dispatch_max_age_sec)

        if not (stalled or growing):
            return CheckResult(key=KEY_DISPATCH, ok=True, detail="")

        reasons = []
        if stalled:
            reasons.append(
                "отправок ещё не было" if last_sent_at is None
                else f"последняя отправка {int(age_sec)} сек назад "
                     f"(порог {self.dispatch_max_age_sec})")
        if growing:
            reasons.append(f"очередь растёт (было {prev}, стало {pending})")
        return CheckResult(key=KEY_DISPATCH, ok=False,
                           detail=f"в очереди {pending}: " + "; ".join(reasons))
