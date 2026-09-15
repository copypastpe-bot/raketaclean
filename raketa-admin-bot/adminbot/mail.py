"""Почтовый ящик робота: забрать отчёты партнёра, ничего не отправляя.

Ящик отдельный (`amoraketaclean@yandex.ru`), робот смотрит одну папку и берёт
только непрочитанные письма с вложениями `.xlsx`. Отправлять письма он не умеет
вовсе — это сознательно: доступ нужен только на чтение.

Две вещи, которые здесь важны:

- `imaplib` синхронный, поэтому работа с сетью уходит в отдельный поток:
  иначе бот замирал бы на время обмена с почтой;
- письмо помечается прочитанным ТОЛЬКО отдельной командой, после того как строки
  разобраны. Робот упал посередине — письмо остаётся непрочитанным и разберётся
  снова. Потерять отчёт хуже, чем обработать его дважды: от повторной обработки
  защищает номер заказа партнёра в базе;
- письма адресуются постоянным UID (`box.uid(...)`), а не порядковым номером.
  Порядковый номер — это место письма в папке: удалили одно письмо, и у всех
  следующих номер сдвинулся. Робот же помнит отложенное письмо неделями (оно
  лежит непрочитанным), и по сдвинутому номеру он пометил бы чужое письмо.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import os
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# Какие вложения считаем отчётом партнёра.
REPORT_SUFFIXES = (".xlsx", ".xls")


class MailError(RuntimeError):
    """Почта недоступна или ответила отказом."""


@dataclass(frozen=True)
class MailSettings:
    """Доступ к ящику. Пароль намеренно не показывается при печати."""

    host: str
    user: str
    password: str = field(repr=False)
    folder: str = "INBOX"
    port: int = 993

    def connect(self) -> imaplib.IMAP4_SSL:
        box = imaplib.IMAP4_SSL(self.host, self.port)
        box.login(self.user, self.password)
        return box


def mail_settings_from_env() -> MailSettings:
    """Собрать доступ к почте из окружения."""
    def require(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
        return value

    return MailSettings(
        host=os.environ.get("MAIL_IMAP_HOST", "imap.yandex.ru").strip(),
        user=require("MAIL_USER"),
        password=require("MAIL_APP_PASSWORD"),
        folder=os.environ.get("MAIL_FOLDER", "INBOX").strip() or "INBOX",
        port=int(os.environ.get("MAIL_IMAP_PORT", "993")),
    )


@dataclass(frozen=True)
class Letter:
    """Письмо с отчётами: то, что нужно роботу, и ничего лишнего."""

    uid: str                                       # постоянный UID письма в папке
    subject: str
    sender: str
    date: Optional[str] = None
    attachments: dict[str, bytes] = field(default_factory=dict)


class MailBox:
    def __init__(self, *, connect: Callable[[], Any], folder: str) -> None:
        self._connect = connect
        self.folder = folder

    async def fetch_new(self) -> list[Letter]:
        """Непрочитанные письма папки, у которых есть вложение-отчёт."""
        return await asyncio.to_thread(self._fetch_new)

    async def mark_seen(self, uid: str) -> None:
        """Пометить письмо разобранным. Вызывается после успешной обработки."""
        await asyncio.to_thread(self._mark_seen, uid)

    # --- синхронная часть, живёт в отдельном потоке ---

    def _fetch_new(self) -> list[Letter]:
        box = self._connect()
        try:
            # Папку открываем только на чтение: даже случайная команда не должна
            # изменить состояние ящика владельца.
            self._select(box, readonly=True)
            ok, data = box.uid("search", None, "UNSEEN")
            if ok != "OK":
                raise MailError(f"Почта не отдала список писем: {data}")

            letters: list[Letter] = []
            for uid in (data[0] or b"").split():
                letter = self._read_letter(box, uid)
                if letter is not None and letter.attachments:
                    letters.append(letter)
            return letters
        finally:
            _close(box)

    def _mark_seen(self, uid: str) -> None:
        box = self._connect()
        try:
            self._select(box, readonly=False)
            box.uid("store", uid.encode(), "+FLAGS", "\\Seen")
        finally:
            _close(box)

    def _select(self, box: Any, *, readonly: bool) -> None:
        ok, data = box.select(self.folder, readonly)
        if ok != "OK":
            raise MailError(f"Не открыть папку {self.folder}: {data}")

    def _read_letter(self, box: Any, uid: bytes) -> Optional[Letter]:
        # BODY.PEEK, а не RFC822: обычное чтение письма ставит ему флаг «прочитано»,
        # и письмо пропало бы из работы ещё до того, как строки разобраны.
        # Поймано на боевой почте 2026-08-26: проверка «что там лежит» съела
        # непрочитанность обоих отчётов партнёра.
        ok, raw = box.uid("fetch", uid, "(BODY.PEEK[])")
        if ok != "OK" or not raw or not isinstance(raw[0], tuple):
            log.warning("Письмо %s прочитать не удалось", uid)
            return None

        message = email.message_from_bytes(raw[0][1])
        attachments: dict[str, bytes] = {}
        for part in message.walk():
            name = _decode(part.get_filename())
            if not name or not name.lower().endswith(REPORT_SUFFIXES):
                continue
            payload = part.get_payload(decode=True)
            if payload:
                attachments[name] = payload

        return Letter(
            uid=uid.decode(),
            subject=_decode(message.get("Subject")) or "",
            sender=_decode(message.get("From")) or "",
            date=message.get("Date"),
            attachments=attachments,
        )


def _decode(value: Optional[str]) -> Optional[str]:
    """Тема и имена файлов приезжают закодированными — приводим к обычному тексту."""
    if not value:
        return None
    try:
        return str(make_header(decode_header(value)))
    except Exception:                              # noqa: BLE001 — кривой заголовок не повод падать
        return value


def _close(box: Any) -> None:
    try:
        box.logout()
    except Exception:                              # noqa: BLE001
        pass
