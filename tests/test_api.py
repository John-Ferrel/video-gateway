from __future__ import annotations

import time

from conftest import png_bytes, wait_for
from fastapi.testclient import TestClient


def test_health_is_public(client: TestClient):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["backend"]["demo"] is True


def test_api_requires_key(client: TestClient):
    response = client.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_models(client: TestClient, auth_headers):
    response = client.get("/v1/models", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert {item["id"] for item in data} == {"fasth3-v2", "fasth3-omniref"}
    assert all(item["default_duration"] == 10 for item in data)
    assert all(item["durations"] == [5, 10, 15] for item in data)
    assert all(item["default_size"] == "1344x768" for item in data)
    assert all(item["native_audio"] for item in data)
    assert all(item["image_to_video"] for item in data)


def test_create_complete_and_stream(client: TestClient, auth_headers):
    response = client.post(
        "/v1/videos",
        headers=auth_headers,
        json={
            "model": "fasth3-v2",
            "prompt": "A red panda walking through a bamboo forest, cinematic light.",
            "size": "832x480",
            "seed": 42,
        },
    )
    assert response.status_code == 201, response.text
    video = response.json()
    assert video["status"] == "queued"
    assert video["object"] == "video"
    assert video["reserved_usd"] == 0  # local demo backend never reserves real budget

    completed = wait_for(client, video["id"], auth_headers)
    assert completed["status"] == "completed", completed
    assert completed["content_url"]
    assert completed["width"] == 832 and completed["height"] == 480
    assert completed["has_audio"] is True

    content = client.get(completed["content_url"])
    assert content.status_code == 200, content.text
    assert content.headers["content-type"].startswith("video/mp4")
    assert len(content.content) > 1000

    thumb = client.get(completed["thumbnail_url"])
    assert thumb.status_code == 200
    assert thumb.headers["content-type"].startswith("image/jpeg")

    # A thumbnail token must not unlock the video stream.
    thumb_url = completed["thumbnail_url"]
    assert "token=" in thumb_url
    thumb_token = thumb_url.split("token=", 1)[1]
    wrong = client.get(f"/v1/videos/{video['id']}/content?token={thumb_token}")
    assert wrong.status_code == 401

    usage = client.get("/usage", headers=auth_headers).json()
    assert usage["soft_budget_usd"] == 28.0
    assert usage["jobs_this_cycle"] >= 1
    assert usage["storage"]["gb"] >= 0


def test_multipart_image_reference(client: TestClient, auth_headers):
    response = client.post(
        "/v1/videos",
        headers=auth_headers,
        data={
            "model": "fasth3-v2",
            "prompt": "The character turns toward the camera and smiles.",
            "size": "480x832",
        },
        files={"input_reference": ("ref.png", png_bytes(), "image/png")},
    )
    assert response.status_code == 201, response.text
    video = response.json()
    completed = wait_for(client, video["id"], auth_headers)
    assert completed["status"] == "completed", completed
    # The upload should be re-encoded to a JPEG and kept for retries.
    assert completed["internal_status"] == "completed"


def test_batch(client: TestClient, auth_headers):
    response = client.post(
        "/v1/videos/batch",
        headers=auth_headers,
        json={
            "model": "fasth3-v2",
            "label": "test batch",
            "items": [
                {"prompt": "A lighthouse in a storm, dramatic waves."},
                {"prompt": "A cat sleeping on a sunlit windowsill."},
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["data"]) == 2
    for item in body["data"]:
        assert item["batch_id"] == body["batch_id"]
        assert wait_for(client, item["id"], auth_headers)["status"] == "completed"
    batch = client.get(f"/v1/batches/{body['batch_id']}", headers=auth_headers).json()
    assert batch["total"] == 2
    assert batch["status_counts"]["completed"] == 2


def test_batch_with_reference_data_uri(client: TestClient, auth_headers):
    import base64

    data_uri = "data:image/png;base64," + base64.b64encode(png_bytes((128, 128))).decode()
    response = client.post(
        "/v1/videos/batch",
        headers=auth_headers,
        json={
            "model": "fasth3-v2",
            "items": [{
                "prompt": "The character waves at the camera.",
                "size": "480x832",
                "input_reference": data_uri,
            }],
        },
    )
    assert response.status_code == 201, response.text
    video = response.json()["data"][0]
    completed = wait_for(client, video["id"], auth_headers)
    assert completed["status"] == "completed", completed


def test_validation_errors(client: TestClient, auth_headers):
    bad_model = client.post("/v1/videos", headers=auth_headers,
                            json={"model": "nope", "prompt": "hello"})
    assert bad_model.status_code == 400
    assert bad_model.json()["error"]["code"] == "unknown_model"

    bad_duration = client.post("/v1/videos", headers=auth_headers,
                               json={"prompt": "hello", "seconds": 7})
    assert bad_duration.status_code == 400
    assert bad_duration.json()["error"]["code"] == "invalid_duration"

    bad_image = client.post("/v1/videos", headers=auth_headers,
                            json={"prompt": "hello", "input_reference": "data:image/png;base64,bm90YW5pbWFnZQ=="})
    assert bad_image.status_code == 400
    assert bad_image.json()["error"]["code"] == "invalid_image"


def test_delete_and_cancel(client: TestClient, auth_headers):
    response = client.post("/v1/videos", headers=auth_headers,
                           json={"prompt": "A paper boat floating down a stream.", "seconds": 5})
    video_id = response.json()["id"]
    canceled = client.post(f"/v1/videos/{video_id}/cancel", headers=auth_headers)
    assert canceled.status_code == 200
    for _ in range(150):
        current = client.get(f"/v1/videos/{video_id}", headers=auth_headers).json()
        if current["status"] in {"cancelled", "completed", "failed"}:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(current)
    deleted = client.delete(f"/v1/videos/{video_id}", headers=auth_headers)
    assert deleted.status_code == 200
    assert client.get(f"/v1/videos/{video_id}", headers=auth_headers).status_code == 404


def test_omniref_requires_a_reference(client: TestClient, auth_headers):
    response = client.post(
        "/v1/videos", headers=auth_headers,
        json={"model": "fasth3-omniref", "prompt": "The character waves.", "size": "832x480"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_reference"


def test_omniref_multiple_image_references(client: TestClient, auth_headers):
    response = client.post(
        "/v1/videos",
        headers=auth_headers,
        data={"model": "fasth3-omniref", "prompt": "Both characters walk together.", "size": "832x480"},
        files=[
            ("reference_images", ("a.png", png_bytes((160, 160)), "image/png")),
            ("reference_images", ("b.png", png_bytes((160, 160)), "image/png")),
        ],
    )
    assert response.status_code == 201, response.text
    video = response.json()
    assert video["reference_count"] == 2
    assert video["reference_types"] == ["image", "image"]
    completed = wait_for(client, video["id"], auth_headers)
    assert completed["status"] == "completed", completed


def test_v2_rejects_multiple_references(client: TestClient, auth_headers):
    response = client.post(
        "/v1/videos",
        headers=auth_headers,
        data={"model": "fasth3-v2", "prompt": "A fox.", "size": "832x480"},
        files=[
            ("reference_images", ("a.png", png_bytes((160, 160)), "image/png")),
            ("reference_images", ("b.png", png_bytes((160, 160)), "image/png")),
        ],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_reference"


def test_i2v_first_and_last_frames_10s(client: TestClient, auth_headers):
    response = client.post(
        "/v1/videos",
        headers=auth_headers,
        data={
            "model": "fasth3-v2",
            "prompt": "The character walks from the first frame to the last frame.",
            "size": "1344x768",
            "seconds": "10",
            "enhance_prompt": "false",
        },
        files=[
            ("input_reference", ("first.png", png_bytes((160, 160)), "image/png")),
            ("last_reference", ("last.png", png_bytes((160, 160)), "image/png")),
        ],
    )
    assert response.status_code == 201, response.text
    video = response.json()
    assert video["seconds"] == 10.0
    assert video["reference_count"] == 2
    assert video["reference_types"] == ["image", "image"]
    completed = wait_for(client, video["id"], auth_headers)
    assert completed["status"] == "completed", completed


def test_low_quality_preview_is_hidden_from_model_list(client: TestClient, auth_headers):
    listed = client.get("/v1/models", headers=auth_headers).json()["data"]
    assert "fasth3-trim" not in {item["id"] for item in listed}
    direct = client.get("/v1/models/fasth3-trim", headers=auth_headers)
    assert direct.status_code == 200
    assert direct.json()["quality"] == "fast"


def test_prompt_enhancement_is_used_and_exposed(client: TestClient, auth_headers):
    from gateway.prompting import EnhancementResult

    gateway = client.app.state.gateway

    async def fake_enhance(prompt, **kwargs):
        return EnhancementResult(
            "integrated_multimodal_description: [Shot 1] " + prompt,
            True,
            "test-model",
        )

    gateway.enhancer.enhance = fake_enhance
    response = client.post(
        "/v1/videos",
        headers=auth_headers,
        json={
            "prompt": "A lantern in the rain.",
            "size": "1344x768",
            "seconds": 5,
            "enhance_prompt": True,
        },
    )
    assert response.status_code == 201, response.text
    video = response.json()
    assert video["prompt"].startswith("integrated_multimodal_description")
    assert video["prompt_original"] == "A lantern in the rain."
    assert video["prompt_enhancement"]["applied"] is True
    assert video["prompt_enhancement"]["model"] == "test-model"


def test_first_frame_selects_adaptive_portrait_canvas(client: TestClient, auth_headers):
    response = client.post(
        "/v1/videos",
        headers=auth_headers,
        data={"model": "fasth3-v2", "prompt": "The portrait moves.", "seconds": "5"},
        files=[("input_reference", ("portrait.png", png_bytes((160, 213)), "image/png"))],
    )
    assert response.status_code == 201, response.text
    video = response.json()
    assert (video["width"], video["height"]) == (768, 1024)
    assert video["size"] == "768x1024"


def test_prompt_preview_endpoint(client: TestClient, auth_headers):
    from gateway.prompting import EnhancementResult

    gateway = client.app.state.gateway

    async def fake_enhance(prompt, **kwargs):
        return EnhancementResult("structured: " + prompt, True, "test-model", mode="t2va", images_sent=0)

    gateway.enhancer.enhance = fake_enhance
    response = client.post(
        "/v1/prompts/enhance",
        headers=auth_headers,
        json={"prompt": "A lantern in rain.", "seconds": 5},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "t2va"
    assert body["applied"] is True
    assert body["enhanced"] == "structured: A lantern in rain."
    assert body["size"] == "1344x768"


def test_regenerate_hd_is_gated_by_gpu_memory(client: TestClient, auth_headers):
    created = client.post(
        "/v1/videos", headers=auth_headers,
        json={"prompt": "A lantern in rain.", "seconds": 5},
    )
    video_id = created.json()["id"]
    completed = wait_for(client, video_id, auth_headers)
    assert completed["status"] == "completed"
    response = client.post(f"/v1/videos/{video_id}/regenerate", headers=auth_headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "regenerate_hd_unavailable"
