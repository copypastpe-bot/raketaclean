from adminbot.phone import normalize_phone, last10

CASES = [
    ("89601861067", "9601861067"),
    ("+7 922 088‑82‑08", "9220888208"),          # юникод-дефисы
    ("Ирина\xa089159496642", "9159496642"),                  # nbsp перед номером
    ("Диван 3300₽\nСушка 40\n89960663965 Фания", "9960663965"),  # цены перед номером
    ("9202572757", "9202572757"),                            # 10 цифр с 9
    ("Матрас 160/1 2000₽ + 700₽", None),                     # телефона нет
    ("", None),
]


def test_last10():
    for raw, expected in CASES:
        assert last10(raw) == expected, raw


def test_normalize_plus7():
    assert normalize_phone("89601861067") == "+79601861067"
    assert normalize_phone("нет номера") is None              # НЕ возвращаем мусор (отличие от bot.py)
