import unittest

from yandex_nosite import sitecheck as sc


class ClassifyTest(unittest.TestCase):
    def test_empty_url_is_no_site(self):
        for value in ["", "   ", "-", "нет", None]:
            status, domain = sc.classify(value or "", [])
            self.assertEqual(status, sc.NO_SITE)
            self.assertEqual(domain, "")

    def test_social_networks(self):
        for url in [
            "https://vk.com/club123",
            "vk.com/beauty",
            "https://t.me/salon",
            "https://www.instagram.com/salon/",
            "https://taplink.cc/salon",
        ]:
            self.assertEqual(sc.classify(url, [])[0], sc.SOCIAL_ONLY, url)

    def test_aggregators(self):
        for url in ["https://zoon.ru/msk/place_1/", "https://yandex.ru/maps/org/1"]:
            self.assertEqual(sc.classify(url, [])[0], sc.AGGREGATOR_ONLY, url)

    def test_builders(self):
        for url in ["https://salon.tilda.ws", "http://mysite.business.site"]:
            self.assertEqual(sc.classify(url, [])[0], sc.BUILDER, url)

    def test_own_site(self):
        status, domain = sc.classify("https://www.salon-krasoty.ru/about", [])
        self.assertEqual(status, sc.OWN_SITE)
        self.assertEqual(domain, "salon-krasoty.ru")

    def test_strongest_link_wins(self):
        # Есть и соцсеть, и собственный сайт — это не наш лид.
        status, _ = sc.classify("https://vk.com/club1", ["https://salon.ru"])
        self.assertEqual(status, sc.OWN_SITE)

    def test_social_only_when_all_links_social(self):
        status, _ = sc.classify("", ["https://vk.com/club1", "https://t.me/x"])
        self.assertEqual(status, sc.SOCIAL_ONLY)

    def test_registrable_domain_multipart_suffix(self):
        self.assertEqual(sc.registrable_domain("shop.example.com.ru"), "example.com.ru")
        self.assertEqual(sc.registrable_domain("a.b.example.ru"), "example.ru")

    def test_normalize_url_adds_scheme(self):
        self.assertEqual(sc.normalize_url("example.ru/x"), "http://example.ru/x")

    def test_normalize_url_rejects_garbage(self):
        self.assertEqual(sc.normalize_url("mailto:a@b.ru"), "")
        self.assertEqual(sc.normalize_url("localhost"), "")

    def test_is_lead_default_and_strict(self):
        self.assertTrue(sc.is_lead(sc.SOCIAL_ONLY))
        self.assertFalse(sc.is_lead(sc.OWN_SITE))
        self.assertFalse(sc.is_lead(sc.SOCIAL_ONLY, sc.STRICT_LEAD_STATUSES))


if __name__ == "__main__":
    unittest.main()
