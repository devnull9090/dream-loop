# dream-loop

By [@anshuc](https://x.com/anshuc).

> **This is a maintained fork.** [devnull9090/dream-loop](https://github.com/devnull9090/dream-loop) is forked from [achimala/dream-loop](https://github.com/achimala/dream-loop), the original by @anshuc. We use it in our own projects and actively maintain this version, folding in what we learn as we keep iterating: critic calibration, token cost, and review of real engine captures against approved targets. Changes land here as pull requests with the evidence behind them.

An agent skill that builds a game, app, or scene with impressive visuals, by creating a closed loop:

1. AI "dreams" up a high-quality target screenshot using image generation
2. AI builds with this target in mind
3. A separate AI critic compares the live screenshot to the target, gate by gate (layout, light and colour, form and materials, fine detail), and returns a scored verdict with the fixes that matter most
4. AI loops back to step 2 until two independent critics pass it, or progress stalls
5. Optionally, AI loops back to step 1 and dreams up an even better target based on the current state.

## Installation

`npx skills add devnull9090/dream-loop` for this fork (`npx skills add achimala/dream-loop` for the original), or clone into your agent's skills directory, or paste the link into your agent and tell it to figure it out.

## Prerequisites

You need an AI agent with:

- access to image generation, either built-in (e.g. Codex, Grok) or via API (e.g. give it a Gemini API key)
- vision input
- subagents (optional but strongly preferred)

Python 3 with Pillow (`python -m pip install Pillow`) is used to prepare the comparison images the critic reads; without it the critic falls back to the raw images.

Install Blender if you want custom 3D modeling. The Blender MCP or scripting interface is preferred to computer use; it produces better results.

At the moment this is only tested with GPT-6 Astra in Codex. Other strong models like Claude Fable 5.1 can likely work too.

## Reviewing without building

The critic also works on its own. If you already have a target image and a product, ask your agent to "use dream-loop to review this capture against the target". It prepares a side-by-side sheet and zoomed probes, gets a verdict from an independent critic, and reports the score, the first gate that fails, regressions since the last capture and the fixes needed, along with anything it left out or could not judge from a still.

## Modelling a 3D subject in Blender

For a character, creature or prop that has to match an approved design sheet, ask your agent to "use dream-loop to model this in Blender". This mode was built from a real art session in which whole-image reviews kept a creature model stuck for seven rounds, and it works differently from the scene loop:

1. The target is turned into a **blueprint** once: the parts, each part's size as a ratio of the subject's height, joints, sampled colours, form notes, and every view with a camera estimate.
2. A **view-match gate** solves one camera per view and freezes it. Nothing is judged until the model's silhouette overlays the target's.
3. **Layout is measured**, not judged: a flat ID-colour mask render gives every part's projected size and position, compared with the blueprint in numbers ("leg height: target 0.42 H, currently 0.55 H").
4. A fast **inner loop** in Blender (render, view match, measure; seconds, no critic) runs after every change. The **outer loop**, a capture of the real product plus independent critics, is entered only when the inner checks pass.
5. Critics review **one part at a time** from crops cut automatically from that part's bounds. Parts that pass are confirmed by a second critic and locked; a locked part that changes is a regression.
6. Work goes in a fixed order: blockout, proportion lock, secondary forms, colour and material, surface detail. A model that fails layout badly is rebuilt from a blockout, not repaired.

It needs Blender, headless or an open session with the blender-mcp add-on. See [references/blender-mode/workflow.md](references/blender-mode/workflow.md).

### Extra views and close-ups from one design

A single concept image often shows the subject from one angle, with small parts a critic cannot read. The agent can offer to generate what is missing from the approved image: front, back, side, top and three-quarter views, and close-ups of single parts. The prompts send the approved image as the input and forbid changing it; they are plain files you can tune ([view](references/blender-mode/prompts/view.md), [part close-up](references/blender-mode/prompts/part-closeup.md)).

You choose the provider and model (OpenAI's `gpt-image-2.6-sunburst` by default) and store the key yourself, in your own terminal. It is asked for without echo and kept in `.dream-loop/secrets.env`, which must be git-ignored:

```sh
python /path/to/dream-loop/scripts/refgen.py config --provider openai --model gpt-image-2.6-sunburst
python /path/to/dream-loop/scripts/refgen.py key --provider openai
```

The agent tells you the estimated cost before each batch (four images at most by default), then shows you every result beside the original. Nothing generated is used until you approve it, each image is recorded with its prompt, inputs, model and cost in `.dream-loop/refs/refs.json`, and a generated image never overrides the approved design. If your design shows only an action pose, the agent can instead generate a neutral-pose turnaround (front, side, back) for you to approve as the modelling target, keeping the original as the final check once the model is rigged and posed. See [references/blender-mode/generated-references.md](references/blender-mode/generated-references.md).

## Example

Prompt:
> Build me a graphics demo: isometric camera, voxel-ish art style with realistic shading and reflective wet floors, a character in an interesting scene. Fantasy setting (think Elden Ring, Diablo). Three.js in browser, >60fps. Don't download assets. Time limit of 1 hour. Controls: click to move the character, camera lazy-follows; drag to rotate camera; scroll to zoom in/out. No gameplay for now. World should feel alive: motion, animations, subtle environmental behaviors. Area around player should look expansive, but only allow movement in a limited space. No need to confirm the art with me or ask questions, just go!

GPT-6 Astra on high effort in Codex:

![Vesper demo: an isometric fantasy scene](assets/vesper-preview.gif)

[Try the live demo](https://dream-loop-demo.anshu.dev)

## Contributing

This fork is maintained here. Report problems, share results and ask questions in [issues](https://github.com/devnull9090/dream-loop/issues), and send edits as pull requests against [devnull9090/dream-loop](https://github.com/devnull9090/dream-loop). Changes that would help everyone are also worth offering to the [original](https://github.com/achimala/dream-loop).

If you open a PR, please provide example results produced by the skill, to ensure the changes don't regress performance.

Run the checks before opening one (Node 18+, Python 3 with Pillow):

```sh
node --test scripts/fal-batch.test.mjs scripts/links.test.mjs
python -m unittest discover -s scripts -p "*_test.py"
```

The Python tests that drive Blender are skipped when Blender is not installed.
