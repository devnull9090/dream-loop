"""Tests for verdict.py. Run: python -m unittest scripts/verdict_test.py"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import verdict  # noqa: E402

GATE_FOR = [(9, None), (7, "fine_detail"), (5, "form_materials"), (3, "light_colour"), (0, "layout")]


def make(score, previous=None, regressions=(), gate="auto"):
    if gate == "auto":
        gate = next(g for low, g in GATE_FOR if score >= low)
    item = {"id": "r1-b1", "where": "C2", "target": "one fractured slab", "current": "stacked boulders", "change": "replace with one slab"}
    data = {"score": score, "first_failed_gate": gate, "blockers": [item] if gate else [], "next": []}
    if previous is not None:
        data.update(previous_score=previous, regressions=[{"where": "A1", "what": w} for w in regressions],
                    previous_directives=[])
    return data


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, data):
        text = data if isinstance(data, str) else json.dumps(data)
        (self.dir / name).write_text(text, encoding="utf-8")

    def rounds(self, *verdicts):
        for n, data in enumerate(verdicts, 1):
            self.write(f"round-{n}.json", data)
        return verdict.decide(self.dir)

    def test_continue_while_gates_fail_and_regressions_come_first(self):
        result = self.rounds(make(2.5), make(4.0, previous=2.5, regressions=["floor reflection lost"]))
        self.assertEqual((result["decision"], result["gain"], result["regressions"]), ("continue", 1.5, 1))
        self.assertIn("regression", result["why"])

    def test_gain_uses_the_critics_own_rescoring_not_last_rounds_number(self):
        # Round 2's critic is harsher overall (5.5 after a 6.5) but saw a real improvement over the previous capture.
        result = self.rounds(make(6.5), make(5.5, previous=5.0))
        self.assertEqual((result["decision"], result["gain"]), ("continue", 0.5))

    def test_two_flat_rounds_call_one_rethink_then_ask_the_user(self):
        flat = make(5.5, previous=5.5)
        self.assertEqual(self.rounds(make(5.5), flat)["decision"], "continue")
        self.assertEqual(self.rounds(make(5.5), flat, flat)["decision"], "rethink")
        self.assertEqual(self.rounds(make(5.5), flat, flat, flat)["decision"], "ask_user")
        # A rethink that works clears the stall.
        self.assertEqual(self.rounds(make(5.5), flat, flat, make(7.2, previous=5.5))["decision"], "continue")

    def test_passing_a_later_gate_counts_as_progress_even_with_a_small_gain(self):
        result = self.rounds(make(4.8), make(4.9, previous=4.8), make(5.1, previous=4.9))
        self.assertEqual(result["decision"], "continue")

    def test_a_gate_flip_between_two_critics_is_not_progress(self):
        # Critic 2 fails layout where critic 1 passed it; critic 3 agrees with critic 1 again. Nothing improved.
        result = self.rounds(make(4.5), make(2.5, previous=2.5), make(4.5, previous=4.4))
        self.assertEqual(result["decision"], "rethink")

    def test_a_full_point_drop_says_revert(self):
        self.assertEqual(self.rounds(make(6.0), make(5.0, previous=6.2))["decision"], "revert")

    def test_pass_needs_a_second_critic_and_no_regressions(self):
        self.assertEqual(self.rounds(make(6.0), make(8.2, previous=6.0, regressions=["glow lost"]))["decision"], "continue")
        self.assertEqual(self.rounds(make(6.0), make(8.2, previous=6.0))["decision"], "confirm")
        self.write("round-2-confirm.json", make(6.5, previous=6.0))
        result = verdict.decide(self.dir)
        self.assertEqual((result["decision"], result["act_on"]), ("continue", "round-2-confirm.json"))
        self.write("round-2-confirm.json", make(8.0, previous=6.5))
        self.assertEqual(verdict.decide(self.dir)["decision"], "done")

    def test_round_limit_asks_the_user(self):
        self.write("round-1.json", make(2.0))
        for n in range(2, 4):
            self.write(f"round-{n}.json", make(2.0 + n, previous=1.0 + n))
        self.assertEqual(verdict.decide(self.dir, max_rounds=3)["decision"], "ask_user")
        self.assertEqual(verdict.decide(self.dir, max_rounds=0)["decision"], "continue")

    def test_fenced_reply_is_accepted_and_rule_breaks_are_reported(self):
        self.write("round-1.json", "Here is the verdict:\n```json\n" + json.dumps(make(5.5)) + "\n```\n")
        self.assertEqual(verdict.decide(self.dir)["decision"], "continue")
        for broken, message in [
            (make(8.0, gate="form_materials"), "outside 5-7"),
            ({**make(5.5), "blockers": []}, "blockers must be listed"),
            ({**make(5.5), "blockers": [{"id": "r1-b1", "where": "C2"}]}, "missing target, current, change"),
            ({**make(5.5), "next": [make(5.5)["blockers"][0]] * 4}, "limit is 3"),
            ("not json at all", "no JSON object"),
        ]:
            self.write("round-1.json", broken)
            with self.assertRaisesRegex(ValueError, message):
                verdict.decide(self.dir)
        self.write("round-1.json", make(5.5))
        self.write("round-2.json", make(6.0))  # no previous_score or regressions
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(verdict.main([str(self.dir)]), 2)
        self.assertIn("previous_score is required", errors.getvalue())
        self.assertIn("regressions is required", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
