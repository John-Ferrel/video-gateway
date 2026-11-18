"""Cost estimation, budget admission and usage reporting.

Modal bills container wall time: GPU seconds plus the requested CPU and memory
for the whole time the container exists, including model load, generation,
video encoding, and the `scaledown_window` tail. We therefore estimate from
allocated resources and measured wall time rather than GPU-seconds alone.

The authoritative number is Modal's invoice. On plans where the billing API is
available the gateway stores the synced cycle total and uses it for admission;
otherwise it falls back to conservative per-job metering and labels the source
as `estimated` in /usage.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import ModelSpec, Settings
from .db import Database, iso, utcnow


@dataclass(frozen=True)
class CostEstimate:
    seconds: float
    usd: float
    breakdown: dict[str, float]
    cold: bool

    def public(self) -> dict:
        return {
            "seconds": round(self.seconds, 1),
            "usd": round(self.usd, 4),
            "cold": self.cold,
            "breakdown": {key: round(value, 4) for key, value in self.breakdown.items()},
        }


def cycle_start(now: datetime | None = None, day: int = 1) -> datetime:
    now = now or utcnow()
    day = max(1, min(28, int(day)))
    if now.day >= day:
        year, month = now.year, now.month
    else:
        year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return datetime(year, month, day, tzinfo=UTC)


def cycle_key(now: datetime | None = None, day: int = 1) -> str:
    return cycle_start(now, day).strftime("%Y-%m-%d")


class BudgetManager:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    # ------------------------------------------------------------------ rates
    def billed_seconds(self, *, generation_seconds: float, include_load: float = 0.0,
                       cold: bool = True) -> float:
        # 8s covers container boot/image setup not measured by the function;
        # the scaledown tail is paid even though the function has returned.
        overhead = 8.0 + (self.settings.worker_scaledown_seconds if cold or generation_seconds else 0)
        return max(1.0, float(generation_seconds) + float(include_load) + overhead)

    def cost_for_seconds(self, seconds: float) -> CostEstimate:
        gpu = seconds * self.settings.gpu_usd_per_second
        cpu = seconds * self.settings.worker_cpu * self.settings.cpu_usd_per_core_second
        memory = seconds * self.settings.worker_memory_gib * self.settings.memory_usd_per_gib_second
        usd = gpu + cpu + memory
        return CostEstimate(
            seconds=seconds,
            usd=usd,
            breakdown={"gpu": gpu, "cpu": cpu, "memory": memory},
            cold=True,
        )

    def estimate(self, spec: ModelSpec, *, cold: bool = True, seconds: float | None = None) -> CostEstimate:
        duration = float(seconds if seconds is not None else spec.seconds)
        scale = max(1.0, duration / max(1, spec.seconds))
        load = spec.expected_load_seconds if cold else 0
        billed = self.billed_seconds(
            generation_seconds=spec.expected_warm_seconds * scale + 5,
            include_load=load,
            cold=cold,
        )
        estimate = self.cost_for_seconds(billed)
        # Conservative admission: reserve 10% for load variance and encoding.
        usd = estimate.usd * 1.10
        return CostEstimate(
            seconds=billed,
            usd=usd,
            breakdown={key: value * 1.10 for key, value in estimate.breakdown.items()},
            cold=cold,
        )

    def actual(self, telemetry: dict[str, Any], *, local: bool = False) -> CostEstimate:
        if local:
            return CostEstimate(seconds=0.0, usd=0.0, breakdown={}, cold=False)
        total = telemetry.get("total_s") or telemetry.get("generation_seconds") or 0.0
        load = telemetry.get("model_load_s") or 0.0
        # `total_s` is measured inside the function and already contains the
        # model load. Only add it when a caller supplies generation-only time.
        if telemetry.get("total_s"):
            seconds = self.billed_seconds(generation_seconds=float(total), cold=True)
        else:
            seconds = self.billed_seconds(generation_seconds=float(total), include_load=float(load), cold=True)
        return self.cost_for_seconds(seconds)

    # ------------------------------------------------------------------ state
    def _active_reserved(self) -> float:
        return sum(float(job.get("reserved_usd") or 0) for job in self.db.active_jobs())

    def sync_modal_billing(self, backend: Any) -> dict:
        """Fetch Modal's official cycle total when the plan allows it."""
        started = cycle_start(day=self.settings.billing_cycle_day)
        key = cycle_key(day=self.settings.billing_cycle_day)
        try:
            data = backend.billing_summary(started)
            if data is None:
                raise RuntimeError("billing API unavailable")
            amount = float(data.get("total_usd", 0.0))
            self.db.set_setting("billing_actual_usd", f"{amount:.6f}")
            self.db.set_setting("billing_actual_at", iso())
            self.db.set_setting("billing_cycle_key", key)
            self.db.set_setting("billing_source", "modal")
            self.db.set_setting("billing_error", "")
            return {"ok": True, "total_usd": amount, "source": "modal"}
        except Exception as exc:  # noqa: BLE001 - billing must not break jobs
            self.db.set_setting("billing_source", "estimated")
            self.db.set_setting("billing_error", str(exc)[:500])
            self.db.set_setting("billing_actual_at", iso())
            return {"ok": False, "error": str(exc), "source": "estimated"}

    def summary(self) -> dict:
        now = utcnow()
        start = cycle_start(now, self.settings.billing_cycle_day)
        start_iso = iso(start)
        ledger_total = self.db.ledger_total(start_iso)
        reserved = self._active_reserved()
        key = cycle_key(now, self.settings.billing_cycle_day)
        billed: float | None = None
        if self.db.get_setting("billing_cycle_key") == key:
            raw = self.db.get_setting("billing_actual_usd")
            if raw is not None:
                try:
                    billed = float(raw)
                except ValueError:
                    billed = None
        spent = max(ledger_total, billed or 0.0)
        soft_budget = self.settings.monthly_soft_budget_usd
        remaining = max(0.0, soft_budget - spent - reserved)
        credits_remaining = max(0.0, self.settings.monthly_credits_usd - spent - reserved)
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        elapsed = max(1.0, (now - start).total_seconds() / 86400)
        projected = spent / elapsed * days_in_month
        source = self.db.get_setting("billing_source", "estimated") or "estimated"
        jobs = self.db.count_videos(since=start_iso)
        completed = self.db.count_videos(status="completed", since=start_iso)
        today_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        today_jobs = self.db.count_videos(since=iso(today_start))
        return {
            "object": "usage",
            "billing_cycle": {
                "start": start.isoformat(timespec="seconds"),
                "end": (start + timedelta(days=days_in_month)).isoformat(timespec="seconds"),
                "day": self.settings.billing_cycle_day,
            },
            "credits_usd": self.settings.monthly_credits_usd,
            "soft_budget_usd": soft_budget,
            "reserve_usd": round(self.settings.monthly_credits_usd - soft_budget, 4),
            "spent_usd": round(spent, 4),
            "estimated_spend_usd": round(ledger_total, 4),
            "billed_usd": round(billed, 4) if billed is not None else None,
            "billing_source": source,
            "billing_error": self.db.get_setting("billing_error", "") or None,
            "billing_synced_at": self.db.get_setting("billing_actual_at"),
            "reserved_usd": round(reserved, 4),
            "remaining_usd": round(remaining, 4),
            "credits_remaining_usd": round(credits_remaining, 4),
            "percent_used": round(min(100.0, (spent + reserved) / soft_budget * 100) if soft_budget else 0, 1),
            "projected_month_usd": round(projected, 4),
            "jobs_this_cycle": jobs,
            "completed_this_cycle": completed,
            "jobs_today": today_jobs,
            "max_jobs_per_day": self.settings.max_jobs_per_day,
            "avg_cost_completed_usd": round(
                (sum(float(j.get("cost_usd") or 0) for j in self.db.list_videos(
                    status="completed", since=start_iso, limit=200)) / completed)
                if completed else 0.0,
                4,
            ),
            "active_jobs": len(self.db.active_jobs()),
            "queued_jobs": self.db.queued_count(),
            "ledger": self.db.ledger_breakdown(start_iso),
            "daily_spend": self.db.daily_spend(start_iso),
        }

    # ------------------------------------------------------------------ admission
    def check_admission(self, spec: ModelSpec, *, seconds: float | None = None,
                        local: bool = False) -> tuple[bool, str | None, dict]:
        summary = self.summary()
        if local:
            return True, None, summary
        estimate = self.estimate(spec, cold=True, seconds=seconds)
        if estimate.usd > self.settings.max_job_estimate_usd:
            return False, (
                f"Job estimate ${estimate.usd:.2f} exceeds MAX_JOB_ESTIMATE_USD="
                f"${self.settings.max_job_estimate_usd:.2f}"
            ), summary
        if summary["jobs_today"] >= self.settings.max_jobs_per_day:
            return False, f"Daily job limit reached ({self.settings.max_jobs_per_day})", summary
        if summary["remaining_usd"] + 1e-9 < estimate.usd:
            return False, (
                f"Monthly budget guard: ${summary['remaining_usd']:.2f} remaining after reserving "
                f"${summary['reserved_usd']:.2f} for in-flight jobs; this job needs up to "
                f"${estimate.usd:.2f}"
            ), summary
        if self.db.queued_count() >= self.settings.max_queue:
            return False, f"Queue is full ({self.settings.max_queue} jobs)", summary
        return True, None, summary

    def reserve_for(self, spec: ModelSpec, *, seconds: float | None = None,
                    local: bool = False) -> float:
        return 0.0 if local else self.estimate(spec, cold=True, seconds=seconds).usd

    def can_reconcile(self) -> bool:
        return True
