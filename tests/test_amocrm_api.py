import unittest

from notifications.amocrm_api import (
    AmoCRMAPIAuthError,
    AmoCRMAPIClient,
    AmoCRMAPIError,
    AmoCRMAPIRateLimitError,
    build_lead_link,
    extract_contact_phone,
    extract_lead_contact_ids,
    normalize_lead,
)


class AmoCRMApiExtractionTests(unittest.TestCase):
    def test_extracts_contact_phone_from_custom_fields(self):
        contact = {
            "id": 10,
            "name": "Иван",
            "custom_fields_values": [
                {
                    "field_code": "PHONE",
                    "values": [{"value": "+7 999 123-45-67", "enum_code": "WORK"}],
                }
            ],
        }

        self.assertEqual(extract_contact_phone(contact), "+7 999 123-45-67")

    def test_extracts_lead_contact_ids_from_embedded_contacts(self):
        lead = {
            "id": 123,
            "_embedded": {
                "contacts": [
                    {"id": 10, "is_main": True},
                    {"id": 11, "is_main": False},
                ]
            },
        }

        self.assertEqual(extract_lead_contact_ids(lead), [10, 11])

    def test_builds_lead_link(self):
        self.assertEqual(
            build_lead_link("https://raketacleancrm.amocrm.ru", 123),
            "https://raketacleancrm.amocrm.ru/leads/detail/123",
        )


class FakeHTTPResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)


class FakeHTTPSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.patches = []

    def get(self, url, *, headers, params, timeout):
        self.calls.append((url, headers, params, timeout))
        return self.response

    def patch(self, url, *, headers, json, timeout):
        self.patches.append((url, headers, json, timeout))
        return self.response


class AmoCRMApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_adds_bearer_token_and_base_url(self):
        session = FakeHTTPSession(FakeHTTPResponse(200, {"ok": True}))
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        result = await client.get("/api/v4/events", params={"limit": 1})

        self.assertEqual(result, {"ok": True})
        url, headers, params, timeout = session.calls[0]
        self.assertEqual(url, "https://example.amocrm.ru/api/v4/events")
        self.assertEqual(headers["Authorization"], "Bearer token")
        self.assertEqual(params, {"limit": 1})

    async def test_get_raises_auth_error_on_401(self):
        session = FakeHTTPSession(FakeHTTPResponse(401, {"detail": "bad token"}))
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        with self.assertRaises(AmoCRMAPIAuthError):
            await client.get("/api/v4/events")

    async def test_get_raises_rate_limit_error_on_429(self):
        session = FakeHTTPSession(FakeHTTPResponse(429, {"detail": "too many"}))
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        with self.assertRaises(AmoCRMAPIRateLimitError):
            await client.get("/api/v4/events")

    async def test_update_lead_status_sends_patch_with_status(self):
        session = FakeHTTPSession(FakeHTTPResponse(200, {"id": 123}))
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        await client.update_lead_status(123, 41463838)

        url, headers, body, _timeout = session.patches[0]
        self.assertEqual(url, "https://example.amocrm.ru/api/v4/leads/123")
        self.assertEqual(headers["Authorization"], "Bearer token")
        self.assertEqual(body, {"status_id": 41463838})

    async def test_denied_write_is_not_swallowed(self):
        """Нет прав на запись — вызывающий должен об этом узнать и сказать
        владельцу: клиент подтвердил заказ, а в CRM этого не видно."""
        session = FakeHTTPSession(FakeHTTPResponse(403, {"detail": "forbidden"}))
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        with self.assertRaises(AmoCRMAPIError):
            await client.update_lead_status(123, 41463838)


class SequenceHTTPSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, headers, params, timeout):
        self.calls.append((url, params))
        return self.responses.pop(0)


class AmoCRMFetchersTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_events_uses_type_and_created_at_filters(self):
        session = SequenceHTTPSession([
            FakeHTTPResponse(200, {"_embedded": {"events": [{"id": "ev-1", "type": "lead_added"}]}})
        ])
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        events = await client.fetch_events(event_types=["lead_added"], created_from=100)

        self.assertEqual(events[0]["id"], "ev-1")
        url, params = session.calls[0]
        self.assertEqual(url, "https://example.amocrm.ru/api/v4/events")
        self.assertEqual(params["filter[type]"], "lead_added")
        self.assertEqual(params["filter[created_at][from]"], 100)

    async def test_fetch_lead_requests_contacts(self):
        session = SequenceHTTPSession([
            FakeHTTPResponse(200, {"id": 123, "_embedded": {"contacts": [{"id": 10}]}})
        ])
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        lead = await client.fetch_lead(123)

        self.assertEqual(lead["id"], 123)
        url, params = session.calls[0]
        self.assertEqual(url, "https://example.amocrm.ru/api/v4/leads/123")
        self.assertEqual(params["with"], "contacts")

    async def test_fetch_unsorted_filters_pipeline_and_created_from(self):
        session = SequenceHTTPSession([
            FakeHTTPResponse(200, {"_embedded": {"unsorted": [{"uid": "u-1"}]}})
        ])
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        items = await client.fetch_unsorted(pipeline_id=55, created_from=100)

        self.assertEqual(items[0]["uid"], "u-1")
        url, params = session.calls[0]
        self.assertEqual(url, "https://example.amocrm.ru/api/v4/leads/unsorted")
        self.assertEqual(params["filter[pipeline_id]"], 55)
        self.assertEqual(params["filter[created_at][from]"], 100)


class AmoCRMPollingAlertBuildTests(unittest.TestCase):
    def test_normalize_lead_keeps_pipeline_status_and_contacts(self):
        lead = normalize_lead(
            {
                "id": 123,
                "name": "Заявка с сайта",
                "pipeline_id": 55,
                "status_id": 777,
                "created_at": 1710000000,
                "_embedded": {"contacts": [{"id": 10}]},
            }
        )

        self.assertEqual(lead.lead_id, 123)
        self.assertEqual(lead.pipeline_id, 55)
        self.assertEqual(lead.status_id, 777)
        self.assertEqual(lead.contact_ids, [10])

class AmoCRMPhoneLookupTests(unittest.IsolatedAsyncioTestCase):
    """Поиск сделок клиента по телефону — через контакт, а не через фильтр
    сделок по контакту: тот фильтр amoCRM молча игнорирует и отдаёт чужие
    сделки (проверено админ-ботом 2026-08-25)."""

    async def test_find_contacts_by_phone_asks_contacts_with_leads(self):
        session = SequenceHTTPSession([
            FakeHTTPResponse(200, {"_embedded": {"contacts": [{"id": 10}]}})
        ])
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        contacts = await client.find_contacts_by_phone("9001234567")

        self.assertEqual(contacts, [{"id": 10}])
        url, params = session.calls[0]
        self.assertEqual(url, "https://example.amocrm.ru/api/v4/contacts")
        self.assertEqual(params["query"], "9001234567")
        self.assertEqual(params["with"], "leads")

    async def test_find_contacts_by_phone_without_embedded_is_empty(self):
        """amoCRM отвечает 204 без тела, когда ничего не нашла: разбирать
        нечего, и клиент обязан вернуть пустой список, а не упасть."""

        class EmptyBodyResponse(FakeHTTPResponse):
            async def json(self):
                raise ValueError("no body")

            async def text(self):
                return ""

        session = SequenceHTTPSession([EmptyBodyResponse(204, {})])
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        self.assertEqual(await client.find_contacts_by_phone("9001234567"), [])

    async def test_fetch_leads_by_ids_without_ids_makes_no_request(self):
        session = SequenceHTTPSession([])
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        self.assertEqual(await client.fetch_leads_by_ids([]), [])
        self.assertEqual(session.calls, [])

    async def test_fetch_leads_by_ids_drops_duplicates_and_keeps_order(self):
        session = SequenceHTTPSession([
            FakeHTTPResponse(200, {"_embedded": {"leads": [{"id": 1}, {"id": 2}]}})
        ])
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        leads = await client.fetch_leads_by_ids([1, 2, 1])

        self.assertEqual([lead["id"] for lead in leads], [1, 2])
        url, params = session.calls[0]
        self.assertEqual(url, "https://example.amocrm.ru/api/v4/leads")
        self.assertEqual([value for key, value in params if key == "filter[id][]"], [1, 2])

    async def test_fetch_leads_by_ids_batches_by_fifty(self):
        session = SequenceHTTPSession([
            FakeHTTPResponse(200, {"_embedded": {"leads": [{"id": i} for i in range(50)]}}),
            FakeHTTPResponse(200, {"_embedded": {"leads": [{"id": 50}]}}),
        ])
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)

        leads = await client.fetch_leads_by_ids(range(51))

        self.assertEqual(len(leads), 51)
        self.assertEqual(len(session.calls), 2)
        first_ids = [value for key, value in session.calls[0][1] if key == "filter[id][]"]
        second_ids = [value for key, value in session.calls[1][1] if key == "filter[id][]"]
        self.assertEqual(len(first_ids), 50)
        self.assertEqual(second_ids, [50])

    async def test_get_passes_pair_list_to_session_as_is(self):
        """Повторяющиеся ключи вида filter[id][] в словарь не уложить —
        список пар должен дойти до aiohttp нетронутым."""
        session = FakeHTTPSession(FakeHTTPResponse(200, {"ok": True}))
        client = AmoCRMAPIClient("https://example.amocrm.ru", "token", session=session)
        pairs = [("filter[id][]", 1), ("filter[id][]", 2)]

        await client.get("/api/v4/leads", params=pairs)

        _url, _headers, params, _timeout = session.calls[0]
        self.assertEqual(params, pairs)


if __name__ == "__main__":
    unittest.main()
