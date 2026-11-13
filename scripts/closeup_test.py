"""Tests for closeup.py and the critic sheets' sizes on synthetic renders. Run: python -m unittest scripts/closeup_test.py"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import closeup  # noqa: E402
import parts  # noqa: E402
from blueprint_test import sheet  # noqa: E402
from form_test import HEAD, ellipse, fractions  # noqa: E402
from parts_test import FRONT, SIDE, picture  # noqa: E402
from viewmatch_test import complete, fake_render  # noqa: E402


class CloseupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        sheet().save(self.root / "target.png")
        self.path = self.root / "blueprint.json"
        self.data = complete()
        self.data["parts"][0]["outlines"] = {"side": fractions(ellipse(HEAD))}
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        self.renders = self.root / "inner"
        for view, shapes in (("side", SIDE), ("front", FRONT)):
            fake_render(self.renders, view, shapes)
            picture(shapes).save(self.renders / f"{view}.png")
            ids = self.renders / f"{view}.ids.json"
            ids.write_text(json.dumps({**json.loads(ids.read_text(encoding="utf-8")), "picture": f"{view}.png"}), encoding="utf-8")

    def test_every_part_gets_a_sheet_per_view_with_target_and_model_at_one_scale(self):
        found = closeup.sheets(self.data, self.path, self.renders)
        self.assertEqual({name: [e["view"] for e in entries] for name, entries in found.items()}, {"head": ["side", "front"], "body": ["side", "front"]})
        side = found["head"][0]
        # The head is 80 px on the target and 81 in the render (its mask includes both edges); both are drawn at 512.
        self.assertEqual((side["target_px"], side["model_px"], side["edge"], side["source"]), (80, 81, 512, "crop"))
        self.assertGreater(side["overlap"], 0.95)  # this view has an outline, so the sheet carries the difference too
        self.assertNotIn("overlap", found["head"][1])
        with Image.open(side["file"]) as image:
            # Two crops of 512 plus padding on each side, and the outline panel.
            self.assertGreater(image.width, 2 * 512 * (1 + 2 * closeup.PAD) + 256)
            self.assertLessEqual(max(image.size), 1568)  # small enough to be read at full size
        with Image.open(found["body"][0]["file"]) as image:
            self.assertGreaterEqual(image.height, 512)  # the body's long side is its height
        self.assertEqual(sorted(json.loads((self.renders / "close" / "close.json").read_text(encoding="utf-8"))), ["body", "head"])

    def test_only_the_named_part_and_never_a_hidden_one(self):
        self.assertEqual(list(closeup.sheets(self.data, self.path, self.renders, only=["head"], edge=256)), ["head"])
        hidden = {"views": {"side": {"parts": {"head": {"status": "hidden"}}}}}
        found = closeup.sheets(self.data, self.path, self.renders, measured=hidden, only=["head"])
        self.assertEqual([e["view"] for e in found["head"]], ["front"])

    def test_requests_ask_blender_for_a_frame_that_gives_the_part_the_edge(self):
        items = closeup.requests(self.data, self.renders, only=["head"], edge=512)
        self.assertEqual([(i["view"], i["part"]) for i in items], [("side", "head"), ("front", "head")])
        self.assertEqual(items[0]["long_edge"], 2529)  # 81 px of a 400 px frame must become 512
        x, y, w, h = items[0]["box"]
        self.assertAlmostEqual(w, (81 + 2 * 8.1) / 400, places=3)
        self.assertLess(x, 160 / 400)
        self.assertEqual(closeup.requests(self.data, self.renders, only=["head"], edge=4096)[0]["long_edge"], closeup.MAX_RENDER)

    def test_a_crop_rendered_by_blender_replaces_the_enlarged_one(self):
        crop = self.root / "crop.png"
        Image.new("RGBA", (614, 614), (10, 200, 10, 255)).save(crop)  # the padded box at 512 px on the part
        found = closeup.sheets(self.data, self.path, self.renders, only=["head"], rendered={("head", "side"): {"file": str(crop), "long_edge": 2560}})
        side, front = found["head"]
        self.assertEqual((side["source"], side["model_px"], front["source"]), ("render", 512, "crop"))
        with Image.open(side["file"]) as image:
            self.assertEqual(image.getpixel((round(1.5 * 614) + 40, 300)), (10, 200, 10))

    def test_command_line(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = closeup.main(["--blueprint", str(self.path), "--renders", str(self.renders), "--part", "body"])
        self.assertEqual((status, list(json.loads(out.getvalue())["closeups"])), (0, ["body"]))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(closeup.main(["--blueprint", str(self.path), "--renders", str(self.renders), "--part", "tail"]), 2)


class CriticSheetTest(unittest.TestCase):
    """What a critic is handed must be large enough to read."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        sheet().save(self.root / "target.png")
        self.blueprint = self.root / "blueprint.json"
        self.blueprint.write_text(json.dumps(complete()), encoding="utf-8")
        self.renders = self.root / "inner"

    def prepare(self, scale):
        size = (400 * scale, 400 * scale)
        for view, shapes in (("side", SIDE), ("front", FRONT)):
            shapes = {name: tuple(v * scale for v in box) for name, box in shapes.items()}
            fake_render(self.renders, view, shapes, size=size)
            picture(shapes, size=size).save(self.renders / f"{view}.png")
            ids = self.renders / f"{view}.ids.json"
            ids.write_text(json.dumps({**json.loads(ids.read_text(encoding="utf-8")), "picture": f"{view}.png"}), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = parts.main(["prepare", "--blueprint", str(self.blueprint), "--round", "1", "--renders", str(self.renders)])
        return status, json.loads(out.getvalue())

    def test_a_part_too_small_in_the_capture_is_reported_with_the_size_to_capture_at(self):
        status, result = self.prepare(1)
        self.assertEqual((status, result["warnings"]), (0, []))  # it stops nothing
        note = next(n for n in result["notes"] if n.startswith("view side:"))
        self.assertIn("head, body cover under 256 px in the capture (head: 81 px)", note)
        self.assertIn("Capture this view about 2560 px on its long edge (inner.py --long-edge)", note)
        status, result = self.prepare(4)
        self.assertEqual(result["notes"], [])

    def test_tall_crops_get_a_sheet_per_view_so_each_is_large_enough(self):
        with mock.patch.object(parts, "CELL", 500):  # two views of a figure this plain just fit; a third view or a taller part would not
            status, result = self.prepare(4)
        folder = Path(next(s["folder"] for s in result["send"] if s["part"] == "body"))
        brief = json.loads((folder / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual((brief["pair"], brief["pairs"]), ("pair.png", ["pair-side.png", "pair-front.png"]))
        with Image.open(folder / "pair.png") as both, Image.open(folder / "pair-side.png") as one:
            self.assertLess(both.width / 2, 500)  # two tall rows on one sheet: each crop is narrow
            self.assertGreater(one.width / 2, 500)
            self.assertLessEqual(max(one.size), 1568)
        head = json.loads((Path(result["send"][0]["folder"]) / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual(head["pairs"], ["pair.png"])  # square crops fit one sheet at full size


if __name__ == "__main__":
    unittest.main()
