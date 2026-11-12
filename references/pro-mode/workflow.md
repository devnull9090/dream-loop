# Dream Loop Pro Workflow

## Loop Structure

Prerequisites:
- The target image

The loop:
1. Take a first pass at implementing the target
2. Test and validate the product yourself and ensure it works and looks as you intend
3. Run a review round as described in [review.md](../review.md): capture, prepare the comparison, and get a verdict from an independent critic with a fresh context
4. Address the verdict carefully: every regression it lists first, then its blockers, then its next items. If the critic says a model, the lighting or the camera needs redoing, redo it; do not patch around it
5. Test and validate the product yourself and ensure it works and looks as you intend
6. Evaluate exit criteria (below). If you should exit, stop here. If not, return to step 3 and loop again.

## 3D Assets

During implementation, you may need 3D assets. At such times, consult [references/pro-mode/assets-3d.md](assets-3d.md) to decide how to get them. Do not just make procedural assets out of laziness, read the doc and make the correct decision.

## Critic

The critic's instructions are in [critic.md](../critic.md); you do not need to read them to run a round. It judges four gates in order (layout, light and colour, form and materials, fine detail) and the score is capped by the first gate that fails, so a capture with the wrong layout cannot score above 3 however good its materials are.

The verdict is JSON. `score` and "the judge" in the exit criteria below mean that verdict.

## Exit criteria

- **score >= 8 and target FPS acceptable**: done! Show the user the latest screenshot and ask if they want more iterations.
- **score >= 8 but target FPS unacceptable**: optimize, aiming for lossless wins first, then optimizations that have minimal visual impact. Re-judge after optimizations to ensure you didn't regress visuals.
- **Stall approaching**: the best score hasn't improved by a full point in 2 rounds, or the judge has named the same gap 2 times in a row. Stop making incremental tweaks. Step back and rethink the entire approach and scene, and try to find architectural or big-picture reasons why you're not reaching the target image. It may be that assets are just not good enough, in completely wrong places, the lighting needs to be reworked entirely, the camera is totally wrongly positioned, or other such major issues. Do not make small changes, aim for a dramatic improvement.
- **Stalled**: you tried the **Stall approaching** large architectural change but it didn't work; the judge still gave the same score or worse. Don't waste tokens trying other dramatic changes. Stop and ask the user to weigh in on if the current state looks good enough or if something is significantly off compared to the target.
- **None of the above**: Continue looping. Do not exit.