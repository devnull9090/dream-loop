#!/usr/bin/env python3
"""The 3D mode's inner loop: render, view-match, measure and compare outlines in one command, with no critic.

  python inner.py --blueprint .dream-loop/blueprint.json --blend model.blend            (headless)
  python inner.py --blueprint .dream-loop/blueprint.json --setup build_model.py         (headless, model built by a script)
  python inner.py --blueprint .dream-loop/blueprint.json --socket                       (the open Blender session)

Run it after every change to the model. It takes seconds:
 1. the first time a view is seen, sweeps a window of cameras round the blueprint's estimate,
    keeps the best in cameras.json and freezes it (later runs reuse it);
 2. renders every view's ID mask and a quick picture at those cameras;
 3. scores each view's silhouette against the target (viewmatch.py);
 4. measures every part against the blueprint (measure.py) and writes <view>.side.png,
    target beside product with the part boxes drawn;
 5. compares every part that has an outline in the blueprint with that outline (form.py) and
    writes <part>.form.png, where the two shapes differ.

Prints one JSON line: "stage" says where the model stands (blockout until the silhouettes
match, proportion until every measure passes, form until every outlined part's shape matches
its outline, then outer: ready for a product capture and critics), "failing" lists each measure
as target and current value. Exit status 0 only at outer.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import blender_run  # noqa: E402
import blueprint as blueprints  # noqa: E402
import form  # noqa: E402
import measure  # noqa: E402
import viewmatch  # noqa: E402


def inner(args):
    started = time.time()
    blueprint = blueprints.load(args.blueprint)
    root = Path(args.blueprint).parent
    out = Path(args.out) if args.out else root / "inner"
    cameras_path = Path(args.cameras) if args.cameras else root / "cameras.json"
    cameras = json.loads(cameras_path.read_text(encoding="utf-8")) if cameras_path.exists() else {}
    solved = []
    for view in blueprint["views"]:
        if view["id"] in cameras:
            continue
        sweep = root / "sweep"
        blender_run.run({"command": "sweep", "blueprint": blueprint, "view": view["id"], "out": str(sweep),
                         **blender_run.sweep_window(blueprint, view["id"])}, args)
        solved.append(viewmatch.solve(blueprint, args.blueprint, sweep, view["id"], cameras_path))
    cameras = json.loads(cameras_path.read_text(encoding="utf-8")) if cameras_path.exists() else {}
    rendered = blender_run.run({"command": "render", "blueprint": blueprint, "out": str(out), "cameras": cameras,
                                "long_edge": args.long_edge, "beauty": args.beauty}, args)
    matched = viewmatch.check(blueprint, args.blueprint, out, cameras=cameras)
    measured = measure.measure(blueprint, args.blueprint, out)
    shaped = form.check(blueprint, args.blueprint, out, measured)
    stage = "blockout" if not matched["pass"] else "proportion" if not measured["pass"] else "form" if not shaped["pass"] else "outer"
    result = {
        "stage": stage, "pass": stage == "outer",
        "viewmatch": [{k: v[k] for k in ("view", "method", "score", "threshold", "pass") if k in v} for v in matched["views"]],
        # Outline differences are listed once the layout passes: a shape is not worth reading on a part of the wrong size.
        "failing": [f"view {v['view']}: {v['why']}" for v in matched["views"] if not v["pass"]] + measured["failing"]
                   + (shaped["failing"] if stage == "form" else []),
        "restart": measured["restart"], "parts": {name: part["status"] for name, part in measured["parts"].items()},
        "sheets": measured["sheets"] + shaped["sheets"], "renders": out.as_posix(), "seconds": round(time.time() - started, 1),
    }
    if shaped["parts"]:
        result["form"] = form.summary(shaped)
    if shaped["notes"]:
        result["notes"] = shaped["notes"]
    if measured.get("advice"):
        result["advice"] = measured["advice"]
    if solved:
        result["cameras_solved"] = solved
    missing = sorted({part for view in rendered["views"].values() for part in view["missing"]})
    if missing:
        result["missing_parts"] = missing
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blueprint", required=True)
    parser.add_argument("--blend", help="Scene file to open headless")
    parser.add_argument("--setup", help="Python script to run in Blender first, to build the model")
    parser.add_argument("--socket", nargs="?", const="127.0.0.1:9876", help="Use the open Blender session through the blender-mcp add-on")
    parser.add_argument("--blender", help="Blender executable")
    parser.add_argument("--out", help="Folder for the renders (default: inner/ beside the blueprint)")
    parser.add_argument("--cameras", help="Solved cameras (default: cameras.json beside the blueprint)")
    parser.add_argument("--long-edge", type=int, default=768, help="Render size, longest edge (default: %(default)s)")
    parser.add_argument("--beauty", choices=["preview", "scene", "none"], default="preview",
                        help="Picture beside the mask: preview (fast), scene (the scene's own engine, for an outer round), none")
    args = parser.parse_args(argv)
    try:
        result = inner(args)
    except (blender_run.BlenderError, blueprints.BlueprintError, OSError, ValueError, KeyError, StopIteration) as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
