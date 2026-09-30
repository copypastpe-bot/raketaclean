"""Ключ Wahelp не попадает в журнал веб-сервера (`notifications/webhook.py`).

Wahelp зовёт бот по адресу `/wahelp/webhook?token=<ключ>`, и журнал aiohttp
(`aiohttp.access`) писал этот адрес целиком — ключ открытым текстом в каждой
строке, ~390 строк в сутки. Решение владельца 2026-09-30: ключ не меняем,
в журнале прячем — `token=***`, остальная строка (адрес, код ответа) остаётся.
"""

import asyncio
import logging
import unittest

import aiohttp
from aiohttp.test_utils import unused_port

from notifications.webhook import WahelpWebhookServer, _MaskTokenFilter


class AccessLogMaskTest(unittest.IsolatedAsyncioTestCase):
    async def test_token_in_url_is_masked_in_access_log(self):
        server = WahelpWebhookServer(pool=None, token="RIGHTKEY")
        port = unused_port()
        await server.start("127.0.0.1", port)
        try:
            with self.assertLogs("aiohttp.access", level="INFO") as captured:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"http://127.0.0.1:{port}/wahelp/webhook?token=WRONGKEY",
                        json={},
                    ) as response:
                        self.assertEqual(response.status, 401)
                await asyncio.sleep(0.05)
        finally:
            await server.stop()

        line = "\n".join(captured.output)
        self.assertNotIn("WRONGKEY", line)
        self.assertIn("/wahelp/webhook?token=***", line)
        self.assertIn("401", line)

    def test_line_without_token_is_unchanged(self):
        record = logging.LogRecord(
            "aiohttp.access", logging.INFO, __file__, 0,
            '%s "%s" %s', ("127.0.0.1", "POST /wahelp/webhook HTTP/1.0", 200), None,
        )
        before = record.getMessage()

        self.assertTrue(_MaskTokenFilter().filter(record))
        self.assertEqual(record.getMessage(), before)


if __name__ == "__main__":
    unittest.main()
