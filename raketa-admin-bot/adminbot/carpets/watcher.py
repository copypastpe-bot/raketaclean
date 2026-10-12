"""Наблюдатель почты: письмо партнёра → разобранные строки → сделки в amoCRM.

Опрашивает почту редко — раз в час: отчёты приходят раз в неделю, чаще незачем.

Четыре правила, которые определяют устройство цикла:

1. **Письмо помечается разобранным только целиком.** Если хоть одна строка
   не прошла, письмо остаётся в работе: лучше разобрать его повторно (от двойной
   записи защищает номер заказа партнёра), чем потерять отчёт молча.
2. **Разобранные письма помнит и база.** Пометка в почтовом ящике может не
   поставиться — тогда сработает вторая страховка, и робот не начнёт заново.
3. **Незавершённые строки живут своей жизнью.** Пока робот ждёт автосделку
   сейлзбота, письмо давно разобрано; строка берётся из базы, где её сохранили.
   Итог письма владельцу уходит один раз — когда у всех его строк есть судьба:
   доведена, ждёт ответа на карточку или отдана владельцу. Строка, которая три
   часа подряд не прошла ни шага, уходит владельцу «сделать руками», и робот её
   больше не трогает (ТЗ 2026-10-12).
4. **Письмо проверяется целиком до первой строки.** Партнёр прислал 15.09.2026
   отчёт за два года (536 строк), и робот принял его за недельный: за три минуты
   в амо появилось 30 лишних сделок. Теперь письмо, где наших строк больше
   порога, и письмо с неразобранным вложением робот откладывает: не проводит
   ни одной строки, один раз говорит владельцу и ждёт его решения.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

from adminbot.carpets.report import CarpetRow, parse_report, rows_to_process
from adminbot.models import CarpetLink

log = logging.getLogger(__name__)

# Статусы, по которым работа ещё не закончена. `waiting_owner` здесь тоже есть:
# движок по нему ничего не делает, но наблюдателю он нужен, чтобы дослать карточку,
# если Telegram в прошлый раз не ответил.
# Пока есть незаконченная работа, следующий проход делаем скоро, а не через час.
QUICK_RETRY_SEC = 60

# Сколько наших строк в письме робот считает нормальным. Недельный отчёт —
# 5–15 строк, месячный свод — до 50. Всё, что больше, скорее архив или чужой
# файл: такое письмо разбирает человек, а не робот.
MAX_ROWS_DEFAULT = 100

ACTIVE_STATUSES: tuple[str, ...] = (
    "new", "in_progress", "waiting_salesbot", "error", "waiting_owner",
)

# Сколько строка может стоять без пройденного шага (ошибка CRM, сбой разбора), прежде
# чем робот отдаст заказ владельцу «сделать руками». Решение владельца 2026-10-12:
# двухчасовой обрыв связи с амо (24.09) робот переживает молча.
MANUAL_AFTER_SEC = 3 * 3600

# У строки есть судьба — итог письма её больше не ждёт: доведена, ждёт ответа
# владельца на карточку (решение 2: ответов итог не ждёт) или отдана «сделать руками».
SETTLED_STATUSES = frozenset(("done", "waiting_owner", "manual"))


@dataclass(frozen=True)
class CarpetLetterSummary:
    """Итог одного письма партнёра: что стало с каждой его строкой."""

    uid: str
    subject: Optional[str]
    delivered: int = 0          # доведены (вместе с проведёнными раньше — решение 4)
    refused: int = 0            # отказы партнёра
    waiting_owner: int = 0      # ждут ответа на карточку
    manual: int = 0             # отданы владельцу «сделать руками»

    @property
    def clean(self) -> bool:
        """Владельцу делать нечего."""
        return not (self.waiting_owner or self.manual)


def summarize_letter(letter: dict, links: list[CarpetLink]) -> Optional[CarpetLetterSummary]:
    """Итог письма или None, если хоть одна строка ещё в работе.

    Строки, которой нет в базе, в итоге нет: считать по ней нечего.
    """
    by_id = {link.partner_id: link for link in links}
    refused_ids = set(letter.get("refused_ids") or ())
    counts: Counter[str] = Counter()
    for partner_id in dict.fromkeys(letter["partner_ids"]):
        link = by_id.get(partner_id)
        if link is None:
            continue
        if link.status not in SETTLED_STATUSES:
            return None
        if link.status == "done":
            counts["refused" if partner_id in refused_ids else "delivered"] += 1
        else:
            counts[link.status] += 1
    return CarpetLetterSummary(uid=letter["uid"], subject=letter.get("subject"),
                               delivered=counts["delivered"], refused=counts["refused"],
                               waiting_owner=counts["waiting_owner"], manual=counts["manual"])


class CarpetStateStore(Protocol):
    """Что наблюдателю нужно от хранилища сверх того, что нужно движку."""

    # `processed` | `held` | `released` | None (письмо роботу незнакомо)
    async def letter_state(self, uid: str) -> Optional[str]: ...

    async def remember_letter(self, uid: str, subject: Optional[str],
                              files: list[str], rows_total: int) -> None: ...

    async def hold_letter(self, uid: str, subject: Optional[str], files: list[str],
                          rows_total: int, reason: str) -> None: ...

    async def pending_rows(self) -> list[tuple[CarpetRow, CarpetLink]]: ...

    async def get(self, partner_id: int) -> Optional[CarpetLink]: ...

    async def update(self, partner_id: int, **fields: Any) -> Optional[CarpetLink]: ...

    async def update_and_log(self, partner_id: int, *, action: str, dry_run: bool,
                             payload: Optional[Any] = None,
                             **fields: Any) -> Optional[CarpetLink]: ...

    # Итог письма (ТЗ 2026-10-12): заказы письма, ещё не отчитанные письма, отметка.
    async def track_letter(self, uid: str, subject: Optional[str],
                           partner_ids: list[int], refused_ids: list[int]) -> None: ...

    async def letters_to_report(self) -> list[dict]: ...

    async def mark_letter_reported(self, uid: str) -> None: ...

    async def links_by_ids(self, partner_ids: list[int]) -> list[CarpetLink]: ...


@dataclass(frozen=True)
class CarpetTickReport:
    """Итог одного прохода — для журнала, отчёта владельцу и команды /status."""

    paused: bool = False
    letters: int = 0
    held: int = 0                                     # писем отложено за проход
    processed: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    questions: tuple[int, ...] = ()                   # заказы, по которым ушёл вопрос
    failures: tuple[tuple[Any, str], ...] = ()        # (номер заказа или письма, что случилось)


class CarpetWatcher:
    def __init__(
        self,
        *,
        engine: Any,
        mailbox: Any,
        store: CarpetStateStore,
        parse: Callable[[bytes], list[CarpetRow]] = parse_report,
        is_enabled: Optional[Callable[[], Any]] = None,
        poll_interval_sec: int = 3600,
        max_rows: int = MAX_ROWS_DEFAULT,
        on_question: Optional[Callable[[CarpetRow, CarpetLink], Awaitable[Optional[int]]]] = None,
        on_report: Optional[Callable[[CarpetLetterSummary], Awaitable[None]]] = None,
        on_held: Optional[Callable[..., Awaitable[None]]] = None,
        on_manual: Optional[Callable[[CarpetRow, CarpetLink], Awaitable[Any]]] = None,
        manual_after_sec: int = MANUAL_AFTER_SEC,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        dry_run: bool = False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.engine = engine
        self.mailbox = mailbox
        self.store = store
        self.parse = parse
        self.is_enabled = is_enabled
        self.poll_interval_sec = poll_interval_sec
        self.max_rows = max_rows
        self.on_question = on_question
        self.on_report = on_report
        self.on_held = on_held
        self.on_manual = on_manual
        self.manual_after_sec = manual_after_sec
        self.now = now
        # В репетиции письмо нельзя помечать прочитанным: пометка уходит в почтовый
        # ящик по-настоящему, и отчёт исчез бы из работы, ничего не сделав в CRM.
        self.dry_run = dry_run
        self.sleep = sleep
        self.last_report: Optional[CarpetTickReport] = None
        # `/carpets` и фоновый цикл не идут одновременно: иначе итог письма или
        # «сделайте руками» ушли бы дважды.
        self._lock = asyncio.Lock()

    async def tick(self) -> CarpetTickReport:
        """Один проход: разобрать новые письма и доделать незавершённое."""
        async with self._lock:
            return await self._tick()

    async def _tick(self) -> CarpetTickReport:
        if not await self._enabled():
            return self._remember(CarpetTickReport(paused=True))

        statuses: Counter[str] = Counter()
        questions: list[int] = []
        failures: list[tuple[Any, str]] = []
        held: list[str] = []

        letters = await self.mailbox.fetch_new()
        for letter in letters:
            await self._handle_letter(letter, statuses, questions, failures, held)

        # Незавершённое из прошлых писем: ожидание автосделки, ошибки, вопросы.
        for row, link in await self.store.pending_rows():
            await self._handle_row(row, None, statuses, questions, failures)

        await self._report_letters()

        return self._remember(CarpetTickReport(
            letters=len(letters), held=len(held), processed=sum(statuses.values()),
            by_status=dict(statuses), questions=tuple(questions),
            failures=tuple(failures)))

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.tick()
            except Exception:                          # noqa: BLE001
                log.exception("Проход по коврам не удался")
            await self.sleep(self._next_delay())

    def _next_delay(self) -> float:
        """Обычно ждём час, но не тогда, когда работа не закончена.

        Письма приходят раз в неделю, поэтому час — нормальный шаг. Но если робот
        передал лид в работу и ждёт автосделку сейлзбота, тот управляется за
        секунды: час простоя здесь был бы нелепым.
        """
        counts = dict(getattr(self.last_report, "by_status", {}) or {})
        unfinished = sum(counts.get(status, 0) for status in
                         ("waiting_salesbot", "in_progress", "new", "error"))
        return QUICK_RETRY_SEC if unfinished else self.poll_interval_sec

    # --- внутреннее ---

    async def _handle_letter(self, letter: Any, statuses: Counter,
                             questions: list, failures: list, held: list) -> None:
        state = await self.store.letter_state(letter.uid)
        if state == "processed":
            log.info("Письмо %s уже разобрано — помечаю прочитанным", letter.uid)
            await self.mailbox.mark_seen(letter.uid)
            return
        if state == "held":
            log.debug("Письмо %s отложено — жду решения владельца", letter.uid)
            return
        # Владелец снял отложение (`--carpets-release`) — значит, он это письмо
        # видел и разрешил. Порог строк к нему больше не применяем, иначе робот
        # отложил бы его снова и снятие ничего бы не меняло.
        released = state == "released"

        # Письмо проверяется целиком до того, как тронута первая строка: файлы
        # разбираются все сразу, строки считаются, и только потом что-то уходит
        # в амо. Иначе «слишком большой файл» выяснился бы на тридцатой сделке.
        parsed: list[tuple[str, list[CarpetRow]]] = []
        completed_total = 0
        refused_total = 0

        for name, data in letter.attachments.items():
            try:
                rows = self.parse(data)
            except Exception as exc:                   # noqa: BLE001 — кривой файл откладывает письмо
                log.exception("Отчёт %s разобрать не удалось", name)
                await self._hold(letter, f"файл {name}: {type(exc).__name__}: {exc}",
                                 completed_total + refused_total, refused_total, held)
                return

            completed, refused = rows_to_process(rows)
            parsed.append((name, completed + refused))
            completed_total += len(completed)
            refused_total += len(refused)

        rows_total = completed_total + refused_total
        if rows_total > self.max_rows and not released:
            await self._hold(letter, f"строк {rows_total}, порог {self.max_rows}",
                             rows_total, refused_total, held)
            return

        before_failures = len(failures)
        for name, rows_of_file in parsed:
            for row in rows_of_file:
                await self._handle_row(row, name, statuses, questions, failures)

        if len(failures) > before_failures:
            log.warning("Письмо %s оставляю в работе: были сбои", letter.uid)
            return

        # Заказы письма запоминаются для итога — только после того, как все строки
        # прошли без сбоев: боевое хранилище здесь же считает письмо разобранным.
        letter_rows = [row for _, rows_of_file in parsed for row in rows_of_file]
        partner_ids = list(dict.fromkeys(row.partner_id for row in letter_rows))
        refused_ids = list(dict.fromkeys(row.partner_id for row in letter_rows
                                         if row.is_refusal))
        await self.store.track_letter(letter.uid, letter.subject, partner_ids, refused_ids)

        if self.dry_run:
            log.info("Репетиция: письмо %s оставляю непрочитанным", letter.uid)
        else:
            await self.store.remember_letter(letter.uid, letter.subject,
                                             list(letter.attachments), rows_total)
            await self.mailbox.mark_seen(letter.uid)

    async def _report_letters(self) -> None:
        """Итог по каждому письму, у всех строк которого есть судьба, — один раз.

        Письмо, где хоть одна строка ещё в работе, ждёт следующего прохода. Сбой
        по одному письму не мешает остальным: итог по нему уйдёт в следующий раз.
        """
        try:
            letters = await self.store.letters_to_report()
        except Exception:                              # noqa: BLE001
            log.exception("Ковры: не смог взять письма для итога")
            return

        for item in letters:
            try:
                links = await self.store.links_by_ids(item["partner_ids"])
                summary = summarize_letter(item, links)
                if summary is None:
                    continue                           # строки письма ещё в работе
                missing = sorted(set(item["partner_ids"])
                                 - {link.partner_id for link in links})
                if missing:
                    log.warning("Ковры: в итоге письма %s нет заказов партнёра %s — "
                                "их нет в базе", item["uid"],
                                ", ".join(f"№{pid}" for pid in missing))
                if self.on_report:
                    await self.on_report(summary)
                await self.store.mark_letter_reported(item["uid"])
            except Exception:                          # noqa: BLE001
                log.exception("Ковры: итог письма %s не отправлен", item.get("uid"))

    async def _hold(self, letter: Any, reason: str, rows_total: int,
                    refused_total: int, held: list) -> None:
        """Отложить письмо: ни одной строки не проводим, ждём решения владельца.

        Прочитанным письмо не помечается ни в боевом режиме, ни в репетиции: оно
        лежит в папке, пока владелец не снимет отложение (`--carpets-release`)
        или не удалит письмо. Сообщение владельцу уходит один раз — на следующем
        проходе состояние `held` отсекает письмо в самом начале.
        """
        log.warning("Ковры: письмо %s отложено, %s", letter.uid, reason)
        await self.store.hold_letter(letter.uid, letter.subject,
                                     list(letter.attachments), rows_total, reason)
        held.append(letter.uid)
        if self.on_held:
            await self.on_held(letter, reason, rows_total, refused_total)

    async def _handle_row(self, row: CarpetRow, source_file: Optional[str],
                          statuses: Counter, questions: list, failures: list) -> None:
        before: Optional[CarpetLink] = None
        try:
            before = await self.store.get(row.partner_id)
            link = await self.engine.process_row(row, source_file=source_file)
        except Exception as exc:                       # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            log.exception("Ковры, заказ партнёра №%s: проход прерван", row.partner_id)
            link = await self._note_trouble(row, before, await self._current(row.partner_id),
                                            error)
            if link is not None and link.status == "manual":
                statuses["manual"] += 1                # отдана владельцу — письмо не держит
                return
            failures.append((row.partner_id, error))
            return

        if link is None:
            return
        if link.status == "error":
            link = await self._note_trouble(row, before, link) or link
        elif link.stuck_since is not None:
            link = await self.store.update(row.partner_id, stuck_since=None) or link
        statuses[link.status] += 1
        if await self._maybe_ask(row, link):
            questions.append(row.partner_id)

    async def _current(self, partner_id: int) -> Optional[CarpetLink]:
        """Строка из базы после сбоя прохода; база недоступна — None."""
        try:
            return await self.store.get(partner_id)
        except Exception:                              # noqa: BLE001
            log.exception("Ковры, заказ партнёра №%s: не смог прочитать строку", partner_id)
            return None

    async def _note_trouble(self, row: CarpetRow, before: Optional[CarpetLink],
                            link: Optional[CarpetLink], error: Optional[str] = None
                            ) -> Optional[CarpetLink]:
        """Строка не сдвинулась: завести или продолжить отсчёт; три часа — владельцу."""
        if link is None:
            return None                     # строки нет: сбой случился до её заведения
        moment = self.now()
        fields: dict[str, Any] = {"last_error": error} if error else {}
        progressed = (before is not None
                      and len(link.checklist or {}) > len(before.checklist or {}))
        if link.stuck_since is None or progressed:
            return await self.store.update(row.partner_id, stuck_since=moment, **fields) or link
        if (moment - link.stuck_since).total_seconds() < self.manual_after_sec:
            return (await self.store.update(row.partner_id, **fields) or link) if fields else link

        link = await self.store.update_and_log(
            row.partner_id, action="manual_handoff", dry_run=self.dry_run,
            payload={"error": error or link.last_error,
                     "stuck_since": link.stuck_since.isoformat()},
            status="manual", **fields) or link
        log.warning("Ковры, заказ партнёра №%s: %s ч без продвижения — отдаю владельцу",
                    row.partner_id, self.manual_after_sec // 3600)
        await self._send_manual(row, link)
        return link

    async def _send_manual(self, row: CarpetRow, link: CarpetLink) -> None:
        """Сообщение «сделайте руками»; его сбой не должен ронять проход."""
        if self.on_manual is None:
            return
        try:
            await self.on_manual(row, link)
        except Exception:                              # noqa: BLE001
            log.exception("Ковры, заказ партнёра №%s: сообщение «сделайте руками» "
                          "не отправлено", row.partner_id)

    async def _maybe_ask(self, row: CarpetRow, link: CarpetLink) -> bool:
        if link.status != "waiting_owner" or self.on_question is None:
            return False
        if link.question_msg_id:
            return False                               # владелец уже спрошен

        message_id = await self.on_question(row, link)
        if not message_id:
            log.warning("Ковры, заказ №%s: карточку отправить не удалось", row.partner_id)
            return False
        await self.store.update(row.partner_id, question_msg_id=int(message_id))
        return True

    def _remember(self, report: CarpetTickReport) -> CarpetTickReport:
        self.last_report = report
        return report

    async def _enabled(self) -> bool:
        if self.is_enabled is None:
            return True
        result = self.is_enabled()
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)
