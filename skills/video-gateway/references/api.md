# Video Gateway API quick reference

Base URL: `$VIDEO_GATEWAY_BASE_URL` (for example `https://video.example.com`).
All `/v1` routes require `Authorization: Bearer $VIDEO_GATEWAY_API_KEY`.
When Cloudflare Access is enabled, add `CF-Access-Client-Id` and
`CF-Access-Client-Secret` as outer headers. Access is not a Bearer substitute.

## Capabilities

`GET /v1/models` returns model IDs, default size/duration, durations, resolutions,
`adaptive_aspect_ratio`, prompt-enhancement availability and estimated costs.
Use `fasth3-v2` for T2V/I2V/FL2VA and `fasth3-omniref` for references.

## Create

`POST /v1/videos` accepts JSON or multipart.

| Field | Notes |
|---|---|
| `model` | `fasth3-v2` (default) or `fasth3-omniref` |
| `prompt` | 1–16000 chars |
| `size` | `auto` or e.g. `1344x768`; auto follows the uploaded image |
| `seconds` | `5`, `10` or `15`; default 10 |
| `seed` | 0..2^31-1, random if omitted |
| `enhance_prompt` | default true; set false to use the prompt exactly |
| `input_reference` | first-frame image |
| `last_reference` | last-frame image (V2) |
| `reference_images` | ordered OmniRef image refs (up to 9) |
| `reference_videos` | ordered OmniRef video refs (up to 3) |
| `reference_audios` | ordered OmniRef audio refs (up to 3) |

Multipart file limits: image 12 MB, video 32 MB, audio 16 MB. Images are
decoded and normalized server-side. OmniRef requires at least one image/video.

## Prompt preview

`POST /v1/prompts/enhance` takes the same fields and returns `mode`, `size`,
`original`, `enhanced`, `applied`, `model`, `images_sent`, `error`. It does not
queue a GPU job. If accepted/edited, submit with `enhance_prompt=false`.

## Status / media

- `GET /v1/videos/{id}` — progress, stage, cost, usage, content/thumbnail URLs.
- `GET /v1/videos/{id}/content?token=...` — MP4; supports `Range`.
- `GET /v1/videos/{id}/thumbnail?token=...` — poster.
- `POST /v1/videos/{id}/cancel` — queued/running job.
- `POST /v1/videos/{id}/retry` — explicit new billable job.
- `DELETE /v1/videos/{id}` — delete a finished record/files.

## Errors

OpenAI-style `{"error":{"message","type","param","code"}}`.

Common: `invalid_api_key` 401, `missing_prompt` 400, `unknown_model` 400,
`invalid_duration` 400, `invalid_reference` 400, `unsupported_reference` 400,
`file_too_large` 413, `budget_exceeded` 429, `rate_limited` 429,
`worker_not_configured` 503, `regenerate_hd_unavailable` 503.

Do not retry a 429 or a failed paid job unless the user explicitly asks.
