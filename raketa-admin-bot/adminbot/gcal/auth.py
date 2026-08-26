"""Ключ служебного аккаунта Google: подпись и токен доступа.

Почему служебный аккаунт, а не вход владельца: такой ключ не протухает и не
требует, чтобы владелец раз в несколько месяцев заново подтверждал доступ в
браузере. Робот на сервере должен работать сам, а не ждать человека.

Как это устроено: робот подписывает своим ключом короткую записку («я такой-то,
хочу читать календарь, действительно час»), Google меняет её на токен доступа.
Токен живёт час и кэшируется — ходить за ним на каждый обмен незачем.

Ключ-файл лежит только на сервере (chmod 600) и в git не попадает никогда.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp
from google.auth import jwt
from google.auth.crypt import RSASigner

log = logging.getLogger(__name__)

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# Токен обновляем чуть раньше срока: обмен, начатый в последнюю минуту жизни,
# успел бы протухнуть на середине.
REFRESH_MARGIN_SEC = 120


class GCalKeyError(RuntimeError):
    """С ключом служебного аккаунта что-то не так — нужен человек."""


class ServiceAccountToken:
    """Токен доступа к календарю. Вызывается как функция: `await token()`."""

    def __init__(
        self,
        *,
        email: str,
        signer: Any,
        token_uri: str = DEFAULT_TOKEN_URI,
        scope: str = CALENDAR_SCOPE,
        session: Optional[aiohttp.ClientSession] = None,
        now: Callable[[], float] = time.time,
        lifetime_sec: int = 3600,
        timeout_sec: float = 15.0,
    ) -> None:
        self.email = email
        self.token_uri = token_uri
        self.scope = scope
        self._signer = signer
        self._now = now
        self._lifetime_sec = lifetime_sec
        self._timeout_sec = timeout_sec
        self._session = session
        self._owns_session = session is None
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> "ServiceAccountToken":
        """Прочитать ключ-файл, скачанный из Google Cloud."""
        path = Path(path).expanduser()
        if not path.exists():
            raise GCalKeyError(
                f"Файл ключа служебного аккаунта не найден: {path}. "
                "Скачайте ключ в Google Cloud и положите его на сервер (chmod 600), "
                "путь задаётся настройкой GCAL_SERVICE_ACCOUNT_FILE.")

        try:
            info = json.loads(path.read_text())
        except ValueError as exc:
            raise GCalKeyError(f"Файл ключа {path} — не JSON: {exc}") from exc

        missing = [name for name in ("client_email", "private_key") if not info.get(name)]
        if missing:
            raise GCalKeyError(
                f"В файле ключа {path} нет полей: {', '.join(missing)}. "
                "Нужен ключ служебного аккаунта, а не идентификатор приложения.")

        try:
            signer = RSASigner.from_service_account_info(info)
        except Exception as exc:                        # noqa: BLE001 — причин много, ответ один
            raise GCalKeyError(f"Ключ в файле {path} прочитать не удалось: {exc}") from exc

        return cls(email=info["client_email"], signer=signer,
                   token_uri=info.get("token_uri") or DEFAULT_TOKEN_URI, **kwargs)

    async def __call__(self) -> str:
        """Действующий токен доступа: из кэша или новый."""
        if self._token and self._now() < self._expires_at:
            return self._token
        return await self._refresh()

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    # --- внутреннее ---

    async def _refresh(self) -> str:
        session = await self._ensure_session()
        assertion = self._assertion()

        try:
            async with session.post(
                self.token_uri,
                data={"grant_type": JWT_GRANT, "assertion": assertion},
                timeout=aiohttp.ClientTimeout(total=self._timeout_sec),
            ) as resp:
                payload = await resp.json(content_type=None)
                if resp.status >= 400 or not isinstance(payload, dict):
                    raise GCalKeyError(
                        "Google отказался выдать доступ по служебному ключу "
                        f"({resp.status}): {payload}. Проверьте, что ключ не отозван "
                        "и Calendar API включён в проекте Google Cloud.")
        except aiohttp.ClientError as exc:
            raise GCalKeyError(f"Не удалось получить токен доступа Google: {exc}") from exc

        token = payload.get("access_token")
        if not token:
            raise GCalKeyError(f"Google не вернул токен доступа: {payload}")

        lifetime = int(payload.get("expires_in") or self._lifetime_sec)
        self._token = str(token)
        self._expires_at = self._now() + max(lifetime - REFRESH_MARGIN_SEC, 60)
        log.info("Получен токен доступа к календарю на %s секунд", lifetime)
        return self._token

    def _assertion(self) -> str:
        issued = int(self._now())
        payload = {
            "iss": self.email,
            "scope": self.scope,
            "aud": self.token_uri,
            "iat": issued,
            "exp": issued + self._lifetime_sec,
        }
        assertion = jwt.encode(self._signer, payload)
        return assertion.decode() if isinstance(assertion, bytes) else assertion

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session
