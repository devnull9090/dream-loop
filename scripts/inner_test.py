"""End-to-end tests of the 3D mode on a creature made of primitives. Run: python -m unittest scripts/inner_test.py

Draws a design sheet, writes its blueprint, builds the blockout in Blender, runs the inner loop, then
takes the result through per-part review to done. Starts Blender several times; skipped when Blender
is not installed.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import blender_run  # noqa: E402
import blueprint  # noqa: E402
import inner  # noqa: E402
import parts  # noqa: E402
import verdict  # noqa: E402
from parts_test import pv  # noqa: E402

try:
    BLENDER = blender_run.find_blender()
except blender_run.BlenderError:
    BLENDER = None

# Pixel boxes on the sheet, one 500 x 500 panel per view. The creature is 400 px tall.
SIDE = {"head": (205, 50, 295, 150), "body": (190, 150, 310, 330), "leg_l": (225, 330, 275, 450)}
FRONT = {"head": (710, 50, 790, 150), "body": (670, 150, 830, 330), "leg_l": (770, 330, 810, 450), "leg_r": (690, 330, 730, 450)}
# A second pose, seen from the front in a third panel: the head leans 40 px (0.1 H) to one side.
LEAN = {name: (x0 + 500 + 40 * (name == "head"), y0, x1 + 500 + 40 * (name == "head"), y1) for name, (x0, y0, x1, y1) in FRONT.items()}
PAINT = {"head": (176, 48, 44), "body": (44, 84, 160), "leg_l": (60, 140, 70), "leg_r": (60, 140, 70)}
HEIGHT = 1.8
GROW_HEAD = "import bpy\nbpy.data.objects['BO_head'].scale = [v * 1.7 for v in bpy.data.objects['BO_head'].scale]\n"
DOUBLE = ("import bpy\nfor o in bpy.data.collections['DreamLoop Blockout'].objects:\n"
          "    o.location = [v * 2 for v in o.location]\n    o.scale = [v * 2 for v in o.scale]\n")
LEAN_POSE = ("import bpy\npose = bpy.data.collections.new('Pose_Lean')\nbpy.context.scene.collection.children.link(pose)\n"
             "for o in list(bpy.data.collections['DreamLoop Blockout'].objects):\n"
             "    c = o.copy()\n    pose.objects.link(c)\n"
             f"    if c['dl_part'] == 'head':\n        c.location.x += {0.1 * HEIGHT}\n")


def design_sheet(panels):
    image = Image.new("RGB", (500 * len(panels), 500), (205, 205, 200))
    draw = ImageDraw.Draw(image)
    for shapes in panels:
        for name in ("leg_r", "leg_l", "body", "head"):
            if name in shapes:
                draw.ellipse(shapes[name], fill=PAINT[name])
    return image


def creature(lean=False):
    panels = {"side": SIDE, "front": FRONT, **({"lean": LEAN} if lean else {})}
    size = (500 * len(panels), 500)

    def box(pixels):
        x0, y0, x1, y1 = pixels
        return [x0 / size[0], y0 / size[1], (x1 - x0) / size[0], (y1 - y0) / size[1]]

    def bounds(shapes):
        return box((min(s[0] for s in shapes.values()), 50, max(s[2] for s in shapes.values()), 450))

    def part(name, form, **extra):
        return {"id": name, "objects": [f"{name}*"], "form": form, "boxes": {view: box(shapes[name]) for view, shapes in panels.items() if name in shapes}, **extra}

    def colour(name):
        x0, y0, x1, y1 = FRONT[name]
        return [{"name": name, "at": {"view": "front", "point": [(x0 + x1) / 2 / size[0], (y0 + y1) / 2 / size[1]]}}]

    def view(name, n, yaw, pose="standing", **extra):
        return {"id": name, "pose": pose, "frame": [n / len(panels), 0, 1 / len(panels), 1], "subject_box": bounds(panels[name]),
                "camera": {"yaw": yaw, "pitch": 0, "lens": 85}, **extra}

    standing = {"scene": {"collection": "DreamLoop Blockout"}} if lean else {}
    views = [view("side", 0, 90, **standing), view("front", 1, 0, **standing)]
    if lean:
        views.append(view("lean", 2, 0, pose="leaning", scene={"collection": "Pose_Lean", "frame": 3}))
    return {
        "version": 1, "subject": "primitive creature", "target": "target.png", "reference_view": "front", "reference_part": "head",
        "views": views,
        "parts": [
            part("head", "An upright egg, slightly deeper than it is wide.", colours=colour("head")),
            part("body", "A broad ellipsoid, wider than it is deep.", colours=colour("body")),
            part("leg_l", "A plain tapered post under the body.", colours=colour("leg_l")),
            {k: v for k, v in part("leg_r", None, same_as="leg_l").items() if k != "form"},
        ],
        "joints": [{"id": "neck", "parts": ["head", "body"], "points": {"front": [750 / size[0], 0.3]}}],
    }


def call(module, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = module.main(list(argv))
    return status, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()


class Built(unittest.TestCase):
    """A working folder with the sheet, its blueprint and the blockout built from it."""
    lean = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name) / ".dream-loop"
        cls.root.mkdir()
        design_sheet([SIDE, FRONT] + ([LEAN] if cls.lean else [])).save(cls.root / "target.png")
        cls.blueprint = cls.root / "blueprint.json"
        cls.blueprint.write_text(json.dumps(creature(cls.lean)), encoding="utf-8")
        cls.blend = cls.root / "model.blend"
        cls.steps = [
            call(blueprint, "validate", str(cls.blueprint), "--write"),
            call(blueprint, "blockout", str(cls.blueprint), "--out", str(cls.root / "blockout.json")),
            call(blender_run, "blockout", "--spec", str(cls.root / "blockout.json"), "--height", str(HEIGHT), "--save", str(cls.blend)),
        ]

    def script(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")
        return str(self.root / name)

    def run_inner(self, *extra):
        return call(inner, "--blueprint", str(self.blueprint), "--blend", str(self.blend), *extra)


@unittest.skipUnless(BLENDER, "Blender is not installed")
class EndToEndTest(Built):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.inner = call(inner, "--blueprint", str(cls.blueprint), "--blend", str(cls.blend))

    def test_1_blueprint_and_blockout(self):
        for status, _, err in self.steps:
            self.assertEqual((status, err), (0, ""))
        written = json.loads(self.blueprint.read_text(encoding="utf-8"))
        self.assertEqual(written["parts"][0]["colours"][0]["hex"], "#b0302c")
        self.assertEqual(written["measures"]["front"]["parts"]["leg_l"]["height"], 0.3)
        self.assertEqual(self.steps[1][1]["assumed"], {"leg_r": ["depth"]})  # seen from the front only
        self.assertEqual(self.steps[2][1]["objects"], ["BO_head", "BO_body", "BO_leg_l", "BO_leg_r"])

    def test_2_inner_loop_passes_a_model_built_to_the_blueprint(self):
        status, result, err = self.inner
        self.assertEqual((status, err), (0, ""), result)
        self.assertEqual((result["stage"], result["failing"], result["restart"]), ("outer", [], False))
        self.assertEqual(result["parts"], {"head": "pass", "body": "pass", "leg_l": "pass", "leg_r": "pass"})
        self.assertEqual([c["view"] for c in result["cameras_solved"]], ["side", "front"])
        for view in result["viewmatch"]:
            self.assertEqual(view["method"], "silhouette")
            self.assertGreater(view["score"], 0.85)
        cameras = json.loads((self.root / "cameras.json").read_text(encoding="utf-8"))
        self.assertLessEqual(cameras["side"]["deviation"]["yaw"], 20)
        for name in result["sheets"]:
            self.assertTrue(Path(name).exists())

    def test_3_second_run_reuses_the_frozen_cameras(self):
        before = (self.root / "cameras.json").read_text(encoding="utf-8")
        status, result, _ = self.run_inner("--out", str(self.root / "again"))
        self.assertEqual(status, 0)
        self.assertNotIn("cameras_solved", result)
        self.assertEqual((self.root / "cameras.json").read_text(encoding="utf-8"), before)

    def test_4_a_wrong_proportion_is_reported_in_absolute_terms(self):
        status, result, _ = self.run_inner("--setup", self.script("grow_head.py", GROW_HEAD), "--out", str(self.root / "wrong"))
        self.assertEqual(status, 1)
        self.assertIn(result["stage"], ("blockout", "proportion"))
        self.assertEqual(result["parts"]["head"], "fail")
        self.assertTrue(any(line.startswith("head height in front: target 0.25 H, currently 0.") for line in result["failing"]), result["failing"])

    def test_5_a_rescaled_model_still_matches_with_the_frozen_cameras(self):
        status, result, err = self.run_inner("--setup", self.script("double.py", DOUBLE), "--out", str(self.root / "double"))
        self.assertEqual((status, err), (0, ""), result)
        self.assertNotIn("cameras_solved", result)
        before = json.loads((self.root / "inner" / "front.ids.json").read_text(encoding="utf-8"))["camera"]
        after = json.loads((self.root / "double" / "front.ids.json").read_text(encoding="utf-8"))["camera"]
        self.assertAlmostEqual(after["subject_height"], 2 * before["subject_height"], places=2)
        self.assertAlmostEqual(after["distance"], 2 * before["distance"], places=2)  # same distance in subject heights
        self.assertAlmostEqual(after["distance_H"], before["distance_H"], places=3)

    def test_6_per_part_review_to_done(self):
        renders = str(self.root / "inner")
        status, result, err = call(parts, "prepare", "--blueprint", str(self.blueprint), "--round", "1", "--renders", renders)
        self.assertEqual((status, err), (0, ""), result)
        self.assertEqual([s["part"] for s in result["send"]], ["head", "body", "leg_l", "leg_r"])
        brief = json.loads((Path(result["send"][3]["folder"]) / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual((brief["views"], brief["form"]), (["front"], "A plain tapered post under the body."))  # leg_r is hidden from the side
        with Image.open(Path(result["send"][0]["folder"]) / "pair.png") as pair:
            self.assertGreater(min(pair.size), 300)
        for entry in result["send"]:
            Path(entry["verdict"]).write_text(json.dumps(pv(entry["part"], 8.5)), encoding="utf-8")
            Path(entry["verdict"].replace(".json", "-confirm.json")).write_text(json.dumps(pv(entry["part"], 8.0)), encoding="utf-8")
        status, decision, _ = call(verdict, str(self.root / "rounds"))
        self.assertEqual((status, decision["decision"]), (0, "continue"))
        self.assertEqual({p["state"] for name, p in decision["parts"].items() if name != "assembly"}, {"locked"})

        status, result, _ = call(parts, "prepare", "--blueprint", str(self.blueprint), "--round", "2", "--renders", renders)
        self.assertEqual((status, [s["part"] for s in result["send"]]), (0, ["assembly"]))
        Path(result["send"][0]["verdict"]).write_text(json.dumps(pv("assembly", 8.5)), encoding="utf-8")
        Path(result["send"][0]["verdict"].replace(".json", "-confirm.json")).write_text(json.dumps(pv("assembly", 8.5)), encoding="utf-8")
        self.assertEqual(call(verdict, str(self.root / "rounds"))[1]["decision"], "done")

        # The model changes after everything was locked: the next round is stopped as a regression.
        status, result, _ = call(parts, "prepare", "--blueprint", str(self.blueprint), "--round", "3", "--renders", str(self.root / "wrong"))
        self.assertEqual(status, 1)
        self.assertFalse(result["layout"])
        self.assertEqual(result["regressions"], ["head"])
        self.assertEqual(call(verdict, str(self.root / "rounds"))[1]["decision"], "revert")


@unittest.skipUnless(BLENDER, "Blender is not installed")
class PoseTest(Built):
    """A target with a second pose, kept in its own collection in the scene."""
    lean = True

    def test_every_pose_is_rendered_measured_and_reviewed(self):
        for status, _, err in self.steps:
            self.assertEqual((status, err), (0, ""))
        pose = self.script("lean_pose.py", LEAN_POSE)
        status, result, err = self.run_inner("--setup", pose)
        self.assertEqual((status, err), (0, ""), result)
        self.assertEqual([v["view"] for v in result["viewmatch"]], ["side", "front", "lean"])
        measured = json.loads((self.root / "inner" / "measure.json").read_text(encoding="utf-8"))
        standing = measured["views"]["front"]["parts"]["head"]["measures"]["cx"]
        leaning = measured["views"]["lean"]["parts"]["head"]["measures"]["cx"]
        self.assertAlmostEqual(leaning["target"] - standing["target"], 0.1, places=2)
        self.assertAlmostEqual(leaning["current"] - standing["current"], 0.1, delta=0.02)

        status, result, err = call(parts, "prepare", "--blueprint", str(self.blueprint), "--round", "1", "--renders", str(self.root / "inner"))
        self.assertEqual((status, err), (0, ""), result)
        brief = json.loads((Path(result["send"][0]["folder"]) / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual((brief["part"], brief["views"]), ("head", ["side", "front", "lean"]))

        # Without the pose in the scene, the view that needs it cannot be rendered, and nothing is judged.
        status, _, err = self.run_inner("--out", str(self.root / "no-pose"))
        self.assertEqual(status, 2)
        self.assertIn("no collection named 'Pose_Lean'", err)


if __name__ == "__main__":
    unittest.main()
