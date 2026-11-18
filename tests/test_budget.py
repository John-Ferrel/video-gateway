from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from gateway.config import MODELS, Settings
from gateway.cost import BudgetManager, cycle_start
from gateway.db import Database, iso, utcnow


def test_cycle_start():
    now = datetime(2026, 11, 17, 12, tzinfo=UTC)
    assert cycle_start(now, 1) == datetime(2026, 11, 1, tzinfo=UTC)
    assert cycle_start(now, 20) == datetime(2026, 10, 20, tzinfo=UTC)
    assert cycle_start(datetime(2026, 11, 25, tzinfo=UTC), 20) == datetime(
        2026, 11, 20, tzinfo=UTC
    )
    jan = datetime(2026, 1, 5, tzinfo=UTC)
    assert cycle_start(jan, 20).year == 2025
    assert cycle_start(jan, 20).month == 12


def test_estimate_matches_modal_rates(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, api_keys=["x"], backend="local")
    settings.ensure()
    db = Database(settings.db_path)
    budget = BudgetManager(settings, db)
    lite = budget.estimate(MODELS["fasth3-v2"], cold=True)
    warm = budget.estimate(MODELS["fasth3-v2"], cold=False)
    pro = budget.estimate(MODELS["fasth3-omniref"], cold=True)
    assert 0.25 < lite.usd < 0.40
    assert warm.usd < lite.usd
    assert pro.usd > lite.usd
    long = budget.estimate(MODELS["fasth3-v2"], cold=True, seconds=15)
    assert long.usd > lite.usd
    # GPU + CPU + memory are all represented.
    assert set(lite.breakdown) == {"gpu", "cpu", "memory"}


def test_admission_stops_at_soft_budget(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        api_keys=["x"],
        backend="modal",
        monthly_credits_usd=30,
        monthly_soft_budget_usd=0.10,
    )
    settings.ensure()
    db = Database(settings.db_path)
    budget = BudgetManager(settings, db)
    allowed, reason, summary = budget.check_admission(MODELS["fasth3-omniref"])
    assert allowed is False
    assert "budget" in (reason or "").lower()
    assert summary["remaining_usd"] == 0.10


def test_active_reservations_are_subtracted(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path, api_keys=["x"], backend="modal",
        monthly_credits_usd=30, monthly_soft_budget_usd=1.0,
    )
    settings.ensure()
    db = Database(settings.db_path)
    budget = BudgetManager(settings, db)
    db.create_video(
        model="fasth3-v2", prompt="x", width=832, height=480, seconds=5, fps=24,
        status="queued", reserved_usd=0.6,
    )
    summary = budget.summary()
    assert summary["reserved_usd"] == 0.6
    assert summary["remaining_usd"] == 0.4


def test_expired_cleanup(tmp_path: Path):
    from gateway.backend import LocalBackend
    from gateway.dispatcher import Dispatcher
    from gateway.storage import Storage

    settings = Settings(data_dir=tmp_path, api_keys=["x"], backend="local")
    settings.ensure()
    db = Database(settings.db_path)
    storage = Storage(settings)
    output = storage.output_path("video_old")
    storage.atomic_write(output, b"not a real video")
    old = iso(utcnow() - timedelta(days=60))
    db.create_video(
        id="video_old", model="fasth3-v2", prompt="x", width=832, height=480,
        seconds=5, fps=24, status="completed", completed_at=old, output_path=str(output),
    )
    backend = LocalBackend(settings)
    dispatcher = Dispatcher(settings, db, storage, backend, BudgetManager(settings, db))
    removed = dispatcher.cleanup_expired()
    assert removed == 1
    assert not output.exists()
    assert db.get_video("video_old") is None
