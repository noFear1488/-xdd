import unittest

from yandex_nosite.api import SearchClient
from yandex_nosite.demo import DemoTransport
from yandex_nosite.geo import BBox
from yandex_nosite.models import parse_feature
from yandex_nosite.pipeline import ScanConfig, scan
from yandex_nosite.scoring import score
from yandex_nosite.sitecheck import NO_SITE, OWN_SITE, SOCIAL_ONLY, STRICT_LEAD_STATUSES

MOSCOW = BBox(37.32, 55.55, 37.90, 55.92)


def make_client(per_query=240):
    return SearchClient(
        "demo", transport=DemoTransport(per_query=per_query), min_interval=0
    )


class ParseFeatureTest(unittest.TestCase):
    def test_parses_full_card(self):
        feature = {
            "geometry": {"coordinates": [37.6, 55.7]},
            "properties": {
                "CompanyMetaData": {
                    "id": "42",
                    "name": "Кафе",
                    "address": "Москва, ул. Мира, 1",
                    "url": "https://vk.com/cafe",
                    "Phones": [{"formatted": "+7 495 000-00-00"}],
                    "Categories": [{"name": "Кафе"}, {"name": "Кофейня"}],
                    "Hours": {"text": "круглосуточно"},
                    "Rating": {"score": 4.6, "reviews": 31},
                }
            },
        }
        business = parse_feature(feature, query="кафе")
        self.assertEqual(business.company_id, "42")
        self.assertEqual(business.phone, "+7 495 000-00-00")
        self.assertEqual(business.categories, ["Кафе", "Кофейня"])
        self.assertEqual(business.rating, 4.6)
        self.assertEqual(business.reviews, 31)
        self.assertEqual(business.query, "кафе")

    def test_skips_non_company_feature(self):
        self.assertIsNone(parse_feature({"properties": {}}))

    def test_synthesizes_id_when_missing(self):
        business = parse_feature(
            {
                "geometry": {"coordinates": [37.6, 55.7]},
                "properties": {"CompanyMetaData": {"name": "Без ID"}},
            }
        )
        self.assertTrue(business.company_id.startswith("noid:"))


class ScoringTest(unittest.TestCase):
    def _business(self, **kwargs):
        from yandex_nosite.models import Business

        defaults = dict(company_id="1", name="X", site_status=NO_SITE, phones=["+7"])
        defaults.update(kwargs)
        return Business(**defaults)

    def test_no_site_scores_above_social(self):
        no_site, _ = score(self._business(site_status=NO_SITE))
        social, _ = score(self._business(site_status=SOCIAL_ONLY))
        self.assertGreater(no_site, social)

    def test_own_site_scores_low(self):
        value, _ = score(self._business(site_status=OWN_SITE))
        self.assertLess(value, 30)

    def test_missing_phone_penalized(self):
        with_phone, _ = score(self._business(phones=["+7"]))
        without, _ = score(self._business(phones=[]))
        self.assertGreater(with_phone, without)

    def test_closed_business_drops(self):
        alive, _ = score(self._business(reviews=30))
        closed, _ = score(self._business(reviews=30, closed=True))
        self.assertGreater(alive, closed)

    def test_dead_site_raises_priority(self):
        base, _ = score(self._business(site_status=SOCIAL_ONLY))
        dead, reasons = score(self._business(site_status=SOCIAL_ONLY), site_alive="dead")
        self.assertGreater(dead, base)
        self.assertIn("указанный сайт не открывается", reasons)

    def test_score_bounds(self):
        value, _ = score(
            self._business(reviews=500, rating=5.0, hours="24/7", categories=["стоматология", "клиника"])
        )
        self.assertLessEqual(value, 100)
        self.assertGreaterEqual(value, 0)


class ScanTest(unittest.TestCase):
    def test_finds_leads_and_filters_sites(self):
        client = make_client()
        config = ScanConfig(queries=["кафе"], area=MOSCOW, max_depth=3)
        result = scan(client, config)

        self.assertGreater(len(result.leads), 0)
        self.assertGreater(result.with_site, 0)
        self.assertTrue(all(b.site_status != OWN_SITE for b in result.leads))

    def test_deduplicates_across_tiles(self):
        client = make_client()
        result = scan(client, ScanConfig(queries=["кафе"], area=MOSCOW, max_depth=3))
        ids = [b.company_id for b in result.leads]
        self.assertEqual(len(ids), len(set(ids)))

    def test_grid_recovers_more_than_one_page(self):
        """Главная проверка: 50 объектов на запрос — не потолок для области."""
        client = make_client(per_query=240)
        result = scan(client, ScanConfig(queries=["кафе"], area=MOSCOW, max_depth=4))
        self.assertGreater(result.seen_total, 50)
        self.assertGreater(result.tiles_visited, 1)

    def test_shallow_scan_misses_data_deep_scan_finds(self):
        shallow = scan(
            make_client(), ScanConfig(queries=["кафе"], area=MOSCOW, max_depth=0, deep_paging=False)
        )
        deep = scan(make_client(), ScanConfig(queries=["кафе"], area=MOSCOW, max_depth=4))
        self.assertLess(shallow.seen_total, deep.seen_total)

    def test_strict_mode_keeps_only_empty_url(self):
        result = scan(
            make_client(),
            ScanConfig(
                queries=["кафе"],
                area=MOSCOW,
                max_depth=2,
                lead_statuses=STRICT_LEAD_STATUSES,
            ),
        )
        self.assertTrue(all(b.site_status == NO_SITE for b in result.leads))
        self.assertTrue(all(not b.url for b in result.leads))

    def test_results_sorted_by_score(self):
        result = scan(make_client(), ScanConfig(queries=["кафе"], area=MOSCOW, max_depth=2))
        scores = [b.lead_score for b in result.leads]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_limit_stops_early(self):
        result = scan(
            make_client(), ScanConfig(queries=["кафе"], area=MOSCOW, max_depth=4, limit=15)
        )
        self.assertLessEqual(len(result.leads), 15)

    def test_min_score_filter(self):
        result = scan(
            make_client(), ScanConfig(queries=["кафе"], area=MOSCOW, max_depth=2, min_score=70)
        )
        self.assertTrue(all(b.lead_score >= 70 for b in result.leads))

    def test_budget_exhaustion_returns_partial_result(self):
        from yandex_nosite.api import RequestBudget

        client = SearchClient(
            "demo",
            transport=DemoTransport(),
            min_interval=0,
            budget=RequestBudget(limit=3),
        )
        result = scan(client, ScanConfig(queries=["кафе", "бар"], area=MOSCOW, max_depth=4))
        self.assertTrue(result.stopped_early)
        self.assertGreater(len(result.leads), 0)

    def test_seen_total_counts_each_company_once(self):
        """Соседние тайлы возвращают одни и те же карточки — считаем их один раз."""
        result = scan(
            make_client(per_query=240), ScanConfig(queries=["кафе"], area=MOSCOW, max_depth=4)
        )
        self.assertLessEqual(result.seen_total, 240)
        self.assertEqual(result.seen_total, len(result.leads) + result.with_site)

    def test_score_spread_not_saturated(self):
        """Верх списка должен ранжироваться, а не состоять из одинаковых сотен."""
        result = scan(make_client(), ScanConfig(queries=["кафе"], area=MOSCOW, max_depth=3))
        top = [b.lead_score for b in result.leads[:20]]
        self.assertGreater(len(set(top)), 1)
        self.assertLess(max(top), 100)

    def test_multiple_queries_recorded(self):
        result = scan(
            make_client(), ScanConfig(queries=["кафе", "бар"], area=MOSCOW, max_depth=1)
        )
        self.assertEqual(set(result.per_query), {"кафе", "бар"})


class DedupeTest(unittest.TestCase):
    """В OSM один бизнес часто есть и точкой, и контуром здания."""

    def _b(self, name, phone="+7 495 111-22-33", **kwargs):
        from yandex_nosite.models import Business

        return Business(
            company_id=kwargs.pop("cid", name + phone),
            name=name,
            phones=[phone] if phone else [],
            **kwargs,
        )

    def test_same_name_and_phone_collapsed(self):
        from yandex_nosite.pipeline import dedupe_similar

        items = [self._b("Лесной городок", cid="1"), self._b("Лесной городок", cid="2")]
        self.assertEqual(len(dedupe_similar(items)), 1)

    def test_richer_record_wins(self):
        from yandex_nosite.pipeline import dedupe_similar

        poor = self._b("Клиника", cid="1")
        rich = self._b("Клиника", cid="2", address="ул. Ленина 1", hours="9-18")
        self.assertEqual(dedupe_similar([poor, rich])[0].address, "ул. Ленина 1")

    def test_same_name_different_phone_kept(self):
        from yandex_nosite.pipeline import dedupe_similar

        items = [self._b("Аптека", "+7 1"), self._b("Аптека", "+7 2")]
        self.assertEqual(len(dedupe_similar(items)), 2)

    def test_name_whitespace_and_case_normalized(self):
        from yandex_nosite.pipeline import dedupe_similar

        items = [self._b("Салон  Красоты"), self._b("салон красоты")]
        self.assertEqual(len(dedupe_similar(items)), 1)

    def test_phone_formatting_ignored(self):
        from yandex_nosite.pipeline import dedupe_similar

        items = [self._b("Кафе", "+7 (495) 111-22-33"), self._b("Кафе", "+74951112233")]
        self.assertEqual(len(dedupe_similar(items)), 1)

    def test_order_preserved(self):
        from yandex_nosite.pipeline import dedupe_similar

        items = [self._b("Б"), self._b("А"), self._b("Б")]
        self.assertEqual([b.name for b in dedupe_similar(items)], ["Б", "А"])


if __name__ == "__main__":
    unittest.main()
