"""Ответы владельца на карточки: выбор сделки и запуск хвоста.

Нажатие кнопки ничего не делает в amoCRM само по себе — оно только записывает
решение владельца рядом с заказом. Дальше обычным ходом сработает наблюдатель.
Так ответ не теряется, даже если робота перезапустят через секунду.
"""

from adminbot.amo import ids
from adminbot.carpets.store import MemoryCarpetStore
from adminbot.models import AmoLink
from adminbot.tg.bot import AddressAnswers, BACKLOG_GO, BACKLOG_HOLD, CarpetAnswers, OwnerAnswers
from adminbot.tg.cards import ADDR_PREFIX
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
        self.actions = []

    async def get(self, order_id):
        return self.links.get(order_id)

    async def update(self, order_id, **fields):
        self.updates.append(fields)
        link = self.links.get(order_id)
        if link is None:
            return None
        self.links[order_id] = AmoLink(**{**link.__dict__, **fields})
        return self.links[order_id]

    async def log(self, order_id, action, *, dry_run, entity=None, amo_id=None, payload=None):
        self.actions.append({"order_id": order_id, "action": action, "dry_run": dry_run,
                             "entity": entity, "amo_id": amo_id, "payload": payload})

    async def update_and_log(self, order_id, *, action, dry_run, entity=None, amo_id=None,
                             payload=None, **fields):
        updated = await self.update(order_id, **fields)
        if updated is not None:
            await self.log(order_id, action, dry_run=dry_run, entity=entity,
                           amo_id=amo_id, payload=payload)
        return updated


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


# --- решения владельца пишутся в журнал (задача 8, ТЗ 2026-09-21) ---

async def test_owner_takes_it_over_is_logged_as_manual():
    """«Сам разберусь» пишется в журнал так, чтобы его можно было отличить и посчитать."""
    answers = make_answers()

    await answers.on_choice(FakeCallback("amosync:596:manual"))

    assert answers.store.actions == [
        {"order_id": 596, "action": "answer_owner", "dry_run": False,
         "entity": None, "amo_id": None, "payload": {"choice": "manual"}}]


async def test_create_new_is_logged_but_not_as_manual():
    """«Заводи новую» тоже попадает в журнал — но с другим значением choice."""
    answers = make_answers()

    await answers.on_choice(FakeCallback("amosync:596:new"))

    assert answers.store.actions == [
        {"order_id": 596, "action": "answer_owner", "dry_run": False,
         "entity": None, "amo_id": None, "payload": {"choice": "new"}}]


async def test_retry_is_logged():
    answers = make_answers(waiting_link(path="B"))

    await answers.on_choice(FakeCallback("amosync:596:retry"))

    assert answers.store.actions == [
        {"order_id": 596, "action": "answer_owner", "dry_run": False,
         "entity": None, "amo_id": None, "payload": {"choice": "retry"}}]


async def test_picking_a_deal_logs_its_lead_id():
    answers = make_answers()

    await answers.on_choice(FakeCallback("amosync:596:41400001"))

    assert answers.store.actions == [
        {"order_id": 596, "action": "answer_owner", "dry_run": False,
         "entity": "lead", "amo_id": 41400001, "payload": {"choice": "lead"}}]


async def test_stranger_writes_nothing_to_the_journal():
    answers = make_answers()

    await answers.on_choice(FakeCallback("amosync:596:manual", user_id=STRANGER_ID))

    assert answers.store.actions == []


async def test_answer_for_a_forgotten_order_writes_nothing_to_the_journal():
    answers = OwnerAnswers(owner_tg_id=OWNER_ID, store=FakeStore(None))

    await answers.on_choice(FakeCallback("amosync:596:manual"))

    assert answers.store.actions == []


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


# --- карточка «сделка без адреса» (ТЗ 2026-09-16, задача 7) ---

def address_link(order_id=596, real_lead_id=41400001, primary_lead_id=None):
    return AmoLink(order_id=order_id, phone10="9601861067", status="done", path="C",
                   real_lead_id=real_lead_id, primary_lead_id=primary_lead_id)


class FakeAddressAmo:
    """amoCRM-двойник: помнит, что у неё просили, и умеет «упасть»."""

    def __init__(self, address=None, fail=False):
        self.address = address
        self.fail = fail
        self.calls = []

    async def get_lead(self, lead_id):
        self.calls.append(lead_id)
        if self.fail:
            raise RuntimeError("амо недоступна")
        if self.address is None:
            return {"id": lead_id}
        return {"id": lead_id, "custom_fields_values": [
            {"field_id": ids.FIELD_ADDRESS, "values": [{"value": self.address}]}]}


def make_address_answers(link=None, amo=None):
    store = FakeStore(link or address_link())
    return AddressAnswers(owner_tg_id=OWNER_ID, store=store, amo=amo or FakeAddressAmo()), store


async def test_filled_button_does_not_take_the_owner_at_their_word():
    """«Я заполнил» не верит на слово: робот идёт и читает сделку заново."""
    amo = FakeAddressAmo(address="ул. Мира, 10")
    answers, store = make_address_answers(amo=amo)

    await answers.on_choice(FakeCallback(f"{ADDR_PREFIX}:596:filled"))

    assert amo.calls == [41400001]
    link = store.links[596]
    assert link.deal_address == "ул. Мира, 10"


async def test_filled_button_keeps_reminding_when_amo_is_still_empty():
    answers, store = make_address_answers(amo=FakeAddressAmo(address=None))
    callback = FakeCallback(f"{ADDR_PREFIX}:596:filled")

    await answers.on_choice(callback)

    link = store.links[596]
    assert link.deal_address is None
    assert link.address_reminder_muted is False         # напоминания не остановлены
    assert callback.message.edits == [
        "Заказ №596: в сделке по-прежнему пусто — буду напоминать дальше."]


async def test_mute_button_stops_reminders_without_asking_amo():
    amo = FakeAddressAmo()
    answers, store = make_address_answers(amo=amo)

    await answers.on_choice(FakeCallback(f"{ADDR_PREFIX}:596:mute"))

    assert store.links[596].address_reminder_muted is True
    assert amo.calls == []                               # «не напоминать» в амо не ходит


async def test_filled_button_survives_amo_failure():
    """Амо недоступна — карточка остаётся с кнопками, можно нажать ещё раз."""
    answers, store = make_address_answers(amo=FakeAddressAmo(fail=True))
    callback = FakeCallback(f"{ADDR_PREFIX}:596:filled")

    await answers.on_choice(callback)

    assert store.links[596].deal_address is None
    assert callback.message.edits == []
    assert callback.answers and "не ответила" in callback.answers[-1]


async def test_unknown_order_replies_politely():
    answers = AddressAnswers(owner_tg_id=OWNER_ID, store=FakeStore(None), amo=FakeAddressAmo())

    callback = FakeCallback(f"{ADDR_PREFIX}:999:filled")
    await answers.on_choice(callback)

    assert callback.answers == ["Этой работы у меня уже нет."]


async def test_stranger_cannot_mute_reminders():
    amo = FakeAddressAmo()
    answers, store = make_address_answers(amo=amo)

    await answers.on_choice(FakeCallback(f"{ADDR_PREFIX}:596:mute", user_id=STRANGER_ID))

    assert store.links[596].address_reminder_muted is False
    assert amo.calls == []


# --- карточка ковров: решения владельца тоже пишутся в журнал (задача 8) ---

async def make_carpet_answers():
    store = MemoryCarpetStore()
    await store.create(44426, "9601945325")
    return CarpetAnswers(owner_tg_id=OWNER_ID, store=store), store


async def test_carpet_owner_takes_it_over_is_logged_as_manual():
    answers, store = await make_carpet_answers()

    await answers.on_choice(FakeCallback("carpet:44426:manual"))

    assert store.actions_of("answer_owner") == [
        {"partner_id": 44426, "action": "answer_owner", "dry_run": False,
         "entity": None, "amo_id": None, "payload": {"choice": "manual"}}]


async def test_carpet_create_new_is_logged_but_not_as_manual():
    answers, store = await make_carpet_answers()

    await answers.on_choice(FakeCallback("carpet:44426:new"))

    assert store.actions_of("answer_owner") == [
        {"partner_id": 44426, "action": "answer_owner", "dry_run": False,
         "entity": None, "amo_id": None, "payload": {"choice": "new"}}]


async def test_carpet_picking_a_deal_logs_its_lead_id():
    answers, store = await make_carpet_answers()

    await answers.on_choice(FakeCallback("carpet:44426:31516051"))

    assert store.actions_of("answer_owner") == [
        {"partner_id": 44426, "action": "answer_owner", "dry_run": False,
         "entity": "lead", "amo_id": 31516051, "payload": {"choice": "lead"}}]


async def test_carpet_stranger_writes_nothing_to_the_journal():
    answers, store = await make_carpet_answers()

    await answers.on_choice(FakeCallback("carpet:44426:manual", user_id=STRANGER_ID))

    assert store.actions == []
