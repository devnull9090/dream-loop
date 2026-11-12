"""Tests for measure.py on synthetic renders. Run: python -m unittest scripts/measure_test.py"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import measure  # noqa: E402
from blueprint_test import sheet  # noqa: E402
from viewmatch_test import complete, fake_render  # noqa: E402

FRONT_HEAD = {"head": (170, 40, 230, 120)}  # the sheet's front view has a narrower head


class MeasureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        sheet().save(self.dir / "target.png")
        self.path = self.dir / "blueprint.json"
        self.path.write_text(json.dumps(complete()), encoding="utf-8")
        self.renders = self.dir / "inner"

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = measure.main(["--blueprint", str(self.path), "--renders", str(self.renders)])
        return status, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def render(self, side=None, front=None, **options):
        fake_render(self.renders, "side", side, **options)
        fake_render(self.renders, "front", {**FRONT_HEAD, **(front or {})}, **options)

    def test_model_built_to_the_blueprint_passes(self):
        self.render()
        status, summary, _ = self.run_main()
        self.assertEqual((status, summary["pass"], summary["failing"], summary["restart"]), (0, True, [], False))
        self.assertEqual(summary["parts"], {"head": "pass", "body": "pass"})
        report = json.loads((self.renders / "measure.json").read_text(encoding="utf-8"))
        head = report["views"]["side"]["parts"]["head"]["measures"]
        self.assertAlmostEqual(head["height"]["target"], 0.25)
        self.assertAlmostEqual(head["height"]["current"], 0.25, delta=0.01)
        for name in summary["sheets"]:
            with Image.open(name) as image:
                self.assertGreater(image.width, 300)  # two panels side by side

    def test_same_model_at_another_distance_and_size_passes(self):
        self.render(size=(640, 640), scale=1.3)
        self.assertEqual(self.run_main()[0], 0)

    def test_failures_state_target_and_current_in_h(self):
        self.render(side={"head": (140, 0, 260, 120)})  # head 1.5 times too big: the subject is now 360 px tall
        status, summary, _ = self.run_main()
        self.assertEqual(status, 1)
        self.assertEqual(summary["parts"], {"head": "fail", "body": "pass"})
        self.assertRegex(summary["failing"][0], r"^head height in side: target 0\.25 H, currently 0\.33\d H$")
        self.assertRegex(summary["failing"][1], r"^head width in side: target 0\.25 H, currently 0\.33\d H$")
        self.assertFalse(any("%" in line for line in summary["failing"]))
        self.assertFalse(summary["restart"])

    def test_position_is_measured(self):
        self.render(side={"head": (220, 40, 300, 120)})  # head pushed 60 px to the right
        _, summary, _ = self.run_main()
        self.assertTrue(any(line.startswith("head centre across in side: target") for line in summary["failing"]))

    def test_small_errors_inside_tolerance_pass(self):
        self.render(side={"head": (158, 36, 242, 120)})  # 5% bigger
        self.assertEqual(self.run_main()[0], 0)

    def test_hidden_part_is_not_measured_in_that_view_but_must_be_somewhere(self):
        self.render(visible={"head": 0.2})
        status, summary, _ = self.run_main()
        self.assertEqual(status, 1)
        self.assertEqual(summary["parts"]["head"], "unmeasured")
        self.assertIn("mostly hidden in every view", summary["failing"][0])
        fake_render(self.renders, "front", FRONT_HEAD)  # visible from the front after all
        status, summary, _ = self.run_main()
        self.assertEqual((status, summary["parts"]["head"]), (0, "pass"))
        report = json.loads((self.renders / "measure.json").read_text(encoding="utf-8"))
        self.assertEqual(report["parts"]["head"]["views"], {"side": "hidden", "front": "pass"})

    def test_missing_part_and_unassigned_objects_fail(self):
        self.render(side={"head": None}, front={"head": None})
        data = json.loads((self.renders / "side.ids.json").read_text(encoding="utf-8"))
        data["unassigned"] = ["Cube"]
        (self.renders / "side.ids.json").write_text(json.dumps(data), encoding="utf-8")
        status, summary, _ = self.run_main()
        self.assertEqual((status, summary["parts"]["head"]), (1, "missing"))
        self.assertIn("head: no object in the scene belongs to this part", summary["failing"])
        self.assertTrue(any("belong to no part" in line and "Cube" in line for line in summary["failing"]))

    def test_every_view_must_be_rendered(self):
        fake_render(self.renders, "side")
        status, summary, _ = self.run_main()
        self.assertEqual(status, 1)
        self.assertTrue(any("view front: no render" in line for line in summary["failing"]))

    def test_restart_is_advised_when_most_parts_are_far_out(self):
        self.render(side={"head": (100, 0, 300, 200), "body": (190, 200, 210, 360)},
                    front={"head": (100, 0, 300, 200), "body": (190, 200, 210, 360)})
        status, summary, _ = self.run_main()
        self.assertEqual((status, summary["restart"]), (1, True))
        self.assertIn("rebuild it from the blockout", summary["advice"])

    def test_incomplete_blueprint_exits_2(self):
        data = complete()
        del data["measures"]
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.render()
        status, _, err = self.run_main()
        self.assertEqual(status, 2)
        self.assertIn("validate --write", err)


if __name__ == "__main__":
    unittest.main()
