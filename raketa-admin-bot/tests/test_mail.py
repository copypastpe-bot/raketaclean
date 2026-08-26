"""Чтение писем партнёра из почтового ящика робота.

Живой почты в тестах нет: подставляем двойник, который отвечает так же, как
imaplib. Проверяем именно то, что легко сделать неправильно, — какие письма
берём, как достаём вложения и когда помечаем письмо разобранным.
"""

from email.message import EmailMessage

import pytest

from adminbot.mail import Letter, MailBox, MailError, mail_settings_from_env


def make_letter(subject: str, attachments: dict[str, bytes] | None = None,
                sender: str = "raketa@raketaclean.ru") -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Date"] = "Wed, 26 Aug 2026 14:15:45 +0300"
    message.set_content("отчёт во вложении")
    for name, data in (attachments or {}).items():
        message.add_attachment(data, maintype="application", subtype="octet-stream",
                               filename=name)
    return message.as_bytes()


class FakeImap:
    """Двойник imaplib: отвечает теми же кортежами ('OK', [...])."""

    def __init__(self, letters: dict[bytes, bytes], unseen: list[bytes] | None = None):
        self.letters = letters
        self.unseen = unseen if unseen is not None else list(letters)
        self.selected = None
        self.marked: list[bytes] = []
        self.logged_out = False
        self.fail_on_select = False

    def select(self, folder, readonly=False):
        if self.fail_on_select:
            return "NO", [b"folder not found"]
        self.selected = (folder, readonly)
        return "OK", [str(len(self.letters)).encode()]

    def search(self, charset, *criteria):
        assert "UNSEEN" in criteria                # берём только неразобранные
        return "OK", [b" ".join(self.unseen)]

    def fetch(self, uid, parts):
        return "OK", [(b"1 (RFC822 {%d}" % len(self.letters[uid]), self.letters[uid]), b")"]

    def store(self, uid, command, flags):
        assert command == "+FLAGS" and flags == "\\Seen"
        self.marked.append(uid)
        return "OK", [b""]

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]


def box_with(letters: dict[bytes, bytes], **kwargs) -> tuple[MailBox, FakeImap]:
    fake = FakeImap(letters, **kwargs)
    return MailBox(connect=lambda: fake, folder="robot_amo"), fake


# --- какие письма берём ---

async def test_takes_only_unread_letters_with_xlsx():
    _, fake = box_with({})
    box, fake = box_with({
        b"1": make_letter("отчёт с 10.08 по 16.08", {"Договоры (10).xlsx": b"PK\x03\x04data"}),
        b"2": make_letter("реклама", {}),
        b"3": make_letter("счёт", {"счёт.pdf": b"%PDF-1.4"}),
    })

    letters = await box.fetch_new()

    assert [letter.uid for letter in letters] == ["1"]
    assert letters[0].attachments["Договоры (10).xlsx"] == b"PK\x03\x04data"
    assert letters[0].subject == "отчёт с 10.08 по 16.08"
    assert fake.selected == ("robot_amo", False)


async def test_letter_with_several_reports_keeps_them_all():
    box, _ = box_with({b"7": make_letter("свод за месяц", {
        "ракета июль.xlsx": b"PK\x03\x04july",
        "ракета забор отказ.xlsx": b"PK\x03\x04refused",
    })})

    letters = await box.fetch_new()

    assert set(letters[0].attachments) == {"ракета июль.xlsx", "ракета забор отказ.xlsx"}


async def test_russian_filename_is_decoded():
    """Имя вложения приезжает закодированным — владелец должен видеть его как есть."""
    box, _ = box_with({b"5": make_letter("отчёт", {"ракета-2.xlsx": b"PK\x03\x04"})})

    letters = await box.fetch_new()

    assert "ракета-2.xlsx" in letters[0].attachments


async def test_read_letters_are_skipped():
    box, _ = box_with(
        {b"1": make_letter("старый отчёт", {"Договоры (9).xlsx": b"PK"})},
        unseen=[],
    )

    assert await box.fetch_new() == []


# --- пометка разобранным ---

async def test_letter_is_marked_read_on_demand():
    """Помечаем только по команде — после того, как строки действительно разобраны."""
    box, fake = box_with({b"4": make_letter("отчёт", {"Договоры (12).xlsx": b"PK"})})

    letters = await box.fetch_new()
    assert fake.marked == []                       # само чтение письмо не «съедает»

    await box.mark_seen(letters[0].uid)
    assert fake.marked == [b"4"]


# --- сбои ---

async def test_missing_folder_is_reported_clearly():
    box, fake = box_with({}, )
    fake.fail_on_select = True

    with pytest.raises(MailError, match="robot_amo"):
        await box.fetch_new()


async def test_connection_is_always_closed():
    box, fake = box_with({b"1": make_letter("отчёт", {"a.xlsx": b"PK"})})

    await box.fetch_new()

    assert fake.logged_out is True


# --- настройки ---

def test_settings_are_read_from_env(monkeypatch):
    monkeypatch.setenv("MAIL_IMAP_HOST", "imap.yandex.ru")
    monkeypatch.setenv("MAIL_USER", "amoraketaclean@yandex.ru")
    monkeypatch.setenv("MAIL_APP_PASSWORD", "секрет")
    monkeypatch.setenv("MAIL_FOLDER", "robot_amo")

    settings = mail_settings_from_env()

    assert settings.host == "imap.yandex.ru"
    assert settings.port == 993                    # значение по умолчанию
    assert settings.folder == "robot_amo"
    assert "секрет" not in repr(settings)          # пароль не должен утечь в логи


def test_missing_mail_password_is_named(monkeypatch):
    monkeypatch.setenv("MAIL_USER", "amoraketaclean@yandex.ru")
    monkeypatch.delenv("MAIL_APP_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="MAIL_APP_PASSWORD"):
        mail_settings_from_env()
