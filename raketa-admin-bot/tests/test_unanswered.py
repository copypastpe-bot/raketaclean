"""Неотвеченные сообщения: кто ждёт ответа, а кто нет.

Главное — не спрятать ждущего клиента: автосообщение робота ответом не
считается, а переписка одного контакта по разным сделкам — одна.
"""

from adminbot.amo.unanswered import (
    INCOMING, OUTGOING, fetch_chat_events, find_waiting, outgoing_authors)

MANAGER = 777
ROBOT = 0


def an_event(event_id: str, kind: str, at: int, *, contact=501, lead=9001,
             by=ROBOT) -> dict:
    event = {"id": event_id, "type": kind, "created_at": at, "created_by": by,
             "entity_type": "lead", "entity_id": lead}
    if contact is not None:
        event["linked_talk_contact_id"] = contact
    return event


def test_client_wrote_last_is_waiting():
    events = [an_event("1", OUTGOING, 100, by=MANAGER), an_event("2", INCOMING, 200),
              an_event("3", INCOMING, 300)]
    [item] = find_waiting(events)
    assert (item.contact_id, item.entity_id) == (501, 9001)
    assert (item.since, item.last_at, item.messages) == (200, 300, 2)
    assert not item.robot_replied


def test_manager_answer_closes_conversation():
    events = [an_event("1", INCOMING, 100), an_event("2", OUTGOING, 200, by=MANAGER)]
    assert find_waiting(events) == []


def test_robot_message_is_not_an_answer():
    """Автосообщение при закрытии сделки не должно прятать ждущего клиента."""
    events = [an_event("1", INCOMING, 100), an_event("2", OUTGOING, 200, by=ROBOT)]
    [item] = find_waiting(events)
    assert item.robot_replied and item.since == 100


def test_client_reply_to_robot_message_is_waiting_plainly():
    events = [an_event("1", OUTGOING, 100, by=ROBOT), an_event("2", INCOMING, 200)]
    [item] = find_waiting(events)
    assert not item.robot_replied and item.since == 200


def test_order_of_events_from_amo_does_not_matter():
    events = [an_event("2", OUTGOING, 200, by=MANAGER), an_event("1", INCOMING, 100)]
    assert find_waiting(events) == []


def test_one_contact_across_two_leads_is_one_conversation():
    events = [an_event("1", INCOMING, 100, lead=9001),
              an_event("2", OUTGOING, 200, lead=9002, by=MANAGER)]
    assert find_waiting(events) == []


def test_link_goes_to_the_latest_lead():
    events = [an_event("1", INCOMING, 100, lead=9001), an_event("2", INCOMING, 200, lead=9002)]
    [item] = find_waiting(events)
    assert item.entity_id == 9002


def test_contact_without_lead():
    event = {"id": "1", "type": INCOMING, "created_at": 100, "created_by": 0,
             "entity_type": "contact", "entity_id": 501}
    [item] = find_waiting([event])
    assert (item.contact_id, item.entity_type, item.entity_id) == (501, "contact", 501)


def test_oldest_waiting_first():
    events = [an_event("1", INCOMING, 300, contact=1), an_event("2", INCOMING, 100, contact=2)]
    assert [w.contact_id for w in find_waiting(events)] == [2, 1]


def test_other_event_types_ignored():
    assert find_waiting([an_event("1", "lead_status_changed", 100)]) == []


def test_outgoing_authors():
    events = [an_event("1", OUTGOING, 1, by=MANAGER), an_event("2", OUTGOING, 2),
              an_event("3", OUTGOING, 3), an_event("4", INCOMING, 4)]
    assert outgoing_authors(events) == {MANAGER: 1, ROBOT: 2}


async def test_events_asked_by_contact_entity():
    class Fake:
        calls = []

        async def get_all(self, path, key, params=None):
            self.calls.append((path, key, list(params)))
            return []

    amo = Fake()
    await fetch_chat_events(amo, 12345)
    path, key, params = amo.calls[0]
    assert (path, key) == ("/api/v4/events", "events")
    assert ("filter[entity][]", "contact") in params
    assert ("filter[type][]", INCOMING) in params and ("filter[type][]", OUTGOING) in params
    assert ("filter[created_at][from]", 12345) in params
    assert ("limit", 100) in params
