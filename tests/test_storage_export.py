import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from yandex_nosite.export import write, write_csv, write_json, write_xlsx
from yandex_nosite.models import Business
from yandex_nosite.sitecheck import NO_SITE, SOCIAL_ONLY
from yandex_nosite.storage import Storage


def business(company_id="1", name="Кафе «Уют»", **kwargs):
    defaults = dict(
        address="Москва, ул. Мира, 1",
        lon=37.6,
        lat=55.7,
        phones=["+7 495 000-00-00"],
        categories=["Кафе"],
        site_status=NO_SITE,
        lead_score=80,
        score_reasons=["сайта нет вообще"],
        reviews=12,
        rating=4.5,
    )
    defaults.update(kwargs)
    return Business(company_id=company_id, name=name, **defaults)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Storage(Path(self.dir.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def test_upsert_new_then_update(self):
        self.assertTrue(self.db.upsert(business(), "moscow"))
        self.assertFalse(self.db.upsert(business(name="Кафе «Уют» 2"), "moscow"))
        self.assertEqual(self.db.total(), 1)
        stored = next(self.db.iter_businesses())
        self.assertEqual(stored.name, "Кафе «Уют» 2")

    def test_save_many_counts(self):
        new, updated = self.db.save_many([business("1"), business("2")], "moscow")
        self.assertEqual((new, updated), (2, 0))
        new, updated = self.db.save_many([business("2"), business("3")], "moscow")
        self.assertEqual((new, updated), (1, 1))

    def test_roundtrip_preserves_lists(self):
        self.db.upsert(
            business(phones=["+7 1", "+7 2"], categories=["Кафе", "Бар"], links=["https://vk.com/x"])
        )
        stored = next(self.db.iter_businesses())
        self.assertEqual(stored.phones, ["+7 1", "+7 2"])
        self.assertEqual(stored.categories, ["Кафе", "Бар"])
        self.assertEqual(stored.links, ["https://vk.com/x"])

    def test_filters(self):
        self.db.save_many(
            [
                business("1", lead_score=90, site_status=NO_SITE),
                business("2", lead_score=40, site_status=SOCIAL_ONLY),
            ],
            "moscow",
        )
        self.assertEqual(len(list(self.db.iter_businesses(min_score=50))), 1)
        self.assertEqual(len(list(self.db.iter_businesses(statuses=[SOCIAL_ONLY]))), 1)
        self.assertEqual(len(list(self.db.iter_businesses(region="spb"))), 0)
        self.assertEqual(len(list(self.db.iter_businesses(limit=1))), 1)

    def test_sorted_by_score_desc(self):
        self.db.save_many([business("1", lead_score=30), business("2", lead_score=95)])
        self.assertEqual([b.lead_score for b in self.db.iter_businesses()], [95, 30])

    def test_counts_by_status(self):
        self.db.save_many([business("1"), business("2", site_status=SOCIAL_ONLY)])
        self.assertEqual(self.db.counts_by_status(), {NO_SITE: 1, SOCIAL_ONLY: 1})

    def test_cache_put_get(self):
        self.db.put("key", {"a": 1})
        self.assertEqual(self.db.get("key"), {"a": 1})
        self.assertIsNone(self.db.get("missing"))

    def test_cache_expires(self):
        db = Storage(Path(self.dir.name) / "ttl.db", cache_ttl_days=1)
        db.put("k", {"a": 1})
        db.conn.execute("UPDATE api_cache SET created_at = '2000-01-01T00:00:00+00:00'")
        db.conn.commit()
        self.assertIsNone(db.get("k"))
        db.close()

    def test_usage_accumulates(self):
        self.db.record_requests(5, "2026-08-21")
        self.db.record_requests(7, "2026-08-21")
        self.assertEqual(self.db.requests_today("2026-08-21"), 12)
        self.assertEqual(self.db.usage_history()[0], ("2026-08-21", 12))


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)
        self.items = [business("1"), business("2", name="Бар «Ритм»", site_status=SOCIAL_ONLY)]

    def tearDown(self):
        self.dir.cleanup()

    def test_csv_has_header_and_rows(self):
        target = self.path / "out.csv"
        self.assertEqual(write_csv(self.items, target), 2)
        text = target.read_text(encoding="utf-8-sig")
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("Название", lines[0])
        self.assertIn("Кафе «Уют»", text)
        self.assertIn("сайта нет", text)

    def test_json_roundtrip(self):
        target = self.path / "out.json"
        write_json(self.items, target)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["name"], "Кафе «Уют»")
        self.assertIn("maps_url", data[0])

    def test_xlsx_is_valid_zip_with_sheet(self):
        target = self.path / "out.xlsx"
        write_xlsx(self.items, target)
        with zipfile.ZipFile(target) as book:
            names = book.namelist()
            self.assertIn("xl/worksheets/sheet1.xml", names)
            self.assertIn("[Content_Types].xml", names)
            sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("Кафе «Уют»", sheet)
        self.assertIn("Название", sheet)

    def test_xlsx_escapes_special_characters(self):
        target = self.path / "esc.xlsx"
        write_xlsx([business("1", name='Кафе <&> "Уют"\x07')], target)
        with zipfile.ZipFile(target) as book:
            sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("&lt;&amp;&gt;", sheet)
        self.assertNotIn("\x07", sheet)

    def test_format_from_extension(self):
        target = self.path / "auto.json"
        write(self.items, target)
        self.assertTrue(json.loads(target.read_text(encoding="utf-8")))

    def test_unknown_format_rejected(self):
        with self.assertRaises(ValueError):
            write(self.items, self.path / "x.pdf")


if __name__ == "__main__":
    unittest.main()
