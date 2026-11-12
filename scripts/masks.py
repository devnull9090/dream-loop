"""Silhouette and ID-mask helpers shared by Dream Loop's 3D checks. Requires Pillow.

A mask is a mode "L" image: 255 on the subject, 0 elsewhere. Boxes are x, y, w, h fractions.
"""
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

KEY_TOLERANCE = 28  # channel distance from the background colour that counts as subject
ID_TOLERANCE = 12  # channel distance within which a mask pixel belongs to a part's ID colour
WORK = 384  # silhouettes are compared at this height


def crop_box(image, box):
    left, top = round(box[0] * image.width), round(box[1] * image.height)
    return image.crop((left, top, max(left + 1, round((box[0] + box[2]) * image.width)), max(top + 1, round((box[1] + box[3]) * image.height))))


def area(mask):
    return mask.histogram()[255]


def near(image, colour, tolerance):
    """Mask of the pixels within tolerance of an RGB colour on every channel."""
    bands = [band.point([255 if abs(v - c) <= tolerance else 0 for v in range(256)]) for band, c in zip(image.split()[:3], colour)]
    return ImageChops.darker(ImageChops.darker(bands[0], bands[1]), bands[2])


def median_colour(strips):
    values = [[], [], []]
    for strip in strips:
        for index, band in enumerate(strip.split()[:3]):
            values[index] += list(band.get_flattened_data() if hasattr(band, "get_flattened_data") else band.getdata())
    return [sorted(v)[len(v) // 2] for v in values]


def border_strips(image):
    edge = max(2, min(image.size) // 50)
    return [image.crop((0, 0, image.width, edge)), image.crop((0, image.height - edge, image.width, image.height)),
            image.crop((0, 0, edge, image.height)), image.crop((image.width - edge, 0, image.width, image.height))]


def border_key(image, tolerance=KEY_TOLERANCE):
    """Key an image against the colour of its own border.

    Returns (mask, share of the border that is that colour); a low share means the background is not flat."""
    image = image.convert("RGB")
    strips = border_strips(image)
    colour = median_colour(strips)
    flat = sum(area(near(s, colour, tolerance)) for s in strips) / sum(s.width * s.height for s in strips)
    return ImageChops.invert(near(image, colour, tolerance)).filter(ImageFilter.MedianFilter(5)), flat


def target_silhouette(target, view, tolerance=KEY_TOLERANCE):
    """The subject's silhouette in one target view, cropped to its subject_box.

    Returns (mask, info). info["reliable"] is False, with a reason, when the background could not be
    keyed cleanly; callers then fall back to comparing part boxes instead of trusting the mask.
    A view may name its own mask image ("mask", white on the subject, the size of the target).
    """
    info = {"method": "mask" if view.get("mask") else "key", "reliable": True}
    if view.get("mask"):
        mask = crop_box(Image.open(view["mask"]).convert("L").resize(target.size), view["subject_box"]).point(lambda v: 255 if v > 127 else 0)
    else:
        background = view.get("background", "auto")
        if background == "none":
            return None, {"method": "key", "reliable": False, "reason": "the blueprint says this view's background cannot be keyed"}
        frame = crop_box(target, view["frame"])
        strips = border_strips(frame)
        colour = median_colour(strips) if background == "auto" else [int(background[i:i + 2], 16) for i in (1, 3, 5)]
        flat = sum(area(near(s, colour, tolerance)) for s in strips) / sum(s.width * s.height for s in strips)
        if background == "auto" and flat < 0.85:
            info.update(reliable=False, reason=f"only {flat:.0%} of the frame's border is one colour; set background or a mask")
        mask = ImageChops.invert(near(crop_box(target, view["subject_box"]), colour, tolerance))
        info["background"] = "#%02x%02x%02x" % tuple(colour)
    scale = WORK / mask.height
    mask = mask.resize((max(1, round(mask.width * scale)), WORK), Image.NEAREST).filter(ImageFilter.MedianFilter(5))
    box = mask.getbbox()
    filled = area(mask) / (mask.width * mask.height)
    if info["reliable"] and (box is None or box[2] - box[0] < 0.9 * mask.width or box[3] - box[1] < 0.9 * mask.height):
        info.update(reliable=False, reason="the keyed subject does not fill subject_box; the key missed part of it")
    elif info["reliable"] and not 0.05 <= filled <= 0.97:
        info.update(reliable=False, reason=f"the keyed subject covers {filled:.0%} of subject_box, which is not a silhouette")
    info["filled"] = round(filled, 3)
    return mask, info


def read_ids(ids_json):
    """An ID render's data, with a mask per part and the whole subject's mask. ids_json is the <view>.ids.json path."""
    data = json.loads(Path(ids_json).read_text(encoding="utf-8"))
    image = Image.open(Path(ids_json).parent / data["ids"]).convert("RGBA")
    alpha = image.getchannel("A").point(lambda v: 255 if v > 127 else 0)
    parts = {name: ImageChops.darker(near(image, part["colour"], ID_TOLERANCE), alpha) for name, part in data["parts"].items()}
    return data, parts, alpha


def fraction_box(mask):
    """The mask's bounding box as x, y, w, h fractions of its image, or None when it is empty."""
    box = mask.getbbox()
    if box is None:
        return None
    return [box[0] / mask.width, box[1] / mask.height, (box[2] - box[0]) / mask.width, (box[3] - box[1]) / mask.height]


def normalised(mask, height=WORK):
    """The mask cropped to its bounding box and scaled to a fixed height, so distance and resolution drop out."""
    box = mask.getbbox()
    if box is None:
        return None
    crop = mask.crop(box)
    return crop.resize((max(1, round(crop.width * height / crop.height)), height), Image.NEAREST)


def overlap(a, b, shift=0.08):
    """Intersection over union of two silhouettes after each is scaled to the same height.

    The better of a few sideways shifts is taken, because a tail or a raised arm moves the
    bounding box's centre without the body being in the wrong place. Returns (iou, a over b aspect).
    """
    a, b = normalised(a), normalised(b)
    if a is None or b is None:
        return 0.0, None
    reach = round(shift * WORK)
    width = max(a.width, b.width) + 2 * reach
    base = Image.new("L", (width, WORK))
    base.paste(a, ((width - a.width) // 2, 0))
    best = 0.0
    for dx in range(-reach, reach + 1, max(1, reach // 6)):
        moved = Image.new("L", (width, WORK))
        moved.paste(b, ((width - b.width) // 2 + dx, 0))
        union = area(ImageChops.lighter(base, moved))
        best = max(best, area(ImageChops.darker(base, moved)) / union if union else 0.0)
    return best, (a.width / a.height) / (b.width / b.height)


def overlay(a, b):
    """Picture of two silhouettes at one height: a only in red, b only in green, both in white."""
    a, b = normalised(a), normalised(b)
    width = max(a.width, b.width) + 16
    canvas_a, canvas_b = Image.new("L", (width, WORK)), Image.new("L", (width, WORK))
    canvas_a.paste(a, ((width - a.width) // 2, 0))
    canvas_b.paste(b, ((width - b.width) // 2, 0))
    return Image.merge("RGB", (canvas_a, canvas_b, ImageChops.darker(canvas_a, canvas_b)))


def box_iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


def within(box, outer):
    """A box restated as fractions of an outer box."""
    return [(box[0] - outer[0]) / outer[2], (box[1] - outer[1]) / outer[3], box[2] / outer[2], box[3] / outer[3]]
