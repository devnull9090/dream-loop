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

When the folder holds round-N/ folders with a review.json (written by parts.py prepare in the
3D mode), the verdicts are per part and the decision covers all of them:

  layout     the measured layout fails; fix it in the inner loop, no critic is needed
  form       no part could be sent: each fails its outline; fix the shapes in the inner loop
  restart    the measured layout is far out; rebuild from the blueprint's blockout
  continue / revert / rethink / confirm / done / ask_user as above, with the parts each applies to

A part is open until a critic passes it, passing until a second critic confirms it, then
locked. A locked part whose model changed without an --unlock is a regression: revert.

A round prepared with --only is about the parts it names. The decision is made on those, a
verdict is owed only for those, and every other part keeps the state, score and verdict file
it had; "scope" in the output says which parts the round covered.

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
# Part verdicts (3D mode). Layout is measured before a critic is asked, so a part critic starts at form.
PART_GATES = ["form", "colour_material", "surface_detail", None]
PART_RANGES = {"form": (3, 5), "colour_material": (5, 7), "surface_detail": (7, 9), None: (9, 10)}
# A size change stated as a proportion of what is there ("shorten 30%") overshoots: each critic
# estimates the current size differently. Sizes must be absolute, with the current value.
DIMENSION = re.compile(r"\b(shorten|lengthen|enlarge|shrink|widen|narrow(er)?|thicken|taller|shorter|longer|bigger|smaller|larger|wider|"
                       r"thinner|thicker|scale|resize|height|width|length|depth|thickness|diameter|radius|size)\b", re.I)
RELATIVE = re.compile(r"(\d\s*%|\b\d+(\.\d+)?\s*(x|times)\b|\bx\s*\d|\b(half|twice|double|triple|a third|a quarter|two thirds)\b)", re.I)
ABSOLUTE = re.compile(r"\d\s*(H|cm|mm|m)\b")


def load(path):
    """Read a verdict, tolerating a Markdown code fence or stray text around the JSON object."""
    text = Path(path).read_text(encoding="utf-8")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


def problems(verdict, round_number, gates=GATES, ranges=RANGES):
    """Everything in a verdict that breaks the critic's output rules."""
    if not isinstance(verdict, dict):
        return ["verdict must be a JSON object"]
    found = []
    score, gate = verdict.get("score"), verdict.get("first_failed_gate")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 10:
        found.append("score must be a number from 0 to 10")
    if gate not in gates:
        found.append(f"first_failed_gate must be one of {gates[:-1]} or null")
    elif not found and not ranges[gate][0] <= score <= ranges[gate][1]:
        low, high = ranges[gate]
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
    if gate in gates and isinstance(verdict.get("blockers"), list) and bool(verdict["blockers"]) != (gate is not None):
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


class Track:
    """Progress of one thing under review across successive verdicts."""

    def __init__(self, gates=GATES, ranges=RANGES):
        self.gates, self.ranges = gates, ranges
        self.flat = 0  # consecutive verdicts without real progress
        self.rethought = False  # a rethink was called and its result has not been judged yet

    def judge(self, v, first, pass_score):
        """Decision, reason and gain for the next verdict in the sequence."""
        gain = round(v["score"] - v["previous_score"], 2) if "previous_score" in v and not first else None
        # Gates passed before and after, both as this critic saw them; another critic's gate is not comparable.
        gate = self.gates.index(v["first_failed_gate"])
        gate_before = None if gain is None else next(
            i for i, name in enumerate(self.gates) if v["previous_score"] < self.ranges[name][1] or name is None)
        progressed = gain is None or gain >= STEP or gate > gate_before
        if progressed:
            self.flat, was_rethought, self.rethought = 0, False, False
        else:
            self.flat, was_rethought, self.rethought = self.flat + 1, self.rethought, False
        if passes(v, pass_score):
            decision, why = "confirm", f"score {v['score']} with no regressions"
        elif not progressed and was_rethought:
            decision, why = "ask_user", "the large change after the last rethink did not help"
        elif gain is not None and gain <= -1:
            decision, why = "revert", f"the critic scored this capture {-gain} below the previous one"
        elif self.flat >= 2:
            decision, why, self.rethought = "rethink", "two rounds in a row without real progress", True
        else:
            decision, why = "continue", "gates still failing" if v["first_failed_gate"] else "score below the pass mark"
            if v.get("regressions"):
                why = f"{len(v['regressions'])} regression(s) to fix first"
        return decision, why, gain


def round_numbers(folder, pattern):
    return sorted(int(m.group(1)) for m in (re.fullmatch(pattern, p.name) for p in Path(folder).iterdir()) if m)


def decide(folder, pass_score=8, max_rounds=10):
    folder = Path(folder)
    rounds = round_numbers(folder, r"round-(\d+)\.json")
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
    track = Track()
    decision = gain = None
    for n in sorted(verdicts):
        decision, why, gain = track.judge(verdicts[n], n == 1, pass_score)

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


def part_problems(verdict, part, brief):
    """Rule breaks in one part's verdict, including the 3D mode's rules on sizes and settled facts."""
    found = problems(verdict, 2 if brief.get("has_previous") else 1, PART_GATES, PART_RANGES)
    if not isinstance(verdict, dict):
        return found
    if verdict.get("part") != part:
        found.append(f"part must be \"{part}\"")
    settled = {fact["key"]: fact for fact in brief.get("settled", []) if fact.get("key")}
    for key in ("blockers", "next"):
        for item in verdict.get(key) if isinstance(verdict.get(key), list) else []:
            if not isinstance(item, dict):
                continue
            name = item.get("id", key)
            change = str(item.get("change", ""))
            if DIMENSION.search(change) and RELATIVE.search(change) and not ABSOLUTE.search(change):
                found.append(f"{name}: a size is given as a relative change; state the target and the current value "
                             "in H (\"0.31 H, currently 0.45 H\")")
            fact = settled.get(item.get("measure"))
            if fact is not None:
                value = item.get("target_value")
                agrees = (isinstance(value, (int, float)) and not isinstance(value, bool)
                          and abs(value - fact["value"]) <= max(0.02, 0.05 * abs(fact["value"])))
                if not agrees and not (isinstance(item.get("evidence"), str) and item["evidence"].strip()):
                    found.append(f"{name}: {fact['key']} is settled at {fact['value']} ({fact.get('source', 'ledger')}); "
                                 "keep it, or say in evidence what on the blueprint shows it is wrong")
    return found


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def replay_parts(folder, pass_score=8, upto=None, strict=True):
    """Walk the 3D rounds in order and return the state of every part after the last one.

    Returns (states, rounds). states maps part id to a dict with state (open, passing, locked), the
    hash and per-view signature it had when it last passed, its Track and its latest outcome.
    With strict, a missing or malformed verdict in the latest round is reported in that round's problems.
    """
    folder = Path(folder)
    numbers = [n for n in round_numbers(folder, r"round-(\d+)") if (folder / f"round-{n}" / "review.json").exists()]
    if upto is not None:
        numbers = [n for n in numbers if n <= upto]
    states, rounds = {}, []
    for n in numbers:
        base = folder / f"round-{n}"
        review = load_json(base / "review.json")
        latest = strict and n == numbers[-1]
        outcome = {"round": n, "layout": review["layout"], "regressions": list(review.get("regressions", [])), "parts": {}, "disputes": [],
                   "held": {part: entry["outline"]["lines"] for part, entry in review["parts"].items() if entry.get("held") == "form"},
                   "scope": review.get("scope")}
        for part, entry in review["parts"].items():
            state = states.setdefault(part, {"state": "open", "track": Track(PART_GATES, PART_RANGES), "reviews": 0})
            if part in review.get("unlocked", {}) or entry.get("changed"):
                state["state"] = "open"
            state.pop("decision", None)
            if not review["layout"]["pass"] or not entry.get("send"):
                continue
            brief = load_json(base / "parts" / part / "brief.json")
            for role in ("critic", "confirm"):
                if role == "critic" and entry["send"] == "confirm":
                    continue  # passed in an earlier round; only the second opinion is owed
                name = f"{part}.json" if role == "critic" else f"{part}-confirm.json"
                path = base / "verdicts" / name
                if role == "confirm" and (state["state"] != "passing" or not path.exists()):
                    if state["state"] == "passing":
                        state.update(decision="confirm")
                    break
                try:
                    verdict = load(path)
                    found = part_problems(verdict, part, brief)
                except OSError:
                    found = ["no verdict yet; ask a critic for this part and save its reply here"]
                except ValueError as error:
                    found = [str(error)]
                if found:
                    if latest:
                        outcome.setdefault("problems", []).append(f"round-{n}/verdicts/{name}: " + "; ".join(found))
                    break
                outcome["disputes"] += [{"part": part, "id": item["id"], "measure": item["measure"], "evidence": item["evidence"]}
                                        for key in ("blockers", "next") for item in verdict[key] if item.get("measure") and item.get("evidence")]
                if role == "critic":
                    decision, why, gain = state["track"].judge(verdict, state["reviews"] == 0, pass_score)
                    state["reviews"] += 1
                    state.update(decision=decision, why=why, gain=gain, score=verdict["score"], gate=verdict["first_failed_gate"],
                                 act_on=f"round-{n}/verdicts/{name}")
                    if decision == "confirm":
                        state.update(state="passing", hash=entry.get("hash"), signature=entry.get("signature"))
                elif passes(verdict, pass_score):
                    state.update(state="locked", decision="locked", why=f"two critics passed it ({state.get('score')} and {verdict['score']})",
                                 hash=entry.get("hash"), signature=entry.get("signature"))
                else:
                    state.update(state="open", decision="continue", why=f"the second critic scored it {verdict['score']}",
                                 score=verdict["score"], gate=verdict["first_failed_gate"], act_on=f"round-{n}/verdicts/{name}")
            outcome["parts"][part] = state.get("decision")
        rounds.append(outcome)
    return states, rounds


def decide_parts(folder, pass_score=8, max_rounds=10):
    states, rounds = replay_parts(folder, pass_score)
    if not rounds:
        raise ValueError(f"no round-N/review.json in {folder}")
    last = rounds[-1]
    if last.get("problems"):
        raise ValueError("\n".join(last["problems"]))
    by = {}
    for part, state in states.items():
        by.setdefault(state.get("decision") or ("locked" if state["state"] == "locked" else "waiting"), []).append(part)
    parts = {part: {k: state[k] for k in ("state", "score", "gate", "gain", "why") if state.get(k) is not None} for part, state in states.items()}
    scope = last.get("scope")
    for part, state in states.items():  # a part outside this round's scope: where its last verdict is, so its directives are not lost
        if scope and part not in scope and state["state"] == "open" and state.get("act_on"):
            parts[part]["last_verdict"] = state["act_on"]
    act_on = sorted({states[p]["act_on"] for key in ("continue", "revert", "rethink", "ask_user") for p in by.get(key, []) if states[p].get("act_on")})
    layout = last["layout"]
    if last["regressions"]:
        decision, why = "revert", "locked part(s) changed: " + ", ".join(last["regressions"]) + "; undo that, or prepare again with --unlock PART=reason"
    elif not layout["pass"]:
        decision = "restart" if layout.get("restart") else "layout"
        why = layout.get("advice") if layout.get("restart") else "measured layout fails: " + "; ".join(layout.get("failing", [])[:4])
    elif by.get("revert"):
        decision, why = "revert", "a critic scored these parts a point or more worse than before: " + ", ".join(by["revert"])
    elif by.get("ask_user"):
        decision, why = "ask_user", "the large change after a rethink did not help: " + ", ".join(by["ask_user"])
    elif by.get("rethink"):
        decision, why = "rethink", "two reviews in a row without real progress: " + ", ".join(by["rethink"])
    elif by.get("confirm"):
        decision, why = "confirm", "passed by one critic, needs a second: " + ", ".join(by["confirm"])
    elif all(state["state"] == "locked" for state in states.values()):
        decision, why = "done", "every part is locked and every view was judged"
    elif last["held"] and not any(last["parts"].values()):
        decision, why = "form", ("no critic was asked: the outline gate fails for " + ", ".join(last["held"])
                                 + "; fix those shapes in the inner loop (inner.py lists what differs)")
    else:
        decision, why = "continue", "parts still open: " + ", ".join(p for p, state in states.items() if state["state"] == "open")
    # A round about one part uses up that part's reviews, not everyone's: count those instead of round numbers.
    used = max((states[p]["reviews"] for p in scope if p in states), default=0) if scope else last["round"]
    if decision in ("continue", "revert", "rethink", "layout", "restart", "form") and max_rounds and used >= max_rounds:
        spent = f"{used} reviews of {', '.join(scope)}" if scope else f"{used} rounds"
        decision, why = "ask_user", f"{spent} used; last decision was {decision}: {why}"
    result = {"round": last["round"], "decision": decision, "why": why, "act_on": act_on, "confirm": by.get("confirm", []),
              "parts": parts, "disputes": last["disputes"], "regressions": len(last["regressions"])}
    if last["held"]:
        result["held"] = last["held"]  # parts kept from the critics by the outline gate, with what differs
    if scope:
        result["scope"] = scope
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", help="Folder holding round-N.json verdicts, or the 3D mode's round-N/ folders")
    parser.add_argument("--pass-score", type=float, default=8, help="Score a capture must reach (default: %(default)s)")
    parser.add_argument("--max-rounds", type=int, default=10, help="Stop and ask after this many rounds; 0 for no limit (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        staged = any((p / "review.json").exists() for p in Path(args.folder).glob("round-*") if p.is_dir())
        print(json.dumps((decide_parts if staged else decide)(args.folder, args.pass_score, args.max_rounds), separators=(",", ":")))
    except (ValueError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
