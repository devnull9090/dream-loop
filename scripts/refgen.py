#!/usr/bin/env python3
"""Generate extra reference images from the approved design, for Dream Loop's 3D mode.

One approved image rarely shows enough: a single three-quarter view gives a blockout no depth and
hides the far side, and a part that is small on the board cannot be judged. This asks an image
model for other views of the same subject and for close-ups of single parts, with the approved
design as the input image and a prompt that forbids changing it.

  python refgen.py config [--provider openai] [--model gpt-image-2.6-sunburst] [--quality high]
  python refgen.py key [--provider openai] [--check | --remove]
  python refgen.py plan     --blueprint .dream-loop/blueprint.json [--view front ...] [--part head ...]
  python refgen.py generate --blueprint .dream-loop/blueprint.json --view front --view left --part foot_l
  python refgen.py list     --blueprint .dream-loop/blueprint.json
  python refgen.py approve  --blueprint .dream-loop/blueprint.json view-front-1 [...]
  python refgen.py reject   --blueprint .dream-loop/blueprint.json view-front-1 --reason "six toes"
  python refgen.py boxes    --blueprint .dream-loop/blueprint.json view-front-1 --box head=0.4,0.1,0.2,0.2 --box body=...
  python refgen.py approve  --blueprint .dream-loop/blueprint.json view-front-1 --for-blockout
  python refgen.py generate --blueprint .dream-loop/blueprint.json --pose neutral --view front
  python refgen.py board    --blueprint .dream-loop/blueprint.json view-front-1 view-left-1 view-back-1
  python refgen.py agree    --blueprint .dream-loop/blueprint-turnaround.json

config    shows or sets the provider, model and options kept in .dream-loop/config.json.
key       asks for the provider's API key without showing it and keeps it in .dream-loop/secrets.env,
          which must be ignored by git. The key is never taken as an argument and never printed.
plan      with no --view or --part, proposes what is worth generating and why. With them, shows the
          exact prompts and an estimated cost. Sends nothing.
generate  makes the images. Each is saved in .dream-loop/refs with a sheet (<id>.sheet.png: the
          approved design beside the result, and a colour check) and recorded in refs.json with the
          provider, model, prompt, input hashes, parameters and cost.
approve   records that the user has looked at the sheet and accepted the image. Nothing generated
          is used anywhere until then; reject records why it was not.
boxes     marks the parts on an approved front, back, left or right view (x,y,w,h as fractions of
          that image) and writes <id>.boxes.png to check them by eye. Once the user approves the
          view --for-blockout, blueprint.py blockout takes from it the extents the approved design
          shows from no direction. It changes no measure and no gate.

--view is one of front, back, left, right, top, three_quarter_left, three_quarter_right,
three_quarter_back_left, three_quarter_back_right. --part is a part id, or PART@VIEW for a close-up
from one of those views instead of the approved design's own angle.

--pose neutral asks for the view in a symmetric modelling stance instead of the design's own pose,
the way a character is drawn to be modelled and rigged. board lays approved views of one pose side
by side as a turnaround sheet and writes a starter blueprint for it; if the user approves that
sheet as the new target, the loop models from it and the original design becomes the final check.
agree compares each part's height across the views of a blueprint, to catch views that disagree.

Otherwise a generated image is supplementary evidence. The approved design stays the authority: measures
still come from its boxes, and a critic is told which images are generated and that the original
wins any conflict. config and key need no dependencies; the rest need Pillow.
"""
import argparse
import base64
import datetime
import getpass
import hashlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import blueprint as blueprints  # noqa: E402

PROMPTS = Path(__file__).parent.parent / "references" / "blender-mode" / "prompts"
TEMPLATES = {"view": "view.md", "part": "part-closeup.md"}
SECRETS = "secrets.env"
DEFAULTS = {
    "provider": "openai",
    "model": None,  # the provider's default
    "quality": "high",
    "size": "auto",  # chosen from the subject's shape; or WIDTHxHEIGHT
    "background": "auto",  # the grey that stands furthest from the subject's colours; or #rrggbb
    "max_batch": 4,  # images one generate call may make
    "max_cost_usd": 2.0,  # estimated cost one generate call may reach
    "timeout": 300,  # seconds to wait for one image
    "input_edge": 2048,  # input images are scaled down to this longest edge
    "base_url": None,  # the provider's default
    "pricing": None,  # {"text_input": .., "image_input": .., "image_output": ..} in USD per million tokens
    "params": {},  # anything else the provider's API accepts, passed through as written
}
# name: (yaw, pitch, what the camera sees, how to frame it). Yaw as in the blueprint: 0 is the front,
# 90 the camera at the subject's own left, 180 the back.
LEVEL = "Orthographic projection, or a very long lens from far away, so that there is no perspective distortion and parallel edges stay parallel. The camera is level with the middle of the subject."
VIEWS = {
    "front": (0, 0, "a straight-on front view. The subject faces the camera squarely, so its left and right halves are seen equally and none of its sides shows.", LEVEL),
    "back": (180, 0, "a straight-on back view. The camera is directly behind the subject, which faces squarely away from it.", LEVEL),
    "left": (90, 0, "a pure side view of the subject's own left side. The subject is in exact profile and faces the left edge of the image.", LEVEL),
    "right": (-90, 0, "a pure side view of the subject's own right side. The subject is in exact profile and faces the right edge of the image.", LEVEL),
    "top": (0, 89, "a plan view from directly above, looking straight down, with the subject's front toward the bottom of the image.",
            "Orthographic projection, or a very long lens from far above, so that there is no perspective distortion."),
    "three_quarter_left": (45, 10, "a three-quarter view from the front and the subject's own left, about 45 degrees round from the front, from slightly above.",
                           "A long lens (about 100 mm) from far away, so that perspective distortion is slight."),
    "three_quarter_right": (-45, 10, "a three-quarter view from the front and the subject's own right, about 45 degrees round from the front, from slightly above.",
                            "A long lens (about 100 mm) from far away, so that perspective distortion is slight."),
    "three_quarter_back_left": (135, 10, "a three-quarter view from behind and the subject's own left, about 45 degrees round from the back, from slightly above.",
                                "A long lens (about 100 mm) from far away, so that perspective distortion is slight."),
    "three_quarter_back_right": (-135, 10, "a three-quarter view from behind and the subject's own right, about 45 degrees round from the back, from slightly above.",
                                 "A long lens (about 100 mm) from far away, so that perspective distortion is slight."),
}
GREYS = ["#808080", "#c8c8c8", "#383838"]
SMALL_PX = 160  # a part whose box is smaller than this on the approved design is hard to judge from it
COLOUR_TOLERANCE = 28  # channel distance within which a pixel counts as one of the blueprint's colours
PRESENT = 0.002  # share of the image a blueprint colour must cover to count as present
NEW_SHARE = 0.03  # a colour covering this much of a generated image must exist in the approved design
STANDARD = {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"}


class RefgenError(Exception):
    """Something the user or agent has to fix; the message says what."""


# ---------------------------------------------------------------- configuration and the API key

def load_config(root, overrides=None):
    """Defaults, then .dream-loop/config.json's "generation" block, then command-line overrides."""
    path = Path(root) / "config.json"
    stored = {}
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8")).get("generation") or {}
        except (ValueError, AttributeError) as error:
            raise RefgenError(f"{path}: {error}")
    unknown = set(stored) - set(DEFAULTS)
    if unknown:
        raise RefgenError(f"{path}: generation may only set {sorted(DEFAULTS)}; found {sorted(unknown)}")
    config = {**DEFAULTS, **stored, **{k: v for k, v in (overrides or {}).items() if v is not None}}
    if config["provider"] not in PROVIDERS:
        raise RefgenError(f"unknown provider {config['provider']!r}; available: {', '.join(sorted(PROVIDERS))}")
    return config


def save_config(root, changes):
    path = Path(root) / "config.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data["generation"] = {**(data.get("generation") or {}), **changes}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def git(folder, *args):
    try:
        return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=20).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def exposure(path):
    """Whether git could pick a file up: "ignored", "untracked-repo" (not ignored), "tracked" or "no-git"."""
    path = Path(path).resolve()
    folder = next(p for p in [path.parent, *path.parent.parents] if p.exists())
    if git(folder, "rev-parse", "--is-inside-work-tree") != 0:
        return "no-git"
    if git(folder, "ls-files", "--error-unmatch", str(path)) == 0:
        return "tracked"
    return "ignored" if git(folder, "check-ignore", "-q", str(path)) == 0 else "untracked-repo"


def read_secrets(root):
    path = Path(root) / SECRETS
    found = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() and not name.lstrip().startswith("#"):
                found[name.strip()] = value.strip()
    return found


def write_secrets(root, values):
    path = Path(root) / SECRETS
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# Dream Loop API keys. Keep this file out of version control.\n" + "".join(f"{k}={v}\n" for k, v in sorted(values.items()))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_hidden(prompt):
    """One line from the terminal without showing it. Refuses when it cannot be hidden."""
    if not (sys.stdin and sys.stdin.isatty()):
        raise RefgenError("this is not an interactive terminal, so the key could not be typed without showing it. "
                          "Run the command yourself in a terminal window (PowerShell, cmd or a system terminal), not through an agent or a pipe.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise RefgenError("this terminal cannot hide what is typed; nothing was read. Use a different terminal window.")


def find_key(provider, root):
    """(key, where it came from) for a provider: the environment first, then secrets.env. (None, None) without one."""
    name = PROVIDERS[provider].key_env
    if not name:
        return None, "not needed"
    if os.environ.get(name, "").strip():
        return os.environ[name].strip(), f"environment variable {name}"
    value = read_secrets(root).get(name)
    return (value, f"{(Path(root) / SECRETS).as_posix()}") if value else (None, None)


def store_key(provider, root):
    name = PROVIDERS[provider].key_env
    if not name:
        raise RefgenError(f"provider {provider} needs no key")
    path = Path(root) / SECRETS
    state = exposure(path)
    if state in ("tracked", "untracked-repo"):
        raise RefgenError(f"{path.as_posix()} is {'tracked by' if state == 'tracked' else 'not ignored by'} git, so the key could be committed. "
                          f"Add \"{Path(root).name}/\" to .gitignore{' and remove the file from the index (git rm --cached)' if state == 'tracked' else ''}, "
                          "then run this again. Nothing was stored.")
    value = read_hidden(f"Paste the {provider} API key (it will not be shown), then press Enter: ").strip()
    if not value or any(c.isspace() for c in value):
        raise RefgenError("no key was stored: it was empty or contained spaces")
    write_secrets(root, {**read_secrets(root), name: value})
    return {"stored": path.as_posix(), "provider": provider, "variable": name,
            "git": "ignored" if state == "ignored" else "this folder is not in a git repository; if it becomes one, ignore this file"}


def scrub(text, key):
    return str(text).replace(key, "***") if key else str(text)


# ---------------------------------------------------------------- providers

def http_post(url, headers, body, timeout):
    """POST and return (status, body bytes). The only place this script touches the network."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class Provider:
    """One image service. A new one subclasses this, sets the names below and implements generate()."""
    name = None
    key_env = None  # environment variable (and secrets.env entry) holding the API key
    default_model = None
    models = {}  # model id: one line on what it is for
    aliases = {}  # what a user may call a model: its real id
    max_inputs = 1
    limits = ""  # anything about the service worth telling the user

    def __init__(self, config, key=None):
        self.config, self.key = config, key
        model = config.get("model") or self.default_model
        self.model = self.aliases.get(model, model)

    def generate(self, prompt, images, size):
        """Return (image bytes, usage dict or None, the parameters sent) for a prompt and input PNGs."""
        raise NotImplementedError

    def estimate(self, prompt, images, size):
        """(USD or None, note) for one image, before it is made."""
        return None, "no cost estimate for this provider"

    def cost(self, usage):
        """USD for one finished call, from the usage the service reported; None when it reported none."""
        return None


class OpenAI(Provider):
    """OpenAI's Images API, edit endpoint: prompt plus up to 16 input images, one image back.
    Contract read from developers.openai.com/api/reference/resources/images/methods/edit on 2026-11-12."""
    name = "openai"
    key_env = "OPENAI_API_KEY"
    default_model = "gpt-image-2.6-sunburst"
    models = {"gpt-image-2.6-sunburst": "detailed work and precise edits (default)", "gpt-image-2.6-flare": "faster, for quick drafts",
              "gpt-image-2.5-sunburst": "previous generation", "gpt-image-2": "older; fixed input fidelity"}
    aliases = {"gpt-image-2.6": "gpt-image-2.6-sunburst", "gpt-image-2.5": "gpt-image-2.5-sunburst"}
    max_inputs = 16
    url = "https://api.openai.com/v1/images/edits"
    data_url_limit = 20_000_000  # documented maximum length of one image_url is 20,971,520
    # USD per million tokens, from the model pages on 2026-11-12. Override with "pricing" in the config.
    rates = {"text_input": 5.0, "image_input": 8.0, "image_output": 30.0, "as_of": "2026-11-12"}
    # Output tokens per image. One real call (gpt-image-2.6-sunburst, high, 1024x1024, 2026-11-12) used 1756
    # output tokens and 992 tokens for one input image. The other cells are scaled from that by area and by the
    # ratios in the table OpenAI publishes for its earlier models, so only high/square is a measured figure.
    output_tokens = {"low": {"square": 115, "portrait": 172, "landscape": 172}, "medium": {"square": 446, "portrait": 669, "landscape": 669},
                     "high": {"square": 1756, "portrait": 2634, "landscape": 2634}}
    input_tokens_guess = 1000  # per input image

    def pricing(self):
        return {**self.rates, **(self.config.get("pricing") or {})}

    def body(self, prompt, images, size):
        body = {"model": self.model, "prompt": prompt,
                "images": [{"image_url": "data:image/png;base64," + base64.b64encode(data).decode("ascii")} for data in images]}
        if size:
            body["size"] = size
        if self.config.get("quality"):
            body["quality"] = self.config["quality"]
        body.update(self.config.get("params") or {})
        return body

    def generate(self, prompt, images, size):
        if not self.key:
            raise RefgenError("no OpenAI API key: run `python refgen.py key --provider openai` in your own terminal, or set OPENAI_API_KEY")
        body = self.body(prompt, images, size)
        sent = {k: v for k, v in body.items() if k not in ("prompt", "images")}
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "User-Agent": "dream-loop-refgen/1"}
        try:
            status, raw = http_post(self.config.get("base_url") or self.url, headers, json.dumps(body).encode("utf-8"), self.config["timeout"])
        except http.client.HTTPException as error:  # the connection broke while the reply was being read
            raise RefgenError(scrub(f"openai: the reply was cut off; the image may still have been made and charged ({error!r}). It was not retried.", self.key))
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            timed_out = isinstance(error, TimeoutError) or "timed out" in str(error).lower()
            raise RefgenError(scrub(f"openai: {'the request timed out; it may still have been accepted and charged' if timed_out else 'the request was not sent'} "
                                    f"({error}). It was not retried.", self.key))
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            reply = {}
        if status != 200:
            detail = (reply.get("error") or {}).get("message") if isinstance(reply.get("error"), dict) else None
            raise RefgenError(scrub(f"openai: HTTP {status}: {detail or raw[:300].decode('utf-8', 'replace')}", self.key))
        try:
            image = base64.b64decode(reply["data"][0]["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise RefgenError(f"openai: the reply held no image (keys: {sorted(reply) if isinstance(reply, dict) else type(reply).__name__})")
        return image, reply.get("usage"), sent

    def estimate(self, prompt, images, size):
        quality = self.config.get("quality")
        table = self.output_tokens.get(quality)
        if not table:
            return None, f"no published figure for quality {quality!r}; make one image and read cost_usd in refs.json"
        width, height = (int(v) for v in size.split("x"))
        shape = "square" if width == height else "portrait" if height > width else "landscape"
        tokens = table[shape] * (width * height) / ({"square": 1024 * 1024}.get(shape, 1024 * 1536))
        rates = self.pricing()
        usd = (tokens * rates["image_output"] + len(images) * self.input_tokens_guess * rates["image_input"] + len(prompt) / 4 * rates["text_input"]) / 1e6
        return round(usd, 3), (f"estimate from token rates of {rates['as_of']} (${rates['image_output']:g} per million image output tokens) "
                               "and one measured call; the real cost is recorded after each call")

    def cost(self, usage):
        if not isinstance(usage, dict) or not usage.get("output_tokens"):
            return None
        rates, detail = self.pricing(), usage.get("input_tokens_details") or {}
        text, image = detail.get("text_tokens"), detail.get("image_tokens")
        if text is None or image is None:
            text, image = 0, usage.get("input_tokens", 0)
        return round((text * rates["text_input"] + image * rates["image_input"] + usage["output_tokens"] * rates["image_output"]) / 1e6, 4)


class Fake(Provider):
    """No service at all: returns the first input, resized and stamped FAKE. For trying the steps
    (prompts, files, sheets) without a key or a charge. Its images cannot be approved."""
    name = "fake"
    default_model = "fake-1"
    models = {"fake-1": "the input image again, stamped FAKE; costs nothing"}
    max_inputs = 16

    def generate(self, prompt, images, size):
        from PIL import Image, ImageDraw
        width, height = (int(v) for v in size.split("x"))
        with Image.open(io.BytesIO(images[0])) as source:
            source = source.convert("RGB")
            ground = source.getpixel((0, 0))
            source.thumbnail((round(width * 0.8), round(height * 0.8)))
            image = Image.new("RGB", (width, height), ground)
            image.paste(source, ((width - source.width) // 2, (height - source.height) // 2))
        ImageDraw.Draw(image).text((8, 8), "FAKE", fill=(255, 0, 255))
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue(), None, {"model": self.model, "size": size}

    def estimate(self, prompt, images, size):
        return 0.0, "the fake provider costs nothing and makes no real image"


PROVIDERS = {"openai": OpenAI, "fake": Fake}


# ---------------------------------------------------------------- what to ask for

def label(part_id):
    """A part id as words: leg_l is "left leg"."""
    bits = part_id.split("_")
    side = {"l": "left", "r": "right"}.get(bits[-1]) if len(bits) > 1 else None
    return f"{side} {' '.join(bits[:-1])}" if side else " ".join(bits)


def camera_words(camera):
    """A blueprint camera estimate as a phrase: "in a three-quarter view from the front and its own left, from slightly above"."""
    yaw = (camera["yaw"] + 180) % 360 - 180
    side = "left" if yaw > 0 else "right"
    turn = abs(yaw)
    where = ("from directly in front" if turn <= 15 else f"in a three-quarter view from the front and its own {side}" if turn < 75
             else f"from its own {side} side" if turn <= 105 else f"in a three-quarter view from behind and its own {side}" if turn < 165
             else "from directly behind")
    pitch = camera.get("pitch", 0)
    height = (", from well above" if pitch > 30 else ", from slightly above" if pitch > 5 else ", from slightly below" if pitch < -5 else "")
    return where + height


def hex_rgb(value):
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def colours_of(blueprint, part):
    return [c for c in (blueprints.resolve(blueprint, part).get("colours") or []) if c.get("hex")]


def pick_background(blueprint, config):
    """The neutral grey furthest from every colour on the subject, so the subject can be keyed off it."""
    if config["background"] != "auto":
        return config["background"]
    used = [hex_rgb(c["hex"]) for part in blueprint["parts"] for c in colours_of(blueprint, part)]
    return max(GREYS, key=lambda grey: min((max(abs(a - b) for a, b in zip(hex_rgb(grey), colour)) for colour in used), default=0))


def grey_words(value):
    level = sum(hex_rgb(value)) / 3
    return f"{'dark' if level < 90 else 'light' if level > 170 else 'mid'} neutral grey ({value})"


def parse_job(text, kind, blueprint):
    """--view NAME or --part PART[@VIEW] as a job."""
    part, view = (None, text) if kind == "view" else (text.partition("@")[0], text.partition("@")[2] or None)
    if view is not None and view not in VIEWS:
        raise RefgenError(f"unknown view {view!r}; choose from {', '.join(VIEWS)}")
    if kind == "part" and part not in [p["id"] for p in blueprint["parts"]]:
        raise RefgenError(f"unknown part {part!r}; the blueprint has {', '.join(p['id'] for p in blueprint['parts'])}")
    return {"kind": kind, "part": part, "view": view}


def box_pixels(box, size):
    return box[2] * size[0], box[3] * size[1]


def source_view(blueprint, job, wanted=None):
    """The view of the approved design a job is made from: the one named, else the one showing the part largest, else the reference view."""
    views = {v["id"]: v for v in blueprint["views"]}
    if wanted:
        if wanted not in views:
            raise RefgenError(f"unknown view {wanted!r} in --from; the blueprint has {', '.join(views)}")
        if job["kind"] == "part" and wanted not in next(p for p in blueprint["parts"] if p["id"] == job["part"])["boxes"]:
            raise RefgenError(f"part {job['part']} has no box in view {wanted}")
        return views[wanted]
    if job["kind"] == "part":
        boxes = next(p for p in blueprint["parts"] if p["id"] == job["part"])["boxes"]
        return views[max(boxes, key=lambda v: boxes[v][2] * boxes[v][3])]
    return views[blueprint["reference_view"]]


def shape_size(width, height, config):
    if config["size"] != "auto":
        return config["size"]
    ratio = width / max(height, 1e-9)
    return STANDARD["landscape" if ratio > 1.25 else "portrait" if ratio < 0.8 else "square"]


def png(image, edge, limit=None):
    """An image as PNG bytes, scaled down to a longest edge and, where a limit is given, until its base64 fits."""
    image = image.convert("RGB")
    while True:
        copy = image.copy()
        copy.thumbnail((edge, edge))
        out = io.BytesIO()
        copy.save(out, "PNG")
        if not limit or len(out.getvalue()) * 4 / 3 + 64 < limit or edge <= 256:
            return out.getvalue()
        edge = int(edge * 0.75)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def template(kind, root):
    """(path, body) of a prompt template: the project's own copy in prompts/ when there is one, else the skill's."""
    path = next((p for p in (Path(root) / "prompts" / TEMPLATES[kind], PROMPTS / TEMPLATES[kind]) if p.exists()), None)
    if path is None:
        raise RefgenError(f"prompt template {TEMPLATES[kind]} is missing from {PROMPTS}")
    text = "\n" + path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if "\n## Prompt\n" not in text:
        raise RefgenError(f"{path}: the prompt must follow a line reading \"## Prompt\"")
    return path, text.split("\n## Prompt\n", 1)[1].strip()


def fill(body, values, path="template"):
    def swap(match):
        if match.group(1) not in values:
            raise RefgenError(f"{path}: unknown placeholder {match.group(0)}; available: {', '.join('{' + k + '}' for k in sorted(values))}")
        return values[match.group(1)]
    text = "\n".join(line.rstrip() for line in re.sub(r"\{([a-z_]+)\}", swap, body).splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


NEUTRAL = ("Pose: a neutral modelling stance, not the pose in image 1. The subject stands upright and symmetric with its weight even on both feet, "
           "its head level and facing straight ahead, and its limbs relaxed and held a little away from the body so that no part hides another. "
           "Changing the stance must not change any part: every part keeps the length, thickness, shape and count it has in image 1, as if the same "
           "rigid parts were only turned at their joints.")


def build(blueprint, blueprint_path, job, config, provider, source=None, extra_refs=(), note=None, pose_mode="design"):
    """Everything one job sends: the prompt, the input images and what each is, and the output size."""
    from PIL import Image
    root = Path(blueprint_path).parent
    target_file = blueprints.target_path(blueprint, blueprint_path)
    target = Image.open(target_file).convert("RGB")
    view = source_view(blueprint, job, source)
    limit = getattr(provider, "data_url_limit", None)
    part = next((p for p in blueprint["parts"] if p["id"] == job["part"]), None)
    subject, pose = blueprint["subject"], view["pose"]

    def frame_of(v):
        return target.crop((round(v["frame"][0] * target.width), round(v["frame"][1] * target.height),
                            round((v["frame"][0] + v["frame"][2]) * target.width), round((v["frame"][1] + v["frame"][3]) * target.height)))

    images = [{"role": "design", "view": view["id"], "box": view["frame"], "data": png(frame_of(view), config["input_edge"], limit)}]
    lines = [f"Image 1 is the approved design of {subject}, seen {camera_words(view['camera'])}. It is the authority for what this subject looks like."]
    for other in [v for v in blueprint["views"] if v["pose"] == pose and v["id"] != view["id"]][:3]:
        images.append({"role": "design", "view": other["id"], "box": other["frame"], "data": png(frame_of(other), config["input_edge"], limit)})
        lines.append(f"Image {len(images)} is the same approved design, seen {camera_words(other['camera'])}. It has the same authority as image 1.")
    if part:
        x, y, w, h = part["boxes"][view["id"]]
        grow = 0.25 * max(w, h)  # enough of the neighbours to show where the part sits
        box = [max(0.0, x - grow), max(0.0, y - grow), min(1.0, x + w + grow) - max(0.0, x - grow), min(1.0, y + h + grow) - max(0.0, y - grow)]
        crop = target.crop((round(box[0] * target.width), round(box[1] * target.height),
                            round((box[0] + box[2]) * target.width), round((box[1] + box[3]) * target.height)))
        scale = max(1.0, 768 / max(crop.size))
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.LANCZOS)
        images.append({"role": "part crop", "view": view["id"], "box": [round(v, 4) for v in box], "data": png(crop, config["input_edge"], limit)})
        lines.append(f"Image {len(images)} is an enlarged crop of image 1 showing the {label(part['id'])} and a little around it. It is image 1's own pixels "
                     "magnified, so it is soft: use it to see which part is meant and where its edges are, not as a level of sharpness to copy.")
    stance = ""
    for entry in extra_refs:
        images.append({"role": "generated reference", "ref": entry["id"],
                       "data": png(Image.open(root / "refs" / entry["file"]), config["input_edge"], limit)})
        lines.append(f"Image {len(images)} is an additional reference of the same subject ({entry['shows']}), generated earlier and accepted by the designer. "
                     "Stay consistent with it, but wherever it differs from image 1 in any way, image 1 is right.")
        if pose_mode == "neutral" and entry.get("pose") == "neutral":
            stance = f" Use exactly the stance shown in image {len(images)}: the same limb angles, seen from the new camera."
    if len(images) > provider.max_inputs:
        raise RefgenError(f"{len(images)} input images, but {provider.name} takes at most {provider.max_inputs}")

    path, body = template(job["kind"], root)
    background = pick_background(blueprint, config)
    values = {"inputs": "\n".join(lines), "subject": subject, "background": grey_words(background),
              "extra": f"Additional instruction from the designer: {note.strip()}" if note and note.strip() else ""}
    if job["kind"] == "view":
        yaw, pitch, description, framing = VIEWS[job["view"]]
        width, height = box_pixels(view["subject_box"], target.size)
        size = shape_size(1, 1, config) if job["view"] == "top" else shape_size(width, height, config)
        shows = f"{job['view'].replace('_', ' ')} view"
        values.update(view_name=job["view"].replace("_", " "), view_description=description, framing=framing,
                      pose_clause=f": the {view['pose']} pose shown in image 1",
                      pose_rule=NEUTRAL + stance if pose_mode == "neutral" else f"Pose: the {view['pose']} pose shown in image 1. Do not re-pose, relax or animate the subject.",
                      parts=", ".join(label(p["id"]) for p in blueprint["parts"]),
                      palette="; ".join(f"{label(p['id'])}: " + ", ".join(f"{c['name']} {c['hex']}" for c in colours_of(blueprint, p))
                                        for p in blueprint["parts"] if colours_of(blueprint, p) and not p.get("same_as")) or "as in image 1")
    else:
        resolved = blueprints.resolve(blueprint, part)
        joined = [(next(n for n in joint["parts"] if n != part["id"]), joint["id"]) for joint in blueprint.get("joints") or [] if part["id"] in joint["parts"]]
        width, height = box_pixels(part["boxes"][view["id"]], target.size)
        size = shape_size(width, height, config)
        if job["view"]:
            _, _, description, framing = VIEWS[job["view"]]
            closeup_view = f"seen in {description}"
            shows = f"{label(part['id'])} close-up, {job['view'].replace('_', ' ')} view"
        else:
            closeup_view = f"seen from the same camera direction as image 1 ({camera_words(view['camera'])}). Do not turn the part or the camera."
            framing = "The same camera direction as image 1, with a long lens so that perspective distortion is slight."
            shows = f"{label(part['id'])} close-up, same angle as the approved design"
        values.update(part=label(part["id"]), form=resolved["form"], closeup_view=closeup_view, framing=framing,
                      colours=", ".join(f"{c['name']} {c['hex']}" for c in colours_of(blueprint, part)) or "as in image 1",
                      neighbours=("It joins " + " and ".join(f"the {label(other)} at the {joint.replace('_', ' ')}" for other, joint in joined) + ".") if joined else "")
    if not re.fullmatch(r"\d+x\d+", size):
        raise RefgenError(f"size must be WIDTHxHEIGHT or \"auto\", not {size!r}")
    if pose_mode == "neutral" and job["kind"] == "view":
        shows += ", neutral pose"
    return {"job": job, "source": view["id"], "shows": shows, "prompt": fill(body, values, path), "images": images, "size": size,
            "pose": "neutral" if pose_mode == "neutral" and job["kind"] == "view" else view["pose"],
            "background": background, "template": {"file": path.as_posix(), "sha256": sha(path.read_bytes())},
            "target": {"file": target_file.name, "sha256": sha(target_file.read_bytes())}}


# ---------------------------------------------------------------- the manifest

def refs_dir(blueprint_path):
    return Path(blueprint_path).parent / "refs"


def read_manifest(folder):
    path = Path(folder) / "refs.json"
    if not path.exists():
        return {"version": 1, "refs": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not (isinstance(data, dict) and isinstance(data.get("refs"), list)):
        raise RefgenError(f"{path}: not a refs manifest")
    return data


def write_manifest(folder, data):
    Path(folder).mkdir(parents=True, exist_ok=True)
    (Path(folder) / "refs.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def state(entry, folder, target_sha=None):
    """pending, approved, rejected, or stale: <why> when the file or the approved design changed since."""
    file = Path(folder) / entry["file"]
    if not file.exists():
        return "stale: the image file is missing"
    if sha(file.read_bytes()) != entry["sha256"]:
        return "stale: the image file changed after it was generated"
    if target_sha and entry["target"]["sha256"] not in target_sha:
        return "stale: the approved design changed after this was generated"
    return "rejected" if entry.get("rejected") else "approved" if entry.get("approved") else "pending"


def target_sha(blueprint, blueprint_path):
    """Hashes of the images a reference may have been made from: the target, and the original design when the target is a turnaround built from it."""
    files = [blueprints.target_path(blueprint, blueprint_path)]
    if blueprint.get("design"):
        files.append(Path(blueprint_path).parent / blueprint["design"])
    return tuple(sha(f.read_bytes()) for f in files if f.exists())


def approved(blueprint, blueprint_path, part=None, kind=None):
    """The generated references that may be used: approved by the user, unchanged since, and made from the current approved design.

    With part, that part's close-ups, plus those of the part it is the same_as. Anything else is ignored."""
    folder = refs_dir(blueprint_path)
    if not (folder / "refs.json").exists():
        return []
    current = target_sha(blueprint, blueprint_path)
    wanted = None
    if part:
        wanted = {part, next((p.get("same_as") for p in blueprint["parts"] if p["id"] == part), None)} - {None}
    return [entry for entry in read_manifest(folder)["refs"]
            if state(entry, folder, current) == "approved" and (kind is None or entry["kind"] == kind) and (wanted is None or entry.get("part") in wanted)]


def blockout_hints(blueprint, blueprint_path):
    """Measures, in H, from the generated views the user approved for the blockout: one entry per view with its yaw.

    Only level views count (front, back, left, right), and only while the view and its boxes are as approved."""
    hints = []
    for entry in approved(blueprint, blueprint_path, kind="view"):
        marked = entry.get("blockout") or {}
        if not (marked.get("approved") and entry.get("boxes") and abs(entry["camera"]["pitch"]) < 1):
            continue
        parts = {name: blueprints.measure_box(box, entry["subject_box"], entry["size"]) for name, box in entry["boxes"].items()}
        hints.append({"ref": entry["id"], "yaw": entry["camera"]["yaw"], "parts": parts})
    return hints


def mark_boxes(args):
    """Record part boxes on an approved generated view and draw them for checking."""
    from PIL import Image, ImageDraw
    import masks
    blueprint = blueprints.load(args.blueprint)
    folder = refs_dir(args.blueprint)
    manifest = read_manifest(folder)
    entry = next((e for e in manifest["refs"] if e["id"] == args.id), None)
    if entry is None:
        raise RefgenError(f"no reference {args.id}; see `refgen.py list`")
    if state(entry, folder, target_sha(blueprint, args.blueprint)) != "approved":
        raise RefgenError(f"{args.id} is not an approved, current reference; the user accepts the image before boxes are drawn on it")
    if entry["kind"] != "view" or abs(entry["camera"]["pitch"]) >= 1:
        raise RefgenError("only a front, back, left or right view can inform the blockout")

    def numbers(text, what):
        try:
            box = [float(v) for v in text.split(",")]
        except ValueError:
            box = None
        if not blueprints.is_box(box):
            raise RefgenError(f"{what} must be x,y,w,h as fractions of the generated image, got {text!r}")
        return box

    image = Image.open(folder / entry["file"]).convert("RGB")
    if args.subject:
        subject = numbers(args.subject, "--subject")
    else:
        keyed, flat = masks.border_key(image)
        subject = masks.fraction_box(keyed)
        if flat < 0.9 or subject is None:
            raise RefgenError("the subject could not be keyed off the background; give its box with --subject x,y,w,h")
    boxes = {}
    known = [p["id"] for p in blueprint["parts"]]
    for item in args.box:
        name, _, value = item.partition("=")
        if name not in known:
            raise RefgenError(f"unknown part {name!r} in --box; the blueprint has {', '.join(known)}")
        boxes[name] = numbers(value, f"--box {name}")
        if not blueprints.inside(boxes[name], subject, 0.02):
            raise RefgenError(f"--box {name} reaches outside the subject's box {[round(v, 3) for v in subject]}")
    entry.update(subject_box=[round(v, 4) for v in subject], boxes=boxes, boxes_sheet=f"{entry['id']}.boxes.png")
    entry.pop("blockout", None)  # new boxes need the user's eye again
    draw = ImageDraw.Draw(image)
    for name, box in [("subject", subject)] + list(boxes.items()):
        colour = (255, 255, 255) if name == "subject" else (255, 80, 80)
        draw.rectangle((box[0] * image.width, box[1] * image.height, (box[0] + box[2]) * image.width, (box[1] + box[3]) * image.height), outline=colour, width=3)
        draw.text((box[0] * image.width + 5, box[1] * image.height + 4), name, fill=colour)
    image.save(folder / entry["boxes_sheet"])
    write_manifest(folder, manifest)
    return {"id": entry["id"], "boxes": sorted(boxes), "sheet": (folder / entry["boxes_sheet"]).as_posix(),
            "next": f"show the user this sheet; `refgen.py approve {entry['id']} --for-blockout` records that these boxes may set the blockout's unseen extents"}


def reference_sheet(entries, folder, max_edge=1568):
    """Approved generated references side by side, each labelled GENERATED, for a critic."""
    from PIL import Image, ImageDraw
    import compare
    edge = min(640, (max_edge - 12) // max(len(entries), 1) - 12)
    sheet = Image.new("RGB", (len(entries) * (edge + 12) + 12, edge + 44), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for n, entry in enumerate(entries):
        with Image.open(Path(folder) / entry["file"]) as image:
            sheet.paste(compare.fit(image.convert("RGB"), (edge, edge)), (12 + n * (edge + 12), 36))
        draw.text((12 + n * (edge + 12), 4), f"GENERATED {entry['id']}", fill=(255, 140, 60), font=compare.font())
        draw.text((12 + n * (edge + 12), 20), entry["shows"][:70], fill=(200, 200, 200), font=compare.font(11))
    return sheet


# ---------------------------------------------------------------- checks and the approval sheet

def dominant(image, count=24, edge=160):
    """The main colours of an image as (rgb, share), largest first."""
    from PIL import Image
    small = image.convert("RGB")
    small.thumbnail((edge, edge))
    quantised = small.quantize(count, method=getattr(Image, "Quantize", Image).MEDIANCUT)
    palette = quantised.getpalette()
    total = small.width * small.height
    return sorted(((tuple(palette[i * 3:i * 3 + 3]), n / total) for n, i in quantised.getcolors()), key=lambda c: -c[1])


def share(image, colour, tolerance=COLOUR_TOLERANCE):
    import masks
    return masks.area(masks.near(image, colour, tolerance)) / (image.width * image.height)


def check(blueprint, entry, generated, design):
    """How far a generated image can be seen to depart from the approved design, without judging it.

    Checks what arithmetic can: that the blueprint's sampled colours are present, that no large new
    colour has appeared, and that the background is flat. It cannot tell whether the shape is right;
    that is what the sheet and the user are for."""
    import masks
    generated, design = generated.convert("RGB"), design.convert("RGB")
    small = generated.copy()
    small.thumbnail((384, 384))
    design_small = design.copy()
    design_small.thumbnail((384, 384))
    part = next((p for p in blueprint["parts"] if p["id"] == entry.get("part")), None)
    wanted = [(label(p["id"]), c) for p in ([part] if part else blueprint["parts"]) if not (p.get("same_as") and not part)
              for c in colours_of(blueprint, p)]
    main = dominant(small)
    palette, warnings_ = [], []
    for owner, colour in wanted:
        rgb = hex_rgb(colour["hex"])
        covered = share(small, rgb)
        nearest = min(main, key=lambda c: max(abs(a - b) for a, b in zip(c[0], rgb)))[0]
        palette.append({"part": owner, "name": colour["name"], "hex": colour["hex"], "share": round(covered, 4),
                        "nearest": "#%02x%02x%02x" % nearest, "present": covered >= PRESENT})
        if covered < PRESENT:
            warnings_.append(f"{owner} colour {colour['name']} {colour['hex']} is missing; the nearest colour in the image is {palette[-1]['nearest']}")
    ground = tuple(masks.median_colour(masks.border_strips(small)))
    for rgb, covered in main:
        if covered >= NEW_SHARE and max(abs(a - b) for a, b in zip(rgb, ground)) > COLOUR_TOLERANCE and share(design_small, rgb, 40) < 0.0005:
            warnings_.append("#%02x%02x%02x covers %d%% of the image but does not appear in the approved design" % (*rgb, round(covered * 100)))
    keyed, flat = masks.border_key(small)
    filled = masks.area(keyed) / (keyed.width * keyed.height)
    if flat < 0.9:
        warnings_.append(f"the background is not flat ({round(flat * 100)}% of the border is one colour)")
    elif not 0.03 <= filled <= 0.9:
        warnings_.append(f"the subject fills {round(filled * 100)}% of the image; expected a single centred subject")
    return {"ok": not warnings_, "warnings": warnings_, "palette": palette, "background_flat": round(flat, 3), "subject_fill": round(filled, 3)}


def approval_sheet(entry, inputs, generated, max_edge=1568):
    """The approved design (and the part crop) beside the generated image, with the colour check, for the user to accept or refuse."""
    from PIL import Image, ImageDraw
    import compare
    panels = [("APPROVED DESIGN (the authority)" if n == 0 else "APPROVED DESIGN, another view" if role == "design" else "PART CROP SENT", image)
              for n, (role, image) in enumerate(inputs)]
    panels.append((f"GENERATED {entry['id']} (not yet approved)", generated))
    edge = min(600, (max_edge - 12) // len(panels) - 12)
    rows = entry["checks"]["palette"]
    notes = entry["checks"]["warnings"]
    sheet = Image.new("RGB", (len(panels) * (edge + 12) + 12, edge + 40 + 22 * len(rows) + 18 * len(notes) + 30), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for n, (title, image) in enumerate(panels):
        x = 12 + n * (edge + 12)
        sheet.paste(compare.fit(image.convert("RGB"), (edge, edge)), (x, 26))
        draw.text((x, 6), title[:int(edge / 7)], fill=(255, 140, 60) if n == len(panels) - 1 else (255, 220, 90), font=compare.font())
    y = edge + 36
    draw.text((12, y - 6), f"Asked for: {entry['shows']}, with {entry['model']}", fill=(200, 200, 200), font=compare.font(12))
    y += 16
    for row in rows:
        draw.rectangle((12, y, 40, y + 16), fill=hex_rgb(row["hex"]))
        draw.rectangle((44, y, 72, y + 16), fill=hex_rgb(row["nearest"]))
        draw.text((80, y + 1), f"{row['part']} {row['name']} {row['hex']} (design) | nearest in generated {row['nearest']} | "
                               f"{'present' if row['present'] else 'MISSING'}", fill=(200, 200, 200) if row["present"] else (255, 90, 90), font=compare.font(12))
        y += 22
    for note in notes or ["no automatic check failed; that does not mean the design is unchanged: compare the pictures"]:
        draw.text((12, y + 4), ("CHECK: " if notes else "") + note[:200], fill=(255, 90, 90) if notes else (150, 150, 150), font=compare.font(12))
        y += 18
    return sheet


# ---------------------------------------------------------------- commands

def suggest(blueprint, blueprint_path):
    """What is worth generating for this blueprint, each with the reason."""
    root = Path(blueprint_path).parent
    out = []
    have = {(e["kind"], e.get("part"), e.get("view")) for e in read_manifest(refs_dir(blueprint_path))["refs"]
            if state(e, refs_dir(blueprint_path), target_sha(blueprint, blueprint_path)) in ("approved", "pending")}
    pose = next(v["pose"] for v in blueprint["views"] if v["id"] == blueprint["reference_view"])
    yaws = [(v["camera"]["yaw"] + 180) % 360 - 180 for v in blueprint["views"] if v["pose"] == pose]
    assumed = [p["id"] for p in blueprints.blockout(blueprint)["parts"] if p["assumed"]]
    if assumed:
        near = "left" if sum(yaws) >= 0 else "right"
        far = "right" if near == "left" else "left"
        covered = {"front": any(abs(y) <= 20 for y in yaws), "back": any(abs(y) >= 160 for y in yaws),
                   "left": any(abs(y - 90) <= 20 for y in yaws), "right": any(abs(y + 90) <= 20 for y in yaws)}
        why = {"front": "gives the blockout its width", near: "gives the blockout its depth", far: "shows the side the approved design hides",
               "back": "shows the back, which the approved design hides"}
        for name in ("front", near, far, "back"):
            if not covered[name] and ("view", None, name) not in have:
                out.append({"job": f"--view {name}", "why": f"{len(assumed)} part(s) are seen from one direction only "
                                                           f"({', '.join(assumed[:6])}{'...' if len(assumed) > 6 else ''}); a {name} view {why[name]}"})
    reasons = {}
    for part in blueprint["parts"]:
        largest = max(max(box_pixels(box, blueprint["target_size"])) for box in part["boxes"].values())
        if largest < SMALL_PX:
            reasons[part["id"]] = f"it is {round(largest)} px across on the approved design, too small to judge form or detail from"
    rounds = root / "rounds"
    if rounds.exists():
        import verdict
        try:
            states, _ = verdict.replay_parts(rounds, strict=False)
        except (ValueError, OSError, KeyError):
            states = {}
        for name, entry in states.items():
            if name not in [p["id"] for p in blueprint["parts"]]:
                continue
            unclear = []
            if entry.get("act_on") and (rounds / entry["act_on"]).exists():
                found = verdict.load(rounds / entry["act_on"]).get("target_unclear")
                unclear = [str(u) for u in found] if isinstance(found, list) else []
            if unclear:
                reasons[name] = "its critic could not judge it from the approved design: " + "; ".join(unclear[:2])
            elif entry.get("decision") in ("rethink", "ask_user"):
                reasons[name] = f"verdict.py says {entry['decision']} for it: {entry.get('why', '')}"
    for name, why in reasons.items():
        source = next(p.get("same_as") for p in blueprint["parts"] if p["id"] == name) or name
        if ("part", name, None) not in have and ("part", source, None) not in have:
            out.append({"job": f"--part {name}", "why": why})
    return out


def jobs_from(args, blueprint, blueprint_path):
    jobs = [parse_job(v, "view", blueprint) for v in args.view or []] + [parse_job(p, "part", blueprint) for p in args.part or []]
    if getattr(args, "suggested", False):
        for item in suggest(blueprint, blueprint_path):
            kind, value = item["job"].split()
            jobs.append(parse_job(value, kind.lstrip("-"), blueprint))
    return jobs


def prepared(args):
    """The blueprint, config, provider and built jobs for plan and generate."""
    blueprint = blueprints.load(args.blueprint)
    root = Path(args.blueprint).parent
    config = load_config(root, {"provider": args.provider, "model": args.model, "quality": args.quality, "size": args.size,
                                "max_batch": args.max_batch, "max_cost_usd": args.max_cost})
    key, source = find_key(config["provider"], root)
    provider = PROVIDERS[config["provider"]](config, key)
    usable = {e["id"]: e for e in approved(blueprint, args.blueprint)}
    missing = [name for name in args.with_ref or [] if name not in usable]
    if missing:
        raise RefgenError(f"--with {', '.join(missing)}: only an approved, current reference can be sent as an input")
    built = [build(blueprint, args.blueprint, job, config, provider, args.source, [usable[n] for n in args.with_ref or []], args.note, args.pose)
             for job in jobs_from(args, blueprint, args.blueprint)]
    for item in built:
        item["estimate"], item["estimate_note"] = provider.estimate(item["prompt"], item["images"], item["size"])
    return blueprint, root, config, provider, source, built


def totals(built, config, provider):
    known = [b["estimate"] for b in built if b["estimate"] is not None]
    out = {"provider": provider.name, "model": provider.model, "images": len(built),
           "estimated_cost_usd": round(sum(known), 2) if len(known) == len(built) else None,
           "estimate_note": built[0]["estimate_note"] if built else None}
    if len(built) > config["max_batch"]:
        raise RefgenError(f"{len(built)} images asked for; the limit for one batch is {config['max_batch']}. "
                          "Generate fewer, or raise it on purpose with --max-batch after telling the user the cost.")
    if out["estimated_cost_usd"] is not None and out["estimated_cost_usd"] > config["max_cost_usd"]:
        raise RefgenError(f"estimated cost ${out['estimated_cost_usd']} is above the limit of ${config['max_cost_usd']} for one batch. "
                          "Lower the quality or the count, or raise it on purpose with --max-cost after telling the user.")
    return out


def plan(args):
    blueprint, root, config, provider, source, built = prepared(args)
    if not built:
        found = suggest(blueprint, args.blueprint)
        return {"suggested": found, "note": "nothing was sent. Offer these to the user; generate with the --view and --part flags shown, or --suggested."
                if found else "nothing to suggest: no part is seen from one direction only, too small to judge, or stuck"}
    out = {**totals(built, config, provider), "key": source or f"missing: run `python refgen.py key --provider {provider.name}` in your own terminal",
           "jobs": [{"shows": b["shows"], "from": b["source"], "size": b["size"], "inputs": [i["role"] for i in b["images"]],
                     "estimated_cost_usd": b["estimate"], "prompt": b["prompt"]} for b in built]}
    if getattr(provider, "name", None) == "fake":
        out["note"] = "the fake provider makes placeholders that cannot be approved"
    return out


def new_id(manifest, job):
    stem = "-".join(v for v in ("view" if job["kind"] == "view" else "part", job["part"], job["view"]) if v)
    used = {e["id"] for e in manifest["refs"]}
    return next(f"{stem}-{n}" for n in range(1, 10_000) if f"{stem}-{n}" not in used)


def generate(args):
    from PIL import Image
    blueprint, root, config, provider, source, built = prepared(args)
    if not built:
        raise RefgenError("name what to generate with --view and --part, or pass --suggested (see `refgen.py plan`)")
    summary = totals(built, config, provider)
    folder = refs_dir(args.blueprint)
    state_of = exposure(folder / "refs.json")
    made, failed = [], None
    for item in built:
        manifest = read_manifest(folder)
        ref_id = new_id(manifest, item["job"])
        try:
            data, usage, sent = provider.generate(item["prompt"], [i["data"] for i in item["images"]], item["size"])
            generated = Image.open(io.BytesIO(data))
            generated.load()
        except RefgenError as error:
            failed = {"job": item["shows"], "error": str(error)}
            break
        except OSError as error:
            failed = {"job": item["shows"], "error": f"the reply was not a readable image: {error}"}
            break
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{ref_id}.png").write_bytes(data)
        entry = {
            "id": ref_id, "kind": item["job"]["kind"], "part": item["job"]["part"], "view": item["job"]["view"], "shows": item["shows"],
            "source_view": item["source"], "file": f"{ref_id}.png", "sheet": f"{ref_id}.sheet.png", "sha256": sha(data), "size": list(generated.size),
            "provider": provider.name, "model": provider.model, "parameters": sent, "prompt": item["prompt"], "template": item["template"],
            "inputs": [{k: v for k, v in {**i, "sha256": sha(i["data"])}.items() if k != "data"} for i in item["images"]],
            "target": item["target"], "background": item["background"], "pose": item["pose"],
            "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "usage": usage, "cost_usd": provider.cost(usage), "estimated_cost_usd": item["estimate"], "approved": False,
        }
        if item["job"]["view"]:
            entry["camera"] = {"yaw": VIEWS[item["job"]["view"]][0], "pitch": VIEWS[item["job"]["view"]][1]}
        inputs = [(i["role"], Image.open(io.BytesIO(i["data"]))) for i in item["images"] if i["role"] != "generated reference"]
        entry["checks"] = check(blueprint, entry, generated, inputs[0][1])
        approval_sheet(entry, inputs, generated).save(folder / entry["sheet"])
        manifest["refs"].append(entry)
        write_manifest(folder, manifest)
        made.append({"id": ref_id, "shows": entry["shows"], "sheet": (folder / entry["sheet"]).as_posix(), "cost_usd": entry["cost_usd"],
                     "checks": entry["checks"]["warnings"] or "no automatic check failed"})
    out = {**summary, "made": made, "cost_usd": round(sum(m["cost_usd"] or 0 for m in made), 4) if any(m["cost_usd"] for m in made) else None,
           "next": "show the user each sheet; nothing here is used until `refgen.py approve ID` records that they accepted it"}
    if failed:
        out["failed"] = failed
    if state_of in ("tracked", "untracked-repo"):
        out["warning"] = f"{folder.as_posix()} is not ignored by git; add \"{root.name}/\" to .gitignore"
    return out, 1 if failed else 0


def board(args):
    """Approved views of one pose side by side as a turnaround sheet, with a starter blueprint for it."""
    from PIL import Image
    import masks
    blueprint = blueprints.load(args.blueprint)
    root = Path(args.blueprint).parent
    folder = refs_dir(args.blueprint)
    manifest = read_manifest(folder)
    usable = {e["id"]: e for e in approved(blueprint, args.blueprint, kind="view")}
    missing = [i for i in args.ids if i not in usable]
    if missing:
        raise RefgenError(f"{', '.join(missing)}: a board is built only from approved, current views (see `refgen.py list`)")
    entries = [usable[i] for i in args.ids]
    if len({e["view"] for e in entries}) != len(entries):
        raise RefgenError("each view may appear once on a board")
    poses = {e.get("pose") for e in entries}
    if len(poses) != 1:
        raise RefgenError(f"the views are in different poses ({', '.join(sorted(str(p) for p in poses))}); a board shows one pose")
    height = 1024
    panels = []
    for entry in entries:
        with Image.open(folder / entry["file"]) as image:
            image = image.convert("RGB")
            panels.append(image.resize((round(image.width * height / image.height), height), Image.LANCZOS))
    sheet = Image.new("RGB", (sum(p.width for p in panels), height))
    views, x = [], 0
    for entry, panel in zip(entries, panels):
        sheet.paste(panel, (x, 0))
        keyed, flat = masks.border_key(panel)
        box = masks.fraction_box(keyed)
        if flat < 0.9 or box is None:
            raise RefgenError(f"{entry['id']}: the subject could not be keyed off its background, so its box on the board is unknown")
        frame = [x / sheet.width, 0.0, panel.width / sheet.width, 1.0]
        views.append({"id": entry["view"], "pose": entry["pose"], "frame": [round(v, 4) for v in frame],
                      "subject_box": [round(frame[0] + box[0] * frame[2], 4), round(box[1], 4), round(box[2] * frame[2], 4), round(box[3], 4)],
                      "background": "auto", "camera": {**entry["camera"], "lens": 200}, "generated": entry["id"]})
        x += panel.width
    out = Path(args.out) if args.out else root / "target-turnaround.png"
    sheet.save(out)
    level = [v for v in views if abs(v["camera"]["pitch"]) < 45] or views
    reference = next((v for v in level if abs(abs(v["camera"]["yaw"]) - 90) < 1), level[0])  # a side view foreshortens least
    stub = {"version": 1, "subject": blueprint["subject"], "target": out.name, "design": blueprint.get("design") or blueprint["target"],
            "reference_view": reference["id"], "reference_part": blueprint["reference_part"], "views": views,
            "parts": [{**{k: p[k] for k in ("id", "objects", "same_as", "form") if k in p},
                       **({"colours": [{"name": c["name"], "hex": c["hex"]} for c in p["colours"] if c.get("hex")]} if p.get("colours") else {}),
                       "boxes": {}} for p in blueprint["parts"]],
            "joints": [{"id": j["id"], "parts": j["parts"], "points": {}} for j in blueprint.get("joints") or []],
            "tolerances": blueprint.get("tolerances") or {}}
    stub_path = out.with_name("blueprint-turnaround.json")
    stub_path.write_text(json.dumps(stub, indent=1) + "\n", encoding="utf-8")
    manifest.setdefault("boards", []).append({"file": out.name, "sha256": sha(out.read_bytes()), "refs": [e["id"] for e in entries], "pose": entries[0]["pose"],
                                              "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
    write_manifest(folder, manifest)
    return {"board": out.as_posix(), "blueprint": stub_path.as_posix(), "views": [v["id"] for v in views], "pose": entries[0]["pose"],
            "next": "show the user the board. If they approve it as the new target, fill in every part's boxes and the joints' points in the starter "
                    "blueprint, run `blueprint.py validate --write` and `refgen.py agree` on it, and start the loop from it; the original design "
                    "stays as the final check once the model is posed"}


def agree(args):
    """Where the views of one pose disagree about a part's height or its height above the base, in H."""
    blueprint = blueprints.load(args.blueprint)
    tolerance = blueprints.tolerances(blueprint)
    measures = blueprint.get("measures") or blueprints.derive(blueprint)
    found, compared = [], 0
    for pose in sorted({v["pose"] for v in blueprint["views"]}):
        views = [v["id"] for v in blueprint["views"] if v["pose"] == pose and abs(v["camera"]["pitch"]) < 45]
        for part in blueprint["parts"]:
            seen = {v: measures[v]["parts"][part["id"]] for v in views if part["id"] in measures[v]["parts"]}
            if len(seen) < 2:
                continue
            compared += 1
            for key, name, allowed in (("height", "height", max(tolerance["size"] * max(m["height"] for m in seen.values()), tolerance["size_abs"])),
                                       ("cy", "centre height", tolerance["position"])):
                low, high = min(seen, key=lambda v: seen[v][key]), max(seen, key=lambda v: seen[v][key])
                if seen[high][key] - seen[low][key] > allowed:
                    found.append(f"{part['id']} {name}: {seen[high][key]} H in {high}, {seen[low][key]} H in {low}")
    return {"agree": not found, "parts_compared": compared, "disagreements": found,
            **({"note": "the views show this part at different sizes or heights. Check the boxes first; if they are right, the views themselves "
                        "disagree: regenerate the odd one, or tell the user which view the model will follow"} if found else {})}, 1 if found else 0


def listing(args):
    blueprint = blueprints.load(args.blueprint)
    folder = refs_dir(args.blueprint)
    current = target_sha(blueprint, args.blueprint)
    return [{"id": e["id"], "shows": e["shows"], "state": state(e, folder, current), "provider": e["provider"], "model": e["model"],
             "cost_usd": e.get("cost_usd"), "checks": e["checks"]["warnings"], "sheet": (folder / e["sheet"]).as_posix(),
             **({"rejected": e["rejected"]} if e.get("rejected") else {})} for e in read_manifest(folder)["refs"]]


def decide(args, accept):
    blueprint = blueprints.load(args.blueprint)
    folder = refs_dir(args.blueprint)
    manifest = read_manifest(folder)
    current = target_sha(blueprint, args.blueprint)
    known = {e["id"]: e for e in manifest["refs"]}
    done = []
    for ref_id in args.ids:
        entry = known.get(ref_id)
        if entry is None:
            raise RefgenError(f"no reference {ref_id}; see `refgen.py list`")
        now = state(entry, folder, current)
        if now.startswith("stale"):
            raise RefgenError(f"{ref_id} is {now}; generate it again")
        if accept and entry["provider"] == "fake":
            raise RefgenError(f"{ref_id} is a placeholder from the fake provider and cannot be approved")
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        if accept and args.for_blockout:
            if not (entry.get("approved") and entry.get("boxes")):
                raise RefgenError(f"{ref_id}: approve the image first, then mark its parts with `refgen.py boxes`, then approve --for-blockout")
            entry["blockout"] = {"approved": True, "approved_at": stamp, "approved_by": args.by}
        elif accept:
            entry.update(approved=True, approved_at=stamp, approved_by=args.by)
            entry.pop("rejected", None)
        else:
            entry.update(approved=False, rejected=args.reason.strip(), rejected_at=stamp)
            entry.pop("blockout", None)
        done.append({"id": ref_id, "state": "approved for the blockout" if accept and args.for_blockout else "approved" if accept else "rejected",
                     "checks": entry["checks"]["warnings"]})
    write_manifest(folder, manifest)
    return done


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def options(command, generation=False):
        command.add_argument("--provider", choices=sorted(PROVIDERS), help="Image service (default: from config.json, else openai)")
        if generation:
            command.add_argument("--model", help="Model id (default: from config.json, else the provider's default)")
            command.add_argument("--quality", help="Quality setting passed to the provider (default: high)")
            command.add_argument("--size", help="WIDTHxHEIGHT, or auto to choose from the subject's shape")
            command.add_argument("--max-batch", type=int, help="Most images one generate call may make (default: 4)")
            command.add_argument("--max-cost", type=float, help="Highest estimated cost in USD for one generate call (default: 2.00)")

    command = sub.add_parser("config", help="Show or set the provider, model and options")
    command.add_argument("--root", default=".dream-loop", help="Working folder (default: %(default)s)")
    options(command, generation=True)
    command = sub.add_parser("key", help="Store the provider's API key, typed without echo")
    command.add_argument("--root", default=".dream-loop", help="Working folder (default: %(default)s)")
    options(command)
    command.add_argument("--check", action="store_true", help="Say whether a key is available and where from, without showing it")
    command.add_argument("--remove", action="store_true", help="Delete the stored key")
    for name in ("plan", "generate"):
        command = sub.add_parser(name)
        command.add_argument("--blueprint", default=".dream-loop/blueprint.json")
        command.add_argument("--view", action="append", metavar="VIEW", help=f"Another view of the subject: {', '.join(VIEWS)}; repeatable")
        command.add_argument("--part", action="append", metavar="PART[@VIEW]", help="A close-up of one part, optionally from one of those views; repeatable")
        command.add_argument("--from", dest="source", metavar="VIEW", help="Blueprint view to work from (default: the reference view; for a part, the view showing it largest)")
        command.add_argument("--with", dest="with_ref", action="append", metavar="REF", help="Also send this approved generated reference, to stay consistent with it; repeatable")
        command.add_argument("--note", help="One extra instruction for these images, added to the end of the prompt")
        command.add_argument("--pose", choices=["design", "neutral"], default="design",
                             help="For views: keep the design's own pose (default), or a symmetric modelling stance to model and rig from")
        if name == "generate":
            command.add_argument("--suggested", action="store_true", help="Also generate everything `plan` suggests")
        options(command, generation=True)
    command = sub.add_parser("list")
    command.add_argument("--blueprint", default=".dream-loop/blueprint.json")
    for name in ("approve", "reject"):
        command = sub.add_parser(name)
        command.add_argument("--blueprint", default=".dream-loop/blueprint.json")
        command.add_argument("ids", nargs="+", metavar="ID")
        if name == "approve":
            command.add_argument("--by", default="user", help="Who accepted it (default: %(default)s)")
            command.add_argument("--for-blockout", action="store_true",
                                 help="The user has also checked the part boxes on this view (refgen.py boxes) and accepts it setting the blockout's unseen extents")
        else:
            command.add_argument("--reason", required=True, help="What is wrong with it")
    command = sub.add_parser("board")
    command.add_argument("--blueprint", default=".dream-loop/blueprint.json")
    command.add_argument("ids", nargs="+", metavar="ID", help="Approved views of one pose, in the order to lay them out")
    command.add_argument("--out", help="Board image to write (default: target-turnaround.png beside the blueprint)")
    command = sub.add_parser("agree")
    command.add_argument("--blueprint", default=".dream-loop/blueprint.json")
    command = sub.add_parser("boxes")
    command.add_argument("--blueprint", default=".dream-loop/blueprint.json")
    command.add_argument("id", metavar="ID")
    command.add_argument("--box", action="append", required=True, metavar="PART=x,y,w,h", help="A part's tight box on the generated image, as fractions; repeatable")
    command.add_argument("--subject", metavar="x,y,w,h", help="The whole subject's box (default: keyed off the flat background)")
    args = parser.parse_args(argv)
    status = 0
    try:
        if args.command == "config":
            changes = {k: v for k, v in {"provider": args.provider, "model": args.model, "quality": args.quality, "size": args.size,
                                         "max_batch": args.max_batch, "max_cost_usd": args.max_cost}.items() if v is not None}
            if changes:
                save_config(args.root, changes)
            config = load_config(args.root)
            provider = PROVIDERS[config["provider"]](config)
            _, source = find_key(config["provider"], args.root)
            result = {"generation": {**config, "model": provider.model}, "key": source or "missing", "models": provider.models,
                      "providers": sorted(PROVIDERS)}
            if (config.get("model") or provider.model) != provider.model:
                result["note"] = f"{config['model']} is not a model id; using {provider.model}"
        elif args.command == "key":
            provider = load_config(args.root, {"provider": args.provider})["provider"]
            if args.check:
                _, source = find_key(provider, args.root)
                result = {"provider": provider, "key": source or "missing"}
                status = 0 if source else 1
            elif args.remove:
                kept = {k: v for k, v in read_secrets(args.root).items() if k != PROVIDERS[provider].key_env}
                write_secrets(args.root, kept)
                result = {"provider": provider, "removed": (Path(args.root) / SECRETS).as_posix()}
            else:
                result = store_key(provider, args.root)
        elif args.command == "plan":
            result = plan(args)
        elif args.command == "generate":
            result, status = generate(args)
        elif args.command == "list":
            result = listing(args)
        elif args.command == "boxes":
            result = mark_boxes(args)
        elif args.command == "board":
            result = board(args)
        elif args.command == "agree":
            result, status = agree(args)
        else:
            result = decide(args, args.command == "approve")
    except (RefgenError, blueprints.BlueprintError, OSError, ValueError, KeyError) as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return status


if __name__ == "__main__":
    sys.exit(main())
