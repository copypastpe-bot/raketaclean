"""Разбор недельного отчёта партнёра по коврам.

Проверяем на настоящих файлах: формат придумываем не мы, и «в целом похоже»
здесь недостаточно. Файлы содержат ФИО, телефоны и адреса клиентов, поэтому
в репозиторий не попадают — путь задаётся переменной CARPET_FIXTURES_DIR,
без неё тесты пропускаются (как тесты базы без TEST_DB_DSN).

    CARPET_FIXTURES_DIR=~/Projects/raketaclean/recon/data pytest tests/test_carpet_report.py
"""

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from adminbot.carpets.report import CarpetRow, parse_report, rows_to_process

FIXTURES = os.environ.get("CARPET_FIXTURES_DIR")
pytestmark = pytest.mark.skipif(not FIXTURES, reason="CARPET_FIXTURES_DIR не задан")


def rows_of(name: str) -> list[CarpetRow]:
    return parse_report((Path(FIXTURES).expanduser() / name).read_bytes())


def by_id(rows: list[CarpetRow], partner_id: int) -> CarpetRow:
    return next(row for row in rows if row.partner_id == partner_id)


# --- выполненные заказы ---

def test_completed_report_is_parsed():
    rows = rows_of("Договоры (11).xlsx")
    assert len(rows) == 4

    row = by_id(rows, 44426)
    assert row.phone10 == "9601945325"            # в файле лежит число 89601945325
    assert row.amount == Decimal("3995")          # «Взято денег у клиента»
    assert row.district == "Советский"
    assert row.address == "Ивлеева 18-101 п6 э1"
    assert row.payment_method == "Наличные"
    assert row.pickup_date == date(2026, 8, 16)   # «Факт забор» 16.08.2026 11:45
    assert row.return_date == date(2026, 8, 23)   # «Факт сдача» 23.08.2026 11:24
    assert row.added_date == date(2026, 8, 12)    # «Добавление» — по нему сверяем со сделкой
    assert row.is_refusal is False
    assert row.is_ours is True                    # «Рекламный источник» = РАКЕТА


def test_phone_written_with_country_code_is_normalized():
    """Партнёр пишет то 8, то 7 в начале — приводим к десяти цифрам."""
    row = by_id(rows_of("Договоры (10).xlsx"), 44345)

    assert row.phone10 == "9108970195"            # из числа 79108970195
    assert row.client_name.startswith("Урзумова")


def test_single_row_file_is_parsed():
    """Недельный отчёт бывает и на одну строку — это не повод падать."""
    rows = rows_of("Договоры (10).xlsx")

    assert len(rows) == 1
    assert rows[0].partner_id == 44345
    assert rows[0].amount == Decimal("1200")
    assert rows[0].payment_method == "Карта"


# --- отказы ---

def test_refusal_report_is_parsed():
    rows = rows_of("ракета забор отказ.xlsx")
    assert len(rows) == 4
    assert all(row.is_refusal for row in rows)

    row = by_id(rows, 43986)
    assert row.amount == Decimal(0)
    assert row.refusal_reason == "Не взяли трубку"
    assert row.pickup_date is None                # забора не было
    assert row.return_date is None


def test_empty_cells_do_not_become_dashes():
    """Партнёр ставит «-» вместо пустоты. В сделку такое писать нельзя."""
    rows = rows_of("ракета забор отказ.xlsx")

    assert all(row.payment_method != "-" for row in rows)
    assert all(row.comment != "-" for row in rows)


# --- отбор строк к обработке ---

def test_rows_to_process_splits_completed_and_refusals():
    done, refused = rows_to_process(rows_of("ракета забор отказ.xlsx"))
    assert done == [] and len(refused) == 4

    done, refused = rows_to_process(rows_of("Договоры (11).xlsx"))
    assert len(done) == 4 and refused == []


def test_foreign_rows_are_dropped():
    """В отчёт может попасть заказ не от нас — такие не трогаем вовсе."""
    ours = rows_of("Договоры (11).xlsx")[0]
    foreign = CarpetRow(**{**ours.__dict__, "partner_id": 1, "is_ours": False})

    done, refused = rows_to_process([ours, foreign])

    assert [row.partner_id for row in done] == [ours.partner_id]
    assert refused == []


def test_row_without_phone_is_not_silently_dropped():
    """Без телефона сделку не найти — такую строку показываем владельцу."""
    ours = rows_of("Договоры (11).xlsx")[0]
    faceless = CarpetRow(**{**ours.__dict__, "partner_id": 2, "phone10": None})

    done, _ = rows_to_process([ours, faceless])

    assert [row.partner_id for row in done] == [ours.partner_id, 2]
    assert faceless.needs_owner is True           # движок отправит карточку
    assert ours.needs_owner is False


# --- весь набор файлов разведки ---

def test_all_five_files_parse_to_fifty_one_orders():
    """Контрольная цифра разведки: 5 файлов, 51 уникальный заказ."""
    names = ["Договоры (10).xlsx", "Договоры (11).xlsx", "ракета июль.xlsx",
             "ракета-2.xlsx", "ракета забор отказ.xlsx"]
    all_rows = [row for name in names for row in rows_of(name)]

    assert len({row.partner_id for row in all_rows}) == 51
    assert all(row.partner_id > 0 for row in all_rows)
    assert all(row.phone10 for row in all_rows)   # телефон распознан в 51 из 51
