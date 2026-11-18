# Video Gateway v1.0.0 — release notes

**Release date:** 2026-11-18

Video Gateway is a private, cost-aware AI video studio: an OpenAI-style
asynchronous API and a small web workbench in front of MiniMax H3 / FastH3 on
Modal. This is the first consolidated public release.

## Highlights

- **Quality-first FastH3 routes.** `fasth3-v2` for text, first-frame,
  first-and-last-frame and last-frame-only generation; `fasth3-omniref` for
  ordered image/video/audio references. Defaults are adaptive 768p and 10
  seconds, with 5/10/15 s available.
- **Native synchronized audio.** H3 generates audio with the video in the same
  diffusion pass. The gateway applies EBU R128 loudness normalization and
  encodes AAC 192 kb/s.
- **Full 36-block VAE** on public routes. The worker's H.264/yuv420p video is
  stream-copied; only audio is re-encoded. Non-H.264 sources use a CRF 16
  fallback.
- **Official H3 prompt structure.** The vendored prompt-writing guides drive
  automatic T2VA / I2VA / FL2VA / L2VA / Ref2VA modes. Prompt enhancement can
  be previewed, edited or bypassed; the original prompt is retained.
- **Adaptive canvas.** Image inputs select an aspect-preserving, 32-pixel
  aligned H3 canvas (for example 480×839 → 768×1344) instead of a forced 16:9
  crop.
- **Polished Studio, Batch, Library, Usage and API views.** Desktop and mobile
  layouts, first/last-frame pickers, live prompt preview, human-readable library
  prompts, batch duration selection and clearer budget/official-bill labeling.
- **Portable agent skill.** `skills/video-gateway/` includes the H3 workflow,
  route selection, prompt references, curl examples and a dependency-free
  Python client for OpenCode, Codex and similar agents.
- **Security and reliability hardening.** Bearer auth, Cloudflare Access service
  tokens, HMAC media URLs, upload validation, SSRF protections, no CORS, a
  loopback default, restart-safe job recovery and a conservative budget guard.

## Measured results

Measured on 2026-11-17 on the reference deployment: one RTX PRO 6000 (96 GB),
4 CPU, 96 GiB RAM, full 36-block VAE. Costs are estimates from measured worker
wall time plus the scaledown tail; they are not per-clip invoices.

| Route | Worker time | Est. cost | Peak VRAM |
|---|---:|---:|---:|
| V2 I2V 1344×768, 10 s | 191 s | $0.249 | 68.5 GiB |
| V2 I2V 1344×768, 15 s | 255 s | $0.318 | 71.7 GiB |
| V2 FL2VA 1344×768, 5 s | 58 s warm | $0.084 | 71.7 GiB |
| V2 I2V 768×1344, 5 s adaptive | 76 s | $0.119 | 56.9 GiB |
| OmniRef 1344×768, 10 s | 244 s | $0.307 | 78.8 GiB |
| OmniRef 1344×768, 15 s | 353 s | $0.426 | 85.2 GiB |

**Official cumulative Modal billing for the validation exercise:** $8.23
metered, $7.69 credits used, $0.00 billed. This includes earlier H3 experiments
and failed attempts; it is not a per-clip price. The existing $30 credits / $28
soft budget was unchanged and no spending limit was raised. Two later demo-only
10 s clips added about $0.35 to the gateway ledger and are outside this
validation total. Modal pricing and credit availability can change.

## Verification performed

- `pytest -q`: **41 passed**.
- `ruff check gateway worker tests scripts skills`: clean.
- `node --check gateway/static/app.js`: clean.
- Real-browser audit with Playwright on desktop (1440 px) and mobile (390 px):
  Studio, Batch, Library, Usage and API tabs; first/last-frame uploads; prompt
  preview/apply; library playback; disabled Regenerate HD; no page/console
  errors and no horizontal overflow.
- API smoke checks: unauthorized `/v1/*` and admin routes return 401, invalid
  media tokens return 401, no CORS headers, SSRF/path-traversal protections
  present, Range and attachment downloads verified.
- Deployment checks: `/healthz`, `/v1/models`, production service active,
  `modal container list` empty (no idle GPU containers), and the dependency-free
  agent client exercised against the live gateway, including real two-image
  first-frame I2V jobs for the public demo stills.

## Known limitations

- **Regenerate HD / 2K is disabled.** Open-H3-Regenerate-2K requires roughly
  150 GiB of GPU memory and a 164.9 GB checkpoint; the 96 GiB worker cannot
  admit it. The endpoint returns a clear 503 with `regenerate_hd_unavailable`.
- **OmniRef continuation is not exposed.** It needs overlap trim, audio mux and
  child-asset lifecycle work; see `docs/CONTINUATION.md`.
- **One GPU, one job at a time.** This is intentional for warm-cache reuse and
  predictable spending. Only the RTX PRO 6000 96 GB profile was validated.
- **Docker deployment is untested.** The systemd/Modal path is the verified
  reference deployment; the Docker files are convenience templates.
- **No 1080p mode.** The released 2K pipeline is fixed at 2560×1440 and silent;
  the gateway does not fake an upscale.
- **Prompt enhancement is optional and external.** It analyzes still images;
  video/audio references are represented by labels only.
- **Model licenses apply.** FastH3 QAD checkpoints are derivatives of MiniMax
  H3. Review `THIRD_PARTY_NOTICES.md` and the upstream MiniMax H3 Community
  License before deploying. The reference deployment's authorization does not
  grant rights to other users.

## Upgrade / install

For a fresh install or upgrade, see the [README](../README.md):

```bash
make dev
make test
make lint
```

For a GPU deployment, follow the Modal setup and `docs/OPERATIONS.md`. The
SQLite schema is created idempotently; no migration tool is required for the v1
schema. Back up `data/gateway.db` and `data/outputs/` before upgrading a
production instance.

## Short announcement

> I built Video Gateway, a small self-hosted studio for MiniMax H3 / FastH3
> video generation on Modal. It supports text, first-frame, first/last-frame
> and ordered-reference generation with native audio, adaptive 768p canvas and
> 5–15 second clips. One warm GPU, scale-to-zero, a $28 soft budget guard,
> crash-safe job recovery and a portable agent skill. The full validation,
> including measured costs and the 2K/continuation limitations, is documented in
> the repository.

## License

Gateway code and documentation: MIT. Model weights, prompt guides and sample
media: see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
