"""FastAPI application: OpenAI-video-style async API, usage and web UI."""

from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .backend import Backend, build_backend
from .config import MODELS, Settings, frames_for_duration, get_model, resolve_size
from .cost import BudgetManager, cycle_key
from .db import Database, iso
from .dispatcher import Dispatcher
from .prompting import EnhancementResult, PromptEnhancer, image_data_uri
from .security import (
    Auth,
    RateLimiter,
    fetch_remote_image,
    make_content_token,
    parse_data_uri,
    validate_image_bytes,
)
from .storage import Storage

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class VideoCreate(BaseModel):
    model: str | None = Field(default=None, examples=["fasth3-v2"])
    prompt: str = Field(min_length=1, max_length=16000)
    size: str | None = Field(default=None, examples=["832x480"])
    width: int | None = None
    height: int | None = None
    aspect_ratio: str | None = Field(default=None, examples=["16:9"])
    seconds: float | None = None
    seed: int | None = None
    with_audio: bool = True
    input_reference: str | None = Field(
        default=None,
        description="First-frame image: data URI or (when enabled) public image URL.",
    )
    last_reference: str | None = Field(
        default=None,
        description="Optional last-frame image for first-and-last-frame generation.",
    )
    last_frame: str | None = None
    enhance_prompt: bool = True
    reference_images: list[str] | str | None = None
    reference_videos: list[str] | str | None = None
    reference_audios: list[str] | str | None = None
    references: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None


class BatchCreate(BaseModel):
    items: list[VideoCreate] = Field(min_length=1)
    label: str | None = None
    model: str | None = None


class ReconcileRequest(BaseModel):
    billed_usd: float = Field(ge=0)
    note: str | None = None


@dataclass
class IncomingReference:
    media_type: str
    data: bytes
    mime: str | None = None
    name: str | None = None
    role: str = "reference"


def _error(message: str, *, code: str, type_: str = "invalid_request_error", param: str | None = None,
           status_code: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "message": message,
                "type": type_,
                "param": param,
                "code": code,
            }
        },
    )


def _int_field(fields: dict, name: str) -> int | None:
    value = fields.get(name)
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise _error(f"{name} must be an integer", code="invalid_value", param=name) from exc


def _float_field(fields: dict, name: str, default: float | None = None) -> float | None:
    value = fields.get(name)
    if value is None or value == "":
        return default
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise _error(f"{name} must be a number", code="invalid_value", param=name) from exc


def _bool_field(fields: dict, name: str, default: bool = False) -> bool:
    value = fields.get(name)
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _status_public(internal: str) -> str:
    if internal in {"queued", "submitting"}:
        return "queued"
    if internal in {"processing", "downloading"}:
        return "in_progress"
    return internal


class Gateway:
    def __init__(self, settings: Settings, *, start_dispatcher: bool = True,
                 backend: Backend | None = None):
        self.settings = settings
        self.settings.ensure()
        self.db = Database(settings.db_path)
        self.storage = Storage(settings)
        self.backend = backend or build_backend(settings)
        self.budget = BudgetManager(settings, self.db)
        self.enhancer = PromptEnhancer(
            base_url=settings.prompt_enhancer_base_url,
            api_key=settings.prompt_enhancer_api_key,
            model=settings.prompt_enhancer_model,
            opencode_session=settings.prompt_enhancer_opencode_session,
            timeout_seconds=settings.prompt_enhancer_timeout_seconds,
            enabled=settings.prompt_enhancer_enabled,
        )
        self.dispatcher = Dispatcher(settings, self.db, self.storage, self.backend, self.budget)
        self.auth = Auth(settings)
        self.limiter = RateLimiter(limit=120, window_seconds=60)
        self._start_dispatcher = start_dispatcher
        self._started_at = time.time()

    # ------------------------------------------------------------------ public objects
    def owned_video(self, video_id: str, owner: int) -> dict | None:
        video = self.db.get_video(video_id)
        if not video or int(video.get("owner") or 0) != owner:
            return None
        return video

    def video_public(self, video: dict, *, include_prompt: bool = True) -> dict:
        base = self.settings.public_base_url.rstrip("/")
        video_id = video["id"]
        content_token = make_content_token(self.settings, video_id, purpose="content")
        thumb_token = make_content_token(self.settings, video_id, purpose="thumbnail")
        status_value = _status_public(video["status"])
        completed = video["status"] == "completed"
        public = {
            "id": video_id,
            "object": "video",
            "model": video["model"],
            "status": status_value,
            "internal_status": video["status"],
            "progress": int(video.get("progress") or 0),
            "stage": video.get("stage"),
            "created_at": video.get("created_at"),
            "queued_at": video.get("queued_at"),
            "started_at": video.get("started_at"),
            "completed_at": video.get("completed_at"),
            "seconds": video.get("seconds"),
            "size": f"{video['width']}x{video['height']}",
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": video.get("fps"),
            "seed": video.get("seed"),
            "has_audio": bool(video.get("with_audio")),
            "batch_id": video.get("batch_id"),
            "cost_usd": round(float(video.get("cost_usd") or 0), 4),
            "reserved_usd": round(float(video.get("reserved_usd") or 0), 4),
            "error": video.get("error") if video["status"] in {"failed", "cancelled"} else None,
            "content_url": f"{base}/v1/videos/{video_id}/content?token={content_token}" if completed else None,
            "download_url": (
                f"{base}/v1/videos/{video_id}/content?download=1&token={content_token}"
                if completed else None
            ),
            "thumbnail_url": (
                f"{base}/v1/videos/{video_id}/thumbnail?token={thumb_token}"
                if completed and video.get("thumb_path") else None
            ),
            "usage": {
                "model_load_s": video.get("model_load_s"),
                "inference_s": video.get("inference_s"),
                "encode_s": video.get("encode_s"),
                "peak_vram_mb": video.get("peak_vram_mb"),
                "cold_start": bool(video.get("cold_start")) if video.get("cold_start") is not None else None,
            },
        }
        if include_prompt:
            public["prompt"] = video.get("prompt")
        meta = {}
        try:
            meta = json.loads(video.get("meta_json") or "{}")
        except (TypeError, ValueError):
            logger.debug("invalid meta_json for %s", video_id)
        refs = meta.get("references") or []
        public["reference_count"] = len(refs)
        public["reference_types"] = [ref.get("type", "image") for ref in refs]
        public["conditioning"] = [ref.get("role", "reference") for ref in refs]
        public["has_last_frame"] = any(ref.get("role") == "last_frame" for ref in refs)
        details = meta.get("metadata") or {}
        if include_prompt:
            public["prompt_original"] = details.get("prompt_original")
            public["prompt_enhanced"] = details.get("prompt_enhanced")
            public["prompt_enhancement"] = details.get("prompt_enhancement")
        if meta.get("warning"):
            public["warning"] = meta["warning"]
        if video["status"] == "queued":
            public["queue_position"] = self.db.queue_position(video_id)
            if video.get("error"):
                public["blocked_reason"] = video["error"]
        return public

    def usage_public(self) -> dict:
        usage = self.budget.summary()
        usage["storage"] = self.storage.usage()
        usage["backend"] = self.backend.health()
        usage["dispatcher"] = {
            "alive": bool(self.dispatcher._thread and self.dispatcher._thread.is_alive()),
            "current_job": self.dispatcher._current_job,
        }
        usage["models"] = [spec.public() for spec in MODELS.values()]
        usage["worker"] = {
            "gpu": self.settings.worker_gpu,
            "cpu": self.settings.worker_cpu,
            "memory_gib": self.settings.worker_memory_gib,
            "scaledown_seconds": self.settings.worker_scaledown_seconds,
            "job_timeout_seconds": self.settings.job_timeout_seconds,
        }
        usage["uptime_s"] = int(time.time() - self._started_at)
        return usage

    # ------------------------------------------------------------------ request parsing
    def reference_from_value(
        self,
        reference: str,
        *,
        media_type: str = "image",
        max_bytes: int | None = None,
        param: str = "input_reference",
        role: str = "reference",
    ) -> IncomingReference:
        limit = max_bytes or (
            self.settings.max_video_reference_bytes
            if media_type == "video"
            else self.settings.max_audio_reference_bytes
            if media_type == "audio"
            else self.settings.max_upload_bytes
        )
        if reference.startswith("data:"):
            try:
                data, mime = parse_data_uri(reference, max_bytes=limit)
            except Exception as exc:
                raise _error(f"Invalid {param} data URI: {exc}", code="invalid_reference", param=param) from exc
            return IncomingReference(media_type=media_type, data=data, mime=mime, role=role)
        if reference.startswith(("http://", "https://")):
            if not self.settings.allow_remote_references:
                raise _error(
                    "Remote reference URLs are disabled. Upload the file as multipart/form-data "
                    "or send a data URI.",
                    code="remote_reference_disabled",
                    param=param,
                )
            try:
                data = fetch_remote_image(reference, max_bytes=limit)
            except Exception as exc:
                raise _error(f"Could not fetch {param}: {exc}", code="invalid_reference", param=param) from exc
            return IncomingReference(media_type=media_type, data=data, role=role)
        raise _error(
            f"{param} must be a data URI or an allowed http(s) URL",
            code="invalid_reference",
            param=param,
        )

    def _references_from_fields(self, fields: dict) -> list[IncomingReference]:
        references: list[IncomingReference] = []

        def add(value: Any, media_type: str, role: str = "reference") -> None:
            if value is None or value == "":
                return
            if isinstance(value, list):
                for item in value:
                    add(item, media_type, role)
                return
            if isinstance(value, dict):
                kind = str(value.get("type") or media_type)
                item_role = str(value.get("role") or role)
                source = value.get("data") or value.get("url") or value.get("source")
                if source:
                    references.append(self.reference_from_value(
                        str(source), media_type=kind, param=f"reference_{kind}s", role=item_role
                    ))
                return
            if isinstance(value, str):
                references.append(self.reference_from_value(
                    value, media_type=media_type, param=f"reference_{media_type}s", role=role
                ))

        if fields.get("input_reference"):
            add(fields.get("input_reference"), "image", "first_frame")
        if fields.get("reference_image"):
            add(fields.get("reference_image"), "image", "first_frame")
        if fields.get("image"):
            add(fields.get("image"), "image", "first_frame")
        if fields.get("last_reference") or fields.get("last_frame"):
            add(fields.get("last_reference") or fields.get("last_frame"), "image", "last_frame")
        add(fields.get("reference_images"), "image", "reference")
        add(fields.get("reference_videos"), "video", "reference")
        add(fields.get("reference_audios"), "audio", "reference")
        add(fields.get("references"), "image", "reference")
        return references

    async def parse_create_request(self, request: Request) -> tuple[dict, list[IncomingReference]]:
        content_type = request.headers.get("content-type", "")
        fields: dict[str, Any] = {}
        references: list[IncomingReference] = []
        if "application/json" in content_type:
            try:
                payload = await request.json()
            except Exception as exc:
                raise _error("Request body must be valid JSON", code="invalid_json") from exc
            if not isinstance(payload, dict):
                raise _error("Request body must be a JSON object", code="invalid_body")
            fields = dict(payload)
            if fields.get("width") and fields.get("height") and not fields.get("size"):
                width = _int_field(fields, "width")
                height = _int_field(fields, "height")
                if width and height:
                    fields["size"] = f"{width}x{height}"
            references = self._references_from_fields(fields)
        elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            for key, value in form.multi_items():
                if key in {"metadata"} and isinstance(value, str):
                    try:
                        fields[key] = json.loads(value)
                    except json.JSONDecodeError:
                        raise _error("metadata must be valid JSON", code="invalid_metadata", param="metadata")
                    continue
                if key == "references" and isinstance(value, str):
                    try:
                        fields[key] = json.loads(value)
                    except json.JSONDecodeError:
                        raise _error("references must be valid JSON", code="invalid_reference", param="references")
                    continue
                role_map = {
                    "input_reference": ("image", "first_frame"),
                    "image": ("image", "first_frame"),
                    "reference_image": ("image", "first_frame"),
                    "last_reference": ("image", "last_frame"),
                    "last_frame": ("image", "last_frame"),
                    "reference_images": ("image", "reference"),
                    "reference_videos": ("video", "reference"),
                    "reference_video": ("video", "reference"),
                    "reference_audios": ("audio", "reference"),
                    "reference_audio": ("audio", "reference"),
                }
                if key not in role_map:
                    if isinstance(value, str):
                        fields[key] = value
                    continue
                media_type, role = role_map[key]
                if isinstance(value, str):
                    references.append(self.reference_from_value(
                        value, media_type=media_type, param=key, role=role
                    ))
                    continue
                if hasattr(value, "read"):
                    limit = (
                        self.settings.max_video_reference_bytes
                        if media_type == "video"
                        else self.settings.max_audio_reference_bytes
                        if media_type == "audio"
                        else self.settings.max_upload_bytes
                    )
                    data = await value.read(limit + 1)
                    if len(data) > limit:
                        raise _error("Reference file exceeds the upload limit", code="file_too_large",
                                     param=key, status_code=413)
                    references.append(IncomingReference(
                        media_type=media_type,
                        data=data,
                        mime=getattr(value, "content_type", None),
                        name=getattr(value, "filename", None),
                        role=role,
                    ))
            if fields.get("width") and fields.get("height") and not fields.get("size"):
                width = _int_field(fields, "width")
                height = _int_field(fields, "height")
                if width and height:
                    fields["size"] = f"{width}x{height}"
            if fields.get("references"):
                references.extend(self._references_from_fields({"references": fields["references"]}))
        else:
            raise _error("Use application/json or multipart/form-data", code="unsupported_media_type",
                         status_code=415)
        return fields, references

    def validate_job(
        self,
        fields: dict,
        references: list[IncomingReference] | None = None,
        *,
        allow_model_override: str | None = None,
    ) -> dict:
        prompt = fields.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise _error("prompt is required", code="missing_prompt", param="prompt")
        prompt = prompt.replace("\x00", "").strip()
        if len(prompt) > 16000:
            raise _error("prompt is too long (max 16000 characters)", code="prompt_too_long", param="prompt")
        model_id = allow_model_override or fields.get("model") or self.settings.default_model
        try:
            spec = get_model(str(model_id))
        except KeyError as exc:
            raise _error(str(exc), code="unknown_model", param="model") from exc
        size = fields.get("size")
        aspect = fields.get("aspect_ratio")
        try:
            resolution = resolve_size(spec, str(size) if size else None, str(aspect) if aspect else None)
        except ValueError as exc:
            raise _error(str(exc), code="invalid_size", param="size") from exc
        requested_seconds = _float_field(fields, "seconds", spec.seconds)
        seconds = float(requested_seconds if requested_seconds is not None else spec.seconds)
        try:
            frames = frames_for_duration(spec, seconds)
        except ValueError as exc:
            raise _error(str(exc), code="invalid_duration", param="seconds") from exc
        with_audio = _bool_field(fields, "with_audio", True)
        if not with_audio:
            raise _error(
                f"{spec.label} always generates synchronized audio in the same pass; with_audio=false "
                "is not supported.",
                code="unsupported_parameter",
                param="with_audio",
            )
        seed = _int_field(fields, "seed")
        if seed is None:
            seed = secrets.randbelow(2**31)
        if seed < 0 or seed > 2**31 - 1:
            raise _error("seed must be between 0 and 2^31-1", code="invalid_seed", param="seed")
        metadata = fields.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                raise _error("metadata must be valid JSON", code="invalid_metadata", param="metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise _error("metadata must be an object", code="invalid_metadata", param="metadata")

        refs = references or []
        first_frames = [r for r in refs if r.role == "first_frame"]
        last_frames = [r for r in refs if r.role == "last_frame"]
        ordered = [r for r in refs if r.role == "reference"]
        images = [r for r in ordered if r.media_type == "image"]
        videos = [r for r in ordered if r.media_type == "video"]
        audios = [r for r in ordered if r.media_type == "audio"]

        if spec.supports_references:
            if first_frames or last_frames:
                raise _error(
                    f"{spec.label} uses ordered reference_images/reference_videos/reference_audios, "
                    "not first/last-frame inputs.",
                    code="unsupported_reference",
                    param="input_reference",
                )
            if len(images) > spec.max_reference_images:
                raise _error(f"{spec.label} accepts at most {spec.max_reference_images} images",
                             code="too_many_references")
            if len(videos) > spec.max_reference_videos:
                raise _error(f"{spec.label} accepts at most {spec.max_reference_videos} videos",
                             code="too_many_references")
            if len(audios) > spec.max_reference_audios:
                raise _error(f"{spec.label} accepts at most {spec.max_reference_audios} audio clips",
                             code="too_many_references")
            if not images and not videos:
                raise _error(f"{spec.label} requires at least one image or video reference",
                             code="missing_reference", param="references")
        else:
            if videos or audios:
                raise _error(
                    f"{spec.label} accepts image conditioning only; use fasth3-omniref for "
                    "video/audio references.",
                    code="unsupported_reference",
                    param="references",
                )
            if len(first_frames) > 1 or len(last_frames) > 1:
                raise _error(
                    f"{spec.label} accepts at most one first frame and one last frame.",
                    code="too_many_references",
                    param="references",
                )
            if not first_frames and len(images) == 1:
                # Backward-compatible reference_image/reference_images use.
                first_frames = [images.pop()]
            if images:
                raise _error(
                    f"{spec.label} accepts one first frame and one last frame; use "
                    "fasth3-omniref for ordered references.",
                    code="unsupported_reference",
                    param="references",
                )
            if last_frames and not spec.supports_last_frame:
                raise _error(
                    f"{spec.label} does not support last-frame conditioning.",
                    code="unsupported_reference",
                    param="last_reference",
                )

        first = first_frames[0] if first_frames else None
        last = last_frames[0] if last_frames else None
        if not aspect and (size is None or str(size).strip().lower() in {"auto", "adaptive"}):
            image_ref = first or last or (images[0] if images else None)
            if image_ref is not None and image_ref.media_type == "image":
                try:
                    _, _, image_w, image_h = validate_image_bytes(image_ref.data, image_ref.mime)
                except ValueError as exc:
                    raise _error(str(exc), code="invalid_image", param="input_reference") from exc
                try:
                    resolution = resolve_size(
                        spec, str(size) if size else None, None,
                        image_size=(image_w, image_h),
                    )
                except ValueError as exc:
                    raise _error(str(exc), code="invalid_size", param="size") from exc
        return {
            "spec": spec,
            "prompt": prompt,
            "width": resolution.width,
            "height": resolution.height,
            "seconds": seconds,
            "frames": frames,
            "fps": spec.fps,
            "seed": seed,
            "with_audio": with_audio,
            "enhance_prompt": _bool_field(fields, "enhance_prompt", True),
            "metadata": metadata or {},
            "references": ordered,
            "first_frame": first,
            "last_frame": last,
        }

    def _reference_extension(self, ref: IncomingReference) -> str:
        if ref.media_type == "image":
            return ".jpg"
        suffix = Path(ref.name or "").suffix.lower()
        allowed = {
            "video": {".mp4", ".mov", ".webm", ".mkv"},
            "audio": {".wav", ".mp3", ".m4a", ".flac", ".ogg"},
        }.get(ref.media_type, set())
        if suffix in allowed:
            return suffix
        return ".mp4" if ref.media_type == "video" else ".wav" if ref.media_type == "audio" else ".bin"

    def _normalize_references(self, validated: dict) -> list[IncomingReference]:
        all_refs: list[IncomingReference] = []
        for role, ref in (("first_frame", validated["first_frame"]),
                          ("last_frame", validated["last_frame"])):
            if ref is not None:
                all_refs.append(IncomingReference(
                    ref.media_type, ref.data, ref.mime, ref.name, role
                ))
        all_refs.extend(validated["references"])

        normalized: list[IncomingReference] = []
        for ref in all_refs:
            if ref.media_type == "image":
                try:
                    data, mime, image_w, image_h = validate_image_bytes(ref.data, ref.mime)
                except ValueError as exc:
                    raise _error(str(exc), code="invalid_image",
                                 param="last_reference" if ref.role == "last_frame" else "input_reference") from exc
                ref = IncomingReference("image", data, mime, ref.name, ref.role)
                validated["metadata"][f"{ref.role}_size"] = f"{image_w}x{image_h}"
            else:
                if ref.media_type == "video" and not (ref.mime or "").startswith("video/"):
                    raise _error("Invalid video reference MIME type", code="invalid_reference",
                                 param="reference_videos")
                if ref.media_type == "audio" and ref.mime and not ref.mime.startswith("audio/"):
                    raise _error("Invalid audio reference MIME type", code="invalid_reference",
                                 param="reference_audios")
            normalized.append(ref)
        return normalized

    def _enhancement_inputs(self, validated: dict) -> tuple[list[dict], list[dict]]:
        spec = validated["spec"]
        references: list[dict] = []
        images: list[dict] = []
        if spec.supports_references:
            counts = {"image": 0, "video": 0, "audio": 0}
            for ref in validated["references"]:
                kind = ref.media_type
                counts[kind] = counts.get(kind, 0) + 1
                label = f"<{kind.capitalize()} {counts[kind]}>"
                references.append({"type": kind, "name": ref.name, "label": label})
                if kind == "image":
                    uri = image_data_uri(ref.data)
                    if uri:
                        images.append({"label": label, "data_uri": uri})
        else:
            first = validated["first_frame"]
            last = validated["last_frame"]
            if first is not None:
                uri = image_data_uri(first.data)
                if uri:
                    images.append({"label": "<Picture 1>", "data_uri": uri})
            if last is not None:
                label = "<Picture 2>" if first is not None else "<Picture 1>"
                uri = image_data_uri(last.data)
                if uri:
                    images.append({"label": label, "data_uri": uri})
        return references, images

    async def _enhance_prompt(self, validated: dict) -> EnhancementResult:
        prompt = validated["prompt"]
        mode = PromptEnhancer.mode_for(
            has_first_frame=validated["first_frame"] is not None,
            has_last_frame=validated["last_frame"] is not None,
            references=validated["references"],
        )
        if not validated["enhance_prompt"]:
            return EnhancementResult(prompt, False, error="disabled by request", mode=mode)
        references, images = self._enhancement_inputs(validated)
        return await self.enhancer.enhance(
            prompt,
            has_first_frame=validated["first_frame"] is not None,
            has_last_frame=validated["last_frame"] is not None,
            references=references,
            seconds=int(validated["seconds"]),
            width=validated["width"],
            height=validated["height"],
            images=images,
        )

    async def create_one(
        self,
        fields: dict,
        references: list[IncomingReference] | None = None,
        *,
        batch_id: str | None = None,
        model_override: str | None = None,
        owner: int = 0,
    ) -> dict:
        validated = self.validate_job(fields, references, allow_model_override=model_override)
        spec = validated["spec"]
        normalized = self._normalize_references(validated)

        if not self.backend.configured:
            raise _error(
                "The GPU worker is not configured yet. Set Modal credentials on the gateway host and restart.",
                code="worker_not_configured",
                status_code=503,
            )
        allowed, reason, _summary = self.budget.check_admission(
            spec, seconds=validated["seconds"], local=self.backend.demo
        )
        if not allowed:
            raise _error(
                reason or "Budget guard rejected the job",
                code="budget_exceeded",
                type_="budget_error",
                status_code=429,
            )

        metadata = dict(validated["metadata"])
        prompt = validated["prompt"]
        enhancement = await self._enhance_prompt(validated)
        if validated["enhance_prompt"] and enhancement.applied:
            prompt = enhancement.prompt
        metadata["prompt_original"] = validated["prompt"]
        metadata["prompt_enhanced"] = prompt if enhancement.applied else None
        metadata["prompt_enhancement"] = {
            "requested": bool(validated["enhance_prompt"]),
            "applied": bool(enhancement.applied),
            "model": enhancement.model,
            "mode": enhancement.mode,
            "images_sent": enhancement.images_sent,
            "error": enhancement.error,
        }

        video_id = "video_" + uuid.uuid4().hex
        reference_meta = []
        first_image_path = None
        first_image_mime = None
        first_image_bytes = None
        for index, ref in enumerate(normalized):
            path = self.storage.uploads / f"{video_id}_ref_{index}{self._reference_extension(ref)}"
            self.storage.atomic_write(path, ref.data)
            self.db.add_upload(
                video_id=video_id,
                kind=f"reference_{ref.media_type}",
                path=str(path),
                mime=ref.mime,
                size=len(ref.data),
            )
            entry = {
                "type": ref.media_type,
                "role": ref.role,
                "path": str(path),
                "mime": ref.mime,
                "name": ref.name or path.name,
            }
            reference_meta.append(entry)
            if ref.role == "first_frame" and first_image_path is None:
                first_image_path = str(path)
                first_image_mime = ref.mime
                first_image_bytes = len(ref.data)
        reserved = self.budget.reserve_for(
            spec, seconds=validated["seconds"], local=self.backend.demo
        )
        video = self.db.create_video(
            id=video_id,
            owner=owner,
            batch_id=batch_id,
            model=spec.id,
            prompt=prompt,
            width=validated["width"],
            height=validated["height"],
            seconds=validated["seconds"],
            fps=validated["fps"],
            seed=validated["seed"],
            with_audio=1 if validated["with_audio"] else 0,
            input_image_path=first_image_path,
            input_image_mime=first_image_mime,
            input_image_bytes=first_image_bytes,
            status="queued",
            progress=0,
            stage="queued",
            reserved_usd=reserved,
            meta_json=json.dumps(
                {"metadata": metadata, "references": reference_meta, "frames": validated["frames"]},
                separators=(",", ":"),
            ),
        )
        self.db.add_event(
            video_id=video_id, level="info", message="job queued",
            detail={
                "model": spec.id,
                "size": f"{validated['width']}x{validated['height']}",
                "seconds": validated["seconds"],
                "frames": validated["frames"],
                "references": [f"{r.role}:{r.media_type}" for r in normalized],
                "prompt_enhanced": bool(enhancement.applied),
            },
        )
        self.dispatcher.wake()
        return video

    # ------------------------------------------------------------------ app factory
    def create_app(self) -> FastAPI:
        gateway = self

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            if gateway._start_dispatcher:
                gateway.dispatcher.start()
            yield
            gateway.dispatcher.stop()

        app = FastAPI(
            title="Video Gateway",
            version="1.1.0",
            description=(
                "A small, OpenAI-video-style gateway for serverless AI video generation on Modal. "
                "Quality-first FastH3 / MiniMax H3: 5/10/15 s first/last-frame I2V and OmniRef, native synchronized audio."
            ),
            lifespan=lifespan,
        )
        app.state.gateway = gateway

        @app.exception_handler(HTTPException)
        async def http_exception_handler(request: Request, exc: HTTPException):
            if isinstance(exc.detail, dict) and "error" in exc.detail:
                return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": "api_error",
                        "param": None,
                        "code": "http_error",
                    }
                },
                headers=exc.headers,
            )

        @app.exception_handler(RequestValidationError)
        async def validation_exception_handler(request: Request, exc: RequestValidationError):
            first = exc.errors()[0] if exc.errors() else {}
            loc = ".".join(str(part) for part in first.get("loc", []) if part != "body")
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "message": first.get("msg", "Invalid request"),
                        "type": "invalid_request_error",
                        "param": loc or None,
                        "code": "validation_error",
                    }
                },
            )

        def require(request: Request) -> tuple[str, int]:
            key = gateway.auth.require(request)
            gateway.limiter.check(key)
            owner = gateway.auth.key_owner(request)
            return key, int(owner or 0)

        def require_admin(request: Request) -> tuple[str, int]:
            key, owner = require(request)
            if owner != 0:
                raise _error(
                    "Administrator key required. Use the first configured API key.",
                    code="forbidden",
                    status_code=403,
                )
            return key, owner

        @app.get("/healthz")
        async def healthz():
            return {
                "status": "ok",
                "service": "video-gateway",
                "time": iso(),
                "backend": gateway.backend.health(),
                "budget_remaining_usd": gateway.budget.summary()["remaining_usd"],
            }

        @app.get("/", include_in_schema=False)
        async def index():
            path = STATIC_DIR / "index.html"
            return HTMLResponse(path.read_text(encoding="utf-8"))

        @app.get("/favicon.svg", include_in_schema=False)
        async def favicon():
            path = STATIC_DIR / "favicon.svg"
            if path.exists():
                return FileResponse(path, media_type="image/svg+xml")
            raise HTTPException(status_code=404)

        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        # ---------------------------------------------------------- models & usage
        @app.get("/v1/models")
        async def list_models(request: Request):
            require(request)
            health = gateway.backend.health()
            return {
                "object": "list",
                "prompt_enhancement_available": gateway.enhancer.available,
                "regenerate_hd_available": bool(gateway.settings.regenerate_hd_enabled),
                "regenerate_hd_reason": (
                    None if gateway.settings.regenerate_hd_enabled else
                    "Open-H3-Regenerate-2K needs ~150 GiB GPU and a 164.9 GB checkpoint; "
                    "the RTX PRO 6000 worker has 96 GiB. See docs/REGENERATE.md."
                ),
                "data": [
                    {**spec.public(), "available": bool(health.get("ready"))}
                    for spec in MODELS.values()
                    if not spec.hidden
                ],
            }

        @app.get("/v1/models/{model_id}")
        async def get_model_endpoint(model_id: str, request: Request):
            require(request)
            try:
                spec = get_model(model_id)
            except KeyError as exc:
                raise _error(str(exc), code="unknown_model", status_code=404) from exc
            return spec.public()

        @app.get("/usage")
        @app.get("/v1/usage")
        async def usage(request: Request):
            _key, owner = require(request)
            data = gateway.usage_public()
            data["is_admin"] = owner == 0
            return data

        # ---------------------------------------------------------- prompt preview
        @app.post("/v1/prompts/enhance")
        async def preview_prompt(request: Request):
            require(request)
            fields, refs = await gateway.parse_create_request(request)
            validated = gateway.validate_job(fields, refs)
            gateway._normalize_references(validated)
            validated["enhance_prompt"] = True
            result = await gateway._enhance_prompt(validated)
            return {
                "object": "prompt.enhancement",
                "mode": result.mode,
                "width": validated["width"],
                "height": validated["height"],
                "size": f"{validated['width']}x{validated['height']}",
                "seconds": validated["seconds"],
                "original": validated["prompt"],
                "enhanced": result.prompt,
                "applied": result.applied,
                "model": result.model,
                "images_sent": result.images_sent,
                "error": result.error,
            }

        # ---------------------------------------------------------- create
        @app.post("/v1/videos", status_code=status.HTTP_201_CREATED)
        async def create_video(request: Request):
            _key, owner = require(request)
            length = request.headers.get("content-length")
            if length:
                limit = (
                    self.settings.max_upload_bytes
                    + self.settings.max_video_reference_bytes
                    + self.settings.max_audio_reference_bytes
                    + 4 * 1024 * 1024
                )
                try:
                    too_large = int(length) > limit
                except ValueError:
                    too_large = False
                if too_large:
                    raise _error("Request body is too large", code="file_too_large", status_code=413)
            fields, references = await gateway.parse_create_request(request)
            video = await gateway.create_one(fields, references, owner=owner)
            return gateway.video_public(video)

        @app.post("/v1/videos/batch", status_code=status.HTTP_201_CREATED)
        async def create_batch(request: Request):
            _key, owner = require(request)
            length = request.headers.get("content-length")
            if length:
                limit = (
                    self.settings.max_upload_bytes
                    + self.settings.max_video_reference_bytes
                    + self.settings.max_audio_reference_bytes
                    + 4 * 1024 * 1024
                )
                try:
                    too_large = int(length) > limit
                except ValueError:
                    too_large = False
                if too_large:
                    raise _error("Batch body is too large", code="file_too_large", status_code=413)
            try:
                payload = await request.json()
            except Exception as exc:
                raise _error("Batch body must be valid JSON", code="invalid_json") from exc
            try:
                batch = BatchCreate.model_validate(payload)
            except Exception as exc:
                raise _error(f"Invalid batch: {exc}", code="validation_error") from exc
            if len(batch.items) > gateway.settings.max_batch_size:
                raise _error(
                    f"Batch limit is {gateway.settings.max_batch_size} items",
                    code="batch_too_large",
                    status_code=413,
                )
            # Validate every item before creating/billing anything.
            normalized: list[tuple[dict, list[IncomingReference]]] = []
            total_reserve = 0.0
            for item in batch.items:
                fields = item.model_dump(exclude_none=True)
                if batch.model and not fields.get("model"):
                    fields["model"] = batch.model
                item_refs = gateway._references_from_fields(fields)
                validated = gateway.validate_job(fields, item_refs)
                normalized.append((fields, item_refs))
                total_reserve += gateway.budget.reserve_for(validated["spec"], seconds=validated["seconds"], local=gateway.backend.demo)
            summary = gateway.budget.summary()
            if not gateway.backend.demo and total_reserve > summary["remaining_usd"] + 1e-9:
                raise _error(
                    f"Batch worst-case reserve ${total_reserve:.2f} exceeds remaining budget "
                    f"${summary['remaining_usd']:.2f}. Reduce the batch or wait for the next cycle.",
                    code="budget_exceeded",
                    type_="budget_error",
                    status_code=429,
                )
            model_id = batch.model or (normalized[0][0].get("model") if normalized else None) or gateway.settings.default_model
            batch_row = gateway.db.create_batch(
                model=model_id, total=len(normalized), label=batch.label, owner=owner
            )
            created = []
            for fields, item_refs in normalized:
                video = await gateway.create_one(
                    fields, item_refs, batch_id=batch_row["id"], owner=owner
                )
                created.append(video)
            return {
                "object": "list",
                "batch_id": batch_row["id"],
                "data": [gateway.video_public(video) for video in created],
            }

        # ---------------------------------------------------------- read
        @app.get("/v1/videos")
        async def list_videos(request: Request):
            _key, owner = require(request)
            try:
                limit = min(100, max(1, int(request.query_params.get("limit", "30"))))
                offset = max(0, int(request.query_params.get("offset", "0")))
            except ValueError as exc:
                raise _error("limit and offset must be integers", code="invalid_pagination") from exc
            status_filter = request.query_params.get("status")
            batch_id = request.query_params.get("batch_id")
            videos = gateway.db.list_videos(
                limit=limit, offset=offset, status=status_filter, batch_id=batch_id, owner=owner
            )
            return {
                "object": "list",
                "data": [gateway.video_public(video) for video in videos],
                "has_more": len(videos) == limit,
            }

        @app.get("/v1/videos/{video_id}")
        async def get_video(video_id: str, request: Request):
            _key, owner = require(request)
            video = gateway.owned_video(video_id, owner)
            if not video:
                raise _error("Video not found", code="not_found", status_code=404)
            return gateway.video_public(video)

        @app.get("/v1/batches/{batch_id}")
        async def get_batch(batch_id: str, request: Request):
            _key, owner = require(request)
            batch = gateway.db.get_batch(batch_id)
            if not batch or int(batch.get("owner") or 0) != owner:
                raise _error("Batch not found", code="not_found", status_code=404)
            videos = gateway.db.list_videos(batch_id=batch_id, limit=100, owner=owner)
            batch["videos"] = [gateway.video_public(video, include_prompt=False) for video in videos]
            return batch

        @app.get("/v1/batches")
        async def list_batches(request: Request):
            _key, owner = require(request)
            return {"object": "list", "data": gateway.db.list_batches(owner=owner)}

        # ---------------------------------------------------------- media
        def _authorized_media(request: Request, video_id: str, purpose: str) -> bool:
            if gateway.auth.check_content_token(request, video_id, purpose=purpose):
                # Media URLs are capability URLs: possession of the signed token
                # is sufficient for browser playback, without a Bearer header.
                return True
            owner = gateway.auth.key_owner(request)
            if owner is None:
                return False
            video = gateway.db.get_video(video_id)
            return bool(video and int(video.get("owner") or 0) == owner)

        @app.get("/v1/videos/{video_id}/content")
        async def video_content(video_id: str, request: Request, download: int = 0):
            if not _authorized_media(request, video_id, "content"):
                raise _error("Invalid or missing content token", code="invalid_token", status_code=401)
            video = gateway.db.get_video(video_id)
            if not video:
                raise _error("Video not found", code="not_found", status_code=404)
            if video["status"] != "completed" or not video.get("output_path"):
                raise _error("Video is not completed yet", code="not_ready", status_code=409)
            path = Path(video["output_path"])
            if not path.exists():
                raise _error("Video file is missing", code="file_missing", status_code=410)
            headers = {}
            if download:
                headers["Content-Disposition"] = f'attachment; filename="{video_id}.mp4"'
            return FileResponse(path, media_type="video/mp4", headers=headers or None)

        @app.get("/v1/videos/{video_id}/thumbnail")
        async def video_thumbnail(video_id: str, request: Request):
            if not _authorized_media(request, video_id, "thumbnail"):
                raise _error("Invalid or missing content token", code="invalid_token", status_code=401)
            video = gateway.db.get_video(video_id)
            if not video or not video.get("thumb_path"):
                raise _error("Thumbnail not found", code="not_found", status_code=404)
            path = Path(video["thumb_path"])
            if not path.exists():
                raise _error("Thumbnail missing", code="file_missing", status_code=410)
            return FileResponse(path, media_type="image/jpeg")

        # ---------------------------------------------------------- lifecycle
        @app.post("/v1/videos/{video_id}/cancel")
        async def cancel_video(video_id: str, request: Request):
            _key, owner = require(request)
            video = gateway.owned_video(video_id, owner)
            if not video:
                raise _error("Video not found", code="not_found", status_code=404)
            if video["status"] in {"completed", "failed", "cancelled", "expired"}:
                return gateway.video_public(video)
            if video["status"] in {"queued", "submitting"}:
                gateway.db.update_video(
                    video_id,
                    status="cancelled",
                    stage="cancelled",
                    completed_at=iso(),
                    reserved_usd=0.0,
                    error="cancelled by user",
                )
                gateway.db.add_event(video_id=video_id, level="warning", message="cancelled while queued")
            else:
                gateway.db.update_video(video_id, cancel_requested=1, stage="cancelling",
                                        error="cancellation requested")
                gateway.dispatcher.wake()
            return gateway.video_public(gateway.db.get_video(video_id) or video)

        @app.post("/v1/videos/{video_id}/regenerate")
        async def regenerate_video(video_id: str, request: Request):
            _key, owner = require(request)
            video = gateway.owned_video(video_id, owner)
            if not video:
                raise _error("Video not found", code="not_found", status_code=404)
            if video["status"] != "completed" or not video.get("output_path"):
                raise _error("Regenerate HD needs a completed video", code="not_ready", status_code=409)
            if not gateway.settings.regenerate_hd_enabled:
                raise _error(
                    "Regenerate HD is disabled: Open-H3-Regenerate-2K needs about 150 GiB "
                    "GPU memory and a 164.9 GB checkpoint. The current RTX PRO 6000 has "
                    "96 GiB. See docs/REGENERATE.md.",
                    code="regenerate_hd_unavailable",
                    type_="regenerate_error",
                    status_code=503,
                )
            raise _error(
                "Regenerate HD is enabled but no matching worker profile is configured.",
                code="regenerate_hd_unavailable",
                type_="regenerate_error",
                status_code=501,
            )

        @app.delete("/v1/videos/{video_id}")
        async def delete_video(video_id: str, request: Request):
            _key, owner = require(request)
            video = gateway.owned_video(video_id, owner)
            if not video:
                raise _error("Video not found", code="not_found", status_code=404)
            if video["status"] in {"queued", "submitting", "processing", "downloading"}:
                raise _error("Cancel the active job before deleting it", code="job_active", status_code=409)
            gateway.storage.remove_video_files(video)
            gateway.db.soft_delete(video_id)
            gateway.db.add_event(video_id=video_id, level="info", message="video deleted")
            return {"id": video_id, "object": "video.deleted", "deleted": True}

        @app.post("/v1/videos/{video_id}/retry", status_code=status.HTTP_201_CREATED)
        async def retry_video(video_id: str, request: Request):
            _key, owner = require(request)
            old = gateway.owned_video(video_id, owner)
            if not old:
                raise _error("Video not found", code="not_found", status_code=404)
            if old["status"] in {"queued", "submitting", "processing", "downloading"}:
                raise _error("Original job is still active", code="job_active", status_code=409)
            reference_meta: list[dict] = []
            try:
                reference_meta = json.loads(old.get("meta_json") or "{}").get("references", [])
            except Exception:
                reference_meta = []
            references: list[IncomingReference] = []
            for ref in reference_meta:
                path = Path(ref.get("path", ""))
                if path.exists():
                    references.append(IncomingReference(
                        media_type=ref.get("type", "image"),
                        data=path.read_bytes(),
                        mime=ref.get("mime"),
                        name=ref.get("name") or path.name,
                        role=ref.get("role", "reference"),
                    ))
            if not references and old.get("input_image_path"):
                path = Path(old["input_image_path"])
                if path.exists():
                    references = [IncomingReference("image", path.read_bytes(),
                                                    old.get("input_image_mime"), path.name,
                                                    role="first_frame")]
            fields = {
                "model": old["model"],
                "prompt": old["prompt"],
                "size": f"{old['width']}x{old['height']}",
                "seconds": old["seconds"],
                "seed": old["seed"],
                "with_audio": bool(old.get("with_audio")),
                "enhance_prompt": False,
            }
            video = await gateway.create_one(fields, references, owner=owner)
            return gateway.video_public(video)

        # ---------------------------------------------------------- admin
        @app.post("/v1/admin/billing/sync")
        async def billing_sync(request: Request):
            require_admin(request)
            return gateway.budget.sync_modal_billing(gateway.backend)

        @app.post("/v1/admin/billing/reconcile")
        async def billing_reconcile(body: ReconcileRequest, request: Request):
            require_admin(request)
            gateway.db.set_setting("billing_actual_usd", f"{body.billed_usd:.6f}")
            gateway.db.set_setting("billing_actual_at", iso())
            gateway.db.set_setting("billing_cycle_key", cycle_key(day=gateway.settings.billing_cycle_day))
            gateway.db.set_setting("billing_source", "manual")
            gateway.db.set_setting("billing_note", body.note or "")
            return {"ok": True, "billing_source": "manual", "billed_usd": body.billed_usd}

        @app.get("/v1/admin/events")
        async def admin_events(request: Request):
            require_admin(request)
            return {"object": "list", "data": gateway.db.recent_events(100)}

        @app.get("/v1/admin/diagnostics")
        async def admin_diagnostics(request: Request):
            require_admin(request)
            return {
                "backend": gateway.backend.health(),
                "usage": gateway.budget.summary(),
                "storage": gateway.storage.usage(),
                "database": str(gateway.settings.db_path),
                "models": list(MODELS),
                "worker": {
                    "gpu": gateway.settings.worker_gpu,
                    "cpu": gateway.settings.worker_cpu,
                    "memory_gib": gateway.settings.worker_memory_gib,
                    "scaledown_seconds": gateway.settings.worker_scaledown_seconds,
                },
                "events": gateway.db.recent_events(20),
            }

        @app.post("/v1/admin/cleanup")
        async def admin_cleanup(request: Request):
            require_admin(request)
            return {
                "expired_removed": gateway.dispatcher.cleanup_expired(),
                "storage_removed": gateway.dispatcher.cleanup_storage(),
                "storage": gateway.storage.usage(),
            }

        return app


def create_app(settings: Settings | None = None, *, start_dispatcher: bool = True,
               backend: Backend | None = None) -> FastAPI:
    gateway = Gateway(settings or Settings.from_env(), start_dispatcher=start_dispatcher, backend=backend)
    return gateway.create_app()
