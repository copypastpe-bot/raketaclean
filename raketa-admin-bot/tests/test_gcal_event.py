"""Разбор записи календаря: что это, для кого и что из этого пишем в CRM.

Все примеры — настоящие записи календаря raketaclean52@gmail.com за 24-27.08.2026
(структура сохранена, телефоны и коды доступа заменены на такие же по форме).

Главное, что проверяют эти тесты, — робот НЕ принимает за заказ то, что заказом
не является: выходной мастера, гарантийный перемыв, запись с неточной датой.
Лишняя сделка в CRM — единственная ошибка этого разбора, которая стоит денег.
"""

from datetime import date

from adminbot.gcal.event import EventKind, parse_event


def test_regular_order():
    parsed = parse_event({
        "id": "p0rag0ivt09on8lrr6s0q64mh0",
        "summary": "Сов! Матрас, Юлия",
        "description": ("Матрас/2\nСушка/2\nВозможно еще 1 будет\n"
                        "Хорошо прополаскивать, потом водой разок пройти чистой, "
                        "аллергик астматик спит\n\n89605379757 Юлия\n89605379755"),
        "location": "Панина д 7к2, кв 132, эт 8, п 3",
        "start": {"dateTime": "2026-08-24T14:30:00+03:00", "timeZone": "Europe/Moscow"},
        "status": "confirmed",
    })

    assert parsed.kind is EventKind.ORDER
    assert parsed.order_date == date(2026, 8, 24)
    assert parsed.phone10 == "9605379757"            # первый телефон — основной
    assert parsed.phones == ("9605379757", "9605379755")
    assert parsed.client_name == "Юлия"
    assert parsed.district == "советский"
    assert parsed.services == ("mattress",)
    assert parsed.address == "Панина д 7к2, кв 132, эт 8, п 3"
    # Комментарий: телефоны вырезаны, пожелания клиента сохранены целиком
    assert "аллергик астматик спит" in parsed.comment
    assert "Сушка/2" in parsed.comment
    assert "89605379757" not in parsed.comment


def test_access_codes_never_leave_the_calendar():
    """Коды домофона, кейбокса и пароль wi-fi в CRM не уезжают (решение 3)."""
    parsed = parse_event({
        "id": "jeqhll8nqiqm0ml7ilpl65u95g",
        "summary": "Ниж! Диван, Наталья",
        "description": ("Диван\nСушка\n+7 (986) 742-19-33 Наталья (Марина аренда)\n\n"
                        "Код домофона 5974#\nКод Кейбокса 3185\n"
                        "Сеть: RT-WIFI-DBD5, Пароль: yMm3L98kea"),
        "location": "Б Покровская д 25 помещ 13",
        "start": {"dateTime": "2026-08-26T12:00:00+03:00", "timeZone": "Europe/Moscow"},
        "status": "confirmed",
    })

    assert parsed.kind is EventKind.ORDER
    assert parsed.services == ("furniture",)
    assert parsed.phone10 == "9867421933"
    for secret in ("5974", "3185", "yMm3L98kea", "742-19-33"):
        assert secret not in parsed.comment


def test_prices_and_jargon_stay_in_the_comment():
    """«Сушка 40», «Нз 1800₽» — рабочий язык мастера, он остаётся (решение 3)."""
    parsed = parse_event({
        "id": "b5k0lf6dklcbvg7ifaqcrpv6kg",
        "summary": "Мос! Диван, Татьяна",
        "description": "Диван 3300₽\nПодушки по размеру\nНз 1500₽\nСушка 40\n89026876667 Татьяна",
        "location": "Ясная, д33, кв 128, п4, эт 5",
        "start": {"dateTime": "2026-08-24T10:00:00+03:00", "timeZone": "Europe/Moscow"},
        "status": "confirmed",
    })

    assert parsed.district == "московский"
    assert parsed.phone10 == "9026876667"
    assert "Нз 1500₽" in parsed.comment
    assert "Сушка 40" in parsed.comment


def test_master_day_off_is_not_an_order():
    parsed = parse_event({"id": "gj0l4116srk1l8urh4183a92bo", "summary": "⛔️Дима",
                          "start": {"date": "2026-08-28"}, "status": "confirmed"})

    assert parsed.kind is EventKind.BLOCK
    assert parsed.order_date == date(2026, 8, 28)


def test_rewash_is_a_warranty_visit():
    """«Перемыв» — доработка по уже выполненному заказу, денег нет (решение 6)."""
    parsed = parse_event({
        "id": "1tn2ekjaiqn6akc2u2ltj4jlds",
        "summary": "Сов! Перемыв, Наталья",
        "description": "Разводы на матрасе прополоскать\nСтул\n89050136291 Наталья",
        "start": {"dateTime": "2026-08-25T14:30:00+03:00"}, "status": "confirmed"})

    assert parsed.kind is EventKind.REWASH


def test_unsettled_date_waits_for_the_owner():
    """Пометка ⁉️ значит «дата не твёрдая»: ждём, пока владелец её снимет (решение 9)."""
    parsed = parse_event({
        "id": "3v9ssoudneisadfhhn0dmqp5mg",
        "summary": "⁉️Перенос(дату уточнить)Сов! Диван Наталья",
        "description": "Диван и кресло\n\n\nНаталья +7 910 104 9710",
        "start": {"dateTime": "2026-08-26T10:00:00+03:00"}, "status": "confirmed"})

    assert parsed.kind is EventKind.UNSETTLED


def test_boat_is_b2b_without_phone():
    """Теплоход: ни телефона, ни цены — робот сам не заводит, спросит (решение 7)."""
    parsed = parse_event({"id": "boat1", "summary": "Толстой с 9:00",
                          "start": {"dateTime": "2026-09-12T09:00:00+03:00"},
                          "status": "confirmed"})

    assert parsed.kind is EventKind.BOAT
    assert parsed.phone10 is None
    assert parsed.client_name == "Толстой"


def test_order_without_phone_is_skipped_quietly():
    """Заказ без телефона придёт из бота после выполнения — молчим (решение 10)."""
    parsed = parse_event({"id": "nophone", "summary": "Авт! Стулья, Светлана",
                          "description": "39 стульев\nОплата безнал",
                          "start": {"dateTime": "2026-08-24T10:00:00+03:00"},
                          "status": "confirmed"})

    assert parsed.kind is EventKind.SKIP
    assert parsed.skip_reason == "телефон не найден"


def test_deleted_event_is_a_cancellation():
    """Удалённая запись приходит только в инкрементальном обмене, с одним id."""
    parsed = parse_event({"id": "b5k0lf6dklcbvg7ifaqcrpv6kg", "status": "cancelled"})

    assert parsed.kind is EventKind.CANCELLED
    assert parsed.event_id == "b5k0lf6dklcbvg7ifaqcrpv6kg"


def test_unknown_prefix_leaves_district_empty():
    """Район выдумывать запрещено: непонятную приставку называем владельцу (решение 12)."""
    parsed = parse_event({"id": "u", "summary": "Печер! Диван, Ольга",
                          "description": "Диван\n89601861067 Ольга",
                          "start": {"dateTime": "2026-08-24T10:00:00+03:00"}})

    assert parsed.kind is EventKind.ORDER
    assert parsed.district is None
    assert parsed.unknown_district == "печер"


def test_comma_instead_of_bang_still_parses():
    """6% заголовков отклоняются от канона: запятая вместо «!» и нет приставки."""
    with_comma = parse_event({"id": "c", "summary": "Сов, Уборка, Андрей",
                              "description": "Генеральная\n89601861067 Андрей",
                              "start": {"dateTime": "2026-08-24T10:00:00+03:00"}})
    assert with_comma.district == "советский"
    assert with_comma.services == ("cleaning",)

    without_prefix = parse_event({"id": "d", "summary": "Ковролин Виктория",
                                  "description": "Ковролин 20 м2\n89601861067 Виктория",
                                  "start": {"dateTime": "2026-08-24T10:00:00+03:00"}})
    assert without_prefix.district is None
    assert without_prefix.unknown_district is None      # приставки не было вовсе
    assert without_prefix.services == ("carpeting",)


def test_service_words_map_to_amo_values():
    """Разбор услуги по решению владельца 11: мебель не детализируем."""
    cases = {
        "Сов! Матрас, Юлия": ("mattress",),
        "Лен! Стулья, Юлия": ("furniture",),
        "Сов! Мебель, Ирина": ("furniture",),
        "Ниж! Диван, Наталья": ("furniture",),
        "Авт! Кресло, Ольга": ("furniture",),
        "Кан! Ковролин, Пётр": ("carpeting",),
        "Сов! Уборка, Андрей": ("cleaning",),
        "Мос! Окна, Ирина": ("windows",),
    }
    for summary, expected in cases.items():
        parsed = parse_event({"id": summary, "summary": summary,
                              "description": "89601861067",
                              "start": {"dateTime": "2026-08-24T10:00:00+03:00"}})
        assert parsed.services == expected, summary


def test_district_shorthands_from_the_real_calendar():
    """Экзамен на 462 записях нашёл сокращения, которых не было в разведке.

    Расшифрованы по адресу записи: «Дзерж» — Дзержинск, «Автоз»/«Авто» — Сазанова
    и Строкина, «Ниже» — Академика Блохиной, «Бог» — Каменки. Приставки, которые
    в список районов амо не ложатся (Печёры, Афонино, Кусаковка, Цветы), остаются
    непонятными намеренно: район в CRM важнее скорости догадки.
    """
    cases = {
        "Дзерж! Диван, Ирина": "дзержинский",
        "Автоз! Диван Екатерина": "автозаводский",
        "Авто! Диван Мария": "автозаводский",
        "Ниже! Уборка + Мебель Влад": "нижегородский",
        "Бог! Диван Марина": "богородский",
    }
    for summary, district in cases.items():
        parsed = parse_event({"id": summary, "summary": summary,
                              "description": "89601861067",
                              "start": {"dateTime": "2026-08-24T10:00:00+03:00"}})
        assert parsed.district == district, summary
        assert parsed.unknown_district is None, summary

    unclear = parse_event({"id": "x", "summary": "Афон! Диван, Артур",
                           "description": "89601861067",
                           "start": {"dateTime": "2026-08-24T10:00:00+03:00"}})
    assert unclear.district is None
    assert unclear.unknown_district == "афон"


def test_plural_furniture_is_recognised():
    """«2 дивана» — две записи из боевого календаря разбирались как «услуга не понята»."""
    parsed = parse_event({"id": "pl", "summary": "Мос! 2 дивана, Ирина",
                          "description": "89601861067 Ирина",
                          "start": {"dateTime": "2026-08-24T10:00:00+03:00"}})

    assert parsed.services == ("furniture",)


def test_rug_in_description_means_cleaning_at_home():
    """«Ковёр» в тексте — это чистка ковра на дому, а не ковры партнёра (решение 11)."""
    parsed = parse_event({"id": "rug", "summary": "Сов! Диван, Ирина",
                          "description": "Диван\nКовёр 2х3\n89601861067 Ирина",
                          "start": {"dateTime": "2026-08-24T10:00:00+03:00"}})

    assert "rug_home" in parsed.services


def test_prices_are_never_taken_for_a_phone():
    """Цены и размеры рядом с номером не должны рождать выдуманный телефон."""
    parsed = parse_event({"id": "p", "summary": "Сов! Диван, Ирина",
                          "description": "Диван 900₽\nКресло 800-1500₽\nМатрас 160/1\n"
                                         "Мин выезд 3000₽\n89601861067 Ирина",
                          "start": {"dateTime": "2026-08-24T10:00:00+03:00"}})

    assert parsed.phones == ("9601861067",)


def test_event_timezone_is_respected():
    """Записи бывают из устройства с другой тайм-зоной: смещение читаем из записи.

    Дата заказа у нас всегда московская — по ней сделка ищется в амо и сверяется
    с заказом из бота. Наивный разбор («первые 10 символов строки») дал бы 24.08.
    """
    parsed = parse_event({"id": "tz", "summary": "Сов! Диван, Ирина",
                          "description": "89601861067",
                          "start": {"dateTime": "2026-08-24T23:30:00+02:00"}})

    assert parsed.order_date == date(2026, 8, 25)


def test_owner_district_canon():
    """Канон приставок от владельца (2026-08-27).

    Балахнинский и Дальнеконстантиновский робот понимает, но заполнить поле
    не сможет: таких значений нет в списке «Район города» амо. Об этом он
    скажет в вечерней сводке, а не будет молча ставить что-то похожее.
    """
    from adminbot.amo import ids

    canon = {
        "Авт": "автозаводский", "Бал": "балахнинский", "Бог": "богородский",
        "Бор": "борский", "Дал": "дальнеконстантиновский", "Кан": "канавинский",
        "Кст": "кстовский", "Лен": "ленинский", "Мос": "московский",
        "Ниж": "нижегородский", "При": "приокский", "Сов": "советский",
        "Сор": "сормовский",
    }
    for prefix, district in canon.items():
        parsed = parse_event({"id": prefix, "summary": f"{prefix}! Диван, Ирина",
                              "description": "89601861067",
                              "start": {"dateTime": "2026-08-27T10:00:00+03:00"}})
        assert parsed.district == district, prefix
        assert parsed.unknown_district is None, prefix

    without_field = {"балахнинский", "дальнеконстантиновский"}
    assert without_field & set(ids.DISTRICT_ENUMS) == set()      # их в амо и правда нет
