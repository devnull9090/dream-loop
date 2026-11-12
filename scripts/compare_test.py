"""Smoke tests for compare.py on synthetic images. Run: python -m unittest scripts/compare_test.py"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import compare  # noqa: E402


def scene(size=(1920, 1080)):
    """A frame with different colour and line detail in every part, so each cell is distinguishable."""
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for x in range(0, size[0], 16):
        for y in range(0, size[1], 16):
            draw.rectangle((x, y, x + 15, y + 15), fill=(60 + x * 120 // size[0], 90 + y * 100 // size[1], 80 + (x * 7 + y * 3) % 90))
    return image


class CompareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.target = self.save("target.png", scene())

    def save(self, name, image):
        path = self.dir / name
        image.save(path)
        return path

    def run_compare(self, current, **options):
        return compare.compare(self.target, current, out_dir=self.dir / "review", **options)

    def test_identical_frames_report_no_difference_and_capped_sheets(self):
        metrics = self.run_compare(self.target, max_edge=1024)
        self.assertEqual(metrics["frame_difference"], 0)
        self.assertEqual(metrics["frame"], {"luma": 0.0, "colour": 0.0, "detail_ratio": 1.0})
        for sheet in metrics["sheets"].values():
            with Image.open(sheet["file"]) as image:
                self.assertLessEqual(max(image.size), 1024)
                self.assertEqual(list(image.size), sheet["size"])
        defaults = self.run_compare(self.target)["sheets"]
        self.assertLessEqual(max(defaults["compare"]["size"]), 1024)
        self.assertGreater(max(defaults["probes"]["size"]), 1024)
        self.assertLessEqual(max(defaults["probes"]["size"]), 1568)
        self.assertEqual(json.loads((self.dir / "review" / "metrics.json").read_text())["grid"], "6x4")

    def test_local_fault_is_probed_even_when_the_frame_average_barely_moves(self):
        current = scene()
        # Flatten one 6x4 grid cell (column E, row 2) to a plain colour: a blocky, untextured patch.
        ImageDraw.Draw(current).rectangle((1280, 270, 1599, 539), fill=(120, 130, 110))
        metrics = self.run_compare(self.save("flat.png", current))
        self.assertEqual(metrics["probes"][0]["where"], "E2")
        self.assertLess(metrics["probes"][0]["detail_ratio"], 0.2)
        self.assertGreater(metrics["frame"]["detail_ratio"], 0.9)

    def test_previous_capture_separates_fixed_cells_from_regressed_cells(self):
        previous, current = scene(), scene()
        ImageDraw.Draw(previous).rectangle((32, 32, 287, 239), fill=(10, 10, 10))  # A1 was wrong, now fixed
        ImageDraw.Draw(current).rectangle((1632, 842, 1887, 1047), fill=(250, 250, 250))  # F4 is newly wrong
        metrics = compare.compare(self.target, self.save("now.png", current), self.save("was.png", previous),
                                  out_dir=self.dir / "review")
        self.assertEqual(metrics["since_previous"]["better_cells"], ["A1"])
        self.assertEqual(metrics["since_previous"]["worse_cells"], ["F4"])
        self.assertIn("F4", [probe["where"] for probe in metrics["probes"]])
        with Image.open(metrics["sheets"]["compare"]["file"]) as image:
            self.assertLessEqual(max(image.size), 1024)

    def test_regions_can_use_different_boxes_when_target_and_capture_are_framed_differently(self):
        current = Image.new("RGB", (1280, 1280), (20, 20, 20))
        current.paste(scene().crop((0, 0, 960, 540)), (160, 370))  # same subject, different frame and aspect
        argument = compare.parse_region("subject=0,0,0.5,0.5:0.125,0.289,0.75,0.422")
        metrics = self.run_compare(self.save("reframed.png", current), probes=0, regions=[argument])
        self.assertTrue(metrics["aspect_mismatch"])
        self.assertEqual([probe["where"] for probe in metrics["probes"]], ["subject"])
        with_previous = compare.compare(self.target, self.dir / "reframed.png", self.target, out_dir=self.dir / "review",
                                        probes=0, regions=[argument])
        self.assertEqual([probe["where"] for probe in with_previous["probes"]], ["subject"])
        self.assertLess(abs(metrics["probes"][0]["luma"]), 0.02)
        self.assertLess(abs(metrics["probes"][0]["detail_ratio"] - 1), 0.25)

    def test_command_line_prints_compact_metrics_and_rejects_bad_regions(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            compare.main(["--target", str(self.target), "--current", str(self.target), "--region", "x=2,0,1,1"])
        with contextlib.redirect_stdout(output):
            compare.main(["--target", str(self.target), "--current", str(self.target), "--out", str(self.dir / "cli"),
                          "--grid", "3x2", "--probes", "1"])
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(output.getvalue())["probes"][0]["where"], "A1")
        self.assertTrue((self.dir / "cli" / "probes.png").exists())
        with contextlib.redirect_stdout(io.StringIO()):
            compare.main(["--target", str(self.target), "--current", str(self.target), "--out", str(self.dir / "cli"),
                          "--probes", "0"])
        self.assertFalse((self.dir / "cli" / "probes.png").exists(), "a stale probe sheet must not survive")


if __name__ == "__main__":
    unittest.main()
