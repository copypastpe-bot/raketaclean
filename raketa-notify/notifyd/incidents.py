"""Инциденты и напоминания (ТЗ 2026-09-18, задача 8).

Превращает результаты сторожа (`notifyd.watchdog.Watchdog.check_once`,
задача 7 — та задача сознательно оставила `notify.incidents` нетронутым,
«следующий исполнитель подключит его сам») в состояние `notify.incidents`:
открывает инцидент, напоминает по расписанию своего уровня, закрывает при
выздоровлении, дублирует затянувшуюся красную поломку в My_assistant.

Три уровня из решения владельца (18.09):

* **Красный** — повтор каждые `RED_REMINDER_SEC` (10 минут), круглосуточно,
  без потолка. Кнопка «сел разбираться» глушит на `RED_ACK_SILENCE_SEC`
  (час); не починилось — напоминания возвращаются сами. Починилось — робот
  сам шлёт «отбой, работает».
* **Жёлтый** — раз в `YELLOW_REMINDER_SEC` (сутки), не больше `YELLOW_CAP`
  (3) сообщений всего. Кнопка «отложить» глушит на `YELLOW_SNOOZE_SEC`
  (тоже сутки) и не расходует потолок. Сегодня в проекте нет ни одного
  производителя жёлтых инцидентов (все семь проверок сторожа — красные,
  «техника»), но механика общая и рассчитана на будущее (второе ТЗ) —
  проверена тестами на искусственных `CheckResult(level="yellow")`.
* Серый в инциденты не попадает вовсе — по определению «не дёргает»
  (`notify.routes.level`), а `Watchdog.check_once` серых результатов не
  производит.

**Эскалация.** Красный инцидент, который «бьёт по клиентам или заказам» и
не закрыт за час, дублируется в My_assistant словами последствия, без
кнопки, один раз (`escalated_at`). Какие из семи ключей сторожа считаются
«бьющими» — решение этого исполнителя, ТЗ называет только один пример
(рассыльщик клиентам, «час не уходят сообщения клиентам» — использован
буквально в `ESCALATION_TEXT`); подробности выбора — у `ESCALATE_KEYS` ниже.

**Гонки.** «Две одновременные проверки не должны дать двух сообщений»
(проверка ТЗ) обеспечена не в этом модуле, а в `notifyd.db`: открытие,
напоминание и закрытие — каждое один атомарный SQL-запрос с `RETURNING`,
который отдаёт строку только тому вызову, который и правда что-то поменял
(тот же приём, что `claim_due` у почтальона). Здесь достаточно проверить
`is None`.

**Запасной путь до Telegram, если лёг прокси** (bullet ТЗ, факт 12) — этот
модуль не решает и не может: сообщение об инциденте (включая эскалацию)
уходит тем же почтальоном (`notify.outbox` → `notifyd.postman`), тем же
путём (прямые адреса + прокси), которым Watchdog проверяет «прокси».
Отдельного резервного пути в обход прокси у службы нет — это ограничение
записано в `docs/runbook.md` («Если служба молчит»), проверить на боевом
сервере фактом (доступа к стенду в этой сессии не было, как и во всех
предыдущих задачах этого ТЗ).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Sequence

from notifyd import db
from notifyd.watchdog import (
    CheckResult, KEY_ADMIN_HEARTBEAT, KEY_AMOCRM_POLL, KEY_CLIENT_HEARTBEAT,
    KEY_DATABASE, KEY_DISPATCH, KEY_PROXY, KEY_WORKER_HEARTBEAT,
)

log = logging.getLogger(__name__)

CheckSource = Callable[[], Awaitable[Sequence[CheckResult]]]

DEFAULT_POLL_INTERVAL_SEC = 60

# --- расписание напоминаний (решения владельца 18.09) ---
RED_REMINDER_SEC = 10 * 60             # «повтор каждые 10 минут»
RED_ACK_SILENCE_SEC = 60 * 60          # кнопка «сел разбираться» — час тишины
YELLOW_REMINDER_SEC = 24 * 60 * 60     # «раз в сутки»
YELLOW_CAP = 3                          # «потолок три» (считая первое сообщение)
YELLOW_SNOOZE_SEC = 24 * 60 * 60       # кнопка «отложить» — тоже сутки

# Красный инцидент, не закрытый за это время, дублируется в My_assistant.
ESCALATION_AFTER_SEC = 60 * 60

# Адреса справочника notify.routes.address (миграция 001): все проверки
# сторожа — техника, поэтому один и тот же адрес для всех (правило владельца
# «кому чинить — тому и сигнал»); эскалация — отдельный, фиксированный адрес.
INCIDENT_ADDRESS = "my_admin"
ESCALATION_ADDRESS = "my_assistant"
ESCALATION_KIND = "notify.incident.escalation"

# Префиксы callback_data кнопок — по id инцидента (не по key: key — русская
# фраза со пробелами, в 64-байтный лимит Telegram может не влезть; id —
# короткое и однозначное).
ACK_CALLBACK_PREFIX = "incident_ack"
SNOOZE_CALLBACK_PREFIX = "incident_snooze"

# Какие из семи ключей сторожа (notifyd.watchdog.KEY_*) «бьют по клиентам
# или заказам» и потому эскалируются в My_assistant (решение исполнителя —
# ТЗ прямо называет только рассыльщика клиентам как пример). Пульс
# админ-бота НЕ входит: это внутренний инструмент владельца (amo_sync),
# в первый час простоя клиента он не касается — деальс просто подождут.
ESCALATE_KEYS = frozenset({
    KEY_WORKER_HEARTBEAT, KEY_CLIENT_HEARTBEAT, KEY_AMOCRM_POLL,
    KEY_DATABASE, KEY_PROXY, KEY_DISPATCH,
})

# Текст последствия — словами дела, не словами техники (ТЗ, задача 8).
# Ключ KEY_DISPATCH использует формулировку ТЗ буквально («час не уходят
# сообщения клиентам»).
ESCALATION_TEXT: dict[str, str] = {
    KEY_WORKER_HEARTBEAT: "уже час бот для клиентов не отвечает — новые заказы никто не принимает",
    KEY_CLIENT_HEARTBEAT: "уже час клиентский бот не отвечает — клиенты не получают ответов",
    KEY_AMOCRM_POLL: "уже час события по сделкам из CRM не доходят до бота — заказы могут потеряться",
    KEY_DATABASE: "уже час база данных не отвечает — оба бота не работают вовсе",
    KEY_PROXY: "уже час нет связи с Telegram — ни одно сообщение клиентам не уходит",
    KEY_DISPATCH: "уже час не уходят сообщения клиентам",
}

# Срок годности сообщения в notify.outbox — не про сам инцидент (тот живёт,
# пока его не закроют), а про то, чтобы протухшая попытка доставки не
# долетела через полдня почтальонских повторов с неактуальным «ещё не
# починили». Красный — с запасом на несколько своих циклов (10 мин),
# жёлтый — на несколько своих (сутки).
_ALERT_TTL = {"red": timedelta(minutes=30), "yellow": timedelta(hours=26)}
_ESCALATION_TTL = timedelta(hours=6)
_ALL_CLEAR_TTL = timedelta(hours=6)


class IncidentManager:
    """Один цикл: спросить `check_source` (обычно `Watchdog.check_once`),
    провести каждый результат через open/remind/close, затем проверить
    эскалации. Свой выключатель, по умолчанию выключен (правило проекта) —
    по «Порядку выката» ТЗ включается ПОСЛЕДНИМ, после сторожа."""

    def __init__(self, *, pool: Any, check_source: CheckSource, enabled: bool,
                now: Optional[Callable[[], datetime]] = None,
                sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC) -> None:
        self.pool = pool
        self.check_source = check_source
        self.enabled = enabled
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.poll_interval_sec = poll_interval_sec
        # Какие kind уже засеяны в notify.routes в этом процессе — тот же
        # приём (и то же ограничение: не переживает перезапуск), что
        # `JournalAdapter._route_seeded`, только на несколько kind сразу.
        self._routes_seeded: set[str] = set()

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        if not self.enabled:
            log.info("notify: инциденты выключены (выключатель в настройках), "
                     "check_once не запускаю и notify.incidents не трогаю")
            while stop is None or not stop.is_set():
                await self.sleep(self.poll_interval_sec)
            return

        while stop is None or not stop.is_set():
            try:
                results = await self.check_source()
                await self.process_checks(results)
            except Exception:                             # noqa: BLE001
                log.exception("notify: проход инцидентов не удался")
            await self.sleep(self.poll_interval_sec)

    async def process_checks(self, results: Sequence[CheckResult]) -> int:
        """Один проход по уже готовым результатам проверок. Возвращает,
        сколько сообщений положено в notify.outbox (для тестов)."""
        now = self._now()
        queued = 0
        for result in results:
            if result.ok:
                queued += await self._handle_recovered(result, now)
            else:
                queued += await self._handle_failing(result, now)
        queued += await self._run_escalations(now)
        return queued

    # --- открытие и напоминание ---

    async def _handle_failing(self, result: CheckResult, now: datetime) -> int:
        opened = await db.open_incident(self.pool, key=result.key, level=result.level,
                                        address=INCIDENT_ADDRESS, detail=result.detail,
                                        now=now)
        if opened is not None:
            log.warning("notify: инцидент открыт — %s (%s)", result.key, result.detail)
            await self._send_alert(opened, first=True, now=now)
            return 1

        claimed = await db.claim_reminder_due(
            self.pool, key=result.key, now=now,
            red_interval_sec=RED_REMINDER_SEC, yellow_interval_sec=YELLOW_REMINDER_SEC,
            yellow_cap=YELLOW_CAP)
        if claimed is not None:
            log.info("notify: напоминание по инциденту %s (напоминание №%s)",
                     result.key, claimed["notify_count"])
            await self._send_alert(claimed, first=False, now=now)
            return 1
        return 0

    async def _handle_recovered(self, result: CheckResult, now: datetime) -> int:
        closed = await db.close_incident(self.pool, result.key, now)
        if closed is None:
            return 0
        log.info("notify: инцидент закрыт — %s", result.key)
        if closed["level"] == "red":
            await self._send_all_clear(closed, now)
            return 1
        return 0

    async def _run_escalations(self, now: datetime) -> int:
        rows = await db.claim_escalations(self.pool, now=now,
                                          threshold_sec=ESCALATION_AFTER_SEC)
        queued = 0
        for row in rows:
            if row["key"] not in ESCALATE_KEYS:
                continue
            log.warning("notify: инцидент %s не закрыт за час — дублирую в My_assistant",
                       row["key"])
            text = ESCALATION_TEXT.get(row["key"],
                                       f"уже час не устранена поломка: {row['key']}")
            await self._ensure_route(ESCALATION_KIND, ESCALATION_ADDRESS)
            await db.insert_event(self.pool, kind=ESCALATION_KIND, text=text, source="notify",
                                  ref=str(row["id"]), now=now, expires_at=now + _ESCALATION_TTL)
            queued += 1
        return queued

    # --- отправка ---

    async def _send_alert(self, incident: dict, *, first: bool, now: datetime) -> None:
        await self._ensure_route(incident["key"], INCIDENT_ADDRESS)
        ttl = _ALERT_TTL.get(incident["level"], timedelta(hours=1))
        await db.insert_event(
            self.pool, kind=incident["key"], text=_alert_text(incident, first=first),
            source="notify", ref=str(incident["id"]), now=now, expires_at=now + ttl,
            reply_markup=_alert_markup(incident))

    async def _send_all_clear(self, incident: dict, now: datetime) -> None:
        text = f"Отбой: «{incident['key']}» — работает."
        await db.insert_event(self.pool, kind=incident["key"], text=text, source="notify",
                              ref=str(incident["id"]), now=now, expires_at=now + _ALL_CLEAR_TTL)

    async def _ensure_route(self, kind: str, address: str) -> None:
        """Завести маршрут `kind -> address`, если его ещё нет — идемпотентно,
        один раз за жизнь процесса (тот же приём и то же ограничение, что
        `JournalAdapter._ensure_route`: не спорит с ручной правкой владельца
        В ЭТОМ запуске службы, но переживёт следующий рестарт заново — тот же
        компромисс, что и там, не расширяем и не сужаем задачей 8)."""
        if kind in self._routes_seeded:
            return
        try:
            await db.upsert_route_address(self.pool, kind, address)
            self._routes_seeded.add(kind)
        except Exception:                                  # noqa: BLE001
            log.warning("notify: не завёл маршрут %s -> %s — событие всё равно уйдёт "
                       "в технический журнал как #неизвестный-вид, повторю попытку "
                       "на следующем проходе", kind, address, exc_info=True)


def _alert_text(incident: dict, *, first: bool) -> str:
    lead = "Сломано" if first else "Ещё не починили"
    detail = f"\n{incident['detail']}" if incident.get("detail") else ""
    return f"{lead}: «{incident['key']}»{detail}"


def _alert_markup(incident: dict) -> dict:
    if incident["level"] == "red":
        button = {"text": "сел разбираться",
                  "callback_data": f"{ACK_CALLBACK_PREFIX}:{incident['id']}"}
    else:
        button = {"text": "отложить",
                  "callback_data": f"{SNOOZE_CALLBACK_PREFIX}:{incident['id']}"}
    return {"inline_keyboard": [[button]]}
