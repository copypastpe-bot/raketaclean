"""Тесты разборной части экзамена autocall: счётчики, маскировка, задержка.

Все данные вымышленные: телефоны несуществующие, содержимое примечаний
не используется. Сетевую часть (main) проверяет запуск на VPS.
"""

from adminbot.amo import ids
from scripts.autocall_exam import (
    MailRow, build_report, delay_sec, describe_delay, find_mail_note,
    gone_from_stage, mail_row, note_type_counts, site_tag_id, split_by_tag,
    tags_preview,
)

SITE = "Заявка с сайта"


def lead(lead_id, *tags, pipeline=ids.PIPELINE_PRIMARY,
         status=ids.PRIM_STAGE_NEW_LEAD, created_at=None):
    payload = {
        "id": lead_id, "pipeline_id": pipeline, "status_id": status,
        "_embedded": {"tags": [{"id": 100 + index, "name": name, "color": None}
                               for index, name in enumerate(tags)]},
    }
    if created_at is not None:
        payload["created_at"] = created_at
    return payload


def note(note_id, note_type, created_at):
    return {"id": note_id, "note_type": note_type, "created_at": created_at}


def report(**overrides):
    """build_report с минимальной начинкой — тесты меняют только нужное."""
    values = dict(
        days=60, observer_total=3,
        site=[lead(111, SITE), lead(112, SITE)], others=[lead(113)],
        tagged_total=3, gone=[lead(114, SITE, status=ids.PRIM_STAGE_CORRESPONDENCE)],
        all_total=40, phones={111: "9601861067", 112: None},
        tags_raw="[{'id': 214087, 'name': 'Заявка с сайта', 'color': None}]",
        mail_rows=[MailRow(111, 62, [("amomail_message", 2), ("common", 1)])],
    )
    values.update(overrides)
    return build_report(**values)


# --- счётчики: с тегом / без тега / ушли с этапа ---

def test_split_by_tag_separates_site_leads():
    site, others = split_by_tag([lead(1, SITE), lead(2), lead(3, "Повтор")])
    assert [item["id"] for item in site] == [1]
    assert [item["id"] for item in others] == [2, 3]


def test_split_by_tag_is_case_insensitive():
    site, _ = split_by_tag([lead(1, "заявка с сайта")])
    assert [item["id"] for item in site] == [1]


def test_gone_from_stage_takes_only_tagged_leads_off_the_stage():
    on_stage = lead(1, SITE)
    moved_stage = lead(2, SITE, status=ids.PRIM_STAGE_CORRESPONDENCE)
    moved_pipeline = lead(3, SITE, pipeline=ids.PIPELINE_REALIZATION,
                          status=ids.REAL_STAGE_CREATED)
    manual_moved = lead(4, status=ids.PRIM_STAGE_CORRESPONDENCE)

    gone = gone_from_stage([on_stage, moved_stage, moved_pipeline, manual_moved])

    assert [item["id"] for item in gone] == [2, 3]


# --- сырой вид тегов ---

def test_site_tag_id_reads_id_of_the_site_tag():
    payload = lead(1, "Повтор", SITE)
    assert site_tag_id(payload) == 101          # id именно сайтового тега
    assert site_tag_id(lead(2, "Повтор")) is None
    assert site_tag_id(None) is None


def test_tags_preview_shows_raw_tags():
    preview = tags_preview(lead(1, SITE))
    assert "Заявка с сайта" in preview and "100" in preview


def test_tags_preview_truncates_long_output():
    payload = lead(1, *[f"Тег номер {index}" for index in range(50)])
    preview = tags_preview(payload, limit=120)
    assert len(preview) <= 121 and preview.endswith("…")


# --- задержка «письмо → сделка» ---

def test_note_type_counts():
    notes = [note(1, "common", 10), note(2, "amomail_message", 20),
             note(3, "common", 30)]
    assert note_type_counts(notes) == [("common", 2), ("amomail_message", 1)]


def test_find_mail_note_picks_earliest_mail_note():
    notes = [note(1, "common", 100), note(2, "amomail_message", 200),
             note(3, "amomail_message", 150)]
    assert find_mail_note(notes)["id"] == 3


def test_find_mail_note_returns_none_without_mail_notes():
    assert find_mail_note([note(1, "common", 100), note(2, "call_in", 50)]) is None
    assert find_mail_note([]) is None


def test_delay_sec_is_lead_created_minus_note_created():
    payload = lead(1, SITE, created_at=1000)
    assert delay_sec(payload, note(1, "amomail_message", 940)) == 60
    assert delay_sec(payload, note(1, "amomail_message", 1030)) == -30


def test_describe_delay_both_directions():
    positive = describe_delay(75)
    assert "1 мин 15 с" in positive and "после письма" in positive
    negative = describe_delay(-30)
    assert "30 с" in negative and "позже" in negative


def test_mail_row_without_mail_note_keeps_the_real_types():
    row = mail_row(lead(5, SITE, created_at=1000), [note(1, "common", 900)])
    assert row.lead_id == 5 and row.delay is None
    assert row.types == [("common", 1)]


# --- отчёт: цифры на месте, телефоны замаскированы ---

def test_report_masks_phones():
    text = report()
    assert "9601861067" not in text            # полного телефона в отчёте нет
    assert "…1067" in text                     # есть только последние 4 цифры


def test_report_shows_all_counters():
    text = report()
    assert "за 60 дней: 3" in text             # всего на этапе
    assert "возьмёт в работу: 2" in text       # с тегом
    assert "не тронет: 1" in text              # без тега
    assert "ушли с этапа" in text and ": 1" in text
    assert "1 из 2" in text                    # телефон извлёкся (цель 100%)
    assert "#112" in text                      # заявка без телефона названа по id


def test_report_shows_raw_tags_and_mail_delay():
    text = report()
    assert "214087" in text                    # сырой вид тегов со всеми id
    assert "1 мин 2 с" in text                 # задержка письма по сделке #111
    assert "amomail_message" in text           # реальные типы примечаний


def test_report_tells_when_mail_note_is_missing():
    text = report(mail_rows=[MailRow(115, None, [("common", 3)])])
    assert "письмо в примечаниях не видно" in text
    assert "common" in text                    # какие типы там на самом деле


def test_report_warns_when_no_tags_seen_at_all():
    """Ни одного тега в списочном ответе — вероятно, амо их не отдаёт."""
    text = report(site=[], phones={}, mail_rows=[],
                  tags_raw="", tagged_total=0, gone=[])
    assert "ВНИМАНИЕ" in text


def test_report_warns_when_page_limit_is_reached():
    """Обратная проверка упёрлась в потолок страниц — цифры могут быть неполными."""
    assert "предел" in report(all_total=5000)
    assert "предел" not in report(all_total=4999)
