from __future__ import annotations

from conftest import png_bytes, wait_for
from fastapi.testclient import TestClient


def test_cloudflare_style_headers_do_not_break_auth(client: TestClient, auth_headers):
    """Cloudflare Access adds headers; the gateway must ignore them and still
    authenticate with its own Bearer key."""
    headers = {
        **auth_headers,
        "CF-Access-Client-Id": "access-id.access-key",
        "CF-Access-Client-Secret": "secret",
        "Cf-Connecting-Ip": "203.0.113.10",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "video.example.com",
    }
    response = client.get("/v1/models", headers=headers)
    assert response.status_code == 200


def test_multipart_upload_with_access_headers(client: TestClient, auth_headers):
    headers = {
        **auth_headers,
        "CF-Access-Client-Id": "access-id.access-key",
        "CF-Access-Client-Secret": "secret",
    }
    response = client.post(
        "/v1/videos",
        headers=headers,
        data={"model": "fasth3-v2", "prompt": "A calm lake at sunrise.", "size": "832x480"},
        files={"input_reference": ("ref.png", png_bytes((160, 160)), "image/png")},
    )
    assert response.status_code == 201, response.text
    completed = wait_for(client, response.json()["id"], auth_headers)
    assert completed["status"] == "completed"


def test_range_playback_and_download_headers(client: TestClient, auth_headers):
    response = client.post(
        "/v1/videos",
        headers=auth_headers,
        json={"model": "fasth3-v2", "prompt": "A lighthouse in a storm.", "size": "832x480"},
    )
    assert response.status_code == 201
    completed = wait_for(client, response.json()["id"], auth_headers)
    assert completed["status"] == "completed"

    # Browsers use Range to seek; Cloudflare passes Range through.
    ranged = client.get(completed["content_url"], headers={"Range": "bytes=0-1023"})
    assert ranged.status_code == 206, ranged.text
    assert len(ranged.content) == 1024
    assert ranged.headers["content-range"].startswith("bytes 0-1023/")

    download = client.get(completed["download_url"])
    assert download.status_code == 200
    assert "attachment" in download.headers.get("content-disposition", "").lower()
    assert download.headers["content-type"].startswith("video/mp4")


def test_absolute_content_urls_for_public_host(settings, auth_headers):
    from gateway.api import Gateway
    from gateway.backend import LocalBackend

    settings.public_base_url = "https://video.example.com"
    settings.ensure()
    gateway = Gateway(settings, start_dispatcher=False, backend=LocalBackend(settings))
    video = gateway.db.create_video(
        model="fasth3-v2", prompt="x", width=832, height=480, seconds=5, fps=24,
        status="queued", reserved_usd=0.0,
    )
    public = gateway.video_public(video)
    assert public["content_url"] is None  # not completed yet
    gateway.db.update_video(video["id"], status="completed", output_path="/tmp/x.mp4")
    public = gateway.video_public(gateway.db.get_video(video["id"]))
    assert public["content_url"].startswith("https://video.example.com/v1/videos/")
    assert public["thumbnail_url"] is None
