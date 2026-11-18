# Demo media

This directory is the drop point for public release demos. The initial v1.0.0
repository includes owner-supplied reference stills and result frames, but keeps
video files out of the Git history to avoid large binaries; add clips here when
ready.

## Included demo stills

| File | Content |
|---|---|
| `reference-catgirl.jpg` | owner-supplied catgirl reference |
| `result-catgirl.jpg` | frame from a 768×1152, 10 s first-frame I2V clip |
| `reference-portrait.jpg` | owner-supplied portrait reference, 3:4 crop |
| `result-portrait.jpg` | frame from a 768×1024, 10 s first-frame I2V clip |

The catgirl and portrait source images were supplied by the project owner and
are included with permission. Do not reuse them as stock media or redistribute
them outside this repository without permission.

## Demo recipe

| Setting | Value |
|---|---|
| Model | `fasth3-v2` |
| Conditioning | first-frame I2V (`input_reference`) |
| Canvas | `auto`: 768×1152 catgirl, 768×1024 portrait |
| Duration / FPS | 10 s / 24 fps |
| Seed | `20261118` |
| Prompt enhancement | **off** (`enhance_prompt: false`) |
| Measured gateway ledger | catgirl **$0.184**, portrait **$0.170** |
| Prompts | [`prompts/catgirl-i2va.txt`](prompts/catgirl-i2va.txt), [`prompts/portrait-i2va.txt`](prompts/portrait-i2va.txt) |

The prompts are hand-authored in the official I2VA structure. They preserve the
reference identity, clothing and background, and focus on a single clear motion
beat and camera move rather than adding new story elements.

## Suggested video clips

Keep clips short and web-friendly (H.264 MP4, 720p or 1080p, 5–15 MB each):

| File | Suggested content |
|---|---|
| `demo-catgirl-i2v-10s.mp4` | catgirl first-frame I2V, 768×1152, 10 s |
| `demo-portrait-i2v-10s.mp4` | portrait first-frame I2V, 768×1024, 10 s |
| `demo-fl2va-5s.mp4` | first + last frame, 5 s |
| `demo-prompt-preview.mp4` | twenty-second UI-only prompt preview/edit flow |

Add a matching poster next to each clip:

```text
docs/media/demo-catgirl-i2v-10s.mp4
docs/media/demo-catgirl-i2v-10s.jpg
```

## Embedding in the README

GitHub does not reliably render repository-relative `<video>` tags in READMEs.
The robust pattern is a linked poster image:

```markdown
[![Catgirl I2V 10 s demo](docs/media/demo-catgirl-i2v-10s.jpg)](docs/media/demo-catgirl-i2v-10s.mp4)
```

Then add one short caption describing the route, prompt type and duration. Do
not include private reference images without permission, API keys, signed media
URLs, account identifiers or billing screenshots in demo media.

## Additional screenshots

Place new UI screenshots in [`docs/screenshots/`](../screenshots/), keep the
existing filenames when replacing the current gallery, and update the
`More UI screenshots` section in both READMEs. Prefer 1440 px desktop captures
and 390 px mobile captures, with no browser chrome, secrets or third-party
media.
