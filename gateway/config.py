"""Configuration and the public model registry.

Values are intentionally plain Python so the service can start with only the
runtime dependencies installed. Environment variables override defaults; a
`.env` file is loaded by the launcher script, not by this module.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Resolution:
    label: str
    width: int
    height: int
    aspect: str

    @property
    def size(self) -> str:
        return f"{self.width}x{self.height}"


# FastH3/MiniMax-H3 native canvases used by the upstream RTX PRO 6000 recipes.
# 124 frames is the closest 17n+5 frame count to five seconds at 24 fps.
RESOLUTIONS: tuple[Resolution, ...] = (
    Resolution("480p landscape", 832, 480, "832:480"),
    Resolution("480p portrait", 480, 832, "480:832"),
    Resolution("768p landscape", 1344, 768, "1344:768"),
    Resolution("768p portrait", 768, 1344, "768:1344"),
)

_SIZE_INDEX = {r.size: r for r in RESOLUTIONS}
_ASPECT_INDEX = {r.aspect: r for r in RESOLUTIONS}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    worker_model: str
    label: str
    family: str
    repo: str
    license: str
    description: str
    quality: str
    native_audio: bool
    image_to_video: bool
    seconds: int
    durations: tuple[int, ...]
    duration_frames: dict[int, int]
    fps: int
    frames: int
    steps: int
    guidance: float
    default_size: str
    # Conservative planning numbers used for budget admission, not billing.
    expected_warm_seconds: int
    expected_load_seconds: int
    peak_vram_gb: int
    cost_cold_estimate_usd: float
    cost_warm_estimate_usd: float
    supports_first_frame: bool = True
    supports_last_frame: bool = False
    supports_references: bool = False
    adaptive_aspect_ratio: bool = True
    max_reference_images: int = 1
    max_reference_videos: int = 0
    max_reference_audios: int = 0
    resolutions: tuple[Resolution, ...] = RESOLUTIONS
    hidden: bool = False

    def frames_for(self, seconds: float) -> int:
        value = round(seconds)
        if abs(seconds - value) > 0.01 or value not in self.durations:
            allowed = ", ".join(f"{item}s" for item in self.durations)
            raise ValueError(f"{self.label} supports {allowed} clips, got {seconds:g}s")
        return self.duration_frames[value]

    def cost_for(self, seconds: float, *, cold: bool = False) -> float:
        base = self.cost_cold_estimate_usd if cold else self.cost_warm_estimate_usd
        return round(base * max(1.0, float(seconds) / max(1, self.seconds)), 4)

    def public(self) -> dict:
        return {
            "id": self.id,
            "object": "model",
            "owned_by": "video-gateway",
            "label": self.label,
            "family": self.family,
            "description": self.description,
            "quality": self.quality,
            "license": self.license,
            "native_audio": self.native_audio,
            "image_to_video": self.image_to_video,
            "supports_first_frame": self.supports_first_frame,
            "supports_last_frame": self.supports_last_frame,
            "adaptive_aspect_ratio": self.adaptive_aspect_ratio,
            "supports_references": self.supports_references,
            "max_reference_images": self.max_reference_images,
            "max_reference_videos": self.max_reference_videos,
            "max_reference_audios": self.max_reference_audios,
            "seconds": self.seconds,
            "durations": list(self.durations),
            "default_duration": self.seconds,
            "default_size": self.default_size,
            "fps": self.fps,
            "frames": self.frames,
            "steps": self.steps,
            "resolutions": [
                {"size": r.size, "label": r.label, "aspect_ratio": r.aspect}
                for r in self.resolutions
            ],
            "estimated_cost_usd": {
                "warm": self.cost_for(self.seconds),
                "cold": self.cost_for(self.seconds, cold=True),
            },
        }


# FastH3/MiniMax H3 weights are governed by the MiniMax H3 Community License.
# The reference deployment was authorized by the project owner for that
# environment; downstream deployers must review the license and obtain their own
# authorization where required. The practical quality family is FastH3 QAD on
# one Blackwell GPU: the original MiniMax H3 is a 33B dense full-attention model
# whose sparse-attention implementation is not in the open-source release, so it
# cannot be served at 768p/10-15s on one 96 GB card.
MODELS: dict[str, ModelSpec] = {
    "fasth3-v2": ModelSpec(
        id="fasth3-v2",
        worker_model="fasth3-v2",
        label="FastH3 V2 NVFP4 QAD",
        family="minimax-h3",
        repo="FastVideo/FastH3-QAD-v2",
        license="MiniMax H3 Community",
        description=(
            "High-quality FastH3 V2: NVFP4 QAD, 8 distilled forwards, full 36-block VAE, "
            "first/last-frame conditioning and native synchronized audio. 768p default."
        ),
        quality="quality",
        native_audio=True,
        image_to_video=True,
        seconds=10,
        durations=(5, 10, 15),
        duration_frames={5: 124, 10: 243, 15: 362},
        fps=24,
        frames=243,
        steps=9,
        guidance=1.0,
        default_size="1344x768",
        expected_warm_seconds=180,
        expected_load_seconds=60,
        peak_vram_gb=90,
        cost_cold_estimate_usd=0.32,
        cost_warm_estimate_usd=0.16,
        supports_first_frame=True,
        supports_last_frame=True,
    ),
    "fasth3-omniref": ModelSpec(
        id="fasth3-omniref",
        worker_model="fasth3-omniref",
        label="FastH3 OmniRef NVFP4 QAD",
        family="minimax-h3",
        repo="FastVideo/FastH3-Omni-NVFP4-QAD",
        license="MiniMax H3 Community",
        description=(
            "Reference-conditioned FastH3: ordered images, video and audio references with "
            "native synchronized audio and the full 36-block VAE. Best for character consistency."
        ),
        quality="reference",
        native_audio=True,
        image_to_video=True,
        seconds=10,
        durations=(5, 10, 15),
        duration_frames={5: 124, 10: 243, 15: 362},
        fps=24,
        frames=243,
        steps=8,
        guidance=1.0,
        default_size="1344x768",
        expected_warm_seconds=240,
        expected_load_seconds=75,
        peak_vram_gb=90,
        cost_cold_estimate_usd=0.40,
        cost_warm_estimate_usd=0.20,
        supports_first_frame=False,
        supports_last_frame=False,
        supports_references=True,
        adaptive_aspect_ratio=True,
        max_reference_images=9,
        max_reference_videos=3,
        max_reference_audios=3,
    ),
    "fasth3-trim": ModelSpec(
        id="fasth3-trim",
        worker_model="fasth3-trim",
        label="FastH3 Trim (preview)",
        family="minimax-h3",
        repo="FastVideo/FastVideo-FastH3-Trim-8-Step-NVFP4",
        license="MiniMax H3 Community",
        description="Pruned 8-step preview. Hidden by default; use for drafts only.",
        quality="fast",
        native_audio=True,
        image_to_video=True,
        seconds=5,
        durations=(5,),
        duration_frames={5: 124},
        fps=24,
        frames=124,
        steps=9,
        guidance=1.0,
        default_size="832x480",
        expected_warm_seconds=45,
        expected_load_seconds=40,
        peak_vram_gb=40,
        cost_cold_estimate_usd=0.10,
        cost_warm_estimate_usd=0.05,
        supports_first_frame=True,
        supports_last_frame=False,
        hidden=True,
    ),
}

DEFAULT_MODEL = "fasth3-v2"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")).resolve())
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8787))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    api_keys: list[str] = field(default_factory=list)
    api_key_file: Path | None = None
    backend: str = field(default_factory=lambda: os.getenv("VIDEO_BACKEND", "auto").lower())
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "").rstrip("/"))

    modal_app_name: str = field(default_factory=lambda: os.getenv("MODAL_APP_NAME", "video-gateway-worker"))
    modal_function_name: str = field(default_factory=lambda: os.getenv("MODAL_FUNCTION_NAME", "generate"))
    modal_environment: str = field(default_factory=lambda: os.getenv("MODAL_ENVIRONMENT", ""))
    modal_models_volume: str = field(
        default_factory=lambda: os.getenv("MODAL_MODELS_VOLUME", "video-gateway-models")
    )
    modal_results_volume: str = field(
        default_factory=lambda: os.getenv("MODAL_RESULTS_VOLUME", "video-gateway-results")
    )
    modal_progress_dict: str = field(
        default_factory=lambda: os.getenv("MODAL_PROGRESS_DICT", "video-gateway-progress")
    )
    worker_gpu: str = field(default_factory=lambda: os.getenv("WORKER_GPU", "RTX-PRO-6000"))
    worker_cpu: float = field(default_factory=lambda: _float("WORKER_CPU", 4.0))
    worker_memory_gib: float = field(default_factory=lambda: _float("WORKER_MEMORY_GIB", 96.0))
    worker_scaledown_seconds: int = field(default_factory=lambda: _int("WORKER_SCALEDOWN_SECONDS", 10))

    monthly_credits_usd: float = field(default_factory=lambda: _float("MONTHLY_CREDITS_USD", 30.0))
    monthly_soft_budget_usd: float = field(default_factory=lambda: _float("MONTHLY_SOFT_BUDGET_USD", 28.0))
    billing_cycle_day: int = field(default_factory=lambda: _int("BILLING_CYCLE_DAY", 1))
    max_job_estimate_usd: float = field(default_factory=lambda: _float("MAX_JOB_ESTIMATE_USD", 0.75))
    max_jobs_per_day: int = field(default_factory=lambda: _int("MAX_JOBS_PER_DAY", 50))
    max_queue: int = field(default_factory=lambda: _int("MAX_QUEUE", 24))
    billing_sync_enabled: bool = field(default_factory=lambda: _bool("BILLING_SYNC_ENABLED", True))
    billing_sync_interval_seconds: int = field(
        default_factory=lambda: _int("BILLING_SYNC_INTERVAL_SECONDS", 900)
    )

    max_upload_bytes: int = field(default_factory=lambda: _int("MAX_UPLOAD_BYTES", 12 * 1024 * 1024))
    max_video_reference_bytes: int = field(
        default_factory=lambda: _int("MAX_VIDEO_REFERENCE_BYTES", 32 * 1024 * 1024)
    )
    max_audio_reference_bytes: int = field(
        default_factory=lambda: _int("MAX_AUDIO_REFERENCE_BYTES", 16 * 1024 * 1024)
    )
    retention_days: int = field(default_factory=lambda: _int("RETENTION_DAYS", 30))
    max_storage_gb: float = field(default_factory=lambda: _float("MAX_STORAGE_GB", 20.0))
    job_timeout_seconds: int = field(default_factory=lambda: _int("JOB_TIMEOUT_SECONDS", 1800))
    max_batch_size: int = field(default_factory=lambda: _int("MAX_BATCH_SIZE", 20))
    allow_remote_references: bool = field(default_factory=lambda: _bool("ALLOW_REMOTE_REFERENCES", False))
    content_token_ttl_seconds: int = field(
        default_factory=lambda: _int("CONTENT_TOKEN_TTL_SECONDS", 3600)
    )
    ffmpeg_bin: str = field(default_factory=lambda: os.getenv("FFMPEG_BIN", ""))
    default_model: str = DEFAULT_MODEL

    # H3-structured prompt enhancement. The client is any OpenAI-compatible
    # chat endpoint, including the local CLIProxyAPI. Empty base URL disables it.
    prompt_enhancer_base_url: str = field(
        default_factory=lambda: os.getenv("H3_PROMPT_ENHANCER_BASE_URL", "").rstrip("/")
    )
    prompt_enhancer_api_key: str = field(
        default_factory=lambda: os.getenv("H3_PROMPT_ENHANCER_API_KEY", "")
    )
    prompt_enhancer_model: str = field(
        default_factory=lambda: os.getenv("H3_PROMPT_ENHANCER_MODEL", "")
    )
    prompt_enhancer_opencode_session: str = field(
        default_factory=lambda: os.getenv("H3_PROMPT_ENHANCER_OPENCODE_SESSION", "")
    )
    prompt_enhancer_timeout_seconds: int = field(
        default_factory=lambda: _int("H3_PROMPT_ENHANCER_TIMEOUT_SECONDS", 180)
    )
    prompt_enhancer_enabled: bool = field(
        default_factory=lambda: _bool("H3_PROMPT_ENHANCER_ENABLED", True)
    )
    regenerate_hd_enabled: bool = field(
        default_factory=lambda: _bool("REGENERATE_HD_ENABLED", False)
    )

    # Cost rates from https://modal.com/pricing (verified 2026-11-17).
    gpu_usd_per_second: float = 0.000842  # RTX PRO 6000
    cpu_usd_per_core_second: float = 0.0000131
    memory_usd_per_gib_second: float = 0.00000222
    volume_usd_per_gib_month: float = 0.09
    egress_usd_per_gib: float = 0.04

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gateway.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / "secret_key"

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.uploads_dir, self.outputs_dir, self.thumbs_dir, self.tmp_dir):
            path.mkdir(parents=True, exist_ok=True)
        if not self.api_keys:
            key_file = self.data_dir / "api_key"
            if key_file.exists():
                keys = [line.strip() for line in key_file.read_text().splitlines() if line.strip()]
            else:
                keys = ["vg_" + secrets.token_urlsafe(32)]
                key_file.write_text(keys[0] + "\n")
                key_file.chmod(0o600)
            self.api_keys = keys
            self.api_key_file = key_file
        if not self.secret_path.exists():
            self.secret_path.write_text(secrets.token_urlsafe(48) + "\n")
            self.secret_path.chmod(0o600)

    @property
    def content_secret(self) -> bytes:
        return self.secret_path.read_bytes().strip()

    @classmethod
    def from_env(cls) -> Settings:
        api_keys = _list("API_KEYS")
        settings = cls(api_keys=api_keys)
        settings.ensure()
        return settings


def get_model(model_id: str | None, default: str = DEFAULT_MODEL) -> ModelSpec:
    key = (model_id or default).strip().lower()
    aliases = {
        "h3": "fasth3-v2",
        "fasth3": "fasth3-v2",
        "i2v": "fasth3-v2",
        "fl2va": "fasth3-v2",
        "fast-h3": "fasth3-v2",
        "fasth3-v2-nvfp4": "fasth3-v2",
        "minimax-h3": "fasth3-v2",
        "omniref": "fasth3-omniref",
        "h3-omniref": "fasth3-omniref",
        "reference": "fasth3-omniref",
        "trim": "fasth3-trim",
        "h3-trim": "fasth3-trim",
    }
    key = aliases.get(key, key)
    if key not in MODELS:
        raise KeyError(f"Unknown model {model_id!r}. Available: {', '.join(MODELS)}")
    return MODELS[key]


def resolve_adaptive_resolution(
    image_width: int,
    image_height: int,
    *,
    short_edge: int = 768,
    max_pixels: int = 768 * 1344,
    multiple: int = 32,
) -> Resolution:
    """H3 canvas from an uploaded image's aspect ratio.

    Mirrors FastVideo's ``resolve_canvas_size``: shorter side 768 for the 768p
    tier, area capped at 768*1344, both sides snapped to 32. This keeps the
    image's aspect ratio without a forced 16:9 crop.
    """
    if image_width <= 0 or image_height <= 0:
        raise ValueError("image dimensions must be positive")
    ratio = image_width / image_height
    if not 0.25 <= ratio <= 4.0:
        raise ValueError("MiniMax-H3 supports image aspect ratios from 1:4 to 4:1")
    if ratio >= 1:
        width, height = float(short_edge) * ratio, float(short_edge)
    else:
        width, height = float(short_edge), float(short_edge) / ratio
    area = width * height
    if area > max_pixels:
        scale = (max_pixels / area) ** 0.5
        width, height = width * scale, height * scale
    height = max(multiple, round(height / multiple) * multiple)
    width = max(multiple, round(width / multiple) * multiple)
    # Rounding can land a few percent above the area cap (e.g. 768x1376 for a
    # 480x839 portrait). Step the long side back to stay within the model's
    # validated 768p area and prefer the native 768x1344 grid.
    while width * height > max_pixels and max(width, height) > multiple:
        if height >= width:
            height -= multiple
        else:
            width -= multiple
    return Resolution("adaptive", width, height, f"{width}:{height}")


def resolve_size(
    model: ModelSpec,
    size: str | None,
    aspect_ratio: str | None,
    *,
    image_size: tuple[int, int] | None = None,
) -> Resolution:
    if size and size.strip().lower() in {"auto", "adaptive"}:
        if image_size is None:
            return _SIZE_INDEX[model.default_size]
        return resolve_adaptive_resolution(*image_size)
    if not size and image_size is not None and model.adaptive_aspect_ratio:
        return resolve_adaptive_resolution(*image_size)
    if size:
        normalized = size.strip().lower().replace("×", "x")
        if normalized in _SIZE_INDEX:
            return _SIZE_INDEX[normalized]
        if "x" in normalized:
            try:
                w, h = (int(part) for part in normalized.split("x", 1))
            except ValueError as exc:
                raise ValueError(f"Invalid size {size!r}; use WIDTHxHEIGHT") from exc
            for resolution in model.resolutions:
                if (resolution.width, resolution.height) == (w, h):
                    return resolution
            raise ValueError(
                f"Unsupported size {size!r} for {model.id}; choose one of "
                + ", ".join(r.size for r in model.resolutions)
            )
        raise ValueError(f"Invalid size {size!r}; use WIDTHxHEIGHT")
    if aspect_ratio:
        normalized = aspect_ratio.strip()
        if normalized in _ASPECT_INDEX:
            return _ASPECT_INDEX[normalized]
        if ":" in normalized:
            try:
                aw, ah = (float(part) for part in normalized.split(":", 1))
            except ValueError as exc:
                raise ValueError(f"Invalid aspect_ratio {aspect_ratio!r}") from exc
            if aw <= 0 or ah <= 0:
                raise ValueError("aspect_ratio values must be positive")
            target = aw / ah
            return min(model.resolutions, key=lambda r: abs((r.width / r.height) - target))
        raise ValueError(f"Invalid aspect_ratio {aspect_ratio!r}; use WIDTH:HEIGHT")
    return _SIZE_INDEX[model.default_size]


def frames_for_duration(model: ModelSpec, seconds: float) -> int:
    return model.frames_for(seconds)


def parse_env_file(path: Path) -> None:
    """Minimal .env loader used by the systemd/local launchers.

    We avoid python-dotenv so the gateway itself has a small dependency set.
    Lines are KEY=VALUE, comments start with #, optional surrounding quotes.
    Existing environment variables win.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
