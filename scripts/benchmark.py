#!/usr/bin/env python3
"""End-to-end benchmark against a running gateway.

This is intentionally conservative: it refuses to start unless the remaining
budget covers a configurable floor, and it stops between jobs if the budget
falls below the floor. It does not retry failed jobs automatically.

Usage:
  .venv/bin/python scripts/benchmark.py --base http://127.0.0.1:8787 \
      --api-key "$(cat data/api_key)" --output reports/benchmark-h3.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

PROMPTS = [
    ("h3-v2-480p-cold", "fasth3-v2", "A red panda walks through a misty bamboo forest at sunrise, cinematic dolly-in, soft wind and birdsong.", "832x480", None),
    ("h3-v2-480p-warm", "fasth3-v2", "An anime heroine in a red coat stands on a neon-lit Tokyo rooftop, rain and synth ambience, hair whipping in the wind.", "832x480", None),
    ("h3-v2-i2v-480p", "fasth3-v2", "The character turns toward the camera, smiles, and raises one hand; rain and distant city noise.", "480x832", "reference"),
    ("h3-omniref-480p", "fasth3-omniref", "Both characters walk together through a neon market, rain and city ambience.", "832x480", "references"),
    ("h3-v2-768p", "fasth3-v2", "A glass marble rolls down a wooden staircase in slow motion, sunlight refracting through it, precise clacks and room tone.", "1344x768", None),
    ("h3-trim-480p", "fasth3-trim", "A paper boat drifts down a rain-filled street at dusk, reflections and soft rain.", "832x480", None),
]



def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--api-key", default=os.getenv("VG_API_KEY", ""))
    parser.add_argument("--output", default="reports/benchmark-h3.json")
    parser.add_argument("--outdir", default="benchmark-output")
    parser.add_argument("--budget-floor", type=float, default=3.0)
    parser.add_argument("--models", default="", help="comma-separated subset of model ids")
    parser.add_argument(
        "--reference-path",
        default="tests/fixtures/anime_reference.png",
        help="original synthetic portrait used for the image-to-video run",
    )
    parser.add_argument(
        "--reference-url",
        default="https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/diffusers/astronaut.jpg",
        help="fallback reference if --reference-path is missing",
    )
    args = parser.parse_args()
    if not args.api_key:
        print("error: --api-key or VG_API_KEY is required", file=sys.stderr)
        return 2

    base = args.base.rstrip("/")
    headers = {"Authorization": "Bearer " + args.api_key}
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    models = {m.strip() for m in args.models.split(",") if m.strip()}

    def sync_billing(client):
        try:
            return client.post(f"{base}/v1/admin/billing/sync", headers=headers).json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    with httpx.Client(timeout=60) as client:
        sync_billing(client)
        usage = client.get(f"{base}/usage", headers=headers).raise_for_status().json()
        billing_before = {
            "spent_usd": usage.get("spent_usd"),
            "billed_usd": usage.get("billed_usd"),
            "billing_source": usage.get("billing_source"),
        }
        if usage["remaining_usd"] < args.budget_floor:
            print(f"refusing: remaining ${usage['remaining_usd']:.2f} < floor ${args.budget_floor:.2f}", file=sys.stderr)
            return 3
        if usage.get("backend", {}).get("demo"):
            print("warning: backend is the LOCAL DEMO; these will not be real GPU numbers", file=sys.stderr)

        reference_bytes = None
        if any(p[4] == "reference" for p in PROMPTS):
            reference_path = Path(args.reference_path)
            if reference_path.exists():
                print(f"using reference {reference_path}")
                reference_bytes = reference_path.read_bytes()
            else:
                print("downloading fallback reference image…")
                reference_bytes = client.get(args.reference_url, follow_redirects=True).raise_for_status().content

        runs = []
        for name, model, prompt, size, reference in PROMPTS:
            if models and model not in models:
                continue
            usage = client.get(f"{base}/usage", headers=headers).raise_for_status().json()
            if usage["remaining_usd"] < args.budget_floor:
                print(f"stopping: remaining ${usage['remaining_usd']:.2f} below floor")
                break
            before = usage["spent_usd"]
            started = now_iso()
            t0 = time.perf_counter()
            if reference:
                files = []
                data = {"model": model, "prompt": prompt, "size": size, "seed": "2026"}
                if reference == "references":
                    files.append(("reference_images", (Path(args.reference_path).name or "reference.png", reference_bytes or b"", "image/png")))
                    second = Path(args.reference_path).with_name("anime_reference_b.png")
                    second_bytes = second.read_bytes() if second.exists() else reference_bytes or b""
                    files.append(("reference_images", (second.name, second_bytes, "image/png")))
                else:
                    files.append(("input_reference", (Path(args.reference_path).name or "reference.png", reference_bytes or b"", "image/png")))
                response = client.post(f"{base}/v1/videos", headers=headers, data=data, files=files)
            else:
                response = client.post(f"{base}/v1/videos", headers=headers, json={
                    "model": model, "prompt": prompt, "size": size, "seed": 2026,
                })
            response.raise_for_status()
            job = response.json()
            print(f"[{name}] queued {job['id']} ({job['reserved_usd']} reserved)")
            submitted = now_iso()
            states = []
            while True:
                time.sleep(1.0)
                job = client.get(f"{base}/v1/videos/{job['id']}", headers=headers).raise_for_status().json()
                states.append({"t": now_iso(), "status": job["internal_status"], "progress": job["progress"]})
                if job["status"] in {"completed", "failed", "cancelled"}:
                    break
                if time.perf_counter() - t0 > 1800:
                    raise TimeoutError(name)
            elapsed = round(time.perf_counter() - t0, 2)
            after_usage = client.get(f"{base}/usage", headers=headers).raise_for_status().json()
            after = after_usage["spent_usd"]
            video_path = None
            if job["status"] == "completed":
                video_path = outdir / f"{name}-{job['id']}.mp4"
                with client.stream("GET", base + job["content_url"]) as download:
                    download.raise_for_status()
                    with open(video_path, "wb") as handle:
                        handle.writelines(download.iter_bytes())
            runs.append({
                "name": name,
                "model": model,
                "prompt": prompt,
                "size": size,
                "reference": bool(reference),
                "job_id": job["id"],
                "status": job["status"],
                "submitted_at": submitted,
                "started_at": started,
                "elapsed_s": elapsed,
                "progress_events": states,
                "usage": job.get("usage"),
                "cost_usd": job.get("cost_usd"),
                "usage_delta_usd": round(max(0.0, after - before), 6),
                "remaining_after_usd": after_usage["remaining_usd"],
                "error": job.get("error"),
                "file": str(video_path) if video_path else None,
                "file_bytes": video_path.stat().st_size if video_path and video_path.exists() else None,
            })
            print(f"[{name}] {job['status']} in {elapsed}s, est ${job.get('cost_usd')}, delta ${after - before:.4f}")

    sync_billing(client)
    final_usage = client.get(f"{base}/usage", headers=headers).raise_for_status().json()
    report = {
        "generated_at": now_iso(),
        "base": base,
        "backend": usage.get("backend"),
        "billing_before": billing_before,
        "billing_after": {
            "spent_usd": final_usage.get("spent_usd"),
            "billed_usd": final_usage.get("billed_usd"),
            "billing_source": final_usage.get("billing_source"),
            "billing_error": final_usage.get("billing_error"),
        },
        "runs": runs,
        "totals": {
            "runs": len(runs),
            "completed": sum(r["status"] == "completed" for r in runs),
            "cost_usd": round(sum(r["cost_usd"] or 0 for r in runs), 4),
            "usage_delta_usd": round(sum(r["usage_delta_usd"] or 0 for r in runs), 4),
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
