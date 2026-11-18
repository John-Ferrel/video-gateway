# Regenerate HD / Open-H3-Regenerate-2K evaluation

Status: **gated and disabled by default**. The current RTX PRO 6000 worker has
96 GiB of GPU memory; the upstream pipeline is documented for a GB200/B200-class
GPU with roughly **150 GiB** resident.

## Upstream shape

- Pipeline: Open-H3-Regenerate-2K (FastVideo port).
- Draft: 1344×768, 121 frames, 24 fps.
- Output: 2560×1440 (fixed), 121 frames; the released refine path is silent.
- Refine: latent upscaler + 3 Euler steps (a 2-step variant exists).
- Weights: `Efficient-Large-Model/Open-H3-Regenerate-2K` is **164.9 GB** on
  disk, including a base MiniMax-H3 copy, the merged LoRA, the latent upscaler,
  sharp-curve branches and a 24-layer fast VAE.
- Reuse: the draft latent can be passed straight into the upscaler; FastVideo
  also supports the NVFP4 decoder and fast 8L/24L decoder on Blackwell.

## Why it is not enabled

The documented ~150 GiB peak (BF16 DiT + 2K activations + VAE) exceeds the
96 GiB card by a wide margin. FP8/NVFP4 and layerwise offload might reduce the
DiT, but 2K activations and the fixed 2560×1440 decode remain; this is not a
safe single-GPU configuration. Running it would also require a larger Modal GPU,
which would change the existing infrastructure and spending envelope.

No paid 2K run was attempted just to force an OOM. The endpoint
`POST /v1/videos/{id}/regenerate` is present but returns HTTP 503 with code
`regenerate_hd_unavailable` and the measured requirement. Set
`REGENERATE_HD_ENABLED=true` only after a larger GPU profile and a real memory
test are available; a worker profile is not configured yet.

## 1080p / audio

The released pipeline is fixed at 2560×1440, not 1080p. A 1080p target would
need a different upscaler/refine configuration, which the release does not
provide. The upstream 2K output is also **silent**; preserving the original
audio would require a gateway mux/concat step after refine. Both are left
unimplemented rather than shipping a fake upscale.

## Cost

No 2K run was made, so there is no invented cost number. On the current
96 GiB worker the job cannot be admitted safely. Any future enablement should
measure the real B200/GB200 wall time and memory first, then reconcile the
official Modal bill against the existing $28 soft budget.
