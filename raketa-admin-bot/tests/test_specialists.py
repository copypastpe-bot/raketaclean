"""Связка «мастер заказа в боте ↔ Специалист в amoCRM».

Значения поля «Специалист» владелец заводил руками, поэтому формат разный:
где-то телефон через плюс, где-то через «: телефон -», где-то без телефона вовсе.
"""

from adminbot.sync.specialists import SpecialistIndex

ENUMS = [
    {"id": 951507, "value": "Дмитрий Козлов +79306858534"},
    {"id": 951505, "value": "Никита Полозов +79101251720"},
    {"id": 952251, "value": "Ольга Скоропашкина 89081572721"},
    {"id": 17421, "value": "Евгений Пастушенко : телефон -79991414561"},
    {"id": 946603, "value": "Александр Авдеев +7 930 807-68-90"},
    {"id": 885503, "value": "Мария Бочарова"},              # без телефона
    {"id": 947245, "value": "Кристал"},                     # подрядчик, не мастер
]


def index():
    return SpecialistIndex.from_enums(ENUMS)


def test_matches_by_phone_regardless_of_format():
    idx = index()
    assert idx.resolve(name="Дмитрий Козлов", phone="+7 930 685-85-34") == (951507,)
    assert idx.resolve(name="кто угодно", phone="89081572721") == (952251,)
    assert idx.resolve(name="", phone="79991414561") == (17421,)
    assert idx.resolve(name="", phone="+7 930 807-68-90") == (946603,)


def test_phone_wins_over_name():
    """Тёзки в амо возможны, поэтому телефон надёжнее имени."""
    idx = SpecialistIndex.from_enums(ENUMS + [{"id": 999, "value": "Дмитрий Козлов"}])
    assert idx.resolve(name="Дмитрий Козлов", phone="79306858534") == (951507,)


def test_falls_back_to_name_when_phone_unknown():
    idx = index()
    assert idx.resolve(name="Мария Бочарова", phone=None) == (885503,)
    assert idx.resolve(name="мария   бочарова", phone="") == (885503,)   # регистр и пробелы


def test_name_matches_ignoring_word_order():
    idx = index()
    assert idx.resolve(name="Бочарова Мария", phone=None) == (885503,)


def test_unknown_master_gives_nothing():
    idx = index()
    assert idx.resolve(name="Пётр Неизвестный", phone="79990000000") == ()
    assert idx.resolve(name=None, phone=None) == ()


def test_ambiguous_name_is_not_guessed():
    """Два одинаковых имени без телефонов — признак не применяем, а не гадаем."""
    idx = SpecialistIndex.from_enums([
        {"id": 1, "value": "Иван Иванов"},
        {"id": 2, "value": "Иванов Иван"},
    ])
    assert idx.resolve(name="Иван Иванов", phone=None) == ()


def test_resolve_many_masters_of_one_order():
    idx = index()
    got = idx.resolve_many([("Дмитрий Козлов", "79306858534"), ("Никита Полозов", None)])
    assert set(got) == {951507, 951505}
