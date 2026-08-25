"""Связка «мастер заказа ↔ Специалист в amoCRM».

Поле «Специалист» (id 39243) — список значений вида «Дмитрий Козлов +79306858534».
Телефон внутри значения совпадает с телефоном мастера в базе бота, поэтому связка
настраивается сама: новый мастер подхватится, как только у него заполнен телефон.

Проверено 2026-08-25: оба действующих мастера (171 заказ за квартал) опознаются
по телефону без ручной настройки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from adminbot.phone import last10

# Из значения enum убираем всё, что не буква: телефоны, плюсы, двоеточия, «телефон -».
_NON_LETTERS = re.compile(r"[^а-яёa-z]+", re.IGNORECASE)


def _name_key(value: Optional[str]) -> str:
    """Ключ имени: без регистра, без цифр и знаков, слова по алфавиту.

    «Бочарова Мария» и «Мария Бочарова» дают один ключ — в амо и в боте порядок
    имени и фамилии не согласован.
    """
    if not value:
        return ""
    words = [word for word in _NON_LETTERS.split(value.lower()) if word]
    return " ".join(sorted(words))


@dataclass(frozen=True)
class SpecialistIndex:
    """Справочник: по телефону или имени мастера даёт enum-значение амо."""

    by_phone: dict[str, int]
    by_name: dict[str, int]

    @classmethod
    def from_enums(cls, enums: Iterable[dict]) -> "SpecialistIndex":
        by_phone: dict[str, int] = {}
        by_name: dict[str, int] = {}
        ambiguous_names: set[str] = set()

        for enum in enums:
            enum_id = enum.get("id")
            value = str(enum.get("value") or "")
            if enum_id is None or not value:
                continue

            phone = last10(value)
            if phone:
                by_phone.setdefault(phone, int(enum_id))

            key = _name_key(value)
            if not key:
                continue
            if key in by_name and by_name[key] != int(enum_id):
                ambiguous_names.add(key)     # тёзки: по имени не различить
            else:
                by_name[key] = int(enum_id)

        for key in ambiguous_names:
            by_name.pop(key, None)

        return cls(by_phone=by_phone, by_name=by_name)

    def resolve(self, *, name: Optional[str], phone: Optional[str]) -> tuple[int, ...]:
        """Найти «Специалиста» для мастера. Пусто — если мастер неизвестен амо."""
        digits = last10(phone or "")
        if digits and digits in self.by_phone:
            return (self.by_phone[digits],)

        key = _name_key(name)
        if key and key in self.by_name:
            return (self.by_name[key],)
        return ()

    def resolve_many(self, masters: Sequence[tuple[Optional[str], Optional[str]]]) -> tuple[int, ...]:
        """Все «Специалисты» для мастеров одного заказа (бывает до пяти)."""
        found: list[int] = []
        for name, phone in masters:
            for enum_id in self.resolve(name=name, phone=phone):
                if enum_id not in found:
                    found.append(enum_id)
        return tuple(found)
