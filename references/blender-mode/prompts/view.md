# Prompt: another view of the approved design

`refgen.py` fills this in and sends it, with the approved design as image 1, to make one extra
view (front, back, a side, top, a three-quarter). Everything below the `## Prompt` heading is sent;
this part is not. To tune it for one project, copy the file to `.dream-loop/prompts/view.md` and
edit the copy: `refgen.py` prefers it, and records the template's hash with every image.

The prompt has one job: turn the camera and change nothing else. Each rule is there because image
models drift in that direction when it is left out: they tidy proportions, add detail to plain
surfaces, complete a hidden side with new ornament, relight, and lay several views out as a sheet.
State what must stay the same as a list of named things, not as "keep it consistent".

Placeholders, filled from the blueprint:

| Placeholder | Filled with |
|---|---|
| `{inputs}` | One sentence per image sent, numbered, saying what it is and how far to trust it |
| `{subject}` | The blueprint's `subject` |
| `{pose_rule}` | The pose line: keep the pose in image 1, or with `--pose neutral` stand the subject in a symmetric modelling stance without changing any part |
| `{view_name}`, `{view_description}`, `{framing}` | The view asked for, from the table in `refgen.py` |
| `{parts}` | Every part, by name |
| `{palette}` | Every part's sampled colours |
| `{background}` | A flat neutral grey chosen to stand apart from the subject's colours |
| `{extra}` | The `--note` given for this image, if any |

Do not call the input "concept art" or ask for an "illustration": the model then feels free to
interpret. It is a design, and the output is a technical reference.

## Prompt

{inputs}

Show this same subject, unchanged, from one new camera position: {view_description}

This is a technical reference view of an existing object, made for a 3D modeller who will measure it. It is not a new picture of a similar subject. Turn the camera; do not touch the design.

What must stay exactly as it is in image 1:
- Proportions. The size of every part against the others, the length and thickness of each limb and segment, and the overall build. Do not slim, enlarge, lengthen, idealise or balance anything.
- Parts and their number. The subject is made of: {parts}. Show each one that this camera can see. Add no part, remove none, merge none and split none. Count repeated features in image 1 (digits, toes, spikes, plates, segments, eyes, straps) and show the same number.
- Shape language. Hard edges stay hard and soft edges stay soft; flat facets stay flat facets and curves stay curves.
- Colours, on the same parts: {palette}. Do not shift hue, saturation or brightness.
- Materials and markings, at the same scale and in the same places.
- {pose_rule}
- Rendering style. Match image 1. Do not restyle, repaint, clean up, add detail or improve anything.

Where this camera sees a side of the subject that image 1 does not show: continue what is visible and nothing more. Use the same shapes, colours, materials and level of detail as the nearest visible parts, mirrored where the subject is plainly symmetric. Invent no new feature there: no ornament, marking, seam, panel, accessory or texture. Where in doubt, keep the surface plain.

Framing: {framing} The whole subject is in frame, centred and uncropped, filling about 80% of the frame. One subject, shown once, from this one camera: no turnaround row, no second copy, no inset, no detail call-out.

Background and light: one flat, uniform {background} background from edge to edge, with no floor line, gradient, vignette, scenery or props, and no shadow cast on the background. Soft, even, neutral white light with no coloured light, rim light, glow, haze or depth-of-field blur, so that the true surface colours can be read.

No text, labels, arrows, dimension lines, borders, watermark or signature. No other characters, hands or objects.

{extra}
