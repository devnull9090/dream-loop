"""Tests for rounds about one part: parts.py prepare --only and verdict.py. Run: python -m unittest scripts/scope_test.py"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import parts_test  # noqa: E402  (the module, not its test class: a class imported by name would be collected and run again here)
from parts_test import pv  # noqa: E402


class Rounds(unittest.TestCase):
    """The working folder and helpers of parts_test.PartsTest, without its tests."""
    setUp, render, prepare, verdicts, decide, brief = (parts_test.PartsTest.setUp, parts_test.PartsTest.render, parts_test.PartsTest.prepare,
                                                       parts_test.PartsTest.verdicts, parts_test.PartsTest.decide, parts_test.PartsTest.brief)

    def review(self, n):
        return json.loads((self.rounds / f"round-{n}" / "review.json").read_text(encoding="utf-8"))


class ScopeTest(Rounds):
    def test_only_the_named_part_is_sent_and_no_verdict_is_owed_for_the_rest(self):
        self.render()
        status, result, err = self.prepare(1, "--only", "head")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(([s["part"] for s in result["send"]], result["scope"]), (["head"], ["head"]))
        self.assertFalse((self.rounds / "round-1" / "parts" / "body").exists())
        self.assertEqual(self.review(1)["scope"], ["head"])
        self.verdicts(1, head=pv("head", 4.0))
        decision = self.decide()  # the body has no verdict, and that is not a rule break
        self.assertEqual((decision["decision"], decision["scope"], decision["act_on"]), ("continue", ["head"], ["round-1/verdicts/head.json"]))
        self.assertEqual(decision["parts"]["body"], {"state": "open"})

    def test_a_part_is_worked_until_it_locks_while_the_others_keep_their_last_state(self):
        self.render()
        self.prepare(1)
        self.verdicts(1, head=pv("head", 4.0), body=pv("body", 5.5))
        self.assertEqual(self.decide()["decision"], "continue")

        self.render(hashes={"head": "head-v2"}, paint={"head": (190, 50, 50)})
        status, result, _ = self.prepare(2, "--only", "head")
        self.assertEqual((status, [(s["part"], s["role"]) for s in result["send"]]), (0, [("head", "critic")]))
        self.assertTrue(self.brief(2, "head")["has_previous"])
        self.verdicts(2, head=pv("head", 8.5, previous=4.0))
        decision = self.decide()
        self.assertEqual((decision["decision"], decision["confirm"]), ("confirm", ["head"]))
        # The body was not in this round: its score, gate and the verdict to act on are what round 1 left.
        self.assertEqual(decision["parts"]["body"], {"state": "open", "score": 5.5, "gate": "colour_material", "why": "gates still failing",
                                                     "last_verdict": "round-1/verdicts/body.json"})
        self.verdicts(2, head_confirm=pv("head", 8.0, previous=4.0))
        decision = self.decide()
        self.assertEqual((decision["decision"], decision["parts"]["head"]["state"]), ("continue", "locked"))
        self.assertIn("parts still open: body", decision["why"])

        # Round 3, back to the body: it is judged against what its own last critic saw, in round 1.
        self.render(hashes={"head": "head-v2", "body": "body-v2"}, paint={"head": (190, 50, 50), "body": (40, 90, 170)})
        status, result, _ = self.prepare(3, "--only", "body")
        self.assertEqual((status, [s["part"] for s in result["send"]], result["locked"]), (0, ["body"], ["head"]))
        brief = self.brief(3, "body")
        self.assertEqual((brief["has_previous"], [d["id"] for d in brief["previous_directives"]]), (True, ["r1-body-b1"]))

    def test_a_locked_part_outside_the_scope_is_still_watched(self):
        self.render()
        self.prepare(1)
        self.verdicts(1, head=pv("head", 8.5), head_confirm=pv("head", 8.5), body=pv("body", 5.5))
        self.render(hashes={"head": "head-v2", "body": "body-v2"})
        status, result, _ = self.prepare(2, "--only", "body")
        self.assertEqual((status, result["regressions"], result["send"]), (1, ["head"], []))
        self.assertEqual(self.decide()["decision"], "revert")

    def test_a_failed_layout_stops_a_scoped_round_too(self):
        self.render(side={"body": (140, 120, 260, 300)})  # the body, which is not in scope, is too short
        status, result, _ = self.prepare(1, "--only", "head")
        self.assertEqual((status, result["layout"], result["send"]), (1, False, []))
        self.assertEqual(self.decide()["decision"], "layout")

    def test_the_assembly_waits_for_every_part_whatever_the_scope(self):
        self.render()
        self.prepare(1, "--only", "head")
        self.verdicts(1, head=pv("head", 8.5), head_confirm=pv("head", 8.5))
        status, result, _ = self.prepare(2, "--only", "head", "--only", "assembly")
        self.assertEqual((status, result["send"]), (0, []))  # the body is still open, so neither is due
        self.assertIn("head (it is locked), assembly (parts are still open)", result["notes"][-1])
        self.assertEqual(self.review(2)["parts"]["assembly"]["send"], None)
        decision = self.decide()
        self.assertEqual((decision["decision"], decision["parts"]["head"]["state"]), ("continue", "locked"))

    def test_the_round_limit_counts_the_reviews_of_the_parts_in_scope(self):
        self.render()
        for n in range(1, 5):
            self.prepare(n, "--only", "head" if n < 4 else "body")
            name = "head" if n < 4 else "body"
            self.verdicts(n, **{name: pv(name, 4.0 + 0.5 * n, previous=None if n in (1, 4) else 3.5 + 0.5 * n)})
        # Four rounds, but only one review of the body: a limit of three does not stop it.
        self.assertEqual(self.decide(max_rounds=3)["decision"], "continue")
        self.prepare(5, "--only", "head")
        self.verdicts(5, head=pv("head", 6.0, previous=5.5))
        decision = self.decide(max_rounds=3)
        self.assertEqual(decision["decision"], "ask_user")
        self.assertIn("4 reviews of head used", decision["why"])

    def test_unknown_part(self):
        self.render()
        status, _, err = self.prepare(1, "--only", "tail")
        self.assertEqual(status, 2)
        self.assertIn("unknown part tail in --only; choose from head, body, assembly", err)


if __name__ == "__main__":
    unittest.main()
