import json
import unittest
import urllib.error

from yandex_nosite.geo import BBox
from yandex_nosite.osm import (
    OverpassClient,
    OverpassError,
    category_of,
    is_chain,
    osm_url,
    to_business,
)
from yandex_nosite.pipeline import ScanConfig, scan_osm
from yandex_nosite.sitecheck import NO_SITE, OWN_SITE, SOCIAL_ONLY

AREA = BBox(37.50, 55.40, 37.62, 55.47)


def element(**tags):
    base = {
        "type": "node",
        "id": tags.pop("_id", 1),
        "lat": 55.42,
        "lon": 37.55,
        "tags": tags,
    }
    return base


def payload(elements, base="2026-08-22T07:10:04Z"):
    return json.dumps(
        {"osm3s": {"timestamp_osm_base": base}, "elements": elements}
    ).encode()


class FakeOpener:
    """Подменяет HTTP: возвращает заготовленные ответы по порядку зеркал."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        item = self.responses.pop(0) if self.responses else b'{"elements":[]}'
        if isinstance(item, Exception):
            raise item
        return item


class ToBusinessTest(unittest.TestCase):
    def test_maps_tags_to_business(self):
        b = to_business(
            element(
                name="Мастерская",
                shop="car_repair",
                phone="+7 495 000-00-00",
                **{"addr:street": "Ленина", "addr:housenumber": "5", "addr:city": "Подольск"},
                opening_hours="Mo-Fr 09:00-18:00",
            )
        )
        self.assertEqual(b.name, "Мастерская")
        self.assertEqual(b.phones, ["+7 495 000-00-00"])
        self.assertEqual(b.categories, ["автосервис"])
        self.assertEqual(b.address, "Подольск Ленина 5")
        self.assertEqual(b.hours, "Mo-Fr 09:00-18:00")
        self.assertTrue(b.company_id.startswith("osm:node/"))

    def test_website_variants_recognized(self):
        for key in ("website", "contact:website", "url"):
            b = to_business(element(name="X", shop="bakery", **{key: "https://x.ru"}))
            self.assertEqual(b.url, "https://x.ru", key)

    def test_social_tags_become_links(self):
        b = to_business(element(name="X", shop="bakery", **{"contact:vk": "salon"}))
        self.assertEqual(b.links, ["https://vk.com/salon"])

    def test_social_tag_with_full_url_kept(self):
        b = to_business(
            element(name="X", shop="bakery", **{"contact:telegram": "https://t.me/x"})
        )
        self.assertEqual(b.links, ["https://t.me/x"])

    def test_multiple_phones_split(self):
        b = to_business(element(name="X", shop="bakery", phone="+7 1; +7 2"))
        self.assertEqual(b.phones, ["+7 1", "+7 2"])

    def test_unnamed_element_skipped(self):
        self.assertIsNone(to_business(element(shop="bakery")))

    def test_infrastructure_skipped(self):
        self.assertIsNone(to_business(element(name="Парковка", amenity="parking")))
        self.assertIsNone(to_business(element(name="Школа №1", amenity="school")))

    def test_way_center_used_as_coordinates(self):
        el = {
            "type": "way",
            "id": 7,
            "center": {"lat": 55.43, "lon": 37.56},
            "tags": {"name": "Кафе", "amenity": "cafe"},
        }
        b = to_business(el)
        self.assertEqual((b.lat, b.lon), (55.43, 37.56))
        self.assertEqual(osm_url(b), "https://www.openstreetmap.org/way/7")

    def test_chain_is_flagged(self):
        b = to_business(
            element(name="Л'Этуаль", shop="cosmetics", **{"brand:wikidata": "Q123"})
        )
        self.assertIn("сетевая точка", b.categories)
        self.assertTrue(is_chain({"brand:wikidata": "Q1"}))
        self.assertFalse(is_chain({"brand": "Своя лавка"}))

    def test_category_titles_translated(self):
        self.assertEqual(category_of({"shop": "hairdresser"})[1], "парикмахерская")
        self.assertEqual(category_of({"amenity": "dentist"})[1], "стоматология")
        # Незнакомое значение остаётся как есть, но без подчёркиваний.
        self.assertEqual(category_of({"shop": "wool_yarn"})[1], "wool yarn")


class OverpassClientTest(unittest.TestCase):
    def test_query_returns_elements(self):
        opener = FakeOpener([payload([element(name="X", shop="bakery")])])
        client = OverpassClient(["https://a/api"], opener=opener, sleep=lambda _: None)
        self.assertEqual(len(client.query("[out:json];")), 1)
        self.assertEqual(client.stats.requests, 1)

    def test_falls_back_to_next_mirror(self):
        opener = FakeOpener([OSError("сброс соединения"), payload([element(name="X", shop="bakery")])])
        client = OverpassClient(
            ["https://dead/api", "https://alive/api"], opener=opener, sleep=lambda _: None
        )
        elements = client.query("[out:json];")
        self.assertEqual(len(elements), 1)
        self.assertIn("dead", client.stats.mirror_failures[0])
        self.assertIn("alive", client.stats.mirror_used)

    def test_mirror_with_empty_database_rejected(self):
        """Зеркало может ответить 200 с мусорным timestamp и пустой базой."""
        opener = FakeOpener(
            [payload([], base="116576"), payload([element(name="X", shop="bakery")])]
        )
        client = OverpassClient(
            ["https://broken/api", "https://good/api"], opener=opener, sleep=lambda _: None
        )
        self.assertEqual(len(client.query("[out:json];")), 1)
        self.assertIn("broken", client.stats.mirror_failures[0])

    def test_all_mirrors_down(self):
        opener = FakeOpener([OSError("x"), OSError("y")])
        client = OverpassClient(
            ["https://a/api", "https://b/api"], opener=opener, sleep=lambda _: None
        )
        with self.assertRaises(OverpassError):
            client.query("[out:json];")

    def test_bbox_order_is_south_west_north_east(self):
        opener = FakeOpener([payload([])] * 7)
        client = OverpassClient(["https://a/api"], opener=opener, sleep=lambda _: None)
        client.fetch_area(AREA, keys=["shop"])
        self.assertIn("55.400000%2C37.500000%2C55.470000%2C37.620000", opener.urls[0])

    def test_fetch_area_deduplicates_across_groups(self):
        same = element(name="X", shop="bakery", amenity="cafe")
        opener = FakeOpener([payload([same]), payload([same])])
        client = OverpassClient(["https://a/api"], opener=opener, sleep=lambda _: None)
        result = client.fetch_area(AREA, keys=["shop", "amenity"])
        self.assertEqual(len(result), 1)


class ScanOsmTest(unittest.TestCase):
    def _client(self, elements):
        opener = FakeOpener([payload(elements)] * 8)
        return OverpassClient(["https://a/api"], opener=opener, sleep=lambda _: None)

    def test_filters_out_businesses_with_site(self):
        client = self._client(
            [
                element(_id=1, name="Без сайта", shop="bakery", phone="+7 1"),
                element(_id=2, name="С сайтом", shop="bakery", website="https://x.ru"),
                element(_id=3, name="ВК", shop="bakery", **{"contact:vk": "x"}),
            ]
        )
        result = scan_osm(client, ScanConfig(queries=["osm"], area=AREA, region="test"))
        statuses = {b.name: b.site_status for b in result.leads}
        self.assertEqual(statuses.get("Без сайта"), NO_SITE)
        self.assertEqual(statuses.get("ВК"), SOCIAL_ONLY)
        self.assertNotIn("С сайтом", statuses)
        self.assertEqual(result.with_site, 1)

    def test_chain_scores_lower_than_independent(self):
        client = self._client(
            [
                element(_id=1, name="Своя пекарня", shop="bakery", phone="+7 1"),
                element(
                    _id=2,
                    name="Сетевая",
                    shop="bakery",
                    phone="+7 2",
                    **{"brand:wikidata": "Q1"},
                ),
            ]
        )
        result = scan_osm(client, ScanConfig(queries=["osm"], area=AREA))
        scores = {b.name: b.lead_score for b in result.leads}
        self.assertGreater(scores["Своя пекарня"], scores["Сетевая"])

    def test_results_sorted_and_limited(self):
        elements = [
            element(_id=i, name=f"Точка {i}", shop="bakery", phone="+7 1")
            for i in range(1, 6)
        ]
        result = scan_osm(
            self._client(elements), ScanConfig(queries=["osm"], area=AREA, limit=3)
        )
        self.assertEqual(len(result.leads), 3)
        scores = [b.lead_score for b in result.leads]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_unavailable_overpass_reported_not_raised(self):
        client = OverpassClient(
            ["https://a/api"],
            opener=FakeOpener([OSError("нет связи")] * 8),
            sleep=lambda _: None,
        )
        result = scan_osm(client, ScanConfig(queries=["osm"], area=AREA))
        self.assertEqual(result.leads, [])
        self.assertEqual(result.seen_total, 0)

    def test_min_score_filter(self):
        client = self._client(
            [
                element(_id=1, name="С телефоном", shop="bakery", phone="+7 1"),
                element(_id=2, name="Без телефона", shop="bakery"),
            ]
        )
        result = scan_osm(
            client, ScanConfig(queries=["osm"], area=AREA, min_score=50)
        )
        names = [b.name for b in result.leads]
        self.assertIn("С телефоном", names)
        self.assertNotIn("Без телефона", names)


def busy(code=429):
    return urllib.error.HTTPError("https://a/api", code, "Too Many Requests", {}, None)


class MirrorResilienceTest(unittest.TestCase):
    """Перегруженное зеркало нужно переждать, а не списывать со счетов."""

    def test_retries_on_429_then_succeeds(self):
        opener = FakeOpener([busy(429), busy(429), payload([element(name="X", shop="bakery")])])
        client = OverpassClient(["https://a/api"], opener=opener, sleep=lambda _: None)
        self.assertEqual(len(client.query("[out:json];")), 1)
        self.assertEqual(client.stats.retries, 2)
        self.assertEqual(client.stats.mirror_failures, [])

    def test_busy_mirror_not_marked_dead(self):
        """Главный дефект: одна ошибка 429 не должна ломать весь прогон."""
        opener = FakeOpener(
            [busy(504)] * 4 + [payload([element(name="X", shop="bakery")])]
        )
        client = OverpassClient(
            ["https://a/api", "https://b/api"], opener=opener, sleep=lambda _: None
        )
        client.query("[out:json];")  # первый запрос ушёл на второе зеркало
        opener.responses = [payload([element(name="Y", shop="bakery")])]
        # Первое зеркало по-прежнему в строю: следующий запрос снова пробует его.
        self.assertEqual(len(client.query("[out:json];")), 1)
        self.assertNotIn("a", client._dead)

    def test_network_failure_marks_mirror_dead(self):
        opener = FakeOpener([OSError("сеть недоступна"), payload([])])
        client = OverpassClient(
            ["https://dead/api", "https://alive/api"], opener=opener, sleep=lambda _: None
        )
        client.query("[out:json];")
        self.assertIn("dead", client._dead)

    def test_failed_group_recorded(self):
        opener = FakeOpener([OSError("x")] * 4)
        client = OverpassClient(["https://a/api"], opener=opener, sleep=lambda _: None)
        client.fetch_area(AREA, keys=["shop"])
        self.assertTrue(client.stats.failed_groups)


class PickupPointTest(unittest.TestCase):
    def test_marketplace_pickup_points_skipped(self):
        """ПВЗ Wildberries и подобные — точки сетей, а не лиды."""
        self.assertIsNone(
            to_business(element(name="Wildberries", shop="outpost", brand="Wildberries"))
        )
        self.assertIsNone(to_business(element(name="Постамат", amenity="parcel_locker")))


if __name__ == "__main__":
    unittest.main()
