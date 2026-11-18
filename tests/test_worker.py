from __future__ import annotations

from worker.modal_app import (
    MODEL_SPECS,
    _generator_cache_key,
    _should_use_adaln_table,
    build_generation_request,
)


def test_adaln_table_is_only_used_for_t2v(tmp_path):
    table = tmp_path / "adaln_tables.pt"
    table.write_bytes(b"x")
    spec = {"adaln_table": "adaln_tables.pt"}
    assert _should_use_adaln_table(spec, tmp_path, has_image=False, has_references=False)
    assert not _should_use_adaln_table(spec, tmp_path, has_image=True, has_references=True)
    assert not _should_use_adaln_table(spec, tmp_path, has_image=False, has_references=True)
    assert not _should_use_adaln_table({"adaln_table": None}, tmp_path,
                                       has_image=False, has_references=False)


def test_h3_model_directories_and_steps():
    assert set(MODEL_SPECS) == {"h3-fl2va", "fasth3-v2", "fasth3-omniref", "fasth3-trim"}
    for spec in MODEL_SPECS.values():
        assert "h3" in spec["dir"].lower()
        assert spec["frames"] in (124, 243)
    assert MODEL_SPECS["fasth3-v2"]["steps"] == 9
    assert MODEL_SPECS["fasth3-omniref"]["steps"] == 8
    assert MODEL_SPECS["fasth3-trim"]["steps"] == 9


def test_generation_request_contract():
    payload = {
        "prompt": "a fox in snow",
        "width": 832,
        "height": 480,
        "frames": 124,
        "fps": 24,
        "seed": 7,
        "steps": 9,
        "guidance": 1.0,
    }
    image = object()
    request = build_generation_request(
        payload, image=image, last_image=None, references=None, output_path="/results/x",
        spec=MODEL_SPECS["fasth3-v2"],
    )
    assert request["inputs"]["pil_image"] is image
    assert request["sampling"] == {
        "seed": 7,
        "height": 480,
        "width": 832,
        "num_frames": 124,
        "fps": 24,
        "num_inference_steps": 9,
        "guidance_scale": 1.0,
        "batch_cfg": False,
    }
    assert request["output"]["save_video"] is True
    assert request["output"]["output_path"] == "/results/x"

    refs = [object(), object()]
    referenced_payload = dict(payload, steps=8)
    referenced = build_generation_request(
        referenced_payload, image=None, last_image=None, references=refs, output_path="/results/x",
        spec=MODEL_SPECS["fasth3-omniref"],
    )
    assert referenced["inputs"]["references"] is refs
    assert referenced["sampling"]["num_inference_steps"] == 8


def test_generator_cache_separates_t2v_and_i2v():
    v2 = MODEL_SPECS["fasth3-v2"]
    assert _generator_cache_key("fasth3-v2", v2, has_image=False, has_references=False) == "fasth3-v2:t2v"
    assert _generator_cache_key("fasth3-v2", v2, has_image=True, has_references=True) == "fasth3-v2:i2v"
    assert _generator_cache_key("fasth3-omniref", MODEL_SPECS["fasth3-omniref"],
                                has_image=True, has_references=True) == "fasth3-omniref:ref2va"


def test_generation_request_supports_first_and_last_frame():
    payload = {
        "prompt": "a fox runs from the first frame to the last frame",
        "width": 1344,
        "height": 768,
        "frames": 243,
        "fps": 24,
        "seed": 9,
        "steps": 9,
        "guidance": 1.0,
    }
    first, last = object(), object()
    request = build_generation_request(
        payload, image=first, last_image=last, references=None,
        output_path="/results/x", spec=MODEL_SPECS["fasth3-v2"],
    )
    assert request["inputs"]["pil_image"] is first
    assert request["inputs"]["last_image"] is last
    assert request["sampling"]["num_frames"] == 243
