"""Неотвеченные сообщения в чатах amoCRM: клиент написал, ему не ответили.

Просьба владельца 2026-10-29. Только чтение. Источник — события амо о
сообщениях чата (`incoming_chat_message` / `outgoing_chat_message`): сами
тексты не нужны, достаточно, кто писал последним.

Переписка — это контакт: у одного клиента бывает несколько сделок, а беседа
одна. Амо отдаёт такие события с привязкой к сделке и кладёт id контакта в
`linked_talk_contact_id`; у событий без сделки контакт — сама сущность.

Ответ робота или автосообщения амо (`created_by` = 0) живым ответом не
считается: такие переписки показываются отдельной кучкой, а не прячутся.
Зачем: 29.10 автосообщение ушло 72 клиентам разом при закрытии сделок, и
старое сообщение клиента без живого ответа иначе выглядело бы отвеченным.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

INCOMING = "incoming_chat_message"
OUTGOING = "outgoing_chat_message"

UNANSWERED_DAYS = 14
EVENTS_PAGE = 100                   # больше амо для событий не отдаёт


@dataclass(frozen=True)
class Waiting:
    """Переписка, где последним писал клиент (или после него — только робот)."""

    contact_id: Optional[int]
    entity_type: str                # куда вести ссылку: lead / contact / customer
    entity_id: int
    since: int                      # первое сообщение клиента после живого ответа
    last_at: int                    # последнее сообщение клиента
    messages: int                   # сколько сообщений клиента ждут ответа
    robot_replied: bool             # после клиента писал только робот


def conversation_key(event: dict) -> tuple[str, int]:
    contact_id = event.get("linked_talk_contact_id")
    if contact_id:
        return ("contact", int(contact_id))
    return (str(event.get("entity_type") or "?"), int(event.get("entity_id") or 0))


def find_waiting(events: Sequence[dict]) -> list[Waiting]:
    """Переписки без живого ответа после последних сообщений клиента.

    Порядок событий амо не гарантирует — сортируем сами, по времени и id.
    Ответом считается исходящее сообщение человека; исходящее робота
    помечает переписку, но клиента не «отвечает».
    """
    talks: dict[tuple[str, int], list[dict]] = {}
    for event in events:
        if event.get("type") not in (INCOMING, OUTGOING):
            continue
        talks.setdefault(conversation_key(event), []).append(event)

    waiting: list[Waiting] = []
    for key, talk in talks.items():
        talk.sort(key=lambda e: (int(e.get("created_at") or 0), str(e.get("id") or "")))
        pending: list[dict] = []
        robot_replied = False
        for event in talk:
            if event["type"] == INCOMING:
                pending.append(event)
            elif int(event.get("created_by") or 0) != 0:
                pending = []                     # живой ответ закрывает всё до него
                robot_replied = False
            elif pending:
                robot_replied = True
        if not pending:
            continue
        last = talk[-1]
        waiting.append(Waiting(
            contact_id=key[1] if key[0] == "contact" else None,
            entity_type=str(last.get("entity_type") or key[0]),
            entity_id=int(last.get("entity_id") or key[1]),
            since=int(pending[0].get("created_at") or 0),
            last_at=int(pending[-1].get("created_at") or 0),
            messages=len(pending),
            robot_replied=robot_replied,
        ))
    return sorted(waiting, key=lambda w: w.since)


async def fetch_chat_events(amo: Any, created_from_ts: int) -> list[dict]:
    """События сообщений чатов с указанного момента.

    Фильтр сущности — `contact`: так велит документация амо для этих типов,
    хотя в ответе сущностью будет сделка.
    """
    params: list[tuple[str, Any]] = [
        ("filter[type][]", INCOMING),
        ("filter[type][]", OUTGOING),
        ("filter[entity][]", "contact"),
        ("filter[created_at][from]", created_from_ts),
        ("limit", EVENTS_PAGE),
    ]
    return await amo.get_all("/api/v4/events", "events", params=params)


def outgoing_authors(events: Sequence[dict]) -> dict[int, int]:
    """Кто сколько исходящих написал: id пользователя → число; 0 — робот.

    Сводка печатается рядом со списком, чтобы было видно по факту, а не по
    догадке, какие исходящие засчитаны живым ответом.
    """
    authors: dict[int, int] = {}
    for event in events:
        if event.get("type") == OUTGOING:
            author = int(event.get("created_by") or 0)
            authors[author] = authors.get(author, 0) + 1
    return authors


async def stage_names(amo: Any) -> dict[tuple[int, int], str]:
    """Этапы всех воронок: (воронка, этап) → «воронка · этап». Для подписи в списке.

    Ключ — пара: «Успешно» и «Закрыто» (142/143) в каждой воронке под одним id.
    """
    payload = await amo.get("/api/v4/leads/pipelines")
    names: dict[tuple[int, int], str] = {}
    for pipeline in ((payload or {}).get("_embedded") or {}).get("pipelines") or []:
        pipeline_id = int(pipeline.get("id") or 0)
        for status in ((pipeline.get("_embedded") or {}).get("statuses") or []):
            names[(pipeline_id, int(status.get("id") or 0))] = (
                f"{pipeline.get('name') or pipeline_id} · {status.get('name') or status.get('id')}")
    return names


async def user_names(amo: Any) -> dict[int, str]:
    """Пользователи аккаунта: id → имя. Для сводки авторов исходящих."""
    return {int(user["id"]): str(user.get("name") or user["id"])
            for user in await amo.get_all("/api/v4/users", "users")
            if user.get("id")}
