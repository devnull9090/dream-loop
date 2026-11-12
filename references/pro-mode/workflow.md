# Dream Loop Pro Workflow

## Loop Structure

Prerequisites:
- The target image

The loop:
1. Take a first pass at implementing the target
2. Test and validate the product yourself and ensure it works and looks as you intend
3. Run a review round as described in [review.md](../review.md): capture, prepare the comparison, and get a verdict from an independent critic with a fresh context
4. Decide what to do next from the verdict (see "Deciding what to do next" below). If the decision is to stop, stop here
5. Address the verdict carefully: every regression it lists first, then its blockers, then its next items. If the critic says a model, the lighting or the camera needs redoing, redo it; do not patch around it
6. Test and validate the product yourself and ensure it works and looks as you intend, then return to step 3

## 3D Assets

During implementation, you may need 3D assets. At such times, consult [references/pro-mode/assets-3d.md](assets-3d.md) to decide how to get them. Do not just make procedural assets out of laziness, read the doc and make the correct decision.

## Critic

The critic's instructions are in [critic.md](../critic.md); you do not need to read them to run a round. It judges four gates in order (layout, light and colour, form and materials, fine detail) and the score is capped by the first gate that fails, so a capture with the wrong layout cannot score above 3 however good its materials are.

## Deciding what to do next

After each review round, run (Python, no dependencies):

```sh
python /path/to/dream-loop/scripts/verdict.py .dream-loop/verdicts
```

If it exits with status 2, the verdict breaks the critic's output rules; send the listed problems back to the same critic once and save the corrected reply. Otherwise it prints one line with a `decision`:

- **continue**: act on the verdict file named in `act_on` and loop again. Address all of it this round, not only the first item; rounds are expensive.
- **revert**: the critic scored this capture a point or more below the previous one. Undo the change that caused it (the whole round if you cannot tell which), then try a different way.
- **rethink**: two rounds in a row brought no real progress. Stop making incremental tweaks. Step back and find the architectural or big-picture reason the capture is not reaching the target: the assets may not be good enough or may be in the wrong places, the lighting may need reworking entirely, the camera may be wrong. Make one large change aimed at a dramatic improvement, then loop again.
- **confirm**: the capture reached the pass score with no regressions. Ask a second fresh critic to review the same review folder (step 3 of [review.md](../review.md); do not prepare the images again and do not tell it the capture passed), save its reply as `.dream-loop/verdicts/round-N-confirm.json`, and run `verdict.py` again.
- **done**: two independent critics passed the capture. If the target FPS is acceptable, show the user the latest capture and ask if they want more iterations. If not, optimize, aiming for lossless wins first and then optimizations with minimal visual impact, and run another review round afterwards to make sure the visuals did not regress.
- **ask_user**: the large change after a rethink did not help, or the round limit was reached. Do not spend tokens on further dramatic changes. Show the user the latest `compare.png` and the remaining blockers, and ask whether the current state is good enough or something is significantly off.

Progress is measured inside one verdict (`score - previous_score`, or passing a further gate), never between two critics' scores, and a gain under half a point counts as none.

The pass score is 8; pass `--pass-score` to change it if the user asked for a different bar. The round limit is 10 (`--max-rounds`, 0 for none); a time budget from the user still applies on top.

If Python is unavailable, apply the same rules by hand.
