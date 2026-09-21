"""Почта владельца: что происходит с сообщением, когда Telegram молчит.

Проверяем ровно то, из-за чего почта появилась: 2026-09-02 отчёт о сделанной
работе не ушёл по таймауту и пропал навсегда — повторной попытки в роботе
не было вовсе.
"""

from datetime import datetime, timedelta, timezone

import pytest

from adminbot.tg.outbox import (
    BACKOFF_SEC, DEFAULT_TTL_SEC, MemoryMailStore, OwnerMail, Purpose,
)

NOW = datetime(2026, 9, 2, 12, 42, tzinfo=timezone.utc)
OWNER = 190933209


class FakeBot:
    """Telegram, который можно уронить по команде."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.parse_modes: list[object] = []
        self.fail = False
        self._next_id = 500

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        if self.fail:
            raise TimeoutError("Request timeout error")
        self._next_id += 1
        self.sent.append((chat_id, text, reply_markup))
        self.parse_modes.append(parse_mode)
        return type("Sent", (), {"message_id": self._next_id})()


class Clock:
    """Часы, которые двигает тест: ждать настоящие паузы незачем."""

    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def forward(self, seconds: int) -> None:
        self.moment += timedelta(seconds=seconds)


def build(bot: FakeBot, clock: Clock, **purposes) -> OwnerMail:
    return OwnerMail(bot=bot, chat_id=OWNER, store=MemoryMailStore(),
                     purposes=purposes, now=clock)


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def clock():
    return Clock()


async def test_message_goes_out_as_before_when_telegram_answers(bot, clock):
    """Связь есть — ведём себя как раньше: отправили и вернули номер сообщения."""
    mail = build(bot, clock)

    message_id = await mail.send("Сделка заведена", kind="gcal_done", ref="evt-1")

    assert message_id == 501
    assert bot.sent == [(OWNER, "Сделка заведена", None)]
    assert await mail.waiting() == 0


async def test_broken_connection_turns_the_message_into_a_debt(bot, clock):
    """Обрыв связи — сообщение не потеряно, а отложено. Отправитель узнаёт None.

    None здесь важен: вызывающий код по нему понимает, что отмечать
    «владельцу сказано» нельзя, иначе долг остался бы навсегда неоплаченным.
    """
    bot.fail = True
    mail = build(bot, clock)

    message_id = await mail.send("Сделка заведена", kind="gcal_done", ref="evt-1")

    assert message_id is None
    assert await mail.waiting() == 1


async def test_debt_is_delivered_when_the_line_is_back(bot, clock):
    """Связь вернулась — долг уходит владельцу, и отметку ставит сама почта.

    Отметка — то место, из-за которого сегодняшний отчёт пропал: её нельзя
    ставить до подтверждения Telegram, но и ставить её после доставки было
    некому.
    """
    marked: list[tuple[str, int]] = []
    mail = build(bot, clock, gcal_done=Purpose(
        on_delivered=lambda ref, message_id: _remember(marked, ref, message_id)))
    bot.fail = True
    await mail.send("Сделка заведена", kind="gcal_done", ref="evt-1")

    bot.fail = False
    clock.forward(BACKOFF_SEC[0])
    delivered = await mail.deliver_debts()

    assert delivered == 1
    assert bot.sent == [(OWNER, "Сделка заведена", None)]
    assert marked == [("evt-1", 501)]
    assert await mail.waiting() == 0


async def test_debt_waits_for_its_turn(bot, clock):
    """Раньше срока не досылаем: обрыв связи обычно длится минуты."""
    mail = build(bot, clock)
    bot.fail = True
    await mail.send("Сделка заведена", kind="gcal_done", ref="evt-1")
    bot.fail = False

    assert await mail.deliver_debts() == 0
    assert bot.sent == []


async def test_repeated_failure_backs_off(bot, clock):
    """Каждая неудача отодвигает следующую попытку — не стучимся в стену."""
    mail = build(bot, clock)
    bot.fail = True
    await mail.send("Сделка заведена", kind="gcal_done", ref="evt-1")

    clock.forward(BACKOFF_SEC[0])
    await mail.deliver_debts()
    letter = mail.store.letters[0]

    assert letter["attempts"] == 2
    assert letter["next_try_at"] == clock.moment + timedelta(seconds=BACKOFF_SEC[1])
    assert await mail.waiting() == 1


async def test_debt_that_is_no_longer_needed_is_dropped(bot, clock):
    """Отметка уже стоит — значит доставка удалась другим путём. Молчим.

    Иначе владелец получил бы вторую карточку с кнопками по одному вопросу.
    """
    mail = build(bot, clock, gcal_question=Purpose(
        still_needed=lambda ref: _answer(False)))
    bot.fail = True
    await mail.send("Заказ отменён — закрыть сделку?", kind="gcal_question", ref="evt-1")

    bot.fail = False
    clock.forward(BACKOFF_SEC[0])

    assert await mail.deliver_debts() == 0
    assert bot.sent == []
    assert mail.store.letters[0]["drop_reason"] == "нужда отпала"


async def test_stale_summary_is_not_delivered_next_morning(bot, clock):
    """Вечерняя сводка живёт часы: доставить её к обеду — смутить владельца."""
    mail = build(bot, clock, summary=Purpose(ttl_sec=6 * 3600))
    bot.fail = True
    await mail.send("Сводка за день", kind="summary")

    bot.fail = False
    clock.forward(7 * 3600)

    assert await mail.deliver_debts() == 0
    assert bot.sent == []
    assert mail.store.letters[0]["drop_reason"] == "протухло"


async def test_report_still_matters_a_day_later(bot, clock):
    """Отчёт о сделанной работе не устаревает так быстро — сутки его срок."""
    mail = build(bot, clock)
    bot.fail = True
    await mail.send("Сделка заведена", kind="gcal_done", ref="evt-1")

    bot.fail = False
    clock.forward(DEFAULT_TTL_SEC - 60)

    assert await mail.deliver_debts() == 1


async def test_debts_go_out_in_the_order_they_happened(bot, clock):
    """Владелец читает события по порядку, а не вперемешку."""
    mail = build(bot, clock)
    bot.fail = True
    await mail.send("Первое", kind="gcal_done", ref="evt-1")
    await mail.send("Второе", kind="gcal_done", ref="evt-2")

    bot.fail = False
    clock.forward(BACKOFF_SEC[0])
    await mail.deliver_debts()

    assert [text for _chat, text, _markup in bot.sent] == ["Первое", "Второе"]


async def test_buttons_survive_the_wait(bot, clock):
    """Карточка с кнопками должна дойти кнопками, а не голым текстом."""
    keyboard = {"inline_keyboard": [[{"text": "Закрыть", "callback_data": "gcal:close"}]]}
    mail = build(bot, clock)
    bot.fail = True
    await mail.send("Заказ отменён?", kind="gcal_question", ref="evt-1",
                    reply_markup=keyboard)

    bot.fail = False
    clock.forward(BACKOFF_SEC[0])
    await mail.deliver_debts()

    assert bot.sent[0][2] == keyboard


async def test_parse_mode_comes_from_the_purpose_not_the_call(bot, clock):
    """Задача 5 (ТЗ 2026-09-21-evening-summary-rework.md): у вечерней сводки
    появляются ссылки на сделки — их видно только с HTML-разметкой. Разметка
    решается по назначению (`Purpose`), а не по вызову `send`: так первая
    попытка и досылка после сбоя выглядят одинаково."""
    mail = build(bot, clock, summary=Purpose(parse_mode="HTML"))

    await mail.send("📊 Вечерняя сверка", kind="summary")

    assert bot.parse_modes == ["HTML"]


async def test_parse_mode_survives_a_retry_after_a_debt(bot, clock):
    """Сообщение не ушло с первой попытки и стало долгом — досылка должна нести
    ту же разметку, иначе владелец увидит сырые HTML-теги вместо ссылки."""
    mail = build(bot, clock, summary=Purpose(parse_mode="HTML"))
    bot.fail = True
    await mail.send("📊 Вечерняя сверка", kind="summary")

    bot.fail = False
    clock.forward(BACKOFF_SEC[0])
    await mail.deliver_debts()

    assert bot.parse_modes == ["HTML"]


async def test_other_kinds_keep_sending_without_any_parse_mode(bot, clock):
    """Остальные виды писем не задевает: разметка включена точечно, по одному
    назначению, а не для всей почты владельца."""
    mail = build(bot, clock, gcal_done=Purpose())

    await mail.send("Сделка заведена", kind="gcal_done", ref="evt-1")

    assert bot.parse_modes == [None]


async def test_a_broken_store_does_not_break_the_pass(bot, clock):
    """База тоже недоступна — сообщение потеряно, но работа с CRM продолжается."""
    mail = build(bot, clock)
    mail.store = _BrokenStore()
    bot.fail = True

    assert await mail.send("Сделка заведена", kind="gcal_done", ref="evt-1") is None


async def test_failed_mark_does_not_lose_the_message(bot, clock):
    """Отметку не поставили — сообщение всё равно считается доставленным.

    Владелец его уже прочитал; повторить отчёт хуже, чем промолчать, но
    молчание здесь было бы обманом: работа сделана, письмо ушло.
    """
    mail = build(bot, clock, gcal_done=Purpose(on_delivered=lambda ref, mid: _boom()))
    bot.fail = True
    await mail.send("Сделка заведена", kind="gcal_done", ref="evt-1")

    bot.fail = False
    clock.forward(BACKOFF_SEC[0])

    assert await mail.deliver_debts() == 1
    assert await mail.waiting() == 0


# --- вспомогательное ---

async def _remember(box: list, ref: str, message_id: int) -> None:
    box.append((ref, message_id))


async def _answer(value: bool) -> bool:
    return value


async def _boom() -> None:
    raise RuntimeError("хранилище недоступно")


class _BrokenStore(MemoryMailStore):
    async def add(self, **fields):
        raise RuntimeError("база недоступна")
