"""Tests for blender_run.py and the Blender helper. Run: python -m unittest scripts/blender_run_test.py

The protocol tests need nothing. The render tests start Blender and are skipped when it is not installed.
"""
import contextlib
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import blender_run  # noqa: E402
import blueprint  # noqa: E402
from blueprint_test import sample  # noqa: E402

try:
    BLENDER = blender_run.find_blender()
except blender_run.BlenderError:
    BLENDER = None


def complete(data=None):
    data = data or sample()
    data["parts"][0]["colours"][0]["hex"] = "#b42828"
    data["measures"] = blueprint.derive(data)
    return data


class FakeAddon:
    """Stands in for the blender-mcp add-on: reads one JSON command, replies with one JSON object in two pieces."""

    def __init__(self, reply):
        self.reply, self.received = reply, None
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.address = "127.0.0.1:%d" % self.server.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        client, _ = self.server.accept()
        buffer = b""
        while True:
            buffer += client.recv(65536)
            try:
                self.received = json.loads(buffer.decode("utf-8"))
                break
            except ValueError:
                continue
        data = json.dumps(self.reply).encode("utf-8")
        client.sendall(data[:7])
        client.sendall(data[7:])
        client.close()
        self.server.close()


class ProtocolTest(unittest.TestCase):
    def test_socket_sends_helper_and_request_and_reads_split_reply(self):
        addon = FakeAddon({"status": "success", "result": {"executed": True, "result": "noise\nDREAM_LOOP_RESULT {\"blender\": \"5.3\"}\n"}})
        result = blender_run.run_socket({"command": "info", "note": "it's \"quoted\""}, addon.address)
        self.assertEqual(result, {"blender": "5.3"})
        self.assertEqual(addon.received["type"], "execute_code")
        code = addon.received["params"]["code"]
        compile(code, "sent", "exec")  # what Blender will exec must at least be valid Python
        self.assertIn("def command_render", code)
        self.assertTrue('"note"' in code)

    def test_socket_error_reply_is_reported(self):
        addon = FakeAddon({"status": "error", "message": "NameError: boom"})
        with self.assertRaisesRegex(blender_run.BlenderError, "boom"):
            blender_run.run_socket({"command": "info"}, addon.address)

    def test_no_listener_is_explained(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaisesRegex(blender_run.BlenderError, "cannot reach"):
            blender_run.run_socket({"command": "info"}, f"127.0.0.1:{port}")

    def test_missing_result_line_shows_output_tail(self):
        with self.assertRaisesRegex(blender_run.BlenderError, "Traceback"):
            blender_run.parse_result("Blender 5.3\nTraceback (most recent call last)\n")

    def test_sweep_window_stays_inside_the_tolerances(self):
        data = complete()
        data["tolerances"] = {"camera_yaw": 10, "camera_pitch": 4}
        window = blender_run.sweep_window(data, "side")
        self.assertEqual(window["yaws"], [-10.0, -5.0, 0.0, 5.0, 10.0])
        self.assertEqual(window["pitches"], [-4.0, 0.0, 4.0])
        self.assertIn(85.0, window["lenses"])
        self.assertNotIn(35.0, window["lenses"])  # more than twice as wide as the 85 mm estimate

    def test_where_to_run_is_required(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status = blender_run.main(["info"])
        self.assertEqual(status, 1)
        self.assertIn("--blend", err.getvalue())


@unittest.skipUnless(BLENDER, "Blender is not installed")
class BlenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.dir = Path(cls.tmp.name)
        cls.blueprint = sample()
        cls.blueprint["parts"][1]["boxes"]["side"] = [0.2, 0.3, 0.1, 0.6]  # the body is thinner from the side
        cls.blueprint = complete(cls.blueprint)
        (cls.dir / "blueprint.json").write_text(json.dumps(cls.blueprint), encoding="utf-8")
        (cls.dir / "blockout.json").write_text(json.dumps(blueprint.blockout(cls.blueprint)), encoding="utf-8")
        cls.blend = cls.dir / "model.blend"
        cls.built = cls.call("blockout", "--spec", str(cls.dir / "blockout.json"), "--height", "2", "--save", str(cls.blend))
        cls.rendered = cls.call("render", "--blueprint", str(cls.dir / "blueprint.json"), "--out", str(cls.dir / "r"),
                                "--blend", str(cls.blend), "--long-edge", "512")

    @classmethod
    def call(cls, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = blender_run.main(list(argv))
        if status:
            raise AssertionError(err.getvalue())
        return json.loads(out.getvalue())

    def ids(self, view):
        return json.loads((self.dir / "r" / f"{view}.ids.json").read_text(encoding="utf-8"))

    def test_blockout_builds_one_tagged_object_per_part(self):
        self.assertEqual(self.built["objects"], ["BO_head", "BO_body"])
        self.assertTrue(self.blend.exists())

    def test_render_reports_every_view_with_nothing_unassigned(self):
        self.assertEqual(sorted(self.rendered["views"]), ["front", "side"])
        for view in self.rendered["views"].values():
            self.assertEqual((view["unassigned"], view["missing"]), ([], []))

    def test_mask_colours_are_exact_and_match_the_counts(self):
        data = self.ids("side")
        with Image.open(self.dir / "r" / "side.ids.png") as image:
            self.assertEqual(image.size, tuple(data["resolution"]))
            colours = {colour[:3]: count for count, colour in image.convert("RGBA").getcolors(1 << 20) if colour[3] > 127}
        for part in data["parts"].values():
            near = sum(count for colour, count in colours.items() if max(abs(a - b) for a, b in zip(colour, part["colour"])) <= 12)
            self.assertEqual(near, part["visible_px"])
            self.assertGreater(near, 500)
        self.assertEqual(sum(colours.values()), sum(p["visible_px"] for p in data["parts"].values()))  # no blended edge pixels

    def test_projected_boxes_follow_the_blueprint_proportions(self):
        for view, width in (("side", 0.25), ("front", 0.375)):
            data = self.ids(view)
            head, body = data["parts"]["head"]["projected_box"], data["parts"]["body"]["projected_box"]
            pixels = data["resolution"]
            total = (body[1] + body[3] - head[1]) * pixels[1]
            self.assertAlmostEqual(head[3] * pixels[1] / total, 0.25, delta=0.02)
            self.assertAlmostEqual(body[2] * pixels[0] / total, width, delta=0.03)

    def test_camera_is_recorded_for_reuse(self):
        camera = self.ids("side")["camera"]
        self.assertEqual((camera["yaw"], camera["pitch"], camera["lens"]), (90, 0, 85))
        self.assertGreater(camera["location"][0], 2)  # yaw 90: the camera stands on +X
        self.assertAlmostEqual(camera["subject_height"], 2.0, places=3)
        self.assertAlmostEqual(camera["distance_H"] * 2.0, camera["distance"], places=2)

    def test_visibility_and_hash(self):
        parts = self.ids("front")["parts"]
        self.assertGreater(parts["head"]["visible"], 0.95)
        self.assertEqual(len(parts["head"]["hash"]), 16)
        self.assertNotEqual(parts["head"]["hash"], parts["body"]["hash"])
        again = self.call("render", "--blueprint", str(self.dir / "blueprint.json"), "--out", str(self.dir / "r2"),
                          "--blend", str(self.blend), "--long-edge", "256", "--views", "front", "--beauty", "none")
        self.assertEqual(list(again["views"]), ["front"])
        second = json.loads((self.dir / "r2" / "front.ids.json").read_text(encoding="utf-8"))
        self.assertEqual(second["parts"]["head"]["hash"], parts["head"]["hash"])  # same model, other size: same hash
        self.assertFalse((self.dir / "r2" / "front.png").exists())

    def test_picture_is_written(self):
        with Image.open(self.dir / "r" / "side.png") as image:
            self.assertEqual(image.size, tuple(self.ids("side")["resolution"]))

    def test_sweep_renders_a_window_of_candidates(self):
        result = self.call("sweep", "--blueprint", str(self.dir / "blueprint.json"), "--view", "side", "--out", str(self.dir / "s"),
                           "--blend", str(self.blend), "--long-edge", "128")
        listing = json.loads(Path(result["file"]).read_text(encoding="utf-8"))
        self.assertEqual(result["candidates"], len(listing["candidates"]))
        yaws = {c["camera"]["yaw"] for c in listing["candidates"]}
        self.assertEqual((min(yaws), max(yaws)), (70, 110))
        self.assertTrue(all((self.dir / "s" / c["file"]).exists() for c in listing["candidates"]))

    def test_code_sent_to_the_addon_runs_the_way_the_addon_runs_it(self):
        # The blender-mcp add-on execs the code with only bpy in scope and returns what it printed.
        request = {"command": "render", "blueprint": self.blueprint, "out": str(self.dir / "exec"), "views": ["front"],
                   "long_edge": 128, "beauty": "none"}
        (self.dir / "sent.py").write_text(blender_run.socket_code(request), encoding="utf-8")
        (self.dir / "as_addon.py").write_text(
            "import bpy, io, sys\nfrom contextlib import redirect_stdout\n"
            "code = open(sys.argv[-1], encoding='utf-8').read()\nbuffer = io.StringIO()\n"
            "with redirect_stdout(buffer):\n    exec(code, {'bpy': bpy})\nprint(buffer.getvalue())\n", encoding="utf-8")
        done = subprocess.run([BLENDER, "--background", str(self.blend), "--python", str(self.dir / "as_addon.py"), "--", str(self.dir / "sent.py")],
                              capture_output=True, text=True, errors="replace", timeout=300)
        result = blender_run.parse_result(done.stdout)
        self.assertEqual(result["views"]["front"]["missing"], [], result)
        self.assertTrue((self.dir / "exec" / "front.ids.png").exists())

    def test_blender_error_is_reported(self):
        data = complete()
        data["views"][0]["scene"] = {"collection": "Pose_Missing"}
        (self.dir / "bad.json").write_text(json.dumps(data), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            status = blender_run.main(["render", "--blueprint", str(self.dir / "bad.json"), "--out", str(self.dir / "bad"),
                                       "--blend", str(self.blend)])
        self.assertEqual(status, 1)
        self.assertIn("no collection named", err.getvalue())


if __name__ == "__main__":
    unittest.main()
