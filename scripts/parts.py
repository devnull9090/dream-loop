#!/usr/bin/env python3
"""Prepare a per-part review round for Dream Loop's 3D mode.

  python parts.py prepare --blueprint .dream-loop/blueprint.json --round 3 --renders .dream-loop/inner \
         [--capture side=captures/side.png ...] [--capture-mask side=masks/side.png ...] [--unlock head="jaw redesigned"]
         [--only head ...]

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
    colours, measured values, settled facts, previous directives). Each colour carries the
    target's value at the blueprint's point and the capture's value at the same place on the
    part, sampled here, so that critics compare one pair of numbers instead of each picking a
    different pixel of a lit painting.
 5. Writes round-N/review.json, which verdict.py reads.

A part the blueprint marks same_as another (a right leg, same_as the left) is sent to the same
critic as that part when the model's two are the same geometry, mirrored or copied, as measured
in Blender: one folder, one verdict, applied to both. When they differ in the model, or the
render carries no such measurement, each gets its own critic.

--only PART (repeatable) makes it a round about those parts alone: only they are sent, and
verdict.py decides on them and carries every other part's state forward as it was. Everything
else still runs for the whole subject: the gates, the capture check, and the watch on locked
parts, so a change to a locked part outside the scope still stops the round.

Prints one JSON line listing the parts to send and where each verdict goes, and the parts held
back by the outline gate with what differs. Exit status 1 when a gate fails, a locked part changed
or every part that needed a critic is held back; 2 when the inputs are unusable. Requires Pillow.
"""
import argparse
import hashlib
import json
import math
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
LOST = 0.03  # share of the subject (or of a part) that a capture's backdrop may swallow before it is worth saying
SMALL = 256  # a part with fewer pixels than this on its longer side in a capture is too small to judge detail from
CELL = 448  # when one sheet of every view would show the crops narrower than this, each view also gets a sheet of its own
SCOPE = ("Layout is already measured and passes: judge form, then colour and material, then surface detail. "
         "The target is a design sheet; lighting and background are not part of the judgement.")
COLOUR_RULE = ("For each colour, hex is the target sampled by the script at the blueprint's point and capture is the product sampled at the "
               "same place on the part; difference compares the two. Judge colour from these pairs and quote them in directives. Do not "
               "take colour values from the crops by eye: a painted target is lit, and two points on one surface of it differ by more "
               "than a correct product differs from it.")
SEARCH = 0.03  # a colour point that misses the part in the capture is looked for this share of the box further out, ring by ring
TWIN_RULE = ("{others} {verb} the same geometry as {part} in the model ({how}), measured, so this one verdict covers {all}. "
             "The rows labelled with another part's name show that part in its own views: read them as further views of the same design. "
             "Every directive you give is applied to all of them, so give each once, and do not ask for one side to differ from the other.")
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


def backdrop_advice(info):
    """What to say when a capture's backdrop is too close to the subject's own colours for the key to separate them."""
    where = f", most of it on {', '.join(info['lost_on'])}" if info.get("lost_on") else ""
    return (f"{info['lost']:.0%} of the subject in the capture is within the key's tolerance of the backdrop {info['backdrop']}{where}, so the key "
            "takes it for background. Capture against a flat, unlit, saturated colour that none of the subject's colours are near "
            "(blueprint.py backdrop suggests one), or pass --capture-mask")


def capture_alignment(capture, mask, render_subject, need, part_masks=None):
    """Where the subject sits in the capture, and whether the capture agrees with the mask render.

    Returns (subject box in the capture or None, info). With no usable mask the capture is assumed
    to be framed exactly as the render, which holds when it was taken with the recorded camera.
    """
    backdrop = None
    if mask is None:
        keyed, flat = masks.border_key(capture)
        filled = masks.area(keyed) / (keyed.width * keyed.height)
        if flat < 0.85 or not 0.01 <= filled <= 0.9:
            return None, {"method": "assumed", "note": "the capture's background cannot be keyed; it is assumed to be framed like the mask render"}
        mask, backdrop = keyed, masks.median_colour(masks.border_strips(capture.convert("RGB")))
    mask = mask.convert("L").point(lambda v: 255 if v > 127 else 0)
    render = render_subject.resize(mask.size, Image.NEAREST)
    union = masks.area(ImageChops.lighter(mask, render))
    iou = masks.area(ImageChops.darker(mask, render)) / union if union else 0.0
    info = {"method": "mask", "capture_iou": round(iou, 3), "pass": iou >= need}
    if backdrop:
        # How much of the subject the key could not tell from the backdrop, and on which parts. Backdrop colour where the render
        # has the subject means that only if the capture is framed like the render: a subject that is shifted or the wrong size
        # leaves real backdrop there, and then shows outside the render's silhouette as well, or with different bounds.
        alike = ImageChops.darker(masks.near(capture.convert("RGB"), backdrop, masks.KEY_TOLERANCE), render)
        lost = masks.area(alike) / max(1, masks.area(render))
        outside = masks.area(ImageChops.darker(mask, ImageChops.invert(render))) / max(1, masks.area(render))
        a, b = masks.fraction_box(mask), masks.fraction_box(render)
        info["backdrop"] = "#%02x%02x%02x" % tuple(backdrop)
        if outside <= max(0.02, lost / 3):
            info.update(lost=round(lost, 3), framed=bool(a and b and max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[0] + a[2] - b[0] - b[2]),
                                                                         abs(a[1] + a[3] - b[1] - b[3])) <= 0.03))
        if info.get("lost", 0) >= LOST and part_masks:
            shares = {name: masks.area(ImageChops.darker(alike, part.resize(mask.size, Image.NEAREST))) / max(1, masks.area(part))
                      for name, part in part_masks.items() if masks.area(part)}
            info["lost_on"] = [name for name, share in sorted(shares.items(), key=lambda kv: -kv[1]) if share >= LOST][:4]
    return masks.fraction_box(mask), info


def lab(rgb):
    """An sRGB colour (0-255 per channel) in CIE L*a*b* under D65."""
    linear = [(v / 255 / 12.92) if v / 255 <= 0.04045 else ((v / 255 + 0.055) / 1.055) ** 2.4 for v in rgb]
    xyz = [sum(m * c for m, c in zip(row, linear)) / white for row, white in (
        ((0.4124564, 0.3575761, 0.1804375), 0.95047), ((0.2126729, 0.7151522, 0.0721750), 1.0), ((0.0193339, 0.1191920, 0.9503041), 1.08883))]
    f = [v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116 for v in xyz]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def colour_difference(target_hex, capture_hex):
    """How the capture's colour differs from the target's: lightness and chroma (L*a*b* units), hue (degrees) and delta E (CIE76)."""
    a, b = (lab([int(value[i:i + 2], 16) for i in (1, 3, 5)]) for value in (target_hex, capture_hex))
    chroma = [math.hypot(c[1], c[2]) for c in (a, b)]
    out = {"lightness": round(b[0] - a[0], 1), "chroma": round(chroma[1] - chroma[0], 1), "delta_e": round(math.dist(a, b), 1)}
    if min(chroma) > 6:  # hue means nothing for a grey
        turn = math.degrees(math.atan2(b[2], b[1]) - math.atan2(a[2], a[1]))
        out["hue"] = round((turn + 180) % 360 - 180)
    words = [f"{abs(out['lightness'])} L* {'darker' if out['lightness'] < 0 else 'lighter'}" if abs(out["lightness"]) >= 2 else None,
             f"chroma {abs(out['chroma'])} {'lower' if out['chroma'] < 0 else 'higher'}" if abs(out["chroma"]) >= 2 else None,
             f"hue {abs(out['hue'])} degrees off" if abs(out.get("hue", 0)) >= 4 else None]
    out["reads"] = f"capture is {', '.join(w for w in words if w) or 'the same'} (delta E {out['delta_e']})"
    return out


def sample_on_part(capture, capture_box, mask, mask_box, point):
    """The capture's colour at a point given as fractions of the part's box, taken from the part's own pixels.

    capture_box and mask_box are the part's box in the capture and in the mask render. When the
    point misses the part there (the shapes differ a little), the nearest place on it is used.
    Returns "#rrggbb", or None when nothing of the part is near."""
    for ring in range(9):
        for dx, dy in ([(0, 0)] if not ring else [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)]):
            u, v = point[0] + dx * ring * SEARCH, point[1] + dy * ring * SEARCH
            if not (0 <= u <= 1 and 0 <= v <= 1):
                continue
            mx, my = (mask_box[0] + u * mask_box[2]) * mask.width, (mask_box[1] + v * mask_box[3]) * mask.height
            if mask.getpixel((min(mask.width - 1, int(mx)), min(mask.height - 1, int(my)))):
                x, y = round((capture_box[0] + u * capture_box[2]) * (capture.width - 1)), round((capture_box[1] + v * capture_box[3]) * (capture.height - 1))
                patch = capture.crop((max(0, x - 2), max(0, y - 2), min(capture.width, x + 3), min(capture.height, y + 3)))
                return "#%02x%02x%02x" % tuple(sorted(compare.pixels(band))[len(compare.pixels(band)) // 2] for band in patch.split())
    return None


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
            ledger_path=None, pass_score=8, drift_limit=DRIFT, only=None):
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
    scope = list(dict.fromkeys(only or []))
    for name in scope:
        if name not in part_ids + [ASSEMBLY]:
            raise ValueError(f"unknown part {name} in --only; choose from {', '.join(part_ids + [ASSEMBLY])}")
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
    warnings, boxes, crops, signatures, view_hashes, small, twin_info, seen = [], {}, {}, {}, {}, {}, {}, {}
    for view in blueprint["views"]:
        vid = view["id"]
        data, part_masks, subject = masks.read_ids(kept / f"{vid}.ids.json")
        capture = Image.open(base / "captures" / f"{vid}.png").convert("RGB")
        render_subject = masks.fraction_box(subject)
        info = {"method": "same render"}
        capture_subject = render_subject
        if view_data[vid]["capture_source"] == "product" and render_subject:
            mask = Image.open(capture_masks[vid]) if vid in capture_masks else None
            found, info = capture_alignment(capture, mask, subject, tolerance["capture_iou"], part_masks)
            capture_subject = found or render_subject
            if info["method"] == "assumed":
                warnings.append(f"view {vid}: {info['note']}")
            elif not info["pass"]:
                layout["pass"] = False
                camera = "take the capture with the camera recorded in cameras.json"
                layout["failing"].append(f"view {vid}: the capture overlaps the mask render by {info['capture_iou']}, below {tolerance['capture_iou']}; "
                                         + (camera if info.get("lost", 0) < LOST else backdrop_advice(info) if info["framed"] else
                                            f"{camera}. If the camera is right: {backdrop_advice(info)}"))
            elif info.get("lost", 0) >= LOST and info["framed"]:
                notes.append(f"view {vid}: the capture passes its check ({info['capture_iou']}), but {backdrop_advice(info)}")
        view_data[vid].update(alignment=info, camera={k: data["camera"].get(k) for k in ("yaw", "pitch", "lens")})
        if not render_subject:
            continue
        boxes[(ASSEMBLY, vid)] = (view["subject_box"], capture_subject)
        signatures[(ASSEMBLY, vid)] = signature(capture, capture_subject, subject, render_subject)
        for part in blueprint["parts"]:
            view_hashes.setdefault(part["id"], []).append(data["parts"].get(part["id"], {}).get("hash"))
            if data["parts"].get(part["id"], {}).get("twin"):
                twin_info.setdefault(part["id"], []).append(data["parts"][part["id"]]["twin"])
            status = measured["views"].get(vid, {}).get("parts", {}).get(part["id"], {}).get("status")
            box = masks.fraction_box(part_masks[part["id"]]) if part["id"] in part_masks else None
            if vid in part["boxes"] and box and status in ("pass", "fail"):
                boxes[(part["id"], vid)] = (part["boxes"][vid], into(box, render_subject, capture_subject))
                seen[(part["id"], vid)] = (capture, part_masks[part["id"]], box)
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
    # Twins: a same_as part that the model, in every view that measured it, shows to be its source's geometry mirrored or copied.
    twins = {}
    for part in blueprint["parts"]:
        found = twin_info.get(part["id"], [])
        if part.get("same_as") and found and all(t.get("of") == part["same_as"] and t.get("how") for t in found):
            twins[part["id"]] = (part["same_as"], found[0]["how"])
        elif part.get("same_as") and found:
            why = next(t.get("why", "they differ") for t in found if not t.get("how"))
            notes.append(f"{part['id']} is same_as {part['same_as']} in the blueprint but not in the model ({why}); each gets its own critic")
    for name, (source, _) in twins.items():  # a change to one is a change to the other, so a round about one is about both
        if scope and (name in scope) != (source in scope):
            scope.append(source if name in scope else name)
            notes.append(f"{scope[-1]} was added to this round: it is the same geometry as {name if scope[-1] == source else source}")
    review = {"round": round_number, "views": view_data, "layout": layout, "parts": {}, "regressions": [], "unlocked": unlock, "warnings": warnings}
    if scope:
        review["scope"] = scope
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
    for name, entry in review["parts"].items():
        if scope and name not in scope:
            entry["send"] = None  # not this round's business; its state is carried forward
            entry.pop("held", None)
    if not layout["pass"] or review["regressions"]:
        for entry in review["parts"].values():
            entry["send"] = None
    for name, (source, _) in twins.items():  # one shape: if either fails its outline, a critic has nothing to add about the other
        for failed, other in ((source, name), (name, source)):
            if review["parts"][failed].get("held") == "form" and review["parts"][other]["send"] and (not scope or other in scope):
                lines = [f"{other} is the same geometry as {failed}, which fails its outline"]
                review["parts"][other].update(send=None, held="form")
                review["parts"][other]["outline"] = {**review["parts"][other].get("outline", {"status": "held", "views": {}}), "lines": lines}
    for name, (source, how) in twins.items():  # owed the same kind of review: one critic sees both
        if review["parts"][name]["send"] and review["parts"][name]["send"] == review["parts"][source]["send"]:
            review["parts"][name]["shared"] = source
            review["parts"][source].setdefault("shares", []).append(name)
            review["parts"][source]["twin"] = how

    # 4. Pair sheets and briefs for the parts that go to a critic. A part's twins share its folder and its critic.
    send = []
    for name, entry in review["parts"].items():
        if not entry["send"] or entry.get("shared"):
            continue
        members = [name] + entry.get("shares", [])
        folder = base / "parts" / name
        folder.mkdir(parents=True, exist_ok=True)
        rows, views, has_previous = [], {member: [] for member in members}, False
        for member in members:
            # PREVIOUS is what the last critic of this part saw, not merely the last capture taken.
            previous_base, previous = next(((folder_, review_) for folder_, review_ in earlier
                                            if review_["parts"].get(member, {}).get("send") == "critic"), (None, {"parts": {}}))
            for view in blueprint["views"]:
                vid = view["id"]
                if (member, vid) not in crops:
                    continue
                row = [("TARGET", crops[(member, vid)][0]), ("CURRENT", crops[(member, vid)][1])]
                old_box = previous["parts"].get(member, {}).get("boxes", {}).get(vid)
                if old_box and entry["send"] == "critic":
                    with Image.open(previous_base / "captures" / f"{vid}.png") as old:
                        row.append(("PREVIOUS", masks.crop_box(old.convert("RGB"), padded(old_box, old))))
                    has_previous = True
                rows.append((vid if len(members) == 1 else f"{member} {vid}", row))
                views[member].append(vid)
        pair_sheet(rows).save(folder / "pair.png")
        pairs = ["pair.png"]
        if len(rows) > 1 and cell_width(rows) < CELL:  # too small to judge from: give every view a sheet of its own as well
            pairs = [f"pair-{label.replace(' ', '-')}.png" for label, _ in rows]
            for row, file in zip(rows, pairs):
                pair_sheet([row]).save(folder / file)
        part = next((blueprints.resolve(blueprint, p) for p in blueprint["parts"] if p["id"] == name), None)
        measured_values, settled = {member: {} for member in members}, []
        for member in members if part else []:
            for vid in views[member]:
                for key, m in measured["views"][vid]["parts"][member]["measures"].items():
                    measured_values[member].setdefault(vid, {})[key] = {"target": m["target"], "current": m["current"]}
                    settled.append({"key": f"{member}.{vid}.{key}", "value": m["target"], "source": "blueprint",
                                    "fact": f"{member} {measure.LABELS[key]} in {vid} is {m['target']} H (currently {m['current']} H, within tolerance)"})
        for member in members:
            settled += [fact for fact in ledger.facts_for(ledger_path, member if part else None) if fact not in settled]
        directives = []
        for act_on in dict.fromkeys(states.get(member, {}).get("act_on") for member in members):
            if act_on and entry["send"] == "critic" and (rounds / act_on).exists():
                old_verdict = verdict.load(rounds / act_on)
                directives += [{"id": item["id"], "change": item["change"]} for key in ("blockers", "next") for item in old_verdict.get(key, [])]
        colours = []
        own = next((p for p in blueprint["parts"] if p["id"] == name), {})
        for colour in part["colours"] if part else []:
            colour, at = dict(colour), colour.get("at")
            # The point lies on the part that carries the colours; a same_as part is sampled at that place on itself, or its mirror image.
            owner = name if own.get("colours") else own.get("same_as")
            box = next((p["boxes"].get(at["view"]) for p in blueprint["parts"] if p["id"] == owner), None) if at else None
            if box and (name, at["view"]) in seen and colour.get("hex"):
                u, v = (at["point"][0] - box[0]) / box[2], (at["point"][1] - box[1]) / box[3]
                capture, mask, mask_box = seen[(name, at["view"])]
                for point in [(u, v)] + ([(1 - u, v)] if owner != name else []):
                    found = sample_on_part(capture, boxes[(name, at["view"])][1], mask, mask_box, point) if 0 <= u <= 1 and 0 <= v <= 1 else None
                    if found:
                        colour.update(capture=found, difference=colour_difference(colour["hex"], found))
                        break
            colours.append(colour)
        brief = {"part": name, "round": round_number, "subject": blueprint["subject"], "pair": "pair.png", "pairs": pairs, "views": views[name],
                 "form": part["form"] if part else ASSEMBLY_FORM, "colours": colours,
                 "measured": measured_values[name], "settled": settled, "has_previous": has_previous, "previous_directives": directives, "scope": SCOPE}
        if any("capture" in colour for colour in colours):
            brief["colour_rule"] = COLOUR_RULE
        if own.get("same_as") and not entry.get("shares") and review["parts"].get(own["same_as"], {}).get("send"):
            entry["same_as"] = own["same_as"]  # judged apart from the part it should match: verdict.py compares the two verdicts
        if len(members) > 1:
            others = members[1:]
            how = "mirrored left to right" if entry["twin"] == "mirror" else "copied"
            brief["also"] = [{"part": member, "views": views[member], "measured": measured_values[member]} for member in others]
            brief["twins"] = TWIN_RULE.format(others=" and ".join(others), verb="is" if len(others) == 1 else "are", part=name, how=how,
                                              all="both" if len(others) == 1 else "all of them")
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
        send.append({"part": name, **({"parts": members} if len(members) > 1 else {}), "role": entry["send"], "folder": folder.as_posix(),
                     "verdict": (base / "verdicts" / f"{name}{suffix}.json").as_posix()})

    (base / "review.json").write_text(json.dumps(review, indent=1) + "\n", encoding="utf-8")
    held = [name for name, entry in review["parts"].items() if entry.get("held") == "form"]
    waiting = [name for name in scope if not review["parts"][name]["send"] and review["parts"][name].get("held") != "form"]
    if waiting and layout["pass"] and not review["regressions"]:
        why = {name: ("it is locked" if review["parts"][name]["state"] == "locked" else "parts are still open" if name == ASSEMBLY else "nothing to send")
               for name in waiting}
        notes.append("--only named parts that need no critic this round: " + ", ".join(f"{name} ({reason})" for name, reason in why.items()))
    return {"round": round_number, "layout": layout["pass"], "failing": layout["failing"], "restart": layout["restart"], "send": send,
            **({"scope": scope} if scope else {}),
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
    parser.add_argument("--only", action="append", metavar="PART", help="Send only this part (or assembly) to a critic this round; repeatable")
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
                         args.root, args.ledger, args.pass_score, args.drift, args.only)
    except (blueprints.BlueprintError, OSError, ValueError, KeyError) as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["layout"] and not result["regressions"] and (result["send"] or not result["held"]) else 1


if __name__ == "__main__":
    sys.exit(main())
