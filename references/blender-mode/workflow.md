# Dream Loop Blender workflow

Use this to build or rebuild a 3D subject (a character, creature, vehicle or prop) in Blender so that it matches an approved target: a design sheet, a concept board or reference renders. The model may stay in Blender or go on to a game engine; either way the capture that is judged is of the real product.

It differs from the other workflows in four ways. Proportions are measured, not judged. A critic sees one part at a time. Most iterations never reach a critic. And work proceeds in a fixed order, so nobody polishes a surface on a model whose legs are the wrong length.

## What you need

- Blender (tested with 5.3), run headless, or an open session with the [blender-mcp](https://github.com/ahujasid/blender-mcp) add-on connected (port 9876).
- Python 3 with Pillow.
- The approved target saved as `.dream-loop/target.png`. If the user has not approved one, see "The target image" in SKILL.md first.
- A model whose objects can be assigned to parts. The best setup is a Python script that builds the model from named parts, so a rebuild takes seconds; a hand-made `.blend` works too. The surest assignment is a custom property on each object, `obj["dl_part"] = "head"`; otherwise the blueprint matches objects by name pattern.
- If the target shows more than one pose, each pose as its own collection in the scene (or its own timeline frame), named in the blueprint's views.

Resolve script paths from this skill's directory; `SCRIPTS` below stands for `/path/to/dream-loop/scripts`. Keep everything in `.dream-loop` (gitignored):

| File | What it is |
|---|---|
| `blueprint.json` | The target as numbers. Written once. |
| `cameras.json` | One solved camera per view. Written once, then frozen. |
| `ledger.json` | Facts agreed since. |
| `inner/` | The latest inner-loop renders and side-by-side sheets. |
| `rounds/round-N/` | One outer round: renders, captures, part folders, verdicts. |

`WHERE` in the commands is how to reach Blender: `--blend model.blend` (headless), `--setup build_model.py` (headless, the script builds the model; add `--blend` to start from a file) or `--socket` (the open session). Prefer whichever rebuilds fastest.

## Stages

Work through these in order. Each has one gate, and the gate decides when to move on, not your impression of the picture.

| Stage | What you make | Gate | Checked by |
|---|---|---|---|
| 1. Blockout | One primitive per part, silhouette only | Every view's silhouette overlays the target | `inner.py` (view match) |
| 2. Proportion lock | The same primitives at the blueprint's sizes and places | Every part's measures within tolerance in every view | `inner.py` (measured layout) |
| 3. Secondary forms | Real geometry for each part: volumes, plates, joints | Form | Part critic |
| 4. Colour and material | Base colours from the blueprint, roughness, metal, emission | Colour and material | Part critic |
| 5. Surface detail | Texture, seams, wear, small features | Surface detail | Part critic |

Do no work from a later stage on a part that fails an earlier one. In particular, no materials and no detail anywhere until stages 1 and 2 pass for the whole subject.

**Restart rule.** If an existing model fails stage 2 badly (`inner.py` or `verdict.py` says `restart`), do not repair it. Build a new blockout from the blueprint and carry the old model's good pieces over part by part, fitting each to its primitive. Repairing a model built to the wrong proportions costs more rounds than rebuilding it, because every fix moves its neighbours.

## 1. Write the blueprint

Read [blueprint.md](blueprint.md) and follow it. This is the one step that needs your full attention on the target; everything after it is checked against what you write here. Include every view and pose the target shows.

## 2. Build the blockout

```sh
python SCRIPTS/blueprint.py blockout .dream-loop/blueprint.json --out .dream-loop/blockout.json
python SCRIPTS/blender_run.py blockout --spec .dream-loop/blockout.json --height 1.75 WHERE
```

`--height` is the subject's height in scene units. This puts one ellipsoid per part in a "DreamLoop Blockout" collection, each tagged with its part id. If your model comes from a script, read `blockout.json` there instead and size your own primitives from it: centre and size per part, in H.

## 3. Inner loop: no critic

```sh
python SCRIPTS/inner.py --blueprint .dream-loop/blueprint.json WHERE
```

Run this after every change. It renders a flat ID-colour mask of every view, scores the silhouettes, measures every part and prints one line. Read `stage` and `failing`:

- `blockout`: a view's silhouette does not overlay the target. Open `.dream-loop/inner/<view>.match.png` (target only in red, model only in green) and fix the shape.
- `proportion`: the silhouettes match but a part is the wrong size or in the wrong place. Each line gives the target and the current value, for example `leg_l height in side: target 0.42 H, currently 0.55 H`. Set the model to the target value. Do not nudge.
- `outer`: both gates pass. Only now is a critic worth asking.

`.dream-loop/inner/<view>.side.png` shows the target beside the model with every part's box drawn, failures in red. Look at it; do not ask anyone else to.

The first run solves each view's camera: it tries a window of cameras round the blueprint's estimate, keeps the best and freezes it in `cameras.json`. Make that first run on the blockout, not on an older model: a camera solved against the wrong shape is the wrong camera. From then on every render and every capture of that view uses that camera. If the result warns that the best camera is at the edge of the window, the estimate in the blueprint is wrong or the blockout's shape is; fix that and solve again with `viewmatch.py --sweep ... --resolve "reason"`. Never re-solve a camera to make a failing model pass.

Stay in this loop through stages 1 and 2, and come back to it after every edit in the later stages: a form change that breaks a proportion is caught here in seconds.

## 4. Outer loop: capture and critics

Enter it only when `inner.py` exits with status 0.

**Capture every view of the real product.** If Blender is the product, render with the scene's own engine: `inner.py ... --beauty scene --out .dream-loop/outer`. If the product is a game engine, export, then capture each view there with the camera written to that view's `<view>.ids.json` by the latest render: `camera.location` and `camera.target` in scene units, `fov_h` and `fov_v` in degrees, or `yaw`, `pitch`, `distance_H` and `height_H` relative to the subject (yaw 0 faces the subject's front; the camera aims at the centre of its bounds). Use the aspect ratio in `resolution`, and a plain backdrop if you can. Every view needs its own capture: a round with captures for only some views does not start.

**Prepare the round:**

```sh
python SCRIPTS/parts.py prepare --blueprint .dream-loop/blueprint.json --round N --renders .dream-loop/inner \
       --capture side=path/to/side.png --capture front=path/to/front.png
```

When Blender is the product, give no `--capture` at all and point `--renders` at the folder you rendered to; the pictures there are the captures. Engine captures are given for every view or not at all. The script reruns both gates, checks each engine capture against the mask render from the same camera, and cuts a paired crop for every part that needs judging from that part's own bounds in the mask. It prints the parts to send and where each verdict goes.

**Ask one critic per part listed.** Use a subagent with a fresh, empty context for each; run them in parallel. Give each one only:

- the absolute path to [part-critic.md](part-critic.md), with the instruction to read it and act as that critic;
- the absolute path to that part's folder (`rounds/round-N/parts/<part>`);
- the round number.

Do not give it the other parts, the whole subject, a previous score or your opinion. Save each reply to the `verdict` path printed for it. If a reply is not one valid JSON object, ask the same subagent once to resend it.

**Decide:**

```sh
python SCRIPTS/verdict.py .dream-loop/rounds
```

Status 2 means a verdict breaks the rules (a size given as a relative change, a settled fact reversed without evidence, a missing verdict); send the listed problem back to that part's critic once. Otherwise act on `decision`:

- **layout** / **restart**: a gate failed at capture time. Go back to the inner loop, or apply the restart rule.
- **continue**: work on the open parts using the verdict files in `act_on`: regressions first, then blockers, then next. Leave locked parts alone. Then inner loop, then a new round.
- **confirm**: the parts in `confirm` were passed by one critic. Ask a second fresh critic for each, with the same folder, saving the reply as `<part>-confirm.json` beside the first, and run `verdict.py` again. Other parts listed as open still need work.
- **revert**: a locked part changed, or a part got a point worse. Undo that change. If you meant to change a locked part, prepare the round again with `--unlock PART="reason"`.
- **rethink**: a part has had two reviews without real progress. Stop adjusting it; rebuild that part from its primitive with a different construction.
- **ask_user**: stalled after a rethink, or out of rounds. Show the user the part's `pair.png` and the remaining blockers.
- **done**: every part and the assembled subject have been passed by two critics, with every view judged.

A part is `open` until a critic passes it, `passing` until a second confirms it, then `locked`. Locked parts are not sent again, and their model hash and pixels are watched: a change without `--unlock` stops the round. When every part is locked, the next round sends one more subject, `assembly`, which checks how the parts read together.

## Rules that keep rounds from being wasted

- **Absolute values only.** The blueprint, the measures and every critic directive state a size as a target with the current value ("0.31 H, currently 0.45 H"). Never apply or pass on "30% shorter": the next reader estimates the current size differently and the model oscillates.
- **The blueprint settles proportions.** A critic that disagrees with a measured value must cite what on the target shows it (`verdict.py` lists these under `disputes`). Check the blueprint's box for that part against the target. If the box is wrong, fix the blueprint and rerun `blueprint.py validate --write`; if it is right, record the outcome in the ledger and carry on.
- **Record what is agreed.** When a size inside a part is settled, the user accepts a difference, or a dispute is resolved:
  ```sh
  python SCRIPTS/ledger.py add .dream-loop/ledger.json --part head --fact "eye diameter is 0.04 H" --key head.eye.diameter --value 0.04 --source "round 3"
  ```
  Every later critic of that part receives it as a constraint, and one that contradicts a keyed fact without evidence is rejected.
- **One camera per view.** Captures taken with any other camera are not comparable with earlier rounds and fail the capture check.
- **Engine captures drift.** A lock is watched through the Blender model's hash and through the part's own pixels in the capture. If two captures of an unchanged model make `parts.py` report locked parts as changed, pass a higher `--drift`. A neighbour moving in front of a locked part changes which of its pixels show and can trip the check; unlock it with that reason if so.

## Reporting

Report the state as `verdict.py` gives it: which parts are locked, passing and open, each open part's score and first failed gate, anything the verdicts list as unverifiable, and any view that was not captured. Do not describe the subject as passing unless the decision is `done`.
