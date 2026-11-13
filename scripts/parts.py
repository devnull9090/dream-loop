#!/usr/bin/env python3
"""Prepare a per-part review round for Dream Loop's 3D mode.

  python parts.py prepare --blueprint .dream-loop/blueprint.json --round 3 --renders .dream-loop/inner \
         [--capture side=captures/side.png ...] [--capture-mask side=masks/side.png ...] [--unlock head="jaw redesigned"]

It needs a mask render (blender_run.py render, at the solved cameras) and a capture of the real
product for every view in the blueprint; a view left out stops the round. With no --capture at
all, the captures are the Blender pictures from the render, which is right when Blender is the
product. Captures from anywhere else must be given for every view, never for some.

What it does, in order:
 1. Runs the view-match and measured-layout gates on the renders. If either fails the round stops
    there: no critic is asked while the layout is wrong. Then compares every part that has an
    outline in the blueprint with it (form.py); a part whose shape fails is held back from the
    critics, because the difference is already known in numbers.
 2. Checks each capture against the mask render from the same camera, when the capture's
    background can be keyed or a --capture-mask is given.
 3. Works out each part's state from the earlier rounds (open, passing, locked) and whether a
    passing or locked part has changed since (model hash from Blender, then the part's own pixels).
    A locked part that changed is a regression unless it is named in --unlock.
 4. For every part that needs a critic, writes round-N/parts/<part>/pair.png (target, current and
    previous crop for each view, cut from the part's own bounds) and brief.json (form notes,
    colours, measured values, settled facts, previous directives).
 5. Writes round-N/review.json, which verdict.py reads.

Prints one JSON line listing the parts to send and where each verdict goes, and the parts held
back by the outline gate with what differs. Exit status 1 when a gate fails, a locked part changed
or every part that needed a critic is held back; 2 when the inputs are unusable. Requires Pillow.
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import blueprint as blueprints  # noqa: E402
import compare  # noqa: E402
import form  # noqa: E402
import ledger  # noqa: E402
import masks  # noqa: E402
import measure  # noqa: E402
import refgen  # noqa: E402
import verdict  # noqa: E402
import viewmatch  # noqa: E402

ASSEMBLY = "assembly"
DRIFT = 0.06  # mean colour change (0-1) in a part's crop that counts as the part having changed
PAD = 0.15
SMALL = 256  # a part with fewer pixels than this on its longer side in a capture is too small to judge detail from
CELL = 448  # when one sheet of every view would show the crops narrower than this, each view also gets a sheet of its own
SCOPE = ("Layout is already measured and passes: judge form, then colour and material, then surface detail. "
         "The target is a design sheet; lighting and background are not part of the judgement.")
OUTLINE_SCOPE = (" The part's silhouette has been compared with the outline traced on the target and is within tolerance in the views "
                 "listed under outline: judge the forms inside it, and do not ask for the silhouette to change in those views.")
GENERATED_RULE = ("generated.png was made by an image model from the approved target and accepted by the user as a reading aid. It is not the target. "
                  "Where it differs from a TARGET crop in pair.png, the TARGET crop is right. Judge the product against TARGET; use these only to "
                  "understand what TARGET shows too small or from one side. Evidence for a challenge must come from TARGET, never from these.")
ASSEMBLY_FORM = ("The whole subject. Every part has passed on its own; judge only what the parts cannot show alone: how they join, "
                 "whether materials and colours are consistent across parts, and whether the whole reads as the target does.")


def padded(box, image):
    return masks.padded(box, PAD, image.size)


def signature(capture, capture_box, mask, mask_box):
    """A coarse colour fingerprint of a part's own pixels in a capture: 8 x 8 mean colours.

    Everything outside the part's ID mask is greyed out, so repainting a neighbour or the
    background does not read as a change to this part."""
    own = masks.crop_box(mask, mask_box)
    crop = masks.crop_box(capture, capture_box).convert("RGB").resize(own.size)
    flat = Image.new("RGB", own.size, (128, 128, 128))
    flat.paste(crop, mask=own)
    return [v for pixel in compare.pixels(flat.resize((8, 8), Image.BOX)) for v in pixel]


def drift(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / (255 * len(a)) if a and b and len(a) == len(b) else 0.0


def capture_alignment(capture, mask, render_subject, need):
    """Where the subject sits in the capture, and whether the capture agrees with the mask render.

    Returns (subject box in the capture or None, info). With no usable mask the capture is assumed
    to be framed exactly as the render, which holds when it was taken with the recorded camera.
    """
    if mask is None:
        keyed, flat = masks.border_key(capture)
        filled = masks.area(keyed) / (keyed.width * keyed.height)
        if flat < 0.85 or not 0.01 <= filled <= 0.9:
            return None, {"method": "assumed", "note": "the capture's background cannot be keyed; it is assumed to be framed like the mask render"}
        mask = keyed
    mask = mask.convert("L").point(lambda v: 255 if v > 127 else 0)
    render = render_subject.resize(mask.size, Image.NEAREST)
    union = masks.area(ImageChops.lighter(mask, render))
    iou = masks.area(ImageChops.darker(mask, render)) / union if union else 0.0
    info = {"method": "mask", "capture_iou": round(iou, 3), "pass": iou >= need}
    return masks.fraction_box(mask), info


def into(box, source, destination):
    """A box inside source moved to the same place inside destination."""
    x, y, w, h = masks.within(box, source)
    return [destination[0] + x * destination[2], destination[1] + y * destination[3], w * destination[2], h * destination[3]]


def cell_width(rows, max_edge=1568):
    """How wide each crop is drawn when these rows share one sheet."""
    per = max(len(row) for _, row in rows)
    shapes = [max(image.height / image.width for _, image in row) for _, row in rows]
    return int(min((max_edge - per * compare.MARGIN) / per, (max_edge - len(rows) * compare.MARGIN) / sum(shapes), 640))


def pair_sheet(rows, max_edge=1568):
    """One row per view: TARGET, CURRENT and (when there is one) PREVIOUS crop of the same part."""
    per = max(len(row) for _, row in rows)
    shapes = [max(image.height / image.width for _, image in row) for _, row in rows]
    margin = compare.MARGIN
    width = cell_width(rows, max_edge)
    heights = [max(16, int(width * shape)) for shape in shapes]
    sheet = Image.new("RGB", (per * (width + margin), sum(heights) + len(rows) * margin), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    y = margin
    for (view, row), height in zip(rows, heights):
        for n, (label, image) in enumerate(row):
            x = n * (width + margin) + margin
            sheet.paste(compare.fit(image.convert("RGB"), (width, height)), (x, y))
            draw.text((x + 2, y - margin + 1), f"{view} {label}", fill=(255, 220, 90), font=compare.font())
        y += height + margin
    return sheet


flatten = masks.flatten


def parse_pairs(items, what):
    out = {}
    for item in items or []:
        name, _, value = item.partition("=")
        if not (name and value):
            raise ValueError(f"{what} must be NAME=VALUE, got {item!r}")
        out[name] = value
    return out


def prepare(blueprint_path, round_number, renders, captures=None, capture_masks=None, unlock=None, root=None,
            ledger_path=None, pass_score=8, drift_limit=DRIFT):
    blueprint = blueprints.load(blueprint_path)
    tolerance = blueprints.tolerances(blueprint)
    root = Path(root) if root else Path(blueprint_path).parent
    ledger_path = Path(ledger_path) if ledger_path else root / "ledger.json"
    rounds = root / "rounds"
    base = rounds / f"round-{round_number}"
    captures, capture_masks, unlock = captures or {}, capture_masks or {}, unlock or {}
    part_ids = [p["id"] for p in blueprint["parts"]]
    for name in list(captures) + list(capture_masks):
        if name not in [v["id"] for v in blueprint["views"]]:
            raise ValueError(f"unknown view {name}")
    for name in unlock:
        if name not in part_ids:
            raise ValueError(f"unknown part {name} in --unlock")
    uncaptured = [v["id"] for v in blueprint["views"] if v["id"] not in captures]
    if captures and uncaptured:
        raise ValueError(f"no --capture for view(s) {', '.join(uncaptured)}: when the product is captured outside Blender, "
                         "every view in the blueprint needs its own capture")

    # Keep the round self-contained: its renders and captures live in its own folder.
    kept = base / "renders"
    kept.mkdir(parents=True, exist_ok=True)
    (base / "captures").mkdir(exist_ok=True)
    (base / "verdicts").mkdir(exist_ok=True)
    renders = Path(renders)
    missing = [v["id"] for v in blueprint["views"] if not (renders / f"{v['id']}.ids.json").exists()]
    if missing:
        raise ValueError(f"no mask render for view(s) {', '.join(missing)}: every view in the blueprint must be captured and judged")
    view_data = {}
    for view in blueprint["views"]:
        data = json.loads((renders / f"{view['id']}.ids.json").read_text(encoding="utf-8"))
        for name in [f"{view['id']}.ids.json", data["ids"]] + ([data["picture"]] if data.get("picture") else []):
            if renders.resolve() != kept.resolve():
                shutil.copyfile(renders / name, kept / name)
        source = captures.get(view["id"]) or (renders / data["picture"] if data.get("picture") else None)
        if source is None:
            raise ValueError(f"view {view['id']} has no capture: pass --capture {view['id']}=FILE or render with a picture")
        with Image.open(source) as image:
            flatten(image).save(base / "captures" / f"{view['id']}.png")
        view_data[view["id"]] = {"capture_source": "product" if view["id"] in captures else "blender"}

    # 1. Gates. Nothing below is worth a critic's time until these pass.
    matched = viewmatch.check(blueprint, blueprint_path, kept)
    measured = measure.measure(blueprint, blueprint_path, kept)
    failing = [f"view {v['view']}: view match fails ({v.get('why', '')})" for v in matched["views"] if not v["pass"]] + measured["failing"]
    layout = {"pass": not failing, "restart": measured["restart"], "failing": failing}
    if measured.get("advice"):
        layout["advice"] = measured["advice"]
    shaped = form.check(blueprint, blueprint_path, kept, measured) if layout["pass"] else {"parts": {}, "notes": []}
    notes = list(shaped["notes"])  # things worth knowing that stop nothing

    # 2. Captures against the mask renders, and each part's box in each capture.
    target = Image.open(blueprints.target_path(blueprint, blueprint_path)).convert("RGB")
    warnings, boxes, crops, signatures, view_hashes, small = [], {}, {}, {}, {}, {}
    for view in blueprint["views"]:
        vid = view["id"]
        data, part_masks, subject = masks.read_ids(kept / f"{vid}.ids.json")
        capture = Image.open(base / "captures" / f"{vid}.png").convert("RGB")
        render_subject = masks.fraction_box(subject)
        info = {"method": "same render"}
        capture_subject = render_subject
        if view_data[vid]["capture_source"] == "product" and render_subject:
            mask = Image.open(capture_masks[vid]) if vid in capture_masks else None
            found, info = capture_alignment(capture, mask, subject, tolerance["capture_iou"])
            capture_subject = found or render_subject
            if info["method"] == "assumed":
                warnings.append(f"view {vid}: {info['note']}")
            elif not info["pass"]:
                layout["pass"] = False
                layout["failing"].append(f"view {vid}: the capture overlaps the mask render by {info['capture_iou']}, below {tolerance['capture_iou']}; "
                                         "take the capture with the camera recorded in cameras.json")
        view_data[vid].update(alignment=info, camera={k: data["camera"].get(k) for k in ("yaw", "pitch", "lens")})
        if not render_subject:
            continue
        boxes[(ASSEMBLY, vid)] = (view["subject_box"], capture_subject)
        signatures[(ASSEMBLY, vid)] = signature(capture, capture_subject, subject, render_subject)
        for part in blueprint["parts"]:
            view_hashes.setdefault(part["id"], []).append(data["parts"].get(part["id"], {}).get("hash"))
            status = measured["views"].get(vid, {}).get("parts", {}).get(part["id"], {}).get("status")
            box = masks.fraction_box(part_masks[part["id"]]) if part["id"] in part_masks else None
            if vid in part["boxes"] and box and status in ("pass", "fail"):
                boxes[(part["id"], vid)] = (part["boxes"][vid], into(box, render_subject, capture_subject))
                shown = max(boxes[(part["id"], vid)][1][2] * capture.width, boxes[(part["id"], vid)][1][3] * capture.height)
                if shown < SMALL:
                    small.setdefault(vid, []).append((shown, part["id"]))
                signatures[(part["id"], vid)] = signature(capture, boxes[(part["id"], vid)][1], part_masks[part["id"]], box)
        for (name, v), (target_box, capture_box) in boxes.items():
            if v == vid:
                crops[(name, vid)] = (masks.crop_box(target, padded(target_box, target)), masks.crop_box(capture, padded(capture_box, capture)))
        if vid in small:
            shown, name = min(small[vid])
            need = round(max(capture.size) * 2 * SMALL / shown / 64) * 64
            notes.append(f"view {vid}: {', '.join(n for _, n in sorted(small[vid]))} cover under {SMALL} px in the capture "
                            f"({name}: {round(shown)} px); a critic cannot read detail at that size. "
                            f"Capture this view about {need} px on its long edge"
                            + (" (inner.py --long-edge)" if view_data[vid]["capture_source"] == "blender" else ""))

    # 3. States from the earlier rounds, and what changed since a part passed.
    states, _ = verdict.replay_parts(rounds, pass_score, upto=round_number - 1, strict=False) if rounds.exists() else ({}, [])
    # A part may be different objects in different poses, so its hash covers every view.
    hashes = {name: hashlib.sha1(json.dumps(found).encode()).hexdigest()[:16] for name, found in view_hashes.items()}
    hashes[ASSEMBLY] = hashlib.sha1(json.dumps(sorted(hashes.items())).encode()).hexdigest()[:16]
    earlier = [(rounds / f"round-{n}", json.loads((rounds / f"round-{n}" / "review.json").read_text(encoding="utf-8")))
               for n in range(round_number - 1, 0, -1) if (rounds / f"round-{n}" / "review.json").exists()]
    review = {"round": round_number, "views": view_data, "layout": layout, "parts": {}, "regressions": [], "unlocked": unlock, "warnings": warnings}
    for name in part_ids + [ASSEMBLY]:
        state = states.get(name, {"state": "open"})
        views = [v for (n, v) in crops if n == name]
        own = {v: signatures[(name, v)] for v in views}
        changed = False
        if state["state"] in ("passing", "locked"):
            moved = max((drift(own[v], (state.get("signature") or {}).get(v)) for v in views), default=0.0)
            changed = bool(hashes.get(name) != state.get("hash") or moved > drift_limit)
        if changed and state["state"] == "locked" and name != ASSEMBLY and name not in unlock:
            review["regressions"].append(name)
        now = "open" if changed or name in unlock else state["state"]
        review["parts"][name] = {"state_before": state["state"], "state": now, "changed": changed, "hash": hashes.get(name), "signature": own,
                                 "boxes": {v: [round(x, 4) for x in boxes[(name, v)][1]] for v in views},
                                 "send": {"open": "critic", "passing": "confirm", "locked": None}[now]}
        outline = shaped["parts"].get(name)
        if outline:
            review["parts"][name]["outline"] = {"status": outline["status"], "lines": outline["lines"],
                                                "views": {v: {k: r[k] for k in ("status", "overlap", "needs", "why") if k in r}
                                                          for v, r in outline["views"].items()}}
            if outline["status"] == "fail" and review["parts"][name]["send"]:
                review["parts"][name].update(send=None, held="form")  # the difference is measured; a critic would only restate it
    if any(review["parts"][name]["state"] == "open" for name in part_ids):
        review["parts"][ASSEMBLY]["send"] = None  # judged once every part has passed on its own
    if not layout["pass"] or review["regressions"]:
        for entry in review["parts"].values():
            entry["send"] = None

    # 4. Pair sheets and briefs for the parts that go to a critic.
    send = []
    for name, entry in review["parts"].items():
        if not entry["send"]:
            continue
        folder = base / "parts" / name
        folder.mkdir(parents=True, exist_ok=True)
        rows, has_previous = [], False
        # PREVIOUS is what the last critic of this part saw, not merely the last capture taken.
        previous_base, previous = next(((folder_, review_) for folder_, review_ in earlier
                                        if review_["parts"].get(name, {}).get("send") == "critic"), (None, {"parts": {}}))
        for view in blueprint["views"]:
            vid = view["id"]
            if (name, vid) not in crops:
                continue
            row = [("TARGET", crops[(name, vid)][0]), ("CURRENT", crops[(name, vid)][1])]
            old_box = previous["parts"].get(name, {}).get("boxes", {}).get(vid)
            if old_box and entry["send"] == "critic":
                with Image.open(previous_base / "captures" / f"{vid}.png") as old:
                    row.append(("PREVIOUS", masks.crop_box(old.convert("RGB"), padded(old_box, old))))
                has_previous = True
            rows.append((vid, row))
        pair_sheet(rows).save(folder / "pair.png")
        pairs = ["pair.png"]
        if len(rows) > 1 and cell_width(rows) < CELL:  # too small to judge from: give every view a sheet of its own as well
            pairs = [f"pair-{vid}.png" for vid, _ in rows]
            for row, file in zip(rows, pairs):
                pair_sheet([row]).save(folder / file)
        part = next((blueprints.resolve(blueprint, p) for p in blueprint["parts"] if p["id"] == name), None)
        measured_values, settled = {}, []
        for vid in [v for v, _ in rows] if part else []:
            for key, m in measured["views"][vid]["parts"][name]["measures"].items():
                measured_values.setdefault(vid, {})[key] = {"target": m["target"], "current": m["current"]}
                settled.append({"key": f"{name}.{vid}.{key}", "value": m["target"], "source": "blueprint",
                                "fact": f"{name} {measure.LABELS[key]} in {vid} is {m['target']} H (currently {m['current']} H, within tolerance)"})
        settled += ledger.facts_for(ledger_path, name if part else None)
        directives = []
        act_on = states.get(name, {}).get("act_on")
        if act_on and entry["send"] == "critic" and (rounds / act_on).exists():
            old_verdict = verdict.load(rounds / act_on)
            directives = [{"id": item["id"], "change": item["change"]} for key in ("blockers", "next") for item in old_verdict.get(key, [])]
        brief = {"part": name, "round": round_number, "subject": blueprint["subject"], "pair": "pair.png", "pairs": pairs, "views": [v for v, _ in rows],
                 "form": part["form"] if part else ASSEMBLY_FORM, "colours": part["colours"] if part else [],
                 "measured": measured_values, "settled": settled, "has_previous": has_previous, "previous_directives": directives, "scope": SCOPE}
        passed = {v: {"overlap": r["overlap"], "needs": r["needs"]} for v, r in entry.get("outline", {}).get("views", {}).items() if r["status"] == "pass"}
        if passed:
            brief.update(outline=passed, scope=SCOPE + OUTLINE_SCOPE)
        # Generated references the user has approved: a part's own close-ups, or the extra views for the assembly.
        generated = refgen.approved(blueprint, blueprint_path, part=name) if part else refgen.approved(blueprint, blueprint_path, kind="view")
        if generated:
            refgen.reference_sheet(generated[-4:], refgen.refs_dir(blueprint_path)).save(folder / "generated.png")
            brief["generated"] = {"sheet": "generated.png", "rule": GENERATED_RULE,
                                  "images": [{"id": g["id"], "shows": g["shows"], "model": g["model"]} for g in generated[-4:]]}
        (folder / "brief.json").write_text(json.dumps(brief, indent=1) + "\n", encoding="utf-8")
        suffix = "-confirm" if entry["send"] == "confirm" else ""
        send.append({"part": name, "role": entry["send"], "folder": folder.as_posix(),
                     "verdict": (base / "verdicts" / f"{name}{suffix}.json").as_posix()})

    (base / "review.json").write_text(json.dumps(review, indent=1) + "\n", encoding="utf-8")
    held = [name for name, entry in review["parts"].items() if entry.get("held") == "form"]
    return {"round": round_number, "layout": layout["pass"], "failing": layout["failing"], "restart": layout["restart"], "send": send,
            "held": held, "form_failing": [line for name in held for line in review["parts"][name]["outline"]["lines"]], "notes": notes,
            "locked": [n for n, e in review["parts"].items() if e["state_before"] == "locked" and not e["changed"] and n not in unlock],
            "regressions": review["regressions"], "warnings": warnings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["prepare"])
    parser.add_argument("--blueprint", required=True)
    parser.add_argument("--round", type=int, required=True, help="Round number, from 1")
    parser.add_argument("--renders", required=True, help="Folder of mask renders for every view (blender_run.py render)")
    parser.add_argument("--capture", action="append", metavar="VIEW=FILE", help="Capture of the real product for a view; repeatable")
    parser.add_argument("--capture-mask", action="append", metavar="VIEW=FILE", help="Subject mask of that capture (white on the subject); repeatable")
    parser.add_argument("--unlock", action="append", metavar="PART=REASON", help="Reopen a locked part on purpose; repeatable")
    parser.add_argument("--root", help="Working folder (default: the blueprint's folder)")
    parser.add_argument("--ledger", help="Settled-facts ledger (default: ledger.json in the working folder)")
    parser.add_argument("--pass-score", type=float, default=8, help="Score a part must reach (default: %(default)s)")
    parser.add_argument("--drift", type=float, default=DRIFT,
                        help="Colour change in a part's crop that counts as a change (default: %(default)s); "
                             "raise it if two captures of an unchanged model differ by more")
    args = parser.parse_args(argv)
    try:
        if args.round < 1:
            raise ValueError("--round starts at 1")
        result = prepare(args.blueprint, args.round, args.renders, parse_pairs(args.capture, "--capture"),
                         parse_pairs(args.capture_mask, "--capture-mask"), parse_pairs(args.unlock, "--unlock"),
                         args.root, args.ledger, args.pass_score, args.drift)
    except (blueprints.BlueprintError, OSError, ValueError, KeyError) as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["layout"] and not result["regressions"] and (result["send"] or not result["held"]) else 1


if __name__ == "__main__":
    sys.exit(main())
