import unittest

from yandex_nosite.geo import BBox, iter_grid, resolve_region


class BBoxTest(unittest.TestCase):
    def test_parse_both_separators(self):
        a = BBox.parse("36.83,55.67~38.24,55.91")
        b = BBox.parse("36.83, 55.67, 38.24, 55.91")
        self.assertEqual(a, b)

    def test_parse_normalizes_order(self):
        area = BBox.parse("38.24,55.91~36.83,55.67")
        self.assertEqual(area.lon_min, 36.83)
        self.assertEqual(area.lat_max, 55.91)

    def test_parse_rejects_wrong_arity(self):
        with self.assertRaises(ValueError):
            BBox.parse("36.83,55.67")

    def test_degenerate_box_rejected(self):
        with self.assertRaises(ValueError):
            BBox(37.0, 55.0, 37.0, 55.5)

    def test_split_covers_area_without_overlap(self):
        area = BBox(37.0, 55.0, 38.0, 56.0)
        parts = area.split()
        self.assertEqual(len(parts), 4)
        total = sum(p.width * p.height for p in parts)
        self.assertAlmostEqual(total, area.width * area.height)
        for part in parts:
            self.assertTrue(area.contains(*part.center))

    def test_from_center_radius(self):
        area = BBox.from_center(37.6, 55.75, 5)
        width_km, height_km = area.km_size()
        self.assertAlmostEqual(height_km, 10, places=1)
        self.assertAlmostEqual(width_km, 10, places=1)

    def test_from_center_rejects_bad_radius(self):
        with self.assertRaises(ValueError):
            BBox.from_center(37.6, 55.75, 0)

    def test_to_api_format(self):
        self.assertEqual(
            BBox(37.0, 55.0, 38.0, 56.0).to_api(),
            "37.000000,55.000000~38.000000,56.000000",
        )

    def test_ll_spn_is_center_and_extent(self):
        ll, spn = BBox(37.0, 55.0, 38.0, 56.0).to_ll_spn()
        self.assertEqual(ll, "37.500000,55.500000")
        self.assertEqual(spn, "1.000000,1.000000")

    def test_grid_tiles_count(self):
        tiles = list(iter_grid(BBox(37.0, 55.0, 38.0, 56.0), 3, 4))
        self.assertEqual(len(tiles), 12)

    def test_resolve_region_by_slug_and_title(self):
        self.assertEqual(resolve_region("moscow"), resolve_region("Москва"))
        with self.assertRaises(KeyError):
            resolve_region("атлантида")


if __name__ == "__main__":
    unittest.main()
