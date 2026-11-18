from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gateway.api import create_app
from gateway.backend import LocalBackend
from gateway.config import Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    value = Settings(
        data_dir=tmp_path / "data",
        api_keys=["test-key", "second-key"],
        backend="local",
        max_queue=10,
        max_jobs_per_day=20,
        job_timeout_seconds=60,
        retention_days=30,
        max_storage_gb=1,
    )
    value.ensure()
    return value


@pytest.fixture()
def client(settings: Settings):
    backend = LocalBackend(settings)
    app = create_app(settings, start_dispatcher=True, backend=backend)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers():
    return {"Authorization": "Bearer test-key"}


@pytest.fixture()
def second_headers():
    return {"Authorization": "Bearer second-key"}


def wait_for(client: TestClient, video_id: str, headers: dict, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        response = client.get(f"/v1/videos/{video_id}", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in {"completed", "failed", "cancelled"}:
            return last
        time.sleep(0.25)
    raise AssertionError(f"job did not finish: {last}")


def png_bytes(size=(256, 256)) -> bytes:
    image = Image.new("RGB", size, (30, 140, 220))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
