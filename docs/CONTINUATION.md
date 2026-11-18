# OmniRef video continuation investigation

OmniRef (Ref2VA) supports a continuation reference in the upstream FastVideo
implementation:

- Append `MiniMaxH3Reference(source=clip, media_type="continuation")` to the
  ordered references. A request takes at most one continuation clip and still
  needs at least one normal image/video reference.
- The clip is resampled to 24 fps and must contain 5, 22 or 39 frames on the
  target canvas. The generated video starts by regenerating those frames at the
  same positions, so the caller drops the overlap and appends the new tail.
- `num_frames` counts the continuation: 22 overlap frames plus 5 new seconds is
  146, aligned up to 158 (17n+5 grid).
- Continuation preserves the original audio timing better than conditioning on
  the previous last frame as an image reference; the upstream notes the join is
  roughly half as visible and the sound continues instead of restarting.
- Prompt from the clip's actual end state; a contradictory opening causes a cut.
  Fewer, longer segments hold up better than many short ones.

## Why it is not enabled yet

A correct gateway action needs three pieces beyond the worker flag: extract the
last 5/22/39 frames from the completed clip, submit a 158-frame/22-overlap job
with the original references, then trim the overlap and mux the extended video
without duplicating audio. That changes the result lifecycle (the new asset is a
child of the old video) and needs its own quality/cost validation.

The existing OmniRef 10 s and 15 s routes are tested and stay the supported
quality path. Continuation remains a documented next step, not a hidden
half-implemented button.
