"""Инциденты и напоминания (ТЗ 2026-09-18, задача 8).

Превращает результаты сторожа (`notifyd.watchdog.Watchdog.check_once`,
задача 7 — та задача сознательно оставила `notify.incidents` нетронутым,
«следующий исполнитель подключит его сам») в состояние `notify.incidents`:
открывает инцидент, напоминает по расписанию своего уровня, закрывает при
выздоровлении, дублирует затянувшуюся красную поломку в My_assistant.

Три уровня из решения владельца (18.09):

* **Красный** — повтор каждые `RED_REMINDER_SEC` (10 минут), без потолка,
  но НЕ ночью: с 00:00 до 08:00 по Москве уходит только первое сообщение,
  повторы ждут утра (решение владельца 19.09, см. `is_night`). Кнопка «сел
  разбираться» глушит на `RED_ACK_SILENCE_SEC` (час); не починилось —
  напоминания возвращаются сами. Починилось — робот сам шлёт «отбой,
  работает», в любое время суток.
* **Жёлтый** — раз в `YELLOW_REMINDER_SEC` (сутки), не больше `YELLOW_CAP`
  (3) сообщений всего. Кнопка «отложить» глушит на `YELLOW_SNOOZE_SEC`
  (тоже сутки) и не расходует потолок. Сегодня в проекте нет ни одного
  производителя жёлтых инцидентов (все семь проверок сторожа — красные,
  «техника»), но механика общая и рассчитана на будущее (второе ТЗ) —
  проверена тестами на искусственных `CheckResult(level="yellow")`.
* **Серый** — «не дёргает»: инцидент не заводится вовсе, в чат по адресу
  маршрута уходит одна запись о поломке и одна об отбое. Уровень берётся
  из справочника, поэтому серым поломку может сделать только владелец
  правкой в базе: команда `routes_cli set-level` для сторожевых проверок
  серый не принимает и отсылает к выключателю маршрута (решение 19.09).

**Порог подтверждения.** Инцидент открывается только если проверка сказала
«сломано» `CONFIRM_PASSES` проходов подряд. Мигнувшее (упало и поднялось
само) уходит строкой в технический журнал видом `BLIP_KIND` — это решение
владельца 19.09: «если что-то упало и сразу починилось, это запись в
технический журнал, а не тревога». Порог стоит только на открытие:
выздоровление принимается с первого же удачного прохода.

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

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from notifyd import db
from notifyd.watchdog import (
    CheckResult, KEY_ADMIN_HEARTBEAT, KEY_AMOCRM_POLL, KEY_CLIENT_HEARTBEAT,
    KEY_DATABASE, KEY_DISPATCH, KEY_PROXY, KEY_WORKER_HEARTBEAT,
)

log = logging.getLogger(__name__)

# --- ночная пауза (решение владельца 19.09) ---
# «Ночью красный дёргает 1 раз и ждёт до 8:00 по МСК, дальше работает штатно.
# Первая тревога может приходить сразу, просто не надо лупить каждые 10 минут.»
# Москву считаем явно: часового пояса в настройках службы нет, сервер может
# стоять в UTC, а «ночь» владельца от этого зависеть не должна.
MOSCOW = ZoneInfo("Europe/Moscow")
NIGHT_START_HOUR = 0
NIGHT_END_HOUR = 8


def is_night(moment: datetime) -> bool:
    """Ночь по Москве: 00:00-08:00. Пауза касается только ПОВТОРОВ красного —
    первое сообщение о поломке, отбой и дубль затянувшейся поломки разовые
    и уходят сразу, ночью тоже.

    Время без зоны считаем UTC, а не временем машины: служба живёт в UTC,
    и «ночь владельца» не должна зависеть от того, где стоит сервер."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return NIGHT_START_HOUR <= moment.astimezone(MOSCOW).hour < NIGHT_END_HOUR


# --- расписание напоминаний (решения владельца 18.09) ---
RED_REMINDER_SEC = 10 * 60             # «повтор каждые 10 минут»
RED_ACK_SILENCE_SEC = 60 * 60          # кнопка «сел разбираться» — час тишины
YELLOW_REMINDER_SEC = 24 * 60 * 60     # «раз в сутки»
YELLOW_CAP = 3                          # «потолок три» (считая первое сообщение)
YELLOW_SNOOZE_SEC = 24 * 60 * 60       # кнопка «отложить» — тоже сутки

# Сколько проходов подряд проверка должна говорить «сломано», чтобы это
# стало тревогой (решение владельца 19.09: «3 прохода — дальше переживаем
# и тревожимся»). При проходе раз в минуту это три минуты ожидания.
# Счётчик живёт в процессе: после перезапуска службы поломка подтверждается
# заново — те же три минуты, дешевле собственной таблицы.
CONFIRM_PASSES = 3

# Мигнувшая поломка: упало и поднялось само, не дойдя до порога. Не тревога,
# а след в техническом журнале — «если что-то упало и сразу починилось, это
# запись в технический журнал, а не тревога» (владелец, 19.09).
BLIP_KIND = "notify.incident.blip"
BLIP_ADDRESS = "tech_journal"

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
    """Проводит готовые результаты проверок через open/remind/close и
    проверяет эскалации. Своего цикла нет: результаты приносит единственный
    цикл службы — `Watchdog.run_forever(on_results=...)`. Так было не всегда:
    до правки 19.09 здесь крутился второй цикл, который звал `check_once()`
    на том же объекте сторожа и затирал ему межпроходную память (замечание 2
    ревью 18.09). Свой выключатель остался — по «Порядку выката» ТЗ инциденты
    включаются ПОСЛЕДНИМИ, уже при работающем стороже."""

    def __init__(self, *, pool: Any, enabled: bool,
                now: Optional[Callable[[], datetime]] = None,
                confirm_passes: int = CONFIRM_PASSES) -> None:
        self.pool = pool
        self.enabled = enabled
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.confirm_passes = confirm_passes
        # Сколько проходов подряд проверка говорит «сломано». Живёт в
        # процессе — см. CONFIRM_PASSES.
        self._failing_streak: dict[str, int] = {}
        # Какие kind уже засеяны в notify.routes в этом процессе — тот же
        # приём (и то же ограничение: не переживает перезапуск), что
        # `JournalAdapter._route_seeded`, только на несколько kind сразу.
        self._routes_seeded: set[str] = set()
        # О каких серых поломках уже сообщили: серый инцидентом не бывает
        # (CHECK в миграции 001), значит и «уже сообщали» записать некуда.
        # После перезапуска службы возможна одна повторная запись — дешевле,
        # чем заводить под серые поломки собственную таблицу.
        self._grey_noted: set[str] = set()

    async def process_checks(self, results: Sequence[CheckResult]) -> int:
        """Один проход по готовым результатам проверок — их приносит цикл
        сторожа. Возвращает, сколько сообщений положено в notify.outbox
        (для тестов).

        Выключатель проверяется здесь, а не в цикле: сторож обязан работать
        и при выключенных инцидентах — он в это время пишет в журнал, и
        владелец по «Порядку выката» сначала обживается с ним."""
        if not self.enabled:
            return 0
        now = self._now()
        queued = 0
        for result in results:
            if result.ok:
                queued += await self._note_blip(result, now)
                queued += await self._handle_recovered(result, now)
                continue

            streak = self._failing_streak.get(result.key, 0) + 1
            self._failing_streak[result.key] = streak
            if streak < self.confirm_passes:
                log.debug("notify: %s сломано %s-й проход подряд, порог %s — жду",
                          result.key, streak, self.confirm_passes)
                continue
            queued += await self._handle_failing(result, now)
        queued += await self._run_escalations(now)
        return queued

    async def _note_blip(self, result: CheckResult, now: datetime) -> int:
        """Проверка поднялась, не дойдя до порога, — записываем след и
        молчим. Дошедшая до порога поломка сюда не попадает: её закрывает
        `_handle_recovered` отбоем, как и раньше."""
        streak = self._failing_streak.pop(result.key, 0)
        if not 0 < streak < self.confirm_passes:
            return 0
        log.info("notify: %s мигнуло (%s проход(а) подряд) — тревоги нет, "
                 "только запись в журнал", result.key, streak)
        await self._ensure_route(BLIP_KIND, BLIP_ADDRESS, "grey")
        await db.insert_event(
            self.pool, kind=BLIP_KIND,
            text=f"Мигнуло: «{result.key}» — сломалось и починилось само "
                 f"(держалось проходов: {streak}).",
            source="notify", now=now, expires_at=now + _ALL_CLEAR_TTL)
        return 1

    # --- открытие и напоминание ---

    async def _handle_failing(self, result: CheckResult, now: datetime) -> int:
        level = await self._route_level(result)
        if level == "grey":
            return await self._note_grey_failure(result, now)

        opened = await db.open_incident(self.pool, key=result.key, level=level,
                                        address=INCIDENT_ADDRESS, detail=result.detail,
                                        now=now)
        if opened is not None:
            log.warning("notify: инцидент открыт — %s (%s)", result.key, result.detail)
            await self._send_alert(opened, first=True, now=now)
            return 1

        # Уровень могли сменить командой, пока поломка идёт: решение владельца
        # 19.09 — смена действует сразу. Понижение красного до жёлтого при
        # уже израсходованном потолке жёлтого означает тишину: это и есть то,
        # ради чего уровень понижают.
        changed = await db.sync_incident_level(self.pool, key=result.key, level=level)
        if changed is not None:
            log.info("notify: уровень инцидента %s сменён на %s (справочник)",
                     result.key, level)

        if level == "red" and is_night(now):
            # Напоминание не берём вовсе: claim_reminder_due не только решает
            # «пора», но и отмечает попытку — пропускать надо ДО него, иначе
            # ночь съедала бы утренние отсчёты.
            log.debug("notify: ночь по Москве — повтор по %s ждёт восьми утра",
                      result.key)
            return 0

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
        was_grey_noted = result.key in self._grey_noted
        self._grey_noted.discard(result.key)

        if closed is not None:
            log.info("notify: инцидент закрыт — %s", result.key)
            if closed["level"] == "red":
                await self._send_all_clear(closed, now)
                return 1
            return 0

        if was_grey_noted:
            # Серая поломка инцидента не заводила, но запись о ней была —
            # закрываем её парной записью, чтобы в чате не висело «сломано».
            await db.insert_event(
                self.pool, kind=result.key,
                text=f"Отбой: «{result.key}» — работает.",
                source="notify", now=now, expires_at=now + _ALL_CLEAR_TTL)
            return 1
        return 0

    async def _route_level(self, result: CheckResult) -> str:
        """Уровень поломки решает справочник, а не код — ради этого вся затея
        (комментарий к `notify.routes` в миграции 001). Код даёт значение
        только для первого засева: `watchdog.DEFAULT_LEVELS`, табличка
        владельца 19.09."""
        route = await db.get_route(self.pool, result.key)
        if route is not None:
            return route["level"]
        await self._ensure_route(result.key, INCIDENT_ADDRESS, result.level)
        return result.level

    async def _note_grey_failure(self, result: CheckResult, now: datetime) -> int:
        """Серый уровень — «не дёргает»: инцидент не заводим (серого инцидента
        не бывает, CHECK в миграции 001), кладём одну запись по адресу маршрута
        и молчим, пока поломка держится."""
        if result.key in self._grey_noted:
            return 0
        self._grey_noted.add(result.key)
        log.info("notify: %s сломано, но уровень в справочнике серый — тревоги "
                 "нет, только запись (%s)", result.key, result.detail)
        detail = f"\n{result.detail}" if result.detail else ""
        await db.insert_event(
            self.pool, kind=result.key, text=f"Сломано: «{result.key}»{detail}",
            source="notify", now=now, expires_at=now + _ALL_CLEAR_TTL)
        return 1

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
            await self._ensure_route(ESCALATION_KIND, ESCALATION_ADDRESS, "grey")
            await db.insert_event(self.pool, kind=ESCALATION_KIND, text=text, source="notify",
                                  ref=str(row["id"]), now=now, expires_at=now + _ESCALATION_TTL)
            queued += 1
        return queued

    # --- отправка ---

    async def _send_alert(self, incident: dict, *, first: bool, now: datetime) -> None:
        # Маршрут уже засеян в `_route_level` — здесь только отправка.
        ttl = _ALERT_TTL.get(incident["level"], timedelta(hours=1))
        await db.insert_event(
            self.pool, kind=incident["key"], text=_alert_text(incident, first=first),
            source="notify", ref=str(incident["id"]), now=now, expires_at=now + ttl,
            reply_markup=_alert_markup(incident))

    async def _send_all_clear(self, incident: dict, now: datetime) -> None:
        text = f"Отбой: «{incident['key']}» — работает."
        await db.insert_event(self.pool, kind=incident["key"], text=text, source="notify",
                              ref=str(incident["id"]), now=now, expires_at=now + _ALL_CLEAR_TTL)

    async def _ensure_route(self, kind: str, address: str, level: str) -> None:
        """Завести маршрут `kind -> address` с нужным уровнем, если его ещё
        нет. Существующую строку не трогает вовсе (`db.seed_route`): ручная
        правка владельца должна пережить рестарт службы, иначе команды
        `set-address` и `set-level` работают до ближайшего перезапуска."""
        if kind in self._routes_seeded:
            return
        try:
            await db.seed_route(self.pool, kind, address, level)
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
