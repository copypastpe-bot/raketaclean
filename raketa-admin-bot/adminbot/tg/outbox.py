"""Почта владельца: сообщение не теряется, когда Telegram не отвечает.

Telegram с этого VPS доступен нестабильно, и раньше сообщение, не ушедшее
с первой попытки, пропадало навсегда: робот писал ошибку в журнал и шёл
работать дальше. Так 2026-09-02 потерялся отчёт о сделке по записи календаря,
а 2026-09-01 — вечерняя сводка (решение владельца 2026-09-02: доотправлять).

Три правила, которые определяют устройство почты:

1. **Сначала как раньше.** Почта пробует отправить сразу и возвращает номер
   сообщения вызывающему коду. Пока связь есть, поведение робота не меняется
   ни в чём — и все нынешние отметки «уже отправлено» продолжают работать.
2. **Не ушло — становится долгом.** Текст, кнопки и назначение ложатся
   в хранилище и досылаются с растущей паузой, пока не уйдут или не протухнут.
   После доставки почта сама ставит отметку, которую поставил бы вызывающий
   код: номер сообщения в `done_msg_id` или `question_msg_id`.
3. **Перед досылкой спрашиваем, нужен ли долг ещё.** Отметка уже стоит —
   значит доставка удалась другим путём, и второе сообщение об одном и том же
   владельцу не нужно.

Чего почта НЕ обещает: «ровно один раз». Если Telegram принял сообщение,
а ответ потерялся по дороге, повтор даст дубль. Для владельца «два раза об
одном» лучше, чем «ни разу»; у карточек с кнопками дубли снимает правило 3.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

from adminbot import db

log = logging.getLogger(__name__)

# Сколько ждать перед следующей попыткой — по номеру уже сделанных. Первые
# паузы короткие: обрывы связи здесь обычно длятся минуты. Дальше реже, чтобы
# долгая недоступность Telegram не превращалась в непрерывный стук в стену.
BACKOFF_SEC: tuple[int, ...] = (60, 180, 600, 1800)

# Сколько живёт сообщение по умолчанию. Сутки — про отчёты и карточки: работа
# всё равно сделана, вопрос всё равно ждёт ответа.
DEFAULT_TTL_SEC = 24 * 3600

# Вечерняя сводка за прошедший день устаревает быстро: доставить её к обеду
# следующего дня значит смутить владельца, а не помочь ему.
SUMMARY_TTL_SEC = 6 * 3600

# Как часто фоновая задача заглядывает в долги.
POLL_INTERVAL_SEC = 60


@dataclass(frozen=True)
class Purpose:
    """Назначение сообщения: нужно ли оно ещё и что отметить после доставки.

    Реестр назначений собирается там же, где собираются отправители (main.py):
    только там известны хранилища, которым принадлежат отметки. Сама почта про
    записи календаря и заказы партнёра ничего не знает.
    """

    still_needed: Optional[Callable[[str], Awaitable[bool]]] = None
    on_delivered: Optional[Callable[[str, int], Awaitable[None]]] = None
    ttl_sec: int = DEFAULT_TTL_SEC


class MailStore(Protocol):
    """Что почте нужно от хранилища долгов — и ничего сверх того."""

    async def add(self, *, chat_id: int, kind: str, ref: Optional[str], text: str,
                  reply_markup: Optional[dict], expires_at: datetime,
                  next_try_at: datetime, error: str) -> int: ...

    async def due(self, now: datetime, limit: int = 20) -> list[dict]: ...

    async def mark_sent(self, letter_id: int, message_id: Optional[int],
                        now: datetime) -> None: ...

    async def postpone(self, letter_id: int, next_try_at: datetime,
                       error: str) -> None: ...

    async def drop(self, letter_id: int, now: datetime, reason: str) -> None: ...

    async def waiting(self) -> int: ...


class MemoryMailStore:
    """Долги в памяти: для тестов и для запуска без своей базы."""

    def __init__(self) -> None:
        self.letters: list[dict] = []
        self._next_id = 1

    async def add(self, *, chat_id: int, kind: str, ref: Optional[str], text: str,
                  reply_markup: Optional[dict], expires_at: datetime,
                  next_try_at: datetime, error: str) -> int:
        letter_id = self._next_id
        self._next_id += 1
        self.letters.append({
            "id": letter_id, "chat_id": chat_id, "kind": kind, "ref": ref,
            "text": text, "reply_markup": reply_markup, "attempts": 1,
            "expires_at": expires_at, "next_try_at": next_try_at,
            "sent_at": None, "dropped_at": None, "message_id": None,
            "last_error": error,
        })
        return letter_id

    async def due(self, now: datetime, limit: int = 20) -> list[dict]:
        ready = [letter for letter in self.letters
                 if letter["sent_at"] is None and letter["dropped_at"] is None
                 and letter["next_try_at"] <= now]
        return sorted(ready, key=lambda letter: letter["id"])[:limit]

    async def mark_sent(self, letter_id: int, message_id: Optional[int],
                        now: datetime) -> None:
        letter = self._find(letter_id)
        if letter is not None:
            letter["sent_at"], letter["message_id"] = now, message_id

    async def postpone(self, letter_id: int, next_try_at: datetime, error: str) -> None:
        letter = self._find(letter_id)
        if letter is not None:
            letter["attempts"] += 1
            letter["next_try_at"], letter["last_error"] = next_try_at, error

    async def drop(self, letter_id: int, now: datetime, reason: str) -> None:
        letter = self._find(letter_id)
        if letter is not None:
            letter["dropped_at"], letter["drop_reason"] = now, reason

    async def waiting(self) -> int:
        return len([letter for letter in self.letters
                    if letter["sent_at"] is None and letter["dropped_at"] is None])

    def _find(self, letter_id: int) -> Optional[dict]:
        return next((letter for letter in self.letters if letter["id"] == letter_id), None)


class PgMailStore:
    """Боевое хранилище долгов: схема `adminbot`, таблица owner_outbox.

    Долг живёт в базе, а не в памяти процесса, потому что обрыв связи и
    перезапуск службы часто ходят парой: деплой посреди недоступного Telegram
    стоил бы владельцу всех накопленных сообщений.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def add(self, *, chat_id: int, kind: str, ref: Optional[str], text: str,
                  reply_markup: Optional[dict], expires_at: datetime,
                  next_try_at: datetime, error: str) -> int:
        return await db.add_owner_letter(
            self._pool, chat_id=chat_id, kind=kind, ref=ref, text=text,
            reply_markup=reply_markup, expires_at=expires_at,
            next_try_at=next_try_at, last_error=error)

    async def due(self, now: datetime, limit: int = 20) -> list[dict]:
        return await db.fetch_due_owner_letters(self._pool, now, limit)

    async def mark_sent(self, letter_id: int, message_id: Optional[int],
                        now: datetime) -> None:
        await db.mark_owner_letter_sent(self._pool, letter_id, message_id, now)

    async def postpone(self, letter_id: int, next_try_at: datetime, error: str) -> None:
        await db.postpone_owner_letter(self._pool, letter_id, next_try_at, error)

    async def drop(self, letter_id: int, now: datetime, reason: str) -> None:
        await db.drop_owner_letter(self._pool, letter_id, now, reason)

    async def waiting(self) -> int:
        return await db.count_owner_letters_waiting(self._pool)


class OwnerMail:
    """Единственная дверь, через которую робот пишет владельцу."""

    def __init__(self, *, bot: Any, chat_id: int, store: MailStore,
                 purposes: Optional[dict[str, Purpose]] = None,
                 now: Optional[Callable[[], datetime]] = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 poll_interval_sec: int = POLL_INTERVAL_SEC) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.store = store
        self.purposes = dict(purposes or {})
        self.sleep = sleep
        self.poll_interval_sec = poll_interval_sec
        self._now = now or (lambda: datetime.now(timezone.utc))

    def register(self, kind: str, purpose: Purpose) -> None:
        """Назначить, что делать с сообщением этого вида после доставки."""
        self.purposes[kind] = purpose

    async def send(self, text: str, *, kind: str, ref: Optional[Any] = None,
                   reply_markup: Any = None) -> Optional[int]:
        """Отправить владельцу. Не вышло — сообщение становится долгом.

        Возвращает номер сообщения (как `bot.send_message`) или None, если
        отправить сейчас не удалось. None означает «доставлю позже сама», и
        вызывающему коду отмечать «отправлено» нельзя — иначе долг пропадёт.
        """
        try:
            sent = await self.bot.send_message(self.chat_id, text,
                                               reply_markup=reply_markup)
        except Exception as exc:                       # noqa: BLE001 — Telegram падает
            await self._remember(text, kind=kind, ref=ref,
                                 reply_markup=reply_markup, error=exc)
            return None
        return getattr(sent, "message_id", None)

    async def deliver_debts(self) -> int:
        """Досылка созревших долгов. Возвращает, сколько ушло."""
        now = self._now()
        delivered = 0
        for letter in await self.store.due(now):
            if await self._settle(letter, now):
                delivered += 1
        return delivered

    async def run_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.deliver_debts()
            except Exception:                          # noqa: BLE001
                log.exception("Почта владельцу: разбор долгов не удался")
            await self.sleep(self.poll_interval_sec)

    async def waiting(self) -> int:
        """Сколько сообщений ждёт отправки — строка для /status и сводки."""
        return await self.store.waiting()

    # --- внутреннее ---

    async def _settle(self, letter: dict, now: datetime) -> bool:
        """Разобраться с одним долгом. True — сообщение ушло."""
        letter_id = letter["id"]
        if letter["expires_at"] <= now:
            log.warning("Почта владельцу: сообщение %s (%s) протухло, не отправлено",
                        letter_id, letter["kind"])
            await self.store.drop(letter_id, now, "протухло")
            return False

        purpose = self.purposes.get(letter["kind"])
        if not await self._still_needed(purpose, letter):
            log.info("Почта владельцу: сообщение %s (%s) больше не нужно",
                     letter_id, letter["kind"])
            await self.store.drop(letter_id, now, "нужда отпала")
            return False

        try:
            sent = await self.bot.send_message(letter["chat_id"], letter["text"],
                                               reply_markup=letter.get("reply_markup"))
        except Exception as exc:                       # noqa: BLE001
            await self.store.postpone(letter_id, self._next_try(letter, now),
                                      f"{type(exc).__name__}: {exc}")
            return False

        message_id = getattr(sent, "message_id", None)
        await self.store.mark_sent(letter_id, message_id, now)
        log.info("Почта владельцу: сообщение %s (%s) доставлено с %s попытки",
                 letter_id, letter["kind"], letter.get("attempts", 0) + 1)
        await self._after_delivery(purpose, letter, message_id)
        return True

    async def _still_needed(self, purpose: Optional[Purpose], letter: dict) -> bool:
        """Не отпала ли нужда в сообщении. Сомнение решается в пользу отправки."""
        if purpose is None or purpose.still_needed is None or not letter.get("ref"):
            return True
        try:
            return bool(await purpose.still_needed(letter["ref"]))
        except Exception:                              # noqa: BLE001
            log.exception("Почта владельцу: не удалось проверить сообщение %s",
                          letter["id"])
            return True

    async def _after_delivery(self, purpose: Optional[Purpose], letter: dict,
                              message_id: Optional[int]) -> None:
        """Поставить отметку, которую поставил бы вызывающий код при удаче."""
        if (purpose is None or purpose.on_delivered is None
                or not letter.get("ref") or message_id is None):
            return
        try:
            await purpose.on_delivered(letter["ref"], int(message_id))
        except Exception:                              # noqa: BLE001
            # Отметку не поставили — сообщение уже у владельца, и терять его
            # нельзя. Худшее, чем это грозит, — повторный отчёт следующим
            # проходом; молчание было бы хуже.
            log.exception("Почта владельцу: отметка по сообщению %s не поставлена",
                          letter["id"])

    async def _remember(self, text: str, *, kind: str, ref: Optional[Any],
                        reply_markup: Any, error: BaseException) -> None:
        now = self._now()
        purpose = self.purposes.get(kind)
        ttl = purpose.ttl_sec if purpose else DEFAULT_TTL_SEC
        try:
            letter_id = await self.store.add(
                chat_id=self.chat_id, kind=kind,
                ref=None if ref is None else str(ref), text=text,
                reply_markup=_dump_markup(reply_markup),
                expires_at=now + timedelta(seconds=ttl),
                next_try_at=now + timedelta(seconds=BACKOFF_SEC[0]),
                error=f"{type(error).__name__}: {error}")
        except Exception:                              # noqa: BLE001
            # База тоже недоступна — сказать больше нечем, но проход не роняем:
            # работа с CRM от Telegram не зависит.
            log.exception("Почта владельцу: сообщение (%s) не ушло и не сохранилось",
                          kind)
            return
        log.warning("Почта владельцу: сообщение (%s) не ушло (%s), досылаю позже "
                    "[долг %s]", kind, error, letter_id)

    def _next_try(self, letter: dict, now: datetime) -> datetime:
        attempts = int(letter.get("attempts") or 1)
        pause = BACKOFF_SEC[min(attempts, len(BACKOFF_SEC) - 1)]
        return now + timedelta(seconds=pause)


def _dump_markup(reply_markup: Any) -> Optional[dict]:
    """Кнопки — в JSON: долг переживает перезапуск, а объект aiogram не пережил бы."""
    if reply_markup is None:
        return None
    if isinstance(reply_markup, dict):
        return reply_markup
    dump = getattr(reply_markup, "model_dump", None)
    if dump is None:
        return None
    return dump(mode="json", exclude_none=True)
