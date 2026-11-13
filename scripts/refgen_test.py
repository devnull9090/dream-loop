"""Tests for refgen.py. No network and no real key: the HTTP layer is replaced. Run: python -m unittest scripts/refgen_test.py"""
import base64
import contextlib
import getpass
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import refgen  # noqa: E402
from blueprint_test import sheet  # noqa: E402
from viewmatch_test import complete  # noqa: E402

KEY = "sk-test-SENTINEL-0123456789"
USAGE = {"input_tokens": 3100, "output_tokens": 6000, "total_tokens": 9100, "input_tokens_details": {"text_tokens": 600, "image_tokens": 2500}}


def picture(head=(180, 40, 40), body=(40, 80, 160), ground=(200, 200, 200), size=(512, 768), extra=None):
    """What a provider might send back: the figure from the front on a flat ground."""
    image = Image.new("RGB", size, ground)
    draw = ImageDraw.Draw(image)
    draw.ellipse((206, 90, 306, 220), fill=head)
    draw.rectangle((176, 220, 336, 660), fill=body)
    if extra:
        draw.rectangle((176, 400, 336, 660), fill=extra)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def reply(image=None, usage=USAGE):
    return 200, json.dumps({"created": 1, "data": [{"b64_json": base64.b64encode(image or picture()).decode()}], "usage": usage}).encode()


class RefgenCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.root = self.project / ".dream-loop"
        self.root.mkdir()
        sheet().save(self.root / "target.png")
        self.blueprint = self.root / "blueprint.json"
        self.blueprint.write_text(json.dumps(complete()), encoding="utf-8")
        self.refs = self.root / "refs"
        self.calls = []
        self.replies = []
        patches = [mock.patch.object(refgen, "http_post", self.post), mock.patch.dict(os.environ, {"OPENAI_API_KEY": KEY})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def post(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body), "timeout": timeout})
        answer = self.replies.pop(0) if self.replies else reply()
        if isinstance(answer, Exception):
            raise answer
        return answer

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = refgen.main(list(argv))
        self.output = out.getvalue() + err.getvalue()
        return status, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()

    def bp(self, command, *argv):
        return self.run_cli(command, "--blueprint", str(self.blueprint), *argv)

    def manifest(self):
        return json.loads((self.refs / "refs.json").read_text(encoding="utf-8"))["refs"]


class KeyTest(RefgenCase):
    def git(self, *args):
        subprocess.run(["git", "-C", str(self.project), *args], check=True, capture_output=True)

    def typed(self, value=KEY, tty=True):
        return (mock.patch.object(refgen.sys, "stdin", mock.Mock(isatty=lambda: tty)), mock.patch.object(getpass, "getpass", return_value=value))

    def store(self, value=KEY, tty=True):
        a, b = self.typed(value, tty)
        with a, b as asked, mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            status, result, err = self.run_cli("key", "--root", str(self.root), "--provider", "openai")
            return status, result, err, asked

    def test_key_is_read_hidden_stored_in_an_ignored_file_and_never_printed(self):
        self.git("init", "-q")
        (self.project / ".gitignore").write_text(".dream-loop/\n", encoding="utf-8")
        status, result, err, asked = self.store()
        self.assertEqual((status, err), (0, ""))
        asked.assert_called_once()
        self.assertEqual((self.root / "secrets.env").read_text(encoding="utf-8").splitlines()[-1], f"OPENAI_API_KEY={KEY}")
        self.assertNotIn(KEY, self.output)
        self.assertEqual(result["git"], "ignored")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(refgen.find_key("openai", self.root), (KEY, (self.root / "secrets.env").as_posix()))
            status, result, _ = self.run_cli("key", "--root", str(self.root), "--check")
            self.assertEqual(status, 0)
            self.assertNotIn(KEY, self.output)
            self.run_cli("key", "--root", str(self.root), "--remove")
            self.assertEqual(self.run_cli("key", "--root", str(self.root), "--check")[0], 1)
            self.assertNotIn(KEY, (self.root / "secrets.env").read_text(encoding="utf-8"))

    def test_key_is_refused_when_git_could_commit_the_file(self):
        self.git("init", "-q")
        status, result, err, asked = self.store()
        self.assertEqual((status, result), (2, None))
        self.assertIn("not ignored by git", err)
        asked.assert_not_called()  # refused before anything was typed
        self.assertFalse((self.root / "secrets.env").exists())

    def test_key_is_refused_when_the_terminal_cannot_hide_it(self):
        status, _, err, asked = self.store(tty=False)
        self.assertEqual(status, 2)
        self.assertIn("not an interactive terminal", err)
        asked.assert_not_called()
        a, _ = self.typed()
        with a, mock.patch.object(getpass, "getpass", side_effect=getpass.GetPassWarning("echo")), mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            status, _, err = self.run_cli("key", "--root", str(self.root))
        self.assertEqual(status, 2)
        self.assertIn("cannot hide", err)
        self.assertFalse((self.root / "secrets.env").exists())

    def test_key_outside_git_is_stored_and_the_environment_wins(self):
        status, result, _, _ = self.store("sk-from-file")
        self.assertEqual(status, 0)
        self.assertIn("not in a git repository", result["git"])
        self.assertEqual(refgen.find_key("openai", self.root), (KEY, "environment variable OPENAI_API_KEY"))
        self.assertEqual(self.store("has space")[0], 2)

    def test_key_has_no_command_line_form(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            refgen.main(["key", "--root", str(self.root), KEY])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            refgen.main(["key", "--root", str(self.root), "--key", KEY])


class PromptTest(RefgenCase):
    def plan(self, *argv):
        status, result, err = self.bp("plan", *argv)
        self.assertEqual((status, err), (0, ""))
        return result

    def test_view_prompt_names_every_input_and_forbids_changing_the_design(self):
        job = self.plan("--view", "front")["jobs"][0]
        prompt = job["prompt"]
        self.assertEqual((job["inputs"], job["from"], job["size"]), (["design", "design"], "side", "1024x1536"))
        self.assertTrue(prompt.startswith("Image 1 is the approved design of test figure, seen from its own left side."))
        self.assertIn("Image 2 is the same approved design, seen from directly in front.", prompt)
        for needed in ("a straight-on front view", "Turn the camera; do not touch the design", "Add no part, remove none",
                       "head: skin #b42828; body: cloth #2850a0", "Orthographic projection", "flat, uniform", "No text, labels",
                       "No other characters", "Invent no new feature", "the standing pose"):
            self.assertIn(needed, prompt)
        self.assertNotIn("{", prompt)
        self.assertNotIn("concept art", prompt.lower())
        self.assertEqual(self.calls, [])  # plan sends nothing

    def test_part_prompt_carries_the_form_note_colours_neighbours_and_the_crop(self):
        job = self.plan("--part", "head", "--note", "the visor is one piece")["jobs"][0]
        prompt = job["prompt"]
        self.assertEqual(job["inputs"], ["design", "design", "part crop"])
        self.assertIn("Image 3 is an enlarged crop of image 1 showing the head", prompt)
        for needed in ("Show only the head of this same test figure", "A plain sphere sitting on the body.", "skin #b42828",
                       "It joins the body at the neck.", "Resolve; do not invent", "Do not turn the part or the camera",
                       "Additional instruction from the designer: the visor is one piece"):
            self.assertIn(needed, prompt)
        turned = self.plan("--part", "head@back")["jobs"][0]
        self.assertIn("seen in a straight-on back view", turned["prompt"])

    def test_a_project_template_replaces_the_skills_own_and_bad_placeholders_are_reported(self):
        folder = self.root / "prompts"
        folder.mkdir()
        (folder / "view.md").write_text("notes\n\n## Prompt\n\n{inputs}\nONLY {view_name} of {subject}.\n", encoding="utf-8")
        self.assertTrue(self.plan("--view", "top")["jobs"][0]["prompt"].endswith("ONLY top of test figure."))
        (folder / "view.md").write_text("## Prompt\n{nonsense}\n", encoding="utf-8")
        status, _, err = self.bp("plan", "--view", "top")
        self.assertEqual(status, 2)
        self.assertIn("unknown placeholder {nonsense}", err)

    def test_plan_without_jobs_suggests_views_for_parts_seen_from_one_side_and_close_ups_for_small_parts(self):
        suggested = self.plan()["suggested"]
        self.assertEqual([s["job"] for s in suggested], ["--part head"])  # side and front are both on the sheet
        self.assertIn("80 px", suggested[0]["why"])
        data = complete()
        data["views"] = data["views"][:1]
        data["views"][0]["camera"]["yaw"] = 40
        for part in data["parts"]:
            part["boxes"].pop("front")
        data["joints"] = []
        data["measures"] = refgen.blueprints.derive(data)
        self.blueprint.write_text(json.dumps(data), encoding="utf-8")
        suggested = self.plan()["suggested"]
        self.assertEqual([s["job"] for s in suggested], ["--view front", "--view left", "--view right", "--view back", "--part head"])
        self.assertIn("seen from one direction only (head, body)", suggested[0]["why"])

    def test_unknown_names_are_refused(self):
        for argv in (["--view", "underneath"], ["--part", "tail"], ["--part", "head@sideways"], ["--view", "front", "--from", "nowhere"],
                     ["--view", "front", "--with", "view-back-1"]):
            self.assertEqual(self.bp("plan", *argv)[0], 2, argv)


class GenerateTest(RefgenCase):
    def test_generate_sends_the_design_and_records_everything_but_the_key(self):
        status, result, err = self.bp("generate", "--view", "front", "--part", "head")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual([m["id"] for m in result["made"]], ["view-front-1", "part-head-1"])
        first, second = self.calls
        self.assertEqual(first["url"], "https://api.openai.com/v1/images/edits")
        self.assertEqual(first["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual({k: v for k, v in first["body"].items() if k not in ("prompt", "images")},
                         {"model": "gpt-image-2.6-sunburst", "size": "1024x1536", "quality": "high"})
        self.assertEqual(len(first["body"]["images"]), 2)
        self.assertEqual(len(second["body"]["images"]), 3)
        sent = base64.b64decode(first["body"]["images"][0]["image_url"].split(",", 1)[1])
        with Image.open(io.BytesIO(sent)) as image:
            self.assertEqual(image.size, (400, 400))  # the side view's frame, not the whole sheet
        entry = self.manifest()[0]
        self.assertEqual((entry["provider"], entry["model"], entry["approved"], entry["kind"], entry["view"]), ("openai", "gpt-image-2.6-sunburst", False, "view", "front"))
        self.assertEqual(entry["prompt"], first["body"]["prompt"])
        self.assertEqual(entry["inputs"][0]["sha256"], refgen.sha(sent))
        self.assertEqual(entry["target"]["sha256"], refgen.sha((self.root / "target.png").read_bytes()))
        self.assertEqual(entry["parameters"], {"model": "gpt-image-2.6-sunburst", "size": "1024x1536", "quality": "high"})
        self.assertEqual(entry["usage"], USAGE)
        self.assertEqual(entry["cost_usd"], round((600 * 5 + 2500 * 8 + 6000 * 30) / 1e6, 4))
        self.assertTrue(entry["created"].startswith("20"))
        self.assertEqual(entry["checks"]["warnings"], [])
        self.assertTrue((self.refs / "view-front-1.png").exists() and (self.refs / "view-front-1.sheet.png").exists())
        self.assertNotIn(KEY, self.output)
        for file in self.root.rglob("*"):
            if file.is_file():
                self.assertNotIn(KEY.encode(), file.read_bytes(), file)

    def test_nothing_is_usable_until_approved_and_approval_lapses_when_anything_changes(self):
        self.bp("generate", "--view", "front", "--part", "head")
        blueprint = refgen.blueprints.load(self.blueprint)
        self.assertEqual(refgen.approved(blueprint, self.blueprint), [])
        status, result, _ = self.bp("approve", "part-head-1")
        self.assertEqual((status, result[0]["state"]), (0, "approved"))
        self.assertEqual([e["id"] for e in refgen.approved(blueprint, self.blueprint)], ["part-head-1"])
        self.assertEqual([e["id"] for e in refgen.approved(blueprint, self.blueprint, part="head")], ["part-head-1"])
        self.assertEqual(refgen.approved(blueprint, self.blueprint, part="body"), [])
        self.assertEqual(refgen.approved(blueprint, self.blueprint, kind="view"), [])
        self.bp("reject", "view-front-1", "--reason", "two heads")
        states = {e["id"]: e["state"] for e in self.bp("list")[1]}
        self.assertEqual(states, {"view-front-1": "rejected", "part-head-1": "approved"})
        (self.refs / "part-head-1.png").write_bytes(picture(head=(10, 200, 10)))  # edited after approval
        self.assertEqual(refgen.approved(blueprint, self.blueprint), [])
        self.assertIn("stale", self.bp("list")[1][1]["state"])
        self.assertEqual(self.bp("approve", "part-head-1")[0], 2)

    def test_a_changed_design_makes_every_reference_stale(self):
        self.bp("generate", "--view", "back")
        self.bp("approve", "view-back-1")
        blueprint = refgen.blueprints.load(self.blueprint)
        self.assertEqual(len(refgen.approved(blueprint, self.blueprint)), 1)
        changed = sheet()
        ImageDraw.Draw(changed).rectangle((0, 0, 20, 20), fill=(0, 0, 0))
        changed.save(self.root / "target.png")
        self.assertEqual(refgen.approved(blueprint, self.blueprint), [])
        self.assertIn("approved design changed", self.bp("list")[1][0]["state"])

    def test_an_approved_reference_can_be_sent_along_and_is_ranked_below_the_design(self):
        self.bp("generate", "--view", "front")
        self.assertEqual(self.bp("generate", "--view", "back", "--with", "view-front-1")[0], 2)  # not approved yet
        self.bp("approve", "view-front-1")
        self.assertEqual(self.bp("generate", "--view", "back", "--with", "view-front-1")[0], 0)
        body = self.calls[-1]["body"]
        self.assertEqual(len(body["images"]), 3)
        self.assertIn("Image 3 is an additional reference of the same subject (front view)", body["prompt"])
        self.assertIn("wherever it differs from image 1 in any way, image 1 is right", body["prompt"])
        self.assertEqual(self.manifest()[-1]["inputs"][2]["ref"], "view-front-1")

    def test_an_api_error_is_reported_once_without_the_key_and_keeps_what_was_made(self):
        self.replies = [reply(), (401, json.dumps({"error": {"message": f"Incorrect API key provided: {KEY}"}}).encode())]
        status, result, _ = self.bp("generate", "--view", "front", "--view", "back", "--view", "left")
        self.assertEqual(status, 1)
        self.assertEqual([m["id"] for m in result["made"]], ["view-front-1"])
        self.assertIn("HTTP 401", result["failed"]["error"])
        self.assertNotIn(KEY, self.output)
        self.assertEqual(len(self.calls), 2)  # the failed call was not retried and the batch stopped
        self.assertEqual(len(self.manifest()), 1)

    def test_a_timeout_is_not_retried_and_says_it_may_have_been_charged(self):
        self.replies = [TimeoutError("timed out")]
        status, result, _ = self.bp("generate", "--view", "front")
        self.assertEqual((status, len(self.calls), result["made"]), (1, 1, []))
        self.assertIn("may still have been accepted and charged", result["failed"]["error"])
        self.replies = [urllib.error.URLError("getaddrinfo failed")]
        self.assertIn("was not sent", self.bp("generate", "--view", "front")[1]["failed"]["error"])
        self.replies = [(200, b'{"data": []}')]
        self.assertIn("held no image", self.bp("generate", "--view", "front")[1]["failed"]["error"])
        self.assertFalse((self.refs / "refs.json").exists())

    def test_no_key_is_reported_with_the_command_that_stores_one(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            status, result, _ = self.bp("generate", "--view", "front")
        self.assertEqual((status, self.calls), (1, []))
        self.assertIn("refgen.py key --provider openai", result["failed"]["error"])

    def test_batch_size_and_cost_are_capped_before_anything_is_sent(self):
        views = [arg for name in ("front", "back", "left", "right", "top") for arg in ("--view", name)]
        status, _, err = self.bp("generate", *views)
        self.assertEqual((status, self.calls), (2, []))
        self.assertIn("limit for one batch is 4", err)
        status, _, err = self.bp("generate", "--view", "front", "--max-cost", "0.05")
        self.assertEqual((status, self.calls), (2, []))
        self.assertIn("above the limit of $0.05", err)
        status, result, _ = self.bp("plan", "--view", "front", "--view", "back")
        self.assertEqual((result["images"], result["estimated_cost_usd"]), (2, round(2 * result["jobs"][0]["estimated_cost_usd"], 2)))
        self.assertAlmostEqual(result["jobs"][0]["estimated_cost_usd"], 0.21, delta=0.03)
        self.assertIn("estimate", result["estimate_note"])
        self.assertEqual(self.bp("generate", *views, "--max-batch", "5")[0], 0)

    def test_provider_model_and_options_come_from_config_and_flags(self):
        status, result, _ = self.run_cli("config", "--root", str(self.root), "--model", "gpt-image-2.6", "--quality", "medium")
        self.assertEqual((status, result["generation"]["model"]), (0, "gpt-image-2.6-sunburst"))  # the family name is not a model id
        self.assertIn("gpt-image-2.6-flare", result["models"])
        self.assertNotIn(KEY, self.output)
        config = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        config["generation"]["params"] = {"complexity": "high"}
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.bp("generate", "--view", "front", "--model", "gpt-image-2.6-flare", "--size", "1536x1024")
        body = self.calls[0]["body"]
        self.assertEqual((body["model"], body["quality"], body["size"], body["complexity"]), ("gpt-image-2.6-flare", "medium", "1536x1024", "high"))
        self.assertNotIn("input_fidelity", body)
        (self.root / "config.json").write_text(json.dumps({"generation": {"provider": "dall-y"}}), encoding="utf-8")
        self.assertEqual(self.bp("plan", "--view", "front")[0], 2)

    def test_the_fake_provider_needs_no_key_or_network_and_cannot_be_approved(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            status, result, _ = self.bp("generate", "--view", "front", "--provider", "fake")
        self.assertEqual((status, self.calls, result["estimated_cost_usd"]), (0, [], 0.0))
        status, _, err = self.bp("approve", "view-front-1")
        self.assertEqual(status, 2)
        self.assertIn("placeholder", err)

    def test_oversized_inputs_are_scaled_to_fit_the_services_limit(self):
        noise = Image.effect_noise((1600, 1600), 90).convert("RGB")
        data = refgen.png(noise, 2048, limit=2_000_000)
        self.assertLess(len(data) * 4 / 3, 2_000_000)
        with Image.open(io.BytesIO(data)) as image:
            self.assertLess(image.width, 1600)


class CheckTest(RefgenCase):
    def check(self, image, part=None):
        blueprint = refgen.blueprints.load(self.blueprint)
        design = Image.open(self.root / "target.png").crop((0, 0, 400, 400))
        return refgen.check(blueprint, {"part": part}, Image.open(io.BytesIO(image)), design)

    def test_a_faithful_image_passes_and_drift_is_named(self):
        self.assertEqual(self.check(picture())["warnings"], [])
        recoloured = self.check(picture(head=(60, 170, 60)))
        self.assertFalse(recoloured["ok"])
        self.assertTrue(any("head colour skin #b42828 is missing" in w for w in recoloured["warnings"]))
        added = self.check(picture(extra=(230, 200, 30)))
        self.assertTrue(any("does not appear in the approved design" in w for w in added["warnings"]))
        self.assertEqual(self.check(picture(body=(250, 250, 0)), part="head")["palette"][0]["present"], True)  # a close-up answers for its own part

    def test_a_busy_background_or_a_missing_subject_is_named(self):
        busy = Image.open(io.BytesIO(picture()))
        draw = ImageDraw.Draw(busy)
        for x in range(0, busy.width, 40):
            draw.rectangle((x, 0, x + 19, busy.height), fill=(90, 120, 60))
        out = io.BytesIO()
        busy.save(out, "PNG")
        self.assertTrue(any("background is not flat" in w for w in self.check(out.getvalue())["warnings"]))
        empty = io.BytesIO()
        Image.new("RGB", (256, 256), (200, 200, 200)).save(empty, "PNG")
        self.assertTrue(any("subject fills 0%" in w for w in self.check(empty.getvalue())["warnings"]))

    def test_warnings_are_recorded_with_the_image_and_shown_again_at_approval(self):
        self.replies = [reply(picture(head=(60, 170, 60)))]
        status, result, _ = self.bp("generate", "--view", "front")
        self.assertEqual(status, 0)
        self.assertIn("is missing", result["made"][0]["checks"][0])
        self.assertIn("is missing", self.bp("approve", "view-front-1")[1][0]["checks"][0])


if __name__ == "__main__":
    unittest.main()
