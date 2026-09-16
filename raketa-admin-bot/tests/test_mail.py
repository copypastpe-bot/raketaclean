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
    """Двойник imaplib: отвечает теми же кортежами ('OK', [...]).

    Письма живут под постоянными UID, а порядковый номер — это место письма
    в папке: удалили одно, и у следующих номер сдвинулся. Команды без `uid`
    адресуют письмо номером, команды `uid(...)` — постоянным UID. Поэтому
    возврат к номерам виден в тестах сразу, а не на проде.
    """

    def __init__(self, letters: dict[bytes, bytes], unseen: list[bytes] | None = None):
        self.letters = dict(letters)
        self.unseen = list(unseen) if unseen is not None else list(self.letters)
        self.selected = None
        self.marked: list[bytes] = []
        self.fetched: list[bytes] = []
        self.logged_out = False
        self.fail_on_select = False

    def remove(self, uid: bytes) -> None:
        """Письмо удалили из папки: порядковые номера остальных сдвинулись."""
        self.letters.pop(uid, None)
        if uid in self.unseen:
            self.unseen.remove(uid)

    def select(self, folder, readonly=False):
        if self.fail_on_select:
            return "NO", [b"folder not found"]
        self.selected = (folder, readonly)
        return "OK", [str(len(self.letters)).encode()]

    # --- команды по порядковому номеру ---

    def search(self, charset, *criteria):
        assert "UNSEEN" in criteria                # берём только неразобранные
        order = list(self.letters)
        numbers = [str(order.index(uid) + 1).encode() for uid in self.unseen]
        return "OK", [b" ".join(numbers)]

    def fetch(self, number, parts):
        return self._fetch(self._by_number(number), parts)

    def store(self, number, command, flags):
        return self._store(self._by_number(number), command, flags)

    # --- команды по постоянному UID ---

    def uid(self, command, *args):
        name = command.lower()
        if name == "search":
            criteria = [arg for arg in args if arg is not None]
            assert criteria and criteria[-1] in ("UNSEEN", "ALL")
            wanted = self.unseen if criteria[-1] == "UNSEEN" else list(self.letters)
            return "OK", [b" ".join(wanted)]
        if name == "fetch":
            target, parts = args
            return self._fetch(self._as_uid(target), parts)
        if name == "store":
            target, command_name, flags = args
            return self._store(self._as_uid(target), command_name, flags)
        raise AssertionError(f"двойник не знает команду UID {command}")

    # --- общее ---

    def _fetch(self, uid, parts):
        # Настоящий сервер на «(RFC822)» пометил бы письмо прочитанным. Двойник
        # этого не делает, поэтому проверяем сам запрос — иначе ошибка не видна.
        assert "PEEK" in parts, "письмо нужно читать через BODY.PEEK, иначе оно «съедается»"
        self.fetched.append(uid)
        return "OK", [(b"1 (BODY[] {%d}" % len(self.letters[uid]), self.letters[uid]), b")"]

    def _store(self, uid, command, flags):
        assert command == "+FLAGS" and flags == "\\Seen"
        assert uid in self.letters, f"письма {uid!r} в папке нет"
        self.marked.append(uid)
        return "OK", [b""]

    def _by_number(self, number) -> bytes:
        text = number.decode() if isinstance(number, bytes) else str(number)
        order = list(self.letters)
        index = int(text) - 1
        assert 0 <= index < len(order), f"в папке нет письма с номером {text}"
        return order[index]

    def _as_uid(self, target) -> bytes:
        uid = target if isinstance(target, bytes) else str(target).encode()
        assert uid in self.letters, f"письма с UID {uid!r} в папке нет"
        return uid

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
    assert fake.selected == ("robot_amo", True)   # чтение не меняет состояние ящика


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


async def test_uid_survives_deletion_of_another_letter():
    """Удалили письмо из папки — у остальных сдвинулся номер, но не UID.

    Отложенное письмо лежит в папке непрочитанным неделями. Если адресовать его
    порядковым номером, после удаления любого соседа робот пометил бы прочитанным
    чужое письмо, а отложенное так и осталось бы висеть.
    """
    box, fake = box_with({
        b"5": make_letter("старый отчёт", {"а.xlsx": b"PK"}),
        b"6": make_letter("отчёт за неделю", {"б.xlsx": b"PK"}),
        b"7": make_letter("отказы за месяц", {"в.xlsx": b"PK"}),
    })

    letters = await box.fetch_new()
    assert [letter.uid for letter in letters] == ["5", "6", "7"]

    fake.remove(b"5")                              # владелец удалил лишнее письмо

    await box.mark_seen("7")

    # По порядковому номеру у письма 7 был номер 3 (третье при чтении); после
    # удаления письма 5 в папке остались только два письма, и номера 3 больше
    # нет — адресация по номеру здесь не промахнулась бы мимо письма, а упала бы
    # с ошибкой «в папке нет письма с номером 3» (задача 4, 16.09).
    assert fake.marked == [b"7"]


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
