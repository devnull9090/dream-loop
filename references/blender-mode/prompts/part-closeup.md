# Prompt: a close-up of one part

`refgen.py` fills this in and sends it to make a larger picture of one part (a head, a hand, a
foot) that is too small on the approved design to judge. Image 1 is the approved design; the next
image is that part's own crop from it, enlarged. Everything below the `## Prompt` heading is sent;
this part is not. To tune it for one project, copy the file to `.dream-loop/prompts/part-closeup.md`
and edit the copy: `refgen.py` prefers it, and records the template's hash with every image.

The risk with a close-up is the opposite of the risk with a view. The model is asked for more
pixels and fills them with detail the design never had: panel lines, pores, scratches, extra
toes. So the prompt separates two things. Resolving is allowed: sharper edges, the forms that are
there made clear. Inventing is not. It also names the neighbours, so the part is cut off cleanly
at its joints and the model does not redraw the whole subject.

Placeholders, filled from the blueprint:

| Placeholder | Filled with |
|---|---|
| `{inputs}` | One sentence per image sent, numbered, saying what it is and how far to trust it |
| `{subject}` | The blueprint's `subject` |
| `{part}` | The part's name |
| `{form}` | The part's `form` note |
| `{colours}` | The part's sampled colours |
| `{neighbours}` | The parts it joins and the joint between them, from `joints` |
| `{closeup_view}`, `{framing}` | The camera: the same as image 1, or a view asked for with `PART@VIEW` |
| `{background}` | A flat neutral grey chosen to stand apart from the subject's colours |
| `{extra}` | The `--note` given for this image, if any |

## Prompt

{inputs}

Show only the {part} of this same {subject}, larger and sharper than image 1 shows it, {closeup_view}

Treat this as moving the camera closer to the same object with a sharper lens. The result must be the {part} that is in image 1, not a new design for one. It is a technical reference for a 3D modeller.

What this part is: {form}
Its colours, sampled from image 1: {colours}.
{neighbours}

What must stay exactly as it is in image 1:
- Outline and proportions. The part's silhouette, how thick each segment is against the others, its taper and its curvature.
- Features and their number. Count what image 1 shows on this part (digits, toes, claws, spikes, plates, segments, eyes, openings, straps) and show exactly that many, in the same places and at the same relative size.
- Shape language. Hard edges stay hard and soft edges stay soft; flat facets stay flat facets and curves stay curves. Do not round off a faceted form or facet a rounded one.
- Colours, materials and markings, where image 1 has them.
- Rendering style. Match image 1. Do not restyle, repaint or improve it.

Resolve; do not invent. The extra resolution may sharpen edges and make the forms that are already there easier to read. It must not add anything image 1 does not support: no new panel lines, seams, rivets, scratches, scales, pores, wrinkles, patterns, wear, decals or texture. Where image 1 shows a plain surface, keep it plain. Where image 1 is too small or too soft to say what a detail is, choose the simplest reading that fits the rest of the design and leave it at that.

Where this camera sees a side of the part that image 1 does not show: continue what is visible and nothing more, with the same shapes, colours, materials and level of detail, mirrored where the part is plainly symmetric. Invent no new feature there.

Framing: {framing} The {part} fills about 80% of the frame, centred and uncropped. Show only the first short stub of each neighbouring part, cut off cleanly at the edge of the frame, so that the joint can be read; do not show the rest of the subject. One part, shown once: no second angle, no inset, no exploded view.

Background and light: one flat, uniform {background} background from edge to edge, with no floor line, gradient, vignette, scenery or props, and no shadow cast on the background. Soft, even, neutral white light with no coloured light, rim light, glow, haze or depth-of-field blur, so that the true surface colours can be read.

No text, labels, arrows, dimension lines, borders, watermark or signature. No other characters, hands or objects.

{extra}
