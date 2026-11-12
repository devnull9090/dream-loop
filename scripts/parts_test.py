"""Tests for parts.py, ledger.py and verdict.py's per-part mode. Run: python -m unittest scripts/parts_test.py"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import ledger  # noqa: E402
import parts  # noqa: E402
import verdict  # noqa: E402
from blueprint_test import sheet  # noqa: E402
from viewmatch_test import FIGURE, complete, fake_render  # noqa: E402

FRONT = {"head": (170, 40, 230, 120), "body": FIGURE["body"]}
SIDE = dict(FIGURE)
PAINT = {"head": (180, 40, 40), "body": (40, 80, 160)}


def picture(shapes, paint=None, ground=None, size=(400, 400), busy=False):
    """A product picture of the figure: transparent ground unless one is given."""
    image = Image.new("RGBA", size, (*ground, 255) if ground else (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if busy:
        for x in range(0, size[0], 20):
            draw.rectangle((x, 0, x + 9, size[1]), fill=(90, 120, 60, 255))
    for name, box in shapes.items():
        (draw.ellipse if name == "head" else draw.rectangle)(box, fill=(*{**PAINT, **(paint or {})}[name], 255))
    return image


def item(part, n=1, change="replace the smooth dome with three overlapping plates", **extra):
    return {"id": f"r1-{part}-b{n}", "where": "side", "target": "three overlapping plates", "current": "one smooth dome", "change": change, **extra}


def pv(part, score, previous=None, regressions=(), **extra):
    """A part verdict with the gate its score implies."""
    gate = next(g for low, g in [(9, None), (7, "surface_detail"), (5, "colour_material"), (3, "form")] if score >= low)
    data = {"part": part, "score": score, "first_failed_gate": gate, "blockers": [item(part)] if gate else [], "next": []}
    if previous is not None:
        data.update(previous_score=previous, regressions=[{"where": "side", "what": w} for w in regressions], previous_directives=[])
    data.update(extra)
    return data


class PartsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        sheet().save(self.root / "target.png")
        self.blueprint = self.root / "blueprint.json"
        self.blueprint.write_text(json.dumps(complete()), encoding="utf-8")
        self.renders = self.root / "inner"
        self.rounds = self.root / "rounds"

    def render(self, hashes=None, side=None, front=None, paint=None):
        for view, shapes in (("side", {**SIDE, **(side or {})}), ("front", {**FRONT, **(front or {})})):
            fake_render(self.renders, view, shapes, hashes=hashes)
            picture(shapes, paint).save(self.renders / f"{view}.png")
            path = self.renders / f"{view}.ids.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["picture"] = f"{view}.png"
            path.write_text(json.dumps(data), encoding="utf-8")

    def prepare(self, n, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = parts.main(["prepare", "--blueprint", str(self.blueprint), "--round", str(n), "--renders", str(self.renders), *extra])
        return status, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def verdicts(self, n, **files):
        for name, data in files.items():
            (self.rounds / f"round-{n}" / "verdicts" / (name.replace("_confirm", "-confirm") + ".json")).write_text(json.dumps(data), encoding="utf-8")

    def decide(self, **options):
        return verdict.decide_parts(self.rounds, **options)

    def brief(self, n, part):
        return json.loads((self.rounds / f"round-{n}" / "parts" / part / "brief.json").read_text(encoding="utf-8"))

    def test_first_round_sends_every_part_with_its_own_pair_and_brief(self):
        self.render()
        status, result, err = self.prepare(1)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual([(s["part"], s["role"]) for s in result["send"]], [("head", "critic"), ("body", "critic")])
        self.assertEqual((result["layout"], result["regressions"], result["locked"]), (True, [], []))
        brief = self.brief(1, "head")
        self.assertEqual((brief["views"], brief["has_previous"], brief["previous_directives"]), (["side", "front"], False, []))
        self.assertEqual(brief["colours"][0]["hex"], "#b42828")
        self.assertEqual(brief["measured"]["side"]["height"]["target"], 0.25)
        self.assertIn("head.side.height", [fact["key"] for fact in brief["settled"]])
        with Image.open(self.rounds / "round-1" / "parts" / "head" / "pair.png") as pair:
            self.assertGreater(pair.width, pair.height * 0.8)  # two views, two crops each
            self.assertLessEqual(max(pair.size), 1568)
        review = json.loads((self.rounds / "round-1" / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(review["parts"]["assembly"]["send"], None)
        self.assertEqual(review["views"]["side"]["capture_source"], "blender")
        self.assertTrue(Path(result["send"][0]["verdict"]).parent.is_dir())

    def test_failed_layout_stops_the_round_before_any_critic(self):
        self.render(side={"head": (140, 0, 260, 120)})
        status, result, _ = self.prepare(1)
        self.assertEqual((status, result["layout"], result["send"]), (1, False, []))
        self.assertTrue(any("target 0.25 H, currently" in line for line in result["failing"]))
        decision = self.decide()
        self.assertEqual(decision["decision"], "layout")
        self.assertIn("measured layout fails", decision["why"])

    def test_far_out_layout_says_restart(self):
        wrong = {"head": (100, 0, 300, 200), "body": (190, 200, 210, 360)}
        self.render(side=wrong, front=wrong)
        self.assertEqual(self.prepare(1)[0], 1)
        decision = self.decide()
        self.assertEqual(decision["decision"], "restart")
        self.assertIn("rebuild it from the blockout", decision["why"])

    def test_every_view_must_be_rendered(self):
        self.render()
        (self.renders / "front.ids.json").unlink()
        status, _, err = self.prepare(1)
        self.assertEqual(status, 2)
        self.assertIn("every view in the blueprint must be captured", err)

    def test_missing_verdict_is_a_problem_not_a_decision(self):
        self.render()
        self.prepare(1)
        self.verdicts(1, head=pv("head", 4.0))
        with self.assertRaisesRegex(ValueError, "body.json: no verdict yet"):
            self.decide()

    def test_open_passing_locked_then_assembly_then_done(self):
        self.render()
        self.prepare(1)
        self.verdicts(1, head=pv("head", 8.5), body=pv("body", 5.5))
        decision = self.decide()
        self.assertEqual((decision["decision"], decision["confirm"]), ("confirm", ["head"]))
        self.assertEqual((decision["parts"]["head"]["state"], decision["parts"]["body"]["state"]), ("passing", "open"))
        self.verdicts(1, head_confirm=pv("head", 8.2))
        decision = self.decide()
        self.assertEqual((decision["decision"], decision["act_on"]), ("continue", ["round-1/verdicts/body.json"]))
        self.assertEqual(decision["parts"]["head"]["state"], "locked")

        # Round 2: only the body changed. The locked head is not sent again; the body's pair shows the previous crop.
        self.render(hashes={"body": "body-v2"}, paint={"body": (40, 90, 170)})
        status, result, _ = self.prepare(2)
        self.assertEqual((status, [(s["part"], s["role"]) for s in result["send"]], result["locked"]), (0, [("body", "critic")], ["head"]))
        brief = self.brief(2, "body")
        self.assertTrue(brief["has_previous"])
        self.assertEqual(brief["previous_directives"], [{"id": "r1-body-b1", "change": item("body")["change"]}])
        with self.assertRaisesRegex(ValueError, "previous_score is required"):
            self.verdicts(2, body=pv("body", 8.5))
            self.decide()
        self.verdicts(2, body=pv("body", 8.5, previous=5.5), body_confirm=pv("body", 8.0, previous=5.5))
        decision = self.decide()
        self.assertEqual((decision["decision"], decision["parts"]["body"]["state"]), ("continue", "locked"))
        self.assertIn("assembly", decision["why"])

        # Round 3: nothing changed, every part is locked, so the whole subject is judged.
        status, result, _ = self.prepare(3)
        self.assertEqual([(s["part"], s["role"]) for s in result["send"]], [("assembly", "critic")])
        self.assertIn("how they join", self.brief(3, "assembly")["form"])
        self.verdicts(3, assembly=pv("assembly", 8.5))
        self.assertEqual(self.decide()["decision"], "confirm")
        self.verdicts(3, assembly_confirm=pv("assembly", 7.5))
        self.assertEqual(self.decide()["decision"], "continue")
        self.verdicts(3, assembly_confirm=pv("assembly", 9.0))
        decision = self.decide()
        self.assertEqual((decision["decision"], decision["regressions"]), ("done", 0))

    def lock_head(self):
        self.render()
        self.prepare(1)
        self.verdicts(1, head=pv("head", 8.5), head_confirm=pv("head", 8.5), body=pv("body", 5.5))

    def test_a_locked_part_that_changes_is_a_regression(self):
        self.lock_head()
        self.render(hashes={"head": "head-v2"})
        status, result, _ = self.prepare(2)
        self.assertEqual((status, result["regressions"], result["send"]), (1, ["head"], []))
        decision = self.decide()
        self.assertEqual((decision["decision"], decision["regressions"]), ("revert", 1))
        self.assertIn("--unlock", decision["why"])

    def test_unlock_reopens_a_locked_part_on_purpose(self):
        self.lock_head()
        self.render(hashes={"head": "head-v2"})
        status, result, _ = self.prepare(2, "--unlock", "head=jaw redesigned after the user's note")
        self.assertEqual((status, result["regressions"]), (0, []))
        self.assertEqual(sorted(s["part"] for s in result["send"]), ["body", "head"])
        review = json.loads((self.rounds / "round-2" / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(review["unlocked"], {"head": "jaw redesigned after the user's note"})

    def test_pixels_catch_a_change_the_model_hash_cannot_see(self):
        self.lock_head()
        self.render()  # same model, same hashes
        repainted, front = self.root / "side-capture.png", self.root / "front-capture.png"
        picture(SIDE, {"head": (40, 200, 60)}).save(repainted)
        picture(FRONT).save(front)
        status, result, _ = self.prepare(2, "--capture", f"side={repainted}", "--capture", f"front={front}")
        self.assertEqual((status, result["regressions"]), (1, ["head"]))
        status, result, _ = self.prepare(2)  # the same capture as before: nothing changed
        self.assertEqual((status, result["regressions"], result["locked"]), (0, [], ["head"]))

    def test_repainting_a_neighbour_or_the_background_does_not_trip_a_lock(self):
        self.lock_head()
        self.render()
        side, front = self.root / "side-capture.png", self.root / "front-capture.png"
        picture(SIDE, {"body": (230, 220, 40)}, ground=(15, 60, 90)).save(side)  # the body touches the head's crop
        picture(FRONT, {"body": (230, 220, 40)}, ground=(15, 60, 90)).save(front)
        status, result, _ = self.prepare(2, "--capture", f"side={side}", "--capture", f"front={front}")
        self.assertEqual((status, result["regressions"], result["locked"]), (0, [], ["head"]))
        self.assertEqual([s["part"] for s in result["send"]], ["body"])

    def test_product_captures_are_needed_for_every_view_or_none(self):
        self.render()
        side = self.root / "side-capture.png"
        picture(SIDE, ground=(70, 70, 70)).save(side)
        status, _, err = self.prepare(1, "--capture", f"side={side}")
        self.assertEqual(status, 2)
        self.assertIn("no --capture for view(s) front", err)

    def test_a_passing_part_left_unconfirmed_is_sent_for_confirmation_next_round(self):
        self.render()
        self.prepare(1)
        self.verdicts(1, head=pv("head", 8.5), body=pv("body", 5.5))
        self.render(hashes={"body": "body-v2"})
        _, result, _ = self.prepare(2)
        self.assertEqual({s["part"]: s["role"] for s in result["send"]}, {"head": "confirm", "body": "critic"})
        self.assertTrue(result["send"][0]["verdict"].endswith("head-confirm.json"))
        self.verdicts(2, body=pv("body", 6.2, previous=5.5))
        self.assertEqual(self.decide()["decision"], "confirm")
        self.verdicts(2, head_confirm=pv("head", 8.0))
        decision = self.decide()
        self.assertEqual((decision["decision"], decision["parts"]["head"]["state"]), ("continue", "locked"))

    def test_revert_rethink_and_round_limit_per_part(self):
        self.render()
        self.prepare(1)
        self.verdicts(1, head=pv("head", 6.0), body=pv("body", 5.5))
        for n in (2, 3):
            self.render(hashes={"head": f"head-v{n}", "body": f"body-v{n}"})
            self.prepare(n)
            self.verdicts(n, head=pv("head", 6.2, previous=6.0), body=pv("body", 5.5, previous=5.5))
        decision = self.decide()
        self.assertEqual(decision["decision"], "rethink")
        self.assertIn("body", decision["why"])
        self.assertEqual(self.decide(max_rounds=3)["decision"], "ask_user")
        self.verdicts(3, head=pv("head", 5.0, previous=6.2), body=pv("body", 6.5, previous=5.5))
        decision = self.decide()
        self.assertEqual((decision["decision"], decision["act_on"]), ("revert", ["round-3/verdicts/body.json", "round-3/verdicts/head.json"]))

    def test_part_verdict_rules(self):
        self.render()
        self.prepare(1)
        good = pv("body", 5.5)
        cases = [
            (pv("head", 4.0, blockers=[item("head", change="shorten the jaw plate by 40%")]), "relative change"),
            (pv("head", 4.0, blockers=[item("head", change="make the eye half the size")]), "relative change"),
            (pv("head", 4.0, blockers=[item("head", change="scale the brow plate 1.5x")]), "relative change"),
            (pv("torso", 4.0), 'part must be "head"'),
            ({**pv("head", 4.0), "first_failed_gate": "layout"}, "first_failed_gate must be one of"),
            (pv("head", 8.0, first_failed_gate="form"), "outside 3-5"),
            (pv("head", 4.0, blockers=[item("head", measure="head.side.height", target_value=0.36)]), "head.side.height is settled at 0.25 .blueprint."),
            (pv("head", 4.0, blockers=[item("head", measure="head.side.height")]), "is settled at 0.25"),
        ]
        for data, message in cases:
            with self.subTest(message=message, change=data["blockers"][0].get("change") if data.get("blockers") else None):
                self.verdicts(1, head=data, body=good)
                with self.assertRaisesRegex(ValueError, message):
                    self.decide()
        allowed = [
            item("head", 1, change="reduce visor roughness by 40% and tint the rim #4f7f6a"),
            item("head", 2, change="jaw plate depth 0.06 H, currently 0.10 H"),
            item("head", 3, change="scale the jaw plate down 40% to 0.06 H, currently 0.10 H"),
            item("head", 4, change="keep the head height", measure="head.side.height", target_value=0.25),
            item("head", 5, change="head height 0.31 H, currently 0.25 H", measure="head.side.height", target_value=0.31,
                 evidence="the side view's head box stops at the brow; the crest above it belongs to the head"),
        ]
        self.verdicts(1, head=pv("head", 4.0, blockers=allowed), body=good)
        decision = self.decide()
        self.assertEqual(decision["decision"], "continue")
        self.assertEqual([(d["part"], d["id"], d["measure"]) for d in decision["disputes"]], [("head", "r1-head-b5", "head.side.height")])

    def test_ledger_facts_reach_the_brief_and_bind_the_critic(self):
        path = self.root / "ledger.json"
        ledger.add(path, "eye diameter is 0.04 H", part="head", key="head.eye.diameter", value=0.04, source="round 2")
        ledger.add(path, "the user accepted a darker overall palette than the board")
        ledger.add(path, "body plates are riveted, not welded", part="body")
        with self.assertRaisesRegex(ValueError, "already settled"):
            ledger.add(path, "eye diameter is 0.05 H", part="head", key="head.eye.diameter", value=0.05)
        self.render()
        self.prepare(1)
        facts = [fact["fact"] for fact in self.brief(1, "head")["settled"] if fact["source"] != "blueprint"]
        self.assertEqual(facts, ["eye diameter is 0.04 H", "the user accepted a darker overall palette than the board"])
        self.verdicts(1, body=pv("body", 5.5),
                      head=pv("head", 4.0, blockers=[item("head", change="eye diameter 0.07 H, currently 0.04 H", measure="head.eye.diameter", target_value=0.07)]))
        with self.assertRaisesRegex(ValueError, r"head.eye.diameter is settled at 0.04 \(round 2\)"):
            self.decide()
        ledger.withdraw(path, "f1", "target redrawn with larger eyes")
        self.assertEqual([f["id"] for f in ledger.facts_for(path, "head")], ["f2"])

    def test_ledger_command_line(self):
        path = str(self.root / "ledger.json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ledger.main(["add", path, "--fact", "tail has nine segments", "--part", "tail"]), 0)
            self.assertEqual(ledger.main(["add", path, "--fact", "x", "--key", "a.b"]), 2)
            self.assertEqual(ledger.main(["withdraw", path, "f9", "--reason", "x"]), 2)
            self.assertEqual(ledger.main(["withdraw", path, "f1", "--reason", "miscounted"]), 0)
            self.assertEqual(ledger.main(["list", path]), 0)
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1]), [])

    def test_product_capture_is_checked_against_the_mask_render(self):
        self.render()
        good, moved, busy = (self.root / name for name in ("good.png", "moved.png", "busy.png"))
        picture(SIDE, ground=(70, 70, 70)).save(good)
        picture(FRONT, ground=(70, 70, 70)).save(self.root / "front.png")
        front = ("--capture", f"front={self.root / 'front.png'}")
        status, result, _ = self.prepare(1, "--capture", f"side={good}", *front)
        self.assertEqual((status, result["warnings"]), (0, []))
        review = json.loads((self.rounds / "round-1" / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(review["views"]["side"]["capture_source"], "product")
        self.assertGreater(review["views"]["side"]["alignment"]["capture_iou"], 0.95)

        picture({"head": (40, 140, 100, 200), "body": (25, 200, 115, 380)}, ground=(70, 70, 70)).save(moved)  # other camera
        status, result, _ = self.prepare(1, "--capture", f"side={moved}", *front)
        self.assertEqual((status, result["send"]), (1, []))
        self.assertTrue(any("take the capture with the camera recorded in cameras.json" in line for line in result["failing"]))

        picture(SIDE, ground=(70, 70, 70), busy=True).save(busy)  # an engine scene behind the subject
        status, result, _ = self.prepare(1, "--capture", f"side={busy}", *front)
        self.assertEqual(status, 0)
        self.assertIn("assumed to be framed like the mask render", result["warnings"][0])
        mask = self.root / "mask.png"
        picture(SIDE, {"head": (255, 255, 255), "body": (255, 255, 255)}, ground=(0, 0, 0)).save(mask)
        status, result, _ = self.prepare(1, "--capture", f"side={busy}", "--capture-mask", f"side={mask}", *front)
        self.assertEqual((status, result["warnings"]), (0, []))

    def test_capture_at_another_resolution_maps_part_boxes(self):
        self.render()
        big = self.root / "big.png"
        picture({name: tuple(v * 2 for v in box) for name, box in SIDE.items()}, ground=(70, 70, 70), size=(800, 800)).save(big)
        picture(FRONT, ground=(70, 70, 70)).save(self.root / "front.png")
        self.assertEqual(self.prepare(1, "--capture", f"side={big}", "--capture", f"front={self.root / 'front.png'}")[0], 0)
        review = json.loads((self.rounds / "round-1" / "review.json").read_text(encoding="utf-8"))
        self.assertEqual([round(v, 2) for v in review["parts"]["head"]["boxes"]["side"]], [0.4, 0.1, 0.2, 0.2])

    def test_flat_verdict_folder_still_uses_the_whole_image_rule(self):
        flat = self.root / "verdicts"
        flat.mkdir()
        whole = {"score": 5.5, "first_failed_gate": "form_materials", "next": [],
                 "blockers": [{"id": "r1-b1", "where": "C2", "target": "t", "current": "c", "change": "replace it"}]}
        (flat / "round-1.json").write_text(json.dumps(whole), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(verdict.main([str(flat)]), 0)
        self.assertEqual(json.loads(out.getvalue())["decision"], "continue")
        self.render()
        self.prepare(1)
        self.verdicts(1, head=pv("head", 4.0), body=pv("body", 5.5))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(verdict.main([str(self.rounds)]), 0)
        self.assertEqual(sorted(json.loads(out.getvalue())["parts"]), ["assembly", "body", "head"])


if __name__ == "__main__":
    unittest.main()
