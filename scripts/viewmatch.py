#!/usr/bin/env python3
"""View-match gate: does the product, seen from this camera, overlay the target view?

Nothing is worth judging until it does. A critic shown a capture from a closer or wider camera
than the target's reports perspective as model faults, and every number measured from that
capture is off by the same error.

  python viewmatch.py --blueprint .dream-loop/blueprint.json --renders .dream-loop/inner
      Scores every view's mask render (from blender_run.py render) against the target. Exit status 1
      when any view is below its threshold; writes viewmatch.json and <view>.match.png there.

  python viewmatch.py --blueprint .dream-loop/blueprint.json --sweep .dream-loop/sweep --view side \
                      --cameras .dream-loop/cameras.json [--resolve "why"]
      Picks the best camera from a sweep (blender_run.py sweep) and records it in cameras.json.
      A view's camera is solved once and then frozen: solving it again needs --resolve with the
      reason, which is kept in the file. The sweep only covers a window round the blueprint's
      estimate, so the camera cannot wander off to flatter a wrong model.

Score: silhouette intersection over union after both are scaled to one height. When the target's
background cannot be keyed, the boxes of the parts are compared instead. Requires Pillow.
"""
import argparse
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import blueprint as blueprints  # noqa: E402
import masks  # noqa: E402

LANDMARK_IOU = 0.55  # mean box overlap required when only part boxes can be compared
ASPECT = 0.15  # the subject's width-to-height may differ from the target's by this much
STAY = 0.03  # score a camera must gain over the estimate, per full window of deviation, to be preferred


def landmark_score(blueprint, view, part_masks, subject_mask):
    """Mean overlap of each part's box, restated inside the subject's box, between target and product."""
    subject = masks.fraction_box(subject_mask)
    scores = []
    for part in blueprint["parts"]:
        target_box = part["boxes"].get(view["id"])
        product_box = masks.fraction_box(part_masks[part["id"]]) if part["id"] in part_masks else None
        if target_box and product_box:
            scores.append(masks.box_iou(masks.within(target_box, view["subject_box"]), masks.within(product_box, subject)))
        elif target_box:
            scores.append(0.0)
    return sum(scores) / len(scores) if scores else 0.0


def score_view(blueprint, view, target, subject_mask, part_masks=None):
    """How well one product silhouette matches one target view."""
    tolerance = blueprints.tolerances(blueprint)
    silhouette, info = masks.target_silhouette(target, view)
    box = masks.fraction_box(subject_mask)
    if box is None:
        return {"view": view["id"], "method": "none", "score": 0.0, "threshold": tolerance["view_iou"], "pass": False,
                "why": "the product render is empty"}, None
    size = blueprint["target_size"]
    target_aspect = view["subject_box"][2] * size[0] / (view["subject_box"][3] * size[1])
    product_aspect = box[2] * subject_mask.width / (box[3] * subject_mask.height)
    result = {"view": view["id"], "aspect": {"target": round(target_aspect, 3), "product": round(product_aspect, 3)}}
    picture = None
    if info["reliable"]:
        iou, _ = masks.overlap(silhouette, subject_mask)
        result.update(method="silhouette", score=round(iou, 3), threshold=tolerance["view_iou"])
        picture = masks.overlay(silhouette, subject_mask)
    elif part_masks is not None:
        result.update(method="landmarks", score=round(landmark_score(blueprint, view, part_masks, subject_mask), 3),
                      threshold=LANDMARK_IOU, key=info["reason"])
    else:  # a sweep candidate has no part data: fall back to how close the outline's shape is
        result.update(method="aspect", score=round(max(0.0, 1 - abs(product_aspect / target_aspect - 1)), 3),
                      threshold=1 - ASPECT, key=info["reason"])
    aspect_ok = abs(product_aspect / target_aspect - 1) <= ASPECT
    result["pass"] = bool(result["score"] >= result["threshold"] and aspect_ok)
    if not result["pass"]:
        result["why"] = (f"{result['method']} score {result['score']} is below {result['threshold']}" if result["score"] < result["threshold"]
                         else f"subject is {product_aspect:.2f} wide per height, the target {target_aspect:.2f}")
    return result, picture


def check(blueprint, blueprint_path, renders, views=None, cameras=None):
    target = Image.open(blueprints.target_path(blueprint, blueprint_path)).convert("RGB")
    renders = Path(renders)
    results = []
    for view in blueprint["views"]:
        if views and view["id"] not in views:
            continue
        ids = renders / f"{view['id']}.ids.json"
        if not ids.exists():
            results.append({"view": view["id"], "method": "none", "score": 0.0, "pass": False, "why": f"no render: {ids.name} is missing"})
            continue
        data, part_masks, subject = masks.read_ids(ids)
        result, picture = score_view(blueprint, view, target, subject, part_masks)
        result["camera"] = {k: data["camera"][k] for k in ("yaw", "pitch", "lens")}
        result["camera_solved"] = bool(cameras and view["id"] in cameras)
        if picture:
            picture.save(renders / f"{view['id']}.match.png")
            result["picture"] = (renders / f"{view['id']}.match.png").as_posix()
        results.append(result)
    report = {"pass": all(r["pass"] for r in results), "views": results}
    (renders / "viewmatch.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def solve(blueprint, blueprint_path, sweep, view_id, cameras_path, resolve=None):
    """Choose the sweep candidate that best matches the target view and record it as that view's camera."""
    cameras_path = Path(cameras_path)
    cameras = json.loads(cameras_path.read_text(encoding="utf-8")) if cameras_path.exists() else {}
    if view_id in cameras and not resolve:
        raise ValueError(f"view {view_id} already has a solved camera; it stays fixed so rounds stay comparable. "
                         "Pass --resolve with the reason if the blueprint or the target really changed.")
    view = next(v for v in blueprint["views"] if v["id"] == view_id)
    listing = json.loads((Path(sweep) / f"{view_id}.candidates.json").read_text(encoding="utf-8"))
    target = Image.open(blueprints.target_path(blueprint, blueprint_path)).convert("RGB")
    tolerance = blueprints.tolerances(blueprint)
    estimate = view["camera"]
    scored = []
    for candidate in listing["candidates"]:
        with Image.open(Path(sweep) / candidate["file"]) as image:
            subject = image.convert("RGBA").getchannel("A").point(lambda v: 255 if v > 127 else 0)
        result, _ = score_view(blueprint, view, target, subject)
        camera = candidate["camera"]
        yaw_off = abs((camera["yaw"] - estimate["yaw"] + 180) % 360 - 180)
        pitch_off = abs(camera["pitch"] - estimate["pitch"])
        deviation = max(yaw_off / max(tolerance["camera_yaw"], 1e-9), pitch_off / max(tolerance["camera_pitch"], 1e-9),
                        abs(camera["lens"] / estimate["lens"] - 1))
        scored.append((result["score"] - STAY * deviation, result, camera, {"yaw": round(yaw_off, 2), "pitch": round(pitch_off, 2)}, deviation))
    _, result, camera, off, deviation = max(scored, key=lambda s: s[0])
    entry = {
        **camera, "score": result["score"], "method": result["method"], "pass": result["pass"],
        "deviation": off, "at_window_edge": bool(deviation >= 0.999 and (tolerance["camera_yaw"] or tolerance["camera_pitch"])),
        "estimate": estimate, "history": cameras.get(view_id, {}).get("history", []),
    }
    if view_id in cameras:
        old = cameras[view_id]
        entry["history"] = entry["history"] + [{"yaw": old["yaw"], "pitch": old["pitch"], "lens": old["lens"], "score": old["score"], "replaced_because": resolve}]
    cameras[view_id] = entry
    cameras_path.parent.mkdir(parents=True, exist_ok=True)
    cameras_path.write_text(json.dumps(cameras, indent=1) + "\n", encoding="utf-8")
    out = {"view": view_id, "camera": {k: camera[k] for k in ("yaw", "pitch", "lens")}, "score": result["score"],
           "method": result["method"], "pass": result["pass"], "deviation": off, "candidates": len(scored)}
    if entry["at_window_edge"]:
        out["warning"] = ("the best camera is at the edge of the allowed window: either the blueprint's camera estimate is wrong "
                          "(correct it there) or the model's shape differs from the target (fix the model, not the camera)")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blueprint", required=True)
    parser.add_argument("--renders", help="Folder of <view>.ids.json mask renders to check")
    parser.add_argument("--views", help="Comma-separated view ids to check (default: all)")
    parser.add_argument("--sweep", help="Folder of sweep candidates to solve a camera from")
    parser.add_argument("--view", help="View id the sweep belongs to")
    parser.add_argument("--cameras", help="cameras.json holding solved cameras")
    parser.add_argument("--resolve", metavar="REASON", help="Replace a view's solved camera, recording why")
    args = parser.parse_args(argv)
    try:
        blueprint = blueprints.load(args.blueprint)
        if args.sweep:
            if not (args.view and args.cameras):
                parser.error("--sweep needs --view and --cameras")
            print(json.dumps(solve(blueprint, args.blueprint, args.sweep, args.view, args.cameras, args.resolve), separators=(",", ":")))
            return 0
        if not args.renders:
            parser.error("give --renders to check, or --sweep to solve a camera")
        cameras = json.loads(Path(args.cameras).read_text(encoding="utf-8")) if args.cameras and Path(args.cameras).exists() else None
        report = check(blueprint, args.blueprint, args.renders, args.views.split(",") if args.views else None, cameras)
        print(json.dumps(report, separators=(",", ":")))
        return 0 if report["pass"] else 1
    except (blueprints.BlueprintError, OSError, ValueError, StopIteration) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
