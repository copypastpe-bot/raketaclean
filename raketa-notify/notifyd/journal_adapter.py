"""Переходник журнала — без единой правки в коде ботов (ТЗ 2026-09-18,
задача 5).

Читает журнал systemd обоих ботов (`telegram-bot.service`,
`raketa-admin-bot.service` — константа `WATCHED_UNITS`), берёт записи
уровня «предупреждение» и выше, маскирует ПД (`notifyd.redact`) и кладёт
в `notify.outbox` своим видом события (`JOURNAL_KIND`, `source='notify'`) —
доставка дальше уже дело почтальона (`notifyd.postman`), этот модуль сам
в Telegram не ходит.

Уровень и модуль берутся из ТЕКСТА строки, не из PRIORITY journald. Причина:
оба бота настроены голым `logging.basicConfig(...)` без `stream=` — это
означает запись в stderr, а systemd по умолчанию присваивает всему, что
приходит по stderr, один и тот же приоритет независимо от настоящего уровня
записи (см. systemd.exec(5), «StandardError=journal»). Поэтому единственный
надёжный источник уровня — сам форматтер: рабочий бот пишет голым
`"%(levelname)s:%(name)s:%(message)s"` (bot.py:387, формат по умолчанию),
админ-бот — явным `"%(asctime)s %(levelname)s %(name)s: %(message)s"`
(adminbot/main.py:1065). Строка, не подошедшая ни под один формат
(например, продолжение трассировки без префикса уровня — каждая строка
многострочной записи уходит в journald отдельной записью), не разбирается
и дальше не едет: гадать об уровне вслепую хуже, чем промолчать про эту
строку (сама трассировка целиком остаётся в настоящем журнале — просмотр
руками не отменяем).

Схлопывание повторов (окно 10 минут) и потолок на минуту — обязательные
свойства ТЗ, подробности у каждой функции ниже.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from notifyd import db
from notifyd.journal_source import JournalSource
from notifyd.redact import redact

log = logging.getLogger(__name__)

# Юниты, которые слушаем, и короткий псевдоним для тега (без ".service").
WATCHED_UNITS: dict[str, str] = {
    "telegram-bot.service": "telegram-bot",
    "raketa-admin-bot.service": "raketa-admin-bot",
}

# Свой вид события в notify.outbox — один на весь переходник: адрес и
# уровень маршрута всегда одни и те же (tech_journal, серый), различие
# между записями — только в тексте и тегах внутри него.
JOURNAL_KIND = "notify.journal"

# Окно схлопывания повторов и потолок на цикл — числа ТЗ («окно 10 минут»,
# «потолок на минуту»). Потолок именно «на цикл», а не на отдельные часы —
# цикл этого адаптера длится POLL_INTERVAL_SEC (по умолчанию 60 = минута),
# так что при настройках по умолчанию это одно и то же; поменяли интервал —
# поменялся и практический смысл потолка, что естественно.
DEDUP_WINDOW_SEC = 600
DEFAULT_POLL_INTERVAL_SEC = 60
DEFAULT_CAP_PER_CYCLE = 20

# Забыть состояние подавления повтора, если оно тихо не обновлялось дольше
# этого срока — иначе память процесса растёт вечно (у каждой отдельной
# сделки/лида/заказа в тексте свой id, поэтому одинаковых по смыслу, но
# разных по id ошибок за долгую жизнь службы накопится много).
_STALE_FORGET_SEC = 3600

_LEVEL_VALUE = {
    "DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
    "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL,
}
_LEVEL_ALTS = "DEBUG|INFO|WARNING|ERROR|CRITICAL"

# Формат админ-бота: "%(asctime)s %(levelname)s %(name)s: %(message)s"
# (adminbot/main.py:1065).
_RE_WITH_ASCTIME = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} "
    rf"(?P<level>{_LEVEL_ALTS}) (?P<module>\S+): (?P<msg>.*)$",
    re.DOTALL,
)

# Формат рабочего бота: голый logging.basicConfig(level=logging.INFO) без
# format= — питон сам ставит "%(levelname)s:%(name)s:%(message)s" (bot.py:387).
_RE_BARE = re.compile(
    rf"^(?P<level>{_LEVEL_ALTS}):(?P<module>[^:]+):(?P<msg>.*)$",
    re.DOTALL,
)

# Ревью п.6: ключ схлопывания раньше сравнивал текст дословно, а номер
# заказа/сделки стоит прямо в тексте записи — массовый сбой давал разный
# текст на каждый номер вместо одной строки со счётчиком. \x00 не встречается
# в обычном тексте лога, поэтому годится как заглушка-разделитель: заглушка
# используется только внутри ключа группировки (см. _dedup_shape), наружу,
# в текст сообщения, никогда не попадает — туда идёт info["text"] первого
# вхождения (см. _collect), с настоящим числом.
_DIGITS_RE = re.compile(r"\d+")


def _dedup_shape(masked_text: str) -> str:
    """Привести текст к «форме» для сравнения повторов: числа — не то, что
    отличает одну и ту же ошибку от другой, поэтому заменяем их на общую
    заглушку и группируем уже по форме. Маскированный телефон (…1234) под
    эту же гребёнку — это уже не ПД, схлопывание таких строк допустимо и
    желательно."""
    return _DIGITS_RE.sub("\x00", masked_text)


@dataclass(frozen=True)
class ParsedLine:
    level: str
    module: str
    text: str


def parse_level_and_module(raw: str) -> Optional[ParsedLine]:
    """Разобрать уровень/модуль/текст одной строки журнала — или вернуть
    None, если строка не подошла ни под один известный формат."""
    m = _RE_WITH_ASCTIME.match(raw) or _RE_BARE.match(raw)
    if not m:
        return None
    return ParsedLine(level=m.group("level"), module=m.group("module"),
                      text=m.group("msg"))


@dataclass
class _Suppressed:
    """Состояние подавления повторов для одного ключа (окно 10 минут)."""

    last_flush_at: datetime
    unit: str
    level: str
    module: str
    text: str
    accumulated: int = 0


def _module_tag(module: str) -> str:
    """Имя модуля -> тег: Telegram считает хэштегом только буквы/цифры/_,
    поэтому точки в dotted-имени модуля (`crm.wahelp_dispatcher`) меняем
    на подчёркивание."""
    return re.sub(r"\W", "_", module)


def _compose_new(unit: str, level: str, module: str, text: str, count: int,
                 when: datetime) -> str:
    tag_unit = WATCHED_UNITS.get(unit, unit)
    suffix = f" (×{count})" if count > 1 else ""
    stamp = when.astimezone(timezone.utc).strftime("%d.%m %H:%M:%S")
    return (f"[{level}] {stamp} {text}{suffix}\n\n"
           f"#{tag_unit} #{_module_tag(module)}")


def _compose_summary(state: "_Suppressed") -> str:
    tag_unit = WATCHED_UNITS.get(state.unit, state.unit)
    return (f"[{state.level}] {state.text} — за 10 минут повторилось "
           f"×{state.accumulated}\n\n#{tag_unit} #{_module_tag(state.module)}")


class JournalAdapter:
    """Один цикл разбора журнала обоих ботов. Отправку не делает —
    только кладёт в `notify.outbox`, доставляет `notifyd.postman`."""

    def __init__(self, *, pool, sources: dict[str, JournalSource], enabled: bool,
                now: Optional[Callable[[], datetime]] = None,
                sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
                cap_per_cycle: int = DEFAULT_CAP_PER_CYCLE) -> None:
        self.pool = pool
        self.sources = sources
        self.enabled = enabled
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.poll_interval_sec = poll_interval_sec
        self.cap_per_cycle = cap_per_cycle
        self._state: dict[tuple, _Suppressed] = {}
        self._route_seeded = False

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        if not self.enabled:
            log.info("notify: переходник журнала выключен (выключатель в "
                     "настройках), journalctl не читаю")
            while stop is None or not stop.is_set():
                await self.sleep(self.poll_interval_sec)
            return

        while stop is None or not stop.is_set():
            try:
                await self.poll_once()
            except Exception:                                # noqa: BLE001
                log.exception("notify: проход переходника журнала не удался")
            await self.sleep(self.poll_interval_sec)

    async def _ensure_route(self) -> None:
        """Завести маршрут `JOURNAL_KIND -> tech_journal` при первом проходе,
        если его ещё нет, и не трогать существующий (`db.seed_route`): засев
        идёт при каждом запуске службы, а через `upsert_route_address` он
        возвращал адрес на умолчание и затирал ручную правку владельца —
        тот же дефект, что нашли у инцидентов (ревью 18.09, замечание 7).
        Не удалось — не страшно и не фатально: событие всё равно уйдёт
        в tech_journal как #неизвестный-вид (тот же фолбэк, что у любого
        вида без маршрута), просто с чужим тегом; повторим на следующем
        проходе."""
        if self._route_seeded:
            return
        try:
            await db.seed_route(self.pool, JOURNAL_KIND, "tech_journal", "grey")
            self._route_seeded = True
        except Exception:                                   # noqa: BLE001
            log.warning("notify: не завёл маршрут %s — не страшно, событие "
                       "всё равно уйдёт в tech_journal как #неизвестный-вид, "
                       "повторю попытку на следующем проходе",
                       JOURNAL_KIND, exc_info=True)

    async def poll_once(self) -> int:
        """Один проход: прочитать новое у обоих юнитов, схлопнуть повторы,
        применить потолок, положить в ящик. Возвращает число строк,
        положенных в notify.outbox (для тестов)."""
        await self._ensure_route()
        now = self._now()
        batch = await self._collect(now)
        to_queue = self._flush_batch(batch, now)
        self._sweep_stale_windows(batch.keys(), now, to_queue)
        to_queue = self._apply_cap(to_queue)

        for text in to_queue:
            await db.insert_event(self.pool, kind=JOURNAL_KIND, text=text,
                                  source="notify", now=now,
                                  expires_at=now + timedelta(hours=24))
        return len(to_queue)

    async def _collect(self, now: datetime) -> dict[tuple, dict]:
        """Прочитать новые записи у всех источников, разобрать, замаскировать
        ПД и сгруппировать одинаковые по ФОРМЕ (числа заменены на заглушку,
        см. _dedup_shape — ревью п.6: номер заказа/сделки в тексте не должен
        мешать схлопыванию) под одним ключом — счётчик `count` уже здесь
        ловит залповые повторы (проверка ТЗ: 100 одинаковых ошибок сразу ->
        одна строка со счётчиком). Текст в слоте — человеческий, первого
        вхождения, с настоящим числом: форма нужна только для сравнения."""
        batch: dict[tuple, dict] = {}
        for unit, source in self.sources.items():
            entries = await source.read_new()
            for entry in entries:
                parsed = parse_level_and_module(entry.message)
                if parsed is None:
                    continue
                if _LEVEL_VALUE.get(parsed.level, 0) < logging.WARNING:
                    continue
                masked = redact(parsed.text)
                shape = _dedup_shape(masked)
                key = (unit, parsed.module, parsed.level, shape)
                slot = batch.get(key)
                if slot is None:
                    batch[key] = {"count": 1, "level": parsed.level,
                                 "module": parsed.module, "unit": unit,
                                 "text": masked, "ts": entry.timestamp}
                else:
                    slot["count"] += 1
                    slot["ts"] = entry.timestamp
        return batch

    def _flush_batch(self, batch: dict[tuple, dict], now: datetime) -> list[str]:
        """Для каждого ключа этого прохода — отправить сразу, если окно
        предыдущей отправки уже истекло (или ключ новый), иначе молча
        накопить в state и ничего не слать (это и есть схлопывание повтора
        в окне 10 минут: пока окно открыто, вторая и следующие идентичные
        записи не порождают новых строк)."""
        to_queue: list[str] = []
        for key, info in batch.items():
            state = self._state.get(key)
            if state is None or (now - state.last_flush_at).total_seconds() >= DEDUP_WINDOW_SEC:
                total = info["count"] + (state.accumulated if state else 0)
                to_queue.append(_compose_new(info["unit"], info["level"],
                                             info["module"], info["text"],
                                             total, info["ts"]))
                self._state[key] = _Suppressed(last_flush_at=now, unit=info["unit"],
                                               level=info["level"], module=info["module"],
                                               text=info["text"], accumulated=0)
            else:
                state.accumulated += info["count"]
        return to_queue

    def _sweep_stale_windows(self, touched_keys, now: datetime,
                             to_queue: list[str]) -> None:
        """Окна, которые накопили повторы, но в этом проходе не получили ни
        одной новой записи (сбой прекратился ДО того, как истекли полные
        10 минут) — не должны молчать вечно: закрываем их здесь, отдельно
        от `_flush_batch`. Одновременно забываем то, что давно не подавало
        признаков жизни — иначе state растёт без предела на долгой жизни
        службы (у каждой сделки/заказа в тексте свой id — ключи не повторяются)."""
        stale: list[tuple] = []
        for key, state in self._state.items():
            if key in touched_keys:
                continue
            age = (now - state.last_flush_at).total_seconds()
            if state.accumulated > 0 and age >= DEDUP_WINDOW_SEC:
                to_queue.append(_compose_summary(state))
                state.accumulated = 0
                state.last_flush_at = now
            elif state.accumulated == 0 and age >= _STALE_FORGET_SEC:
                stale.append(key)
        for key in stale:
            del self._state[key]

    def _apply_cap(self, to_queue: list[str]) -> list[str]:
        """Потолок на цикл: сверх него — одна строка «ещё N записей,
        смотри журнал» вместо всех остальных построчно."""
        if len(to_queue) <= self.cap_per_cycle:
            return to_queue
        overflow = len(to_queue) - self.cap_per_cycle
        return to_queue[: self.cap_per_cycle] + [f"ещё {overflow} записей, смотри журнал"]
