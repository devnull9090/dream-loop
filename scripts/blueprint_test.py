"""Tests for blueprint.py. Run: python -m unittest scripts/blueprint_test.py"""
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import blueprint  # noqa: E402

TEMPLATE = Path(__file__).parent.parent / "references" / "blender-mode" / "blueprint.template.json"
SIZE = (800, 400)


def sheet():
    """A two-view design sheet: side view on the left half, front view on the right, on a flat grey ground."""
    image = Image.new("RGB", SIZE, (200, 200, 200))
    draw = ImageDraw.Draw(image)
    draw.ellipse((160, 40, 240, 120), fill=(180, 40, 40))  # head, side
    draw.rectangle((140, 120, 260, 360), fill=(40, 80, 160))  # body, side
    draw.ellipse((570, 40, 630, 120), fill=(180, 40, 40))  # head, front
    draw.rectangle((540, 120, 660, 360), fill=(40, 80, 160))  # body, front
    return image


def sample():
    view = {"pose": "standing", "background": "auto"}
    return {
        "version": 1, "subject": "test figure", "target": "target.png", "target_size": list(SIZE),
        "reference_view": "side", "reference_part": "head",
        "views": [
            {**view, "id": "side", "frame": [0, 0, 0.5, 1], "subject_box": [0.175, 0.1, 0.15, 0.8], "camera": {"yaw": 90, "pitch": 0, "lens": 85}},
            {**view, "id": "front", "frame": [0.5, 0, 0.5, 1], "subject_box": [0.675, 0.1, 0.15, 0.8], "camera": {"yaw": 0, "pitch": 0, "lens": 85}},
        ],
        "parts": [
            {"id": "head", "objects": ["Head*"], "form": "A plain sphere sitting on the body.",
             "colours": [{"name": "skin", "at": {"view": "side", "point": [0.25, 0.2]}}],
             "boxes": {"side": [0.2, 0.1, 0.1, 0.2], "front": [0.7125, 0.1, 0.075, 0.2]}},
            {"id": "body", "objects": ["Body*"], "form": "A tall box, wider from the front than the side.",
             "colours": [{"name": "cloth", "hex": "#2850a0"}],
             "boxes": {"side": [0.175, 0.3, 0.15, 0.6], "front": [0.675, 0.3, 0.15, 0.6]}},
        ],
        "joints": [{"id": "neck", "parts": ["head", "body"], "points": {"side": [0.25, 0.3]}}],
    }


class BlueprintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        sheet().save(self.dir / "target.png")
        self.path = self.dir / "blueprint.json"

    def save(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = blueprint.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def errors(self, change):
        data = sample()
        change(data)
        return "\n".join(blueprint.problems(data)[0])

    def test_write_records_size_colours_and_measures(self):
        data = sample()
        del data["target_size"]
        self.save(data)
        status, out, err = self.run_main("validate", str(self.path), "--write")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(json.loads(out)["warnings"], [])
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["target_size"], list(SIZE))
        self.assertEqual(written["parts"][0]["colours"][0]["hex"], "#b42828")
        head = written["measures"]["side"]["parts"]["head"]
        self.assertEqual((head["height"], head["width"], head["cx"], head["cy"]), (0.25, 0.25, 0.0, 0.875))
        body = written["measures"]["side"]["parts"]["body"]
        self.assertEqual((body["height"], body["height_ref"], body["cy"]), (0.75, 3.0, 0.375))
        self.assertEqual(written["measures"]["side"]["joints"]["neck"], {"x": 0.0, "y": 0.75})
        self.assertEqual(self.run_main("validate", str(self.path))[0], 0)
        blueprint.load(self.path)  # complete: loads without raising

    def test_unwritten_sheet_is_valid_but_not_loadable(self):
        self.save(sample())
        status, out, _ = self.run_main("validate", str(self.path))
        self.assertEqual(status, 0)
        self.assertTrue(any("validate --write" in w for w in json.loads(out)["warnings"]))
        with self.assertRaises(blueprint.BlueprintError):
            blueprint.load(self.path)

    def test_edited_box_makes_measures_stale(self):
        self.save(sample())
        self.run_main("validate", str(self.path), "--write")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["parts"][0]["boxes"]["side"][3] = 0.15
        self.save(data)
        status, _, err = self.run_main("validate", str(self.path))
        self.assertEqual(status, 2)
        self.assertIn("measures no longer match", err)

    def test_hand_edited_measures_are_rejected(self):
        self.save(sample())
        self.run_main("validate", str(self.path), "--write")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["measures"]["side"]["parts"]["head"]["height"] = 0.31
        self.assertIn("never edit measures", "\n".join(blueprint.problems(data)[0]))

    def test_problems_are_named(self):
        cases = [
            (lambda d: d["views"][0].pop("camera"), "view side: camera needs"),
            (lambda d: d["views"][0].update(subject_box=[0.4, 0.1, 0.3, 0.8]), "subject_box must lie inside frame"),
            (lambda d: d["parts"][0]["boxes"].update(side=[0.0, 0.1, 0.1, 0.2]), "reaches outside that view's subject_box"),
            (lambda d: d["parts"][0]["boxes"].update(top=[0.2, 0.1, 0.1, 0.2]), "unknown view top"),
            (lambda d: d["parts"][1].update(objects=["Head*"]), "also claimed by part head"),
            (lambda d: d["parts"][0].update(form="round"), "form must describe"),
            (lambda d: d["parts"][0].update(colours=[{"name": "skin"}]), "needs a #rrggbb hex, or an at point"),
            (lambda d: d["parts"][1]["boxes"].pop("front"), "view front: at least two parts"),
            (lambda d: d.update(reference_part="tail"), "reference_part must be the id of a part"),
            (lambda d: d["joints"][0].update(parts=["head", "tail"]), "joint neck: parts must name"),
            (lambda d: d.update(tolerances={"sizes": 0.1}), "tolerances may only set"),
            (lambda d: d["parts"][1].update(same_as="body"), "same_as must name another part"),
            (lambda d: d["views"][0].update(scene={"frame": "12"}), "scene may set frame"),
        ]
        for change, message in cases:
            with self.subTest(message):
                self.assertIn(message, self.errors(change))

    def test_same_as_inherits_form_and_colours(self):
        data = sample()
        data["parts"].append({"id": "body_2", "objects": ["Other*"], "same_as": "body", "boxes": {"side": [0.2, 0.4, 0.05, 0.1]}})
        self.assertEqual(blueprint.problems(data)[0], [])
        self.assertEqual(blueprint.resolve(data, data["parts"][2])["colours"], data["parts"][1]["colours"])

    def test_blockout_combines_views(self):
        data = sample()
        data["parts"][1]["boxes"]["side"] = [0.2, 0.3, 0.1, 0.6]  # thinner from the side: 0.25 H deep, 0.375 H wide
        spec = blueprint.blockout(data)
        body = next(p for p in spec["parts"] if p["id"] == "body")
        self.assertEqual(body["size"], [0.375, 0.25, 0.75])
        self.assertEqual(body["centre"], [0.0, 0.0, 0.375])
        self.assertEqual(body["assumed"], [])

    def test_blockout_flags_an_extent_seen_from_one_side_only(self):
        data = sample()
        del data["parts"][0]["boxes"]["front"]
        head = blueprint.blockout(data)["parts"][0]
        self.assertEqual(head["assumed"], ["width"])
        self.assertEqual(head["size"][0], head["size"][1])

    def test_blockout_ignores_views_of_another_pose(self):
        data = sample()
        data["views"][1]["pose"] = "leaping"
        spec = blueprint.blockout(data)
        self.assertEqual(spec["pose"], "standing")
        self.assertTrue(all(p["views"] == ["side"] for p in spec["parts"]))

    def test_overlay_and_blockout_commands(self):
        self.save(sample())
        self.run_main("validate", str(self.path), "--write")
        status, out, _ = self.run_main("overlay", str(self.path), "--out", str(self.dir / "o" / "overlay.png"))
        self.assertEqual(status, 0)
        with Image.open(self.dir / "o" / "overlay.png") as image:
            self.assertEqual(image.size, SIZE)
        status, out, _ = self.run_main("blockout", str(self.path), "--out", str(self.dir / "blockout.json"))
        self.assertEqual((status, json.loads(out)["parts"]), (0, 2))

    def test_template_is_valid_once_sized(self):
        data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        data["target_size"] = [1600, 900]
        errors, warnings = blueprint.problems(copy.deepcopy(data))
        self.assertEqual(errors, [])
        self.assertTrue(all("validate --write" in w for w in warnings))

    def test_unreadable_file_exits_2(self):
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(self.run_main("validate", str(self.path))[0], 2)


if __name__ == "__main__":
    unittest.main()
