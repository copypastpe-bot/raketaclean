"""Наблюдатель почты: письмо партнёра → разобранные строки → сделки в amoCRM.

Опрашивает почту редко — раз в час: отчёты приходят раз в неделю, чаще незачем.

Три правила, которые определяют устройство цикла:

1. **Письмо помечается разобранным только целиком.** Если хоть одна строка
   не прошла, письмо остаётся в работе: лучше разобрать его повторно (от двойной
   записи защищает номер заказа партнёра), чем потерять отчёт молча.
2. **Разобранные письма помнит и база.** Пометка в почтовом ящике может не
   поставиться — тогда сработает вторая страховка, и робот не начнёт заново.
3. **Незавершённые строки живут своей жизнью.** Пока робот ждёт автосделку
   сейлзбота, письмо давно разобрано; строка берётся из базы, где её сохранили.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from adminbot.carpets.report import CarpetRow, parse_report, rows_to_process
from adminbot.models import CarpetLink

log = logging.getLogger(__name__)

# Статусы, по которым работа ещё не закончена. `waiting_owner` здесь тоже есть:
# движок по нему ничего не делает, но наблюдателю он нужен, чтобы дослать карточку,
# если Telegram в прошлый раз не ответил.
# Пока есть незаконченная работа, следующий проход делаем скоро, а не через час.
QUICK_RETRY_SEC = 60

ACTIVE_STATUSES: tuple[str, ...] = (
    "new", "in_progress", "waiting_salesbot", "error", "waiting_owner",
)


class CarpetStateStore(Protocol):
    """Что наблюдателю нужно от хранилища сверх того, что нужно движку."""

    async def letter_processed(self, uid: str) -> bool: ...

    async def remember_letter(self, uid: str, subject: Optional[str],
                              files: list[str], rows_total: int) -> None: ...

    async def pending_rows(self) -> list[tuple[CarpetRow, CarpetLink]]: ...

    async def update(self, partner_id: int, **fields: Any) -> Optional[CarpetLink]: ...


@dataclass(frozen=True)
class CarpetTickReport:
    """Итог одного прохода — для журнала, отчёта владельцу и команды /status."""

    paused: bool = False
    letters: int = 0
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
        on_question: Optional[Callable[[CarpetRow, CarpetLink], Awaitable[Optional[int]]]] = None,
        on_report: Optional[Callable[[Any, CarpetTickReport], Awaitable[None]]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.engine = engine
        self.mailbox = mailbox
        self.store = store
        self.parse = parse
        self.is_enabled = is_enabled
        self.poll_interval_sec = poll_interval_sec
        self.on_question = on_question
        self.on_report = on_report
        self.sleep = sleep
        self.last_report: Optional[CarpetTickReport] = None

    async def tick(self) -> CarpetTickReport:
        """Один проход: разобрать новые письма и доделать незавершённое."""
        if not await self._enabled():
            return self._remember(CarpetTickReport(paused=True))

        statuses: Counter[str] = Counter()
        questions: list[int] = []
        failures: list[tuple[Any, str]] = []

        letters = await self.mailbox.fetch_new()
        for letter in letters:
            await self._handle_letter(letter, statuses, questions, failures)

        # Незавершённое из прошлых писем: ожидание автосделки, ошибки, вопросы.
        for row, link in await self.store.pending_rows():
            await self._handle_row(row, None, statuses, questions, failures)

        return self._remember(CarpetTickReport(
            letters=len(letters), processed=sum(statuses.values()),
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
                             questions: list, failures: list) -> None:
        if await self.store.letter_processed(letter.uid):
            log.info("Письмо %s уже разобрано — помечаю прочитанным", letter.uid)
            await self.mailbox.mark_seen(letter.uid)
            return

        letter_report = CarpetTickReport()
        before_failures = len(failures)
        rows_total = 0

        for name, data in letter.attachments.items():
            try:
                rows = self.parse(data)
            except Exception as exc:                   # noqa: BLE001 — кривой файл не роняет проход
                log.exception("Отчёт %s разобрать не удалось", name)
                failures.append((name, f"{type(exc).__name__}: {exc}"))
                continue

            completed, refused = rows_to_process(rows)
            rows_total += len(completed) + len(refused)
            for row in completed + refused:
                await self._handle_row(row, name, statuses, questions, failures)

        if len(failures) > before_failures:
            log.warning("Письмо %s оставляю в работе: были сбои", letter.uid)
            return

        await self.store.remember_letter(letter.uid, letter.subject,
                                         list(letter.attachments), rows_total)
        await self.mailbox.mark_seen(letter.uid)

        if self.on_report:
            await self.on_report(letter, CarpetTickReport(
                letters=1, processed=rows_total, by_status=dict(statuses),
                questions=tuple(questions), failures=()))

    async def _handle_row(self, row: CarpetRow, source_file: Optional[str],
                          statuses: Counter, questions: list, failures: list) -> None:
        try:
            link = await self.engine.process_row(row, source_file=source_file)
        except Exception as exc:                       # noqa: BLE001
            log.exception("Ковры, заказ партнёра №%s: проход прерван", row.partner_id)
            failures.append((row.partner_id, f"{type(exc).__name__}: {exc}"))
            return

        if link is None:
            return
        statuses[link.status] += 1
        if await self._maybe_ask(row, link):
            questions.append(row.partner_id)

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
