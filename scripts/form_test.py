"""Tests for form.py and blueprint.py's outlines on synthetic renders. Run: python -m unittest scripts/form_test.py"""
import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import blueprint  # noqa: E402
import form  # noqa: E402
import masks  # noqa: E402
import parts  # noqa: E402
import verdict  # noqa: E402
from blueprint_test import SIZE, sheet  # noqa: E402
from parts_test import picture, pv  # noqa: E402
from viewmatch_test import COLOURS, FIGURE, complete  # noqa: E402

HEAD = FIGURE["head"]  # the head's box in a 400 x 400 side frame, and at the same pixels on the 800 x 400 target


def ellipse(box, count=48):
    cx, cy, rx, ry = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, (box[2] - box[0]) / 2, (box[3] - box[1]) / 2
    return [(cx + rx * math.cos(2 * math.pi * i / count), cy + ry * math.sin(2 * math.pi * i / count)) for i in range(count)]


def rectangle(box):
    return [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]


def bitten(box):
    """The ellipse with its top-left shoulder cut away square, which leaves its bounds alone."""
    cx, cy, rx, ry = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, (box[2] - box[0]) / 2, (box[3] - box[1]) / 2
    x, y = cx - 0.3 * rx, cy - 0.3 * ry
    left = cx - rx * math.sqrt(1 - 0.09)  # where the cut's horizontal edge meets the ellipse
    top = cy - ry * math.sqrt(1 - 0.09)
    ring = ellipse(box, 96)  # clockwise on screen from the right-hand end: bottom, left, then top
    first = next(i for i, p in enumerate(ring) if p[0] < x and p[1] < y)
    last = max(i for i, p in enumerate(ring) if p[0] < x and p[1] < y)
    return ring[:first] + [(left, y), (x, y), (x, top)] + ring[last + 1:]


def fractions(points, size=SIZE):
    return [[round(x / size[0], 5), round(y / size[1], 5)] for x, y in points]


def render(folder, view, shapes, size=(400, 400), visible=None):
    """An ID render where each part is a polygon in pixels, drawn in the order given."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for name, points in shapes.items():
        draw.polygon(points, fill=(*COLOURS[name], 255))
    image.save(folder / f"{view}.ids.png")
    data = {"view": view, "ids": f"{view}.ids.png", "resolution": list(size), "unassigned": [],
            "camera": {"yaw": 90, "pitch": 0, "lens": 85},
            "parts": {name: {"colour": COLOURS[name], "objects": 1, "hash": name, "visible": (visible or {}).get(name, 1.0)} for name in shapes}}
    (folder / f"{view}.ids.json").write_text(json.dumps(data), encoding="utf-8")


class FormTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        sheet().save(self.root / "target.png")
        self.path = self.root / "blueprint.json"
        self.renders = self.root / "inner"

    def blueprint(self, head=None, body=None, **tolerances):
        data = complete()
        if head:
            data["parts"][0]["outlines"] = {"side": fractions(head)}
        if body:
            data["parts"][1]["outlines"] = {"side": fractions(body)}
        data["tolerances"] = tolerances
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def check(self, data, shapes, **options):
        shapes = {**shapes}  # drawn in this order, so a body given after the head lies in front of it
        shapes.setdefault("body", rectangle(FIGURE["body"]))
        render(self.renders, "side", shapes, visible=options.pop("visible", None))
        return form.check(data, self.path, self.renders, **options)

    def test_a_matching_shape_passes_and_a_part_without_an_outline_is_not_checked(self):
        report = self.check(self.blueprint(head=ellipse(HEAD)), {"head": ellipse(HEAD)})
        self.assertEqual((report["pass"], report["failing"], list(report["parts"])), (True, [], ["head"]))
        side = report["parts"]["head"]["views"]["side"]
        self.assertEqual(side["status"], "pass")
        self.assertGreater(side["overlap"], 0.97)
        self.assertTrue(Path(report["parts"]["head"]["sheet"]).exists())
        self.assertEqual(json.loads((self.renders / "form.json").read_text(encoding="utf-8"))["parts"]["head"]["status"], "pass")

    def test_size_is_not_judged_here_only_shape(self):
        # The same ellipse a tenth larger: the layout gate's business, not this one's.
        grown = (HEAD[0] - 4, HEAD[1] - 4, HEAD[2] + 4, HEAD[3] + 4)
        report = self.check(self.blueprint(head=ellipse(HEAD)), {"head": ellipse(grown)})
        self.assertTrue(report["pass"])

    def test_material_outside_the_outline_is_located_in_absolute_terms(self):
        report = self.check(self.blueprint(head=ellipse(HEAD)), {"head": rectangle(HEAD)})
        self.assertFalse(report["pass"])
        side = report["parts"]["head"]["views"]["side"]
        self.assertAlmostEqual(side["overlap"], math.pi / 4, delta=0.02)
        worst = [d for d in side["differences"] if not d["ok"]]
        self.assertEqual({d["extends"] for d in worst}, {"model"})  # the square has material the ellipse lacks, never the reverse
        self.assertEqual({d["at"] for d in worst}, {"top left", "top right", "bottom left", "bottom right"})
        # A corner of a 0.25 H square stands 0.25 H * (1 - 1/sqrt 2) / 2 = 0.037 H outside the inscribed circle, measured along an axis.
        self.assertAlmostEqual(worst[0]["reach_H"], 0.037, delta=0.006)
        self.assertEqual(report["failing"][0], "head outline in side: overlap 0.78, needs 0.85")
        corner = next(line for line in report["failing"] if "top left" in line)
        self.assertRegex(corner, r"^head outline in side: model extends 0\.0\d+ H beyond the target at the top left \(0\.0\d? across, 0\.0\d? down its box\)$")
        self.assertFalse(any("target extends" in line for line in report["failing"]))

    def test_material_missing_from_the_model_is_reported_the_other_way_round(self):
        report = self.check(self.blueprint(head=ellipse(HEAD)), {"head": bitten(HEAD)})
        side = report["parts"]["head"]["views"]["side"]
        self.assertEqual(side["status"], "fail")
        worst = [d for d in side["differences"] if not d["ok"]]
        self.assertEqual([(d["extends"], d["at"]) for d in worst], [("target", "top left")])
        self.assertLess(worst[0]["across"], 0.35)
        self.assertLess(worst[0]["down"], 0.35)
        self.assertTrue(any(line.startswith("head outline in side: target extends 0.") and "top left" in line for line in report["failing"]))
        self.assertFalse(any("model extends" in line and "bottom" in line for line in report["failing"]))

    def test_tolerances_come_from_the_blueprint(self):
        report = self.check(self.blueprint(head=ellipse(HEAD), outline_iou=0.7, outline_reach=0.3), {"head": rectangle(HEAD)})
        self.assertTrue(report["pass"])
        self.assertEqual(report["parts"]["head"]["views"]["side"]["needs"], 0.7)

    def test_what_another_target_part_covers_is_not_held_against_the_model(self):
        # The target shows the head with a bite out of it. With nothing to explain the bite, a whole head has material to lose.
        whole = {"head": ellipse(HEAD)}
        self.assertFalse(self.check(self.blueprint(head=bitten(HEAD)), whole)["pass"])
        # When the bite is where the target shows another part in front, the head may well continue under it: not judged.
        bite = rectangle((HEAD[0] - 10, HEAD[1] - 10, HEAD[0] + 30, HEAD[1] + 30))
        report = self.check(self.blueprint(head=bitten(HEAD), body=bite), whole, only=["head"])
        self.assertTrue(report["pass"], report["failing"])
        with Image.open(report["parts"]["head"]["sheet"]) as diff:
            self.assertIn((110, 110, 110), {colour for _, colour in diff.getcolors(1 << 16)})  # drawn grey, not green

    def test_what_another_model_part_covers_is_not_held_against_the_model(self):
        # A small part in front of the head hides a patch of it: the target's outline there is not counted as missing.
        patch = rectangle((185, 65, 215, 95))
        report = self.check(self.blueprint(head=ellipse(HEAD)), {"head": ellipse(HEAD), "body": patch})
        side = report["parts"]["head"]["views"]["side"]
        self.assertEqual(side["status"], "pass")
        self.assertAlmostEqual(side["covered"], 0.18, delta=0.03)

    def test_a_view_where_a_neighbour_hides_too_much_is_not_judged_and_says_so(self):
        cover = rectangle((165, 45, 235, 115))
        report = self.check(self.blueprint(head=ellipse(HEAD)), {"head": ellipse(HEAD), "body": cover})
        self.assertEqual((report["pass"], report["parts"]["head"]["status"]), (True, "unjudged"))
        self.assertRegex(report["notes"][0], r"^head outline in side: not judged \(body covers \d\d% of where the target shows it")
        # ... and likewise when the layout measure already found the part mostly hidden there.
        measured = {"views": {"side": {"parts": {"head": {"status": "hidden"}}}}}
        report = self.check(self.blueprint(head=ellipse(HEAD)), {"head": ellipse(HEAD)}, measured=measured, visible={"head": 0.2})
        self.assertEqual(report["parts"]["head"]["status"], "unjudged")
        self.assertIn("only 20% of it shows", report["notes"][0])

    def test_command_line(self):
        self.blueprint(head=ellipse(HEAD))
        render(self.renders, "side", {"body": rectangle(FIGURE["body"]), "head": rectangle(HEAD)})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = form.main(["--blueprint", str(self.path), "--renders", str(self.renders)])
        result = json.loads(out.getvalue())
        self.assertEqual((status, result["pass"], result["parts"]["head"]["status"]), (1, False, "fail"))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(form.main(["--blueprint", str(self.path), "--renders", str(self.renders), "--part", "tail"]), 2)
        self.assertIn("unknown part tail", err.getvalue())


class OutlineBlueprintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        sheet().save(self.root / "target.png")
        self.path = self.root / "blueprint.json"

    def save(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = blueprint.main(list(argv))
        return status, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def test_validate_checks_outlines(self):
        data = complete()
        data["parts"][0]["outlines"] = {"side": fractions(ellipse(HEAD))}
        self.assertEqual(blueprint.problems(data), ([], []))
        data["parts"][0]["outlines"] = {"side": [[0.2, 0.1], [0.3, 0.1]]}
        self.assertIn("part head: outline in view side must be at least three x, y points as fractions of the target", blueprint.problems(data)[0])
        data["parts"][0]["outlines"] = {"top": fractions(ellipse(HEAD))}
        self.assertIn("part head: outline in view top needs the part's box in that view", blueprint.problems(data)[0])
        data["parts"][0]["outlines"] = {"side": fractions(ellipse((HEAD[0], HEAD[1], HEAD[2], HEAD[3] - 30)))}
        errors, warnings = blueprint.problems(data)
        self.assertEqual(errors, [])
        self.assertIn("does not fill the part's box", warnings[0])
        data["tolerances"] = {"outline_iou": 0.9, "outline_reach": 0.1}
        self.assertEqual(blueprint.problems(data)[0], [])

    def test_trace_follows_a_region_s_outer_boundary(self):
        grid = ["....", ".##.", ".#..", "...."]
        inside = [c == "#" for row in grid for c in row]
        self.assertEqual(blueprint.trace(inside, 4, 4), [(3, 1), (3, 2), (2, 2), (2, 3), (1, 3), (1, 1)])

    def test_outline_is_proposed_from_the_part_s_colours_and_written_on_request(self):
        self.save(complete())
        status, result, err = self.run_main("outline", str(self.path), "--part", "head", "--out", str(self.root / "outlines.png"))
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(sorted(result["outlines"]["head"]), ["front", "side"])
        self.assertTrue((self.root / "outlines.png").exists())
        self.assertNotIn("outlines", json.loads(self.path.read_text(encoding="utf-8"))["parts"][0])  # nothing stored without --write
        points = result["outlines"]["head"]["side"]
        self.assertLessEqual(len(points), 28)
        # The proposal is the head's ellipse on the sheet, not its box and not the body below it.
        drawn, truth = Image.new("L", SIZE), Image.new("L", SIZE)
        ImageDraw.Draw(drawn).polygon([(x * SIZE[0], y * SIZE[1]) for x, y in points], fill=255)
        ImageDraw.Draw(truth).ellipse(HEAD, fill=255)
        union = masks.area(Image.composite(drawn, truth, drawn))
        both = masks.area(Image.composite(truth, Image.new("L", SIZE), drawn))
        self.assertGreater(both / union, 0.93)

        status, result, _ = self.run_main("outline", str(self.path), "--write")
        self.assertEqual((status, result["written"], result["kept"]), (0, True, []))
        self.assertEqual(result["traced"], {"head": ["front", "side"], "body": ["front", "side"]})
        written = blueprint.load(self.path)
        self.assertEqual(blueprint.problems(written), ([], []))  # the proposals fill their boxes
        # Hand corrections survive a second run unless --replace is given.
        written["parts"][0]["outlines"]["side"] = fractions(rectangle(HEAD))
        self.save(written)
        status, result, _ = self.run_main("outline", str(self.path), "--part", "head", "--view", "side", "--write")
        self.assertEqual(result["kept"], ["head in side"])
        self.assertEqual(len(blueprint.load(self.path)["parts"][0]["outlines"]["side"]), 4)
        self.run_main("outline", str(self.path), "--part", "head", "--view", "side", "--write", "--replace")
        self.assertGreater(len(blueprint.load(self.path)["parts"][0]["outlines"]["side"]), 4)
        self.assertEqual(self.run_main("outline", str(self.path), "--part", "tail")[0], 2)
        self.assertEqual(self.run_main("overlay", str(self.path), "--out", str(self.root / "blueprint.png"))[0], 0)


class FormGateTest(unittest.TestCase):
    """The outline gate inside a review round: a part that fails it is not sent to a critic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        sheet().save(self.root / "target.png")
        self.blueprint = self.root / "blueprint.json"
        data = complete()
        data["parts"][0]["outlines"] = {"side": fractions(ellipse(HEAD))}
        self.blueprint.write_text(json.dumps(data), encoding="utf-8")
        self.renders = self.root / "inner"
        self.rounds = self.root / "rounds"

    def render(self, head):
        front = {"head": (170, 40, 230, 120), "body": FIGURE["body"]}
        render(self.renders, "side", {"body": rectangle(FIGURE["body"]), "head": head})
        render(self.renders, "front", {"body": rectangle(front["body"]), "head": ellipse(front["head"])})
        for view, shapes in (("side", FIGURE), ("front", front)):
            picture(shapes).save(self.renders / f"{view}.png")
            path = self.renders / f"{view}.ids.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["picture"] = f"{view}.png"
            path.write_text(json.dumps(data), encoding="utf-8")

    def prepare(self, n):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = parts.main(["prepare", "--blueprint", str(self.blueprint), "--round", str(n), "--renders", str(self.renders)])
        return status, json.loads(out.getvalue())

    def test_a_part_failing_its_outline_is_held_back_and_the_others_go_on(self):
        self.render(rectangle(HEAD))
        status, result = self.prepare(1)
        self.assertEqual((status, [s["part"] for s in result["send"]], result["held"]), (0, ["body"], ["head"]))
        self.assertIn("head outline in side: overlap 0.78, needs 0.85", result["form_failing"])
        self.assertFalse((self.rounds / "round-1" / "parts" / "head").exists())
        (self.rounds / "round-1" / "verdicts" / "body.json").write_text(json.dumps(pv("body", 5.0)), encoding="utf-8")
        decision = verdict.decide_parts(self.rounds)
        self.assertEqual((decision["decision"], list(decision["held"])), ("continue", ["head"]))
        self.assertTrue(any("model extends" in line for line in decision["held"]["head"]))
        self.assertEqual(decision["parts"]["head"]["state"], "open")

    def test_when_nothing_can_be_sent_the_decision_is_form_and_no_verdict_is_owed(self):
        self.render(rectangle(HEAD))
        self.prepare(1)
        for name in ("body", "body-confirm"):
            (self.rounds / "round-1" / "verdicts" / f"{name}.json").write_text(json.dumps(pv("body", 8.5)), encoding="utf-8")
        status, result = self.prepare(2)
        self.assertEqual((status, result["send"], result["held"]), (1, [], ["head"]))  # not the assembly either: the head is still open
        decision = verdict.decide_parts(self.rounds)
        self.assertEqual(decision["decision"], "form")
        self.assertIn("outline gate fails for head", decision["why"])

    def test_a_part_passing_its_outline_goes_to_the_critic_with_that_fact(self):
        self.render(ellipse(HEAD))
        status, result = self.prepare(1)
        self.assertEqual((status, [s["part"] for s in result["send"]], result["held"]), (0, ["head", "body"], []))
        brief = json.loads((self.rounds / "round-1" / "parts" / "head" / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual(list(brief["outline"]), ["side"])
        self.assertGreater(brief["outline"]["side"]["overlap"], 0.95)
        self.assertIn("do not ask for the silhouette to change", brief["scope"])
        body = json.loads((self.rounds / "round-1" / "parts" / "body" / "brief.json").read_text(encoding="utf-8"))
        self.assertNotIn("outline", body)
        self.assertNotIn("silhouette", body["scope"])


if __name__ == "__main__":
    unittest.main()
