#!/usr/bin/env python3
"""Measured layout: compare each part's projected size and position with the blueprint, in numbers.

  python measure.py --blueprint .dream-loop/blueprint.json --renders .dream-loop/inner

Reads the ID-mask renders made by blender_run.py render (one per blueprint view, at the matched
camera) and, for every part with a box in that view, measures its visible height, width and
centre as ratios of subject height H. Each is compared with the blueprint's measure:

  leg_l height in side: target 0.42 H, currently 0.55 H

Proportion questions are settled here, by arithmetic, not by a critic's eye. Writes measure.json
and one <view>.side.png (target beside product, boxes drawn, failures in red) into the renders
folder and prints a one-line summary. Exit status 1 when any part is out of tolerance, missing
or unmeasured; 2 when the inputs are unusable. Requires Pillow.
"""
import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import blueprint as blueprints  # noqa: E402
import masks  # noqa: E402

RESTART_FACTOR = 3  # a size this many tolerances out is far out
RESTART_SHARE = 0.5  # ... and this share of the parts far out means the model was not built to the blueprint
LABELS = {"height": "height", "width": "width", "cx": "centre across", "cy": "centre height"}


def compare(name, target, current, tolerance):
    """One measure against its target. Sizes are held to a relative tolerance, positions to an absolute one."""
    allowed = max(tolerance["size"] * target, tolerance["size_abs"]) if name in ("height", "width") else tolerance["position"]
    error = abs(current - target)
    return {"target": round(target, 3), "current": round(current, 3), "allowed": round(allowed, 3), "ok": error <= allowed,
            "times_allowed": round(error / allowed, 2) if allowed else 0.0}


def measure_view(blueprint, view, data, part_masks, subject_mask):
    tolerance = blueprints.tolerances(blueprint)
    subject = masks.fraction_box(subject_mask)
    size = (subject_mask.width, subject_mask.height)
    targets = blueprint["measures"][view["id"]]["parts"]
    out = {"subject_box": [round(v, 4) for v in subject] if subject else None, "parts": {}}
    for part, target in targets.items():
        info = data["parts"].get(part) or {"objects": 0}
        box = masks.fraction_box(part_masks[part]) if part in part_masks else None
        if not info.get("objects"):
            out["parts"][part] = {"status": "missing", "lines": [f"{part}: no object in the scene belongs to this part"]}
        elif box is None or info.get("visible", 1.0) < tolerance["min_visible"]:
            out["parts"][part] = {"status": "hidden", "visible": info.get("visible", 0.0), "lines": []}
        else:
            current = blueprints.measure_box(box, subject, size)
            measures = {name: compare(name, target[name], current[name], tolerance) for name in LABELS}
            lines = [f"{part} {LABELS[name]} in {view['id']}: target {m['target']} H, currently {m['current']} H"
                     for name, m in measures.items() if not m["ok"]]
            out["parts"][part] = {"status": "fail" if lines else "pass", "visible": info.get("visible", 1.0),
                                  "box": [round(v, 4) for v in box], "measures": measures, "lines": lines}
    return out


def side_by_side(blueprint, view, target, result, picture, max_edge=1024):
    """Target view beside the product, each cropped to its subject with the part boxes drawn."""
    def panel(image, subject, boxes):
        pad = 0.06
        outer = [max(0.0, subject[0] - pad * subject[2]), max(0.0, subject[1] - pad * subject[3]),
                 min(1.0, subject[2] * (1 + 2 * pad)), min(1.0, subject[3] * (1 + 2 * pad))]
        outer[2], outer[3] = min(outer[2], 1 - outer[0]), min(outer[3], 1 - outer[1])
        crop = masks.crop_box(image, outer).convert("RGB")
        scale = (max_edge // 2) / max(crop.size)
        crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))))
        draw = ImageDraw.Draw(crop)
        for name, box, ok in boxes:
            x, y, w, h = masks.within(box, outer)
            colour = (60, 220, 110) if ok else (255, 70, 70)
            draw.rectangle((x * crop.width, y * crop.height, (x + w) * crop.width, (y + h) * crop.height), outline=colour, width=2)
            draw.text((x * crop.width + 3, y * crop.height + 2), name, fill=colour)
        return crop

    status = {name: part["status"] != "fail" for name, part in result["parts"].items()}
    left = panel(target, view["subject_box"], [(p["id"], p["boxes"][view["id"]], status.get(p["id"], True))
                                               for p in blueprint["parts"] if view["id"] in p["boxes"]])
    right = panel(picture, result["subject_box"], [(name, part["box"], part["status"] == "pass")
                                                   for name, part in result["parts"].items() if "box" in part])
    height = max(left.height, right.height)
    sheet = Image.new("RGB", (left.width + right.width + 12, height + 18), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    draw.text((2, 2), f"TARGET {view['id']}", fill=(255, 220, 90))
    draw.text((left.width + 14, 2), "CURRENT", fill=(255, 220, 90))
    sheet.paste(left, (0, 18))
    sheet.paste(right, (left.width + 12, 18))
    return sheet


def measure(blueprint, blueprint_path, renders):
    renders = Path(renders)
    target = Image.open(blueprints.target_path(blueprint, blueprint_path)).convert("RGB")
    report = {"pass": True, "restart": False, "views": {}, "parts": {}, "failing": [], "unassigned": [], "sheets": []}
    for view in blueprint["views"]:
        ids = renders / f"{view['id']}.ids.json"
        if not ids.exists():
            report["failing"].append(f"view {view['id']}: no render ({ids.name} is missing); every blueprint view must be measured")
            continue
        data, part_masks, subject = masks.read_ids(ids)
        if masks.fraction_box(subject) is None:
            report["failing"].append(f"view {view['id']}: the render is empty")
            continue
        result = measure_view(blueprint, view, data, part_masks, subject)
        report["views"][view["id"]] = result
        report["unassigned"] += [name for name in data.get("unassigned", []) if name not in report["unassigned"]]
        picture_file = renders / data["picture"] if data.get("picture") else renders / data["ids"]
        with Image.open(picture_file) as picture:
            flat = Image.new("RGB", picture.size, (40, 40, 40))
            flat.paste(picture.convert("RGBA"), mask=picture.convert("RGBA").getchannel("A"))
        sheet = side_by_side(blueprint, view, target, result, flat)
        sheet.save(renders / f"{view['id']}.side.png")
        report["sheets"].append((renders / f"{view['id']}.side.png").as_posix())

    far = 0
    for part in blueprint["parts"]:
        seen = {v: r["parts"][part["id"]] for v, r in report["views"].items() if part["id"] in r["parts"]}
        states = {p["status"] for p in seen.values()}
        lines = [line for p in seen.values() for line in p["lines"]]
        if "missing" in states:
            status, lines = "missing", lines[:1]
        elif "fail" in states:
            status = "fail"
        elif "pass" in states:
            status = "pass"
        else:
            status = "unmeasured"
            if seen:
                lines = [f"{part['id']}: mostly hidden in every view that shows it ({', '.join(seen)}); it cannot be measured"]
        worst = max((m["times_allowed"] for p in seen.values() for name, m in p.get("measures", {}).items() if name in ("height", "width")), default=0)
        far += worst >= RESTART_FACTOR or status == "missing"
        report["parts"][part["id"]] = {"status": status, "views": {v: p["status"] for v, p in seen.items()}, "lines": lines}
        report["failing"] += lines
    if report["unassigned"]:
        report["failing"].append("objects that belong to no part (add them to a part's objects, or hide them from render): "
                                 + ", ".join(report["unassigned"][:12]))
    report["pass"] = not report["failing"]
    report["restart"] = bool(far >= max(2, RESTART_SHARE * len(blueprint["parts"])))
    if report["restart"]:
        report["advice"] = (f"{far} of {len(blueprint['parts'])} parts are more than {RESTART_FACTOR} tolerances from the blueprint. "
                            "Do not repair this model: rebuild it from the blockout (blueprint.py blockout), then add form.")
    (renders / "measure.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blueprint", required=True)
    parser.add_argument("--renders", required=True, help="Folder of <view>.ids.json mask renders")
    args = parser.parse_args(argv)
    try:
        report = measure(blueprints.load(args.blueprint), args.blueprint, args.renders)
    except (blueprints.BlueprintError, OSError, ValueError, KeyError) as error:
        print(error, file=sys.stderr)
        return 2
    summary = {"pass": report["pass"], "restart": report["restart"], "failing": report["failing"],
               "parts": {name: part["status"] for name, part in report["parts"].items()}, "sheets": report["sheets"]}
    if report["restart"]:
        summary["advice"] = report["advice"]
    print(json.dumps(summary, separators=(",", ":")))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
