"""Окно звонков: когда роботу можно набирать клиента.

Заявка ночью не должна будить клиента: звонки идут только с 10:00 до 20:00 МСК
(решение владельца №5), всё остальное ждёт ближайшего утра. Через эту же
функцию проходят повторы попыток — повтор, выпавший на 20:08, тоже уедет
на завтра.

Чистый модуль: времени «сейчас» не знает, сеть и хранилище не трогает.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from adminbot.amo.fields import MOSCOW_TZ


def validate_window(from_hour: int, to_hour: int) -> None:
    """Проверить часы окна при старте сервиса.

    Опечатку в настройке ловим при запуске: упавший старт дешевле, чем робот,
    который звонит клиенту в три часа ночи (тот же принцип, что у
    `_service_by_master` в config.py).
    """
    if not (0 <= from_hour <= 23) or not (0 <= to_hour <= 23):
        raise RuntimeError(
            f"Окно звонков задано неверно: часы {from_hour} и {to_hour} "
            "должны быть в пределах 0–23. Проверьте настройки AUTOCALL."
        )
    if from_hour >= to_hour:
        raise RuntimeError(
            f"Окно звонков задано неверно: начало ({from_hour}:00) должно быть "
            f"раньше конца ({to_hour}:00). Проверьте настройки AUTOCALL."
        )


def next_call_moment(created_at: datetime, *, now: datetime,
                     from_hour: int = 10, to_hour: int = 20) -> datetime:
    """Когда можно звонить по заявке. Всё в МСК (MOSCOW_TZ из amo/fields.py).

    В окне 10:00–20:00 — прямо сейчас; ночью/вечером — ближайшие 10:00
    (решение владельца №5: заявка в 21:30 ждёт до утра).

    `created_at` — желаемый момент: создание заявки или назначенный повтор
    попытки. Если он уже в прошлом (робот был выключен), отсчитываем от `now`.
    Граница `to_hour` в окно не входит: ровно в 20:00 звонить уже поздно.
    Вход — только aware-datetime (амо отдаёт unix-время, то есть UTC);
    результат — aware в МСК.
    """
    if created_at.tzinfo is None or now.tzinfo is None:
        raise ValueError(
            "next_call_moment принимает только aware-datetime: "
            "naive-время нельзя однозначно привязать к МСК"
        )

    desired = max(created_at, now).astimezone(MOSCOW_TZ)
    morning = desired.replace(hour=from_hour, minute=0, second=0, microsecond=0)

    if desired < morning:            # ночь или раннее утро — ждём открытия окна
        return morning
    if desired.hour >= to_hour:      # вечер — окно закрыто, ждём завтрашнего утра
        return morning + timedelta(days=1)
    return desired                   # окно открыто — звонить можно прямо сейчас
