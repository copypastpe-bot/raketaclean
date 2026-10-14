"""Сообщение в чат денег клининга уходит обычным текстом (cleaning/notify.py).

Комментарий админа вставляется в текст как есть. С разметкой HTML знаки
`<`, `>`, `&` в нём ломали разбор: Telegram отвечал «can't parse entities»,
а `send_cleaning_money_flow` только писала предупреждение в журнал — запись
об операции в чат молча не приходила. Разметки в этих сообщениях нет, поэтому
они уходят обычным текстом (решение владельца 14.10).
"""

import unittest
from unittest import mock

from cleaning import notify


class CleaningMoneyFlowPlainTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_with_html_chars_goes_as_plain_text(self):
        bot = mock.Mock()
        bot.send_message = mock.AsyncMock()
        text = "💸 Расход клининга: 500₽\nКомментарий: долг <500 & «скидка» >0"

        with mock.patch.object(notify, "CLEANING_MONEY_FLOW_CHAT_ID", -100777):
            await notify.send_cleaning_money_flow(bot, text)

        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.await_args
        self.assertEqual(args, (-100777, text))
        self.assertIn("parse_mode", kwargs)
        self.assertIsNone(kwargs["parse_mode"])

    async def test_no_chat_no_send(self):
        bot = mock.Mock()
        bot.send_message = mock.AsyncMock()

        with mock.patch.object(notify, "CLEANING_MONEY_FLOW_CHAT_ID", 0):
            await notify.send_cleaning_money_flow(bot, "текст")

        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
