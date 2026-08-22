import json
import unittest

from yandex_nosite.api import (
    AuthError,
    BudgetExhausted,
    QuotaError,
    RequestBudget,
    SearchClient,
    SearchResponse,
    YandexApiError,
)
from yandex_nosite.geo import BBox

AREA = BBox(37.0, 55.0, 38.0, 56.0)


def payload(found=2, count=2):
    return {
        "type": "FeatureCollection",
        "properties": {"ResponseMetaData": {"SearchResponse": {"found": found}}},
        "features": [
            {
                "geometry": {"coordinates": [37.5, 55.5]},
                "properties": {"CompanyMetaData": {"id": str(i), "name": f"Org {i}"}},
            }
            for i in range(count)
        ],
    }


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        status, body = self.responses.pop(0)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        return status, body


def make_client(transport, **kwargs):
    kwargs.setdefault("min_interval", 0)
    kwargs.setdefault("sleep", lambda _: None)
    return SearchClient("key", transport=transport, **kwargs)


class ClientTest(unittest.TestCase):
    def test_requires_api_key(self):
        with self.assertRaises(AuthError):
            SearchClient("")

    def test_successful_search(self):
        client = make_client(FakeTransport([(200, payload(found=7, count=2))]))
        response = client.search("кафе", AREA)
        self.assertEqual(response.found, 7)
        self.assertEqual(len(response.features), 2)
        self.assertEqual(client.stats["requests"], 1)

    def test_request_params_include_bbox_and_rspn(self):
        transport = FakeTransport([(200, payload())])
        client = make_client(transport)
        client.search("кафе", AREA, results=50, skip=100)
        url = transport.urls[0]
        self.assertIn("rspn=1", url)
        self.assertIn("type=biz", url)
        self.assertIn("skip=100", url)
        self.assertIn("bbox=", url)

    def test_results_capped_at_api_maximum(self):
        client = make_client(FakeTransport([(200, payload())]))
        params = client.build_params("кафе", AREA, results=500, skip=0)
        self.assertEqual(params["results"], "50")

    def test_auth_error_on_403(self):
        client = make_client(FakeTransport([(403, {"message": "Invalid key"})]))
        with self.assertRaises(AuthError):
            client.search("кафе", AREA)

    def test_quota_error_detected(self):
        client = make_client(
            FakeTransport([(403, {"message": "Daily limit exceeded"})])
        )
        with self.assertRaises(QuotaError):
            client.search("кафе", AREA)

    def test_retry_on_server_error_then_success(self):
        transport = FakeTransport([(500, b"oops"), (200, payload(found=1, count=1))])
        client = make_client(transport, max_retries=3)
        response = client.search("кафе", AREA)
        self.assertEqual(response.found, 1)
        self.assertEqual(client.stats["retries"], 1)

    def test_gives_up_after_max_retries(self):
        transport = FakeTransport([(503, b"x")] * 3)
        client = make_client(transport, max_retries=2)
        with self.assertRaises(YandexApiError):
            client.search("кафе", AREA)

    def test_bad_request_is_not_retried(self):
        transport = FakeTransport([(400, {"message": "bad bbox"})])
        client = make_client(transport, max_retries=3)
        with self.assertRaises(YandexApiError):
            client.search("кафе", AREA)
        self.assertEqual(len(transport.urls), 1)

    def test_budget_blocks_extra_requests(self):
        transport = FakeTransport([(200, payload())] * 3)
        client = make_client(transport, budget=RequestBudget(limit=2))
        client.search("кафе", AREA)
        client.search("кафе", AREA, skip=50)
        with self.assertRaises(BudgetExhausted):
            client.search("кафе", AREA, skip=100)
        self.assertEqual(len(transport.urls), 2)


class CacheTest(unittest.TestCase):
    class MemoryCache:
        def __init__(self):
            self.data = {}

        def get(self, key):
            return self.data.get(key)

        def put(self, key, value):
            self.data[key] = value

    def test_second_identical_call_served_from_cache(self):
        transport = FakeTransport([(200, payload(found=3, count=1))])
        cache = self.MemoryCache()
        client = make_client(transport, cache=cache)
        first = client.search("кафе", AREA)
        second = client.search("кафе", AREA)
        self.assertEqual(len(transport.urls), 1)
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.found, 3)
        self.assertEqual(client.stats["cache_hits"], 1)

    def test_api_key_not_stored_in_cache_key(self):
        client = make_client(FakeTransport([]), cache=self.MemoryCache())
        key = client._cache_key(client.build_params("кафе", AREA, results=50, skip=0))
        self.assertNotIn("key", json.loads(key))


class ResponseTest(unittest.TestCase):
    def test_missing_metadata_defaults_to_zero(self):
        response = SearchResponse.from_payload({"features": []})
        self.assertEqual(response.found, 0)
        self.assertEqual(response.features, [])

class KeyHintTest(unittest.TestCase):
    """Ключи разных сервисов Яндекса не взаимозаменяемы — ошибка должна это объяснять."""

    def test_cloud_service_account_key_recognized(self):
        from yandex_nosite.api import describe_key_problem

        hint = describe_key_problem("AQVNx000000000000000000000000000000000000")
        self.assertIn("Yandex Cloud", hint)
        self.assertIn("developer.tech.yandex.ru", hint)

    def test_iam_token_recognized(self):
        from yandex_nosite.api import describe_key_problem

        self.assertIn("токен", describe_key_problem("t1.abcdef"))

    def test_uuid_key_gets_activation_hint(self):
        from yandex_nosite.api import describe_key_problem

        hint = describe_key_problem("12345678-1234-1234-1234-123456789abc")
        self.assertIn("активирован", hint)

    def test_hint_included_in_auth_error(self):
        transport = FakeTransport([(403, {"message": "Invalid api key"})])
        client = SearchClient(
            "AQVNxxxx", transport=transport, min_interval=0, sleep=lambda _: None
        )
        with self.assertRaises(AuthError) as ctx:
            client.search("кафе", AREA)
        self.assertIn("Yandex Cloud", str(ctx.exception))


class ErrorMessageTest(unittest.TestCase):
    def test_xml_error_body_is_unwrapped(self):
        from yandex_nosite.api import _error_message

        body = (
            b'<?xml version="1.0" encoding="UTF-8"?><error><statusCode>403</statusCode>'
            b"<error>Forbidden</error><message>Invalid api key</message></error>"
        )
        self.assertEqual(_error_message(body), "Invalid api key")

    def test_json_error_body(self):
        from yandex_nosite.api import _error_message

        self.assertEqual(_error_message(b'{"message": "bad bbox"}'), "bad bbox")

    def test_empty_body(self):
        from yandex_nosite.api import _error_message

        self.assertEqual(_error_message(b""), "нет тела ответа")


if __name__ == "__main__":
    unittest.main()
