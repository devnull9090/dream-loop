#!/usr/bin/env python3
"""Builder's close-ups: each part's target crop beside the model, large enough to work from.

  python closeup.py --blueprint .dream-loop/blueprint.json --renders .dream-loop/inner [--part head ...] [--edge 512]

The whole-view sheets show a part about a hundred pixels across, which is enough to check a box
and not enough to model from. For every part (or the ones named), in every view that shows it,
this writes close/<part>.<view>.png into the renders folder: the TARGET crop, the MODEL crop cut
with the same padding and scaled so the part's longer side is --edge pixels in both, and, when
the part has an outline in that view, where the two silhouettes differ.

inner.py calls this after every run. With inner.py --close render (or scene), Blender renders
each crop at that size instead of enlarging a crop of the whole-view picture; each sheet says
which it is and how many pixels the part really had. Requires Pillow.
"""
import argparse
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import blueprint as blueprints  # noqa: E402
import compare  # noqa: E402
import form  # noqa: E402
import masks  # noqa: E402

PAD = 0.1
EDGE = 512
MAX_RENDER = 8192  # a close-up never asks Blender for a frame larger than this on its long edge


def shown(blueprint, renders, measured=None, only=None):
    """(part, view, ids data, part masks, the part's box in the render) for every part in every view that shows it."""
    tolerance = blueprints.tolerances(blueprint)
    for view in blueprint["views"]:
        ids = Path(renders) / f"{view['id']}.ids.json"
        if not ids.exists():
            continue
        data, part_masks, _ = masks.read_ids(ids)
        for part in blueprint["parts"]:
            if (only and part["id"] not in only) or view["id"] not in part["boxes"] or part["id"] not in part_masks:
                continue
            state = ((measured or {}).get("views", {}).get(view["id"], {}).get("parts", {}).get(part["id"]) or {}).get("status")
            box = masks.fraction_box(part_masks[part["id"]])
            visible = (data["parts"].get(part["id"]) or {}).get("visible", 1.0)
            if box and state not in ("hidden", "missing") and (state or visible >= tolerance["min_visible"]):
                yield part, view, data, part_masks, box


def requests(blueprint, renders, measured=None, only=None, edge=EDGE):
    """What to ask Blender for: one crop per part and view, with the frame size that gives the part `edge` pixels."""
    items = []
    for part, view, data, _, box in shown(blueprint, renders, measured, only):
        width, height = data["resolution"]
        native = max(box[2] * width, box[3] * height)
        items.append({"view": view["id"], "part": part["id"], "box": [round(v, 5) for v in masks.padded(box, PAD, (width, height))],
                      "long_edge": min(MAX_RENDER, math.ceil(max(width, height) * max(1.0, edge / native)))})
    return items


def scaled(image, box, edge):
    """A padded crop of a part, scaled so the part's longer side is `edge` pixels. Returns (image, the part's own pixels)."""
    native = max(box[2] * image.width, box[3] * image.height)
    crop = masks.crop_box(image, masks.padded(box, PAD, image.size)).convert("RGB")
    scale = edge / native
    return crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))), Image.LANCZOS), round(native)


def sheets(blueprint, blueprint_path, renders, measured=None, only=None, edge=EDGE, rendered=None):
    """Write close/<part>.<view>.png for every part and view shown. Returns {part: [{view, file, ...}]}.

    rendered maps (part, view) to a crop Blender rendered for it (the file and the frame's long edge)."""
    renders = Path(renders)
    target = Image.open(blueprints.target_path(blueprint, blueprint_path)).convert("RGB")
    tolerance = blueprints.tolerances(blueprint)
    folder = renders / "close"
    folder.mkdir(parents=True, exist_ok=True)
    pictures, out = {}, {}
    for part, view, data, part_masks, box in shown(blueprint, renders, measured, only):
        vid = view["id"]
        left, target_px = scaled(target, part["boxes"][vid], edge)
        crop = (rendered or {}).get((part["id"], vid))
        if crop:
            with Image.open(crop["file"]) as image:
                flat = masks.flatten(image)
            outer = masks.padded(box, PAD, data["resolution"])  # what Blender was asked to render
            native = round(max(box[2] / outer[2] * flat.width, box[3] / outer[3] * flat.height))
            grow = edge / native
            right = flat.resize((max(1, round(flat.width * grow)), max(1, round(flat.height * grow))), Image.LANCZOS)
            source = f"rendered at this size ({native} px)"
        else:
            if vid not in pictures:
                name = data.get("picture") or data["ids"]
                with Image.open(renders / name) as image:
                    pictures[vid] = masks.flatten(image)
            right, native = scaled(pictures[vid], box, edge)
            source = f"cut from the {max(data['resolution'])} px render ({native} px)"
        panels = [(f"TARGET {part['id']} {vid} ({target_px} px on the target)", left), (f"MODEL, {source}", right)]
        entry = {"view": vid, "target_px": target_px, "model_px": native, "edge": edge, "source": "render" if crop else "crop"}
        if vid in part.get("outlines", {}):
            result, diff = form.compare_view(blueprint, view, part, part["outlines"][vid], part_masks, tolerance)
            if diff:
                panels.append((f"OUTLINE overlap {result['overlap']} (needs {result['needs']})", diff))
                entry["overlap"] = result["overlap"]
        margin = compare.MARGIN
        sheet = Image.new("RGB", (sum(image.width + margin for _, image in panels), max(image.height for _, image in panels) + margin), (24, 24, 24))
        draw = ImageDraw.Draw(sheet)
        x = margin // 2
        for label, image in panels:
            sheet.paste(image, (x, margin))
            draw.text((x + 2, 2), label, fill=(255, 220, 90), font=compare.font())
            x += image.width + margin
        entry["file"] = (folder / f"{part['id']}.{vid}.png").as_posix()
        sheet.save(entry["file"])
        out.setdefault(part["id"], []).append(entry)
    (folder / "close.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blueprint", required=True)
    parser.add_argument("--renders", required=True, help="Folder of <view>.ids.json mask renders and their pictures")
    parser.add_argument("--part", action="append", help="Only this part; repeatable (default: every part)")
    parser.add_argument("--edge", type=int, default=EDGE, help="Pixels on the part's longer side in each crop (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        blueprint = blueprints.load(args.blueprint)
        unknown = set(args.part or []) - {p["id"] for p in blueprint["parts"]}
        if unknown:
            raise ValueError(f"unknown part {', '.join(sorted(unknown))}")
        found = sheets(blueprint, args.blueprint, args.renders, only=args.part, edge=max(64, args.edge))
    except (blueprints.BlueprintError, OSError, ValueError, KeyError) as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps({"closeups": {name: [entry["file"] for entry in entries] for name, entries in found.items()}}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
