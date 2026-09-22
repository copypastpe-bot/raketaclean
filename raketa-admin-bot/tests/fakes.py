"""Двойники amoCRM и хранилища для тестов движка.

Живая CRM и Postgres здесь не участвуют: нужны сценарии, которые на настоящих
системах не вызвать по заказу — «амо упала посреди цепочки», «сейлзбот не создал
автосделку», «робота перезапустили между шагами».
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from adminbot.amo import ids
from adminbot.amo.client import ROBOT_TASK_RESULT, AmoError, Intent
from adminbot.models import AmoLink


def _remember_fields(entity: dict, custom_fields: Any) -> None:
    """Записанные поля должны читаться обратно — как в настоящей амо.

    Робот сначала смотрит, что в сделке уже стоит, и только потом решает, писать
    ли туда. Двойник, который забывает свои же записи, показывал бы «поле пустое»
    вечно, и правило «заполненное не трогаем» в тестах не проверялось бы вовсе.
    """
    stored = {field["field_id"]: field
              for field in entity.get("custom_fields_values") or []}
    for field in custom_fields:
        stored[field["field_id"]] = dict(field)
    entity["custom_fields_values"] = list(stored.values())


class FakeAmo:
    """amoCRM в памяти: помнит, что у неё просили и что в ней меняли."""

    def __init__(self, *, dry_run: bool = False):
        self.dry_run = dry_run
        self.leads: dict[int, dict] = {}
        self.contacts: list[dict] = []
        self.tasks: dict[int, list[dict]] = {}
        self.task_results: dict[int, str] = {}   # чем закрыта задача
        # Примечания сейлзбота «родитель → дочка»: parent_id -> [(created_at, child_id)].
        self.child_notes: dict[int, list[tuple[int, int]]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.fail_on: Optional[str] = None       # имя метода, который должен упасть
        # Задача 3 ТЗ 2026-09-22: правка обеих сделок — нужен способ уронить
        # update_lead только для одной из них (лида воронки 1), не для другой.
        # None — падает на любом lead_id, как раньше.
        self.fail_lead_id: Optional[int] = None
        self._next_id = 90000

    # --- чтение ---

    async def find_leads_by_phone(self, phone10: str) -> list[dict]:
        self._maybe_fail("find_leads_by_phone")
        self.calls.append(("find_leads_by_phone", phone10))
        return list(self.leads.values())

    async def get_child_lead_id(self, lead_id: int) -> Optional[int]:
        self._maybe_fail("get_child_lead_id")
        self.calls.append(("get_child_lead_id", lead_id))
        notes = self.child_notes.get(lead_id) or []
        if not notes:
            return None
        return max(notes, key=lambda item: item[0])[1]

    async def find_contacts_by_phone(self, phone10: str) -> list[dict]:
        self._maybe_fail("find_contacts_by_phone")
        self.calls.append(("find_contacts_by_phone", phone10))
        return list(self.contacts)

    async def get_lead(self, lead_id: int) -> Optional[dict]:
        self._maybe_fail("get_lead")
        self.calls.append(("get_lead", lead_id))
        return self.leads.get(lead_id)

    async def get_lead_tasks(self, lead_id: int) -> list[dict]:
        self._maybe_fail("get_lead_tasks")
        self.calls.append(("get_lead_tasks", lead_id))
        return list(self.tasks.get(lead_id, []))

    # --- запись ---

    async def update_lead(self, lead_id: int, **fields) -> Optional[Intent]:
        payload = {key: value for key, value in fields.items() if value is not None}
        if not payload:
            return None
        self._maybe_fail("update_lead", lead_id)
        self.calls.append(("update_lead", (lead_id, payload)))
        if not self.dry_run:
            lead = self.leads.setdefault(lead_id, {"id": lead_id})
            lead.update({key: value for key, value in payload.items()
                         if key != "custom_fields"})
            _remember_fields(lead, payload.get("custom_fields") or [])
        return Intent(action="update_lead", entity="lead", entity_id=lead_id,
                      payload=payload, performed=not self.dry_run)

    async def move_lead(self, lead_id: int, pipeline_id: int, status_id: int) -> Intent:
        self._maybe_fail("move_lead")
        self.calls.append(("move_lead", (lead_id, pipeline_id, status_id)))
        if not self.dry_run:
            lead = self.leads.setdefault(lead_id, {"id": lead_id})
            lead["pipeline_id"], lead["status_id"] = pipeline_id, status_id
        return Intent(action="move_lead", entity="lead", entity_id=lead_id,
                      payload={"pipeline_id": pipeline_id, "status_id": status_id},
                      performed=not self.dry_run)

    async def create_lead(self, **fields) -> Intent:
        self._maybe_fail("create_lead")
        self.calls.append(("create_lead", fields))
        new_id = None
        if not self.dry_run:
            new_id = self._take_id()
            self.leads[new_id] = {
                "id": new_id, "pipeline_id": fields["pipeline_id"],
                "status_id": fields["status_id"], "name": fields.get("name"),
            }
            _remember_fields(self.leads[new_id], fields.get("custom_fields") or [])
        return Intent(action="create_lead", entity="lead", entity_id=new_id,
                      payload=fields, performed=not self.dry_run)

    async def get_contact(self, contact_id: int) -> Optional[dict]:
        self._maybe_fail("get_contact")
        self.calls.append(("get_contact", contact_id))
        return next((c for c in self.contacts if c["id"] == contact_id), None)

    async def update_contact(self, contact_id: int, *, name: str) -> Intent:
        self._maybe_fail("update_contact")
        self.calls.append(("update_contact", (contact_id, name)))
        if not self.dry_run:
            for contact in self.contacts:
                if contact["id"] == contact_id:
                    contact["name"] = name
        return Intent(action="update_contact", entity="contact", entity_id=contact_id,
                      payload={"name": name}, performed=not self.dry_run)

    async def create_contact(self, *, name: str, phone: str) -> Intent:
        self._maybe_fail("create_contact")
        self.calls.append(("create_contact", (name, phone)))
        new_id = None
        if not self.dry_run:
            new_id = self._take_id()
            self.contacts.append({"id": new_id, "name": name})
        return Intent(action="create_contact", entity="contact", entity_id=new_id,
                      payload={"name": name}, performed=not self.dry_run)

    async def complete_task(self, task_id: int, result_text: str = ROBOT_TASK_RESULT) -> Intent:
        self._maybe_fail("complete_task")
        self.calls.append(("complete_task", task_id))
        self.task_results[task_id] = result_text
        return Intent(action="complete_task", entity="task", entity_id=task_id,
                      payload={"is_completed": True}, performed=not self.dry_run)

    async def add_note(self, lead_id: int, text: str) -> Intent:
        self._maybe_fail("add_note")
        self.calls.append(("add_note", (lead_id, text)))
        return Intent(action="add_note", entity="lead", entity_id=lead_id,
                      payload={"text": text}, performed=not self.dry_run)

    # --- вспомогательное для тестов ---

    def add_lead(self, lead_id: int, pipeline_id: int, status_id: int, **extra) -> None:
        self.leads[lead_id] = {"id": lead_id, "pipeline_id": pipeline_id,
                               "status_id": status_id, **extra}

    def add_task(self, lead_id: int, task_id: int, task_type_id: int) -> None:
        self.tasks.setdefault(lead_id, []).append(
            {"id": task_id, "task_type_id": task_type_id, "is_completed": False})

    def add_child_note(self, parent_id: int, child_id: int, created_at: int = 0) -> None:
        """Примечание сейлзбота «у лида parent_id есть дочка child_id»."""
        self.child_notes.setdefault(parent_id, []).append((created_at, child_id))

    def calls_of(self, name: str) -> list[Any]:
        return [payload for called, payload in self.calls if called == name]

    def _take_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _maybe_fail(self, name: str, lead_id: Optional[int] = None) -> None:
        if self.fail_on != name:
            return
        if self.fail_lead_id is not None and lead_id != self.fail_lead_id:
            return                          # эта сделка падать не должна
        raise AmoError(500, f"поддельный сбой в {name}")


class FakeStore:
    """Хранилище состояния в памяти.

    Часы передаются снаружи: движок сверяет по ним, сколько уже ждёт автосделку,
    поэтому хранилище и движок должны жить в одном времени.
    """

    def __init__(self, now=None):
        self.links: dict[int, AmoLink] = {}
        self.actions: list[dict] = []
        self._now = now or _now

    async def get(self, order_id: int) -> Optional[AmoLink]:
        return self.links.get(order_id)

    async def create(self, order_id: int, phone10: Optional[str]) -> AmoLink:
        link = self.links.get(order_id)
        if link is None:
            link = AmoLink(order_id=order_id, phone10=phone10 or "", status="new",
                           checklist={}, created_at=self._now(), updated_at=self._now())
            self.links[order_id] = link
        return link

    async def update(self, order_id: int, **fields) -> Optional[AmoLink]:
        link = self.links.get(order_id)
        if link is None:
            return None
        self.links[order_id] = replace(link, **fields, updated_at=self._now())
        return self.links[order_id]

    async def mark_step(self, order_id: int, step: str) -> None:
        link = self.links[order_id]
        checklist = dict(link.checklist)
        checklist.setdefault(step, self._now().isoformat())
        self.links[order_id] = replace(link, checklist=checklist, updated_at=self._now())

    async def log(self, order_id: int, action: str, *, dry_run: bool,
                  entity=None, amo_id=None, payload=None) -> None:
        self.actions.append({"order_id": order_id, "action": action, "dry_run": dry_run,
                             "entity": entity, "amo_id": amo_id, "payload": payload})

    async def taken_leads(self, phone10: str, exclude_order_id: int) -> set[int]:
        taken: set[int] = set()
        for link in self.links.values():
            if link.phone10 != phone10 or link.order_id == exclude_order_id:
                continue
            taken.update(value for value in (link.primary_lead_id, link.real_lead_id) if value)
        return taken

    def actions_of(self, action: str) -> list[dict]:
        return [row for row in self.actions if row["action"] == action]


def _now() -> datetime:
    return datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
