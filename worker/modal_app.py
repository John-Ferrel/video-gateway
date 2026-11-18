"""Modal worker for the Video Gateway — FastH3 / MiniMax-H3 family.

Deploy:
    modal deploy worker/modal_app.py

Pre-download weights on a cheap CPU worker (one-time):
    modal run worker/modal_app.py --action prepare

The GPU function is a single RTX PRO 6000 container (`max_containers=1`,
`buffer_containers=0`, `retries=0`). Weights live on a Modal Volume and are
loaded lazily; the container is released after `scaledown_window` seconds.

Primary models:
  fasth3-v2       FastH3 V2 NVFP4 QAD, 8 transformer forwards, native audio
  fasth3-omniref  Ordered image/video/audio references, native audio
  fasth3-trim     Pruned 8-step preview, smaller/faster

FastH3/MiniMax H3 weights are governed by the MiniMax H3 Community License.
The reference deployment was authorized by the project owner for that specific
environment; this does not grant downstream rights. Review the upstream license
before deploying.
"""

from __future__ import annotations

import gc
import json
import os
import time
import traceback
from pathlib import Path

import modal

APP_NAME = "video-gateway-worker"
GPU = os.getenv("WORKER_GPU", "RTX-PRO-6000")
CPU = float(os.getenv("WORKER_CPU", "4"))
MEMORY_MIB = int(float(os.getenv("WORKER_MEMORY_GIB", "96")) * 1024)
SCALEDOWN = int(os.getenv("WORKER_SCALEDOWN_SECONDS", "10"))
ENABLE_GPU_SNAPSHOT = os.getenv("WORKER_GPU_SNAPSHOT", "0") == "1"

MODELS_VOLUME = os.getenv("MODAL_MODELS_VOLUME", "video-gateway-models")
CACHE_VOLUME = os.getenv("MODAL_CACHE_VOLUME", "video-gateway-cache")
RESULTS_VOLUME = os.getenv("MODAL_RESULTS_VOLUME", "video-gateway-results")
PROGRESS_DICT = os.getenv("MODAL_PROGRESS_DICT", "video-gateway-progress")
AUTOTUNE_CACHE = "/cache/flashinfer_autotune.json"

# FastVideo's PyPI release is behind main; pin the current implementation that
# includes the FastH3 V2 / OmniRef / Trim loaders and NVFP4 kernels.
FASTVIDEO_REF = os.getenv("FASTVIDEO_REF", "c938a8aa2b856a594f791a61875ba0bdac5ef318")

MODEL_SPECS = {
    "h3-fl2va": {
        "repo": "MiniMaxAI/MiniMax-H3",
        "dir": "MiniMax-H3",
        "steps": 50,
        "frames": 243,
        "reference_mode": None,
        "pipeline_class": None,
        "vae_decoder": None,
        "adaln_table": None,
        "text_encoder_offload": True,
        "vae_tile_batch": 8,
        "lazy_module_load": True,
        "quantization": None,
        "attention_backend": "TORCH_SDPA",
        "h3_vsa_fp4": False,
        "vsa_sparsity": None,
        "vsa_tile_size": None,
        "vae_offload": True,
        "vae_tiling": True,
        "description": "Original MiniMax H3 FL2VA (experimental; 33B dense/full-attention)",
        "experimental": True,
        "allow_patterns": [
            "model_index.json",
            "modular_model_index.json",
            "LICENSE",
            "transformer/*",
            "text_encoder/*",
            "tokenizer/*",
            "processor/*",
            "vae/*",
            "audio_vae/*",
            "scheduler/*",
            "audio_scheduler/*",
        ],
    },
    "fasth3-v2": {
        "repo": "FastVideo/FastH3-QAD-v2",
        "dir": "FastH3-QAD-v2",
        "steps": 9,
        "frames": 124,
        "reference_mode": None,
        "pipeline_class": None,
        "vae_decoder": None,
        "adaln_table": "transformer/adaln_tables.pt",
        "text_encoder_offload": False,
        "vae_tile_batch": 28,
        "lazy_module_load": False,
        "quantization": {"transformer_quant": "NVFP4", "layer_profile": "h3_dit_vsa"},
        "attention_backend": "VIDEO_SPARSE_ATTN_H3",
        "h3_vsa_fp4": True,
        "vsa_sparsity": 0.8,
        "vsa_tile_size": 64,
        "vae_offload": False,
        "vae_tiling": True,
        "description": "FastH3 V2 NVFP4 QAD (8 forwards, native audio)",
    },
    "fasth3-omniref": {
        "repo": "FastVideo/FastH3-Omni-NVFP4-QAD",
        "dir": "FastH3-Omni-NVFP4-QAD",
        "steps": 8,
        "frames": 124,
        "reference_mode": "omniref",
        "pipeline_class": "MiniMaxH3Ref2VAModularPipeline",
        "vae_decoder": None,
        "adaln_table": None,
        "text_encoder_offload": False,
        "vae_tile_batch": 8,
        "lazy_module_load": False,
        "quantization": {"transformer_quant": "NVFP4", "layer_profile": "h3_dit_vsa"},
        "attention_backend": "VIDEO_SPARSE_ATTN_H3",
        "h3_vsa_fp4": True,
        "vsa_sparsity": 0.9,
        "vsa_tile_size": 128,
        "vae_offload": False,
        "vae_tiling": True,
        "description": "FastH3 OmniRef NVFP4 QAD (ordered references, full VAE)",
    },
    "fasth3-trim": {
        "repo": "FastVideo/FastVideo-FastH3-Trim-8-Step-NVFP4",
        "dir": "FastVideo-FastH3-Trim-8-Step-NVFP4",
        "steps": 9,
        "frames": 124,
        "reference_mode": None,
        "pipeline_class": None,
        "vae_decoder": None,
        "adaln_table": None,
        "text_encoder_offload": False,
        "vae_tile_batch": 28,
        "lazy_module_load": False,
        "quantization": {"transformer_quant": "NVFP4", "layer_profile": "h3_dit_vsa"},
        "attention_backend": "VIDEO_SPARSE_ATTN_H3",
        "h3_vsa_fp4": True,
        "vsa_sparsity": 0.8,
        "vsa_tile_size": 64,
        "vae_offload": False,
        "vae_tiling": True,
        "description": "FastH3 Trim NVFP4 (fast preview)",
    },
}

# CUDA 13 toolkit for torch 2.12/cu130.  add_python installs a clean CPython.
image = (
    modal.Image.from_registry(
        "nvidia/cuda:13.0.0-cudnn-devel-ubuntu22.04",
        add_python="3.12",
    )
    .apt_install(
        "git",
        "ffmpeg",
        "libgl1",
        "libglib2.0-0",
        "libsndfile1",
        "curl",
        "ca-certificates",
        "build-essential",
        "ninja-build",
        "cmake",
        "pkg-config",
    )
    .uv_pip_install(
        "torch==2.12.0",
        "torchvision",
        "torchaudio",
        index_url="https://download.pytorch.org/whl/cu130",
    )
    .uv_pip_install(
        f"fastvideo @ git+https://github.com/hao-ai-lab/FastVideo.git@{FASTVIDEO_REF}",
        "huggingface_hub>=0.30",
        "hf_transfer==0.1.9",
    )
    # The PyPI fastvideo-kernel wheel predates the H3 FP4 prefix-split API
    # (KEY_LENGTH_SIZING). Build the matching kernel from the same checkout for
    # sm_120a. This is an image-build step; it does not allocate a GPU.
    .pip_install("uv")
    .run_commands(
        "git clone --filter=blob:none https://github.com/hao-ai-lab/FastVideo.git /opt/FastVideo",
        f"cd /opt/FastVideo && git fetch --depth 1 origin {FASTVIDEO_REF} && git checkout {FASTVIDEO_REF}",
        "cd /opt/FastVideo && git submodule update --init --recursive fastvideo-kernel/include/cutlass fastvideo-kernel/include/tk",
        "cd /opt/FastVideo/fastvideo-kernel && "
        "CC=/usr/bin/gcc CXX=/usr/bin/g++ CUDACXX=/usr/local/cuda/bin/nvcc "
        "UV_SYSTEM_PYTHON=1 TORCH_CUDA_ARCH_LIST=12.0a CMAKE_BUILD_PARALLEL_LEVEL=4 "
        "CMAKE_ARGS='-DFASTVIDEO_KERNEL_BUILD_ATTN_QAT_INFER=ON' "
        "./build.sh --wheel-dir /opt/fastvideo-kernel-wheel",
        "uv pip install --system --break-system-packages --reinstall-package fastvideo-kernel --no-deps "
        "/opt/fastvideo-kernel-wheel/*.whl",
        "rm -rf /opt/FastVideo",
        "python -c \"import pathlib; p=pathlib.Path('/usr/local/lib/python3.12/site-packages/attn_qat_infer/api.py'); "
        "assert 'KEY_LENGTH_SIZING = True' in p.read_text(); "
        "print('fastvideo-kernel H3 FP4 prefix API OK')\"",
        "python -c \"import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda)\"",
        "python -c \"import pathlib; p=pathlib.Path('/usr/local/lib/python3.12/site-packages/fastvideo'); "
        "assert (p / 'models/dits/minimax_h3.py').exists(); "
        "print('fastvideo package + minimax_h3 module present')\"",
    )
    .env(
        {
            "UV_TORCH_BACKEND": "cu130",
            "HF_HOME": "/models/hf",
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
            "HF_XET_HIGH_PERFORMANCE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
            "FLASHINFER_WORKSPACE_BASE": "/cache",
            "FASTVIDEO_CACHE_ROOT": "/cache/fastvideo",
            "FASTVIDEO_STAGE_LOGGING": os.getenv("FASTVIDEO_STAGE_LOGGING", "1"),
            # Exact cross-request reuse of Qwen3-VL conditioning. The directory
            # lives on the cache Volume so it survives scale-to-zero.
            "FASTVIDEO_H3_PROMPT_EMBEDS_CACHE": "/cache/prompt_embeds",
            "PYTHONUNBUFFERED": "1",
        }
    )
)

app = modal.App(APP_NAME, image=image)
models_volume = modal.Volume.from_name(MODELS_VOLUME, create_if_missing=True, version=2)
cache_volume = modal.Volume.from_name(CACHE_VOLUME, create_if_missing=True, version=2)
results_volume = modal.Volume.from_name(RESULTS_VOLUME, create_if_missing=True, version=2)
progress_dict = modal.Dict.from_name(PROGRESS_DICT, create_if_missing=True)


def _progress(job_id: str, stage: str, pct: int, **extra) -> None:
    try:
        progress_dict[job_id] = {"stage": stage, "pct": pct, "ts": time.time(), **extra}
    except Exception:
        pass


_AUTOTUNE_LOADED = False


def _load_autotune_cache() -> None:
    """Try to reuse FlashInfer's measured (runner, tactic) choices.

    Qwen3-VL's NVFP4 linears ask FlashInfer's autotuner to pick a kernel on
    first use. On a fresh container that can cost tens of seconds. The cache is
    plain JSON on the cache Volume; an empty or environment-mismatched file is
    harmless and leaves the tuner to profile normally.
    """
    global _AUTOTUNE_LOADED
    if _AUTOTUNE_LOADED:
        return
    _AUTOTUNE_LOADED = True
    try:
        from flashinfer.autotuner import AutoTuner

        if Path(AUTOTUNE_CACHE).is_file():
            loaded = AutoTuner.get().load_configs(AUTOTUNE_CACHE)
            print(f"FlashInfer autotune cache loaded={loaded} from {AUTOTUNE_CACHE}")
    except Exception as exc:  # noqa: BLE001
        print(f"FlashInfer autotune cache load skipped: {exc}")


def _save_autotune_cache() -> None:
    try:
        from flashinfer.autotuner import AutoTuner

        AutoTuner.get().save_configs(AUTOTUNE_CACHE)
        print(f"FlashInfer autotune cache saved to {AUTOTUNE_CACHE}")
    except Exception as exc:  # noqa: BLE001
        print(f"FlashInfer autotune cache save skipped: {exc}")


def _should_use_adaln_table(spec: dict, model_dir: Path, *, has_image: bool, has_references: bool) -> bool:
    """The packed AdaLN table covers only the T2V ladder.

    First-frame (FL2VA) and reference (Ref2VA) requests use a different
    timestep schedule; the table raises for those keys. Let the loader compute
    modulation for them instead.
    """
    adaln = spec.get("adaln_table")
    return bool(adaln and (model_dir / adaln).exists() and not has_image and not has_references)


def _set_h3_env(
    spec: dict,
    model_dir: Path,
    *,
    has_image: bool = False,
    has_references: bool = False,
) -> None:
    """Configure the H3 path before importing/constructing FastVideo."""
    backend = spec.get("attention_backend") or "VIDEO_SPARSE_ATTN_H3"
    os.environ["FASTVIDEO_ATTENTION_BACKEND"] = backend
    os.environ["FASTVIDEO_MINIMAX_H3_FUSIONS"] = "all"
    os.environ["FASTVIDEO_MINIMAX_H3_EXACT_KERNELS"] = "all"
    if spec.get("h3_vsa_fp4"):
        os.environ.update({
            "FASTVIDEO_H3_VSA_FP4": "1",
            "FASTVIDEO_H3_VSA_FP4_PREFIX": "fp8",
            "FASTVIDEO_H3_VSA_FP4_TWO_LEVEL_P": "1",
            "FASTVIDEO_H3_VSA_FP4_K_SMOOTH": "1",
            "FASTVIDEO_H3_GATE_CHUNK_ROWS": "8192",
            "FASTVIDEO_VSA_TRITON": "1",
            "FASTVIDEO_VSA_PTX_BLACKWELL": "0",
            "FASTVIDEO_FA4": "0",
        })
    else:
        os.environ["FASTVIDEO_H3_VSA_FP4"] = "0"
        os.environ.pop("FASTVIDEO_H3_VSA_FP4_PREFIX", None)
        os.environ.pop("FASTVIDEO_H3_VSA_FP4_TWO_LEVEL_P", None)
        os.environ.pop("FASTVIDEO_H3_VSA_FP4_K_SMOOTH", None)
        os.environ.pop("FASTVIDEO_H3_GATE_CHUNK_ROWS", None)
        os.environ.pop("FASTVIDEO_VSA_TRITON", None)
        os.environ.pop("FASTVIDEO_VSA_PTX_BLACKWELL", None)
        os.environ.pop("FASTVIDEO_FA4", None)
    quantization = spec.get("quantization") or {}
    if quantization.get("transformer_quant") == "NVFP4":
        os.environ["FASTVIDEO_NVFP4_MM_BACKEND"] = "cutlass"
    else:
        os.environ.pop("FASTVIDEO_NVFP4_MM_BACKEND", None)
    os.environ["FASTVIDEO_H3_VAE_TILE_BATCH"] = str(spec["vae_tile_batch"])
    os.environ["FASTVIDEO_H3_VAE_TILE_PARALLEL"] = "0"
    if _should_use_adaln_table(spec, model_dir, has_image=has_image, has_references=has_references):
        os.environ["FASTVIDEO_H3_ADALN_TABLE"] = str(model_dir / spec["adaln_table"])
    else:
        os.environ.pop("FASTVIDEO_H3_ADALN_TABLE", None)
    decoder = spec.get("vae_decoder")
    if decoder and (model_dir / decoder).exists():
        os.environ["FASTVIDEO_H3_VAE_NVFP4_DECODER"] = str(model_dir / decoder)
    else:
        os.environ.pop("FASTVIDEO_H3_VAE_NVFP4_DECODER", None)


def _generator_cache_key(
    model_key: str,
    spec: dict,
    *,
    has_image: bool,
    has_references: bool,
) -> str:
    """Distinguish T2V from image/reference conditioning for a warm model.

    The two paths use different AdaLN schedules; a cached T2V generator must not
    serve an I2V/Ref2VA request that needs computed modulation.
    """
    if spec.get("reference_mode") == "omniref":
        mode = "ref2va"
    elif has_image or has_references:
        mode = "i2v"
    else:
        mode = "t2v"
    return f"{model_key}:{mode}"


def build_generator_config(model_key: str, model_dir: Path, *, has_references: bool, has_image: bool) -> dict:
    spec = MODEL_SPECS[model_key]
    experimental = {
        "attention_backend": spec.get("attention_backend") or "VIDEO_SPARSE_ATTN_H3",
        "h3_sequential_load": bool(spec.get("lazy_module_load")),
        "inference_torch_compile": False,
        "vae_parallel_decode": False,
        "video_decode_backend": "h3-vae",
    }
    if spec.get("h3_vsa_fp4"):
        experimental["h3_vsa_fp4"] = {
            "enabled": True,
            "prefix_precision": "fp8",
            "two_level_p": True,
            "smooth_k": True,
        }
    if spec.get("vsa_sparsity") is not None:
        experimental["VSA_sparsity"] = spec["vsa_sparsity"]
        experimental["VSA_tile_size"] = spec["vsa_tile_size"]
    pipeline = {
        "workload_type": "i2v" if (has_references or has_image) else "t2v",
        "vae_tiling": bool(spec.get("vae_tiling", True)),
        "experimental": experimental,
    }
    if spec.get("pipeline_class"):
        pipeline["components"] = {"override_pipeline_cls_name": spec["pipeline_class"]}
    quantization = spec.get("quantization") or {}
    return {
        "model_path": str(model_dir),
        "engine": {
            "num_gpus": 1,
            "execution_backend": "uni",
            "use_fsdp_inference": False,
            "parallelism": {"tp_size": 1, "sp_size": 1},
            "quantization": quantization,
            "offload": {
                "dit": False,
                "dit_layerwise": False,
                "text_encoder": bool(spec["text_encoder_offload"]),
                "vae": bool(spec.get("vae_offload", False)),
                "pin_cpu_memory": True,
                "lazy_module_load": bool(spec.get("lazy_module_load", False)),
            },
            "compile": {"enabled": False, "vae_enabled": False},
        },
        "pipeline": pipeline,
    }


def build_generation_request(
    payload: dict,
    *,
    image: object | None,
    last_image: object | None,
    references: list[object] | None,
    output_path: str,
    spec: dict,
) -> dict:
    """Build the typed FastVideo request. Kept pure so it can be unit tested."""
    inputs: dict = {}
    if references:
        inputs["references"] = references
    else:
        if image is not None:
            inputs["pil_image"] = image
        if last_image is not None:
            inputs["last_image"] = last_image
    return {
        "prompt": payload["prompt"],
        "inputs": inputs,
        "sampling": {
            "seed": int(payload.get("seed", 0)),
            "height": int(payload["height"]),
            "width": int(payload["width"]),
            "num_frames": int(payload.get("frames", spec["frames"])),
            "fps": int(payload.get("fps", 24)),
            "num_inference_steps": int(payload.get("steps", spec["steps"])),
            "guidance_scale": float(payload.get("guidance", 1.0)),
            "batch_cfg": False,
        },
        "output": {
            "output_path": output_path,
            "save_video": True,
            "return_frames": False,
        },
    }


def _download_models(model_keys: list[str]) -> dict:
    from huggingface_hub import snapshot_download

    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
    results = {}
    for key in model_keys:
        spec = MODEL_SPECS[key]
        target = Path("/models") / spec["dir"]
        target.mkdir(parents=True, exist_ok=True)
        marker = target / ".gateway_ready.json"
        if marker.exists():
            try:
                results[key] = json.loads(marker.read_text())
                print(f"{key}: already prepared at {target}")
                continue
            except Exception:
                pass
        print(f"{key}: downloading {spec['repo']} -> {target}")
        started = time.time()
        snapshot_download(
            repo_id=spec["repo"],
            local_dir=str(target),
            max_workers=8,
            allow_patterns=spec.get("allow_patterns"),
        )
        files = [p for p in target.rglob("*") if p.is_file()]
        size = sum(p.stat().st_size for p in files)
        manifest = {
            "key": key,
            "repo": spec["repo"],
            "path": str(target),
            "files": len(files),
            "bytes": size,
            "downloaded_at": time.time(),
            "elapsed_s": round(time.time() - started, 1),
        }
        marker.write_text(json.dumps(manifest, indent=2))
        results[key] = manifest
        print(f"{key}: {size / 1e9:.1f} GB in {manifest['elapsed_s']}s")
    models_volume.commit()
    return results


@app.function(
    cpu=2,
    memory=8192,
    timeout=7200,
    volumes={"/models": models_volume},
)
def prepare(model_keys: list[str] | None = None) -> dict:
    """CPU-only model download. No GPU is allocated, so this is cheap."""
    keys = model_keys or [key for key, spec in MODEL_SPECS.items() if not spec.get("experimental")]
    unknown = set(keys) - set(MODEL_SPECS)
    if unknown:
        raise ValueError(f"Unknown models: {sorted(unknown)}")
    return _download_models(keys)


@app.function(
    gpu=GPU,
    cpu=CPU,
    memory=MEMORY_MIB,
    timeout=int(os.getenv("WORKER_TIMEOUT", "1800")),
    startup_timeout=int(os.getenv("WORKER_STARTUP_TIMEOUT", "1200")),
    scaledown_window=SCALEDOWN,
    min_containers=0,
    max_containers=1,
    buffer_containers=0,
    retries=0,
    volumes={
        "/models": models_volume,
        "/cache": cache_volume,
        "/results": results_volume,
    },
    enable_memory_snapshot=ENABLE_GPU_SNAPSHOT,
    experimental_options={"enable_gpu_snapshot": True} if ENABLE_GPU_SNAPSHOT else None,
)
def generate(payload: dict) -> dict:
    """Run one clip. A failure is written to the result volume and returned."""
    # Do not force offline: the first request may need the small Qwen3-VL
    # processor/config cache. All weights are local.
    os.environ.pop("HF_HUB_OFFLINE", None)
    job_id = str(payload["job_id"])
    model_key = str(payload["model"])
    started = time.monotonic()
    result_dir = Path("/results") / "results" / job_id
    result_dir.mkdir(parents=True, exist_ok=True)
    temp_files: list[Path] = []
    load_s = 0.0
    cold = True

    def write_result(data: dict) -> None:
        data.setdefault("job_id", job_id)
        data.setdefault("model", model_key)
        data.setdefault("total_s", round(time.monotonic() - started, 3))
        (result_dir / "result.json").write_text(json.dumps(data, indent=2, default=str))
        try:
            results_volume.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"volume commit failed: {exc}")

    try:
        if model_key not in MODEL_SPECS:
            raise ValueError(f"Unknown worker model {model_key!r}")
        spec = MODEL_SPECS[model_key]
        model_dir = Path("/models") / spec["dir"]
        if not (model_dir / "modular_model_index.json").exists() and not (model_dir / "model_index.json").exists():
            raise RuntimeError(
                f"Model files missing at {model_dir}. Run `modal run worker/modal_app.py --action prepare` first."
            )

        incoming_refs = payload.get("references", []) or []
        first_ref = next((r for r in incoming_refs if str(r.get("role") or "reference") == "first_frame"), None)
        last_ref = next((r for r in incoming_refs if str(r.get("role") or "reference") == "last_frame"), None)
        has_image = first_ref is not None or last_ref is not None
        has_references = spec["reference_mode"] == "omniref" and bool(incoming_refs)
        _set_h3_env(
            spec,
            model_dir,
            has_image=has_image,
            has_references=has_references,
        )
        _progress(job_id, "loading", 5)
        _load_autotune_cache()
        import torch
        from fastvideo import VideoGenerator
        from fastvideo.pipelines.basic.minimax_h3 import MiniMaxH3Reference
        from PIL import Image

        # Materialize first/last frames and ordered references. The gateway has
        # already validated dimensions and media types.
        image_path = None
        last_image_path = None
        reference_paths: list[tuple[str, str]] = []
        for index, ref in enumerate(incoming_refs):
            import base64

            media_type = str(ref["type"])
            role = str(ref.get("role") or "reference")
            suffix = Path(str(ref.get("name") or "")).suffix
            if not suffix:
                suffix = {
                    "image": ".jpg",
                    "video": ".mp4",
                    "audio": ".wav",
                }.get(media_type, ".bin")
            path = Path("/tmp") / f"{job_id}_ref_{index}{suffix}"
            path.write_bytes(base64.b64decode(ref["b64"]))
            temp_files.append(path)
            if role == "first_frame":
                image_path = str(path)
            elif role == "last_frame":
                last_image_path = str(path)
            else:
                reference_paths.append((media_type, str(path)))

        if spec["reference_mode"] is None:
            if reference_paths:
                raise ValueError(
                    f"{model_key} accepts first/last-frame images; use fasth3-omniref for ordered references."
                )
            image = Image.open(image_path).convert("RGB") if image_path else None
            last_image = Image.open(last_image_path).convert("RGB") if last_image_path else None
            refs = []
        else:
            if image_path or last_image_path:
                raise ValueError("FastH3 OmniRef uses ordered references, not first/last-frame inputs.")
            image = None
            last_image = None
            refs = [
                MiniMaxH3Reference(source=path, media_type=media_type)
                for media_type, path in reference_paths
            ]
            if not refs:
                raise ValueError("FastH3 OmniRef needs at least one image or video reference.")

        global _GENERATOR_CACHE
        if "_GENERATOR_CACHE" not in globals():
            _GENERATOR_CACHE = {"key": None, "generator": None, "load_s": 0.0}

        generator_key = _generator_cache_key(
            model_key, spec, has_image=image is not None or last_image is not None,
            has_references=bool(refs)
        )
        if _GENERATOR_CACHE["key"] != generator_key or _GENERATOR_CACHE["generator"] is None:
            _GENERATOR_CACHE["generator"] = None
            _GENERATOR_CACHE["key"] = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            config = build_generator_config(
                model_key,
                model_dir,
                has_references=bool(refs),
                has_image=image is not None or last_image is not None,
            )
            print(f"Loading {model_key} from {model_dir} (cold start)")
            load_started = time.monotonic()
            _GENERATOR_CACHE["generator"] = VideoGenerator.from_config(config)
            _GENERATOR_CACHE["key"] = generator_key
            load_s = time.monotonic() - load_started
            _GENERATOR_CACHE["load_s"] = load_s
            cold = True
            print(f"Model loaded in {load_s:.1f}s")
        else:
            load_s = 0.0
            cold = False
            print(f"Reusing warm {generator_key}")

        generator = _GENERATOR_CACHE["generator"]
        load_peak_mb = None
        if torch.cuda.is_available():
            load_peak_mb = round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1)
            torch.cuda.reset_peak_memory_stats()

        _progress(job_id, "generating", 15)
        generate_started = time.monotonic()
        request = build_generation_request(
            payload,
            image=image,
            last_image=last_image,
            references=refs or None,
            output_path=str(result_dir),
            spec=spec,
        )
        result = generator.generate(request)
        if isinstance(result, list):
            if not result:
                raise RuntimeError("generator returned no results")
            result = result[0]
        inference_s = time.monotonic() - generate_started
        _progress(job_id, "encoding", 85)

        video_path = getattr(result, "video_path", None)
        if not video_path or not Path(video_path).exists():
            candidates = sorted(result_dir.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not candidates:
                raise RuntimeError("generator did not produce an mp4 file")
            video_path = str(candidates[0])
        final_path = result_dir / "video.mp4"
        if Path(video_path) != final_path:
            os.replace(video_path, final_path)

        peak_mb = None
        gpu_name = None
        if torch.cuda.is_available():
            peak_mb = round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1)
            if load_peak_mb:
                peak_mb = max(peak_mb, load_peak_mb)
            gpu_name = torch.cuda.get_device_name(0)
        size = getattr(result, "size", None)
        audio_rate = getattr(result, "audio_sample_rate", None)
        if size and len(size) >= 3:
            height, width, frames = int(size[0]), int(size[1]), int(size[2])
        else:
            height = int(payload["height"])
            width = int(payload["width"])
            frames = int(payload.get("frames", spec["frames"]))
        _progress(job_id, "finishing", 95)
        output = {
            "ok": True,
            "video_filename": final_path.name,
            "width": width,
            "height": height,
            "frames": frames,
            "fps": int(payload.get("fps", 24)),
            "has_audio": True,
            "audio_sample_rate": audio_rate,
            "duration_s": round(frames / int(payload.get("fps", 24)), 3),
            "cold_start": cold,
            "model_load_s": round(load_s, 3),
            "inference_s": round(inference_s, 3),
            "encode_s": 0.0,
            "peak_vram_mb": peak_mb,
            "peak_load_vram_mb": load_peak_mb,
            "gpu_name": gpu_name,
            "backend": "modal",
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
        }
        write_result(output)
        _progress(job_id, "completed", 100)
        try:
            progress_dict.pop(job_id, None)
        except Exception as exc:  # noqa: BLE001
            print(f"progress cleanup failed: {exc}")
        return output
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        print(error)
        print(traceback.format_exc())
        output = {
            "ok": False,
            "error": error[:4000],
            "traceback": traceback.format_exc()[-8000:],
            "cold_start": cold,
            "model_load_s": round(load_s, 3),
        }
        write_result(output)
        _progress(job_id, "failed", 100, error=error[:500])
        return output
    finally:
        _save_autotune_cache()
        try:
            # Persist FlashInfer/prompt-embeds caches written during this call so
            # the next scale-from-zero container does not repeat autotuning or
            # Qwen3-VL conditioning for the same presentation.
            cache_volume.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"cache volume commit failed: {exc}")
        for path in temp_files:
            try:
                path.unlink()
            except OSError:
                pass


@app.function(
    cpu=1,
    memory=2048,
    timeout=300,
    volumes={"/models": models_volume},
)
def model_status() -> dict:
    """Inspect the model Volume without allocating a GPU."""
    status = {}
    for key, spec in MODEL_SPECS.items():
        path = Path("/models") / spec["dir"]
        marker = path / ".gateway_ready.json"
        status[key] = {
            "path": str(path),
            "exists": path.exists(),
            "model_index": (path / "model_index.json").exists() or (path / "modular_model_index.json").exists(),
            "ready_marker": json.loads(marker.read_text()) if marker.exists() else None,
        }
    return status


@app.local_entrypoint()
def main(action: str = "status", models: str = ""):
    """`modal run worker/modal_app.py --action prepare|status|warmup`"""
    keys = [m.strip() for m in models.split(",") if m.strip()] or None
    if action == "prepare":
        print(prepare.remote(keys))
    elif action == "status":
        print(model_status.remote())
    elif action == "warmup":
        payload = {
            "job_id": "warmup_" + str(int(time.time())),
            "model": (keys or ["fasth3-v2"])[0],
            "prompt": "A calm ocean at sunrise, slow cinematic camera move, gentle waves and seagulls.",
            "width": 832,
            "height": 480,
            "frames": 124,
            "fps": 24,
            "steps": 9,
            "guidance": 1.0,
            "seed": 2026,
        }
        print(generate.remote(payload))
    else:
        raise SystemExit(f"unknown action {action!r}")
