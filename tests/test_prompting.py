from __future__ import annotations

import asyncio
import json

import httpx

from gateway.prompting import PromptEnhancer, image_data_uri


def test_prompt_enhancer_uses_h3_context_format():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["session"] = request.headers.get("x-opencode-session")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "integrated_multimodal_description: [Shot 1] A test.\noverall_soundscape: Rain.\nnon_diegetic_music: N/A"}}]},
        )

    enhancer = PromptEnhancer(
        base_url="http://127.0.0.1:8317/v1",
        api_key="test-key",
        model="test-model",
        opencode_session="ses_test",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        enhancer.enhance(
            "a woman walks through rain",
            has_first_frame=True,
            has_last_frame=False,
            references=[],
            seconds=10,
            width=1344,
            height=768,
        )
    )
    assert result.applied is True
    assert result.prompt.startswith("integrated_multimodal_description")
    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["auth"] == "Bearer test-key"
    assert seen["session"] == "ses_test"
    assert seen["body"]["model"] == "test-model"
    assert "10.00 seconds" in seen["body"]["messages"][1]["content"]


def test_prompt_enhancer_falls_back_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    enhancer = PromptEnhancer(
        base_url="http://127.0.0.1:8317/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        enhancer.enhance(
            "keep my prompt",
            has_first_frame=False,
            has_last_frame=False,
            references=[],
            seconds=5,
            width=832,
            height=480,
        )
    )
    assert result.applied is False
    assert result.prompt == "keep my prompt"
    assert result.error


def test_official_guide_and_mode_selection():
    from gateway.prompting import _guide

    assert "T2VA / I2VA / FL2VA / L2VA" in _guide("i2va")
    assert "subject_definitions" in _guide("ref2va")
    assert PromptEnhancer.mode_for(has_first_frame=True, has_last_frame=False, references=[]) == "i2va"
    assert PromptEnhancer.mode_for(has_first_frame=True, has_last_frame=True, references=[]) == "fl2va"
    assert PromptEnhancer.mode_for(has_first_frame=False, has_last_frame=True, references=[]) == "l2va"
    assert PromptEnhancer.mode_for(has_first_frame=False, has_last_frame=False, references=[]) == "t2va"
    assert PromptEnhancer.mode_for(
        has_first_frame=False, has_last_frame=False,
        references=[{"type": "image"}],
    ) == "ref2va"


def test_image_data_uri_compresses_to_jpeg():
    from pathlib import Path

    uri = image_data_uri(Path("tests/fixtures/anime_reference.png").read_bytes())
    assert uri and uri.startswith("data:image/jpeg;base64,")
