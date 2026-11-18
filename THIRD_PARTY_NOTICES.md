# Third-party notices

This repository contains **software**, **documentation**, screenshots and small
test images, not model weights, datasets, CUDA binaries or FFmpeg binaries. The
gateway code is MIT licensed; the models, prompt guides and sample media carry
their own terms. This file is a practical attribution list, not legal advice.

The export was audited for credentials and deployment identifiers before the
v1.0.0 public commit. That audit cannot grant rights to third-party material;
the sections below identify the upstream terms that still apply.

## 1. Gateway code

Copyright (c) 2026 Video Gateway contributors.

Licensed under the MIT License. See [LICENSE](LICENSE).

## 2. MiniMax H3 / FastH3 model weights

No model weights are redistributed in this repository. The worker downloads
them from Hugging Face into a Modal Volume when you run
`modal run worker/modal_app.py --action prepare`.

| Asset | Source | Terms |
|---|---|---|
| MiniMax H3 base model | <https://huggingface.co/MiniMaxAI/MiniMax-H3> | [MiniMax H3 Community License Agreement](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE) |
| FastH3 V2 NVFP4 QAD | <https://huggingface.co/FastVideo/FastH3-QAD-v2> | Model card marks the license as `other`; it is a derivative of MiniMax H3. Treat it as subject to the MiniMax H3 Community License. |
| FastH3 OmniRef NVFP4 QAD | <https://huggingface.co/FastVideo/FastH3-Omni-NVFP4-QAD> | Model card identifies the MiniMax H3 Community License. |
| FastH3 Trim NVFP4 (hidden) | <https://huggingface.co/FastVideo/FastVideo-FastH3-Trim-8-Step-NVFP4> | Derivative of MiniMax H3; same caution as above. |
| Open-H3-Regenerate-2K (gated off) | <https://huggingface.co/Efficient-Large-Model/Open-H3-Regenerate-2K> | Model card identifies the MiniMax H3 Community License. |

**Important restrictions.** At the time of writing, the MiniMax H3 Community
License defines an “Applicable Territory” of worldwide **excluding the European
Union, the United Kingdom, the Republic of Korea and the United States of
America**, and it includes acceptable-use, attribution, redistribution and
commercial-revenue terms. The license text controls; read it in full before
downloading, deploying or redistributing the weights.

The reference deployment described in this repository was authorized by the
project owner for that specific environment. **That statement does not grant
authorization or rights to any other user.** If you deploy in a restricted
territory, obtain your own authorization from MiniMax and comply with the
license, the Acceptable Use Policy and applicable law.

## 3. Vendored H3 prompt-writing guides

`gateway/prompts/h3/base-en.txt` and `gateway/prompts/h3/ref-en.txt` are
vendored from the official MiniMax H3
[`h3-prompt-writing`](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing)
skill. They are included to keep prompt construction deterministic and
reviewable. The upstream MiniMax H3 Community License applies to these texts;
the repository's MIT license does not override it. Public redistribution of the
guides is therefore subject to that license and its territorial/commercial
terms. See `gateway/prompts/h3/README.md`.

The portable agent skill in `skills/video-gateway/` is original integration
code and documentation; it links to, rather than replaces, the upstream guides.

## 4. Runtime and development dependencies

The gateway and worker install these packages at install/build time. They are
not vendored as source in this repository. If you redistribute a built Docker
image, include the license texts shipped in that image.

| Component | License | Link |
|---|---|---|
| FastAPI | MIT | <https://github.com/fastapi/fastapi> |
| Starlette | BSD-3-Clause | <https://github.com/encode/starlette> |
| Uvicorn | BSD-3-Clause | <https://github.com/encode/uvicorn> |
| HTTPX | BSD-3-Clause | <https://github.com/encode/httpx> |
| Modal client | Apache-2.0 | <https://github.com/modal-labs/modal-client> |
| Pillow | HPND / MIT-CMU | <https://python-pillow.org> |
| python-multipart | Apache-2.0 | <https://github.com/Kludex/python-multipart> |
| imageio-ffmpeg | BSD-2-Clause | <https://github.com/imageio/imageio-ffmpeg> |
| pytest (dev) | MIT | <https://github.com/pytest-dev/pytest> |
| pytest-asyncio (dev) | Apache-2.0 | <https://github.com/pytest-dev/pytest-asyncio> |
| Ruff (dev) | MIT | <https://github.com/astral-sh/ruff> |
| Playwright (dev) | Apache-2.0 | <https://github.com/microsoft/playwright> |

The worker image additionally installs:

| Component | License | Link |
|---|---|---|
| FastVideo | Apache-2.0 | <https://github.com/hao-ai-lab/FastVideo> |
| PyTorch / torchvision / torchaudio | BSD-3-Clause | <https://pytorch.org> |
| FlashInfer | Apache-2.0 | <https://github.com/flashinfer-ai/flashinfer> |
| NVIDIA CUDA / cuDNN container | NVIDIA proprietary terms | <https://docs.nvidia.com/cuda/eula/> |
| Hugging Face Hub / hf_transfer | Apache-2.0 / MIT | <https://huggingface.co/docs/hub> |

## 5. FFmpeg

The gateway invokes FFmpeg as a separate process. It uses `FFMPEG_BIN` if set,
otherwise system `ffmpeg`, otherwise the binary bundled by `imageio-ffmpeg`.
FFmpeg is not part of this repository's MIT-licensed source.

FFmpeg licensing depends on the build configuration. Some builds are LGPL-2.1+
and some enable GPL components. The Linux `imageio-ffmpeg` wheel used during
validation bundles an FFmpeg build with `--enable-gpl --enable-version3`; run
`ffmpeg -version` and check the configure flags for the binary you distribute.
See <https://ffmpeg.org/legal.html>.

## 6. Screenshots and test fixtures

`docs/screenshots/`, `docs/media/` and `tests/fixtures/` contain sample images
and renders used to demonstrate and test the UI. The catgirl and portrait demo
stills in `docs/media/` were supplied by the project owner and are included with
permission for this repository; they are not stock media. Their inclusion does
not grant rights to any third-party character, likeness, trademark or brand.
Replace sample media with your own assets before commercial redistribution.

## 7. Trademarks

MiniMax, MiniMax H3, Modal, Cloudflare, FastVideo and other names are trademarks
of their respective owners. This project is not endorsed by or affiliated with
those companies.
