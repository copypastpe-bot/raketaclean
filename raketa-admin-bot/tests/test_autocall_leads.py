"""Тесты отбора заявок с сайта: тег и телефон клиента.

Все данные вымышленные: телефоны несуществующие, имена нейтральные.
"""

from adminbot.autocall.leads import SITE_TAG, is_site_lead, lead_phone10


def _lead_with_tags(*names):
    return {"id": 601, "_embedded": {"tags": [
        {"id": index, "name": name} for index, name in enumerate(names, start=1)
    ]}}


def _contact(contact_id, *phones, name="Клиент"):
    return {
        "id": contact_id,
        "name": name,
        "custom_fields_values": [
            {"field_code": "PHONE",
             "values": [{"value": phone} for phone in phones]},
        ],
    }


# --- is_site_lead ---

def test_site_lead_by_exact_tag():
    assert is_site_lead(_lead_with_tags(SITE_TAG))


def test_site_tag_matches_ignoring_case_and_spaces():
    """Тег руками правят в амо — регистр и пробелы не должны ломать отбор."""
    assert is_site_lead(_lead_with_tags("заявка с сайта"))
    assert is_site_lead(_lead_with_tags("ЗАЯВКА С САЙТА"))
    assert is_site_lead(_lead_with_tags("  Заявка с сайта  "))


def test_site_tag_found_among_other_tags():
    assert is_site_lead(_lead_with_tags("Повтор", SITE_TAG, "VIP"))


def test_other_tags_do_not_count():
    assert not is_site_lead(_lead_with_tags("Повтор"))
    assert not is_site_lead(_lead_with_tags("Заявка"))          # часть имени — не совпадение


def test_lead_without_tags_is_not_site_lead():
    assert not is_site_lead({})
    assert not is_site_lead({"_embedded": {}})
    assert not is_site_lead(None)


# --- lead_phone10 ---

def test_phone_taken_from_main_contact():
    """У сделки два контакта — главный не первый в списке, но берём его."""
    lead = {"id": 601, "_embedded": {"contacts": [
        {"id": 11, "is_main": False},
        {"id": 22, "is_main": True},
    ]}}
    contacts = [_contact(11, "79001111111"), _contact(22, "79002222222")]

    assert lead_phone10(lead, contacts) == "9002222222"


def test_phone_falls_back_to_first_contact_without_main_flag():
    lead = {"id": 601, "_embedded": {"contacts": [{"id": 11}, {"id": 22}]}}
    contacts = [_contact(22, "79002222222"), _contact(11, "79001111111")]

    assert lead_phone10(lead, contacts) == "9001111111"


def test_first_phone_of_contact_wins():
    lead = {"id": 601, "_embedded": {"contacts": [{"id": 11, "is_main": True}]}}
    contacts = [_contact(11, "+7 900 123-45-67", "79009999999")]

    assert lead_phone10(lead, contacts) == "9001234567"


def test_no_phone_or_no_contact_gives_none():
    """Телефона нет или не разобрался — None: дальше решает наблюдатель."""
    lead = {"id": 601, "_embedded": {"contacts": [{"id": 11, "is_main": True}]}}

    assert lead_phone10(lead, [_contact(11)]) is None            # контакт без телефона
    assert lead_phone10(lead, [_contact(11, "12345")]) is None   # мусор вместо номера
    assert lead_phone10(lead, []) is None                        # контакт не подгружен
    assert lead_phone10({}, [_contact(11, "79001111111")]) is None   # сделка без контактов
    assert lead_phone10(None, []) is None
