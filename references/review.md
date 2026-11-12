# Running a review round

A review round compares one capture with its target and returns a verdict from an independent critic. Round numbers start at 1.

## 1. Capture

Capture the product at the target's resolution and aspect ratio and save it as `.dream-loop/captures/round-N.png`. Keep earlier rounds.

For a browser product without a screenshot tool, `scripts/preview-server.py` serves the workspace on `127.0.0.1:4172` and saves any PNG the page POSTs to `/__capture` (for example from `canvas.toBlob`) as `.dream-loop/captures/latest.png`; copy that to `round-N.png`.

## 2. Prepare the images

Resolve the script path from this skill's directory (needs Python with Pillow):

```sh
python /path/to/dream-loop/scripts/compare.py --target .dream-loop/target.png --current .dream-loop/captures/round-N.png --out .dream-loop/review
```

From round 2 on, add `--previous .dream-loop/captures/round-<N-1>.png`.

This writes `compare.png` (at most 1024 px on its longest edge), `probes.png` (at most 1568 px) and `metrics.json` to `.dream-loop/review`. Use `compare.png` for your own look at the result too; there is no need to open the full-size target and capture again each round.

The probes are the grid cells that differ most. That only means something when capture and target show the same frame. If the target is a reference board or a different camera, pass `--probes 0` and name each subject with its box in each image, as fractions of the frame: `--region statue=0.05,0,0.4,1:0.3,0.15,0.35,0.8` (target box, then capture box). Add `--region` for anything the user cares about that the automatic probes might miss.

Look at `probes.png` once before sending it. If a region's crop misses its subject, correct the box and run the script again; a critic handed a probe of bare floor has to open the full-size images instead.

If Pillow cannot be installed, give the critic the target, capture and previous capture directly and tell it there is no review folder.

## 3. Ask the critic

Use a subagent with a fresh, empty context; do not fork your own. If your harness has no subagents, run another instance of it from the shell. Only as a last resort judge it yourself, and tell the user the review was not independent.

Give the critic only:

- the absolute path to [critic.md](critic.md), with the instruction to read it and act as that critic;
- the absolute path to the review folder;
- the round number;
- scope notes: what the target is (same frame, reference board, different camera) and any differences the user has accepted, with the reason;
- from round 2 on, the previous round's `blockers` and `next` items as `id` and `change` only.

Do not give it the previous score, your own opinion of the result, or a description of what you changed. A critic told the last score was 7.5 scores near 7.5; one that sees only the images scores what is there.

## 4. Keep the verdict

Save the reply as `.dream-loop/verdicts/round-N.json`. If it is not a single valid JSON object, ask the same subagent once to resend it as JSON only.

Act on `regressions` first, then `blockers`, then `next`. The score places the capture on the scale; use `score - previous_score` from the same verdict, not the difference from last round's verdict, to tell whether the round helped, because two critics differ by a few tenths on the same image.

One critic per round is enough. Use several only for independent views (different screens or cameras), one critic each.

## Review only

Use this when there is no build loop to run: the user has a target and a product and wants a verdict, or you are iterating on the product yourself and want an independent check after each change.

- The target is whatever the user approved. Pass its path as `--target`; do not regenerate or alter it. Never substitute a concept render or paintover for a capture of the real product.
- Keep working files in `.dream-loop` as above (gitignore it), with one `round-N` per capture, so later rounds can show what changed.
- Run steps 1 to 4, then `python /path/to/dream-loop/scripts/verdict.py .dream-loop/verdicts`. It checks the verdict and, when you are iterating, says whether to continue, rethink, get a confirming second critic or stop. Its decisions are explained in [pro-mode/workflow.md](pro-mode/workflow.md) under "Deciding what to do next"; that is the only part of that file you need.
- Report the verdict as it stands: score, first failed gate, regressions, blockers, and everything under `excluded` and `unverifiable`. Do not round the score up, drop items, or describe a capture as passing unless two critics passed it.
