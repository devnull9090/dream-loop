"""Tests for viewmatch.py and masks.py on synthetic renders. Run: python -m unittest scripts/viewmatch_test.py"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import blueprint  # noqa: E402
import masks  # noqa: E402
import viewmatch  # noqa: E402
from blueprint_test import sample, sheet  # noqa: E402

COLOURS = {"head": [115, 185, 45], "body": [255, 45, 115]}
# The figure of blueprint_test.sheet() as it sits in a 400 x 400 view frame: head ellipse, body rectangle.
FIGURE = {"head": (160, 40, 240, 120), "body": (140, 120, 260, 360)}


def complete(data=None):
    data = data or sample()
    data["parts"][0]["colours"][0]["hex"] = "#b42828"
    data["measures"] = blueprint.derive(data)
    return data


def fake_render(folder, view, shapes=None, size=(400, 400), scale=1.0, hashes=None, visible=None, camera=None):
    """Write <view>.ids.png and <view>.ids.json the way blender_run.py render does, for shapes given in pixels."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    shapes = {**FIGURE, **(shapes or {})}
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    parts = {}
    for name, box in shapes.items():
        if box is None:
            parts[name] = {"colour": COLOURS[name], "visible_px": 0, "objects": 0, "visible": 0.0}
            continue
        box = tuple(round(v * scale) for v in box)
        (draw.ellipse if name == "head" else draw.rectangle)(box, fill=(*COLOURS[name], 255))
        parts[name] = {"colour": COLOURS[name], "objects": 1, "visible": (visible or {}).get(name, 1.0),
                       "hash": (hashes or {}).get(name, name + "-v1"),
                       "projected_box": [box[0] / size[0], box[1] / size[1], (box[2] - box[0]) / size[0], (box[3] - box[1]) / size[1]]}
    image.save(folder / f"{view}.ids.png")
    for name, part in parts.items():
        if part["objects"]:
            part["visible_px"] = masks.area(masks.near(image, part["colour"], 2))
            part["solo_px"] = part["visible_px"]
    data = {"view": view, "ids": f"{view}.ids.png", "resolution": list(size), "unassigned": [], "parts": parts,
            "camera": camera or {"yaw": 90 if view == "side" else 0, "pitch": 0, "lens": 85, "distance": 9.0, "distance_H": 4.5}}
    (folder / f"{view}.ids.json").write_text(json.dumps(data), encoding="utf-8")
    return data


class ViewMatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        sheet().save(self.dir / "target.png")
        self.path = self.dir / "blueprint.json"
        self.save(complete())
        self.renders = self.dir / "inner"

    def save(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = viewmatch.main(["--blueprint", str(self.path), *argv])
        return status, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def view(self, report, name):
        return next(v for v in report["views"] if v["view"] == name)

    def test_matching_render_passes_and_writes_a_picture(self):
        fake_render(self.renders, "side")
        # The front view on the sheet has a narrower head (60 px) than FIGURE's.
        fake_render(self.renders, "front", {"head": (170, 40, 230, 120)})
        status, report, _ = self.run_main("--renders", str(self.renders))
        self.assertEqual(status, 0)
        for name in ("side", "front"):
            view = self.view(report, name)
            self.assertEqual((view["method"], view["pass"]), ("silhouette", True))
            self.assertGreater(view["score"], 0.95)
            self.assertTrue(Path(view["picture"]).exists())
        self.assertEqual(json.loads((self.renders / "viewmatch.json").read_text(encoding="utf-8")), report)

    def test_distance_and_resolution_do_not_matter(self):
        fake_render(self.renders, "side", size=(800, 800), scale=0.9)
        status, report, _ = self.run_main("--renders", str(self.renders), "--views", "side")
        self.assertEqual(status, 0)
        self.assertGreater(self.view(report, "side")["score"], 0.93)

    def test_wrong_shape_blocks(self):
        fake_render(self.renders, "side", {"body": (60, 120, 340, 360)})
        status, report, _ = self.run_main("--renders", str(self.renders), "--views", "side")
        self.assertEqual(status, 1)
        view = self.view(report, "side")
        self.assertFalse(view["pass"])
        self.assertLess(view["score"], 0.7)
        self.assertIn("below", view["why"])

    def test_missing_render_blocks(self):
        fake_render(self.renders, "side")
        status, report, _ = self.run_main("--renders", str(self.renders))
        self.assertEqual(status, 1)
        self.assertIn("front.ids.json is missing", self.view(report, "front")["why"])

    def test_unkeyable_background_falls_back_to_part_boxes(self):
        data = complete()
        data["views"][0]["background"] = "none"
        self.save(data)
        fake_render(self.renders, "side")
        status, report, _ = self.run_main("--renders", str(self.renders), "--views", "side")
        view = self.view(report, "side")
        self.assertEqual((status, view["method"], view["pass"]), (0, "landmarks", True))
        fake_render(self.renders, "side", {"head": (160, 40, 240, 240), "body": (140, 240, 260, 360)})  # head twice as tall
        status, report, _ = self.run_main("--renders", str(self.renders), "--views", "side")
        self.assertEqual((status, self.view(report, "side")["pass"]), (1, False))

    def test_busy_background_is_detected_not_trusted(self):
        target = sheet()
        draw = ImageDraw.Draw(target)
        for x in range(0, 400, 20):  # stripes behind the side view
            draw.rectangle((x, 0, x + 9, 399), fill=(90, 120, 60))
        draw.ellipse(FIGURE["head"], fill=(180, 40, 40))
        draw.rectangle(FIGURE["body"], fill=(40, 80, 160))
        target.save(self.dir / "target.png")
        fake_render(self.renders, "side")
        _, report, _ = self.run_main("--renders", str(self.renders), "--views", "side")
        view = self.view(report, "side")
        self.assertEqual(view["method"], "landmarks")
        self.assertIn("border is one colour", view["key"])

    def sweep(self, shapes_by_yaw):
        folder = self.dir / "sweep"
        folder.mkdir(exist_ok=True)
        candidates = []
        for n, (yaw, body) in enumerate(shapes_by_yaw):
            fake_render(folder, "c", {"body": body})
            (folder / "c.ids.png").replace(folder / f"side.{n:03d}.png")
            candidates.append({"file": f"side.{n:03d}.png", "camera": {"yaw": yaw, "pitch": 0, "lens": 85, "distance": 9.0}})
        (folder / "side.candidates.json").write_text(json.dumps({"view": "side", "candidates": candidates}), encoding="utf-8")
        return folder

    def test_solve_picks_the_best_candidate_and_freezes_it(self):
        folder = self.sweep([(70, (100, 120, 300, 360)), (80, FIGURE["body"]), (90, (120, 120, 280, 360))])
        cameras = self.dir / "cameras.json"
        status, result, _ = self.run_main("--sweep", str(folder), "--view", "side", "--cameras", str(cameras))
        self.assertEqual((status, result["camera"]["yaw"], result["pass"]), (0, 80, True))
        self.assertEqual(result["deviation"], {"yaw": 10, "pitch": 0})
        saved = json.loads(cameras.read_text(encoding="utf-8"))["side"]
        self.assertEqual((saved["yaw"], saved["distance"], saved["history"]), (80, 9.0, []))
        status, _, err = self.run_main("--sweep", str(folder), "--view", "side", "--cameras", str(cameras))
        self.assertEqual(status, 2)
        self.assertIn("already has a solved camera", err)
        status, result, _ = self.run_main("--sweep", str(folder), "--view", "side", "--cameras", str(cameras), "--resolve", "target redrawn")
        self.assertEqual(status, 0)
        history = json.loads(cameras.read_text(encoding="utf-8"))["side"]["history"]
        self.assertEqual((history[0]["yaw"], history[0]["replaced_because"]), (80, "target redrawn"))

    def test_solve_stays_at_the_estimate_unless_moving_clearly_helps(self):
        folder = self.sweep([(70, FIGURE["body"]), (90, FIGURE["body"])])  # identical silhouettes
        _, result, _ = self.run_main("--sweep", str(folder), "--view", "side", "--cameras", str(self.dir / "cameras.json"))
        self.assertEqual(result["camera"]["yaw"], 90)
        self.assertNotIn("warning", result)

    def test_solve_warns_when_the_best_camera_is_at_the_window_edge(self):
        folder = self.sweep([(70, FIGURE["body"]), (90, (60, 120, 340, 360))])
        _, result, _ = self.run_main("--sweep", str(folder), "--view", "side", "--cameras", str(self.dir / "cameras.json"))
        self.assertEqual(result["camera"]["yaw"], 70)
        self.assertIn("edge of the allowed window", result["warning"])


class MasksTest(unittest.TestCase):
    def test_overlap_is_one_for_the_same_shape_at_any_size(self):
        a = Image.new("L", (200, 200))
        ImageDraw.Draw(a).ellipse((40, 20, 160, 180), fill=255)
        b = a.resize((90, 90), Image.NEAREST)
        iou, aspect = masks.overlap(a, b)
        self.assertGreater(iou, 0.97)
        self.assertAlmostEqual(aspect, 1.0, delta=0.03)
        self.assertEqual(masks.overlap(a, Image.new("L", (10, 10)))[0], 0.0)

    def test_read_ids_separates_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = fake_render(tmp, "side")
            _, parts, subject = masks.read_ids(Path(tmp) / "side.ids.json")
            self.assertEqual(masks.area(parts["head"]), data["parts"]["head"]["visible_px"])
            self.assertEqual(masks.area(subject), sum(p["visible_px"] for p in data["parts"].values()))
            self.assertEqual([round(v, 3) for v in masks.fraction_box(parts["body"])], [0.35, 0.3, 0.302, 0.603])

    def test_box_helpers(self):
        self.assertAlmostEqual(masks.box_iou([0, 0, 0.5, 0.5], [0.25, 0, 0.5, 0.5]), 1 / 3)
        self.assertEqual(masks.within([0.2, 0.2, 0.1, 0.2], [0.1, 0.1, 0.2, 0.4]), [0.5, 0.25, 0.5, 0.5])


if __name__ == "__main__":
    unittest.main()
