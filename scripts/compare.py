#!/usr/bin/env python3
"""Prepare a target/capture pair for visual review.

Writes one labelled comparison sheet, one sheet of zoomed region probes and a
small metrics file, so a reviewer reads two capped images and a few numbers
instead of several full-resolution frames. Requires Pillow. Run --help.
"""
import argparse
import json
import math
import string
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("compare.py needs Pillow: python -m pip install Pillow")

MARGIN = 18  # label strip above and left of every panel
WORK_EDGE = 512  # analysis size, so detail figures do not depend on capture resolution
REGRESSION_STEP = 0.04  # change in a cell's difference that counts as better or worse


def font(size=13):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def load(path):
    return Image.open(path).convert("RGB")


def pixels(image):
    return image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()


def cell_name(col, row):
    return f"{string.ascii_uppercase[col]}{row + 1}"


def cell_stats(image, cols, rows):
    """Per-cell mean luma (0-1), mean RGB (0-1) and edge energy (0-1), row-major."""
    scale = WORK_EDGE / max(image.size)
    work = image.resize((max(cols, round(image.width * scale)), max(rows, round(image.height * scale))), Image.BILINEAR)
    grey = work.convert("L")
    luma = pixels(grey.resize((cols, rows), Image.BOX))
    rgb = pixels(work.resize((cols, rows), Image.BOX))
    # Crop the one-pixel border FIND_EDGES leaves bright before averaging.
    edges = grey.filter(ImageFilter.FIND_EDGES).crop((1, 1, work.width - 1, work.height - 1))
    edge = pixels(edges.resize((cols, rows), Image.BOX))
    return [
        {"luma": l / 255, "rgb": tuple(c / 255 for c in p), "detail": e / 255}
        for l, p, e in zip(luma, rgb, edge)
    ]


def difference(a, b):
    """Difference of image b from reference a for one cell: 0 is identical."""
    colour = math.dist(a["rgb"], b["rgb"]) / math.sqrt(3)
    detail = abs(a["detail"] - b["detail"]) / max(a["detail"], b["detail"], 0.02)
    return abs(a["luma"] - b["luma"]) + colour + 0.5 * detail


def describe(target, other):
    return {
        "luma": round(other["luma"] - target["luma"], 3),
        "colour": round(math.dist(target["rgb"], other["rgb"]) / math.sqrt(3), 3),
        "detail_ratio": round(other["detail"] / max(target["detail"], 0.004), 2),
    }


def fit(image, size):
    """Scale without distortion into size, centred on black."""
    scale = min(size[0] / image.width, size[1] / image.height)
    scaled = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGB", size, "black")
    canvas.paste(scaled, ((size[0] - scaled.width) // 2, (size[1] - scaled.height) // 2))
    return canvas


def layout(count, aspect, max_edge):
    """Pick the rows x cols arrangement that gives the largest panels within max_edge."""
    best = None
    for cols in range(1, count + 1):
        rows = math.ceil(count / cols)
        width = min((max_edge - cols * MARGIN) / cols, (max_edge - rows * MARGIN) / rows * aspect)
        if best is None or width > best[0]:
            best = (width, cols, rows)
    width, cols, rows = best
    return cols, rows, (max(16, int(width)), max(16, int(width / aspect)))


def sheet(panels, aspect, max_edge, grid=None):
    """panels: [(label, image)]. Grid letters and numbers go in the margins, never over the image."""
    cols, rows, size = layout(len(panels), aspect, max_edge)
    out = Image.new("RGB", (cols * (size[0] + MARGIN), rows * (size[1] + MARGIN)), (24, 24, 24))
    draw = ImageDraw.Draw(out)
    for index, (label, image) in enumerate(panels):
        x = (index % cols) * (size[0] + MARGIN) + MARGIN
        y = (index // cols) * (size[1] + MARGIN) + MARGIN
        out.paste(fit(image, size), (x, y))
        draw.text((x + 2, y - MARGIN + 1), label, fill=(255, 220, 90), font=font())
        if grid:
            gcols, grows = grid
            start = draw.textlength(label + "  ", font=font())
            for c in range(gcols):
                cx = x + (c + 0.5) * size[0] / gcols
                if cx > x + start:
                    draw.text((cx - 4, y - MARGIN + 1), string.ascii_uppercase[c], fill=(170, 170, 170), font=font())
            for r in range(grows):
                draw.text((x - MARGIN + 4, y + (r + 0.5) * size[1] / grows - 7), str(r + 1), fill=(170, 170, 170), font=font())
    return out


def crop_fraction(image, box):
    """Crop a fractional x, y, w, h box, slid inside the frame so its shape is kept."""
    w, h = min(1.0, box[2]), min(1.0, box[3])
    x, y = min(max(0.0, box[0]), 1 - w), min(max(0.0, box[1]), 1 - h)
    left, top = round(x * image.width), round(y * image.height)
    return image.crop((left, top, max(left + 1, round((x + w) * image.width)), max(top + 1, round((y + h) * image.height))))


def parse_region(text):
    """NAME=x,y,w,h, or NAME=x,y,w,h:x,y,w,h when the capture frames the subject differently from the target."""
    name, _, spec = text.partition("=")
    try:
        boxes = [tuple(float(v) for v in part.split(",")) for part in spec.split(":")]
    except ValueError:
        boxes = []
    if not name or len(boxes) not in (1, 2) or any(
            len(b) != 4 or not all(0 <= v <= 1 for v in b) or b[2] <= 0 or b[3] <= 0 for b in boxes):
        raise argparse.ArgumentTypeError(
            "Use NAME=x,y,w,h (fractions of the frame), or NAME=x,y,w,h:x,y,w,h for target:capture boxes")
    return name, boxes[0], boxes[-1]


def parse_grid(text):
    try:
        cols, rows = (int(v) for v in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("Use COLSxROWS, e.g. 6x4")
    if not (1 <= cols <= 26 and 1 <= rows <= 26):
        raise argparse.ArgumentTypeError("Grid must be between 1x1 and 26x26")
    return cols, rows


def compare(target_path, current_path, previous_path=None, out_dir=".dream-loop/review", max_edge=1568,
            grid=(6, 4), probes=4, regions=(), overview_edge=1024):
    target, current = load(target_path), load(current_path)
    previous = load(previous_path) if previous_path else None
    cols, rows = grid
    aspect = current.width / current.height
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    t_cells, c_cells = cell_stats(target, cols, rows), cell_stats(current, cols, rows)
    diffs = [difference(t, c) for t, c in zip(t_cells, c_cells)]
    names = [cell_name(i % cols, i // cols) for i in range(cols * rows)]
    order = sorted(range(len(diffs)), key=lambda i: -diffs[i])

    metrics = {
        "target": {"file": Path(target_path).as_posix(), "size": list(target.size)},
        "current": {"file": Path(current_path).as_posix(), "size": list(current.size)},
        "grid": f"{cols}x{rows}",
        "frame": describe(cell_stats(target, 1, 1)[0], cell_stats(current, 1, 1)[0]),
        "frame_difference": round(sum(diffs) / len(diffs), 3),
    }
    if abs(target.width / target.height - aspect) / aspect > 0.02:
        metrics["aspect_mismatch"] = True  # capture and target cannot be compared cell for cell

    chosen = order[:max(0, probes)]
    if previous:
        p_cells = cell_stats(previous, cols, rows)
        before = [difference(t, p) for t, p in zip(t_cells, p_cells)]
        change = [now - was for now, was in zip(diffs, before)]
        worse = sorted((i for i in range(len(change)) if change[i] > REGRESSION_STEP), key=lambda i: -change[i])
        better = [i for i in range(len(change)) if change[i] < -REGRESSION_STEP]
        metrics["previous"] = {"file": Path(previous_path).as_posix(), "size": list(previous.size)}
        metrics["since_previous"] = {
            "frame_difference_was": round(sum(before) / len(before), 3),
            "better_cells": [names[i] for i in better],
            "worse_cells": [names[i] for i in worse],
        }
        if probes > 0:  # with probes off the frames are not aligned, so cells say nothing
            chosen += [i for i in worse[:2] if i not in chosen]

    def cell_box(i):
        return ((i % cols - 0.15) / cols, (i // cols - 0.15) / rows, 1.3 / cols, 1.3 / rows)

    boxes = [(names[i], cell_box(i), cell_box(i), i) for i in chosen]
    boxes += [(*region, None) for region in regions]
    metrics["probes"] = []
    groups = []
    for name, t_box, c_box, index in boxes:
        crops = [("TARGET", crop_fraction(target, t_box)), ("CURRENT", crop_fraction(current, c_box))]
        if previous:
            crops.append(("PREVIOUS", crop_fraction(previous, c_box)))
        if index is not None:  # a grid cell: report the figures it was ranked by
            entry = {"where": name, **describe(t_cells[index], c_cells[index])}
            if previous:
                entry["change"] = round(change[index], 3)
        else:
            t_stat, c_stat = (cell_stats(image, 1, 1)[0] for _, image in crops[:2])
            entry = {"where": name, **describe(t_stat, c_stat)}
            if previous:
                was = difference(t_stat, cell_stats(crops[2][1], 1, 1)[0])
                entry["change"] = round(difference(t_stat, c_stat) - was, 3)
        metrics["probes"].append(entry)
        groups.append([(f"{name} {label}", image) for label, image in crops])

    panels = [("TARGET", target), ("CURRENT", current)] + ([("PREVIOUS", previous)] if previous else [])
    compare_sheet = sheet(panels, aspect, min(overview_edge, max_edge), grid)
    compare_sheet.save(out / "compare.png")
    metrics["sheets"] = {"compare": {"file": (out / "compare.png").as_posix(), "size": list(compare_sheet.size)}}
    if groups:
        # One probe per row, its target/current(/previous) crops side by side, each row as tall as its crops need.
        per = len(groups[0])
        shapes = [max(image.height / image.width for _, image in group) for group in groups]
        width = int(min((max_edge - per * MARGIN) / per, (max_edge - len(groups) * MARGIN) / sum(shapes)))
        heights = [max(16, int(width * shape)) for shape in shapes]
        probe_sheet = Image.new("RGB", (per * (width + MARGIN), sum(heights) + len(groups) * MARGIN), (24, 24, 24))
        draw = ImageDraw.Draw(probe_sheet)
        y = MARGIN
        for group, height in zip(groups, heights):
            for n, (label, image) in enumerate(group):
                x = n * (width + MARGIN) + MARGIN
                probe_sheet.paste(fit(image, (width, height)), (x, y))
                draw.text((x + 2, y - MARGIN + 1), label, fill=(255, 220, 90), font=font())
            y += height + MARGIN
        probe_sheet.save(out / "probes.png")
        metrics["sheets"]["probes"] = {"file": (out / "probes.png").as_posix(), "size": list(probe_sheet.size)}

    else:
        (out / "probes.png").unlink(missing_ok=True)  # never leave an earlier round's probes for the reviewer

    (out / "metrics.json").write_text(json.dumps(metrics, indent=1) + "\n", encoding="utf-8")
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=(
        "Outputs in --out: compare.png (labelled panels with grid letters/numbers in the margins),\n"
        "probes.png (zoomed crops of the most different cells and any --region) and metrics.json.\n"
        "When the target is a reference board or another camera, cells do not correspond: pass\n"
        "--probes 0 and name each subject with --region NAME=target-box:capture-box.\n"
        "Figures are current minus target: luma and colour are 0-1; detail_ratio is edge energy\n"
        "current/target (below 1 means flatter than the target). They locate differences; they do not judge them."))
    parser.add_argument("--target", required=True, help="Target image")
    parser.add_argument("--current", required=True, help="Latest capture")
    parser.add_argument("--previous", help="Previous round's capture, to show what changed")
    parser.add_argument("--out", default=".dream-loop/review", help="Output folder (default: %(default)s)")
    parser.add_argument("--max-edge", type=int, default=1568, help="Longest edge of probes.png in pixels (default: %(default)s)")
    parser.add_argument("--overview-edge", type=int, default=1024,
                        help="Longest edge of compare.png; it carries layout and light, the probes carry detail (default: %(default)s)")
    parser.add_argument("--grid", type=parse_grid, default=(6, 4), help="Analysis grid COLSxROWS (default: 6x4)")
    parser.add_argument("--probes", type=int, default=4, help="Most-different cells to zoom into (default: %(default)s)")
    parser.add_argument("--region", type=parse_region, action="append", default=[], metavar="NAME=x,y,w,h[:x,y,w,h]",
                        help="Named probe as fractions of the frame; add a second box after ':' when the capture "
                             "frames the subject differently from the target; repeatable")
    args = parser.parse_args(argv)
    if min(args.max_edge, args.overview_edge) < 256:
        parser.error("--max-edge and --overview-edge must be at least 256")
    metrics = compare(args.target, args.current, args.previous, args.out, args.max_edge, args.grid, args.probes, args.region,
                      args.overview_edge)
    print(json.dumps(metrics, separators=(",", ":")))


if __name__ == "__main__":
    main()
