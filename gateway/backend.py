"""Worker backends: real Modal functions and a local demo/test backend."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .postprocess import ffmpeg_bin

logger = logging.getLogger(__name__)


@dataclass
class PollResult:
    state: str  # running | done | failed
    result: dict | None = None
    error: str | None = None


class Backend(Protocol):
    name: str
    configured: bool
    demo: bool

    def submit(self, payload: dict[str, Any]) -> str: ...

    def poll(self, call_id: str | None, job_id: str) -> PollResult: ...

    def fetch_video(self, job_id: str, result: dict[str, Any], destination: Path) -> None: ...

    def cancel(self, call_id: str | None) -> None: ...

    def progress(self, job_id: str) -> dict | None: ...

    def health(self) -> dict: ...

    def billing_summary(self, start: datetime) -> dict | None: ...


def _has_modal_credentials() -> bool:
    import os

    if os.getenv("MODAL_TOKEN_ID") and os.getenv("MODAL_TOKEN_SECRET"):
        return True
    if os.getenv("MODAL_CONFIG_PATH"):
        return True
    return (Path.home() / ".modal.toml").exists()


class LocalBackend:
    """Generates a synthetic clip locally so the API/UI can be exercised.

    This is explicitly a demo backend. It never claims to be a GPU worker and
    reports `demo: true` everywhere, so a local deployment cannot be mistaken
    for a real one.
    """

    name = "local"
    demo = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self.configured = True
        self._states: dict[str, dict] = {}
        self._progress: dict[str, dict] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def submit(self, payload: dict[str, Any]) -> str:
        call_id = "local_" + uuid.uuid4().hex
        cancel = threading.Event()
        state = {"status": "running", "result": None, "error": None}
        with self._lock:
            self._states[call_id] = state
            self._progress[payload["job_id"]] = {"stage": "starting", "pct": 2, "ts": time.time()}
            self._cancel[call_id] = cancel
        thread = threading.Thread(
            target=self._run,
            args=(call_id, payload, state, cancel),
            name=f"local-worker-{payload['job_id'][-6:]}",
            daemon=True,
        )
        thread.start()
        return call_id

    def _run(self, call_id: str, payload: dict, state: dict, cancel: threading.Event) -> None:
        job_id = payload["job_id"]
        try:
            output = self.settings.tmp_dir / f"{job_id}.local.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            self._set_progress(job_id, "generating", 20)
            width, height = int(payload["width"]), int(payload["height"])
            frames = int(payload.get("frames", 121))
            fps = int(payload.get("fps", 24))
            duration = max(1.0, frames / fps)
            command = [
                ffmpeg_bin(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=44100:duration={duration}",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(output),
            ]
            # Simulate a short queue/load so the UI states are visible.
            for pct, stage in ((30, "loading"), (55, "generating"), (80, "encoding")):
                for _ in range(10):
                    if cancel.is_set():
                        raise RuntimeError("cancelled")
                    time.sleep(0.1)
                self._set_progress(job_id, stage, pct)
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
            if cancel.is_set():
                raise RuntimeError("cancelled")
            self._set_progress(job_id, "finishing", 95)
            state["result"] = {
                "ok": True,
                "job_id": job_id,
                "model": payload.get("model"),
                "video_filename": output.name,
                "width": width,
                "height": height,
                "frames": frames,
                "fps": fps,
                "has_audio": True,
                "duration_s": duration,
                "cold_start": True,
                "model_load_s": 0.0,
                "inference_s": duration,
                "encode_s": 0.1,
                "total_s": 3.0,
                "peak_vram_mb": 0,
                "gpu_name": "local-demo",
                "backend": "local",
                "demo": True,
            }
            state["status"] = "done"
        except Exception as exc:  # noqa: BLE001
            state["status"] = "failed"
            state["error"] = str(exc)
        finally:
            with self._lock:
                self._cancel.pop(call_id, None)

    def _set_progress(self, job_id: str, stage: str, pct: int) -> None:
        with self._lock:
            self._progress[job_id] = {"stage": stage, "pct": pct, "ts": time.time()}

    def poll(self, call_id: str | None, job_id: str) -> PollResult:
        with self._lock:
            state = self._states.get(call_id or "")
            if not state:
                return PollResult("failed", error="local call not found")
            if state["status"] == "done":
                return PollResult("done", result=state["result"])
            if state["status"] == "failed":
                return PollResult("failed", error=state["error"] or "local generation failed")
        return PollResult("running")

    def fetch_video(self, job_id: str, result: dict[str, Any], destination: Path) -> None:
        source = self.settings.tmp_dir / result["video_filename"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        try:
            source.unlink()
        except OSError:
            pass

    def cancel(self, call_id: str | None) -> None:
        if not call_id:
            return
        with self._lock:
            event = self._cancel.get(call_id)
        if event:
            event.set()

    def progress(self, job_id: str) -> dict | None:
        with self._lock:
            return dict(self._progress.get(job_id) or {})

    def health(self) -> dict:
        return {
            "backend": "local",
            "ready": True,
            "demo": True,
            "message": "Local demo backend: outputs are synthetic test patterns, not AI video.",
        }

    def billing_summary(self, start: datetime) -> dict | None:
        return None


class ModalBackend:
    name = "modal"
    demo = False

    def __init__(self, settings: Settings):
        self.settings = settings
        self.configured = _has_modal_credentials()
        self._function = None
        self._results = None
        self._progress_dict = None
        self._health_cache: tuple[float, dict] | None = None
        self._last_reload = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ setup
    def _modal(self):
        import modal

        return modal

    def _function_ref(self):
        if self._function is None:
            modal = self._modal()
            kwargs = {}
            if self.settings.modal_environment:
                kwargs["environment_name"] = self.settings.modal_environment
            self._function = modal.Function.from_name(
                self.settings.modal_app_name,
                self.settings.modal_function_name,
                **kwargs,
            )
        return self._function

    def _volume_ref(self):
        if self._results is None:
            modal = self._modal()
            kwargs = {"create_if_missing": False}
            if self.settings.modal_environment:
                kwargs["environment_name"] = self.settings.modal_environment
            self._results = modal.Volume.from_name(self.settings.modal_results_volume, **kwargs)
        return self._results

    def _progress_ref(self):
        if self._progress_dict is None:
            modal = self._modal()
            kwargs = {"create_if_missing": False}
            if self.settings.modal_environment:
                kwargs["environment_name"] = self.settings.modal_environment
            self._progress_dict = modal.Dict.from_name(self.settings.modal_progress_dict, **kwargs)
        return self._progress_dict

    # ------------------------------------------------------------------ protocol
    def submit(self, payload: dict[str, Any]) -> str:
        if not self.configured:
            raise RuntimeError(
                "Modal credentials are not configured. Run `modal token set` for the gateway user "
                "or set MODAL_TOKEN_ID/MODAL_TOKEN_SECRET."
            )
        function = self._function_ref()
        call = function.spawn(payload)
        return call.object_id

    def _read_result(self, job_id: str, *, reload: bool = False) -> dict | None:
        import io

        volume = self._volume_ref()
        now = time.monotonic()
        if reload and now - self._last_reload > 5:
            try:
                volume.reload()
                self._last_reload = now
            except Exception:
                pass
        buffer = io.BytesIO()
        try:
            volume.read_file_into_fileobj(f"results/{job_id}/result.json", buffer)
        except Exception:
            return None
        try:
            return json.loads(buffer.getvalue().decode("utf-8"))
        except Exception:
            return None

    def poll(self, call_id: str | None, job_id: str) -> PollResult:
        result = self._read_result(job_id, reload=True)
        if result is not None:
            if result.get("ok"):
                return PollResult("done", result=result)
            return PollResult("failed", error=result.get("error") or "worker reported failure")
        if not call_id:
            return PollResult("failed", error="missing Modal call id")
        modal = self._modal()
        try:
            call = modal.FunctionCall.from_id(call_id)
            value = call.get(timeout=0)
            if isinstance(value, dict):
                if value.get("ok"):
                    return PollResult("done", result=value)
                return PollResult("failed", error=value.get("error") or "worker reported failure")
            return PollResult("done", result={"ok": True})
        except (TimeoutError, modal.exception.TimeoutError):
            return PollResult("running")
        except modal.exception.NotFoundError:
            # The call may have been removed by a redeploy. The result volume
            # was already checked above, so this is a genuine loss of tracking.
            return PollResult("failed", error="Modal call not found (app redeployed or call expired)")
        except modal.exception.OutputExpiredError:
            return PollResult("failed", error="Modal output expired before it could be downloaded")
        except (modal.exception.ConnectionError, modal.exception.InternalError):
            return PollResult("running")
        except modal.exception.FunctionTimeoutError as exc:
            return PollResult("failed", error=f"Modal function timeout: {exc}")
        except modal.exception.ExecutionError as exc:
            return PollResult("failed", error=f"Modal execution error: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Modal poll error for %s: %s", job_id, exc)
            return PollResult("running")

    def fetch_video(self, job_id: str, result: dict[str, Any], destination: Path) -> None:
        volume = self._volume_ref()
        try:
            volume.reload()
        except Exception:
            pass
        filename = result.get("video_filename") or "video.mp4"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "wb") as handle:
            volume.read_file_into_fileobj(f"results/{job_id}/{filename}", handle)

    def cancel(self, call_id: str | None) -> None:
        if not call_id:
            return
        modal = self._modal()
        try:
            call = modal.FunctionCall.from_id(call_id)
            call.cancel()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not cancel Modal call %s: %s", call_id, exc)

    def progress(self, job_id: str) -> dict | None:
        try:
            value = self._progress_ref().get(job_id)
            return dict(value) if isinstance(value, dict) else None
        except Exception:
            return None

    def health(self) -> dict:
        now = time.monotonic()
        if self._health_cache and now - self._health_cache[0] < 60:
            return dict(self._health_cache[1])
        result = {
            "backend": "modal",
            "ready": False,
            "demo": False,
            "app": self.settings.modal_app_name,
            "function": self.settings.modal_function_name,
            "gpu": self.settings.worker_gpu,
            "message": "",
        }
        if not self.configured:
            result["message"] = "Modal credentials missing; run `modal token set`."
        else:
            try:
                info = self._function_ref().info()
                result["ready"] = True
                result["function_id"] = getattr(info, "object_id", None)
                result["message"] = "Modal function resolved."
            except Exception as exc:  # noqa: BLE001
                result["message"] = f"Cannot resolve Modal function: {exc}"
        self._health_cache = (now, result)
        return dict(result)

    def billing_summary(self, start: datetime) -> dict | None:
        if not self.configured:
            return None
        import modal

        client = modal.Client.from_env()
        workspace = modal.Workspace.from_context(client=client)
        # Modal's official billing summary is calendar-month aligned. The SDK
        # exposes this through a blocking live method.
        cycle = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        summary = workspace.billing.summary(cycle=cycle)
        metered = getattr(summary, "metered_cost", None)
        billed = getattr(summary, "billed_cost", None)
        return {
            "total_usd": float(metered if metered is not None else billed or 0),
            "metered_usd": float(metered or 0),
            "billed_usd": float(billed or 0),
            "breakdown": str(getattr(summary, "metered_cost_breakdown", "")),
        }


def build_backend(settings: Settings) -> Backend:
    requested = settings.backend
    if requested == "local":
        logger.warning("Using LOCAL DEMO backend; outputs are synthetic test patterns.")
        return LocalBackend(settings)
    if requested == "modal":
        backend = ModalBackend(settings)
        if not backend.configured:
            logger.error("VIDEO_BACKEND=modal but no Modal credentials were found.")
        return backend
    if requested == "auto":
        if _has_modal_credentials():
            return ModalBackend(settings)
        logger.warning(
            "No Modal credentials found; starting LOCAL DEMO backend. Set MODAL_TOKEN_ID/"
            "MODAL_TOKEN_SECRET (or run `modal token set`) and restart for real generation."
        )
        return LocalBackend(settings)
    raise ValueError(f"Unknown VIDEO_BACKEND {requested!r}; use modal, local or auto")
