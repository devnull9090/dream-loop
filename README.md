# dream-loop

By [@anshuc](https://x.com/anshuc).

An agent skill that builds a game, app, or scene with impressive visuals, by creating a closed loop:

1. AI "dreams" up a high-quality target screenshot using image generation
2. AI builds with this target in mind
3. A separate AI critic compares the live screenshot to the target, gate by gate (layout, light and colour, form and materials, fine detail), and returns a scored verdict with the fixes that matter most
4. AI loops back to step 2 until two independent critics pass it, or progress stalls
5. Optionally, AI loops back to step 1 and dreams up an even better target based on the current state.

## Installation

`npx skills add achimala/dream-loop`, or clone into your agent's skills directory, or paste the link into your agent and tell it to figure it out.

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

## Example

Prompt:
> Build me a graphics demo: isometric camera, voxel-ish art style with realistic shading and reflective wet floors, a character in an interesting scene. Fantasy setting (think Elden Ring, Diablo). Three.js in browser, >60fps. Don't download assets. Time limit of 1 hour. Controls: click to move the character, camera lazy-follows; drag to rotate camera; scroll to zoom in/out. No gameplay for now. World should feel alive: motion, animations, subtle environmental behaviors. Area around player should look expansive, but only allow movement in a limited space. No need to confirm the art with me or ask questions, just go!

GPT-6 Astra on high effort in Codex:

![Vesper demo: an isometric fantasy scene](assets/vesper-preview.gif)

[Try the live demo](https://dream-loop-demo.anshu.dev)

## Contributing

I'd love to hear feedback, see results, and accept edits to the skill.

If you open a PR, please provide example results produced by the skill, to ensure the changes don't regress performance.

Run the checks before opening one (Node 18+):

```sh
node --test scripts/fal-batch.test.mjs scripts/links.test.mjs
```
