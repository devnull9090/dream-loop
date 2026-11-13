#!/usr/bin/env python3
"""Check a Dream Loop 3D blueprint and derive its measures.

A blueprint is the approved target turned into numbers once, before any modelling: the
views on the target, the parts of the subject with one box per view, joints, sampled
colours and form notes. Every later check and critic works from this sheet.

  python blueprint.py validate .dream-loop/blueprint.json [--write]
  python blueprint.py overlay  .dream-loop/blueprint.json --out .dream-loop/blueprint.png
  python blueprint.py blockout .dream-loop/blueprint.json --out .dream-loop/blockout.json [--no-refs]

validate  lists everything wrong with the sheet (exit status 2) or prints one JSON line.
          --write fills in target_size, colours sampled at their "at" points, and the
          derived "measures" block (sizes and positions as ratios of subject height H).
overlay   draws every box and joint on the target so the boxes can be checked by eye.
blockout  turns the measures into one primitive per part (centre and size in H) to
          start, or restart, the model from. An extent the target shows from no direction is
          taken from a generated view the user approved for that purpose (refgen.py), when
          there is one; --no-refs leaves it assumed.

All boxes are x, y, w, h as fractions of the target image, the same as compare.py.
validate needs no dependencies when target_size is already recorded; the rest need Pillow.
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

VERSION = 1
TOLERANCES = {
    "size": 0.12,  # relative error allowed on a part's height or width
    "size_abs": 0.02,  # ... or this much of H, whichever is larger, so small parts are not held to a pixel
    "position": 0.06,  # error allowed on a part's centre, in H
    "view_iou": 0.7,  # silhouette overlap the product must reach against a target view
    "capture_iou": 0.85,  # overlap a product capture must reach against the mask render from the same camera
    "min_visible": 0.35,  # a part showing less than this share of itself in a view is not measured there
    "camera_yaw": 20.0,  # how far a camera solve may move from the blueprint estimate, degrees
    "camera_pitch": 10.0,
}
HEX = re.compile(r"#[0-9a-fA-F]{6}")


class BlueprintError(ValueError):
    """Carries every problem found, one per line."""


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_box(box):
    return (isinstance(box, list) and len(box) == 4 and all(is_number(v) for v in box)
            and box[2] > 0 and box[3] > 0 and box[0] >= 0 and box[1] >= 0 and box[0] + box[2] <= 1.0001 and box[1] + box[3] <= 1.0001)


def is_point(point):
    return isinstance(point, list) and len(point) == 2 and all(is_number(v) and 0 <= v <= 1 for v in point)


def inside(box, outer, slack=0.01):
    return (box[0] >= outer[0] - slack and box[1] >= outer[1] - slack
            and box[0] + box[2] <= outer[0] + outer[2] + slack and box[1] + box[3] <= outer[1] + outer[3] + slack)


def tolerances(blueprint):
    return {**TOLERANCES, **(blueprint.get("tolerances") or {})}


def resolve(blueprint, part):
    """A part with the form notes and colours of the part named in its same_as, when it leaves them out."""
    source = next((p for p in blueprint["parts"] if p.get("id") == part.get("same_as")), {})
    return {**part, "form": part.get("form") or source.get("form"), "colours": part.get("colours") or source.get("colours")}


def measure_box(box, subject_box, size):
    """A box as ratios of subject height H: height, width, and its centre measured from the
    subject's vertical centre line (cx, positive to the right of the image) and from its base (cy, up)."""
    width, height = size
    h_px = subject_box[3] * height
    return {
        "height": round(box[3] * height / h_px, 4),
        "width": round(box[2] * width / h_px, 4),
        "cx": round(((box[0] + box[2] / 2) - (subject_box[0] + subject_box[2] / 2)) * width / h_px, 4),
        "cy": round(((subject_box[1] + subject_box[3]) - (box[1] + box[3] / 2)) * height / h_px, 4),
    }


def derive(blueprint):
    """The measures block: per view, every part and joint in units of that view's subject height."""
    size = blueprint["target_size"]
    reference = next(p for p in blueprint["parts"] if p["id"] == blueprint["reference_part"])
    out = {}
    for view in blueprint["views"]:
        subject = view["subject_box"]
        parts = {p["id"]: measure_box(p["boxes"][view["id"]], subject, size)
                 for p in blueprint["parts"] if view["id"] in p.get("boxes", {})}
        if view["id"] in reference.get("boxes", {}):
            ref = parts[reference["id"]]
            for m in parts.values():  # the same sizes against the reference part, the way artists state them
                m["height_ref"] = round(m["height"] / ref["height"], 3)
                m["width_ref"] = round(m["width"] / ref["height"], 3)
        joints = {}
        for joint in blueprint.get("joints") or []:
            if view["id"] in joint.get("points", {}):
                x, y = joint["points"][view["id"]]
                m = measure_box([x, y, 1e-9, 1e-9], subject, size)
                joints[joint["id"]] = {"x": m["cx"], "y": m["cy"]}
        out[view["id"]] = {"aspect": round(subject[2] * size[0] / (subject[3] * size[1]), 4), "parts": parts, "joints": joints}
    return out


def problems(blueprint):
    """Everything wrong with a blueprint, and warnings, as two lists of strings."""
    errors, warnings = [], []
    if not isinstance(blueprint, dict):
        return ["blueprint must be a JSON object"], []
    if blueprint.get("version") != VERSION:
        errors.append(f"version must be {VERSION}")
    for key in ("subject", "target", "reference_view", "reference_part"):
        if not (isinstance(blueprint.get(key), str) and blueprint[key].strip()):
            errors.append(f"{key} must be a non-empty string")
    size = blueprint.get("target_size")
    if not (isinstance(size, list) and len(size) == 2 and all(isinstance(v, int) and v > 0 for v in size)):
        errors.append("target_size must be [width, height] in pixels (validate --write records it from the target)")
    unknown = set(blueprint.get("tolerances") or {}) - set(TOLERANCES)
    if unknown or not all(is_number(v) and v >= 0 for v in (blueprint.get("tolerances") or {}).values()):
        errors.append(f"tolerances may only set {sorted(TOLERANCES)} to non-negative numbers")

    views = blueprint.get("views")
    if not (isinstance(views, list) and views and all(isinstance(v, dict) for v in views)):
        return errors + ["views must be a non-empty list of objects"], warnings
    view_ids = [v.get("id") for v in views]
    if len(set(view_ids)) != len(view_ids) or not all(isinstance(i, str) and re.fullmatch(r"[a-z0-9_]+", i) for i in view_ids):
        errors.append("every view needs a unique id made of a-z, 0-9 and _")
    for view in views:
        name = f"view {view.get('id')}"
        for key in ("frame", "subject_box"):
            if not is_box(view.get(key)):
                errors.append(f"{name}: {key} must be x, y, w, h as fractions of the target")
        if is_box(view.get("frame")) and is_box(view.get("subject_box")) and not inside(view["subject_box"], view["frame"]):
            errors.append(f"{name}: subject_box must lie inside frame")
        if not (isinstance(view.get("pose"), str) and view["pose"].strip()):
            errors.append(f"{name}: pose must name the pose shown (views of one pose share the name)")
        camera = view.get("camera")
        if not (isinstance(camera, dict) and all(is_number(camera.get(k)) for k in ("yaw", "pitch", "lens"))
                and -180 <= camera["yaw"] <= 180 and -89 <= camera["pitch"] <= 89 and 8 <= camera["lens"] <= 600):
            errors.append(f"{name}: camera needs yaw (-180 to 180), pitch (-89 to 89) and lens (mm), as estimated from the target")
        scene = view.get("scene", {})
        if not (isinstance(scene, dict) and set(scene) <= {"frame", "collection"}
                and isinstance(scene.get("frame", 0), int) and isinstance(scene.get("collection", ""), str)):
            errors.append(f"{name}: scene may set frame (a timeline frame) and collection (the collection holding this pose)")
        background = view.get("background", "auto")
        if not (background in ("auto", "none") or (isinstance(background, str) and HEX.fullmatch(background))):
            errors.append(f"{name}: background must be \"auto\", \"none\" or a #rrggbb colour")
    if blueprint.get("reference_view") not in view_ids:
        errors.append("reference_view must be the id of a view")

    parts = blueprint.get("parts")
    if not (isinstance(parts, list) and parts and all(isinstance(p, dict) for p in parts)):
        return errors + ["parts must be a non-empty list of objects"], warnings
    part_ids = [p.get("id") for p in parts]
    if len(set(part_ids)) != len(part_ids) or not all(isinstance(i, str) and re.fullmatch(r"[a-z0-9_]+", i) for i in part_ids):
        errors.append("every part needs a unique id made of a-z, 0-9 and _")
    seen_patterns = {}
    boxes_in = {i: 0 for i in view_ids}
    for part in parts:
        name = f"part {part.get('id')}"
        patterns = part.get("objects")
        if not (isinstance(patterns, list) and patterns and all(isinstance(p, str) and p for p in patterns)):
            errors.append(f"{name}: objects must list the object names (or * patterns) that make up this part in the scene")
        else:
            for pattern in patterns:
                if pattern in seen_patterns:
                    errors.append(f"{name}: object pattern {pattern!r} is also claimed by part {seen_patterns[pattern]}; "
                                  "left and right instances need their own id, objects and boxes")
                seen_patterns[pattern] = part.get("id")
        same_as = part.get("same_as")
        if same_as is not None and (same_as not in part_ids or same_as == part.get("id")
                                    or next(p for p in parts if p.get("id") == same_as).get("same_as")):
            errors.append(f"{name}: same_as must name another part that carries its own form and colours")
            same_as = None
        if not same_as or "form" in part:
            if not (isinstance(part.get("form"), str) and len(part["form"].strip()) >= 12):
                errors.append(f"{name}: form must describe the part's shape in a sentence (or set same_as)")
        if not same_as or "colours" in part:
            colours = part.get("colours")
            if not (isinstance(colours, list) and colours):
                errors.append(f"{name}: colours must list at least one sampled colour (or set same_as)")
            for colour in colours if isinstance(colours, list) else []:
                at = colour.get("at") if isinstance(colour, dict) else None
                at_ok = isinstance(at, dict) and at.get("view") in view_ids and is_point(at.get("point"))
                hex_ok = isinstance(colour, dict) and isinstance(colour.get("hex"), str) and HEX.fullmatch(colour["hex"])
                if not (isinstance(colour, dict) and isinstance(colour.get("name"), str) and colour["name"].strip()):
                    errors.append(f"{name}: every colour needs a name")
                elif at is not None and not at_ok:
                    errors.append(f"{name}: colour {colour['name']}: at must be {{\"view\": id, \"point\": [x, y]}}")
                elif not hex_ok and not at_ok:
                    errors.append(f"{name}: colour {colour['name']} needs a #rrggbb hex, or an at point for validate --write to sample")
                elif not hex_ok:
                    warnings.append(f"{name}: colour {colour['name']} has not been sampled yet; run validate --write")
        boxes = part.get("boxes")
        if not (isinstance(boxes, dict) and boxes):
            errors.append(f"{name}: boxes must give the part's box in at least one view")
            continue
        for view_id, box in boxes.items():
            view = next((v for v in views if v.get("id") == view_id), None)
            if view is None:
                errors.append(f"{name}: boxes names unknown view {view_id}")
            elif not is_box(box):
                errors.append(f"{name}: box in view {view_id} must be x, y, w, h as fractions of the target")
            elif is_box(view.get("subject_box")) and not inside(box, view["subject_box"]):
                errors.append(f"{name}: box in view {view_id} reaches outside that view's subject_box")
            else:
                boxes_in[view_id] += 1
    for view_id, count in boxes_in.items():
        if count < 2:
            errors.append(f"view {view_id}: at least two parts need a box in it, or nothing there can be measured")
    reference = next((p for p in parts if p.get("id") == blueprint.get("reference_part")), None)
    if reference is None:
        errors.append("reference_part must be the id of a part")
    elif blueprint.get("reference_view") not in (reference.get("boxes") or {}):
        errors.append("reference_part needs a box in reference_view")

    for joint in blueprint.get("joints") or []:
        name = f"joint {joint.get('id') if isinstance(joint, dict) else joint}"
        if not (isinstance(joint, dict) and isinstance(joint.get("id"), str) and joint["id"]):
            errors.append(f"{name}: needs an id")
            continue
        if not (isinstance(joint.get("parts"), list) and len(joint["parts"]) == 2 and all(p in part_ids for p in joint["parts"])):
            errors.append(f"{name}: parts must name the two parts it connects")
        points = joint.get("points")
        if not (isinstance(points, dict) and points and all(v in view_ids and is_point(p) for v, p in points.items())):
            errors.append(f"{name}: points must give x, y in at least one view")

    if not errors and "measures" in blueprint and blueprint["measures"] != derive(blueprint):
        errors.append("measures no longer match the boxes; run validate --write (never edit measures by hand)")
    if not errors and "measures" not in blueprint:
        warnings.append("measures have not been derived yet; run validate --write")
    return errors, warnings


def load(path, check=True):
    """Read a blueprint; with check, raise BlueprintError unless it is valid and complete."""
    path = Path(path)
    try:
        blueprint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BlueprintError(f"{path}: {error}")
    if check:
        errors, warnings = problems(blueprint)
        errors += [w for w in warnings if "validate --write" in w]
        if errors:
            raise BlueprintError("\n".join(f"{path.name}: {e}" for e in errors))
    return blueprint


def pixels(image):
    return list(image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata())


def target_path(blueprint, blueprint_path):
    return Path(blueprint_path).parent / blueprint["target"]


def write_derived(blueprint, path):
    """Record target_size, sample colours at their points and rebuild measures. Returns problems()."""
    from PIL import Image
    try:
        image = Image.open(target_path(blueprint, path)).convert("RGB")
    except OSError as error:
        return [f"target: {error}"], []
    blueprint["target_size"] = list(image.size)
    for part in blueprint.get("parts") or []:
        for colour in part.get("colours") or [] if isinstance(part, dict) else []:
            at = colour.get("at") if isinstance(colour, dict) else None
            if isinstance(at, dict) and is_point(at.get("point")):
                x, y = (round(f * (edge - 1)) for f, edge in zip(at["point"], image.size))
                patch = image.crop((max(0, x - 2), max(0, y - 2), min(image.width, x + 3), min(image.height, y + 3)))
                channels = [sorted(pixels(band))[len(pixels(band)) // 2] for band in patch.split()]  # median resists outlines
                colour["hex"] = "#%02x%02x%02x" % tuple(channels)
    blueprint.pop("measures", None)
    errors, warnings = problems(blueprint)
    if not errors:
        blueprint["measures"] = derive(blueprint)
        warnings = [w for w in warnings if "measures" not in w]
        Path(path).write_text(json.dumps(blueprint, indent=1) + "\n", encoding="utf-8")
    return errors, warnings


def overlay(blueprint, path, out, max_edge=1568):
    from PIL import Image, ImageDraw
    image = Image.open(target_path(blueprint, path)).convert("RGB")
    scale = min(1, max_edge / max(image.size))
    image = image.resize((round(image.width * scale), round(image.height * scale)))
    draw = ImageDraw.Draw(image)

    def rect(box, colour, width, label=None):
        x0, y0 = box[0] * image.width, box[1] * image.height
        draw.rectangle((x0, y0, (box[0] + box[2]) * image.width, (box[1] + box[3]) * image.height), outline=colour, width=width)
        if label:
            draw.text((x0 + 3, y0 + 2), label, fill=colour)

    palette = [(255, 80, 80), (80, 220, 120), (90, 160, 255), (255, 200, 60), (230, 110, 255), (60, 230, 230), (255, 140, 60)]
    for view in blueprint["views"]:
        rect(view["frame"], (140, 140, 140), 1, f"{view['id']} frame")
        rect(view["subject_box"], (255, 255, 255), 2)
    for n, part in enumerate(blueprint["parts"]):
        for box in part["boxes"].values():
            rect(box, palette[n % len(palette)], 2, part["id"])
    for joint in blueprint.get("joints") or []:
        for x, y in joint["points"].values():
            cx, cy = x * image.width, y * image.height
            draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), outline=(255, 255, 255), width=2)
            draw.text((cx + 6, cy - 6), joint["id"], fill=(255, 255, 255))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    image.save(out)


def solve_pair(rows):
    """Least-squares a, b for rows of (ca, cb, value) meaning ca*a + cb*b = value.
    Returns (a, b, determined); with one direction only, the smallest solution and determined False."""
    saa = sum(ca * ca for ca, _, _ in rows)
    sbb = sum(cb * cb for _, cb, _ in rows)
    sab = sum(ca * cb for ca, cb, _ in rows)
    sav = sum(ca * v for ca, _, v in rows)
    sbv = sum(cb * v for _, cb, v in rows)
    det = saa * sbb - sab * sab
    if det > 0.05 * max(saa, sbb, 1e-9) ** 2:
        return (sav * sbb - sbv * sab) / det, (sbv * saa - sav * sab) / det, True
    # One direction only: the smallest a, b that fit, which lie along that direction.
    ca, cb = max(rows, key=lambda r: abs(r[0]) + abs(r[1]))[:2]
    along = sum((r[0] * ca + r[1] * cb) * r[2] for r in rows) / max(sum((r[0] * ca + r[1] * cb) ** 2 for r in rows), 1e-9)
    return ca * along, cb * along, False


HINT_WEIGHT = 1 / 3  # a measure from a generated view counts this much of one from the approved target


def blockout(blueprint, hints=None):
    """One primitive per part, in H, from the views that show the reference view's pose.

    Axes follow Blender with the subject facing -Y: x to the image right of a front view, y away
    from a front camera, z up from the subject's base. Camera yaw 0 is the front view, 90 the
    view from +X. A part seen in only one direction gets its unseen extent assumed equal to the
    seen one; those are listed under "assumed" and are the first thing to check in the model.

    hints are measures from generated views the user approved for this purpose, as
    [{"ref": id, "yaw": degrees, "parts": {part id: measure}}]. They are used only for a part the
    target leaves undetermined, never to adjust one it settles, and the part then lists the
    references under "generated".
    """
    measures = blueprint.get("measures") or derive(blueprint)
    pose = next(v["pose"] for v in blueprint["views"] if v["id"] == blueprint["reference_view"])
    views = [v for v in blueprint["views"] if v["pose"] == pose]
    out = []
    for part in blueprint["parts"]:
        seen = [(v, measures[v["id"]]["parts"][part["id"]]) for v in views if part["id"] in measures[v["id"]]["parts"]]
        rows = [(math.radians(v["camera"]["yaw"]), m, 1) for v, m in seen]
        extra = [(math.radians(h["yaw"]), h["parts"][part["id"]], HINT_WEIGHT) for h in hints or [] if part["id"] in h["parts"]]
        if not rows and not extra:
            continue

        def solve(rows):
            position = solve_pair([(math.cos(a) * w, math.sin(a) * w, m["cx"] * w) for a, m, w in rows])
            size = solve_pair([(abs(math.cos(a)) * w, abs(math.sin(a)) * w, m["width"] * w) for a, m, w in rows])
            return position[0], position[1], size[0], size[1], size[2]

        used = []
        if rows:
            x, y, width, depth, known = solve(rows)
        if extra and not (rows and known):
            x, y, width, depth, known = solve(rows + extra)
            used = sorted({h["ref"] for h in hints if part["id"] in h["parts"]})
        assumed = []
        if not known:
            # Only one direction was seen, so the split between width and depth is unknown: assume a round section.
            across = sum(m["width"] for _, m, _ in rows + extra) / len(rows + extra)
            a = (rows + extra)[0][0]
            width = depth = across / (abs(math.cos(a)) + abs(math.sin(a)))
            assumed = ["depth" if abs(math.cos(a)) >= abs(math.sin(a)) else "width"]
        vertical = [m for _, m, _ in rows or extra]  # height and base come from the target whenever it shows the part
        height = sum(m["height"] for m in vertical) / len(vertical)
        z = sum(m["cy"] for m in vertical) / len(vertical)
        entry = {"id": part["id"], "centre": [round(x, 4), round(y, 4), round(z, 4)],
                 "size": [round(max(width, 0.01), 4), round(max(depth, 0.01), 4), round(height, 4)],
                 "views": [v["id"] for v, _ in seen], "assumed": assumed}
        if used:
            entry["generated"] = used
        out.append(entry)
    return {"unit": "H", "pose": pose, "parts": out}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "overlay", "blockout"):
        command = sub.add_parser(name)
        command.add_argument("blueprint", help="Path to blueprint.json")
        if name == "validate":
            command.add_argument("--write", action="store_true", help="Record target_size, sampled colours and measures in the file")
        else:
            command.add_argument("--out", required=True, help="File to write")
        if name == "blockout":
            command.add_argument("--no-refs", action="store_true", help="Ignore generated views approved for the blockout")
    args = parser.parse_args(argv)
    try:
        blueprint = load(args.blueprint, check=False)
        if args.command == "validate":
            errors, warnings = write_derived(blueprint, args.blueprint) if args.write else problems(blueprint)
            if errors:
                raise BlueprintError("\n".join(errors))
            print(json.dumps({"ok": True, "views": [v["id"] for v in blueprint["views"]],
                              "parts": [p["id"] for p in blueprint["parts"]], "warnings": warnings}, separators=(",", ":")))
            return 0
        blueprint = load(args.blueprint)
        if args.command == "overlay":
            overlay(blueprint, args.blueprint, args.out)
            print(json.dumps({"overlay": Path(args.out).as_posix()}))
        else:
            hints = []
            if not args.no_refs and (Path(args.blueprint).parent / "refs" / "refs.json").exists():
                import refgen  # here, not at the top: refgen imports this module
                hints = refgen.blockout_hints(blueprint, args.blueprint)
            spec = blockout(blueprint, hints)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(spec, indent=1) + "\n", encoding="utf-8")
            result = {"blockout": Path(args.out).as_posix(), "parts": len(spec["parts"]),
                      "assumed": {p["id"]: p["assumed"] for p in spec["parts"] if p["assumed"]}}
            if any(p.get("generated") for p in spec["parts"]):
                result["from_generated"] = {p["id"]: p["generated"] for p in spec["parts"] if p.get("generated")}
            print(json.dumps(result, separators=(",", ":")))
    except BlueprintError as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
