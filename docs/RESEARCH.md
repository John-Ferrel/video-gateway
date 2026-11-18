# Research, licensing and architecture decisions

Checked **2026-11-17**. Prices, model capabilities and licenses change; re-check
before a major deployment. This document records why the v1.0 reference profile
looks the way it does; it is not a claim that every alternative was exhausted.

## 1. Hardware and platform

| Item | Value |
|---|---|
| GPU | Modal `RTX-PRO-6000`, 96 GB, sm_120 Blackwell |
| Price | **$0.000842/s** = $3.0312/h ([Modal pricing](https://modal.com/pricing)) |
| CPU | **$0.0000131/core/s**; request is 4 cores |
| RAM | **$0.00000222/GiB/s**; request is 96 GiB |
| Volume | **$0.09/GiB/month**, first 1 TiB free |
| Egress | **$0.04/GiB**, first 1 TiB/month free |
| Plan | Modal Starter, **$30/month free credits** at the time of measurement, scale-to-zero |

All-in rate for this worker at the measured prices:

```
GPU  $0.00084200 /s
CPU  4 × $0.00001310 = $0.00005240 /s
RAM  96 × $0.00000222 = $0.00021312 /s
                        ------------
                        $0.00110696 /s  =  $3.9851 /h  =  $0.06642 /min
```

Consequences:

- The 10-second `scaledown_window` tail costs about **$0.0111** per job even
  when the next job never arrives. It is deliberately short.
- A measured 191 s 768p 10 s V2 job costs about **$0.249** all-in. Cold model
  loading and the full VAE decode are the dominant fixed costs.
- CPU/RAM is roughly a quarter of the bill. We do not request 8 CPUs or 128 GiB
  "just in case"; 96 GiB is used for H3 checkpoint staging and host-side
  handling, while the GPU peak is the binding constraint.
- V2 (63.3 GB) + OmniRef (108.6 GB) + hidden Trim (35.4 GB) is about **207 GB**
  on a Volume, inside the free 1 TiB tier.
- Returning 5–20 MB MP4s from the gateway is far below the free egress tier.
- Image builds are not charged as GPU time; the image is cached by Modal.

## 2. Model decision: FastH3 / MiniMax H3

The reference deployment's use of MiniMax H3 was authorized by the project owner
for that specific environment. **This public repository does not grant model
rights to anyone else.** The MiniMax H3 Community License defines an Applicable
Territory that excludes the European Union, the United Kingdom, the Republic of
Korea and the United States of America, and includes acceptable-use,
attribution and commercial terms. Deployers must review the license and obtain
their own authorization where required. See
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

The primary family is:

- **`fasth3-v2` — FastH3 V2 NVFP4 QAD** (default). Eight transformer forwards,
  NVFP4 W4A4 on attention/MLP/VSA gates, NVFP4 Qwen3-VL text encoder, full
  36-block VAE, native synchronized audio. 5/10/15 s at 480p/768p, first-frame
  and first-and-last-frame I2V. QAD is trained against the BF16 teacher for the
  deployed 4-bit forward pass; it is preferred over a post-hoc quantized export
  at the same inference cost.
- **`fasth3-omniref` — FastH3 OmniRef NVFP4 QAD**. Eight-forward Ref2VA with
  ordered image, video and audio references, full VAE, 5/10/15 s. Best for
  character/style/reference fidelity.
- **`fasth3-trim` — FastH3 Trim NVFP4**. Pruned 8-step preview, **hidden** from
  the public model list. It is a draft tool, not a quality route.
- **`h3-fl2va` — original MiniMax H3 FL2VA** (experimental, not public). The
  33B dense, full-attention-only release is retained only for investigation; it
  is not practical at 768p/10–15 s on one 96 GB GPU.

### Checkpoint compatibility, not assumptions

The FastH3 V2 contract is labelled T2VA, and the original FastVideo docs call it
"T2AV-only." The FL2VA pipeline nevertheless accepts `pil_image` and
`last_image`, so we tested it instead of assuming: real 1344×768 renders at 5 s,
10 s and 15 s preserve frame 0 exactly and keep the character coherent.
First-and-last-frame conditioning anchors both endpoints. The QAD student is
therefore used for the primary I2V route.

The original `MiniMaxAI/MiniMax-H3` FL2VA/Ref2VA release is a 33B dense
transformer with full attention only; the official sparse-attention
implementation is not in the initial open-source release. Its FL2VA stack is
~144 GB on disk and the reference examples use 50 steps. FP8 community exports
reduce bytes but do not remove the full-attention/50-step cost. That is a
multi-GPU/B200-class workload, not a single 96 GB card at 10–15 s, so the
practical quality choice is FastH3 QAD NVFP4 with VSA sparse attention and the
full VAE. The hidden Trim and other pruned variants stay out of the quality
workflow.

Open-H3-Regenerate-2K is a separate 2K video-to-video refine stack that needs
roughly 150 GB with the DiT, 2K activations and VAE; the release is 164.9 GB on
disk. The gateway exposes a gated `POST /v1/videos/{id}/regenerate` endpoint that
returns `regenerate_hd_unavailable` with the memory requirement; it is not
enabled on the 96 GB card. The upstream 2K output is silent, so preserving the
original audio would need an additional mux/concat step. OmniRef continuation is
investigated in [`CONTINUATION.md`](CONTINUATION.md); it is not exposed until
overlap trim and child-asset lifecycle are implemented.

### Prompt structure and adaptive canvas

The enhancer vendors the official
[`h3-prompt-writing`](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing)
guides and selects `base-en.txt` for T2VA/I2VA/FL2VA/L2VA or `ref-en.txt` for
OmniRef. It sends downscaled first/last/reference images to an optional
OpenAI-compatible vision-capable chat endpoint, so the rewrite can see style,
identity and background. The studio exposes preview, edit and bypass. Dialogue,
lyrics and on-screen text are preserved in their original language; a one-shot
rule keeps short clips temporally coherent unless the user asks for cuts.
Video/audio references are passed as labels in the text prompt, not as pixels or
audio, so their fine detail is not analyzed by the enhancer.

Image-conditioned requests can select `size=auto`. The gateway computes an
aspect-preserving H3 canvas (short edge 768, area cap 768×1344, 32 px grid) from
the uploaded frame. This supports 16:9, 9:16, 4:3, 3:4, 1:1 and 21:9 without a
forced crop. Explicit resolutions still override it. A 480×839 upload resolved
to 768×1344 and rendered a real 5 s portrait I2V at $0.119 with an exact first
frame.

### FastVideo/runtime

The image installs FastVideo from pinned main commit
`c938a8aa2b856a594f791a61875ba0bdac5ef318` (2026-11-15), plus a
`fastvideo-kernel` build from the same checkout for `sm_120a`. The PyPI kernel
wheel predates the H3 FP4 prefix-split API; building from source is required for
native NVFP4/VSA, not optional. The build runs during image creation, so no GPU
is allocated for compilation.

### FastH3 published performance vs measured 10–15 s

FastVideo's RTX PRO 6000 notes report 10.1 s 1344×768 clips with the 4-step
NVFP4 preview at **41–42 s** and the 8-step V2 at **86.5 s**, with an 80 GB peak
for the 4-step stack. Those are optimized planning numbers. On the reference
single RTX PRO 6000, the quality-first 768p I2V measurements are 191 s for 10 s
and 255 s for 15 s, with 68–72 GiB peak; OmniRef 10 s is 244 s at 78.8 GiB.
The extra time is the full VAE, 8-step denoise and the cold/warm conditioning
split, not a regression. Full stage timings are in
[`BENCHMARK.md`](BENCHMARK.md).

## 3. Alternatives considered

The main requirement was a single-GPU, quality-first I2V/T2V service with native
audio, a small gateway and no always-on GPU. FastH3 QAD with VSA fit that
requirement without a second serving stack.

ComfyUI was evaluated as an escape hatch, not the primary runtime:

- A ComfyUI workflow encodes the graph in JSON and requires custom-node and
  model-management layers; the native FastVideo API is a smaller surface.
- FastVideo exposes a typed `GenerationRequest` and returns `video_path`, timing
  and peak memory directly, which makes cost accounting straightforward.
- The FastH3 V2/OmniRef/Trim recipes are maintained upstream in FastVideo with
  the exact NVFP4, VSA and distilled sampling contracts.
- A persistent ComfyUI server has a larger idle/startup surface; the worker here
  loads one model lazily and exits.

If FastH3 support regresses, `modal-comfyui` plus the official MiniMax H3
ComfyUI nodes remains a documented escape hatch, subject to the same model
license. Other video families were not adopted for v1.0; this is not a claim
that they are unsuitable for every use case.

## 4. GPU lifecycle and cost control

- **`min_containers=0`, `max_containers=1`, `buffer_containers=0`.** No idle GPU,
  no parallel containers. One job at a time, serialized in the gateway.
- **`scaledown_window=10`.** Long enough to submit the next batch item, short
  enough that idle cost is cents. A batch pays one model load, not N.
- **`retries=0`.** Modal retries are disabled. The gateway also never retries a
  submission. A job whose call id was lost is marked failed; a manual "Try
  again" creates a new billable job and is explicit.
- **CPU pre-download.** `prepare()` runs with no GPU, 2 CPUs, 8 GiB, and writes
  the checkpoints to a Volume. The GPU function never downloads weights.
- **Model cache in the container.** A module-level `_GENERATOR_CACHE` keeps the
  warm model across inputs. Switching V2→OmniRef→Trim reloads; batches should
  group by model. The exact prompt-embeds cache and FlashInfer workspace are on
  the cache Volume, so a repeated presentation can be a cache hit even after
  scale-to-zero.
- **No automatic GPU fallback.** The GPU string is fixed. If RTX PRO 6000
  capacity is unavailable, the job waits or fails rather than silently moving
  to a more expensive GPU.
- **Budget guard.** Reserve the cold worst case at queue time, release on
  completion/failure/cancel, and stop admission when the remaining soft budget
  is below the estimate. Default is a $28 soft cap of $30 credits, leaving a $2
  reserve.
- **Billing data.** `Workspace.billing.summary()` worked on the reference
  workspace. The gateway syncs it every 15 minutes and falls back to a
  conservative ledger if a plan does not expose it. The UI displays the source
  and allows manual invoice reconciliation.
- **Memory snapshots.** Implemented behind `WORKER_GPU_SNAPSHOT=1`, off by
  default. Modal's own docs say snapshots do not speed up loading weights from
  storage, which is the dominant cold-start cost here.
- **No Redis/Celery/Kubernetes.** SQLite WAL + one dispatcher thread is enough
  for a single-user gateway and survives restarts.

## 5. Resource sizing

| Resource | Value | Why |
|---|---|---|
| GPU | RTX PRO 6000 96 GB | V2 10 s peaks 68.5 GiB, V2 15 s 71.7 GiB, OmniRef 10 s 78.8 GiB, OmniRef 15 s 85.2 GiB; NVFP4 tensor cores and 96 GB headroom |
| CPU | 4 | VAE decode, audio decode, FFmpeg mux; avoids starved data loading |
| RAM | 96 GiB | Checkpoint staging and host-side handling; OmniRef is 108.6 GB on disk. The GPU peak is the binding constraint at 85.2 GiB |
| Volume | models + cache + results | 1 TiB free; persistent across deploys; cache holds FlashInfer JIT and prompt embeddings |
| Disk | 20 GB gateway cap | 10–15 s 768p clips are 11–13 MB; 30-day retention |

H3 V2 is 63.3 GB on disk and 36–72 GiB resident depending on duration; OmniRef
is 108.6 GB on disk and 85.2 GiB resident at 15 s/768p. The 96 GB card is
justified by 15 s OmniRef, not by 480p alone.

## 6. What the quality-first H3 validation proved

Measured 2026-11-17 on the reference RTX PRO 6000 (full report:
[`BENCHMARK.md`](BENCHMARK.md)):

1. **V2 768p 10 s I2V:** 31.2 s load + 159.9 s inference = 191.2 s worker total,
   $0.249, 68.5 GiB peak. Frame 0 is the exact supplied image; the character
   stays coherent through 10 s.
2. **V2 768p 15 s I2V:** 25.8 s load + 229.0 s inference = 254.8 s, $0.318,
   71.7 GiB peak. 362 frames complete without OOM; denoise 171.8 s, VAE decode
   54.6 s.
3. **First-and-last-frame:** 5 s, 57.6 s warm, $0.084. Both endpoints are exact
   anchors; the generated middle keeps the character.
4. **OmniRef 768p 10 s:** 25.6 s load + 218.6 s inference = 244.2 s, $0.307,
   78.8 GiB peak. With the official subject/`<Picture N>` prompt structure the
   red and green references stay distinct through 9.5 s.
5. **OmniRef 768p 15 s:** 43.4 s load + 309.5 s inference = 352.9 s, $0.426,
   85.2 GiB peak. The same two references stay distinct through 14.5 s; this is
   the tightest memory route, with ~10.8 GiB headroom.
6. **Prompt structure is not optional for OmniRef.** A vague "both characters"
   prompt collapsed both references to red and added an unrequested man. The
   same 10 s route with explicit subject definitions and picture labels was
   faithful. The prompt enhancer is designed to add that structure.
7. **Original 33B H3 is not practical here.** Full attention, 50 steps and a
   144 GB FL2VA stack do not fit the single-GPU 10–15 s use case. FP8 variants
   reduce bytes but not the full-attention/step cost.
8. **Full VAE is affordable.** The 36-block VAE raises peak memory to 71.7 GiB
   for V2 15 s, 78.8 GiB for OmniRef 10 s and 85.2 GiB for OmniRef 15 s, still
   inside 96 GB. The public routes no longer use the 8L/NVFP4 decoder.
9. **Billing:** cumulative official metered **$8.23** for the whole exercise,
   including earlier H3 experiments and prototype work. This is not a per-clip
   price. The existing $28 soft budget was not changed; credits covered it and
   out-of-pocket was $0.
10. **Audio:** native 96 kHz stereo in the same diffusion pass; the gateway
    normalizes loudness and encodes AAC 192 kb/s.

The earlier 5 s matrix remains useful for warm/cold cost: V2 480p warm is
15.9 s/$0.038 and cold is 107.7 s/$0.155. It is retained in
[`BENCHMARK.md`](BENCHMARK.md).

## References

- Modal pricing: <https://modal.com/pricing>
- Modal resources/billing docs: <https://modal.com/docs/guide/resources>,
  <https://modal.com/docs/guide/billing>, <https://modal.com/docs/guide/budgets>
- Modal cold start / scale: <https://modal.com/docs/guide/cold-start>,
  <https://modal.com/docs/guide/scale>
- FastVideo: <https://github.com/hao-ai-lab/FastVideo>
- FastVideo RTX PRO 6000 FastH3 notes:
  <https://github.com/hao-ai-lab/FastVideo/blob/main/docs/inference/fasth3_rtx_pro_6000.md>
- MiniMax H3: <https://github.com/MiniMax-AI/MiniMax-H3>
- MiniMax H3 Community License:
  <https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE>
- ComfyUI: <https://github.com/Comfy-Org/ComfyUI>
- modal-comfyui: <https://github.com/caru-ini/modal-comfyui>
