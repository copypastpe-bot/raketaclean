"""Связь с Telegram с российского сервера.

Имя api.telegram.org с этого сервера не разрешается, зато сами адреса доступны.
Рабочий бот компании давно живёт по прямым адресам — админ-бот делает так же:
для api.telegram.org подставляет адрес из списка, для остальных имён работает
обычным образом.
"""

import socket

from adminbot.tg.session import TelegramIPResolver, parse_ip_pool

POOL = ["149.154.167.220", "149.154.167.91"]


class Reachability:
    """Какие адреса «отвечают» в этом тесте."""

    def __init__(self, alive):
        self.alive = set(alive)
        self.probes = []

    async def __call__(self, ip, port):
        self.probes.append(ip)
        return ip in self.alive


async def test_telegram_name_resolves_to_a_reachable_address():
    probe = Reachability(alive=POOL)
    resolver = TelegramIPResolver(POOL, probe=probe)

    records = await resolver.resolve("api.telegram.org", 443)

    assert [r["host"] for r in records] == ["149.154.167.220"]
    assert records[0]["hostname"] == "api.telegram.org"
    assert records[0]["port"] == 443
    assert records[0]["family"] == socket.AF_INET


async def test_unreachable_address_is_skipped():
    """Первый адрес молчит — берём следующий, а не падаем."""
    probe = Reachability(alive={"149.154.167.91"})
    resolver = TelegramIPResolver(POOL, probe=probe)

    records = await resolver.resolve("api.telegram.org", 443)

    assert [r["host"] for r in records] == ["149.154.167.91"]
    assert probe.probes == POOL                     # проверили по порядку


async def test_choice_is_remembered_between_calls():
    """Проверять связь на каждый запрос — лишняя задержка."""
    probe = Reachability(alive=POOL)
    resolver = TelegramIPResolver(POOL, probe=probe)

    await resolver.resolve("api.telegram.org", 443)
    await resolver.resolve("api.telegram.org", 443)

    assert probe.probes == ["149.154.167.220"]      # второй раз не проверяли


async def test_all_addresses_silent_still_returns_something():
    """Связи нет вовсе — отдаём первый адрес: пусть ошибку покажет сам запрос."""
    probe = Reachability(alive=set())
    resolver = TelegramIPResolver(POOL, probe=probe)

    records = await resolver.resolve("api.telegram.org", 443)

    assert records[0]["host"] == POOL[0]


async def test_other_hosts_go_the_usual_way():
    calls = []

    class FakeDefault:
        async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
            calls.append(host)
            return [{"hostname": host, "host": "1.2.3.4", "port": port,
                     "family": socket.AF_INET, "proto": 0, "flags": 0}]

        async def close(self):
            pass

    resolver = TelegramIPResolver(POOL, probe=Reachability(POOL), default=FakeDefault())

    records = await resolver.resolve("raketacleancrm.amocrm.ru", 443)

    assert calls == ["raketacleancrm.amocrm.ru"]
    assert records[0]["host"] == "1.2.3.4"


# --- разбор настройки ---

def test_ip_pool_accepts_the_usual_separators():
    assert parse_ip_pool("1.1.1.1, 2.2.2.2;3.3.3.3 4.4.4.4") == [
        "1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"]


def test_ip_pool_drops_junk_and_duplicates():
    assert parse_ip_pool("1.1.1.1, не-адрес, 1.1.1.1, 2.2.2.2") == ["1.1.1.1", "2.2.2.2"]


def test_empty_setting_means_usual_resolution():
    assert parse_ip_pool("") == []
    assert parse_ip_pool(None) == []
