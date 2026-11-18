"""Single-GPU job dispatcher.

Only one job is sent to Modal at a time. The dispatcher persists the Modal call
id before polling, recovers calls after a gateway restart, and never retries a
submission automatically: a retry would be a second billable GPU run.
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .backend import Backend, PollResult
from .config import Settings, frames_for_duration, get_model
from .cost import BudgetManager
from .db import Database, iso, utcnow
from .postprocess import probe, transcode
from .storage import Storage

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, settings: Settings, db: Database, storage: Storage,
                 backend: Backend, budget: BudgetManager):
        self.settings = settings
        self.db = db
        self.storage = storage
        self.backend = backend
        self.budget = budget
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._current_job: str | None = None
        self._last_billing_sync = 0.0
        self._last_cleanup = 0.0
        self._last_budget_block: str | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._recover()
        self._thread = threading.Thread(target=self._run, name="job-dispatcher", daemon=True)
        self._thread.start()
        logger.info("Dispatcher started (backend=%s, configured=%s)", self.backend.name, self.backend.configured)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake.set()

    def _recover(self) -> None:
        jobs = self.db.active_jobs()
        for job in jobs:
            status = job["status"]
            if status == "submitting" and not job.get("modal_call_id"):
                # A spawn may have succeeded just before a crash. Check the
                # result volume first; only fail if there is definitively no
                # result, and never resubmit automatically.
                poll = self.backend.poll(None, job["id"])
                if poll.state == "done" and poll.result:
                    self._set_meta(job, worker_result=poll.result)
                    self.db.update_video(job["id"], status="downloading", stage="downloading", progress=90)
                    continue
                claimed_at = job.get("started_at") or job.get("created_at")
                age = 0.0
                if claimed_at:
                    try:
                        age = (utcnow() - datetime.fromisoformat(claimed_at)).total_seconds()
                    except ValueError:
                        age = 0.0
                if age > 120:
                    self.db.update_video(
                        job["id"],
                        status="failed",
                        stage="failed",
                        completed_at=iso(),
                        reserved_usd=0.0,
                        error="Submission was interrupted before a Modal call id was saved. "
                              "Not retried automatically to avoid duplicate GPU billing.",
                    )
                    self.db.add_event(
                        video_id=job["id"], level="error",
                        message="interrupted submission marked failed (no automatic retry)",
                    )
            elif status == "processing" and not job.get("modal_call_id"):
                self.db.update_video(
                    job["id"],
                    status="failed",
                    stage="failed",
                    completed_at=iso(),
                    reserved_usd=0.0,
                    error="Processing job has no Modal call id. Not retried automatically.",
                )
        # Prefer an already-running job over claiming a new one.
        for job in self.db.active_jobs():
            if job["status"] in {"processing", "downloading"}:
                self._current_job = job["id"]
                break

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                did_work = self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Dispatcher tick failed")
                self.db.add_event(video_id=None, level="error", message="dispatcher tick failed", detail=str(exc))
                did_work = False
            self._maybe_sync_billing()
            self._maybe_cleanup()
            if not did_work:
                self._wake.wait(timeout=3.0)
                self._wake.clear()

    def _tick(self) -> bool:
        if self._stop.is_set():
            return False
        self._reap_stale_submissions()
        if not self._current_job:
            active = [j for j in self.db.active_jobs() if j["status"] in {"processing", "downloading"}]
            if active:
                self._current_job = active[0]["id"]
            else:
                job = self.db.claim_next_queued()
                if job:
                    self._current_job = job["id"]
                    return True
                return False
        job = self.db.get_video(self._current_job)
        if not job:
            self._current_job = None
            return True
        if job["status"] in {"completed", "failed", "cancelled", "expired"}:
            self._current_job = None
            return True
        return self._advance(job)

    def _reap_stale_submissions(self) -> None:
        for job in self.db.active_jobs():
            if job["status"] != "submitting" or job.get("modal_call_id"):
                continue
            claimed_at = job.get("started_at") or job.get("created_at")
            try:
                age = (utcnow() - datetime.fromisoformat(claimed_at)).total_seconds() if claimed_at else 0
            except ValueError:
                age = 0
            if age > 120:
                self.db.update_video(
                    job["id"],
                    status="failed",
                    stage="failed",
                    completed_at=iso(),
                    reserved_usd=0.0,
                    error="Submission was interrupted before a Modal call id was saved. "
                          "Not retried automatically to avoid duplicate GPU billing.",
                )
                self.db.add_event(
                    video_id=job["id"], level="error",
                    message="stale submission marked failed (no automatic retry)",
                )

    def _meta(self, job: dict) -> dict:
        try:
            return json.loads(job.get("meta_json") or "{}")
        except Exception:
            return {}

    def _set_meta(self, job: dict, **updates: Any) -> None:
        meta = self._meta(job)
        meta.update(updates)
        self.db.update_video(job["id"], meta_json=json.dumps(meta, separators=(",", ":"), default=str))

    def _advance(self, job: dict) -> bool:
        if job.get("cancel_requested") and job["status"] in {"queued", "submitting"}:
            self._finalize_cancelled(job)
            return True
        status = job["status"]
        if status == "submitting":
            if job.get("modal_call_id"):
                self.db.update_video(job["id"], status="processing")
                return True
            return self._submit(job)
        if status == "processing":
            return self._poll(job)
        if status == "downloading":
            return self._finalize_download(job)
        return False

    # ------------------------------------------------------------------ submit
    def _payload(self, job: dict) -> dict:
        spec = get_model(job["model"])
        payload: dict[str, Any] = {
            "job_id": job["id"],
            "model": spec.worker_model,
            "model_repo": spec.repo,
            "prompt": job["prompt"],
            "width": int(job["width"]),
            "height": int(job["height"]),
            "frames": int(self._meta(job).get("frames") or frames_for_duration(spec, job["seconds"])),
            "seconds": float(job["seconds"]),
            "fps": int(spec.fps),
            "steps": int(spec.steps),
            "guidance": float(spec.guidance),
            "seed": int(job["seed"] if job.get("seed") is not None else 0),
            "result_volume": self.settings.modal_results_volume,
            "progress_dict": self.settings.modal_progress_dict,
            "job_timeout_s": self.settings.job_timeout_seconds,
        }
        refs = self._meta(job).get("references", [])
        if not refs and job.get("input_image_path"):
            refs = [{
                "type": "image",
                "role": "first_frame",
                "path": job["input_image_path"],
                "mime": job.get("input_image_mime") or "image/jpeg",
                "name": Path(job["input_image_path"]).name,
            }]
        payload["references"] = []
        for ref in refs:
            path = Path(ref.get("path", ""))
            if not path.exists():
                continue
            role = ref.get("role")
            if not role:
                # Jobs created before roles existed stored the first image in
                # input_image_path. OmniRef's ordered refs keep role=reference.
                role = "reference" if spec.supports_references else "first_frame"
            payload["references"].append({
                "type": ref.get("type", "image"),
                "role": role,
                "mime": ref.get("mime"),
                "name": ref.get("name") or path.name,
                "b64": base64.b64encode(path.read_bytes()).decode("ascii"),
            })
        return payload

    def _submit(self, job: dict) -> bool:
        if not self.backend.configured:
            self.db.update_video(
                job["id"],
                status="failed",
                stage="failed",
                completed_at=iso(),
                reserved_usd=0.0,
                error="Modal credentials are not configured. The job was not submitted and was not billed.",
            )
            self._current_job = None
            return True
        allowed, reason, _summary = self.budget.check_admission(
            get_model(job["model"]), seconds=job["seconds"], local=self.backend.demo
        )
        if not allowed:
            # Return it to the queue and stop submitting. The reservation stays
            # in place so a burst of queued jobs cannot bypass the budget.
            self.db.update_video(job["id"], status="queued", stage="queued",
                                 error=f"Budget guard: {reason}")
            self.db.add_event(video_id=job["id"], level="warning", message="job blocked by budget guard",
                              detail=reason)
            self._current_job = None
            self._last_budget_block = reason
            self._wake.wait(timeout=60)
            return False
        try:
            self.db.update_video(job["id"], stage="submitting", error=None)
            call_id = self.backend.submit(self._payload(job))
            self.db.update_video(
                job["id"],
                status="processing",
                stage="starting",
                started_at=iso(),
                modal_call_id=call_id,
                progress=max(1, int(job.get("progress") or 1)),
            )
            self.db.add_event(video_id=job["id"], level="info", message="submitted to Modal",
                              detail={"call_id": call_id})
            logger.info("Submitted %s as %s", job["id"], call_id)
            return True
        except Exception as exc:
            logger.exception("Submit failed for %s", job["id"])
            self.db.update_video(
                job["id"],
                status="failed",
                stage="failed",
                completed_at=iso(),
                reserved_usd=0.0,
                error=f"Submission failed before the GPU worker started: {exc}",
            )
            self.db.add_event(video_id=job["id"], level="error", message="submit failed", detail=str(exc))
            self._current_job = None
            return True

    # ------------------------------------------------------------------ poll
    def _poll(self, job: dict) -> bool:
        if job.get("cancel_requested"):
            self.backend.cancel(job.get("modal_call_id"))
            self._finalize_cancelled(job)
            return True
        started = job.get("started_at")
        if started:
            try:
                elapsed = (utcnow() - datetime.fromisoformat(started)).total_seconds()
            except ValueError:
                elapsed = 0.0
            if elapsed > self.settings.job_timeout_seconds:
                self.backend.cancel(job.get("modal_call_id"))
                self._finalize_failure(job, f"Job exceeded timeout of {self.settings.job_timeout_seconds}s", {})
                return True
        poll: PollResult = self.backend.poll(job.get("modal_call_id"), job["id"])
        if poll.state == "running":
            progress = self.backend.progress(job["id"]) or {}
            updates: dict[str, Any] = {}
            if progress.get("stage"):
                updates["stage"] = str(progress["stage"])[:80]
            pct = progress.get("pct")
            if isinstance(pct, (int, float)):
                updates["progress"] = max(0, min(99, int(pct)))
            if updates:
                self.db.update_video(job["id"], **updates)
            return False
        if poll.state == "done" and poll.result:
            self._set_meta(job, worker_result=poll.result)
            self.db.update_video(job["id"], status="downloading", stage="downloading", progress=90)
            job = self.db.get_video(job["id"]) or job
            return self._finalize_download(job)
        self._finalize_failure(job, poll.error or "Worker failed without an error message", poll.result or {})
        return True

    # ------------------------------------------------------------------ finalize
    def _finalize_download(self, job: dict) -> bool:
        started = job.get("started_at")
        if started:
            try:
                elapsed = (utcnow() - datetime.fromisoformat(started)).total_seconds()
            except ValueError:
                elapsed = 0.0
            if elapsed > self.settings.job_timeout_seconds + 300:
                self._finalize_failure(job, "Timed out waiting for the result download", {})
                return True
        meta = self._meta(job)
        result = meta.get("worker_result")
        if not result:
            # Recovery case: the result volume may already have the manifest.
            poll = self.backend.poll(job.get("modal_call_id"), job["id"])
            if poll.state == "done" and poll.result:
                result = poll.result
                self._set_meta(job, worker_result=result)
            else:
                return False
        raw = self.storage.raw_path(job["id"])
        output = self.storage.output_path(job["id"])
        thumb = self.storage.thumb_path(job["id"])
        fetch_error = None
        for attempt in range(3):
            try:
                self.backend.fetch_video(job["id"], result, raw)
                fetch_error = None
                break
            except Exception as exc:  # noqa: BLE001
                fetch_error = exc
                logger.warning("Result download attempt %d failed for %s: %s", attempt + 1, job["id"], exc)
                time.sleep(2)
        if fetch_error is not None:
            self._finalize_failure(job, f"Could not download the generated video: {fetch_error}", result)
            return True
        warning = None
        info: dict = {}
        try:
            info = transcode(raw, output, thumb)
        except Exception as exc:
            logger.exception("FFmpeg post-processing failed for %s", job["id"])
            warning = f"post-processing failed ({exc}); serving raw worker MP4"
            try:
                shutil.copyfile(raw, output)
                info = probe(output)
                info["bytes"] = output.stat().st_size
            except Exception as copy_exc:  # noqa: BLE001
                self._finalize_failure(job, f"Could not post-process or keep the video: {copy_exc}", result)
                return True
        finally:
            self.storage.remove(raw)
        cost = self.budget.actual(result, local=self.backend.demo)
        meta = self._meta(job)
        meta["worker_result"] = result
        if warning:
            meta["warning"] = warning
        meta["postprocess"] = {
            "width": info.get("width"),
            "height": info.get("height"),
            "duration_s": info.get("duration_s"),
            "has_audio": info.get("has_audio"),
        }
        self.db.update_video(
            job["id"],
            status="completed",
            stage="completed",
            progress=100,
            completed_at=iso(),
            reserved_usd=0.0,
            cost_usd=round(cost.usd, 6),
            gpu_seconds=round(float(result.get("total_s") or 0), 3),
            cpu_seconds=round(float(cost.seconds) * self.settings.worker_cpu, 3),
            mem_gib_seconds=round(float(cost.seconds) * self.settings.worker_memory_gib, 3),
            model_load_s=result.get("model_load_s"),
            inference_s=result.get("inference_s"),
            encode_s=result.get("encode_s"),
            peak_vram_mb=result.get("peak_vram_mb"),
            cold_start=1 if result.get("cold_start") else 0,
            output_path=str(output),
            thumb_path=str(thumb) if thumb.exists() else None,
            output_bytes=info.get("bytes") or output.stat().st_size,
            duration_s=info.get("duration_s") or result.get("duration_s"),
            error=None,
            meta_json=json.dumps(meta, separators=(",", ":"), default=str),
        )
        if not self.backend.demo and cost.usd > 0:
            self.db.add_ledger(
                video_id=job["id"],
                kind="generation",
                amount_usd=cost.usd,
                detail=f"{job['model']} {job['width']}x{job['height']} {cost.seconds:.1f}s",
            )
        self.db.add_event(
            video_id=job["id"], level="info", message="video completed",
            detail={"cost_usd": round(cost.usd, 4), "seconds": round(cost.seconds, 1),
                    "warning": warning},
        )
        logger.info("Completed %s in %.1fs (est $%.4f)", job["id"], cost.seconds, cost.usd)
        self._current_job = None
        self._wake.set()
        return True

    def _finalize_failure(self, job: dict, error: str, result: dict | None) -> None:
        result = result or {}
        cost = self.budget.actual(result, local=self.backend.demo)
        if not result and job.get("started_at"):
            try:
                elapsed = (utcnow() - datetime.fromisoformat(job["started_at"])).total_seconds()
            except ValueError:
                elapsed = 0.0
            cost = self.budget.cost_for_seconds(max(10.0, elapsed + self.settings.worker_scaledown_seconds))
        meta = self._meta(job)
        if result:
            meta["worker_result"] = result
        self.db.update_video(
            job["id"],
            status="failed",
            stage="failed",
            completed_at=iso(),
            reserved_usd=0.0,
            cost_usd=round(cost.usd, 6) if not self.backend.demo else 0.0,
            gpu_seconds=result.get("total_s"),
            error=error[:4000],
            meta_json=json.dumps(meta, separators=(",", ":"), default=str),
        )
        if not self.backend.demo and cost.usd > 0:
            self.db.add_ledger(
                video_id=job["id"],
                kind="generation",
                amount_usd=cost.usd,
                detail=f"failed run {job['model']} ({cost.seconds:.1f}s)",
            )
        self.db.add_event(video_id=job["id"], level="error", message="video failed", detail=error)
        logger.error("Job %s failed: %s", job["id"], error)
        self._current_job = None
        self._wake.set()

    def _finalize_cancelled(self, job: dict) -> None:
        if job["status"] in {"queued", "submitting"} and not job.get("started_at"):
            cost_usd = 0.0
            cost_seconds = 0.0
        else:
            cost = self.budget.cost_for_seconds(max(10.0, self.settings.worker_scaledown_seconds))
            if not self.backend.demo and job.get("started_at"):
                try:
                    elapsed = (utcnow() - datetime.fromisoformat(job["started_at"])).total_seconds()
                    cost = self.budget.cost_for_seconds(elapsed + self.settings.worker_scaledown_seconds)
                except ValueError:
                    pass
            cost_usd = 0.0 if self.backend.demo else round(cost.usd, 6)
            cost_seconds = cost.seconds
        self.db.update_video(
            job["id"],
            status="cancelled",
            stage="cancelled",
            completed_at=iso(),
            reserved_usd=0.0,
            cost_usd=cost_usd,
            error=job.get("error") or "cancelled by user",
        )
        if not self.backend.demo and cost_usd > 0:
            self.db.add_ledger(
                video_id=job["id"],
                kind="generation",
                amount_usd=cost_usd,
                detail=f"cancelled run {job['model']} ({cost_seconds:.1f}s)",
            )
        self.db.add_event(video_id=job["id"], level="warning", message="video cancelled")
        self._current_job = None
        self._wake.set()

    # ------------------------------------------------------------------ maintenance
    def _maybe_sync_billing(self) -> None:
        if not self.settings.billing_sync_enabled or self.backend.demo:
            return
        now = time.monotonic()
        if now - self._last_billing_sync < self.settings.billing_sync_interval_seconds:
            return
        self._last_billing_sync = now
        try:
            self.budget.sync_modal_billing(self.backend)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Billing sync failed: %s", exc)

    def _maybe_cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < 3600:
            return
        self._last_cleanup = now
        try:
            self.cleanup_expired()
            self.cleanup_storage()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cleanup failed: %s", exc)

    def cleanup_expired(self) -> int:
        cutoff = iso(utcnow() - timedelta(days=self.settings.retention_days))
        removed = 0
        for video in self.db.expired_videos(cutoff):
            self.storage.remove_video_files(video)
            self.db.soft_delete(video["id"])
            removed += 1
        if removed:
            logger.info("Retention cleanup removed %d videos", removed)
            self.db.add_event(video_id=None, level="info", message=f"retention cleanup removed {removed} videos")
        return removed

    def cleanup_storage(self) -> int:
        usage = self.storage.usage()
        limit = int(self.settings.max_storage_gb * 1e9)
        if usage["bytes"] <= limit:
            return 0
        removed = 0
        for video in self.db.list_videos(status="completed", limit=200):
            if usage["bytes"] <= limit:
                break
            size = int(video.get("output_bytes") or 0)
            self.storage.remove_video_files(video)
            self.db.soft_delete(video["id"])
            usage["bytes"] -= size
            removed += 1
        if removed:
            logger.info("Storage cleanup removed %d videos", removed)
            self.db.add_event(video_id=None, level="info", message=f"storage cleanup removed {removed} videos")
        return removed
