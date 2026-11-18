# API

Base URL: `http://127.0.0.1:8787` or `https://video.example.com`.
All `/v1` and `/usage` routes require:

```http
Authorization: Bearer $VG_API_KEY
```

### Multiple keys and isolation

`API_KEYS` is a list of library slots. A key can read and mutate only videos and
batches created by that key; another key receives `404` for hidden resources.
Budget, Usage, storage, the GPU queue and worker health are global and shared.
The first key is the only key allowed to call `/v1/admin/*`. Keep key order
stable when rotating values; append new keys for new users.

When Cloudflare Access is in front, API clients also need an Access **service
token**:

```http
CF-Access-Client-Id: <id>.access
CF-Access-Client-Secret: <secret>
Authorization: Bearer $VG_API_KEY
```

### Edge access note

The reference deployment uses a narrow Cloudflare Access bypass for exactly
these collection paths:

- `/v1/models`
- `/v1/videos`
- `/v1/batches`
- `/v1/prompts/enhance`
- `/v1/usage`

These are exact paths, not subtree wildcards. Object status, media, thumbnail,
cancel, retry, regenerate and `/v1/batches/{id}` remain Access-protected and
must be called with Access service-token headers (or from a browser with an
Access session). The gateway Bearer key is required on every route.

Errors use an OpenAI-style envelope:

```json
{"error":{"message":"...","type":"invalid_request_error","param":"model","code":"unknown_model"}}
```

## Models

### `GET /v1/models`

```json
{
  "object": "list",
  "prompt_enhancement_available": false,
  "data": [{
    "id": "fasth3-v2",
    "object": "model",
    "label": "FastH3 V2 NVFP4 QAD",
    "family": "minimax-h3",
    "license": "MiniMax H3 Community",
    "native_audio": true,
    "image_to_video": true,
    "supports_first_frame": true,
    "supports_last_frame": true,
    "adaptive_aspect_ratio": true,
    "seconds": 10,
    "durations": [5, 10, 15],
    "default_duration": 10,
    "default_size": "1344x768",
    "fps": 24,
    "frames": 243,
    "steps": 9,
    "supports_references": false,
    "resolutions": [
      {"size":"832x480","label":"480p landscape","aspect_ratio":"832:480"},
      {"size":"480x832","label":"480p portrait","aspect_ratio":"480:832"},
      {"size":"1344x768","label":"768p landscape","aspect_ratio":"1344:768"},
      {"size":"768x1344","label":"768p portrait","aspect_ratio":"768:1344"}
    ],
    "estimated_cost_usd": {"warm": 0.16, "cold": 0.32},
    "available": true
  }]
}
```

## Create a video

### `POST /v1/videos`

JSON body:

| Field | Type | Notes |
|---|---|---|
| `model` | string | `fasth3-v2` (default) or `fasth3-omniref`; hidden preview models are not part of the supported workflow |
| `prompt` | string | 1–16000 chars, required |
| `size` | string | `auto` derives an H3-aligned canvas from the first/last/reference image; fixed resolutions are also accepted. Omitting it defaults to `auto` when an image is supplied, otherwise `1344x768` |
| `aspect_ratio` | string | Alternative to `size`, e.g. `832:480`, `1344:768` |
| `seconds` | number | `5`, `10` or `15`; default `10` |
| `seed` | integer | 0..2^31-1; random when omitted |
| `with_audio` | bool | Must be true; FastH3 always generates native audio |
| `input_reference` | string | First-frame image, data URI or public URL if enabled |
| `last_reference` | string | Optional last-frame image for first-and-last-frame generation |
| `enhance_prompt` | bool | Apply official H3-structure prompt enhancement; default true |
| `reference_images` | array/string | Ordered image references for `fasth3-omniref` (up to 9) |
| `reference_videos` | array/string | Ordered video references for `fasth3-omniref` (up to 3) |
| `reference_audios` | array/string | Ordered audio references for `fasth3-omniref` (up to 3) |
| `metadata` | object | Stored with the job; not sent to the model |

Multipart body: same fields. `input_reference` is a first-frame image;
`last_reference` adds the last frame. For OmniRef use repeated
`reference_images`/`reference_videos`/`reference_audios` parts (in order). Image
max 12 MB, video max 32 MB, audio max 16 MB.

Prompt enhancement is best-effort. If the configured endpoint is unavailable,
generation proceeds with the original prompt and the job metadata records the
error. `GET /v1/models` includes `prompt_enhancement_available`. A live
OpenCode Go call (`deepseek-v4.1-flash`, `x-opencode-session`) produced the
structured H3 prompt used by the quality-first route.

Response `201`:

```json
{
  "id": "video_ab12...",
  "object": "video",
  "model": "fasth3-v2",
  "status": "queued",
  "internal_status": "queued",
  "progress": 0,
  "stage": "queued",
  "seconds": 10.0,
  "size": "1344x768",
  "seed": 42,
  "has_audio": true,
  "cost_usd": 0.0,
  "reserved_usd": 0.3204,
  "content_url": null,
  "queue_position": 1,
  "prompt": "..."
}
```

Common errors: `invalid_api_key` 401, `unknown_model` 400, `invalid_duration`
400, `invalid_image` 400, `budget_exceeded` 429, `worker_not_configured` 503,
`rate_limited` 429.

## Prompt preview

### `POST /v1/prompts/enhance`

Accepts the same JSON or multipart fields as `POST /v1/videos` (including the
first/last/reference images). It validates the request but does not queue a job.
The response contains the selected `mode` (`t2va`, `i2va`, `fl2va`, `l2va` or
`ref2va`), the effective `width`/`height`, the original prompt and the enhanced
prompt. Use the edited prompt with `enhance_prompt: false` to bypass a second
enhancement. Images are downscaled and sent to the enhancer; video/audio
references are represented by their labels only.

```json
{
  "object": "prompt.enhancement",
  "mode": "i2va",
  "size": "768x1344",
  "applied": true,
  "model": "deepseek-v4.1-flash",
  "images_sent": 1,
  "original": "The woman turns toward the camera.",
  "enhanced": "For the target video, at 0.00 seconds ...",
  "error": null
}
```

## Regenerate HD

`POST /v1/videos/{id}/regenerate` is reserved for Open-H3-Regenerate-2K. It
currently returns HTTP 503 with code `regenerate_hd_unavailable`: the release
needs roughly 150 GiB GPU memory and a 164.9 GB checkpoint, while the worker has
96 GiB. The `/v1/models` response exposes `regenerate_hd_available` and
`regenerate_hd_reason`. See [`REGENERATE.md`](REGENERATE.md).

## Batch

### `POST /v1/videos/batch`

```json
{
  "model": "fasth3-v2",
  "label": "storyboard",
  "items": [
    {"prompt": "A fox in snow at dawn.", "size": "832x480"},
    {"prompt": "A robot watering a plant.", "seed": 7}
  ]
}
```

Max 20 items. The full worst-case reserve is checked before anything is queued.
Response is a list; each item has a `batch_id`.

### `GET /v1/batches/{id}`

Returns `total`, `status_counts`, `completed` (terminal count) and `videos`.

## Read / lifecycle

| Route | Result |
|---|---|
| `GET /v1/videos?limit=30&offset=0&status=completed&batch_id=...` | List |
| `GET /v1/videos/{id}` | Current object |
| `GET /v1/videos/{id}/content` | MP4, Range supported; `?download=1` attachment |
| `GET /v1/videos/{id}/thumbnail` | JPEG poster |
| `POST /v1/videos/{id}/cancel` | Idempotent; stops active GPU work |
| `DELETE /v1/videos/{id}` | Deletes files and soft-deletes the record |
| `POST /v1/videos/{id}/retry` | New billable variation with same prompt/seed/image |

`content_url` and `thumbnail_url` contain a short-lived HMAC token, so they work
directly in `<video>`/`<img>` tags without an Authorization header. Requesting
the video with a thumbnail token returns 401. A Bearer key may also fetch its
own media directly; another key's media returns 401 unless it presents the
capability token. Do not forward signed media URLs outside the intended user.

## Usage

### `GET /usage` (alias `GET /v1/usage`)

```json
{
  "object": "usage",
  "billing_cycle": {"start":"2026-11-01T00:00:00+00:00","end":"2026-12-01T00:00:00+00:00","day":1},
  "credits_usd": 30.0,
  "soft_budget_usd": 28.0,
  "reserve_usd": 2.0,
  "spent_usd": 1.23,
  "estimated_spend_usd": 1.23,
  "billed_usd": null,
  "billing_source": "estimated",
  "billing_error": "billing API unavailable",
  "is_admin": true,
  "reserved_usd": 0.3204,
  "remaining_usd": 26.58,
  "percent_used": 5.1,
  "projected_month_usd": 3.1,
  "jobs_this_cycle": 12,
  "completed_this_cycle": 11,
  "jobs_today": 3,
  "avg_cost_completed_usd": 0.089,
  "storage": {"bytes":12345678,"gb":0.012,"files":34,"max_gb":20.0},
  "backend": {"backend":"modal","ready":true,"demo":false,"gpu":"RTX-PRO-6000"},
  "worker": {"gpu":"RTX-PRO-6000","cpu":4,"memory_gib":96,"scaledown_seconds":10},
  "daily_spend": [{"day":"2026-11-17","total":0.45}],
  "ledger": [{"kind":"generation","entries":11,"total":1.23}]
}
```

## Admin

Admin routes require the **first** configured key (slot 0). Other keys receive
`403 forbidden`; Usage remains visible to all keys because the budget is shared.

| Route | Purpose |
|---|---|
| `POST /v1/admin/billing/sync` | Force a Modal billing sync |
| `POST /v1/admin/billing/reconcile` | `{"billed_usd":4.37,"note":"invoice"}` baseline |
| `GET /v1/admin/events` | Recent lifecycle events |
| `GET /v1/admin/diagnostics` | Worker, storage, DB, models, events |
| `POST /v1/admin/cleanup` | Retention + storage-cap cleanup now |

## Health

`GET /healthz` is public:

```json
{
  "status": "ok",
  "service": "video-gateway",
  "time": "2026-11-17T...",
  "backend": {"backend":"modal","ready":true,"demo":false},
  "budget_remaining_usd": 26.58
}
```

## Python client sketch

```python
import time, requests

BASE = "http://127.0.0.1:8787"
H = {"Authorization": "Bearer " + open("data/api_key").read().strip()}

job = requests.post(f"{BASE}/v1/videos", headers=H, json={
    "model": "fasth3-v2",
    "prompt": "A red panda in a bamboo forest, cinematic light.",
    "size": "832x480",
}).json()

while job["status"] not in {"completed", "failed", "cancelled"}:
    time.sleep(2)
    job = requests.get(f"{BASE}/v1/videos/{job['id']}", headers=H).json()

if job["status"] == "completed":
    # Add CF-Access-Client-Id/Secret here if Access protects the public origin.
    mp4 = requests.get(BASE + job["content_url"]).content
    open("clip.mp4", "wb").write(mp4)
else:
    raise RuntimeError(job["error"])
```
