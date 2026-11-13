"""Tests for captures taken outside Blender: the backdrop check in parts.py and blueprint.py backdrop.

Run: python -m unittest scripts/capture_test.py
"""
import contextlib
import io
import json
import math
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import blueprint  # noqa: E402
import parts  # noqa: E402
import scope_test  # noqa: E402
from parts_test import FRONT, SIDE, picture  # noqa: E402


class BackdropTest(scope_test.Rounds):
    def capture(self, ground, body=(40, 80, 160)):
        """Engine captures of both views on a flat ground, framed as the mask renders are."""
        self.render()
        args = []
        for view, shapes in (("side", SIDE), ("front", FRONT)):
            picture(shapes, {"body": body}, ground=ground).save(self.root / f"capture-{view}.png")
            args += ["--capture", f"{view}={self.root / f'capture-{view}.png'}"]
        return self.prepare(1, *args)

    def test_a_backdrop_near_a_part_s_colour_fails_the_check_and_says_why(self):
        # A grey body on a grey backdrop: the key takes the body for background. The body is the lowest part, so the
        # subject's bounds change too, which a wrong camera would also do: both are named, the camera first.
        status, result, _ = self.capture((128, 128, 128), body=(120, 126, 134))
        self.assertEqual((status, result["layout"], result["send"]), (1, False, []))
        line = next(f for f in result["failing"] if f.startswith("view side: the capture overlaps the mask render by 0."))
        self.assertRegex(line, r"below 0\.85; take the capture with the camera recorded in cameras\.json\. If the camera is right: \d\d% of the subject "
                               r"in the capture is within the key's tolerance of the backdrop #808080, most of it on body,")
        self.assertIn("blueprint.py backdrop suggests one", line)
        review = self.review(1)
        self.assertEqual((review["views"]["side"]["alignment"]["backdrop"], review["views"]["side"]["alignment"]["lost_on"]), ("#808080", ["body"]))

    def test_a_part_inside_the_silhouette_lost_to_the_backdrop_is_blamed_on_the_backdrop_alone(self):
        # The subject's outline is where the render has it; a dark grey plate in the middle of it has gone into the backdrop.
        render, plate = Image.new("L", (400, 400)), Image.new("L", (400, 400))
        ImageDraw.Draw(render).ellipse((100, 60, 300, 340), fill=255)
        ImageDraw.Draw(plate).rectangle((110, 120, 290, 280), fill=255)
        capture = Image.new("RGB", (400, 400), (128, 128, 128))
        ImageDraw.Draw(capture).ellipse((100, 60, 300, 340), fill=(190, 150, 60))
        ImageDraw.Draw(capture).rectangle((110, 120, 290, 280), fill=(118, 122, 130))
        _, info = parts.capture_alignment(capture, None, render, 0.85, {"shell": render, "plate": plate})
        self.assertEqual((info["pass"], info["framed"], info["backdrop"], info["lost_on"]), (False, True, "#808080", ["plate", "shell"]))
        self.assertAlmostEqual(info["lost"], 0.65, delta=0.05)
        self.assertTrue(parts.backdrop_advice(info).startswith("6"))
        self.assertIn("most of it on plate, shell, so the key takes it for background", parts.backdrop_advice(info))

    def test_a_saturated_backdrop_passes_without_comment(self):
        status, result, _ = self.capture((255, 0, 255))
        self.assertEqual((status, result["layout"]), (0, True))
        self.assertFalse([note for note in result["notes"] if "backdrop" in note])
        self.assertLess(self.review(1)["views"]["side"]["alignment"]["lost"], 0.01)

    def test_a_wrong_camera_is_still_blamed_on_the_camera(self):
        self.render()
        args = []
        for view, shapes in (("side", SIDE), ("front", FRONT)):
            moved = {name: (x0 + 90, y0, x1 + 90, y1) for name, (x0, y0, x1, y1) in shapes.items()}
            picture(moved, ground=(255, 0, 255)).save(self.root / f"capture-{view}.png")
            args += ["--capture", f"{view}={self.root / f'capture-{view}.png'}"]
        status, result, _ = self.prepare(1, *args)
        self.assertEqual(status, 1)
        self.assertTrue(any("take the capture with the camera recorded in cameras.json" in f for f in result["failing"]))

    def test_blueprint_suggests_the_colour_furthest_from_the_subject(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(blueprint.main(["backdrop", str(self.blueprint)]), 0)
        result = json.loads(out.getvalue())
        self.assertIn(result["backdrop"], blueprint.BACKDROPS.values())
        self.assertGreater(result["distance"], 150)
        chosen = [int(result["backdrop"][i:i + 2], 16) for i in (1, 3, 5)]
        for colour in ((180, 40, 40), (40, 80, 160), (200, 200, 200)):  # the head, the body and the sheet's own ground
            self.assertGreater(math.dist(chosen, colour), 150)
        self.assertNotIn(result["name"], ("red", "blue"))
        self.assertEqual(len(result["others"]), 3)


if __name__ == "__main__":
    unittest.main()
