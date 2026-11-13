"""Tests for what keeps critics consistent: sampled colour pairs in the brief (parts.py) and reversal flags (verdict.py).

Run: python -m unittest scripts/critic_facts_test.py
"""
import json
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import parts  # noqa: E402
import scope_test  # noqa: E402
import twin_test  # noqa: E402
import verdict  # noqa: E402
from parts_test import item, pv  # noqa: E402


def directive(part, n, where, change, round_number=1, **extra):
    return {**item(part, n, change), "id": f"r{round_number}-{part}-b{n}", "where": where, **extra}


class ColourTest(scope_test.Rounds):
    def test_the_brief_pairs_the_target_s_colour_with_the_capture_s_at_the_same_point(self):
        self.render()
        self.prepare(1)
        brief = self.brief(1, "head")
        skin = brief["colours"][0]
        self.assertEqual((skin["name"], skin["hex"], skin["capture"]), ("skin", "#b42828", "#b42828"))
        self.assertEqual(skin["difference"], {"lightness": 0.0, "chroma": 0.0, "delta_e": 0.0, "hue": 0, "reads": "capture is the same (delta E 0.0)"})
        self.assertIn("Do not take colour values from the crops by eye", brief["colour_rule"])
        body = self.brief(1, "body")  # its colour was typed as a hex with no point to sample at: nothing to pair it with
        self.assertEqual((body["colours"], "colour_rule" in body), ([{"name": "cloth", "hex": "#2850a0"}], False))

    def test_a_darker_duller_product_is_said_so_in_numbers(self):
        self.render(paint={"head": (120, 50, 50)})
        self.prepare(1)
        skin = self.brief(1, "head")["colours"][0]
        self.assertEqual(skin["capture"], "#783232")
        self.assertLess(skin["difference"]["lightness"], -8)
        self.assertLess(skin["difference"]["chroma"], -10)
        self.assertRegex(skin["difference"]["reads"], r"^capture is [\d.]+ L\* darker, chroma [\d.]+ lower, hue \d+ degrees off \(delta E [\d.]+\)$")

    def test_difference_names_hue_only_for_colours_that_have_one(self):
        self.assertNotIn("hue", parts.colour_difference("#808080", "#909090"))
        turned = parts.colour_difference("#b42828", "#b46428")
        self.assertGreater(turned["hue"], 15)
        self.assertIn("degrees off", turned["reads"])

    def test_the_sample_comes_from_the_part_even_when_the_point_just_misses_it(self):
        # A round part in a square box: the box's corner is not on the part, in the mask or in the capture.
        mask, capture = Image.new("L", (200, 200)), Image.new("RGB", (400, 400), (40, 40, 40))
        ImageDraw.Draw(mask).ellipse((50, 50, 150, 150), fill=255)
        ImageDraw.Draw(capture).ellipse((100, 100, 300, 300), fill=(200, 60, 60))
        box = [0.25, 0.25, 0.5, 0.5]
        self.assertEqual(parts.sample_on_part(capture, box, mask, box, (0.5, 0.5)), "#c83c3c")
        self.assertEqual(parts.sample_on_part(capture, box, mask, box, (0.12, 0.12)), "#c83c3c")  # the part's paint, not the ground beside it
        self.assertIsNone(parts.sample_on_part(capture, box, Image.new("L", (200, 200)), box, (0.5, 0.5)))


class TwinColourTest(twin_test.TwinTest):
    test_mirrored_legs_go_to_one_critic_and_its_verdict_is_applied_to_both = None
    test_a_problem_in_the_shared_verdict_is_reported_once = None
    test_legs_that_differ_in_the_model_keep_their_own_critics = None
    test_without_a_measurement_from_blender_nothing_is_assumed = None
    test_twins_owed_different_reviews_are_sent_separately = None

    def test_a_same_as_part_is_sampled_on_itself(self):
        self.render(twin=None)
        _, result, _ = self.prepare(1)
        folder = next(s["folder"] for s in result["send"] if s["part"] == "leg_r")
        limb = json.loads((Path(folder) / "brief.json").read_text(encoding="utf-8"))["colours"][0]
        self.assertEqual((limb["hex"], limb["capture"]), ("#3c8c46", "#3c8c46"))

    def test_two_critics_of_a_pair_that_ask_for_opposite_things_are_flagged(self):
        self.render(twin=None)
        self.prepare(1)
        left = pv("leg_l", 4.5, blockers=[directive("leg_l", 1, "front, shin", "shin width 0.05 H, currently 0.04 H")])
        right = pv("leg_r", 4.5, blockers=[directive("leg_r", 1, "front, shin", "shin width 0.03 H, currently 0.04 H")])
        self.save(1, leg_l=left, leg_r=right, **self.others())
        decision = verdict.decide_parts(self.rounds)
        self.assertEqual(decision["decision"], "continue")
        self.assertEqual(decision["reversals"], [{"part": "leg_r", "id": "r1-leg_r-b1", "where": "front, shin", "against": "r1-leg_l-b1",
                                                  "in": "round-1/verdicts/leg_l.json",
                                                  "why": "it asked for 0.05 H from 0.04 H; this asks for 0.03 H from 0.04 H"}])


class ReversalTest(scope_test.Rounds):
    def rounds_of(self, first, second, **extra):
        self.render()
        self.prepare(1, "--only", "head")
        self.verdicts(1, head=pv("head", 4.0, blockers=[first]))
        self.render(hashes={"head": "head-v2"})
        self.prepare(2, "--only", "head")
        self.verdicts(2, head=pv("head", 4.5, previous=4.0, blockers=[second], **extra))
        return self.decide()

    def test_a_size_sent_back_the_way_it_came_is_flagged_with_both_figures(self):
        decision = self.rounds_of(directive("head", 1, "side, jaw plate", "jaw plate depth 0.06 H, currently 0.10 H"),
                                  directive("head", 1, "side and front, jaw plate", "jaw plate depth 0.09 H, currently 0.06 H", 2))
        self.assertEqual(decision["decision"], "continue")  # a flag, not a rule break
        self.assertEqual(decision["reversals"], [{"part": "head", "id": "r2-head-b1", "where": "side and front, jaw plate", "against": "r1-head-b1",
                                                  "in": "round-1/verdicts/head.json",
                                                  "why": "it asked for 0.06 H from 0.1 H; this asks for 0.09 H from 0.06 H"}])

    def test_an_overshoot_towards_the_same_target_is_not_a_reversal(self):
        decision = self.rounds_of(directive("head", 1, "side, jaw plate", "jaw plate depth 0.06 H, currently 0.10 H"),
                                  directive("head", 1, "side, jaw plate", "jaw plate depth 0.06 H, currently 0.04 H", 2))
        self.assertNotIn("reversals", decision)

    def test_the_same_direction_again_or_another_place_is_not_a_reversal(self):
        decision = self.rounds_of(directive("head", 1, "side, jaw plate", "jaw plate depth 0.06 H, currently 0.10 H"),
                                  directive("head", 1, "side, brow ridge", "brow ridge depth 0.09 H, currently 0.06 H", 2))
        self.assertNotIn("reversals", decision)

    def test_opposite_wording_is_flagged_as_a_guess(self):
        decision = self.rounds_of(directive("head", 1, "front, brow ridge", "make the brow ridge sharper, a hard crease"),
                                  directive("head", 1, "front, brow ridge", "soften the brow ridge into the forehead", 2))
        self.assertEqual(decision["reversals"][0]["why"], 'by its wording ("sharper" then "soften"); read both before acting')

    def test_a_critic_can_say_it_reverses_a_directive(self):
        decision = self.rounds_of(directive("head", 1, "front, visor", "tint the visor rim green"),
                                  directive("head", 1, "whole visor", "the rim is grey in the target; remove the tint", 2, reverses="r1-head-b1"))
        self.assertEqual((decision["reversals"][0]["against"], decision["reversals"][0]["why"]), ("r1-head-b1", "the critic says it reverses that directive"))

    def test_opposed_reads_sizes_before_words(self):
        old = {"id": "a", "where": "side, shin", "change": "make the shin thicker: 0.05 H, currently 0.04 H"}
        self.assertIsNone(verdict.opposed(old, {"id": "b", "where": "side, shin", "change": "shin 0.05 H, currently 0.06 H; it is thinner in the target"}))
        self.assertIsNone(verdict.opposed(old, {"id": "b", "where": "front, toes", "change": "thinner toes"}))
        self.assertIn("wording", verdict.opposed(old, {"id": "b", "where": "shin, side view", "change": "the shin should be thinner"}))


if __name__ == "__main__":
    unittest.main()
