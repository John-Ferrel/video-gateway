# Benchmark and cost report — FastH3 / MiniMax H3

Measured on **2026-11-17** against the deployed service, not a mock.

The primary family is **FastH3 / MiniMax H3** on one RTX PRO 6000 Blackwell.
This report covers the routes supported by v1.0; earlier prototype benchmarks
for models that are no longer in the registry are not reproduced here.

## Environment

| Item | Value |
|---|---|
| Modal workspace | `your-modal-workspace` (Starter) |
| GPU | `RTX-PRO-6000` 96 GB, one container |
| Worker allocation | 4 CPU, 96 GiB RAM, `scaledown_window=10s`, `retries=0` |
| Models | `FastVideo/FastH3-QAD-v2` (63.3 GB), `FastVideo/FastH3-Omni-NVFP4-QAD` (108.6 GB), hidden `FastVideo/FastVideo-FastH3-Trim-8-Step-NVFP4` (35.4 GB); original `MiniMaxAI/MiniMax-H3` FL2VA downloaded for investigation, not served |
| Runtime | FastVideo main `c938a8aa2b856a594f791a61875ba0bdac5ef318`, PyTorch 2.12.0+cu130 |
| Kernels | `fastvideo-kernel` built from the same checkout for `sm_120a`; FlashInfer 0.7.4 |
| Acceleration | NVFP4 W4A4, VSA sparse attention, 8 distilled transformer forwards, full 36-block VAE |
| Output | 832×480, 480×832, 1344×768 or 768×1344; 24 fps; 124/243/362 frames (5/10/15 s); H.264 + AAC |
| Audio | Native synchronized stereo; H3 emits 96 kHz, gateway encodes AAC 192 kb/s and applies EBU R128 `loudnorm` |

The image build compiles the H3 FP4 prefix-split kernel (`KEY_LENGTH_SIZING`)
from source. The PyPI wheel predates that API, so it cannot serve the H3 NVFP4
path. Image builds run on Modal's build workers, not on a billed GPU.

## Quality-first I2V validation (current)

`worker total` is load + inference measured inside the function. Peak VRAM is
the maximum reported by the FastVideo stage logger, which is more accurate than
the worker's post-run counter (FastVideo resets between stages). All routes use
the full 36-block VAE.

| # | Model | Task | Load | Conditioning | Denoise | Decode | Worker total | Peak VRAM | Est. cost |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | `fasth3-v2` | I2V 1344×768, 10 s, first frame | 31.2 s | 52.1 s | 69.9 s | 36.5 s | **191.2 s** | 68.5 GiB | $0.249 |
| 2 | `fasth3-v2` | I2V 1344×768, 15 s, first frame | 25.8 s | ~2.5 s cache | 171.8 s | 54.6 s | **254.8 s** | 71.7 GiB | $0.318 |
| 3 | `fasth3-v2` | FL2VA 1344×768, 5 s, first + last | 0 s warm | 3.5 s | 34.8 s | 18.2 s | **57.6 s** | 71.7 GiB | $0.084 |
| 4 | `fasth3-omniref` | Ref2VA 1344×768, 10 s, 2 images | 25.6 s | 1.1 s cache | 194.6 s | 17.3 s | **244.2 s** | 78.8 GiB | $0.307 |
| 5 | `fasth3-omniref` | Ref2VA 1344×768, 5 s, 2 images | 25.7 s | — | — | — | **165.0 s** | 78.8 GiB | $0.218 |
| 6 | `fasth3-omniref` | Ref2VA 1344×768, 15 s, 2 images | 43.4 s | 0.2 s cache | 277.7 s | 25.7 s | **352.9 s** | 85.2 GiB | $0.426 |
| 7 | `fasth3-v2` | I2V 768×1344, 5 s, adaptive portrait | 21.1 s | — | — | — | **75.6 s** | 56.9 GiB | $0.119 |

All completed outputs are H.264 High, yuv420p, 24 fps, 1344×768, with AAC-LC
96 kHz stereo audio. File sizes are 11–13 MB for 10–15 s. The 10 s and 15 s runs
both completed without OOM on the 96 GB card; 15 s peaked at 71.7 GiB and
OmniRef 10 s at 78.8 GiB.

### Quality notes

- **V2 10 s / 15 s first-frame I2V:** frame 0 is the exact supplied image. The
  character keeps her face, dark bob hair and red palette through 15 s; the
  market background, rain and reflections stay coherent. The model reinterprets
  wardrobe details (the reference button shirt becomes a red coat/hoodie), and
  the first second can establish a new background with a visible cut. No
  temporal collapse or face swap was observed in the sampled frames.
- **First-and-last-frame:** frame 0 and the final frame are exact anchors; the
  generated middle keeps the red-coat character. A deliberately different green
  last frame is reached at the end, proving FL2VA conditioning.
- **OmniRef 10 s and 15 s:** with the official subject/`<Picture N>` prompt
  structure, the red and green women remain distinct and keep their outfits,
  faces and relative positions through 9.5 s and 14.5 s. A vague "both
  characters" prompt collapsed both to red and added an unrequested man; that
  completed run is rejected for reference fidelity, not counted as a quality
  result. OmniRef 15 s peaks at 85.2 GiB, leaving about 10.8 GiB of headroom.
- **Style:** OmniRef keeps the flat cutout look of the supplied reference art
  against a rendered market background. That is reference fidelity, not a
  rendering failure; use a photographic reference if a photographic result is
  wanted.
- **Adaptive portrait:** a 480×839 uploaded frame resolved to 768×1344 (the
  native portrait grid), not a cropped 16:9 canvas. The first frame stayed
  exact and the 5 s render cost $0.119. The H.264 video stream was stream-copied
  by FFmpeg, so no CRF 20 re-encode was applied.
- **Prompt enhancement:** the first live enhanced I2V run over-invented a
  photorealistic mansion and restyled the anime reference. The system prompt was
  tightened to treat the supplied frame as the visual source of truth; a second
  live run preserved the anime/cutout character, red outfit and neon market.
  The enhanced 5 s/480p job cost $0.100. Enhancement is best-effort and the
  original prompt is retained in metadata.
- **Audio:** native 96 kHz stereo; no separate TTS or mux stage. The gateway
  normalizes loudness to -16 LUFS and encodes AAC 192 kb/s.

### Checkpoint compatibility decision

The original `MiniMaxAI/MiniMax-H3` release is a 33B dense transformer with
full attention only; the official sparse-attention implementation is not in the
open-source release. Its FL2VA/Ref2VA stack is ~144 GB on disk and the reference
example samples 50 steps. That is not practical at 768p/10–15 s on one 96 GB
RTX PRO 6000, so it is retained only as an experimental, non-public worker
profile. FP8/FastH3-Trim variants are likewise hidden; the practical quality
routes are the FastH3 QAD NVFP4 checkpoints with VSA sparse attention and the
full VAE.

The V2 QAD contract is labelled T2V, but the FL2VA pipeline accepts `pil_image`
and `last_image`; this was verified with real 768p renders. It is not assumed
from the contract.

### Earlier 5 s matrix

These runs established the warm/cold behaviour and the cache. They remain
useful for cost comparison, but the quality-first routes above supersede them
for the primary I2V use case.

| # | Model | Task | Load | Inference | Worker total | Peak VRAM | Est. cost |
|---|---|---|---:|---:|---:|---:|---:|
| 1 | `fasth3-v2` | T2V 832×480, cold container, new prompt | 23.7 s | 83.9 s | 107.7 s | 36.4 GiB | $0.155 |
| 2 | `fasth3-v2` | T2V 832×480, warm, same prompt | 0 s | 15.9 s | **15.9 s** | 36.9 GiB | **$0.038** |
| 3 | `fasth3-v2` | T2V 1344×768, cold container, cached prompt | 17.9 s | 100.8 s | 118.7 s | 43.6 GiB | $0.168 |
| 4 | `fasth3-omniref` | Ref2VA 832×480, two ordered images, fast decoder | 32.3 s | 139.9 s | 172.2 s | 62.1 GiB | $0.227 |
| 5 | `fasth3-trim` | T2V 832×480, cold (now hidden) | 34.3 s | 76.0 s | 110.4 s | 34.7 GiB | $0.156 |

## VAE, quantization and encoding audit

- **VAE:** both public routes use the full 36-block video VAE. The 8L/NVFP4
  decoder is not selected for V2 or OmniRef; `vae_decoder=None` and
  `FASTVIDEO_H3_VAE_NVFP4_DECODER` is removed. Tiling is a memory strategy and
  does not change the latent space.
- **Quantization:** V2 and OmniRef use the QAD NVFP4 W4A4 student with VSA and
  the matching FP8 prefix split. No extra post-hoc quantization is applied.
- **FFmpeg:** the worker's H.264/yuv420p video stream is now **stream-copied**
  unchanged; only audio is loudness-normalized and re-encoded to AAC 192 kb/s
  (96 kHz preserved). Non-H.264 sources fall back to libx264 `preset slow`,
  CRF 16. This removes the previous CRF 20 generation loss for normal H3 output.
- **Adaptive canvas:** a first/last/reference image selects an aspect-preserving
  canvas snapped to 32 px and capped at the 768p area (for example
  832×480 → 1344×768, 480×839 → 768×1344, 1024×768 → 1024×768). No forced 16:9
  crop.
- **Regenerate HD:** `POST /v1/videos/{id}/regenerate` is gated off. The
  Open-H3-Regenerate-2K release is 164.9 GB on disk and needs roughly 150 GiB
  GPU; the 96 GiB worker cannot admit it. No 2K cost was invented.
- **Continuation:** upstream OmniRef supports 5/22/39-frame continuation clips,
  but a correct gateway action needs overlap trim, audio mux and a child-asset
  lifecycle; it remains a documented next step.

## Actual Modal billing

Official `modal billing summary` after the quality-first validation:

```
Metered Cost:                8.23
  Deployed Apps:             7.44
  Network Egress:            0.54
  Ephemeral Apps:            0.25
  Network Egress Allowance: -0.54
  Credits:                  -7.69
  Billed Cost:              $0.00
```

- The cumulative figure includes earlier H3 experiments, image builds, model
  preparation, failed attempts and this quality-first validation. It is **not**
  a per-clip price.
- It is within the existing $28 soft budget / $30 credits; no spending limit
  was raised. Credits covered the spend; out-of-pocket is **$0.00**.
- At the time of the measurement, `modal app stats` reported 3 `generate`
  calls in the deployment window, all completed, 3 containers, 0 live.
- The gateway ledger is deliberately conservative; it syncs the official
  billing API when available.

### Monthly capacity at the $28 soft cap

| Route | Measured cost | Clips / $28 |
|---|---:|---:|
| V2 5 s warm / repeat | $0.038 | ~730 |
| V2 480p cold prompt | $0.155 | ~180 |
| V2 768p 10 s | $0.249 | ~112 |
| V2 768p 15 s | $0.318 | ~88 |
| OmniRef 768p 10 s | $0.307 | ~91 |
| OmniRef 768p 15 s | $0.426 | ~65 |

Quality-first 10–15 s work is naturally more expensive than the earlier 5 s
matrix. A mixed workload of 10 s I2V with some repeats lands around
**90–150 clips/month** within $28.

## Findings and adjustments

1. **True I2V works on the QAD checkpoint.** The FL2VA path preserves frame 0
   exactly and produced stable 10 s and 15 s 768p clips. The earlier assumption
   that a T2V-labelled distilled checkpoint could not do I2V was too pessimistic.
2. **Full VAE fits.** The 36-block VAE raises peak memory to 71.7 GiB for V2
   15 s, 78.8 GiB for OmniRef 10 s and 85.2 GiB for OmniRef 15 s, still within
   96 GB. The pruned/8L decoder is no longer used by the public routes.
3. **First-and-last-frame works.** Both endpoints are anchored; the middle is
   generated. This is the FL2VA mode the original H3 release also supports.
4. **Prompt structure matters for OmniRef.** The official subject definitions
   and `<Picture N>` labels preserve distinct red/green characters; a vague
   prompt collapsed them. The prompt enhancer is designed to add this structure.
5. **OmniRef 15 s fits, but with the tightest margin.** 85.2 GiB reserved
   leaves ~10.8 GiB on the 96 GB card; it is the ceiling route for this GPU.
6. **The original 33B H3 is not practical here.** Full attention + 50 steps +
   144 GB is a multi-GPU/B200 workload, not a single 96 GB card at 10–15 s.
   FastH3 QAD NVFP4 with VSA is the practical quality choice.
7. **15 s needs no special mode.** 362 frames completed at 71.7 GiB; the main
   cost is latency (255 s worker total), not memory.
8. **Audio is native.** 96 kHz stereo is generated with the video; the gateway
   only normalizes loudness and re-encodes to AAC.
9. **Trim is hidden.** It is a smaller/faster checkpoint but not a quality
   route; the API model list no longer advertises it.

## Reproduction

```bash
# Check the deployed worker and model Volume
modal run worker/modal_app.py --action status

# Deploy the worker
modal deploy worker/modal_app.py

# Restart the gateway on the host
systemctl --user restart video-gateway

# One 768p 10 s first-frame I2V clip
curl -sS http://127.0.0.1:8787/v1/videos \
  -H "Authorization: Bearer $(cat data/api_key)" \
  -F model=fasth3-v2 \
  --form-string 'prompt=The woman in the red coat turns toward the camera and smiles; rain and city ambience.' \
  -F size=1344x768 -F seconds=10 \
  -F input_reference=@tests/fixtures/anime_reference.png

# First-and-last-frame
curl -sS http://127.0.0.1:8787/v1/videos \
  -H "Authorization: Bearer $(cat data/api_key)" \
  -F model=fasth3-v2 --form-string 'prompt=Walk from the first frame to the last frame.' \
  -F size=1344x768 -F seconds=5 \
  -F input_reference=@first.png -F last_reference=@last.png

# OmniRef 10 s with ordered references
curl -sS http://127.0.0.1:8787/v1/videos \
  -H "Authorization: Bearer $(cat data/api_key)" \
  -F model=fasth3-omniref \
  --form-string 'prompt=<Picture 1> and <Picture 2> walk together through a neon market.' \
  -F size=1344x768 -F seconds=10 \
  -F reference_images=@tests/fixtures/anime_reference.png \
  -F reference_images=@tests/fixtures/anime_reference_b.png

```

Machine-readable H3 results: [`reports/benchmark-h3.json`](../reports/benchmark-h3.json).
