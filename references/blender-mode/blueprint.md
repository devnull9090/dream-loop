# The blueprint

The blueprint is the approved target turned into numbers, once, before any modelling. It is saved as `.dream-loop/blueprint.json`. Every later check measures the model against it, and every critic judges against it, so nobody re-estimates a proportion from the picture halfway through the work.

Sizes are stated as ratios of subject height **H** (the height of the subject's bounding box in a view), never in pixels or as changes. "Head height 0.31 H" stays true whatever the camera distance or resolution.

## Writing it

Copy [blueprint.template.json](blueprint.template.json) to `.dream-loop/blueprint.json` and fill it in while looking at the target. Every box is `x, y, w, h` as fractions of the target image (left, top, width, height), the same convention as `compare.py --region`.

1. **Views.** One entry for each view or pose the target shows. `frame` is the region of the target that holds that view; `subject_box` is the tight box round the whole subject in it. `pose` names the pose; views that show the same pose from different sides share a name. `background` is `"auto"` (sampled from the frame's border), a `#rrggbb` colour, or `"none"` when the subject sits on a busy background and cannot be keyed.
   When the poses are separate states of the scene, say how to reach each one with `"scene": {"frame": 12}` (a timeline frame) or `"scene": {"collection": "Pose_Leap"}` (only that collection is rendered). If any pose lives in its own collection, give every view its collection, or a view without one will render all the poses at once.
2. **Camera estimate.** For each view, `yaw` (0 is the front view, 90 is the view from the camera at +X in Blender with the subject facing -Y, 180 is the back, -90 the other side), `pitch` (degrees above the horizon, negative when the camera looks up) and `lens` in mm (35 for a close, wide shot; 85 or more for a flat design-sheet drawing). It is an estimate: the view-match step refines it, within limits.
3. **Parts.** Split the subject where a modeller would: for a character, head, torso, shell or back, each arm, each hand or clamp, each leg, each foot. Left and right are separate parts with their own `id`, `objects` and boxes; a pair sharing one id would be measured as one wide box spanning both. A part that looks the same as another sets `same_as` instead of repeating `form` and `colours`; when the model's two are also the same geometry, mirrored or copied, they are reviewed together by one critic.
   - `objects`: the names of the scene objects that make up the part (`*` matches any run of characters). An object can also carry a custom property `dl_part` holding the part id.
   - `boxes`: the tight box round the visible part in each view that shows it. Leave a view out when the part is hidden or mostly covered there.
   - `form`: what the shape is, in a sentence or two: main volume, taper, edges, what it must not look like.
   - `colours`: the part's main colours. Give each an `at` point (a view and an `x, y` on the target) and let the script sample it; do not type hex values by eye.
   - `outlines` (optional): the part's silhouette in a view, as a list of `[x, y]` points round it. See "Outlines" below.
4. **Joints.** Where parts meet (neck, shoulder, elbow, hip, knee), as a point in each view that shows it. The modeller places pivots from these.
5. **Reference.** `reference_view` is the clearest, least foreshortened view; `reference_part` is the part other sizes are quoted against (usually the head).

Then run, with the script path resolved from this skill's directory:

```sh
python /path/to/dream-loop/scripts/blueprint.py validate .dream-loop/blueprint.json --write
python /path/to/dream-loop/scripts/blueprint.py overlay .dream-loop/blueprint.json --out .dream-loop/blueprint.png
```

`validate --write` lists everything that is missing or inconsistent (exit status 2). When the sheet is sound it records the target's size, samples the colours and writes the `measures` block: every part's height, width and centre in H for every view, the same sizes against the reference part (`height_ref`), and every joint position. `measures` is derived from the boxes. Never edit it; change a box and run `validate --write` again.

Open `blueprint.png` and check every box against the picture before going on. A box that clips a part or includes its neighbour becomes a wrong measure that the whole loop will then enforce. Fixing the blueprint is cheap now and expensive later.

## Outlines

A box says how big a part is and where it sits. It says nothing about its shape: a box and an egg with the same bounds measure the same. Give a part an outline and its shape is measured too, by `inner.py`, before any critic is asked; without one, the part's form is judged only by critics, which is slower and costs far more.

An outline is the part's silhouette as the target shows it in one view: `"outlines": {"side": [[x, y], ...]}`, at least three points going round the part, in the same fractions as the boxes. Trace what is visible. Where another part lies in front, follow the edge of that part.

Do not place the points by eye. Have the script propose them, look, then correct:

```sh
python /path/to/dream-loop/scripts/blueprint.py outline .dream-loop/blueprint.json --part head --out .dream-loop/outlines.png
python /path/to/dream-loop/scripts/blueprint.py outline .dream-loop/blueprint.json --part head --write
```

The proposal keeps the pixels inside the part's box that are nearer the part's sampled colours than the background or an overlapping neighbour's colours, takes the largest connected patch and traces its edge. Open `outlines.png`. A second `--write` leaves outlines that are already there alone unless you add `--replace`. `overlay` draws the stored outlines with the boxes, so check them there as well.

Expect to correct it. The proposal is right for a part of one flat colour standing clear of its neighbours. On a painted target, a part made of several materials (a cream head with a dark visor) is traced only where it matches a sampled colour, and parts that share a material and overlap (an arm across a torso of the same colour) run into each other. Tried on a real painted turnaround of eleven parts, most proposals needed moving. What helps, in order:

- `--key subject` keeps everything in the box that is not background and not close to an overlapping part's colours. Better for a part of several materials; worse where an unsampled neighbour shares the box.
- More colour points on the part (one per material, one on a shaded area), then `validate --write` and trace again.
- Correcting by hand. `--close .dream-loop/outlines` writes one enlarged picture per part and view (`head.side.outline.png`) with the points numbered, the part's box, and a grid labelled in target fractions: read the right position for a point off the grid and edit that point in `blueprint.json`. `outline --no-propose --close FOLDER` redraws what is stored, to check the corrections. Twenty to thirty points are enough; follow the silhouette, not the interior.

Which parts to outline: the ones whose silhouette carries the design (a head, a shell, a clamp, a foot), in the views that show them clearly. Leave a view out when the part is mostly covered there. A part with no outline is not checked for shape and goes to the critics as before.

`validate` warns when an outline does not fill its part's box in that view. One of the two is then wrong: fix it, because the box sets the size the model is held to and the outline is compared after both are scaled to the outline's own bounds.

How close the model must come is set in `tolerances`: `outline_iou` (default 0.85, the share of the two shapes that must overlap) and `outline_reach` (default 0.12, the furthest either may stand out beyond the other, as a share of the part's longer side). These defaults were set on simple test shapes, not on a painted target. If a part that plainly matches keeps failing, look at its `.form.png` first: an outline traced loosely on the target fails a correct model. Correct the outline, and only then consider the tolerance.

## Starting the model from it

```sh
python /path/to/dream-loop/scripts/blueprint.py blockout .dream-loop/blueprint.json --out .dream-loop/blockout.json
```

This writes one primitive per part: centre and size in H, with x across a front view, y away from a front camera and z up from the subject's base. It combines every view of the reference pose. A part seen from one direction only has its other extent assumed equal and is listed under `assumed`; check those against the target first.

If many parts are `assumed`, the target does not show enough directions. The user can have the missing views generated from the target: see [generated-references.md](generated-references.md). Those are reading aids. Do not add a generated image as a view here or draw the blueprint's boxes on one: every view and box in the blueprint is on the approved target. The one exception is outside the blueprint: a generated front, back or side view that the user approves for the purpose can give the blockout the extents the target shows from no direction. `blockout` then lists those parts under `from_generated` instead of `assumed`; `measures` do not change.

## Changing it

The blueprint changes only when the target does, or when a box turns out to be wrong. Say so to the user when it happens, rerun `validate --write`, and treat everything measured before the change as unmeasured.
