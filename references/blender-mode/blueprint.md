# The blueprint

The blueprint is the approved target turned into numbers, once, before any modelling. It is saved as `.dream-loop/blueprint.json`. Every later check measures the model against it, and every critic judges against it, so nobody re-estimates a proportion from the picture halfway through the work.

Sizes are stated as ratios of subject height **H** (the height of the subject's bounding box in a view), never in pixels or as changes. "Head height 0.31 H" stays true whatever the camera distance or resolution.

## Writing it

Copy [blueprint.template.json](blueprint.template.json) to `.dream-loop/blueprint.json` and fill it in while looking at the target. Every box is `x, y, w, h` as fractions of the target image (left, top, width, height), the same convention as `compare.py --region`.

1. **Views.** One entry for each view or pose the target shows. `frame` is the region of the target that holds that view; `subject_box` is the tight box round the whole subject in it. `pose` names the pose; views that show the same pose from different sides share a name. `background` is `"auto"` (sampled from the frame's border), a `#rrggbb` colour, or `"none"` when the subject sits on a busy background and cannot be keyed.
   When the poses are separate states of the scene, say how to reach each one with `"scene": {"frame": 12}` (a timeline frame) or `"scene": {"collection": "Pose_Leap"}` (only that collection is rendered). If any pose lives in its own collection, give every view its collection, or a view without one will render all the poses at once.
2. **Camera estimate.** For each view, `yaw` (0 is the front view, 90 is the view from the camera at +X in Blender with the subject facing -Y, 180 is the back, -90 the other side), `pitch` (degrees above the horizon, negative when the camera looks up) and `lens` in mm (35 for a close, wide shot; 85 or more for a flat design-sheet drawing). It is an estimate: the view-match step refines it, within limits.
3. **Parts.** Split the subject where a modeller would: for a character, head, torso, shell or back, each arm, each hand or clamp, each leg, each foot. Left and right are separate parts with their own `id`, `objects` and boxes; a pair sharing one id would be measured as one wide box spanning both. A part that looks the same as another sets `same_as` instead of repeating `form` and `colours`.
   - `objects`: the names of the scene objects that make up the part (`*` matches any run of characters). An object can also carry a custom property `dl_part` holding the part id.
   - `boxes`: the tight box round the visible part in each view that shows it. Leave a view out when the part is hidden or mostly covered there.
   - `form`: what the shape is, in a sentence or two: main volume, taper, edges, what it must not look like.
   - `colours`: the part's main colours. Give each an `at` point (a view and an `x, y` on the target) and let the script sample it; do not type hex values by eye.
4. **Joints.** Where parts meet (neck, shoulder, elbow, hip, knee), as a point in each view that shows it. The modeller places pivots from these.
5. **Reference.** `reference_view` is the clearest, least foreshortened view; `reference_part` is the part other sizes are quoted against (usually the head).

Then run, with the script path resolved from this skill's directory:

```sh
python /path/to/dream-loop/scripts/blueprint.py validate .dream-loop/blueprint.json --write
python /path/to/dream-loop/scripts/blueprint.py overlay .dream-loop/blueprint.json --out .dream-loop/blueprint.png
```

`validate --write` lists everything that is missing or inconsistent (exit status 2). When the sheet is sound it records the target's size, samples the colours and writes the `measures` block: every part's height, width and centre in H for every view, the same sizes against the reference part (`height_ref`), and every joint position. `measures` is derived from the boxes. Never edit it; change a box and run `validate --write` again.

Open `blueprint.png` and check every box against the picture before going on. A box that clips a part or includes its neighbour becomes a wrong measure that the whole loop will then enforce. Fixing the blueprint is cheap now and expensive later.

## Starting the model from it

```sh
python /path/to/dream-loop/scripts/blueprint.py blockout .dream-loop/blueprint.json --out .dream-loop/blockout.json
```

This writes one primitive per part: centre and size in H, with x across a front view, y away from a front camera and z up from the subject's base. It combines every view of the reference pose. A part seen from one direction only has its other extent assumed equal and is listed under `assumed`; check those against the target first.

## Changing it

The blueprint changes only when the target does, or when a box turns out to be wrong. Say so to the user when it happens, rerun `validate --write`, and treat everything measured before the change as unmeasured.
