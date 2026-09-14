"""Ключ служебного аккаунта: получение и обновление токена доступа.

Настоящий ключ в тестах не участвует — подпись подменяется двойником. Проверяем
то, что реально ломается в бою: лишние походы за токеном, работа с протухшим
токеном и понятность ошибки, когда доступ отозвали.
"""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from adminbot.gcal.auth import GCalKeyError, ServiceAccountToken

KEY_FILE = {
    "type": "service_account",
    "client_email": "robot@raketa.iam.gserviceaccount.com",
    "private_key_id": "abc123",
    "token_uri": "https://oauth2.googleapis.com/token",
}


class FakeSigner:
    """Подпись без криптографии: тесты проверяют обмен, а не RSA."""

    key_id = "abc123"

    def sign(self, message):
        return b"signature"


class FakeOAuth:
    def __init__(self):
        self.requests: list[dict] = []
        self.responses: list = []

    def stub(self, *responses):
        self.responses = list(responses)

    async def handle(self, request: web.Request):
        self.requests.append(dict(await request.post()))
        item = self.responses.pop(0) if self.responses else {"access_token": "ya29.x",
                                                             "expires_in": 3600}
        if isinstance(item, tuple):
            status, body = item
            return web.json_response(body, status=status)
        return web.json_response(item)


@pytest.fixture
async def oauth():
    fake = FakeOAuth()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield fake, str(server.make_url("/token"))
    finally:
        await server.close()


@pytest.fixture
async def tokens():
    """Фабрика токенов, которая закрывает за собой сетевые сессии."""
    made: list[ServiceAccountToken] = []

    def make(token_uri: str, clock: list[float]) -> ServiceAccountToken:
        token = ServiceAccountToken(
            email=KEY_FILE["client_email"], signer=FakeSigner(), token_uri=token_uri,
            now=lambda: clock[0])
        made.append(token)
        return token

    try:
        yield make
    finally:
        for token in made:
            await token.close()


async def test_token_is_requested_once_and_reused(oauth, tokens):
    """Токен живёт час — ходить за ним на каждый обмен незачем."""
    fake, uri = oauth
    clock = [1_000.0]
    fake.stub({"access_token": "ya29.first", "expires_in": 3600})
    token = tokens(uri, clock)

    assert await token() == "ya29.first"
    assert await token() == "ya29.first"
    assert len(fake.requests) == 1


async def test_expired_token_is_refreshed(oauth, tokens):
    fake, uri = oauth
    clock = [1_000.0]
    fake.stub({"access_token": "ya29.first", "expires_in": 3600},
              {"access_token": "ya29.second", "expires_in": 3600})
    token = tokens(uri, clock)

    assert await token() == "ya29.first"
    clock[0] += 3_600                                  # час прошёл
    assert await token() == "ya29.second"
    assert len(fake.requests) == 2


async def test_request_carries_a_signed_assertion(oauth, tokens):
    fake, uri = oauth
    fake.stub({"access_token": "ya29.x", "expires_in": 3600})
    token = tokens(uri, [1_000.0])

    await token()

    sent = fake.requests[0]
    assert sent["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert sent["assertion"].count(".") == 2           # заголовок.данные.подпись


async def test_revoked_key_gives_a_readable_error(oauth, tokens):
    """Владелец должен понять, что случилось, без чтения журнала Google."""
    fake, uri = oauth
    fake.stub((400, {"error": "invalid_grant",
                     "error_description": "Invalid JWT Signature."}))
    token = tokens(uri, [1_000.0])

    with pytest.raises(GCalKeyError) as failure:
        await token()

    text = str(failure.value).lower()
    assert "служебн" in text and "ключ" in text


def test_key_file_is_read(tmp_path):
    """Ключ читается из файла — путь задаётся настройкой, в git он не попадает."""
    path = tmp_path / "gcal.json"
    path.write_text(json.dumps({**KEY_FILE, "private_key": _TEST_KEY}))

    token = ServiceAccountToken.from_file(path)

    assert token.email == KEY_FILE["client_email"]
    assert token.token_uri == KEY_FILE["token_uri"]


def test_missing_key_file_says_what_to_do(tmp_path):
    with pytest.raises(GCalKeyError) as failure:
        ServiceAccountToken.from_file(tmp_path / "нет.json")

    assert "ключ" in str(failure.value).lower()


# Одноразовый ключ, сгенерированный для теста: боевой лежит только на сервере.
_TEST_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCdOOjuhVE/yZSO
1nu9nr/0v1lhX5Qnm2prfk5zzB/28aN+NYviZd+9tzxao8tywQLpEYZqFqEgwTJv
/ILm98R0DRvEy+sjUDEQo3I3y78rWoMG/1qk4uo6S7BgweZ76+0F6SwtPxxqIbDu
ayUL2TsrcKezixs33MwqN8kOniAfse8GWDqsKDzR+tpuKHwhPvacE2X/hHRgmwKw
ECjOLZvROZ/QbhIpKC0ux3a0Gt/5Ic77yrAc7pE4dhyPcGEsyKjg70raZdAEWAmL
MiklN3u/NCRQPwW7zhp6NAe48nMdBHHiQGmzLBpCkRgoGicXSdspPU4zmt5bPj99
3WXVmbIRAgMBAAECggEABC2Gdwwg4BaNtYv6hsvoourQx7cf+zt0hP26vPYAJXI/
W3O1z2LQorxzRJ9UoSyC1Mmr7jbCulmX6wTlP5j25fSRN9YJXgtb+mq1dskZv9+s
Wqu/b2E+QyVOvwt4AfP/fOg4zcz/Bsz1ZCiBuAJiQaHBOxKnuwDJhhesZpCKHv1z
PeTXZaw+M4XiEgpIKj+yJ6qXUme3fMxk3VSpp6Q8vYpLMGoboN3kwxtO9NyIaJGe
QbcklpsZLMFKSCeXqUX5u5DHfTs0/Y8yq59dPUPLP74k+vTmdy9dv2Xmjhn0iXhW
BYP9rT5HsOS6uwZ+uFnV3n75mE/tDLI5Q65GKCpI5wKBgQDbIGSqvDlAfn11DjQ3
z8adOfaJ9lBmvKwIruZMCPxDPdm5YvbdCACOSb8CDYAWOO6NhPKrQzzm29rnYRgn
5i/jBXuTFmnQU82xFxwdug1kU1dwyO7kaizyGVE1p8xh1gveRW86QlAPXbpoF8Vt
+sLN/ItWYhc5lNJpS9X0JTz4PwKBgQC3rckJW16mg0CZDnbrYP/AAmE3UsVg1wiH
04QfUQFeMcMEiC0KNZWhimW3uy+ovAG+EintOGhrGZRd6Hb5RvG08jw3p1003UYt
aLBEgk/eTnpTYDqIWmaWluL5dm4TtrWelfrU2sd45c5KYEQApkIThTZBIQm6Sua7
+FN3gRxBrwKBgHisFyP7MeA1iGt9Hf6aWrtdH0sMrWxWfLrvbn3y+NEi75LrUB2a
+YtiS2EbBC24vo6K54SvK4vLCXsgekgGuNphu5Ld5fnHHOBoZKBuRE+6oc3Hqd96
JTRSAun0dVZvpOuL+1vvBt3fdPc8GAqf7MW5TRaOQFIChflcvP+NvkzLAoGARed/
8W2yshCVzypwG9jIvNyq/xEjSV3NQ1Q+nmSH7r9lhx4EdjQ6hEZVu/0jgEY9K4di
KYQkSU5s5uiIDwrvBnyCanPpxyrHgJStMQWfO+4GJCElZatyC7HVJDfsYNhSes59
rfCtpddgEXJdrxmXYDJ8mYDnYN0Mv1EdAwhCJsUCgYB9iAA6ZP3oTKoImTMLY3dr
C1XSd/+twEMOTRQzxQ7gfGvv3U4s30sbB9BgFE1T5f5zdMammD/xSxhjcRMpBJcN
hnSkmPIjjZFlgDQQpBvoul4/B3r+PE8rW1SzsrYNGiiY3A63KGRFcGNMnhAQIc75
h3idXFPhhFz/KvLiAz6zbA==
-----END PRIVATE KEY-----"""
