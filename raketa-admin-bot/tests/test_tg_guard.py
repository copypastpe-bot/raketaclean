"""Доступ к боту: он личный.

Бот управляет боевой CRM компании, поэтому право отдавать ему команды есть
ровно у одного человека — владельца. Все остальные получают вежливый отказ
и не могут ни узнать состояние дел, ни что-либо переключить.
"""

from adminbot.control import MemoryControlPanel
from adminbot.tg.bot import OWNER_ONLY_REPLY, OwnerCommands, OwnerOnly, build_router

OWNER_ID = 42
STRANGER_ID = 777


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeMessage:
    """Сообщение в Telegram: от кого пришло и что бот на него ответил."""

    def __init__(self, user_id=OWNER_ID, text="/status"):
        self.from_user = FakeUser(user_id)
        self.text = text
        self.replies = []

    async def answer(self, text, **kwargs):
        self.replies.append(text)
        return FakeSent(len(self.replies))


class FakeSent:
    def __init__(self, message_id):
        self.message_id = message_id


def make_commands(**overrides):
    params = dict(owner_tg_id=OWNER_ID, control=MemoryControlPanel(),
                  sync_enabled=True, dry_run=True)
    params.update(overrides)
    return OwnerCommands(**params)


async def test_filter_passes_owner_only():
    guard = OwnerOnly(OWNER_ID)

    assert await guard(FakeMessage(OWNER_ID)) is True
    assert await guard(FakeMessage(STRANGER_ID)) is False


async def test_message_without_sender_is_rejected():
    """Сообщение из канала или от анонима отправителя не имеет — доступа тоже нет."""
    message = FakeMessage(OWNER_ID)
    message.from_user = None

    assert await OwnerOnly(OWNER_ID)(message) is False


async def test_stranger_gets_refusal_and_changes_nothing():
    control = MemoryControlPanel()
    commands = make_commands(control=control)
    message = FakeMessage(STRANGER_ID, "/pause")

    await commands.stranger(message)

    assert message.replies == [OWNER_ONLY_REPLY]
    assert await control.is_paused() is False        # чужая команда ничего не переключила


async def test_owner_writing_plain_text_gets_a_hint_not_a_refusal():
    """Владелец написал «привет» — это не повод отвечать ему «бот не ваш»."""
    commands = make_commands()
    message = FakeMessage(OWNER_ID, "Привет")

    await commands.unknown(message)

    assert OWNER_ONLY_REPLY not in message.replies[0]
    assert "/status" in message.replies[0]            # подсказываем, что умеем


def test_router_registers_owner_commands():
    """Роутер собирается и знает все команды владельца."""
    router = build_router(make_commands())

    assert router.message.handlers                    # обработчики зарегистрированы
    # шесть команд, подсказка владельцу на прочие сообщения и отказ для чужих
    assert len(router.message.handlers) == 8
    assert router.callback_query.handlers == []       # карточки не подключены — кнопок нет


def test_router_wires_card_buttons_when_answers_are_given():
    from adminbot.control import MemoryControlPanel
    from adminbot.tg.bot import OwnerAnswers

    answers = OwnerAnswers(owner_tg_id=OWNER_ID, store=None)
    router = build_router(make_commands(control=MemoryControlPanel()), answers)

    assert len(router.callback_query.handlers) == 2    # выбор сделки и кнопки хвоста
