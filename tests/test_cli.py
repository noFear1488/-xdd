import io
import contextlib
import tempfile
import unittest
from pathlib import Path

from yandex_nosite import cli
from yandex_nosite.geo import BBox
from yandex_nosite.presets import PRESETS, resolve_queries


def run(argv):
    """Запускает CLI, возвращает (код возврата, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:  # argparse выходит так на неверных аргументах
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


class PresetTest(unittest.TestCase):
    def test_preset_resolution_dedupes(self):
        queries = resolve_queries("quick", ["салон красоты", "новый запрос"])
        self.assertEqual(len(queries), len(set(queries)))
        self.assertIn("новый запрос", queries)

    def test_all_preset_contains_every_group(self):
        for name, group in PRESETS.items():
            if name == "all":
                continue
            for query in group:
                self.assertIn(query, PRESETS["all"])

    def test_unknown_preset_raises(self):
        with self.assertRaises(KeyError):
            resolve_queries("нет-такого", None)


class AreaTest(unittest.TestCase):
    def _args(self, **kwargs):
        import argparse

        defaults = dict(region=None, bbox=None, center=None, radius=3.0)
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_bbox_wins(self):
        area, name = cli.resolve_area(self._args(bbox="37.0,55.0~38.0,56.0"))
        self.assertEqual(area, BBox(37.0, 55.0, 38.0, 56.0))
        self.assertEqual(name, "bbox")

    def test_center_and_radius(self):
        area, _ = cli.resolve_area(self._args(center="37.6,55.75", radius=2))
        width, height = area.km_size()
        self.assertAlmostEqual(height, 4, places=1)

    def test_region_lookup(self):
        area, name = cli.resolve_area(self._args(region="kazan"))
        self.assertEqual(name, "kazan")
        self.assertTrue(area.contains(49.12, 55.79))

    def test_missing_area_is_error(self):
        with self.assertRaises(cli.CliError):
            cli.resolve_area(self._args())

    def test_unknown_region_is_error(self):
        with self.assertRaises(cli.CliError):
            cli.resolve_area(self._args(region="атлантида"))


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_regions_listing(self):
        code, out, _ = run(["regions"])
        self.assertEqual(code, 0)
        self.assertIn("moscow", out)
        self.assertIn("Казань", out)

    def test_presets_listing(self):
        code, out, _ = run(["presets"])
        self.assertEqual(code, 0)
        self.assertIn("beauty", out)

    def test_check_classifies_urls(self):
        code, out, _ = run(["check", "https://vk.com/x", "https://salon.ru"])
        self.assertEqual(code, 0)
        self.assertIn("только соцсети", out)
        self.assertIn("свой сайт", out)

    def test_scan_without_key_fails_clearly(self):
        import os

        saved = os.environ.pop(cli.ENV_KEY, None)
        try:
            code, _, err = run(
                ["scan", "--region", "kazan", "--db", str(self.path / "x.db")]
            )
        finally:
            if saved is not None:
                os.environ[cli.ENV_KEY] = saved
        self.assertEqual(code, 2)
        self.assertIn("ключ", err)

    def test_demo_scan_writes_csv_and_db(self):
        out_file = self.path / "leads.csv"
        db_file = self.path / "demo.db"
        code, out, _ = run(
            [
                "scan",
                "--demo",
                "--region", "moscow",
                "--query", "кафе",
                "--db", str(db_file),
                "--out", str(out_file),
                "--max-depth", "2",
                "--quiet",
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue(out_file.is_file())
        self.assertGreater(out_file.stat().st_size, 200)
        self.assertTrue(db_file.is_file())
        self.assertIn("Найдено лидов", out)

    def test_demo_scan_does_not_touch_quota_counter(self):
        db_file = self.path / "demo.db"
        run(
            ["scan", "--demo", "--region", "moscow", "--query", "кафе",
             "--db", str(db_file), "--max-depth", "1", "--quiet"]
        )
        code, out, _ = run(["stats", "--db", str(db_file)])
        self.assertEqual(code, 0)
        self.assertNotIn("Расход запросов", out)

    def test_export_after_demo_scan(self):
        db_file = self.path / "demo.db"
        run(
            ["scan", "--demo", "--region", "moscow", "--query", "кафе",
             "--db", str(db_file), "--max-depth", "2", "--quiet"]
        )
        target = self.path / "export.xlsx"
        code, out, _ = run(["export", "--db", str(db_file), "--out", str(target)])
        self.assertEqual(code, 0)
        self.assertTrue(target.is_file())
        self.assertIn("выгружено", out)

    def test_export_empty_db_reports_nothing(self):
        code, _, err = run(
            ["export", "--db", str(self.path / "empty.db"), "--out", str(self.path / "e.csv")]
        )
        self.assertEqual(code, 1)
        self.assertIn("нет записей", err)

    def test_plan_estimates_requests(self):
        code, out, _ = run(
            ["plan", "--demo", "--region", "moscow", "--query", "кафе",
             "--db", str(self.path / "p.db")]
        )
        self.assertEqual(code, 0)
        self.assertIn("ИТОГО", out)

    def test_strict_flag_narrows_statuses(self):
        import argparse

        strict = cli.resolve_statuses(argparse.Namespace(statuses=None, strict=True))
        default = cli.resolve_statuses(argparse.Namespace(statuses=None, strict=False))
        self.assertEqual(strict, ("none",))
        self.assertGreater(len(default), len(strict))

    def test_bad_status_rejected(self):
        import argparse

        with self.assertRaises(cli.CliError):
            cli.resolve_statuses(argparse.Namespace(statuses="wat", strict=False))


class EstimateTest(unittest.TestCase):
    def test_small_result_needs_one_request(self):
        self.assertEqual(cli.estimate_requests(12, 50, 4), 1)

    def test_estimate_grows_with_volume(self):
        small = cli.estimate_requests(200, 50, 4)
        big = cli.estimate_requests(5000, 50, 4)
        self.assertGreater(big, small)

    def test_estimate_respects_depth_cap(self):
        capped = cli.estimate_requests(10**6, 50, 2)
        deeper = cli.estimate_requests(10**6, 50, 4)
        self.assertLess(capped, deeper)


if __name__ == "__main__":
    unittest.main()
