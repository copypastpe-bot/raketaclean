"""Ручной стенд-скрипт для боевой АТС onlinePBX — инструмент стенда с владельцем.

НЕ запускать самостоятельно без владельца: `--test-call` реально звонит
живому человеку (менеджеру и указанному номеру). Это осознанное исключение
из правила «репетиция не оставляет следов» — здесь наоборот: тестовый
звонок должен быть настоящим, чтобы услышать отбивку и проверить
соединение, и делается это только на стенде, при владельце (Задача 7 плана
docs/plans/2026-08-31-autocall-implementation.md).

Настройки читаются из окружения (PBX_BASE_URL, PBX_API_KEY, PBX_MANAGER_DIAL —
например, из `~/.pbx.env` на VPS, файл не в git):

    sudo -u adminbot env $(grep -E "^PBX_" ~/.pbx.env | xargs) \\
        /opt/raketa-admin-bot/.venv/bin/python -m scripts.pbx_probe --auth-check

Команды:
  --auth-check              обмен ключа + история звонков за последний час
  --test-call НОМЕР         тестовый звонок: from=PBX_MANAGER_DIAL, to=НОМЕР
                            (номер — как ввели, без преобразований: формат
                            «to» в call/now.json не разведан, это и проверяем)
  --from НОМЕР              кому звонить первым вместо PBX_MANAGER_DIAL:
                            внутренний номер или мобильный менеджера. Нужно
                            потому, что правила «номер при недоступности»
                            (переадресация 100 → мобильные) на звонки через
                            API не распространяются — проверено на стенде
                            2026-09-02, 4 прогона: цепочка не поднялась ни
                            разу, звонило только приложение
  --outcome CALL_ID --called-at UNIX
                            разбор исхода уже отданного звонка

Телефоны в выводе — только замаскированные (последние 4 цифры), правило
проекта по ПД (`adminbot.phone.mask`).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from adminbot.autocall.pbx import OnlinePbx, PbxError, parse_search_id
from adminbot.phone import mask

HISTORY_WINDOW = timedelta(hours=1)


def _settings() -> tuple[str, str, str]:
    base_url = os.environ.get("PBX_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("PBX_API_KEY", "").strip()
    manager_dial = os.environ.get("PBX_MANAGER_DIAL", "").strip()
    if not base_url:
        raise SystemExit("PBX_BASE_URL не задан")
    if not api_key:
        raise SystemExit("PBX_API_KEY не задан")
    return base_url, api_key, manager_dial


def _masked_field(key: str, value: Any) -> Any:
    """Телефонные поля истории — только маскированные, остальные — как есть."""
    if key in ("caller_id_number", "destination_number") and value:
        return mask(str(value))
    return value


# --- команды ---

async def auth_check(pbx: OnlinePbx) -> int:
    """Обмен ключа + история за час — доказать, что доступ к АТС рабочий."""
    print("Проверяю аутентификацию (обмен ключа auth.json)…")
    # Обращение к _auth/_request напрямую: это диагностический инструмент,
    # ему нужен сырой доступ к транспорту, а не протокол Pbx (call_now/
    # call_outcome) — тот рассчитан на движок, а не на ручную проверку.
    await pbx._auth()
    print(f"Ключ получен, key_id={pbx._key_id} (ключ доступа не печатаю).")

    print("\nЧитаю историю звонков за последний час…")
    now = datetime.now(timezone.utc)
    since = now - HISTORY_WINDOW
    payload = await pbx._request(
        "/mongo_history/search.json",
        form={
            "start_stamp_from": str(int(since.timestamp())),
            "start_stamp_to": str(int(now.timestamp())),
            "per_page": "200",
        },
    )
    records = payload.get("data")
    records = records if isinstance(records, list) else []
    print(f"Записей за час: {len(records)}")
    if records:
        sample = records[0]
        print("Поля первой записи (телефоны маскированы):")
        for key in sorted(sample.keys()):
            print(f"  {key}: {_masked_field(key, sample[key])}")
    else:
        print("За последний час записей нет — это нормально, если звонков не было.")
    return 0


async def test_call(
    pbx: OnlinePbx,
    manager_dial: str,
    phone: str,
    *,
    gate_from: str = "",
    orig_number: str = "",
    orig_name: str = "",
) -> int:
    """Тестовый звонок: печатает сырой ответ call/now.json — формат не разведан.

    `gate_from`, `orig_number`, `orig_name` — параметры call/now.json из
    официальной спецификации (api2.onlinepbx.ru/documentation, HTTP API
    2.10.1): транк для первого номера и то, какой номер/имя увидит первый
    вызываемый. Проверяем ими замену голосовой отбивке: менеджер должен
    понять, что звонит робот, ЕЩЁ ДО того, как возьмёт трубку.
    """
    if not manager_dial:
        raise SystemExit("PBX_MANAGER_DIAL не задан — не знаю, с какого номера звонить")
    print(f"Запускаю тестовый звонок: менеджер({_dial_label(manager_dial)}) → {mask(phone)}…")
    body: dict[str, str] = {"from": manager_dial, "to": phone}
    if gate_from:
        body["gate_from"] = gate_from
    if orig_number:
        body["from_orig_number"] = orig_number
    if orig_name:
        body["from_orig_name"] = orig_name
    if len(body) > 2:
        extras = ", ".join(f"{k}={_dial_label(v)}" for k, v in body.items() if k not in ("from", "to"))
        print(f"Дополнительные параметры: {extras}")
    payload = await pbx._request("/call/now.json", json_body=body)
    print("Сырой ответ call/now.json (телефоны в тексте маскированы):")
    print(_masked_repr(payload, phone, manager_dial))
    return 0


def _dial_label(dial: str) -> str:
    """Внутренний номер печатаем как есть, мобильный — под маской.

    `mask("100")` вернул бы «…» и стенд перестал бы показывать, кому звонили;
    ПД в коротком внутреннем номере нет, а в мобильном менеджера — есть.
    """
    digits = "".join(ch for ch in dial if ch.isdigit())
    return mask(dial) if len(digits) >= 7 else dial


def _masked_repr(payload: Any, *phones: str) -> str:
    text = repr(payload)
    for phone in phones:
        if phone and phone in text:
            text = text.replace(phone, mask(phone))
    return text


async def outcome_check(pbx: OnlinePbx, call_id: str, called_at_unix: int) -> int:
    """Разбор исхода уже отданного звонка по его call_id."""
    called_at = datetime.fromtimestamp(called_at_unix, tz=timezone.utc)
    parsed = parse_search_id(call_id)
    if parsed is not None:
        phone, started_at = parsed
        print(f"call_id синтетический: телефон {mask(phone)}, "
              f"время старта {started_at.isoformat()}")
    else:
        print(f"call_id боевой (uuid из АТС): {call_id}")

    outcome = await pbx.call_outcome(call_id, called_at=called_at)
    if outcome is None:
        print("Исход: None — звонок ещё идёт либо истории по нему пока нет.")
    else:
        print(f"Исход: {outcome.value}")
    return 0


# --- аргументы и запуск ---

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ручной стенд-скрипт для боевой АТС onlinePBX. "
                    "Инструмент стенда с владельцем — не запускать самостоятельно.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--auth-check", action="store_true",
                       help="обмен ключа + история звонков за последний час")
    group.add_argument("--test-call", metavar="НОМЕР",
                       help="тестовый звонок: from=PBX_MANAGER_DIAL, to=НОМЕР")
    group.add_argument("--outcome", metavar="CALL_ID", help="разбор исхода по call_id")
    parser.add_argument("--from", dest="from_number", metavar="НОМЕР",
                        help="кому звонить первым вместо PBX_MANAGER_DIAL "
                             "(внутренний номер или мобильный менеджера)")
    parser.add_argument("--gate-from", metavar="ТРАНК", default="",
                        help="через какой внешний номер звонить первому")
    parser.add_argument("--orig-number", metavar="НОМЕР", default="",
                        help="какой номер увидит первый вызываемый")
    parser.add_argument("--orig-name", metavar="ИМЯ", default="",
                        help="какое имя увидит первый вызываемый")
    parser.add_argument("--called-at", type=int, metavar="UNIX",
                        help="unix-время команды АТС (обязательно вместе с --outcome)")
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    if args.outcome and args.called_at is None:
        raise SystemExit("--outcome требует --called-at UNIX")

    base_url, api_key, manager_dial = _settings()
    if args.from_number:
        manager_dial = args.from_number.strip()
    pbx = OnlinePbx(base_url=base_url, api_key=api_key)
    try:
        if args.auth_check:
            return await auth_check(pbx)
        if args.test_call:
            return await test_call(
                pbx, manager_dial, args.test_call,
                gate_from=args.gate_from,
                orig_number=args.orig_number,
                orig_name=args.orig_name,
            )
        return await outcome_check(pbx, args.outcome, args.called_at)
    except PbxError as exc:
        print(f"АТС ответила ошибкой: {exc}")
        return 1
    finally:
        await pbx.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
