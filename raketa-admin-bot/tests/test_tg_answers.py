"""Ответы владельца на карточки: выбор сделки и запуск хвоста.

Нажатие кнопки ничего не делает в amoCRM само по себе — оно только записывает
решение владельца рядом с заказом. Дальше обычным ходом сработает наблюдатель.
Так ответ не теряется, даже если робота перезапустят через секунду.
"""

from adminbot.amo import ids
from adminbot.models import AmoLink
from adminbot.tg.bot import BACKLOG_GO, BACKLOG_HOLD, OwnerAnswers
from tests.test_tg_guard import OWNER_ID, STRANGER_ID, FakeUser

QUESTION = {"reason": "ask_owner", "options": [
    {"lead_id": 41400001, "pipeline_id": ids.PIPELINE_REALIZATION,
     "date": "2026-07-22", "price": 5000, "name": "Ниж! Матрас"},
    {"lead_id": 41400002, "pipeline_id": ids.PIPELINE_PRIMARY,
     "date": "2026-07-24", "price": 6000, "name": None},
]}


class FakeCardMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class FakeCallback:
    def __init__(self, data, user_id=OWNER_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeCardMessage()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


class FakeStore:
    def __init__(self, link):
        self.links = {link.order_id: link} if link else {}
        self.updates = []

    async def get(self, order_id):
        return self.links.get(order_id)

    async def update(self, order_id, **fields):
        self.updates.append(fields)
        link = self.links.get(order_id)
        if link is None:
            return None
        self.links[order_id] = AmoLink(**{**link.__dict__, **fields})
        return self.links[order_id]


def waiting_link(order_id=596, question=QUESTION, path=None):
    return AmoLink(order_id=order_id, phone10="9601861067", status="waiting_owner",
                   path=path, question=question)


def make_answers(link=None, backlog=None):
    return OwnerAnswers(owner_tg_id=OWNER_ID, store=FakeStore(link or waiting_link()),
                        backlog=backlog)


async def test_choosing_a_realization_deal_sets_path_a():
    answers = make_answers()

    await answers.on_choice(FakeCallback("amosync:596:41400001"))

    link = answers.store.links[596]
    assert link.path == "A" and link.real_lead_id == 41400001
    assert link.status == "new"                    # наблюдатель подхватит на ближайшем проходе
    assert link.question is None                   # вопрос снят


async def test_choosing_a_primary_lead_sets_path_b():
    answers = make_answers()

    await answers.on_choice(FakeCallback("amosync:596:41400002"))

    link = answers.store.links[596]
    assert link.path == "B" and link.primary_lead_id == 41400002


async def test_create_new_goes_the_from_scratch_way():
    answers = make_answers()

    await answers.on_choice(FakeCallback("amosync:596:new"))

    assert answers.store.links[596].path == "C"
    assert answers.store.links[596].status == "new"


async def test_owner_takes_it_over_and_robot_steps_aside():
    answers = make_answers()

    await answers.on_choice(FakeCallback("amosync:596:manual"))

    link = answers.store.links[596]
    assert link.status == "done" and link.path == "done"


async def test_retry_keeps_the_chosen_path():
    answers = make_answers(waiting_link(path="B"))

    await answers.on_choice(FakeCallback("amosync:596:retry"))

    link = answers.store.links[596]
    assert link.status == "new" and link.path == "B"


async def test_buttons_disappear_after_the_answer():
    """Карточка перестаёт быть кликабельной: дважды на один вопрос не ответишь."""
    answers = make_answers()
    callback = FakeCallback("amosync:596:new")

    await answers.on_choice(callback)

    assert callback.message.edits                  # текст переписан без кнопок
    assert "создам новую сделку" in callback.message.edits[0].lower()


async def test_stranger_cannot_answer_the_card():
    answers = make_answers()

    await answers.on_choice(FakeCallback("amosync:596:manual", user_id=STRANGER_ID))

    assert answers.store.updates == []
    assert answers.store.links[596].status == "waiting_owner"


async def test_junk_callback_is_ignored():
    answers = make_answers()

    await answers.on_choice(FakeCallback("что-то:не:то"))

    assert answers.store.updates == []


async def test_answer_for_a_forgotten_order_does_not_crash():
    answers = OwnerAnswers(owner_tg_id=OWNER_ID, store=FakeStore(None))

    await answers.on_choice(FakeCallback("amosync:596:new"))

    assert answers.store.updates == []


# --- кнопки хвоста ---

class FakeBacklog:
    def __init__(self):
        self.live_runs = 0

    async def run_live(self):
        self.live_runs += 1
        from adminbot.sync.backlog import PlannedOrder
        return [PlannedOrder(order_id=582, title="Заказ №582", status="done", path="A",
                             actions=(("update_lead", 1),))]


async def test_go_button_starts_the_live_run():
    backlog = FakeBacklog()
    answers = make_answers(backlog=backlog)
    callback = FakeCallback(BACKLOG_GO)

    await answers.on_backlog(callback)

    assert backlog.live_runs == 1
    assert any("582" in text for text in callback.message.edits)   # отчёт по проведённым


async def test_hold_button_changes_nothing():
    backlog = FakeBacklog()
    answers = make_answers(backlog=backlog)
    callback = FakeCallback(BACKLOG_HOLD)

    await answers.on_backlog(callback)

    assert backlog.live_runs == 0
    assert "отложил" in callback.message.edits[0].lower()


async def test_stranger_cannot_start_the_backlog():
    backlog = FakeBacklog()
    answers = make_answers(backlog=backlog)

    await answers.on_backlog(FakeCallback(BACKLOG_GO, user_id=STRANGER_ID))

    assert backlog.live_runs == 0
