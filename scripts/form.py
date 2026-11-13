#!/usr/bin/env python3
"""Measured form: compare each part's silhouette in the mask render with the outline traced on the target.

  python form.py --blueprint .dream-loop/blueprint.json --renders .dream-loop/inner [--part head ...]

The measured layout says a part is the right size and in the right place; it says nothing about
its shape. A part that has an outline in the blueprint (blueprint.py outline) is compared here,
in every view that has one. Both shapes are scaled to the outline's own bounds first, so the
part's size, which the layout gate has already settled, drops out and only the shape is left:

  head outline in side: overlap 0.78, needs 0.85
  head outline in side: target extends 0.021 H beyond the model at the top left (0.14 across, 0.10 down its box)
  head outline in side: model extends 0.030 H beyond the target at the right (0.95 across, 0.52 down its box)

Only what can be seen on both sides is compared. Where another part of the model covers the
place the target shows this part, and where the model shows this part inside another part's
target outline, nothing is concluded; when that leaves too little, the view is not judged and
the part that hides it is named. A part with no outline is not checked at all.

Writes form.json and one <part>.form.png (white: both, red: target only, green: model only,
grey: not judged) into the renders folder and prints a one-line summary. Exit status 1 when a
part fails, 2 when the inputs are unusable. Requires Pillow.
"""
import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
import blueprint as blueprints  # noqa: E402
import compare  # noqa: E402
import masks  # noqa: E402

WORK = 256  # shapes are compared at this many pixels on the outline's longer side
EDGE = 8  # empty border round the compared shapes
REPORT = 0.35  # a difference this share of the allowed reach is worth a line
CELLS = ["top left", "top", "top right", "left", "middle", "right", "bottom left", "bottom", "bottom right"]


def subtract(a, b):
    return ImageChops.darker(a, ImageChops.invert(b))


def cell_masks(width, height):
    """Nine masks dividing the compared area into thirds; the outer ones take in the border."""
    out = {}
    for index, name in enumerate(CELLS):
        col, row = index % 3, index // 3
        x0 = 0 if col == 0 else EDGE + round(col * width / 3)
        y0 = 0 if row == 0 else EDGE + round(row * height / 3)
        x1 = width + 2 * EDGE if col == 2 else EDGE + round((col + 1) * width / 3)
        y1 = height + 2 * EDGE if row == 2 else EDGE + round((row + 1) * height / 3)
        mask = Image.new("L", (width + 2 * EDGE, height + 2 * EDGE))
        mask.paste(255, (x0, y0, x1, y1))
        out[name] = mask
    return out


def reaches(extra, base, cells):
    """How far, in pixels, the shape `extra` extends beyond `base`, for each ninth of the area.

    base is grown one pixel at a time; a cell's reach is the number of steps it takes to cover
    (nearly all of) what lay beyond it there. A long thin sliver is covered in one step however
    large its area, which is the point: it is a tracing error, not a difference of form.
    Returns {cell: (steps, box of the last pixels to be covered)}."""
    start = {name: masks.area(ImageChops.darker(extra, cell)) for name, cell in cells.items()}
    last = {name: ImageChops.darker(extra, cells[name]).getbbox() for name in cells}
    live = {name for name, count in start.items() if count >= 4}
    out, steps = {}, 0
    while live and steps < WORK:
        steps += 1
        base = base.filter(ImageFilter.MaxFilter(3))
        left = subtract(extra, base)
        for name in sorted(live):
            here = ImageChops.darker(left, cells[name])
            if masks.area(here) <= max(2, 0.01 * start[name]):
                out[name] = (steps, last[name])
                live.discard(name)
            else:
                last[name] = here.getbbox()
    out.update({name: (steps, last[name]) for name in live})
    return out


def compare_view(blueprint, view, part, points, part_masks, tolerance):
    """One part's mask against its outline in one view. Returns (result, picture)."""
    size = blueprint["target_size"]
    frame = blueprints.bounds(points)
    scale = WORK / max(frame[2] * size[0], frame[3] * size[1])
    width, height = max(2, round(frame[2] * size[0] * scale)), max(2, round(frame[3] * size[1] * scale))
    canvas = (width + 2 * EDGE, height + 2 * EDGE)
    subject_px = view["subject_box"][3] * size[1]  # H, in target pixels

    def polygon(outline):
        image = Image.new("L", canvas)
        ImageDraw.Draw(image).polygon([(EDGE + (x - frame[0]) / frame[2] * width, EDGE + (y - frame[1]) / frame[3] * height)
                                       for x, y in outline], fill=255)
        return image

    own = part_masks[part["id"]]
    window = own.getbbox()

    def placed(mask):
        image = Image.new("L", canvas)
        image.paste(mask.crop(window).resize((width, height), Image.NEAREST), (EDGE, EDGE))
        return image

    target, model = polygon(points), placed(own)
    covering = {name: placed(mask) for name, mask in part_masks.items() if name != part["id"] and mask.crop(window).getbbox()}
    covered = Image.new("L", canvas)
    for mask in covering.values():
        covered = ImageChops.lighter(covered, mask)
    claimed = Image.new("L", canvas)  # where the target shows another part: this part may or may not continue under it
    for other in blueprint["parts"]:
        if other["id"] != part["id"] and view["id"] in other.get("outlines", {}):
            claimed = ImageChops.lighter(claimed, polygon(other["outlines"][view["id"]]))

    hidden = ImageChops.darker(target, covered)
    share = masks.area(hidden) / max(1, masks.area(target))
    if share > 1 - tolerance["min_visible"]:
        worst = max(covering, key=lambda name: masks.area(ImageChops.darker(target, covering[name])))
        return {"status": "unjudged", "why": f"{worst} covers {share:.0%} of where the target shows it; too little is left to judge"}, None
    target_only, model_only = subtract(subtract(target, model), covered), subtract(subtract(model, target), claimed)
    unjudged = ImageChops.lighter(hidden, ImageChops.darker(subtract(model, target), claimed))
    both = masks.area(ImageChops.darker(target, model))
    union = both + masks.area(target_only) + masks.area(model_only)
    iou = both / union if union else 0.0

    cells = cell_masks(width, height)
    allowed = tolerance["outline_reach"] * WORK  # in compared pixels
    found = []
    for kind, extra, base in (("target", target_only, ImageChops.lighter(model, covered)), ("model", model_only, ImageChops.lighter(target, claimed))):
        for name, (steps, box) in reaches(extra, base, cells).items():
            if box:
                found.append({"extends": kind, "at": name, "reach_H": round(steps / scale / subject_px, 3), "reach": round(steps / WORK, 3),
                              "across": round(min(1.0, max(0.0, ((box[0] + box[2]) / 2 - EDGE) / width)), 2),
                              "down": round(min(1.0, max(0.0, ((box[1] + box[3]) / 2 - EDGE) / height)), 2), "ok": steps <= allowed})
    found.sort(key=lambda d: -d["reach"])
    ok = bool(iou >= tolerance["outline_iou"] and all(d["ok"] for d in found))
    name = f"{part['id']} outline in {view['id']}"
    lines = []
    if not ok:
        lines.append(f"{name}: overlap {iou:.2f}, needs {tolerance['outline_iou']}")
        worth = [d for d in found if d["reach"] >= REPORT * tolerance["outline_reach"]] or found[:1]
        for d in worth[:4]:
            a, b = ("target", "the model") if d["extends"] == "target" else ("model", "the target")
            lines.append(f"{name}: {a} extends {d['reach_H']} H beyond {b} at the {d['at']} ({d['across']} across, {d['down']} down its box)")
    result = {"status": "pass" if ok else "fail", "overlap": round(iou, 3), "needs": tolerance["outline_iou"],
              "reach_allowed_H": round(allowed / scale / subject_px, 3), "differences": found[:8], "lines": lines}
    if share > 0.02:
        result["covered"] = round(share, 3)
    picture = Image.merge("RGB", (target, model, ImageChops.darker(target, model)))
    picture.paste((110, 110, 110), mask=unjudged)
    return result, picture


def sheet(part, panels):
    """The diff pictures of one part, one per view, side by side."""
    margin = compare.MARGIN
    out = Image.new("RGB", (sum(p.width + margin for _, p in panels), max(p.height for _, p in panels) + 2 * margin), (24, 24, 24))
    draw = ImageDraw.Draw(out)
    x = margin // 2
    for label, picture in panels:
        out.paste(picture, (x, margin))
        draw.text((x + 2, 2), label, fill=(255, 220, 90), font=compare.font())
        x += picture.width + margin
    draw.text((2, out.height - margin + 2), f"{part}: white both, red target only, green model only, grey not judged", fill=(170, 170, 170), font=compare.font())
    return out


def check(blueprint, blueprint_path, renders, measured=None, only=None):
    """Compare every outlined part (or only those named) with its mask in the renders.

    measured is measure.py's report, when there is one: a view where the part is hidden or missing
    there is not judged here either. Returns the report; a part with no outline is not in it."""
    renders = Path(renders)
    tolerance = blueprints.tolerances(blueprint)
    report = {"pass": True, "parts": {}, "failing": [], "notes": [], "sheets": []}
    loaded = {}
    for part in blueprint["parts"]:
        if not part.get("outlines") or (only and part["id"] not in only):
            continue
        views, panels = {}, []
        for view in blueprint["views"]:
            vid = view["id"]
            if vid not in part["outlines"]:
                continue
            if vid not in loaded:
                ids = renders / f"{vid}.ids.json"
                loaded[vid] = masks.read_ids(ids) if ids.exists() else None
            if loaded[vid] is None:
                views[vid] = {"status": "unjudged", "why": f"no render ({vid}.ids.json is missing)"}
                continue
            data, part_masks, _ = loaded[vid]
            info = data["parts"].get(part["id"]) or {}
            state = ((measured or {}).get("views", {}).get(vid, {}).get("parts", {}).get(part["id"]) or {}).get("status")
            if state is None:
                state = ("missing" if not info.get("objects") else
                         "hidden" if info.get("visible", 1.0) < tolerance["min_visible"] or not part_masks[part["id"]].getbbox() else "pass")
            if state == "missing":
                views[vid] = {"status": "unjudged", "why": "no object in the scene belongs to this part"}
            elif state == "hidden":
                views[vid] = {"status": "unjudged", "why": f"only {info.get('visible', 0.0):.0%} of it shows in this view; other parts hide the rest"}
            else:
                views[vid], picture = compare_view(blueprint, view, part, part["outlines"][vid], part_masks, tolerance)
                if picture:
                    panels.append((f"{vid}: overlap {views[vid]['overlap']} (needs {views[vid]['needs']})", picture))
        states = {v["status"] for v in views.values()}
        status = "fail" if "fail" in states else "pass" if "pass" in states else "unjudged"
        lines = [line for v in views.values() for line in v.get("lines", [])]
        notes = [f"{part['id']} outline in {vid}: not judged ({v['why']})" for vid, v in views.items() if v["status"] == "unjudged"]
        report["parts"][part["id"]] = {"status": status, "views": views, "lines": lines}
        report["failing"] += lines
        report["notes"] += notes
        if panels:
            sheet(part["id"], panels).save(renders / f"{part['id']}.form.png")
            report["parts"][part["id"]]["sheet"] = (renders / f"{part['id']}.form.png").as_posix()
            report["sheets"].append(report["parts"][part["id"]]["sheet"])
    report["pass"] = not report["failing"]
    (renders / "form.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def summary(report):
    """What a caller prints: each outlined part's status and its overlap in every judged view."""
    return {name: {"status": part["status"], "overlap": {v: r["overlap"] for v, r in part["views"].items() if "overlap" in r}}
            for name, part in report["parts"].items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blueprint", required=True)
    parser.add_argument("--renders", required=True, help="Folder of <view>.ids.json mask renders")
    parser.add_argument("--part", action="append", help="Check only this part; repeatable")
    args = parser.parse_args(argv)
    try:
        blueprint = blueprints.load(args.blueprint)
        unknown = set(args.part or []) - {p["id"] for p in blueprint["parts"]}
        if unknown:
            raise ValueError(f"unknown part {', '.join(sorted(unknown))}")
        report = check(blueprint, args.blueprint, args.renders, only=args.part)
    except (blueprints.BlueprintError, OSError, ValueError, KeyError) as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps({"pass": report["pass"], "failing": report["failing"], "parts": summary(report), "notes": report["notes"],
                      "sheets": report["sheets"]}, separators=(",", ":")))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
