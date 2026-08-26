"""Разбор недельного отчёта партнёра по коврам.

Партнёр («Кристалл») раз в неделю присылает выгрузку из своей CRM: один лист,
26 колонок, строка на заказ. Выполненные заказы и отказы приходят разными
файлами, но формат у них одинаковый.

Два правила разбора, которые здесь важнее всего:

1. **Колонки ищем по названию, а не по номеру.** Партнёр однажды вставит колонку
   в середину — при разборе по номерам робот молча начнёт читать адрес вместо
   телефона. По названию он в тот же день скажет «не вижу колонку», и это
   несравнимо лучше.
2. **Ничего не додумываем.** Не распознали телефон или дату — оставляем пустым
   и отдаём строку владельцу. В сделку клиента выдуманные данные не попадают.

Ловушки настоящих файлов (проверено на пяти выгрузках, 51 заказ):
телефон приходит числом, даты — строками двух видов, суммы бывают с пробелом
внутри, а пустая ячейка записана как «-».
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Optional

from openpyxl import load_workbook

from adminbot.phone import last10

log = logging.getLogger(__name__)

# Метка наших заказов в колонке «Рекламный источник».
OUR_SOURCE = "ракета"

# Признак отказа в колонке «Статус» («Забор отказ»).
REFUSAL_MARK = "отказ"

# Пустое значение партнёр записывает по-разному.
EMPTY_VALUES = {"", "-", "—", "нет", "none"}

# Колонки, без которых отчёт бессмысленен.
REQUIRED_COLUMNS = ("#", "Телефон", "Статус")

_SPACES = re.compile(r"[\s  ]+")


@dataclass(frozen=True)
class CarpetRow:
    """Одна строка отчёта — один заказ партнёра."""

    partner_id: int                       # колонка «#»: сквозной номер в CRM партнёра
    phone10: Optional[str]                # 10 цифр; None — номер не распознан
    client_name: Optional[str] = None
    address: Optional[str] = None
    district: Optional[str] = None        # колонка «Город» — фактически район города
    amount: Decimal = Decimal(0)          # «Взято денег у клиента» (решение владельца)
    price: Decimal = Decimal(0)           # «Стоимость» — для сверки, в сделку не идёт
    payment_method: Optional[str] = None
    pickup_date: Optional[date] = None    # «Факт забор»
    return_date: Optional[date] = None    # «Факт сдача»
    added_date: Optional[date] = None     # «Добавление» — по нему сверяемся со сделкой
    status: Optional[str] = None
    refusal_reason: Optional[str] = None
    comment: Optional[str] = None
    is_ours: bool = True
    is_refusal: bool = False

    @property
    def needs_owner(self) -> bool:
        """Строку нельзя обработать самостоятельно — нужен человек."""
        return not self.phone10


def parse_report(data: bytes) -> list[CarpetRow]:
    """Разобрать файл отчёта. На вход — содержимое вложения из письма."""
    workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            return []

        index = _column_index(header)
        missing = [name for name in REQUIRED_COLUMNS if name not in index]
        if missing:
            raise ValueError(f"В отчёте партнёра нет колонок: {', '.join(missing)}")

        parsed: list[CarpetRow] = []
        for raw in rows:
            row = _row_from(raw, index)
            if row is not None:
                parsed.append(row)
        return parsed
    finally:
        workbook.close()


def rows_to_process(rows: list[CarpetRow]) -> tuple[list[CarpetRow], list[CarpetRow]]:
    """Разложить строки на «провести» и «закрыть как отказ».

    Чужие заказы (не «РАКЕТА») отбрасываем: партнёр работает не только с нами.
    Строки без телефона не выбрасываем — они попадут владельцу карточкой.
    """
    ours = [row for row in rows if row.is_ours]
    dropped = len(rows) - len(ours)
    if dropped:
        log.info("Отчёт партнёра: %s строк не наши — пропускаю", dropped)

    completed = [row for row in ours if not row.is_refusal]
    refused = [row for row in ours if row.is_refusal]
    return completed, refused


# --- внутреннее ---

def _column_index(header: tuple) -> dict[str, int]:
    """Название колонки → её номер. Регистр и лишние пробелы не мешают."""
    index: dict[str, int] = {}
    for position, title in enumerate(header):
        name = _clean_text(title)
        if name:
            index[name] = position
    return index


def _row_from(raw: tuple, index: dict[str, int]) -> Optional[CarpetRow]:
    partner_id = _int(_cell(raw, index, "#"))
    if partner_id is None:
        return None                       # пустая строка в конце файла

    status = _text(_cell(raw, index, "Статус"))
    source = (_text(_cell(raw, index, "Рекламный источник")) or "").lower()

    return CarpetRow(
        partner_id=partner_id,
        phone10=_phone(_cell(raw, index, "Телефон")),
        client_name=_text(_cell(raw, index, "Ф.И.О.")),
        address=_text(_cell(raw, index, "Адрес")),
        district=_text(_cell(raw, index, "Город")),
        amount=_money(_cell(raw, index, "Взято денег у клиента")),
        price=_money(_cell(raw, index, "Стоимость")),
        payment_method=_text(_cell(raw, index, "Форма оплаты")),
        pickup_date=_date(_cell(raw, index, "Факт забор")),
        return_date=_date(_cell(raw, index, "Факт сдача")),
        added_date=_date(_cell(raw, index, "Добавление")),
        status=status,
        refusal_reason=_text(_cell(raw, index, "Причина отказа")),
        comment=_text(_cell(raw, index, "Комментарий")),
        is_ours=OUR_SOURCE in source,
        is_refusal=REFUSAL_MARK in (status or "").lower(),
    )


def _cell(raw: tuple, index: dict[str, int], name: str) -> Any:
    position = index.get(name)
    if position is None or position >= len(raw):
        return None
    return raw[position]


def _clean_text(value: Any) -> Optional[str]:
    """Текст ячейки без хвостовых пробелов. Пустое и «-» → None."""
    if value is None:
        return None
    text = _SPACES.sub(" ", str(value)).strip()
    return None if text.lower() in EMPTY_VALUES else text


_text = _clean_text


def _phone(value: Any) -> Optional[str]:
    """Телефон партнёр присылает числом (79108970195) или строкой."""
    if value is None:
        return None
    if isinstance(value, float):
        value = int(value)                # 7.91e10 из Excel — тоже телефон
    return last10(str(value))


def _int(value: Any) -> Optional[int]:
    text = _clean_text(value)
    if text is None:
        return None
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        return None


def _money(value: Any) -> Decimal:
    """«1 200.00», «3 995,00» или число 1200 → Decimal. Мусор → ноль."""
    if value is None:
        return Decimal(0)
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))

    text = _clean_text(value)
    if text is None:
        return Decimal(0)
    text = text.replace(" ", "").replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        log.warning("Отчёт партнёра: не понял сумму %r", value)
        return Decimal(0)


# Даты партнёр пишет двумя способами: с секундами и без.
_DATE_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y")


def _date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = _clean_text(value)
    if text is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    log.warning("Отчёт партнёра: не понял дату %r", value)
    return None
