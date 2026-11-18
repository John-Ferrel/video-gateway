# H3 prompt modes

Source: MiniMax H3 `skills/h3-prompt-writing`,
`references/base-en.txt` and `references/ref-en.txt`. The gateway vendors the
full guides at `gateway/prompts/h3/`.

## Base modes

- **T2VA**: build the complete audiovisual timeline from the user's text.
- **I2VA**: first line is exactly
  `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`
  Start from the frame's style/subject/background, then develop forward.
- **FL2VA**: first line is
  `How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.`
  Describe the motion path between the two frames; reach Picture 2 at the end.
- **L2VA**: first line is
  `How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.`
  Infer a plausible opening and converge to the final frame.

Then use:

```
integrated_multimodal_description: [Shot 1] ...
overall_soundscape: ...
non_diegetic_music: ...
```

## Ref2VA / OmniRef

Use exactly these six sections in order:

```
subject_definitions:
...
summary:
[reference generation] ...
retention_analysis:
...
detailed_description:
...
overall_soundscape:
...
non_diegetic_music:
...
```

Reference labels are stable across sections. `<Subject N>` is reusable visible
content; `<Picture N>` is a concrete frame/composition anchor; `<Video N>` is a
whole-video/edit/continuation source; `<Audio N>` is copied/reference audio.
Use `[video continuation]`, `[reference generation]`, `[keyframe completion]`,
etc. in the summary prefix when applicable.

## Rules that matter

- Preserve subject identity, clothing, palette, art style and the existing
  background when a frame/reference supplies them.
- Preserve dialogue and lyrics verbatim inside `<d>[Language] ...</d>`; preserve
  visible sign text verbatim in quotes.
- Use one continuous shot unless the user explicitly asks for cuts or multiple
  shots.
- Match the requested duration and final-frame timestamp (two decimals).
- Soundscape: ambience and physical action only; do not repeat dialogue there.
- `non_diegetic_music: N/A` unless music is requested.
- Images are analyzed by the enhancer. Video/audio references are passed as
  labels only.
