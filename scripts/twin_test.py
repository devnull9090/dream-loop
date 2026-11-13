"""Tests for mirrored parts sharing one critic: parts.py and verdict.py. Run: python -m unittest scripts/twin_test.py

The creature of inner_test.py (head, body, leg_l and leg_r, where leg_r is same_as leg_l), with
renders drawn here, so no Blender is needed. What Blender measures about the two legs is given
as the "twin" entry it would write.
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
import blueprint  # noqa: E402
import parts  # noqa: E402
import verdict  # noqa: E402
from inner_test import FRONT, PAINT, SIDE, creature, design_sheet  # noqa: E402
from parts_test import pv  # noqa: E402

IDS = {"head": [115, 185, 45], "body": [255, 45, 115], "leg_l": [45, 115, 255], "leg_r": [185, 255, 45]}
MIRROR = {"of": "leg_l", "how": "mirror"}


class TwinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        design_sheet([SIDE, FRONT]).save(self.root / "target.png")
        self.blueprint = self.root / "blueprint.json"
        self.blueprint.write_text(json.dumps(creature()), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(blueprint.main(["validate", str(self.blueprint), "--write"]), 0)
        self.renders = self.root / "inner"
        self.rounds = self.root / "rounds"

    def render(self, twin=MIRROR, hashes=None):
        """Mask renders and pictures of the creature as drawn on the sheet; leg_r carries what Blender found about it."""
        self.renders.mkdir(exist_ok=True)
        for view, shapes, offset in (("side", SIDE, 0), ("front", FRONT, 500)):
            ids, picture = Image.new("RGBA", (500, 500), (0, 0, 0, 0)), Image.new("RGBA", (500, 500), (0, 0, 0, 0))
            for name in ("leg_r", "leg_l", "body", "head"):
                if name in shapes:
                    box = tuple(v - offset * (i % 2 == 0) for i, v in enumerate(shapes[name]))
                    ImageDraw.Draw(ids).ellipse(box, fill=(*IDS[name], 255))
                    ImageDraw.Draw(picture).ellipse(box, fill=(*PAINT[name], 255))
            ids.save(self.renders / f"{view}.ids.png")
            picture.save(self.renders / f"{view}.png")
            data = {"view": view, "ids": f"{view}.ids.png", "picture": f"{view}.png", "resolution": [500, 500], "unassigned": [],
                    "camera": {"yaw": 90 if view == "side" else 0, "pitch": 0, "lens": 85},
                    "parts": {name: {"colour": IDS[name], "objects": 1, "visible": 1.0 if name in shapes else 0.0,
                                     "hash": (hashes or {}).get(name, name + "-v1")} for name in IDS}}
            if twin:
                data["parts"]["leg_r"]["twin"] = twin
            (self.renders / f"{view}.ids.json").write_text(json.dumps(data), encoding="utf-8")

    def prepare(self, n, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = parts.main(["prepare", "--blueprint", str(self.blueprint), "--round", str(n), "--renders", str(self.renders), *extra])
        return status, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def save(self, n, **files):
        for name, data in files.items():
            (self.rounds / f"round-{n}" / "verdicts" / (name.replace("_confirm", "-confirm") + ".json")).write_text(json.dumps(data), encoding="utf-8")

    def others(self, score=5.5):
        return {"head": pv("head", score), "body": pv("body", score)}

    def test_mirrored_legs_go_to_one_critic_and_its_verdict_is_applied_to_both(self):
        self.render()
        status, result, err = self.prepare(1)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual([s["part"] for s in result["send"]], ["head", "body", "leg_l"])  # three critics, not four
        legs = result["send"][2]
        self.assertEqual((legs["parts"], Path(legs["verdict"]).name), (["leg_l", "leg_r"], "leg_l.json"))
        self.assertFalse((self.rounds / "round-1" / "parts" / "leg_r").exists())
        brief = json.loads((Path(legs["folder"]) / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual((brief["part"], brief["views"], [(a["part"], a["views"]) for a in brief["also"]]), ("leg_l", ["side", "front"], [("leg_r", ["front"])]))
        self.assertIn("leg_r is the same geometry as leg_l in the model (mirrored left to right)", brief["twins"])
        self.assertIn("leg_r.front.height", [fact["key"] for fact in brief["settled"]])
        self.assertEqual(brief["also"][0]["measured"]["front"]["height"]["target"], 0.3)
        with Image.open(Path(legs["folder"]) / "pair.png") as pair, Image.open(Path(result["send"][0]["folder"]) / "pair.png") as head:
            self.assertGreater(pair.height / pair.width, 1.3 * head.height / head.width)  # three rows of tall crops: leg_l twice, leg_r once
        self.assertNotIn("parts", result["send"][0])

        self.save(1, leg_l=pv("leg_l", 4.5), **self.others())
        decision = verdict.decide_parts(self.rounds)  # no verdict is owed for leg_r
        self.assertEqual(decision["decision"], "continue")
        self.assertEqual((decision["parts"]["leg_l"], decision["parts"]["leg_r"]), ({"state": "open", "score": 4.5, "gate": "form", "why": "gates still failing"},) * 2)
        self.assertEqual(decision["act_on"].count("round-1/verdicts/leg_l.json"), 1)

        # Round 2: both legs changed together; the shared critic sees what both looked like before, and passes them.
        self.render(hashes={"leg_l": "leg-v2", "leg_r": "leg-v2"})
        status, result, _ = self.prepare(2, "--only", "leg_r")
        self.assertEqual(([s["part"] for s in result["send"]], result["scope"]), (["leg_l"], ["leg_r", "leg_l"]))  # a round about one leg is about both
        self.assertIn("leg_l was added to this round: it is the same geometry as leg_r", result["notes"])
        brief = json.loads((Path(result["send"][0]["folder"]) / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual((brief["has_previous"], [d["id"] for d in brief["previous_directives"]]), (True, ["r1-leg_l-b1"]))  # listed once, not once per leg
        self.save(2, leg_l=pv("leg_l", 8.5, previous=4.5))
        self.assertEqual(verdict.decide_parts(self.rounds)["confirm"], ["leg_l", "leg_r"])
        self.save(2, leg_l_confirm=pv("leg_l", 8.2, previous=4.5))
        decision = verdict.decide_parts(self.rounds)
        self.assertEqual((decision["parts"]["leg_l"]["state"], decision["parts"]["leg_r"]["state"]), ("locked", "locked"))

    def test_a_problem_in_the_shared_verdict_is_reported_once(self):
        self.render()
        self.prepare(1)
        self.save(1, leg_l={**pv("leg_l", 4.5), "part": "leg_r"}, **self.others())
        with self.assertRaises(ValueError) as caught:
            verdict.decide_parts(self.rounds)
        self.assertEqual(str(caught.exception), 'round-1/verdicts/leg_l.json: part must be "leg_l"')

    def test_legs_that_differ_in_the_model_keep_their_own_critics(self):
        self.render(twin={"of": "leg_l", "how": None, "why": "their shapes differ by up to 0.021 H"})
        status, result, _ = self.prepare(1)
        self.assertEqual([s["part"] for s in result["send"]], ["head", "body", "leg_l", "leg_r"])
        self.assertIn("leg_r is same_as leg_l in the blueprint but not in the model (their shapes differ by up to 0.021 H); each gets its own critic", result["notes"])
        self.assertNotIn("twins", json.loads((Path(result["send"][2]["folder"]) / "brief.json").read_text(encoding="utf-8")))

    def test_without_a_measurement_from_blender_nothing_is_assumed(self):
        self.render(twin=None)  # an older render, or one written by something else
        status, result, _ = self.prepare(1)
        self.assertEqual([s["part"] for s in result["send"]], ["head", "body", "leg_l", "leg_r"])
        self.assertFalse([note for note in result["notes"] if "same" in note])

    def test_twins_owed_different_reviews_are_sent_separately(self):
        self.render()
        self.prepare(1)
        self.save(1, leg_l=pv("leg_l", 8.5), leg_l_confirm=pv("leg_l", 8.5), **self.others())
        # The user asks for the right leg alone to be redone: it is reopened, the left stays locked.
        status, result, _ = self.prepare(2, "--unlock", "leg_r=the user wants a brace on this side only")
        self.assertEqual((status, [(s["part"], s.get("parts")) for s in result["send"]]), (0, [("head", None), ("body", None), ("leg_r", None)]))
        self.assertEqual(result["locked"], ["leg_l"])
        self.assertTrue((self.rounds / "round-2" / "parts" / "leg_r" / "brief.json").exists())

    def test_a_twin_of_a_part_that_fails_its_outline_is_held_with_it(self):
        # Only the left leg has an outline, a box where the model is an egg: the right leg is the same wrong shape.
        data = json.loads(self.blueprint.read_text(encoding="utf-8"))
        x, y, w, h = next(p for p in data["parts"] if p["id"] == "leg_l")["boxes"]["front"]
        next(p for p in data["parts"] if p["id"] == "leg_l")["outlines"] = {"front": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]}
        self.blueprint.write_text(json.dumps(data), encoding="utf-8")
        self.render()
        status, result, _ = self.prepare(1)
        self.assertEqual((status, [s["part"] for s in result["send"]], result["held"]), (0, ["head", "body"], ["leg_l", "leg_r"]))
        self.assertIn("leg_r is the same geometry as leg_l, which fails its outline", result["form_failing"])
        self.save(1, **self.others())
        decision = verdict.decide_parts(self.rounds)
        self.assertEqual((decision["decision"], sorted(decision["held"])), ("continue", ["leg_l", "leg_r"]))


if __name__ == "__main__":
    unittest.main()
