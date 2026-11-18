from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from gateway.backend import PollResult
from gateway.config import Settings
from gateway.cost import BudgetManager
from gateway.db import Database, iso, utcnow
from gateway.dispatcher import Dispatcher
from gateway.storage import Storage


class FakeBackend:
    name = "fake"
    demo = False
    configured = True

    def __init__(self):
        self.submits = 0
        self.cancelled = 0
        self.result = None

    def submit(self, payload):
        self.submits += 1
        return "call_test"

    def poll(self, call_id, job_id):
        if self.result:
            return PollResult("done", result=self.result)
        return PollResult("running")

    def fetch_video(self, job_id, result, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake")

    def cancel(self, call_id):
        self.cancelled += 1

    def progress(self, job_id):
        return {"stage": "generating", "pct": 50}

    def health(self):
        return {"backend": "fake", "ready": True, "demo": False}

    def billing_summary(self, start):
        return {"total_usd": 0.0}


def make(tmp_path: Path, **kwargs):
    settings = Settings(data_dir=tmp_path, api_keys=["x"], backend="modal", **kwargs)
    settings.ensure()
    db = Database(settings.db_path)
    storage = Storage(settings)
    backend = FakeBackend()
    budget = BudgetManager(settings, db)
    return settings, db, storage, backend, budget, Dispatcher(settings, db, storage, backend, budget)


def test_budget_rejects_before_any_spawn(tmp_path: Path):
    _, db, _, backend, _, dispatcher = make(
        tmp_path, monthly_credits_usd=30, monthly_soft_budget_usd=0.01
    )
    job = db.create_video(
        model="fasth3-v2", prompt="x", width=832, height=480, seconds=5, fps=24,
        status="queued", reserved_usd=0.18,
    )
    claimed = db.claim_next_queued()
    assert claimed["id"] == job["id"]
    dispatcher._advance(claimed)
    assert backend.submits == 0
    assert db.get_video(job["id"])["status"] == "queued"


def test_interrupted_submission_is_not_retried(tmp_path: Path):
    _, db, _, backend, _, dispatcher = make(tmp_path)
    old = iso(utcnow() - timedelta(minutes=10))
    job = db.create_video(
        model="fasth3-v2", prompt="x", width=832, height=480, seconds=5, fps=24,
        status="submitting", created_at=old, reserved_usd=0.18,
    )
    dispatcher._recover()
    stored = db.get_video(job["id"])
    assert stored["status"] == "failed"
    assert stored["reserved_usd"] == 0
    assert "not retried" in stored["error"].lower()
    assert backend.submits == 0


def test_recovery_finds_a_completed_result(tmp_path: Path):
    _, db, _, backend, _, dispatcher = make(tmp_path)
    job = db.create_video(
        model="fasth3-v2", prompt="x", width=832, height=480, seconds=5, fps=24,
        status="submitting", reserved_usd=0.18,
    )
    backend.result = {"ok": True, "video_filename": "video.mp4", "total_s": 30}
    dispatcher._recover()
    stored = db.get_video(job["id"])
    assert stored["status"] == "downloading"
    assert backend.submits == 0


def test_running_job_is_polled_not_resubmitted(tmp_path: Path):
    _, db, _, backend, _, dispatcher = make(tmp_path)
    job = db.create_video(
        model="fasth3-v2", prompt="x", width=832, height=480, seconds=5, fps=24,
        status="processing", modal_call_id="call_existing", started_at=iso(),
    )
    dispatcher._current_job = job["id"]
    dispatcher._tick()
    assert backend.submits == 0
    assert db.get_video(job["id"])["stage"] in {"processing", "starting", "generating"}


def test_stale_submission_uses_claim_time_not_creation_time(tmp_path: Path):
    """Regression: a batch job can be minutes old before it is claimed. The
    stale check must measure time since `submitting`, not since creation."""
    _, db, _, _, _, dispatcher = make(tmp_path)
    old_created = iso(utcnow() - timedelta(minutes=10))
    job = db.create_video(
        model="fasth3-v2", prompt="x", width=832, height=480, seconds=5, fps=24,
        status="submitting", created_at=old_created, started_at=iso(), reserved_usd=0.2,
    )
    dispatcher._reap_stale_submissions()
    assert db.get_video(job["id"])["status"] == "submitting"

    stale = db.create_video(
        model="fasth3-v2", prompt="x", width=832, height=480, seconds=5, fps=24,
        status="submitting", created_at=old_created,
        started_at=iso(utcnow() - timedelta(minutes=10)), reserved_usd=0.2,
    )
    dispatcher._reap_stale_submissions()
    assert db.get_video(stale["id"])["status"] == "failed"
