"""Протокол АТС, фейк для репетиции и боевой клиент OnlinePbx.

`MemoryPbx` существует ради принципа «репетиция не звонит и не оставляет
следов» (см. store.py) — она не должна дозваниваться до живых людей, но
обязана вести себя как настоящая АТС достаточно похоже, чтобы движок
(Задача 8) гонялся против неё в тестах.

`OnlinePbx` — боевой клиент api2.onlinepbx.ru (Задача 7). Разбор истории
звонков (`outcome_from_history`) и разбор синтетического id (`parse_search_id`)
— чистые функции без сети, поэтому проверяются напрямую на фикстурах-словарях
по образцу реальных полей истории (см. докстрину `pbx.py`). Транспорт
(аутентификация, ретрай протухшего ключа) проверяется против поддельного
сервера — тот же приём, что FakeAmo в test_amo_client.py.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from adminbot.autocall.chain import Outcome
from adminbot.autocall.pbx import (
    MemoryPbx,
    OnlinePbx,
    PbxError,
    contact_from_history,
    outcome_from_history,
    parse_search_id,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

# Вымышленные телефоны для фикстур истории (реальных ПД в тестах быть не должно).
CLIENT_PHONE = "9007001122"


async def test_call_now_does_not_touch_network_and_records_call():
    """call_now фейка не ходит в сеть — просто копит вызовы в self.calls."""
    pbx = MemoryPbx()

    call_id = await pbx.call_now(to_dial="9161234567", client_phone="9601861067")

    assert call_id == "fake-1"
    assert pbx.calls == [("9161234567", "9601861067")]


async def test_call_now_ids_grow_with_each_call():
    """Каждый следующий звонок получает свой предсказуемый id."""
    pbx = MemoryPbx()

    first = await pbx.call_now(to_dial="9161234567", client_phone="9601861067")
    second = await pbx.call_now(to_dial="9161234567", client_phone="9601861068")

    assert first == "fake-1"
    assert second == "fake-2"
    assert pbx.calls == [
        ("9161234567", "9601861067"),
        ("9161234567", "9601861068"),
    ]


async def test_call_outcome_is_none_until_programmed():
    """Пока исход не запрограммирован — звонок «ещё идёт», как в жизни."""
    pbx = MemoryPbx()
    call_id = await pbx.call_now(to_dial="9161234567", client_phone="9601861067")

    assert await pbx.call_outcome(call_id, called_at=NOW) is None


async def test_call_outcome_returns_programmed_outcome_via_constructor():
    """Исходы можно задать заранее через конструктор — для целого сценария теста."""
    pbx = MemoryPbx(outcomes={"fake-1": Outcome.CONNECTED})
    call_id = await pbx.call_now(to_dial="9161234567", client_phone="9601861067")

    assert await pbx.call_outcome(call_id, called_at=NOW) is Outcome.CONNECTED


async def test_call_outcome_returns_outcome_set_after_the_call():
    """Исход можно запрограммировать и после call_now — методом set_outcome."""
    pbx = MemoryPbx()
    call_id = await pbx.call_now(to_dial="9161234567", client_phone="9601861067")

    pbx.set_outcome(call_id, Outcome.CLIENT_NO_ANSWER)

    assert await pbx.call_outcome(call_id, called_at=NOW) is Outcome.CLIENT_NO_ANSWER


async def test_call_outcome_for_unknown_call_id_is_none():
    """Незнакомый id звонка (опечатка, чужой прогон) — тоже None, а не ошибка."""
    pbx = MemoryPbx()

    assert await pbx.call_outcome("no-such-call", called_at=NOW) is None


# --- outcome_from_history: чистый разбор истории АТС, без сети ---
#
# Поля фикстур — как в реальных записях истории (см. докстрину pbx.py, факты
# разведки). Телефон вымышленный: 9007001122.

def test_outcome_from_history_connected_when_talk_time_positive():
    """Была разговорная длительность — значит, соединились."""
    records = [{
        "uuid": "u1", "destination_number": "79007001122",
        "user_talk_time": 35, "duration": 40, "end_stamp": 1700000100,
        "hangup_cause": "NORMAL_CLEARING",
    }]
    assert outcome_from_history(records, client_phone=CLIENT_PHONE) is Outcome.CONNECTED


# Тест «завершился без разговора → клиент не взял» отсюда убран намеренно:
# он описывал прежнее правило, которое стенд 2026-09-02 опроверг. Такая
# запись (звонок закончен, событий нет) означает, что трубку не взял
# МЕНЕДЖЕР. Оба исхода теперь проверяются на образцах стенда ниже.


# --- Образцы стенда 2026-09-02: робот звонит менеджеру на мобильный ---
#
# Три записи истории, снятые с боевой АТС в один день, по одной на исход.
# Схема звонка сменилась: раньше робот поднимал внутренний номер менеджера
# и рассчитывал на переадресацию «не ответил за 10 секунд — звони на
# мобильный», но правила переадресации к звонкам через API не применяются
# (проверено: 4 прогона, цепочка не поднялась ни разу). Теперь АТС набирает
# мобильный менеджера сразу, и поля истории стали другими: время разговора
# больше не ноль при успехе, а «менеджер не взял» и «клиент не взял»
# различаются наличием события перевода на телефон клиента.

MANAGER_MOBILE = "79001112233"   # вымышленный «рабочий телефон менеджера»


def _stand_record(**overrides):
    """Запись истории по образцу стенда; overrides меняют нужные поля."""
    record = {
        "uuid": "u1",
        "caller_id_number": MANAGER_MOBILE,
        "destination_number": "79007001122",
        "duration": 60,
        "user_talk_time": 0,
        "hangup_cause": "NO_ANSWER",
        "contacted": False,
        "end_stamp": 1700000100,
        "events": [],
    }
    record.update(overrides)
    return record


def test_outcome_from_history_connected_by_contacted_flag():
    """Прогон А стенда: разговор состоялся — 131 секунда, contacted=true."""
    records = [_stand_record(
        duration=150, user_talk_time=131, hangup_cause="NORMAL_CLEARING",
        contacted=True,
        events=[{"type": "transfer", "timestamp": 1700000060, "number": "79007001122"}],
    )]
    assert outcome_from_history(records, client_phone=CLIENT_PHONE) is Outcome.CONNECTED


def test_outcome_from_history_connected_even_if_talk_time_field_missing():
    """Признак успеха — contacted, а не время разговора.

    Время разговора равно нулю в двух исходах из трёх (стенд 2026-09-02),
    поэтому опираться на него нельзя. Если АТС однажды не отдаст поле
    вовсе, `contacted: true` всё равно означает «поговорили».
    """
    records = [_stand_record(
        duration=150, hangup_cause="NORMAL_CLEARING", contacted=True,
        events=[{"type": "transfer", "timestamp": 1700000060, "number": "79007001122"}],
    )]
    records[0].pop("user_talk_time")
    assert outcome_from_history(records, client_phone=CLIENT_PHONE) is Outcome.CONNECTED


def test_outcome_from_history_manager_no_answer_when_client_was_never_dialled():
    """Прогон Б стенда: менеджер не взял — АТС до клиента не дошла.

    Признак — пустой список событий: перевода на телефон клиента не было.
    Раньше эта функция такого исхода не возвращала вовсе, и движок ждал
    пять минут таймаута, чтобы отдать UNKNOWN. Теперь ответ известен сразу.
    """
    records = [_stand_record()]
    assert (
        outcome_from_history(records, client_phone=CLIENT_PHONE)
        is Outcome.MANAGER_NO_ANSWER
    )


def test_outcome_from_history_client_no_answer_when_transfer_happened():
    """Прогон В стенда: менеджер взял, клиент не подошёл.

    Время разговора здесь тоже ноль — АТС считает разговор только после
    того, как соединились ОБЕ стороны. Отличает исход событие перевода
    на телефон клиента: оно есть, значит до клиента дозванивались.
    """
    records = [_stand_record(
        duration=73, hangup_cause="NO_USER_RESPONSE",
        events=[{"type": "transfer", "timestamp": 1700000060, "number": "79007001122"}],
    )]
    assert (
        outcome_from_history(records, client_phone=CLIENT_PHONE)
        is Outcome.CLIENT_NO_ANSWER
    )


def test_outcome_from_history_none_while_stand_call_not_finished_yet():
    """Звонок ещё идёт: ни end_stamp, ни hangup_cause — исхода пока нет."""
    records = [_stand_record(end_stamp=None, hangup_cause=None)]
    assert outcome_from_history(records, client_phone=CLIENT_PHONE) is None


def test_outcome_from_history_none_for_empty_history():
    """Истории пока нет — движок спросит ещё раз позже."""
    assert outcome_from_history([], client_phone=CLIENT_PHONE) is None


def test_outcome_from_history_none_while_call_still_in_progress():
    """Запись есть, но звонок ещё не завершился (нет end_stamp/hangup_cause)."""
    records = [{
        "uuid": "u1", "destination_number": "+79007001122",
        "user_talk_time": 0, "duration": 0,
    }]
    assert outcome_from_history(records, client_phone=CLIENT_PHONE) is None


def test_outcome_from_history_none_when_no_record_matches_client():
    """Записи по этой попытке в истории пока нет — исхода тоже нет.

    Движок спросит на следующем тике, а если история так и не появится —
    сам досчитает таймаут и отдаст машине переходов Outcome.UNKNOWN.
    """
    records = [{"destination_number": "79001110000", "user_talk_time": 50}]
    assert outcome_from_history(records, client_phone=CLIENT_PHONE) is None


@pytest.mark.parametrize("raw_number", ["79007001122", "89007001122", "+79007001122"])
def test_outcome_from_history_matches_phone_tail_regardless_of_prefix(raw_number):
    """destination_number встречается в разных форматах — сравниваем по хвосту в 10 цифр."""
    records = [{"destination_number": raw_number, "user_talk_time": 10}]
    assert outcome_from_history(records, client_phone=CLIENT_PHONE) is Outcome.CONNECTED


def test_outcome_from_history_selects_by_uuid_when_given():
    """uuid боевого call_id — точный ключ поиска, телефон при этом не смотрим."""
    records = [
        {"uuid": "other", "destination_number": CLIENT_PHONE,
         "user_talk_time": 0, "end_stamp": 1},
        {"uuid": "wanted", "destination_number": "70000000000", "user_talk_time": 12},
    ]
    assert outcome_from_history(records, uuid="wanted") is Outcome.CONNECTED


def test_outcome_from_history_ignores_non_mapping_records():
    """Мусор в списке (не словарь) не должен ронять разбор."""
    records = [None, "junk", {"destination_number": CLIENT_PHONE, "user_talk_time": 5}]
    assert outcome_from_history(records, client_phone=CLIENT_PHONE) is Outcome.CONNECTED


# --- parse_search_id: разбор синтетического call_id, без сети ---

def test_parse_search_id_extracts_phone_and_unix_time():
    parsed = parse_search_id(f"search:{CLIENT_PHONE}:1700000000")

    assert parsed == (CLIENT_PHONE, datetime.fromtimestamp(1700000000, tz=timezone.utc))


def test_parse_search_id_returns_none_for_real_uuid():
    """Боевой call_id — не наш синтетический формат, разбирать нечего."""
    assert parse_search_id("f47ac10b-58cc-4372-a567-0e02b2c3d479") is None


def test_parse_search_id_returns_none_for_missing_timestamp():
    assert parse_search_id(f"search:{CLIENT_PHONE}") is None


def test_parse_search_id_returns_none_for_non_numeric_timestamp():
    assert parse_search_id(f"search:{CLIENT_PHONE}:not-a-number") is None


def test_parse_search_id_returns_none_for_empty_phone():
    assert parse_search_id("search::1700000000") is None


def test_pbx_error_is_also_os_error_for_engine_transient_retry():
    """engine._TRANSIENT_ERRORS ловит OSError — PbxError обязана туда попадать,

    иначе сбой АТС не уведёт цепочку в статус "error" на повтор, а уронит
    движок целиком (см. докстрину PbxError в pbx.py).
    """
    assert issubclass(PbxError, OSError)
    assert issubclass(PbxError, RuntimeError)


# --- OnlinePbx: транспорт против поддельного сервера (образец — FakeAmo) ---

class FakePbx:
    """Поддельная АТС: отдаёт заготовленные ответы и помнит запросы."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.responses: dict[str, list] = {}
        self.delays: dict[str, float] = {}
        self.auth_calls = 0

    def stub(self, path: str, *responses):
        self.responses[path] = list(responses)

    def delay(self, path: str, seconds: float):
        """Тянуть с ответом — так АТС ведёт себя, пока менеджер не взял трубку."""
        self.delays[path] = seconds

    def _next(self, path):
        queue = self.responses.get(path) or []
        if not queue:
            return {"status": "0", "comment": "нет заготовленного ответа в тесте"}
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def handle(self, request: web.Request):
        if request.path == "/auth.json":
            self.auth_calls += 1
        pause = self.delays.get(request.path)
        if pause:
            await asyncio.sleep(pause)
        if request.content_type == "application/json":
            body = await request.json()
        else:
            body = dict(await request.post())
        self.requests.append((request.path, body))
        return web.json_response(self._next(request.path))


@pytest.fixture
async def pbx_server():
    fake = FakePbx()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    client = OnlinePbx(base_url=str(server.make_url("")).rstrip("/"), api_key="test-key")
    try:
        yield client, fake
    finally:
        await client.close()
        await server.close()


def _body_for(fake: FakePbx, path: str) -> dict:
    return next(body for req_path, body in fake.requests if req_path == path)


async def test_call_now_returns_uuid_from_call_response(pbx_server):
    """АТС вернула uuid — используем его как call_id, "to" = 7+телефон."""
    client, fake = pbx_server
    fake.stub("/auth.json", {"status": "1", "data": {"key_id": "k1", "key": "s1"}})
    fake.stub("/call/now.json", {"status": "1", "data": {"uuid": "abc-123"}})

    call_id = await client.call_now(to_dial="100", client_phone=CLIENT_PHONE)

    assert call_id == "abc-123"
    assert _body_for(fake, "/call/now.json") == {"from": "100", "to": "7" + CLIENT_PHONE}


async def test_call_now_returns_synthetic_id_when_response_has_no_id(pbx_server):
    """Ответ без uuid/id — строим свой id для поиска по телефону/времени."""
    client, fake = pbx_server
    fake.stub("/auth.json", {"status": "1", "data": {"key_id": "k1", "key": "s1"}})
    fake.stub("/call/now.json", {"status": "1", "data": {}})

    call_id = await client.call_now(to_dial="100", client_phone=CLIENT_PHONE)

    assert call_id.startswith(f"search:{CLIENT_PHONE}:")
    parsed = parse_search_id(call_id)
    assert parsed is not None and parsed[0] == CLIENT_PHONE


async def test_call_now_returns_id_when_manager_does_not_pick_up(pbx_server):
    """Молчание АТС в ответ на команду звонка — не сбой, а «менеджер не взял».

    Стенд 2026-09-02: АТС держит ответ до тех пор, пока первый вызываемый не
    снимет трубку. Взял через 7 секунд — ответ пришёл; не взял — ответа нет
    вовсе, а телефон звонил всю минуту. Значит, по таймауту падать нельзя:
    звонок идёт, и попытку нужно вернуть с id для поиска в истории.
    """
    client, fake = pbx_server
    fake.stub("/auth.json", {"status": "1", "data": {"key_id": "k1", "key": "s1"}})
    fake.stub("/call/now.json", {"status": "1", "data": {"uuid": "не-дождёмся"}})
    fake.delay("/call/now.json", 0.3)
    impatient = OnlinePbx(base_url=client.base_url, api_key="test-key", timeout_sec=0.05)

    try:
        call_id = await impatient.call_now(to_dial="89001112233", client_phone=CLIENT_PHONE)
    finally:
        await impatient.close()

    assert call_id.startswith(f"search:{CLIENT_PHONE}:")


async def test_request_reauthenticates_once_on_expired_key_and_retries(pbx_server):
    """Протухший ключ (isNotAuth) — один обмен ключа заново и повтор запроса."""
    client, fake = pbx_server
    fake.stub("/auth.json",
             {"status": "1", "data": {"key_id": "k1", "key": "s1"}},
             {"status": "1", "data": {"key_id": "k2", "key": "s2"}})
    fake.stub("/call/now.json",
             {"status": "0", "comment": "not authorized", "isNotAuth": True},
             {"status": "1", "data": {"uuid": "abc-123"}})

    call_id = await client.call_now(to_dial="100", client_phone=CLIENT_PHONE)

    assert call_id == "abc-123"
    assert fake.auth_calls == 2


async def test_request_raises_pbx_error_on_status_zero_without_auth_issue(pbx_server):
    """Ошибка АТС не про протухший ключ — переспрашивать бессмысленно, PbxError сразу."""
    client, fake = pbx_server
    fake.stub("/auth.json", {"status": "1", "data": {"key_id": "k1", "key": "s1"}})
    fake.stub("/call/now.json", {"status": "0", "comment": "body must be object"})

    with pytest.raises(PbxError, match="body must be object"):
        await client.call_now(to_dial="100", client_phone=CLIENT_PHONE)

    assert fake.auth_calls == 1                # ключ не протух — переаутентификации не было


async def test_call_outcome_searches_window_around_called_at(pbx_server):
    """Окно поиска строится от called_at: -60с / +900с, per_page=200."""
    client, fake = pbx_server
    fake.stub("/auth.json", {"status": "1", "data": {"key_id": "k1", "key": "s1"}})
    fake.stub("/mongo_history/search.json", {
        "status": "1",
        "data": [{"destination_number": "7" + CLIENT_PHONE, "user_talk_time": 42,
                  "end_stamp": 1700000100}],
    })
    called_at = datetime.fromtimestamp(1700000000, tz=timezone.utc)

    outcome = await client.call_outcome(f"search:{CLIENT_PHONE}:1700000000",
                                        called_at=called_at)

    assert outcome is Outcome.CONNECTED
    body = _body_for(fake, "/mongo_history/search.json")
    assert body["start_stamp_from"] == str(1700000000 - 60)
    assert body["start_stamp_to"] == str(1700000000 + 900)
    assert body["per_page"] == "200"


async def test_call_outcome_by_uuid_call_id(pbx_server):
    """Боевой call_id (не "search:…") ищется в истории по uuid."""
    client, fake = pbx_server
    fake.stub("/auth.json", {"status": "1", "data": {"key_id": "k1", "key": "s1"}})
    fake.stub("/mongo_history/search.json", {
        "status": "1",
        # Запись по образцу стенда: менеджер взял (есть перевод на телефон
        # клиента), клиент не подошёл — разговора нет.
        "data": [{"uuid": "abc-123", "destination_number": "70000000000",
                  "user_talk_time": 0, "end_stamp": 1700000100,
                  "hangup_cause": "NO_USER_RESPONSE", "contacted": False,
                  "events": [{"type": "transfer", "number": "70000000000"}]}],
    })

    outcome = await client.call_outcome("abc-123", called_at=NOW)

    assert outcome is Outcome.CLIENT_NO_ANSWER


# --- contact_since: был ли звонок с клиентом после попытки робота (ТЗ 2026-10-23, задача 3) ---
#
# Образцы — по полям из разведки 23.10 (окна 22.10 09:59–10:15 и 20.10 15:16–15:40
# МСК), телефоны вымышленные. Одна попытка робота — одна запись: destination_number
# — клиент, caller_id_number — набранный телефон менеджера, start_stamp — unix-время
# строкой, совпадает с временем попытки до секунд. accountcode у всех записей
# "inbound" — по нему направление не определить. Входящий от клиента: caller — клиент,
# destination — номер компании, события — переводы на внутренние номера.

RECHECK_CLIENT = "9007001122"
MANAGER_WORK = "89001112233"        # рабочий телефон менеджера (PBX_MANAGER_DIAL, первый)
MANAGER_PERSONAL = "89004445566"    # личный (второй)
COMPANY_NUMBER = "74950001122"      # номер компании, с которого робот звонил клиенту
RECHECK_DIALS = (MANAGER_WORK, MANAGER_PERSONAL)
# Попытка робота 20.10 15:23:49 МСК (клиент не взял).
LAST_CALLED_AT = datetime(2026, 10, 20, 12, 23, 49, 400000, tzinfo=timezone.utc)
LAST_TS = int(LAST_CALLED_AT.timestamp())


def _robot_record(**overrides):
    """Запись попытки робота: менеджер взял, клиент не подошёл."""
    record = {
        "uuid": "robot-uuid-1",
        "accountcode": "inbound",
        "caller_id_number": "7" + MANAGER_WORK[1:],
        "caller_id_name": "7" + MANAGER_WORK[1:],
        "destination_number": "7" + RECHECK_CLIENT,
        "start_stamp": str(LAST_TS),
        "end_stamp": str(LAST_TS + 73),
        "contacted": False,
        "user_talk_time": 0,
        "hangup_cause": "NO_USER_RESPONSE",
        "events": [{"type": "transfer", "number": "7" + RECHECK_CLIENT}],
    }
    record.update(overrides)
    return record


def _client_incoming(start_ts, **overrides):
    """Входящий от клиента, пропущенный (образец 20.10 15:24:55)."""
    record = {
        "uuid": "incoming-uuid-1",
        "accountcode": "inbound",
        "caller_id_number": "8" + RECHECK_CLIENT,
        "caller_id_name": "8" + RECHECK_CLIENT,
        "destination_number": COMPANY_NUMBER,
        "start_stamp": str(start_ts),
        "end_stamp": str(start_ts + 40),
        "contacted": False,
        "user_talk_time": 0,
        "hangup_cause": "NORMAL_CLEARING",
        "events": [
            {"type": "transfer", "number": "5100"},
            {"type": "transfer", "number": "5000"},
            {"type": "transfer", "number": "100"},
            {"type": "user", "number": "100"},
        ],
    }
    record.update(overrides)
    return record


def _unrelated(start_ts):
    """Чужой звонок в том же окне: номер клиента не встречается ни в одном поле."""
    return {"uuid": "other", "accountcode": "inbound", "caller_id_number": "79161230000",
            "destination_number": COMPANY_NUMBER, "start_stamp": str(start_ts),
            "contacted": True, "user_talk_time": 50}


def _contact(records, *, dials=RECHECK_DIALS, since=LAST_CALLED_AT, phone=RECHECK_CLIENT):
    return contact_from_history(records, client_phone=phone, since=since,
                                manager_phones=dials)


def _at(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def test_contact_robot_own_attempt_is_not_a_contact():
    """Своя попытка робота — не звонок с клиентом: повтор идёт, как раньше."""
    assert _contact([_robot_record()]) is None


def test_contact_robot_own_attempt_from_personal_phone_is_not_a_contact():
    """После неудачи менеджера робот звонит на личный телефон — тоже своя запись."""
    record = _robot_record(caller_id_number="7" + MANAGER_PERSONAL[1:],
                           caller_id_name="7" + MANAGER_PERSONAL[1:])
    assert _contact([record]) is None


def test_contact_robot_own_attempt_a_few_seconds_late_is_still_own():
    """start_stamp совпадает с попыткой до секунд — запись в пределах 60 с своя."""
    assert _contact([_robot_record(start_stamp=str(LAST_TS + 60))]) is None


def test_contact_missed_incoming_from_client_counts():
    """Решение владельца 5: любой звонок клиента, даже пропущенный, отменяет повтор.
    Образец 20.10: клиент перезвонил через минуту после попытки робота и не дозвонился."""
    incoming_ts = LAST_TS + 66
    records = [_unrelated(LAST_TS + 10), _robot_record(), _client_incoming(incoming_ts)]

    assert _contact(records) == _at(incoming_ts)


def test_contact_outgoing_from_office_through_pbx_counts():
    """Звонок по номеру клиента через АТС из офиса — тоже отменяет повтор."""
    office_ts = LAST_TS + 200
    record = _robot_record(uuid="office", caller_id_number="101", caller_id_name="Офис",
                           start_stamp=str(office_ts), contacted=True, user_talk_time=95,
                           hangup_cause="NORMAL_CLEARING", events=[])

    assert _contact([_robot_record(), record]) == _at(office_ts)


def test_contact_call_from_manager_phone_after_60_seconds_counts():
    """Звонок с номера менеджера через АТС позже 60 с — уже не попытка робота."""
    later_ts = LAST_TS + 61
    record = _robot_record(uuid="manager-later", start_stamp=str(later_ts))

    assert _contact([_robot_record(), record]) == _at(later_ts)


def test_contact_returns_the_earliest_call():
    records = [_client_incoming(LAST_TS + 400), _robot_record(),
               _client_incoming(LAST_TS + 90), _client_incoming(LAST_TS + 250)]

    assert _contact(records) == _at(LAST_TS + 90)


def test_contact_none_when_only_unrelated_records():
    """В окне бывают записи, где номер клиента не встречается, — к делу не относятся."""
    assert _contact([_unrelated(LAST_TS + 5), _unrelated(LAST_TS + 300)]) is None


def test_contact_none_for_empty_history():
    assert _contact([]) is None


@pytest.mark.parametrize("raw_number", ["79007001122", "89007001122", "+7 (900) 700-11-22"])
def test_contact_matches_client_phone_tail_regardless_of_format(raw_number):
    """Номера в истории в разных форматах — сравниваем по последним 10 цифрам."""
    incoming = _client_incoming(LAST_TS + 120, caller_id_number=raw_number)
    assert _contact([incoming]) == _at(LAST_TS + 120)


def test_contact_manager_phone_matched_by_tail_too():
    """Телефон менеджера в настройке и в истории — в разных форматах."""
    record = _robot_record(caller_id_number="+7 900 111-22-33")
    assert _contact([record]) is None


def test_contact_accepts_numeric_start_stamp():
    incoming = _client_incoming(LAST_TS + 120)
    incoming["start_stamp"] = LAST_TS + 120
    assert _contact([incoming]) == _at(LAST_TS + 120)


def test_contact_ignores_calls_before_last_attempt():
    """Звонок до попытки робота повтор не отменяет: робот звонил уже после него."""
    assert _contact([_client_incoming(LAST_TS - 30)]) is None


def test_contact_skips_record_without_start_stamp():
    """Без времени звонок нельзя ни сравнить с попыткой, ни назвать менеджеру."""
    incoming = _client_incoming(LAST_TS + 120)
    del incoming["start_stamp"]
    broken = _client_incoming(LAST_TS + 130, start_stamp="не число")
    assert _contact([incoming, broken]) is None


def test_contact_ignores_non_mapping_records():
    assert _contact([None, "junk", _client_incoming(LAST_TS + 70)]) == _at(LAST_TS + 70)


def test_contact_none_without_client_phone():
    assert _contact([_client_incoming(LAST_TS + 70)], phone="") is None


async def test_memory_pbx_contact_since_is_none_until_programmed():
    """Фейк в сеть не ходит; ответ задаёт тест, запросы запоминаются."""
    pbx = MemoryPbx()
    until = _at(LAST_TS + 300)

    assert await pbx.contact_since(RECHECK_CLIENT, since=LAST_CALLED_AT, until=until,
                                   manager_phones=RECHECK_DIALS) is None

    pbx.set_contact(_at(LAST_TS + 66))
    assert await pbx.contact_since(RECHECK_CLIENT, since=LAST_CALLED_AT, until=until,
                                   manager_phones=RECHECK_DIALS) == _at(LAST_TS + 66)
    assert pbx.contact_queries == [
        (RECHECK_CLIENT, LAST_CALLED_AT, until, RECHECK_DIALS),
        (RECHECK_CLIENT, LAST_CALLED_AT, until, RECHECK_DIALS),
    ]


async def test_online_contact_since_searches_from_last_attempt_to_now(pbx_server):
    """Окно запроса — [время прошлой попытки; сейчас], per_page=200; разбор — тот же."""
    client, fake = pbx_server
    fake.stub("/auth.json", {"status": "1", "data": {"key_id": "k1", "key": "s1"}})
    fake.stub("/mongo_history/search.json", {
        "status": "1",
        "data": [_robot_record(), _client_incoming(LAST_TS + 66)],
    })
    until = _at(LAST_TS + 600)

    contact_at = await client.contact_since(RECHECK_CLIENT, since=LAST_CALLED_AT,
                                            until=until, manager_phones=RECHECK_DIALS)

    assert contact_at == _at(LAST_TS + 66)
    body = _body_for(fake, "/mongo_history/search.json")
    assert body["start_stamp_from"] == str(LAST_TS)
    assert body["start_stamp_to"] == str(LAST_TS + 600)
    assert body["per_page"] == "200"


async def test_online_contact_since_raises_pbx_error_on_status_zero(pbx_server):
    """Ошибку АТС проверка не глотает — решает движок (recheck_failed и звонок)."""
    client, fake = pbx_server
    fake.stub("/auth.json", {"status": "1", "data": {"key_id": "k1", "key": "s1"}})
    fake.stub("/mongo_history/search.json", {"status": "0", "comment": "history unavailable"})

    with pytest.raises(PbxError, match="history unavailable"):
        await client.contact_since(RECHECK_CLIENT, since=LAST_CALLED_AT,
                                   until=_at(LAST_TS + 600), manager_phones=RECHECK_DIALS)
