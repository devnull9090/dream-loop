# Generated references

One approved image is often not enough to model from. A single three-quarter view gives the blockout no depth and hides the far arm and the far foot. A part that is 60 pixels across on the board cannot be judged: critics guess at its toes and reverse each other from round to round.

`refgen.py` asks an image model for what is missing, using the approved design as the input image:

- **another view** of the same subject: front, back, left, right, top or a three-quarter;
- **a close-up** of one part, from the design's own angle or from one of those views.

This step is optional, costs money, and is the user's decision each time. Offer it; do not run it unasked.

## The rule

A generated image is supplementary evidence. It does not become the target on its own; only the user can make it one, by approving a turnaround as the new target (see "Modelling from a neutral-pose turnaround").

- The approved design stays the authority. Measures come from the blueprint's boxes on the approved design, and the view-match and layout gates never look at a generated image.
- The one number a generated view may supply is an extent the approved design shows from no direction, for the blockout only, and only after the user approves that view for it (see "Depth for the blockout").
- Nothing generated is used until the user has looked at it and accepted it. `refgen.py approve` records that; until then every other script ignores the image.
- Where a generated image and the approved design disagree, the approved design is right. A critic is told which images are generated and told this.
- If the approved design changes, or a generated file is edited, its approval lapses.

## When to offer it

| When | What | Why |
|---|---|---|
| After the blueprint validates, before the blockout | The orthographic views the board lacks | `blueprint.py blockout` lists parts seen from one direction only under `assumed`: their depth is a guess, and parts on the far side have no box at all |
| In the outer loop, when a part's verdict lists `target_unclear` | A close-up of that part | The critic could not read the part on the approved design |
| When `verdict.py` says `rethink` or `ask_user` for a part | A close-up of that part, perhaps also from a second view | Two reviews without progress often mean the builder and the critic are reading a small target differently |

`plan` with no `--view` or `--part` works this out and prints what it would propose and why:

```sh
python SCRIPTS/refgen.py plan --blueprint .dream-loop/blueprint.json
```

Tell the user what it suggests, what each image is for and what the batch is estimated to cost, and ask which they want. Do not offer it when nothing is listed.

## Choosing the provider and model

The choice lives in `.dream-loop/config.json` and is the user's. Ask which provider and model they want the first time; do not pick silently.

```sh
python SCRIPTS/refgen.py config --root .dream-loop --provider openai --model gpt-image-2.6-sunburst --quality high
python SCRIPTS/refgen.py config --root .dream-loop        # shows the settings, the models known for the provider, and whether a key is stored
```

| Provider | Models | Notes |
|---|---|---|
| `openai` | `gpt-image-2.6-sunburst` (default: detail and precise edits), `gpt-image-2.6-flare` (faster drafts), or any other id the Images API accepts | Uses `POST /v1/images/edits` with the design as input. "gpt-image-2.6" alone is not a model id; it is read as `gpt-image-2.6-sunburst` |
| `fake` | `fake-1` | No service and no cost: returns the input, stamped FAKE. For trying the steps. Its images cannot be approved |

`config.json` holds a `generation` block. Besides `provider`, `model`, `quality` and `size` (`auto` picks portrait, landscape or square from the subject's shape), it may set `max_batch` (4), `max_cost_usd` (2.00), `timeout`, `background`, `pricing` (USD per million tokens, when the built-in rates are out of date) and `params`: any other field the provider's API accepts, passed through as written, for example `{"complexity": "high"}`. The same settings can be given as flags for one run.

The OpenAI contract was read from the [image edit reference](https://developers.openai.com/api/reference/resources/images/methods/edit) and the [image generation guide](https://developers.openai.com/api/docs/guides/image-generation) on 2026-11-12. Recheck them when a real error suggests the contract changed. A second provider is one new class in `refgen.py`.

## The API key

The user stores the key themselves, in their own terminal:

```sh
python SCRIPTS/refgen.py key --root .dream-loop --provider openai
```

It asks for the key without showing it and writes it to `.dream-loop/secrets.env`. It refuses if git could commit that file (add `.dream-loop/` to `.gitignore` first) and refuses if the terminal cannot hide what is typed. `OPENAI_API_KEY` in the environment is used in preference when it is set. `key --check` says whether a key is available and where from; `key --remove` deletes it.

Rules for you, the agent:

- Never ask the user to paste a key into the chat, and never accept one there. Give them the command above to run.
- Never run the `key` command yourself: it needs a real terminal and the user's hands. You may run `key --check`.
- Never read `secrets.env`, print it, or pass a key on a command line. There is no flag that takes one.

## Generating

```sh
python SCRIPTS/refgen.py plan     --blueprint .dream-loop/blueprint.json --view front --view left --part foot_l
python SCRIPTS/refgen.py generate --blueprint .dream-loop/blueprint.json --view front --view left --part foot_l
```

`plan` sends nothing. It prints the exact prompt for each image, the inputs each will carry and the estimated cost. Tell the user the count and the estimate before running `generate`. The estimate is rough: it comes from the provider's token rates and one measured call (a 1024x1024 view at `high` from `gpt-image-2.6-sunburst` cost $0.065 on 2026-11-12). The real cost of each call is recorded afterwards as `cost_usd`.

`generate` stops before sending anything if the batch is larger than `max_batch` or its estimate is above `max_cost_usd`. Raise either only on purpose, with `--max-batch` or `--max-cost`, after the user has agreed to the cost. A failed or timed-out call is never retried automatically: a timeout may still have been charged. Images made before the failure are kept.

- `--view NAME`: `front`, `back`, `left`, `right` (the subject's own left and right), `top`, `three_quarter_left`, `three_quarter_right`, `three_quarter_back_left`, `three_quarter_back_right`.
- `--part ID`: a close-up from the design's own angle, the safest kind, since nothing has to be imagined. `--part ID@VIEW` turns it, for a part the design shows from one side.
- `--pose neutral`: for views, a symmetric modelling stance instead of the design's own pose. See "Modelling from a neutral-pose turnaround".
- `--from VIEW`: which blueprint view to work from. By default a view uses the reference view, and a close-up the view that shows the part largest.
- `--with REF`: also send an approved generated reference, so that a back view stays consistent with an accepted front view. The design still outranks it.
- `--note "..."`: one extra instruction for these images, for a retry: "the left hand has three claws, not four".

What is sent: image 1 is the source view's `frame` cut from the approved design (not the whole board, so that labels and other poses do not leak in); then the other views of the same pose; then, for a close-up, the part's own box from the blueprint, padded and enlarged.

## The prompts

The two templates are [prompts/view.md](prompts/view.md) and [prompts/part-closeup.md](prompts/part-closeup.md). They are filled from the blueprint: the subject, every part's name, the part's `form` note, its sampled colours, and the neighbours it meets at each joint. Good `form` notes and colour samples in the blueprint make better prompts.

To tune a prompt for one project, copy the template to `.dream-loop/prompts/` and edit the copy. Keep what the templates insist on: the same object; every proportion, part count, colour, material and marking kept; nothing added, removed or restyled; a flat neutral background and even light; no text and no second subject; for a close-up, resolve what is there and invent nothing; for a hidden side, continue the visible design and nothing more.

## Approval

Every image is saved in `.dream-loop/refs/` with a sheet, `<id>.sheet.png`: the approved design beside the generated image, with the blueprint's colours checked against it. The automatic checks (a blueprint colour missing, a large colour that is not in the design, a background that is not flat) catch only gross drift. They cannot tell whether a shape is right.

**Show the user every sheet and let them decide.** Look at it yourself first and tell them what you see that differs from the design: a changed count, a rounded facet, an added seam. Then record their answer:

```sh
python SCRIPTS/refgen.py approve --blueprint .dream-loop/blueprint.json view-front-1 part-foot_l-1
python SCRIPTS/refgen.py reject  --blueprint .dream-loop/blueprint.json view-left-1 --reason "the tail is twice as long"
python SCRIPTS/refgen.py list    --blueprint .dream-loop/blueprint.json
```

Never approve on the user's behalf, and never approve an image they have not seen. A rejected image is worth one retry with a `--note` naming the fault; after two failures on the same thing, tell the user the model cannot hold that detail and move on without it.

`refs/refs.json` records, for every image: provider, model, the exact prompt and the template's hash, the hash of every input image, the parameters sent, the date, the usage and cost reported, the check results, and whether and when it was approved or rejected.

## Depth for the blockout

A blockout made from one view has to assume that each part is as deep as it is wide. An approved front, back, left or right view can replace that assumption with a number. This takes a second, separate approval, because it lets a generated image set a size:

```sh
python SCRIPTS/refgen.py boxes   --blueprint .dream-loop/blueprint.json view-front-1 --box head=0.40,0.12,0.20,0.17 --box torso=0.34,0.29,0.31,0.40
python SCRIPTS/refgen.py approve --blueprint .dream-loop/blueprint.json view-front-1 --for-blockout
python SCRIPTS/blueprint.py blockout .dream-loop/blueprint.json --out .dream-loop/blockout.json
```

1. `boxes` takes a tight box for each part you can see on the generated view, as `x,y,w,h` fractions of that image, the same way the blueprint's boxes are drawn on the target. The subject's own box is keyed off the flat background (or given with `--subject`). It writes `<id>.boxes.png`.
2. Show the user `<id>.boxes.png` and say what it will be used for. If they agree, `approve --for-blockout` records it. Drawing the boxes again clears that approval.
3. `blueprint.py blockout` then uses the view, and prints `from_generated` with the parts it affected. `--no-refs` ignores generated views.

What it changes is narrow on purpose:

- A part whose width and depth the target already settles is left exactly as it was.
- A generated view speaks only for the axis it faces: a front or back view for width, a left or right view for depth. An axis that a target view faces squarely is never taken from a generated view, and an axis with no generated view stays under `assumed`.
- When the target shows the part at an angle (a three-quarter view) and both axes have a generated view, the generated views decide only how the size splits between width and depth. The size across the target's own view stays what the target measures.
- Height and height above the base always come from the target.

The blueprint, its `measures`, the view-match gate and the measured layout are untouched: the inner loop still checks the model against the target alone. A generated view that disagrees with the target therefore cannot make the model pass or fail anything; it can only give a part a poor starting depth, which is what the assumption gave it before. Check those parts by eye, as you would the `assumed` ones.

## Modelling from a neutral-pose turnaround

A design drawn in an action pose (hunched, mid-stride, one arm forward) is a poor thing to model from: limbs are foreshortened, parts hide each other, and nothing is symmetric. Characters are normally modelled in a neutral stance from front, side and back views, rigged, and posed afterwards. If the approved design shows only an action pose, offer the user this route. It is their decision, because it replaces what the loop measures against.

1. **Generate the front view in a neutral stance** and get it approved:
   ```sh
   python SCRIPTS/refgen.py generate --blueprint .dream-loop/blueprint.json --pose neutral --view front
   ```
   The prompt keeps every rule about the design and changes only the pose line: upright, symmetric, limbs a little away from the body, every part the same length, thickness, shape and count as in the design.
2. **Generate the other views from it**, so they share one stance and one reading of the design:
   ```sh
   python SCRIPTS/refgen.py generate --blueprint .dream-loop/blueprint.json --pose neutral --with view-front-1 --view left --view back
   ```
   A top view and the other side are optional. Get each approved.
3. **Lay them out as a board:**
   ```sh
   python SCRIPTS/refgen.py board --blueprint .dream-loop/blueprint.json view-front-1 view-left-1 view-back-1
   ```
   This writes `target-turnaround.png` and `blueprint-turnaround.json`: a starter blueprint with one view per panel (frame, subject box, exact camera yaw), the parts with their form notes and sampled colours carried over, and `design` naming the original image. Its part boxes and joint points are empty.
4. **Ask the user to approve the board as the new target.** Say plainly what that means: the neutral proportions and every surface the original did not show (the front of a head seen from the side, the back) are the image model's reading. Once approved, they are the design the model is built and judged against.
5. **Fill in the starter blueprint** as in [blueprint.md](blueprint.md): every part's box in every view that shows it, and the joints. Then:
   ```sh
   python SCRIPTS/blueprint.py validate .dream-loop/blueprint-turnaround.json --write
   python SCRIPTS/refgen.py agree --blueprint .dream-loop/blueprint-turnaround.json
   ```
   `agree` compares each part's height and height above the base across the views. Views generated separately can disagree; it names each part that does ("head height: 0.35 H in front, 0.25 H in side"). Check the boxes first. If they are right, the views themselves disagree: regenerate the odd view with a `--note`, or ask the user which view the model follows and record it in the ledger.
6. **Start the loop from the turnaround.** This is a change of target, so everything measured before is unmeasured: archive the old `cameras.json`, `inner/` and `rounds/`, and run the workflow from the blockout with the turnaround blueprint. With true front and side views the blockout has no `assumed` parts and the camera solve is nearly exact.
7. **Keep the original design as the final check.** When the model is rigged, pose it as the original shows and review a capture against the original image with the review-only mode in [../review.md](../review.md). That is the test that the neutral model still is the approved design.

Close-ups and views made from the original design stay current under a turnaround blueprint, because it names the original in `design`.

## How approved references are used

- **By you**, as a reading aid: when checking the parts `blueprint.py blockout` lists under `assumed`, and when building a side the approved design hides.
- **By part critics**: `parts.py prepare` puts a part's approved close-ups in its folder as `generated.png`, marked GENERATED, with the rule in `brief.json` that the TARGET crop wins any conflict and that a directive resting only on a generated picture cannot fail a gate. Approved views go to the `assembly` critic the same way.
- **By the blockout**, for unseen extents only, when the user has approved a view `--for-blockout`.
- **As the target**, only as a turnaround board the user has approved as the new target.
- **Not** by the view-match gate, the measured layout or the blueprint's measures. Those stay on the approved design.
