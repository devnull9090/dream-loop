#!/usr/bin/env python3
"""The settled-facts ledger: things already agreed about the subject, which later critics must not reopen.

  python ledger.py add .dream-loop/ledger.json --fact "eye diameter is 0.04 H" --part head [--key head.eye.diameter --value 0.04] [--source user]
  python ledger.py list .dream-loop/ledger.json [--part head]
  python ledger.py withdraw .dream-loop/ledger.json f2 --reason "target redrawn"

Every part critic is handed the facts for its part (and the ones with no part, which apply to
the whole subject) as constraints. A fact with a --key and --value is also enforced by
verdict.py: a critic directive that names that measure with a different target is rejected
unless it cites evidence from the blueprint. Record a fact when a size beyond the blueprint's
boxes is agreed, when the user accepts a difference, or when a critic's dispute is resolved.
Facts are withdrawn with a reason, never deleted, so the history stays readable. No dependencies.
"""
import argparse
import json
import sys
from pathlib import Path


def read(path):
    path = Path(path)
    if not path.exists():
        return {"facts": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not (isinstance(data, dict) and isinstance(data.get("facts"), list)):
        raise ValueError(f"{path}: not a ledger")
    return data


def facts_for(path, part=None):
    """Active facts that bind a part: its own and the subject-wide ones. With part None, only the subject-wide ones."""
    return [f for f in read(path)["facts"] if not f.get("withdrawn") and f.get("part") in (None, part)]


def add(path, fact, part=None, key=None, value=None, source="user"):
    data = read(path)
    if (key is None) != (value is None):
        raise ValueError("--key and --value go together")
    if key and any(f.get("key") == key and not f.get("withdrawn") for f in data["facts"]):
        raise ValueError(f"{key} is already settled; withdraw that fact first, with the reason")
    entry = {"id": f"f{len(data['facts']) + 1}", "part": part, "fact": fact, "source": source}
    if key:
        entry.update(key=key, value=value)
    data["facts"].append(entry)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return entry


def withdraw(path, fact_id, reason):
    data = read(path)
    entry = next((f for f in data["facts"] if f["id"] == fact_id and not f.get("withdrawn")), None)
    if entry is None:
        raise ValueError(f"no active fact {fact_id}")
    entry["withdrawn"] = reason
    Path(path).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return entry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["add", "list", "withdraw"])
    parser.add_argument("ledger", help="Path to ledger.json")
    parser.add_argument("fact_id", nargs="?", help="Fact to withdraw")
    parser.add_argument("--fact", help="The fact, as one sentence with absolute values")
    parser.add_argument("--part", help="Part the fact binds (default: the whole subject)")
    parser.add_argument("--key", help="Measure name critics use for this value, e.g. head.eye.diameter")
    parser.add_argument("--value", type=float, help="The settled value, in H")
    parser.add_argument("--source", default="user", help="Where it was settled: user, blueprint, round N (default: %(default)s)")
    parser.add_argument("--reason", help="Why a fact is withdrawn")
    args = parser.parse_args(argv)
    try:
        if args.command == "add":
            if not (args.fact and args.fact.strip()):
                parser.error("add needs --fact")
            print(json.dumps(add(args.ledger, args.fact.strip(), args.part, args.key, args.value, args.source), separators=(",", ":")))
        elif args.command == "withdraw":
            if not (args.fact_id and args.reason and args.reason.strip()):
                parser.error("withdraw needs the fact id and --reason")
            print(json.dumps(withdraw(args.ledger, args.fact_id, args.reason.strip()), separators=(",", ":")))
        else:
            facts = [f for f in read(args.ledger)["facts"] if not f.get("withdrawn") and (args.part is None or f.get("part") in (None, args.part))]
            print(json.dumps(facts, separators=(",", ":")))
    except (ValueError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
