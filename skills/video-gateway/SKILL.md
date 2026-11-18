---
name: video-gateway
description: Operate a self-hosted Video Gateway for MiniMax H3 / FastH3 video generation. Use when choosing V2 versus OmniRef, composing official H3 prompts, uploading first/last/reference media, submitting an asynchronous job, polling status, cancelling safely, and downloading the MP4. Defaults to quality-first adaptive 768p and 10–15 seconds.
compatibility: Portable HTTP workflow. Works in OpenCode, Codex, Claude Code, and other agents that can run shell/Python. No extra service or SDK is required.
---

# Video Gateway

The gateway is a private, OpenAI-video-style HTTP service in front of a Modal
GPU. It supports FastH3 V2 for text/first-frame/first-and-last-frame generation
and FastH3 OmniRef for ordered references. Treat every generation as billable
GPU work; do not loop or retry without the user's explicit request.

## 1. Configuration

Read credentials from the environment or the user's secret store. Never write
keys into the repository, prompts, logs, screenshots, or shell history.

```bash
export VIDEO_GATEWAY_BASE_URL="https://video.example.com"
export VIDEO_GATEWAY_API_KEY="vg_..."
# Or read the key from a protected file instead:
# export VIDEO_GATEWAY_API_KEY_FILE="$HOME/.config/video-gateway/api_key"
# Only when Cloudflare Access is in front:
export CF_ACCESS_CLIENT_ID="...access"
export CF_ACCESS_CLIENT_SECRET="..."
```

`Authorization: Bearer <key>` is always required. Cloudflare Access service
token headers are an additional outer layer; they do not replace the Bearer key.

`scripts/video_gateway.py` is a dependency-free Python 3 client (standard
library only) for `models`, `enhance`, `create`, `status`, `wait`, `download`
and `cancel`. Run it directly or copy it into another agent workspace. It never
retries automatically. Start by checking capabilities:

```bash
python skills/video-gateway/scripts/video_gateway.py models
```

## 2. Choose the route

| Need | Model | Conditioning |
|---|---|---|
| Text only | `fasth3-v2` | none |
| First-frame I2V | `fasth3-v2` | `input_reference` |
| First-and-last frame | `fasth3-v2` | `input_reference` + `last_reference` |
| Last-frame-only/L2VA | `fasth3-v2` | `last_reference` |
| Ordered character/style refs | `fasth3-omniref` | 1+ `reference_images` (up to 9), optional videos/audio |

Do **not** use `fasth3-trim` for production; it is a hidden preview model.
Regenerate HD and continuation are not enabled on the reference 96 GiB worker.

## 3. Defaults

Use:

```json
{"size":"auto","seconds":10,"enhance_prompt":true}
```

`size=auto` derives an aspect-preserving canvas from the uploaded first/last/
reference image (32-pixel H3 grid, 768p short edge). Use 15 seconds only for a
hero shot or when the user explicitly asks; it costs more and is slower.

## 4. Prompting

Read [references/h3-prompting.md](references/h3-prompting.md). For the complete
official guide, use `gateway/prompts/h3/base-en.txt` for T2VA/I2VA/FL2VA/L2VA and
`gateway/prompts/h3/ref-en.txt` for OmniRef when the repository is available.

Modes are selected automatically from the inputs:

- **T2VA** no images
- **I2VA** first frame only
- **FL2VA** first + last frame
- **L2VA** last frame only
- **Ref2VA / OmniRef** ordered references

Preserve the user's exact dialogue, lyrics and visible sign text in their
original language inside `<d>...</d>` / quotes. Keep reference labels consistent
(`<Picture N>`, `<Video N>`, `<Audio N>`, `<Subject N>`). Do not add a new
location, character, wardrobe or story beat that the user did not request.

### Preview, edit, bypass

Previewing does not create a GPU job:

```bash
python skills/video-gateway/scripts/video_gateway.py enhance \
  --model fasth3-v2 --prompt "..." --image first.png --seconds 10
```

If the returned prompt is edited or accepted, submit it with
`--no-enhance` (or `"enhance_prompt": false`) so it is not rewritten twice.
Use `--no-enhance` for a prompt already written in H3 structure.

## 5. Submit one job

```bash
python skills/video-gateway/scripts/video_gateway.py create \
  --model fasth3-v2 \
  --prompt "The woman in the red coat turns toward the camera and smiles; rain and city ambience." \
  --image first.png --seconds 10 --size auto --wait --download-dir ./out
```

The script submits exactly one job and polls. It never retries automatically.
A retry is a new billable job; only do it after the user asks or after reading a
terminal error.

### Raw API equivalent

```bash
curl -sS "$VIDEO_GATEWAY_BASE_URL/v1/videos" \
  -H "Authorization: Bearer $VIDEO_GATEWAY_API_KEY" \
  -F model=fasth3-v2 \
  --form-string 'prompt=The woman in the red coat turns toward the camera and smiles; rain and city ambience.' \
  -F size=auto -F seconds=10 \
  -F input_reference=@first.png
```

Use `--form-string` for every text field containing a semicolon; plain
`curl -F 'prompt=a; b'` truncates at the semicolon.

## 6. Poll, cancel, download

```bash
python skills/video-gateway/scripts/video_gateway.py status --id video_...
python skills/video-gateway/scripts/video_gateway.py wait --id video_...
python skills/video-gateway/scripts/video_gateway.py cancel --id video_...
python skills/video-gateway/scripts/video_gateway.py download --id video_... --out clip.mp4
```

Status values: `queued`, `submitting`, `processing`, `downloading`, `completed`,
`failed`, `cancelled`. `content_url`/`download_url` carry short-lived HMAC
tokens; do not print or persist them beyond the download.

## 7. Batch and spending

`POST /v1/videos/batch` accepts up to 20 items and runs them serially on one
warm GPU. It is convenient, but it is also up to 20 paid jobs. Do not submit a
batch unless the user asked for a batch, and state the count and worst-case
reserve first. The server budget guard rejects jobs above the soft budget; do
not try to bypass it or split a rejected job to evade it.

## 8. Errors and safety

- `401 invalid_api_key`: wrong/missing Bearer or expired Access session.
- `400 invalid_image` / `invalid_reference`: send a valid JPEG/PNG/WEBP.
- `400 unsupported_reference`: use OmniRef for multiple/ordered refs.
- `400 missing_reference`: OmniRef needs at least one image/video ref.
- `429 budget_exceeded` / `rate_limited`: stop; report to the user.
- `503 worker_not_configured` / `regenerate_hd_unavailable`: do not retry in a loop.
- Never expose uploaded private media, API keys, Access secrets, or signed URLs.
- Do not disable Cloudflare Access or CORS protections.

## 9. Examples

- [T2V](examples/curl-t2v.sh)
- [I2V](examples/curl-i2v.sh)
- [First + last frame](examples/curl-first-last.sh)
- [OmniRef](examples/curl-omniref.sh)

For request/response details see [references/api.md](references/api.md).
