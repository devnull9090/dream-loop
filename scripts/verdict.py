#!/usr/bin/env python3
"""Check the latest Dream Loop verdict and say what to do next.

Usage: python verdict.py .dream-loop/verdicts [--pass-score 8] [--max-rounds 10]

Reads round-N.json (and round-N-confirm.json) in that folder, checks the latest
against the critic's output rules, and prints one JSON line with a decision:

  continue   act on the verdict named in "act_on" and run another round
  revert     this round made the capture at least a point worse; undo it
  rethink    two rounds without real progress; make one large structural change
  confirm    the capture passes; get a second fresh critic to check the same review
  done       two independent critics passed the capture
  ask_user   stalled after a rethink, or out of rounds; stop and ask

Exit status 2 means the verdict is malformed; the problems are listed on stderr.
No dependencies.
"""
import argparse
import json
import re
import sys
from pathlib import Path

GATES = ["layout", "light_colour", "form_materials", "fine_detail", None]
RANGES = {"layout": (0, 3), "light_colour": (3, 5), "form_materials": (5, 7), "fine_detail": (7, 9), None: (9, 10)}
STEP = 0.5  # smallest gain that counts as progress; critics differ by a few tenths on one image
ITEM_FIELDS = ("id", "where", "target", "current", "change")


def load(path):
    """Read a verdict, tolerating a Markdown code fence or stray text around the JSON object."""
    text = Path(path).read_text(encoding="utf-8")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


def problems(verdict, round_number):
    """Everything in a verdict that breaks the critic's output rules."""
    if not isinstance(verdict, dict):
        return ["verdict must be a JSON object"]
    found = []
    score, gate = verdict.get("score"), verdict.get("first_failed_gate")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 10:
        found.append("score must be a number from 0 to 10")
    if gate not in GATES:
        found.append(f"first_failed_gate must be one of {GATES[:-1]} or null")
    elif not found and not RANGES[gate][0] <= score <= RANGES[gate][1]:
        low, high = RANGES[gate]
        found.append(f"score {score} is outside {low}-{high}, the range when the first failed gate is {gate}")
    for key, limit in (("blockers", 5), ("next", 3)):
        items = verdict.get(key)
        if not isinstance(items, list):
            found.append(f"{key} must be a list")
            continue
        if len(items) > limit:
            found.append(f"{key} has {len(items)} items; the limit is {limit}")
        for item in items:
            missing = [f for f in ITEM_FIELDS if not (isinstance(item, dict) and isinstance(item.get(f), str) and item[f].strip())]
            if missing:
                found.append(f"a {key} item is missing {', '.join(missing)}")
    if gate in GATES and isinstance(verdict.get("blockers"), list) and bool(verdict["blockers"]) != (gate is not None):
        found.append("blockers must be listed when a gate fails and empty when none does")
    if round_number > 1:
        previous = verdict.get("previous_score")
        if isinstance(previous, bool) or not isinstance(previous, (int, float)):
            found.append("previous_score is required from round 2 on")
        if not isinstance(verdict.get("regressions"), list):
            found.append("regressions is required from round 2 on (an empty list if there are none)")
    return found


def passes(verdict, pass_score):
    return verdict["score"] >= pass_score and not verdict.get("regressions")


def decide(folder, pass_score=8, max_rounds=10):
    folder = Path(folder)
    rounds = sorted(int(m.group(1)) for m in (re.fullmatch(r"round-(\d+)\.json", p.name) for p in folder.iterdir()) if m)
    if not rounds:
        raise ValueError(f"no round-N.json in {folder}")
    latest = rounds[-1]
    verdicts = {}
    for n in rounds:
        try:
            verdicts[n] = load(folder / f"round-{n}.json")
        except (ValueError, OSError) as error:
            if n == latest:
                raise ValueError(f"round-{n}.json: {error}")
            continue
        found = problems(verdicts[n], n)
        if found and n == latest:
            raise ValueError(f"round-{n}.json: " + "; ".join(found))
        if found:
            del verdicts[n]  # an old malformed verdict tells us nothing; skip it

    # Replay the rounds so the decision depends only on the files, not on anyone's memory of the loop.
    flat = 0  # consecutive rounds without real progress
    rethought = False  # a rethink was called and its result has not been judged yet
    decision = gain = None
    for n in sorted(verdicts):
        v = verdicts[n]
        gain = round(v["score"] - v["previous_score"], 2) if "previous_score" in v and n > 1 else None
        # Gates passed before and after, both as this critic saw them; another critic's gate is not comparable.
        gate = GATES.index(v["first_failed_gate"])
        gate_before = None if gain is None else next(
            i for i, name in enumerate(GATES) if v["previous_score"] < RANGES[name][1] or name is None)
        progressed = gain is None or gain >= STEP or gate > gate_before
        if progressed:
            flat, was_rethought, rethought = 0, False, False
        else:
            flat, was_rethought, rethought = flat + 1, rethought, False
        if passes(v, pass_score):
            decision, why = "confirm", f"score {v['score']} with no regressions"
        elif not progressed and was_rethought:
            decision, why = "ask_user", "the large change after the last rethink did not help"
        elif gain is not None and gain <= -1:
            decision, why = "revert", f"the critic scored this capture {-gain} below the previous one"
        elif flat >= 2:
            decision, why, rethought = "rethink", "two rounds in a row without real progress", True
        else:
            decision, why = "continue", "gates still failing" if v["first_failed_gate"] else "score below the pass mark"
            if v.get("regressions"):
                why = f"{len(v['regressions'])} regression(s) to fix first"

    v, act_on = verdicts[latest], f"round-{latest}.json"
    confirm_path = folder / f"round-{latest}-confirm.json"
    if decision == "confirm" and confirm_path.exists():
        second = load(confirm_path)
        found = problems(second, latest)
        if found:
            raise ValueError(f"{confirm_path.name}: " + "; ".join(found))
        if passes(second, pass_score):
            decision, why = "done", f"two critics passed it ({v['score']} and {second['score']})"
        else:
            decision, why, act_on = "continue", f"the second critic scored it {second['score']}", confirm_path.name
    if decision in ("continue", "revert", "rethink") and max_rounds and latest >= max_rounds:
        decision, why = "ask_user", f"{latest} rounds used; last decision was {decision}: {why}"
    return {"round": latest, "decision": decision, "why": why, "act_on": act_on, "score": v["score"], "gain": gain,
            "first_failed_gate": v["first_failed_gate"], "regressions": len(v.get("regressions") or [])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", help="Folder holding round-N.json verdicts")
    parser.add_argument("--pass-score", type=float, default=8, help="Score a capture must reach (default: %(default)s)")
    parser.add_argument("--max-rounds", type=int, default=10, help="Stop and ask after this many rounds; 0 for no limit (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(decide(args.folder, args.pass_score, args.max_rounds), separators=(",", ":")))
    except (ValueError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
