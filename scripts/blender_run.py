#!/usr/bin/env python3
"""Run Dream Loop's Blender helper: ID-mask renders, camera sweeps and blockouts.

  python blender_run.py render   --blueprint .dream-loop/blueprint.json --out .dream-loop/inner  WHERE
  python blender_run.py sweep    --blueprint .dream-loop/blueprint.json --view side --out .dream-loop/sweep  WHERE
  python blender_run.py blockout --spec .dream-loop/blockout.json --height 1.75  WHERE | --save new.blend
  python blender_run.py info  WHERE

WHERE is one of:
  --blend scene.blend [--setup build.py]   headless: opens the file (and runs build.py first, for a
                                           model made by a script; --setup alone starts from an empty scene)
  --socket [HOST:PORT]                     the open Blender session, through the blender-mcp add-on
                                           (default 127.0.0.1:9876); nothing is saved or reloaded

render  writes, per view: <view>.ids.png (each part in a flat colour), <view>.ids.json (camera, per-part
        pixel counts, projected boxes, visibility and content hashes) and <view>.png (a picture).
        Cameras come from --cameras (cameras.json written by viewmatch.py) or the blueprint's estimates.
sweep   renders one view's silhouette over a window of cameras round the estimate, for viewmatch.py --sweep.

Prints the helper's result as one JSON line. Exit status 1 means Blender reported an error.
No dependencies beyond blueprint.py.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import blueprint as blueprints  # noqa: E402

HELPER = Path(__file__).parent / "blender" / "dream_loop_bpy.py"
MARKER = "DREAM_LOOP_RESULT "
LENSES = (35.0, 50.0, 85.0, 135.0)


class BlenderError(RuntimeError):
    pass


def find_blender(explicit=None):
    """The Blender executable: --blender, $BLENDER, PATH, then the usual install folders (newest first)."""
    for candidate in (explicit, os.environ.get("BLENDER"), shutil.which("blender")):
        if candidate and Path(candidate).exists():
            return str(candidate)
    roots = [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Blender Foundation", Path("/Applications")]
    found = [p for root in roots if root.exists() for p in list(root.glob("Blender*/blender.exe")) + list(root.glob("Blender*.app/Contents/MacOS/Blender"))]
    if not found:
        raise BlenderError("Blender not found; pass --blender or set BLENDER to its executable")
    return str(sorted(found, key=lambda p: [int(n) for n in "".join(c if c.isdigit() else " " for c in str(p)).split()])[-1])


def parse_result(text):
    lines = [line for line in text.splitlines() if line.startswith(MARKER)]
    if not lines:
        raise BlenderError("Blender gave no result. Output ended:\n" + "\n".join(text.splitlines()[-15:]))
    return json.loads(lines[-1][len(MARKER):])


def run_headless(request, blend=None, setup=None, blender=None, timeout=1800):
    with tempfile.TemporaryDirectory() as tmp:
        request_file = Path(tmp) / "request.json"
        request_file.write_text(json.dumps(request), encoding="utf-8")
        command = [find_blender(blender), "--background"]
        command += [str(Path(blend).resolve())] if blend else ["--factory-startup"]
        if setup:
            command += ["--python", str(Path(setup).resolve())]
        command += ["--python-exit-code", "1", "--python", str(HELPER), "--", str(request_file)]
        done = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=timeout)
    return parse_result(done.stdout + "\n" + done.stderr)


def socket_code(request):
    """The helper plus one call, as a single script for the add-on to exec; what it prints comes back as the result."""
    return HELPER.read_text(encoding="utf-8") + f"\nprint({MARKER!r} + json.dumps(run(json.loads({json.dumps(request)!r}))))\n"


def run_socket(request, address="127.0.0.1:9876", timeout=600):
    """Send the helper and the request to the blender-mcp add-on as one execute_code command."""
    host, _, port = address.rpartition(":")
    message = json.dumps({"type": "execute_code", "params": {"code": socket_code(request)}}).encode("utf-8")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=10) as connection:
            connection.settimeout(timeout)
            connection.sendall(message)
            buffer = b""
            while True:  # the add-on sends one JSON object with no terminator: read until it parses
                chunk = connection.recv(65536)
                if not chunk:
                    raise BlenderError("Blender closed the connection before replying")
                buffer += chunk
                try:
                    reply = json.loads(buffer.decode("utf-8"))
                    break
                except ValueError:
                    continue
    except OSError as error:
        raise BlenderError(f"cannot reach the blender-mcp add-on at {address}: {error}. "
                           "Start it in Blender (sidebar > BlenderMCP > Connect) or use --blend.")
    if reply.get("status") != "success":
        raise BlenderError(f"Blender refused the code: {reply.get('message', reply)}")
    return parse_result(str(reply.get("result", {}).get("result", "")))


def run(request, args):
    for key in ("out", "save"):  # Blender's working directory is not ours
        if request.get(key):
            request[key] = str(Path(request[key]).resolve())
    if args.socket:
        result = run_socket(request, args.socket)
    elif args.blend or args.setup or (request["command"] == "blockout" and request.get("save")):
        request["clear"] = not (args.blend or args.setup)
        result = run_headless(request, args.blend, args.setup, args.blender)
    else:
        raise BlenderError("say where to run: --blend FILE, --setup SCRIPT or --socket")
    if "error" in result:
        raise BlenderError(result["error"] + ("\n" + result["traceback"] if result.get("traceback") else ""))
    return result


def sweep_window(blueprint, view_id, steps=5):
    """Offsets to try round a view's camera estimate, inside the blueprint's camera tolerances."""
    tolerance = blueprints.tolerances(blueprint)
    estimate = next(v["camera"] for v in blueprint["views"] if v["id"] == view_id)

    def spread(limit, count):
        return [round(limit * (2 * i / (count - 1) - 1), 3) for i in range(count)] if limit and count > 1 else [0.0]

    lenses = sorted({float(estimate["lens"]), *(lens for lens in LENSES if 0.5 <= lens / estimate["lens"] <= 2)})
    return {"yaws": spread(tolerance["camera_yaw"], steps), "pitches": spread(tolerance["camera_pitch"], 3), "lenses": lenses}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["render", "sweep", "blockout", "info"])
    parser.add_argument("--blueprint", help="blueprint.json (render, sweep)")
    parser.add_argument("--out", help="Folder for the renders (render, sweep)")
    parser.add_argument("--views", help="Comma-separated view ids to render (default: all)")
    parser.add_argument("--view", help="View id to sweep")
    parser.add_argument("--cameras", help="cameras.json with solved cameras; views without one use the blueprint estimate")
    parser.add_argument("--long-edge", type=int, help="Render size, longest edge in pixels (default: 1024; sweep 384)")
    parser.add_argument("--beauty", choices=["preview", "scene", "none"], default="preview",
                        help="Picture to render beside the mask: preview (fast, material colours), scene (the scene's own engine), none")
    parser.add_argument("--spec", help="Blockout spec from blueprint.py blockout")
    parser.add_argument("--height", type=float, default=1.0, help="Subject height in scene units for a blockout (default: %(default)s)")
    parser.add_argument("--save", help="Save the scene here after building a blockout (headless)")
    parser.add_argument("--blend", help="Scene file to open headless")
    parser.add_argument("--setup", help="Python script to run in Blender first, to build the model")
    parser.add_argument("--socket", nargs="?", const="127.0.0.1:9876", help="Use the open Blender session through the blender-mcp add-on")
    parser.add_argument("--blender", help="Blender executable")
    args = parser.parse_args(argv)
    try:
        request = {"command": args.command}
        if args.command in ("render", "sweep"):
            if not (args.blueprint and args.out):
                parser.error(f"{args.command} needs --blueprint and --out")
            request.update(blueprint=blueprints.load(args.blueprint), out=args.out)
            if args.long_edge:
                request["long_edge"] = args.long_edge
        if args.command == "render":
            request["beauty"] = args.beauty
            if args.views:
                request["views"] = args.views.split(",")
            if args.cameras and Path(args.cameras).exists():
                request["cameras"] = json.loads(Path(args.cameras).read_text(encoding="utf-8"))
        elif args.command == "sweep":
            if not args.view:
                parser.error("sweep needs --view")
            request.update(view=args.view, **sweep_window(request["blueprint"], args.view))
        elif args.command == "blockout":
            if not args.spec:
                parser.error("blockout needs --spec")
            request.update(spec=json.loads(Path(args.spec).read_text(encoding="utf-8")), height=args.height, save=args.save)
        print(json.dumps(run(request, args), separators=(",", ":")))
    except (BlenderError, blueprints.BlueprintError, OSError, ValueError, StopIteration, subprocess.TimeoutExpired) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
