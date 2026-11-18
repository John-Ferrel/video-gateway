from __future__ import annotations

from conftest import wait_for
from fastapi.testclient import TestClient


def test_keys_have_isolated_video_libraries(client: TestClient, auth_headers, second_headers):
    response = client.post(
        "/v1/videos",
        headers=auth_headers,
        json={"prompt": "A lighthouse at sunset.", "size": "832x480"},
    )
    assert response.status_code == 201, response.text
    video = response.json()
    completed = wait_for(client, video["id"], auth_headers)
    assert completed["status"] == "completed"

    first = client.get("/v1/videos", headers=auth_headers)
    assert first.status_code == 200
    assert video["id"] in {item["id"] for item in first.json()["data"]}

    second = client.get("/v1/videos", headers=second_headers)
    assert second.status_code == 200
    assert second.json()["data"] == []

    hidden = client.get(f"/v1/videos/{video['id']}", headers=second_headers)
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "not_found"

    assert client.post(
        f"/v1/videos/{video['id']}/cancel", headers=second_headers
    ).status_code == 404
    assert client.delete(f"/v1/videos/{video['id']}", headers=second_headers).status_code == 404
    assert client.post(
        f"/v1/videos/{video['id']}/retry", headers=second_headers
    ).status_code == 404

    direct_media = client.get(f"/v1/videos/{video['id']}/content", headers=second_headers)
    assert direct_media.status_code == 401
    token_media = client.get(completed["content_url"])
    assert token_media.status_code == 200


def test_keys_share_usage_but_first_key_is_admin(client: TestClient, auth_headers, second_headers):
    response = client.post(
        "/v1/videos",
        headers=auth_headers,
        json={"prompt": "A quiet library at night.", "size": "832x480"},
    )
    assert response.status_code == 201, response.text
    wait_for(client, response.json()["id"], auth_headers)

    first_usage = client.get("/usage", headers=auth_headers)
    second_usage = client.get("/usage", headers=second_headers)
    assert first_usage.status_code == 200
    assert second_usage.status_code == 200
    assert first_usage.json()["is_admin"] is True
    assert second_usage.json()["is_admin"] is False
    assert second_usage.json()["jobs_this_cycle"] >= 1
    assert second_usage.json()["spent_usd"] == first_usage.json()["spent_usd"]

    assert client.get("/v1/admin/diagnostics", headers=auth_headers).status_code == 200
    denied = client.get("/v1/admin/diagnostics", headers=second_headers)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"


def test_batches_are_isolated(client: TestClient, auth_headers, second_headers):
    response = client.post(
        "/v1/videos/batch",
        headers=auth_headers,
        json={"items": [{"prompt": "A paper boat in the rain.", "size": "832x480"}]},
    )
    assert response.status_code == 201, response.text
    batch = response.json()
    for item in batch["data"]:
        wait_for(client, item["id"], auth_headers)

    assert client.get("/v1/batches", headers=second_headers).json()["data"] == []
    assert client.get(f"/v1/batches/{batch['batch_id']}", headers=second_headers).status_code == 404
    assert client.get(f"/v1/batches/{batch['batch_id']}", headers=auth_headers).status_code == 200
