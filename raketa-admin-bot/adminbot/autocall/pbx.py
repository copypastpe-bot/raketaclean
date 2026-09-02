"""Протокол АТС для автозвонка: боевой клиент OnlinePbx и фейк для тестов.

Движок (Задача 8) знает лишь два метода протокола `Pbx`: запустить звонок
«менеджер → клиент» и спросить, чем он закончился. Как АТС это делает
внутри — не касается ни машины переходов (chain.py), ни хранилища
(store.py). `MemoryPbx` — фейк для тестов и репетиции («репетиция не
оставляет следов»); `OnlinePbx` — боевой клиент api2.onlinepbx.ru, факты о
её API разведаны вживую на боевой АТС (Задача 7, 2026-08-31) и уточнены на
стенде с владельцем 2026-09-02.

Голосовой отбивки в звонке НЕТ, хотя дизайн её предполагал: `call/now.json`
аудио не проигрывает, а модуль «Приветствие» ломает переадресацию —
официальный ответ поддержки onlinePBX. Менеджер понимает, что звонит робот,
по другому признаку: телефон зазвонил, а приложение АТС молчало (решение
владельца 2026-09-02). У настоящего входящего сначала звонит приложение и
только через 10 секунд включается переадресация на мобильный.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Protocol

import aiohttp

from adminbot.autocall.chain import Outcome
from adminbot.phone import mask

log = logging.getLogger(__name__)


class Pbx(Protocol):
    """Минимум, который движку нужен от телефонии."""

    async def call_now(self, *, to_dial: str, client_phone: str) -> str:
        """Запустить звонок «менеджеру, затем клиенту», вернуть id в АТС."""
        ...

    async def call_outcome(self, call_id: str, *, called_at: datetime) -> Optional[Outcome]:
        """Чем закончился звонок.

        None — звонок ещё идёт либо истории в АТС пока нет (движок спросит
        ещё раз позже); иначе — исход из adminbot.autocall.chain.Outcome.
        """
        ...


class MemoryPbx:
    """Фейковая АТС в памяти: для тестов и репетиции. В сеть не ходит никогда.

    Тот же принцип, что у MemoryAutocallStore: «репетиция не оставляет
    следов» — реальный звонок живому человеку украл бы у неё право быть
    репетицией. Поэтому call_now ничего не набирает, а просто запоминает
    вызов в self.calls и выдаёт предсказуемый id ("fake-1", "fake-2", …);
    исход звонка не выдумывается автоматически, а программируется заранее
    тестом — через конструктор (`outcomes`) или `set_outcome`.
    """

    def __init__(self, outcomes: Optional[dict[str, Outcome]] = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._outcomes: dict[str, Outcome] = dict(outcomes or {})
        self._next_id = 1

    async def call_now(self, *, to_dial: str, client_phone: str) -> str:
        call_id = f"fake-{self._next_id}"
        self._next_id += 1
        self.calls.append((to_dial, client_phone))
        return call_id

    async def call_outcome(self, call_id: str, *, called_at: datetime) -> Optional[Outcome]:
        # called_at не используется: фейку не нужно время, чтобы отдать
        # запрограммированный исход — оно нужно только боевому клиенту,
        # который ходит в реальную историю звонков АТС.
        return self._outcomes.get(call_id)

    def set_outcome(self, call_id: str, outcome: Outcome) -> None:
        """Запрограммировать исход звонка — можно и после call_now."""
        self._outcomes[call_id] = outcome


class PbxError(RuntimeError, OSError):
    """АТС ответила протокольной ошибкой (`status: "0"` в теле запроса).

    Наследник ОДНОВРЕМЕННО RuntimeError и OSError — не случайно. Движок
    (engine.py, `_TRANSIENT_ERRORS`) ловит `(AmoError, aiohttp.ClientError,
    asyncio.TimeoutError, OSError)` как сбои, после которых цепочку можно
    смело повторить следующим тиком, а не ронять процесс. Сделав PbxError
    подклассом OSError, ошибку АТС движок обрабатывает так же — без
    отдельной правки списка транзиентных ошибок в engine.py.
    """

    def __init__(self, comment: str) -> None:
        super().__init__(comment)
        self.comment = comment


#: Префикс синтетического id звонка, когда call/now.json не вернул свой.
_SEARCH_PREFIX = "search:"


class OnlinePbx:
    """Боевой клиент онлайн-АТС raketaclean (api2.onlinepbx.ru).

    Разведанные факты API (проверено вживую на боевой АТС 2026-08-31,
    см. docs/plans/2026-08-31-autocall-implementation.md, Задача 7):

    - Аутентификация — обмен ключа: `POST {base}/auth.json` form-data
      `auth_key=<ключ>&new=true` → `{"status":"1","data":{"key_id":…,"key":…}}`.
      Дальше каждый запрос несёт заголовок `x-pbx-authentication: key_id:key`
      (Bearer НЕ работает). Пара кэшируется в `_key_id`/`_key` и переживает
      много запросов; протухнув, даёт `status: "0"` с `isNotAuth` или
      `errorCode: "WRONG_AUTH_DATA"` — тогда `_request` обменивает ключ ещё
      раз и повторяет исходный запрос РОВНО один раз.
    - Ответы АТС ВСЕГДА приходят с HTTP 200: ошибка всегда в теле
      (`status: "0"` + `comment`), а не в коде ответа.
    - Используются только три пути: `auth.json`, `call/now.json`,
      `mongo_history/search.json`. Никаких user/get.json и прочего.

    Стиль жизненного цикла (`_ensure_session`/`close`/`__aenter__`) — как у
    AmoClient (adminbot/amo/client.py): один aiohttp.ClientSession на
    экземпляр, закрывается, только если этот клиент сам его создал.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        session: Optional[aiohttp.ClientSession] = None,
        timeout_sec: float = 15.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout_sec = timeout_sec
        # Профиль конструктора — как у AmoClient (тесты подменяют паузу без
        # реального ожидания). Сегодняшняя логика ретраит только протухший
        # ключ и без паузы; поле держим ради той же формы вызова и на случай
        # будущих ретраев по перегрузке АТС.
        self._sleep = sleep
        self._session = session
        self._owns_session = session is None
        self._key_id: Optional[str] = None
        self._key: Optional[str] = None

    # --- жизненный цикл (см. AmoClient._ensure_session/close/__aenter__) ---

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "OnlinePbx":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # --- транспорт ---

    async def _auth(self) -> None:
        """Обменять ключ API на пару key_id:key, которой подписан каждый запрос.

        `POST {base}/auth.json` form-data `auth_key=<ключ>&new=true`. Пара
        кэшируется в self._key_id/self._key до следующего протухания.
        """
        session = await self._ensure_session()
        async with session.post(
            f"{self.base_url}/auth.json",
            data={"auth_key": self.api_key, "new": "true"},
            timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
        ) as resp:
            payload = await resp.json(content_type=None)

        if not isinstance(payload, dict) or str(payload.get("status")) != "1":
            comment = payload.get("comment") if isinstance(payload, dict) else None
            raise PbxError(f"auth.json: {comment or 'не удалось получить ключ доступа'}")

        data = payload.get("data") or {}
        key_id, key = data.get("key_id"), data.get("key")
        if not key_id or not key:
            raise PbxError("auth.json: в ответе нет key_id/key")
        self._key_id, self._key = str(key_id), str(key)

    async def _request(
        self, path: str, *, json_body: Any = None, form: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """POST в АТС с заголовком аутентификации; при протухшем ключе — один повтор.

        Ответ АТС ВСЕГДА HTTP 200 — ошибка сидит в теле (`status: "0"` +
        `comment`), поэтому только протокольная ошибка АТС оборачивается в
        PbxError. Обрывы сети и таймауты (aiohttp.ClientError,
        asyncio.TimeoutError) наружу не заворачиваются: они и так входят в
        список транзиентных ошибок движка (engine.py, `_TRANSIENT_ERRORS`).
        """
        session = await self._ensure_session()
        if self._key_id is None or self._key is None:
            await self._auth()

        url = f"{self.base_url}{path}"
        for attempt in (1, 2):
            headers = {"x-pbx-authentication": f"{self._key_id}:{self._key}"}
            async with session.post(
                url, json=json_body, data=form, headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
            ) as resp:
                payload = await resp.json(content_type=None)

            if not isinstance(payload, dict):
                raise PbxError(f"{path}: неожиданный ответ АТС (не объект)")
            if str(payload.get("status")) == "1":
                return payload

            comment = str(payload.get("comment") or "АТС ответила ошибкой без пояснения")
            if attempt == 1 and _is_auth_expired(payload):
                log.warning("АТС: ключ доступа протух (%s) — переаутентификация", comment)
                await self._auth()
                continue
            raise PbxError(f"{path}: {comment}")

        raise PbxError(f"{path}: АТС снова отказала после переаутентификации")

    # --- протокол Pbx ---

    async def call_now(self, *, to_dial: str, client_phone: str) -> str:
        """Запустить звонок «менеджер(to_dial), затем клиент», вернуть id попытки.

        `POST call/now.json` JSON `{"from": to_dial, "to": "7"+client_phone}`
        (формат тела разведан на стенде: пустое тело даёт «body must be
        object»). В ответе приходит `{"status":"1","data":{"uuid": …}}` —
        так описано в спецификации АТС (HTTP API 2.10.1) и так было на
        стенде 2026-09-02.

        Но ответа может не быть вовсе: АТС держит его, пока менеджер не
        снимет трубку. Взял через 7 секунд — ответ пришёл; не взял — наш
        таймаут оборвал ожидание, а телефон звонил всю минуту (оба случая
        сняты на стенде). Поэтому таймаут здесь НЕ ошибка: звонок идёт,
        и попытке нужен id. В этом случае — как и когда uuid не пришёл —
        строим синтетический `search:{телефон}:{unix-время старта}`: по нему
        `call_outcome` найдёт попытку в истории через телефон и время.

        Обмен ключа делаем ДО команды звонка: если оборвётся он, значит
        звонок не ушёл, и такую ошибку глотать нельзя — движок повторит
        попытку следующим тиком.

        Синтетический id хранит телефон ЦЕЛИКОМ (без маски) — это не лог, а
        рабочие данные для последующего поиска; в логи телефон уходит только
        замаскированным (`adminbot.phone.mask`).
        """
        started_at = datetime.now(timezone.utc)
        if self._key_id is None or self._key is None:
            await self._auth()

        synthetic_id = f"{_SEARCH_PREFIX}{client_phone}:{int(started_at.timestamp())}"
        try:
            payload = await self._request(
                "/call/now.json", json_body={"from": to_dial, "to": "7" + client_phone},
            )
        except asyncio.TimeoutError:
            log.info(
                "АТС: звонок менеджер(%s)→клиент %s запущен, ответа нет — "
                "менеджер пока не снял трубку; исход ищем в истории",
                to_dial, mask(client_phone),
            )
            return synthetic_id

        call_id = _extract_call_id(payload.get("data"))
        if call_id is None:
            call_id = synthetic_id
            log.info(
                "АТС: звонок менеджер(%s)→клиент %s запущен, id не пришёл — "
                "использую синтетический (поиск по телефону/времени)",
                to_dial, mask(client_phone),
            )
        else:
            log.info(
                "АТС: звонок менеджер(%s)→клиент %s запущен, id=%s",
                to_dial, mask(client_phone), call_id,
            )
        return call_id

    async def call_outcome(self, call_id: str, *, called_at: datetime) -> Optional[Outcome]:
        """Чем закончился звонок — разбор истории АТС (`mongo_history/search.json`).

        Окно поиска — [called_at−60с; called_at+900с]: запас на задержку
        записи в истории АТС и на срабатывание таймаута движка (см.
        engine.CALL_OUTCOME_TIMEOUT_SEC = 300 с). call_id вида "search:…"
        (см. call_now) — ищем по телефону, разобранному из id; иначе — по
        боевому uuid, который вернула АТС. Сам разбор списка записей — в
        чистой `outcome_from_history` (тестируется без сети, см.
        tests/test_autocall_pbx.py).
        """
        parsed = parse_search_id(call_id)
        client_phone = parsed[0] if parsed is not None else None
        uuid = call_id if parsed is None else None

        start_from = int(called_at.timestamp()) - 60
        start_to = int(called_at.timestamp()) + 900
        payload = await self._request(
            "/mongo_history/search.json",
            form={
                "start_stamp_from": str(start_from),
                "start_stamp_to": str(start_to),
                "per_page": "200",
            },
        )
        records = payload.get("data")
        if not isinstance(records, list):
            records = []
        return outcome_from_history(records, client_phone=client_phone, uuid=uuid)


# --- разборная часть: чистые функции, без сети (тестируются напрямую) ---

def parse_search_id(call_id: str) -> Optional[tuple[str, datetime]]:
    """Разобрать синтетический id `search:{телефон}:{unix-время}` → (телефон, время).

    None — id не синтетический (боевой uuid из call/now.json) или повреждён;
    тогда вызывающий код ищет по call_id как по uuid и ожидаемо ничего не
    находит (история никогда не хранит наш собственный синтетический id).
    """
    if not call_id.startswith(_SEARCH_PREFIX):
        return None
    rest = call_id[len(_SEARCH_PREFIX):]
    phone, sep, ts_raw = rest.rpartition(":")
    if not sep or not phone or not ts_raw.isdigit():
        return None
    return phone, datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)


def outcome_from_history(
    records: Iterable[Mapping[str, Any]],
    *, client_phone: Optional[str] = None, uuid: Optional[str] = None,
) -> Optional[Outcome]:
    """Разобрать исход попытки по записям истории АТС (чистая функция, без сети).

    Выбор записи попытки: по `uuid` (боевой call_id) ЛИБО по совпадению
    последних 10 цифр `destination_number` с `client_phone` — номер в истории
    встречается в разных форматах (79…/89…/+79…), поэтому сравниваем только
    по хвосту.

    Правила откалиброваны на живых записях боевой АТС (стенд с владельцем
    2026-09-02, по одному прогону на исход):

    - Совпадений нет → None: истории по попытке пока нет, движок спросит
      позже. Если она не появится вовсе, движок сам досчитает секунды до
      CALL_OUTCOME_TIMEOUT_SEC и отдаст машине переходов Outcome.UNKNOWN.
    - `contacted: true` (или `user_talk_time > 0`) → CONNECTED. Главный
      признак — именно `contacted`: время разговора равно нулю в ДВУХ
      исходах из трёх, потому что АТС считает разговор только после того,
      как соединились обе стороны.
    - Звонок ещё не завершён (нет ни `end_stamp`, ни `hangup_cause`) → None.
    - Завершён без соединения, но событие «перевод» на телефон клиента есть
      → CLIENT_NO_ANSWER: менеджер трубку взял, до клиента дозванивались, он
      не подошёл (образец стенда: `NO_USER_RESPONSE`, 73 секунды).
    - Завершён без соединения и без события перевода → MANAGER_NO_ANSWER:
      менеджер не взял, АТС клиента даже не набирала (образец стенда:
      `NO_ANSWER`, 60 секунд, события пустые).

    Последняя ветка — смена поведения: раньше функция такого исхода не
    возвращала вовсе, и «менеджер не взял» приходил к машине переходов как
    CLIENT_NO_ANSWER — то есть виноватым оказывался клиент, а сделка после
    второго раза уезжала на этап «Не было первого контакта».
    """
    matched: list[Mapping[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if uuid is not None:
            if str(record.get("uuid") or "") == uuid:
                matched.append(record)
        elif client_phone is not None and _matches_phone(record, client_phone):
            matched.append(record)

    if not matched:
        return None

    record = matched[0]
    if _is_true(record.get("contacted")) or _as_int(record.get("user_talk_time")) > 0:
        return Outcome.CONNECTED
    if not (record.get("end_stamp") or record.get("hangup_cause")):
        return None
    if _client_was_dialled(record):
        return Outcome.CLIENT_NO_ANSWER
    return Outcome.MANAGER_NO_ANSWER


def _client_was_dialled(record: Mapping[str, Any]) -> bool:
    """Есть ли в событиях записи перевод на телефон клиента.

    Телефон клиента берём из самой записи (`destination_number`), а не из
    аргументов: так признак работает одинаково и когда попытку нашли по
    телефону, и когда по боевому uuid.
    """
    wanted = _last10_digits(str(record.get("destination_number") or ""))
    if len(wanted) != 10:
        return False
    events = record.get("events")
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "transfer":
            continue
        if _last10_digits(str(event.get("number") or "")) == wanted:
            return True
    return False


def _is_true(value: Any) -> bool:
    """`contacted` приходит булевым, но строку "true"/"1" тоже понимаем."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1")


def _matches_phone(record: Mapping[str, Any], client_phone: str) -> bool:
    """Хвост destination_number совпадает с телефоном клиента (10 цифр)."""
    wanted = _last10_digits(client_phone)
    got = _last10_digits(str(record.get("destination_number") or ""))
    return len(wanted) == 10 and got == wanted


def _last10_digits(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits[-10:]


def _extract_call_id(data: Any) -> Optional[str]:
    """id попытки из ответа call/now.json — формат ответа не разведан (см. call_now)."""
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, Mapping):
        return None
    for key in ("uuid", "id", "call_id"):
        value = data.get(key)
        if value:
            return str(value)
    return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_auth_expired(payload: Mapping[str, Any]) -> bool:
    """Протухший ключ: `isNotAuth` либо errorCode "WRONG_AUTH_DATA" (факты разведки)."""
    if payload.get("isNotAuth"):
        return True
    return str(payload.get("errorCode") or "") == "WRONG_AUTH_DATA"
