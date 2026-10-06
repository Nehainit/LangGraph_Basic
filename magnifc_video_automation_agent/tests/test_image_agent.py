import base64
import json
import os
import threading
from pathlib import Path

import pytest

from video_automation.agents import image_agent
from PIL import Image


PNG_1X1 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")


def shot(number, visual):
    return {
        "shot_id": f"shot-{number:03}",
        "scene_id": "scene-001",
        "scene_number": number,
        "characters_present": ["raja_001"],
        "duration_seconds": 5,
        "start_time": f"00:{(number - 1) * 5:02}",
        "end_time": f"00:{number * 5:02}",
        "visuals": visual,
        "camera": "wide 35mm eye level tracking composition",
        "motion": "pan_right",
        "subject_motion": "Raja walks naturally.",
        "continuity_in": "Raja wears a red coat.",
        "continuity_out": "Raja wears a red coat.",
        "narration": "Raja walks.",
    }


def with_fake_images(callback):
    original_request = image_agent._json_request
    original_headers = image_agent._magnific_headers
    original_sleep = image_agent.time.sleep
    calls = []

    def fake_request(url, payload, _headers):
        calls.append((url, payload))
        return {"data": [{"base64": PNG_1X1}]}

    image_agent._json_request = fake_request
    image_agent._magnific_headers = lambda: {}
    image_agent.time.sleep = lambda _seconds: None
    try:
        callback(calls)
    finally:
        image_agent._json_request = original_request
        image_agent._magnific_headers = original_headers
        image_agent.time.sleep = original_sleep


def test_image_prompt_builder_preserves_approved_shot_and_references(monkeypatch):
    shot_plan = [{
        "shot_id": "shot-001", "scene_id": "scene-001", "visual_beat_ids": ["vb-001"],
        "source_segment_ids": ["segment-001"], "characters_present": ["raja_001"],
        "primary_subject": "raja_001", "visual_action": "Raja stands safely outside his home.",
        "visual_focus": "Raja standing safely outside his home after returning", "framing": "medium",
        "camera_angle": "eye_level",
        "composition": "Raja occupies the foreground while the entrance of his home remains clearly visible behind him.",
        "emotion": "relieved", "location_id": "location-001", "estimated_duration_seconds": 3.0,
        "continuity_in": "Raja has reached the path outside his home.",
        "continuity_out": "Raja remains safely outside his home.", "shot_purpose": "Show the homecoming.",
    }]
    prompt = (
        "Single still keyframe. Raja stands safely outside his home. "
        "Visual focus: Raja standing safely outside his home after returning. Framing: medium. "
        "Camera angle: eye_level. Composition: Raja occupies the foreground while the entrance of his home remains clearly visible behind him. "
        "Emotion: relieved. Location: cottage path. Visual style: storybook watercolor. "
        "Preserve raja_001 exactly from the supplied approved character reference."
    )
    monkeypatch.setattr(image_agent, "load_model", lambda _name: (_ for _ in ()).throw(AssertionError("model should not run")))
    result = image_agent.build_image_prompts({
        "shot_plan": shot_plan,
        "parsed_requirements": {"visual_style": "storybook watercolor"},
        "production_bible": {
            "visual_style": "storybook watercolor",
            "characters": [{"character_id": "raja_001", "name": "Raja"}],
            "locations": [{"location_id": "location-001", "description": "cottage path"}],
        },
        "character_reference_files": ["raja.png"],
    })

    assert result["image_prompt_requests"][0]["character_reference_ids"] == ["raja_001"]
    assert result["image_prompt_requests"][0]["scene_id"] == "scene-001"
    assert result["image_prompt_requests"][0]["visual_beat_ids"] == ["vb-001"]
    assert result["image_prompt_requests"][0]["source_segment_ids"] == ["segment-001"]
    assert result["image_prompt_requests"][0]["location_id"] == "location-001"
    built_prompt = result["image_prompt_requests"][0]["image_prompt"]
    assert all(part in built_prompt for part in (
        "Raja stands safely outside his home.", "Framing: medium.",
        "Location: cottage path.", "Visual style: storybook watercolor.", "Preserve raja_001",
    ))
    assert result["llm_evaluations"] == []


def test_image_prompt_request_validator_reports_traceability_mismatches():
    approved = {
        "shot_id": "shot-001", "scene_id": "scene-001", "visual_beat_ids": ["vb-001"],
        "source_segment_ids": ["segment-001"], "location_id": "location-001",
        "characters_present": ["raja_001"],
    }
    request = {
        "shot_id": "shot-002", "scene_id": "scene-002", "visual_beat_ids": ["vb-002"],
        "source_segment_ids": ["segment-002"], "location_id": "location-002",
        "character_reference_ids": ["chuha_001"],
        "location_reference": {"location_id": "location-003"},
    }

    assert image_agent.validate_image_prompt_request(approved, request) == [
        "shot_id mismatch", "scene_id mismatch", "visual_beat_ids mismatch", "source_segment_ids mismatch",
        "location mismatch: expected location-001, got location-002",
        "character references do not match approved shot", "location_reference does not match location_id",
    ]


def test_video_probe_timeout_marks_video_unusable(monkeypatch, tmp_path):
    video = tmp_path / "stuck.mp4"
    video.write_bytes(b"video")

    def timeout(*_args, **_kwargs):
        raise image_agent.subprocess.TimeoutExpired("ffprobe", 30)

    monkeypatch.setattr(image_agent.subprocess, "run", timeout)
    assert image_agent._video_is_usable(video) is False


def test_visual_storyboard_uses_built_image_prompt(tmp_path):
    def run(calls):
        reference = tmp_path / "reference.png"
        reference.write_bytes(b"character")
        built_prompt = "Prepared single-keyframe prompt for shot-001."
        approved = {
            "shot_id": "shot-001", "scene_id": "scene-001", "visual_beat_ids": ["vb-001"],
            "source_segment_ids": ["segment-001"], "characters_present": ["raja_001"],
            "location_id": "location-001",
        }
        result = image_agent.create_visual_storyboard({
            "topic": "Raja returns home",
            "output_dir": str(tmp_path),
            "aspect_ratio": "16:9",
            "video_quality": "high",
            "production_bible": {"characters": [{"character_id": "raja_001"}]},
            "character_reference_files": [str(reference)],
            "shot_plan": [approved],
            "image_prompt_requests": [{
                "shot_id": "shot-001", "scene_id": "scene-001", "visual_beat_ids": ["vb-001"],
                "source_segment_ids": ["segment-001"], "image_prompt": built_prompt,
                "character_reference_ids": ["raja_001"],
                "location_id": "location-001",
                "location_reference": {"location_id": "location-001", "description": "home"},
                "previous_shot_id": None,
            }],
        })

        assert calls[0][1]["prompt"] == built_prompt
        assert calls[0][1]["aspect_ratio"] == "16:9"
        assert calls[0][1]["resolution"] == "2K"
        assert result["generated_images"][0]["generation_status"] == "success"
        assert result["generated_images"][0]["aspect_ratio"] == "16:9"
        assert result["generated_images"][0]["resolution"] == "2K"
        assert result["generated_images"][0]["generation_attempt"] == 1
        assert result["generated_images"][0]["image_path"] == result["image_files"][0]
        assert result["generated_images"][0]["provider"] == "magnific"
        assert result["generated_images"][0]["model_used"] == "nano-banana-pro-flash"

    with_fake_images(run)


def _shot_image_state(tmp_path, previous_ids):
    shots = []
    requests = []
    for number, previous_id in enumerate(previous_ids, start=1):
        shot_id = f"shot-{number:03}"
        shot = {
            "shot_id": shot_id, "scene_id": "scene-001", "visual_beat_ids": [f"vb-{number:03}"],
            "source_segment_ids": [f"segment-{number:03}"], "characters_present": [],
            "location_id": "location-001",
        }
        shots.append(shot)
        requests.append({
            "shot_id": shot_id, "scene_id": "scene-001", "visual_beat_ids": shot["visual_beat_ids"],
            "source_segment_ids": shot["source_segment_ids"], "image_prompt": f"Still for {shot_id}.",
            "character_reference_ids": [], "location_id": "location-001",
            "location_reference": {"location_id": "location-001"}, "previous_shot_id": previous_id,
        })
    return {
        "thread_id": "thread", "topic": "Parallel images", "output_dir": str(tmp_path),
        "shot_plan": shots, "image_prompt_requests": requests,
    }


class FalStub:
    """Fake of fal's queue API: submit, status, and result."""

    class Completed:
        pass

    def __init__(self, result, pending_polls=0):
        self.outcome = result
        self.pending_polls = pending_polls
        self.uploads = []
        self.calls = []
        self.status_checks = []

    def upload_file(self, path):
        self.uploads.append(path)
        return f"https://fal.example/{Path(path).name}"

    def submit(self, model, *, arguments):
        self.calls.append((model, arguments))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return type("Handle", (), {"request_id": f"request-{len(self.calls)}"})()

    def status(self, model, request_id):
        self.status_checks.append(request_id)
        if self.pending_polls:
            self.pending_polls -= 1
            return object()
        return self.Completed()

    def result(self, model, request_id):
        return self.outcome


def _fal_image_result(url="https://fal.example/generated.png"):
    return {
        "images": [{
            "url": url,
            "content_type": "image/png",
            "file_name": "generated.png",
            "file_size": 5,
            "width": 1024,
            "height": 1024,
        }],
        "description": "Generated still image.",
    }


def test_fal_reference_free_image_uses_text_endpoint_without_image_urls(monkeypatch, tmp_path):
    stub = FalStub(_fal_image_result())
    image_file = tmp_path / "shot.png"
    monkeypatch.setattr(image_agent, "fal_client", stub, raising=False)
    monkeypatch.setattr(image_agent, "_get_bytes", lambda url: b"image" if url.endswith("generated.png") else b"")

    model = image_agent._generate_fal_reference_image("A still", "16:9", image_file, [], "2K")

    assert model == "fal-ai/nano-banana-pro"
    assert stub.uploads == []
    assert stub.calls == [("fal-ai/nano-banana-pro", {
        "prompt": "A still", "aspect_ratio": "16:9", "resolution": "2K", "output_format": "png",
    })]
    assert image_file.read_bytes() == b"image"
    assert not image_file.with_suffix(".job.json").exists()


def test_fal_referenced_image_uploads_files_and_uses_edit_endpoint(monkeypatch, tmp_path):
    references = [tmp_path / "character.png", tmp_path / "previous.png"]
    for reference in references:
        reference.write_bytes(b"reference")
    stub = FalStub(_fal_image_result())
    image_file = tmp_path / "shot.png"
    monkeypatch.setattr(image_agent, "fal_client", stub, raising=False)
    monkeypatch.setattr(image_agent, "_get_bytes", lambda _url: b"image")

    model = image_agent._generate_fal_reference_image(
        "Preserve identity", "9:16", image_file, [str(path) for path in references], "1K"
    )

    assert model == "fal-ai/nano-banana-pro/edit"
    assert stub.uploads == [str(path) for path in references]
    assert stub.calls[0] == ("fal-ai/nano-banana-pro/edit", {
        "prompt": "Preserve identity",
        "aspect_ratio": "9:16",
        "resolution": "1K",
        "output_format": "png",
        "image_urls": ["https://fal.example/character.png", "https://fal.example/previous.png"],
    })
    assert image_file.read_bytes() == b"image"


def test_fal_image_without_output_url_fails_cleanly(monkeypatch, tmp_path):
    monkeypatch.setattr(image_agent, "fal_client", FalStub({"images": []}), raising=False)

    with pytest.raises(RuntimeError, match="image URL"):
        image_agent._generate_fal_reference_image("A still", "1:1", tmp_path / "shot.png", [], "1K")


@pytest.mark.parametrize("key", [None, "   "])
def test_fal_key_is_validated_before_worker_pool(monkeypatch, tmp_path, key):
    state = {**_shot_image_state(tmp_path, [None]), "image_provider": "fal"}
    if key is None:
        monkeypatch.delenv("FAL_KEY", raising=False)
    else:
        monkeypatch.setenv("FAL_KEY", key)
    monkeypatch.setattr(
        image_agent,
        "ThreadPoolExecutor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("worker pool must not start")),
    )

    with pytest.raises(RuntimeError, match="FAL_KEY"):
        image_agent.create_visual_storyboard(state)


def test_fal_timeout_is_retried_and_recorded(monkeypatch, tmp_path):
    state = {**_shot_image_state(tmp_path, [None]), "image_provider": "fal"}
    stub = FalStub(TimeoutError("fal request timed out"))
    monkeypatch.setenv("FAL_KEY", "test-key")
    monkeypatch.setattr(image_agent, "fal_client", stub, raising=False)

    item = image_agent.create_visual_storyboard(state)["generated_images"][0]

    assert item["generation_status"] == "failed"
    assert item["generation_attempt"] == 3
    assert item["error"] == "fal request timed out"
    assert len(stub.calls) == 3


def test_fal_failure_does_not_fall_back_to_magnific_and_records_metadata(monkeypatch, tmp_path):
    state = {**_shot_image_state(tmp_path, [None]), "image_provider": "fal"}
    monkeypatch.setenv("FAL_KEY", "test-key")
    monkeypatch.setattr(
        image_agent,
        "_generate_fal_reference_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fal unavailable")),
        raising=False,
    )
    monkeypatch.setattr(
        image_agent,
        "_generate_reference_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Magnific fallback is forbidden")),
    )

    item = image_agent.create_visual_storyboard(state)["generated_images"][0]

    assert item["generation_status"] == "failed"
    assert item["error"] == "fal unavailable"
    assert item["generation_attempt"] == 3
    assert item["provider"] == "fal"
    assert item["model_used"] == "fal-ai/nano-banana-pro"


def test_fal_success_records_reference_appropriate_model(monkeypatch, tmp_path):
    reference = tmp_path / "character.png"
    reference.write_bytes(b"reference")
    state = {**_shot_image_state(tmp_path, [None]), "image_provider": "fal"}
    state["shot_plan"][0]["characters_present"] = ["raja_001"]
    state["image_prompt_requests"][0].update({
        "character_reference_ids": ["raja_001"],
        "character_reference_files": [str(reference)],
    })
    monkeypatch.setenv("FAL_KEY", "test-key")

    def generate(_prompt, _size, image_file, _references, _resolution):
        image_file.write_bytes(b"image")
        return "fal-ai/nano-banana-pro/edit"

    monkeypatch.setattr(image_agent, "_generate_fal_reference_image", generate, raising=False)

    item = image_agent.create_visual_storyboard(state)["generated_images"][0]

    assert item["generation_status"] == "success"
    assert item["provider"] == "fal"
    assert item["model_used"] == "fal-ai/nano-banana-pro/edit"


def test_explicit_image_model_dispatches_independently_and_replaces_other_model_cache(monkeypatch, tmp_path):
    state = {**_shot_image_state(tmp_path, [None]), "image_provider": "magnific", "image_model": "fal-ai/nano-banana-pro", "video_model": "kling-v2-6-pro"}
    cached = tmp_path / "old.png"
    cached.write_bytes(b"old")
    state["image_files"] = [str(cached)]
    state["generated_images"] = [{"shot_id": "shot-001", "generation_status": "success", "provider": "magnific", "model_used": "nano-banana-pro-flash", "image_path": str(cached)}]
    monkeypatch.setenv("FAL_KEY", "test-key")
    calls = []

    def generate(_prompt, _size, image_file, _references, _resolution):
        calls.append(image_file)
        image_file.write_bytes(b"new")
        return "fal-ai/nano-banana-pro"

    monkeypatch.setattr(image_agent, "_generate_fal_reference_image", generate)
    item = image_agent.create_visual_storyboard(state)["generated_images"][0]

    assert len(calls) == 1
    assert item["image_path"] != str(cached)
    assert item["provider"] == "fal"
    assert item["selected_model_id"] == "fal-ai/nano-banana-pro"


def test_cached_images_get_or_preserve_provider_metadata(monkeypatch, tmp_path):
    state = {**_shot_image_state(tmp_path, [None, None]), "image_provider": "fal"}
    cached_files = [tmp_path / "cached-1.png", tmp_path / "cached-2.png"]
    for cached in cached_files:
        cached.write_bytes(b"image")
    state["image_files"] = [str(path) for path in cached_files]
    state["generated_images"] = [{
        "shot_id": "shot-002", "scene_id": "scene-001", "image_path": str(cached_files[1]),
        "image_url": None, "character_reference_ids": [], "location_id": "location-001",
        "generation_status": "success", "provider": "magnific", "model_used": "legacy-model",
        "generation_attempt": 1, "error": None,
    }]
    monkeypatch.setenv("FAL_KEY", "test-key")

    first, second = image_agent.create_visual_storyboard(state)["generated_images"]

    assert (first["provider"], first["model_used"]) == ("fal", "fal-ai/nano-banana-pro")
    assert (second["provider"], second["model_used"]) == ("magnific", "legacy-model")


def test_cached_reference_model_survives_predecessor_regeneration(monkeypatch, tmp_path):
    state = {**_shot_image_state(tmp_path, [None, "shot-001"]), "image_provider": "fal"}
    cached_files = [tmp_path / "cached-1.png", tmp_path / "cached-2.png"]
    for cached in cached_files:
        cached.write_bytes(b"image")
    state["image_files"] = [str(path) for path in cached_files]
    state["shot_image_qa_retry_shots"] = ["shot-001"]
    monkeypatch.setenv("FAL_KEY", "test-key")

    def generate(_prompt, _size, image_file, _references, _resolution):
        image_file.write_bytes(b"new image")
        return "fal-ai/nano-banana-pro"

    monkeypatch.setattr(image_agent, "_generate_fal_reference_image", generate)

    items = image_agent.create_visual_storyboard(state)["generated_images"]

    assert items[1]["image_path"] == str(cached_files[1])
    assert (items[1]["provider"], items[1]["model_used"]) == ("fal", "fal-ai/nano-banana-pro/edit")


def test_cached_partial_provenance_keeps_provider_model_pair(monkeypatch, tmp_path):
    state = {**_shot_image_state(tmp_path, [None]), "image_provider": "fal"}
    cached = tmp_path / "cached.png"
    cached.write_bytes(b"image")
    state["image_files"] = [str(cached)]
    state["generated_images"] = [{
        "shot_id": "shot-001", "scene_id": "scene-001", "image_path": str(cached),
        "generation_status": "success", "model_used": "nano-banana-pro-flash",
    }]
    monkeypatch.setenv("FAL_KEY", "test-key")

    item = image_agent.create_visual_storyboard(state)["generated_images"][0]

    assert (item["provider"], item["model_used"]) == ("magnific", "nano-banana-pro-flash")


def test_magnific_reference_generation_keeps_post_request_delay(monkeypatch, tmp_path):
    delays = []
    monkeypatch.setattr(image_agent, "_json_request", lambda *_args, **_kwargs: {"data": [{"base64": PNG_1X1}]})
    monkeypatch.setattr(image_agent, "_magnific_headers", lambda: {})
    monkeypatch.setattr(image_agent.time, "sleep", delays.append)

    image_agent._generate_reference_image("A still", "1:1", tmp_path / "shot.png", [], improve_prompt=False)

    assert delays == [1]


def test_fal_dependency_cycle_records_provider_and_model(monkeypatch, tmp_path):
    state = {**_shot_image_state(tmp_path, ["shot-002", "shot-001"]), "image_provider": "fal"}
    monkeypatch.setenv("FAL_KEY", "test-key")

    items = image_agent.create_visual_storyboard(state)["generated_images"]

    assert all(item["provider"] == "fal" for item in items)
    assert all(item["model_used"] == "fal-ai/nano-banana-pro" for item in items)


def test_fal_validation_failure_records_edit_model(monkeypatch, tmp_path):
    reference = tmp_path / "character.png"
    reference.write_bytes(b"reference")
    state = {**_shot_image_state(tmp_path, [None]), "image_provider": "fal"}
    state["image_prompt_requests"][0].update({
        "character_reference_ids": ["unexpected-character"],
        "character_reference_files": [str(reference)],
    })
    monkeypatch.setenv("FAL_KEY", "test-key")

    item = image_agent.create_visual_storyboard(state)["generated_images"][0]

    assert item["generation_status"] == "failed"
    assert item["provider"] == "fal"
    assert item["model_used"] == "fal-ai/nano-banana-pro/edit"


def test_independent_shot_images_generate_concurrently_in_order(monkeypatch, tmp_path):
    state = _shot_image_state(tmp_path, [None, None, None])
    active = 0
    peak = 0
    lock = threading.Lock()
    overlap = threading.Event()

    def generate(_prompt, _aspect_ratio, image_file, _references, **_kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                overlap.set()
        try:
            assert overlap.wait(2), "image jobs did not overlap"
            image_file.write_bytes(b"image")
        finally:
            with lock:
                active -= 1

    monkeypatch.setenv("IMAGE_GENERATION_WORKERS", "2")
    monkeypatch.setattr(image_agent, "_generate_reference_image", generate)

    result = image_agent.create_visual_storyboard(state)

    assert peak == 2
    assert [item["shot_id"] for item in result["generated_images"]] == ["shot-001", "shot-002", "shot-003"]
    assert all(result["image_files"])


def test_shot_image_waits_for_previous_image(monkeypatch, tmp_path):
    state = _shot_image_state(tmp_path, [None, "shot-001"])
    calls = []

    def generate(_prompt, _aspect_ratio, image_file, references, **_kwargs):
        calls.append((image_file.stem.rsplit("_v", 1)[0], list(references)))
        image_file.write_bytes(b"image")

    monkeypatch.setattr(image_agent, "_generate_reference_image", generate)

    result = image_agent.create_visual_storyboard(state)

    assert [shot_id for shot_id, _ in calls] == ["shot-001", "shot-002"]
    assert result["image_files"][0] in calls[1][1]


def test_shot_image_dependency_cycle_stops_without_generation(monkeypatch, tmp_path):
    state = _shot_image_state(tmp_path, ["shot-002", "shot-001"])
    calls = []
    monkeypatch.setattr(image_agent, "_generate_reference_image", lambda *_args, **_kwargs: calls.append(True))

    result = image_agent.create_visual_storyboard(state)

    assert calls == []
    assert all(item["generation_status"] == "failed" for item in result["generated_images"])
    assert all("dependency cycle" in item["error"] for item in result["generated_images"])


def test_shot_image_generation_keeps_success_when_another_shot_exhausts_retries(monkeypatch, tmp_path):
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"character")
    shots = [
        {
            "shot_id": f"shot-{number:03}", "scene_id": "scene-001", "visual_beat_ids": [f"vb-{number:03}"],
            "source_segment_ids": [f"segment-{number:03}"], "characters_present": ["raja_001"],
            "location_id": "location-001",
        }
        for number in (1, 2)
    ]
    requests = [
        {
            "shot_id": item["shot_id"], "scene_id": item["scene_id"],
            "visual_beat_ids": item["visual_beat_ids"], "source_segment_ids": item["source_segment_ids"],
            "image_prompt": f"Approved still for {item['shot_id']}.",
            "character_reference_ids": ["raja_001"], "character_reference_files": [str(reference)],
            "location_id": "location-001",
            "location_reference": {"location_id": "location-001", "description": "home"},
            "previous_shot_id": "shot-001" if item["shot_id"] == "shot-002" else None,
        }
        for item in shots
    ]
    attempts = {item["shot_id"]: 0 for item in shots}
    second_references = []

    def generate(prompt, _size, image_file, reference_files, **kwargs):
        shot_id = image_file.stem.rsplit("_v", 1)[0]
        attempts[shot_id] += 1
        assert kwargs["improve_prompt"] is False
        if shot_id == "shot-002":
            assert "Preserve Raja's approved face." in prompt
            second_references[:] = reference_files
            raise RuntimeError("provider unavailable")
        image_file.write_bytes(b"image")

    monkeypatch.setattr(image_agent, "_generate_reference_image", generate)
    result = image_agent.create_visual_storyboard({
        "topic": "Raja returns home", "output_dir": str(tmp_path),
        "shot_plan": shots, "image_prompt_requests": requests,
        "shot_image_qa_retry_shots": ["shot-002"],
        "shot_image_qa_results": [{
            "shot_id": "shot-002",
            "issues": [{"correction": "Preserve Raja's approved face."}],
        }],
    })

    assert [item["generation_status"] for item in result["generated_images"]] == ["success", "failed"]
    assert attempts == {"shot-001": 1, "shot-002": 3}
    assert Path(result["image_files"][0]).is_file() and result["image_files"][1] is None
    assert result["generated_images"][1]["generation_attempt"] == 3
    assert result["generated_images"][1]["error"] == "provider unavailable"
    assert result["image_files"][0] in second_references


def test_reference_package_is_versioned_and_regenerated_together(tmp_path):
    def run(calls):
        state = {
            "topic": "Raja returns home",
            "tone": "cinematic",
            "characters": ["Raja — red coat"],
            "story": "Raja returns home.",
            "production_bible": {"theme": "homecoming"},
            "output_dir": str(tmp_path),
        }
        first = image_agent.create_reference_package(state)
        second = image_agent.create_reference_package({**state, **first, "reference_feedback": "use colder moonlight"})

        assert Path(first["character_board_file"]).name == "characters_reference_v001.png"
        assert Path(second["character_board_file"]).name == "characters_reference_v002.png"
        assert Path(second["mood_board_file"]).name == "mood_board_v002.png"
        assert all(Path(path).exists() for path in (first["character_board_file"], first["mood_board_file"], second["character_board_file"], second["mood_board_file"]))
        assert len(calls) == 4
        # The layout template is opt-in because image models copy the person in it.
        assert "reference_images" not in calls[0][1]
        assert "Layout: three full-body views" in calls[0][1]["prompt"]
        assert "use colder moonlight" in calls[-1][1]["prompt"]

    with_fake_images(run)


def test_reference_package_model_change_regenerates_cached_boards(monkeypatch, tmp_path):
    calls = []

    def generate(state, _prompt, _size, path, _references, *_args, **_kwargs):
        calls.append(state["image_model"])
        path.write_bytes(base64.b64decode(PNG_1X1))

    monkeypatch.setattr(image_agent, "_generate_provider_image", generate)
    state = {
        "topic": "Raja", "tone": "cinematic", "characters": ["Raja"],
        "story": "Raja returns.", "output_dir": str(tmp_path),
        "image_model": "nano-banana-pro-flash",
    }
    first = image_agent.create_reference_package(state)
    second = image_agent.create_reference_package({**state, **first, "image_model": "fal-ai/nano-banana-pro"})

    assert calls == ["nano-banana-pro-flash"] * 2 + ["fal-ai/nano-banana-pro"] * 2
    assert first["reference_model_id"] == "nano-banana-pro-flash"
    assert second["reference_model_id"] == "fal-ai/nano-banana-pro"
    assert second["character_board_file"] != first["character_board_file"]


def test_fal_reference_package_never_calls_magnific(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("FAL_KEY", "test-key")

    def fal_image(prompt, size, path, references, resolution):
        calls.append((prompt, list(references)))
        path.write_bytes(base64.b64decode(PNG_1X1))
        return image_agent._fal_image_model(references)

    monkeypatch.setattr(image_agent, "_generate_fal_reference_image", fal_image)
    monkeypatch.setattr(image_agent, "_generate_reference_image", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Magnific used")))
    state = {
        "topic": "Fal reference run", "tone": "cinematic", "characters": ["Raja"],
        "story": "Raja returns home.", "output_dir": str(tmp_path), "image_provider": "fal",
    }

    result = image_agent.create_reference_package(state)

    assert len(calls) == 2
    assert Path(result["character_board_file"]).is_file()
    assert Path(result["mood_board_file"]).is_file()


def test_fal_video_uses_fal_image_upload_without_magnific(monkeypatch, tmp_path, capsys):
    image = tmp_path / "shot.png"
    image.write_bytes(base64.b64decode(PNG_1X1))
    calls = []
    monkeypatch.setenv("FAL_KEY", "test-key")

    class FakeFal:
        def upload_file(self, path):
            calls.append(("upload", path))
            return "https://fal.example/shot.png"

        Completed = FalStub.Completed

        def submit(self, model, arguments, **_kwargs):
            calls.append((model, arguments))
            return type("Handle", (), {"request_id": "video-request"})()

        def status(self, _model, _request_id):
            return self.Completed()

        def result(self, _model, _request_id):
            return {"video": {"url": "https://fal.example/shot.mp4"}}

    monkeypatch.setattr(image_agent, "fal_client", FakeFal())
    monkeypatch.setattr(image_agent, "_json_request", lambda *_args: (_ for _ in ()).throw(AssertionError("Magnific used")))
    monkeypatch.setattr(image_agent, "_get_bytes", lambda _url: b"fake mp4")
    monkeypatch.setattr(image_agent, "_video_is_usable", lambda _path: True)
    monkeypatch.setattr(image_agent.time, "sleep", lambda _seconds: None)

    output = image_agent._generate_scene_video_file(
        {"image_provider": "magnific", "video_model": "fal-ai/kling-video/v2.6/pro/image-to-video"}, {"duration_seconds": 5}, str(image),
        tmp_path / "shot.mp4", "Animate the shot", aspect_ratio="social_story_9_16",
    )

    assert Path(output).read_bytes() == b"fake mp4"
    assert calls[0] == ("upload", str(image))
    assert calls[1][0] == "fal-ai/kling-video/v2.6/pro/image-to-video"
    assert calls[1][1]["start_image_url"] == "https://fal.example/shot.png"
    log = capsys.readouterr().out
    assert all(f"{phase} elapsed=" in log for phase in ("upload", "provider_wait", "download", "probe"))


def test_regenerates_only_selected_shot(tmp_path):
    def run(calls):
        reference = tmp_path / "reference.png"
        mood = tmp_path / "mood.png"
        reference.write_bytes(b"x")
        mood.write_bytes(b"x")
        state = {
            "topic": "Selective regeneration",
            "tone": "cinematic",
            "characters": ["Raja — red coat"],
            "story": "Raja crosses a forest and reaches a lake.",
            "production_bible": {
                "theme": "one moonlit journey",
                "characters": [
                    {"character_id": "raja_001", "name": "Raja", "appearance": "traveler", "wardrobe": "red coat", "props": []}
                ],
            },
            "character_reference_files": [str(reference)],
            "mood_board_file": str(mood),
            "output_dir": str(tmp_path),
            "storyboard": [shot(1, "Raja in a forest."), shot(2, "Raja at a lake.")],
        }
        first = image_agent.create_visual_storyboard(state)
        second = image_agent.create_visual_storyboard(
            {**state, **first, "visual_feedback": [{"shot_id": "shot-002", "note": "make the lake brighter"}]}
        )

        assert first["image_files"][0] == second["image_files"][0]
        assert first["image_files"][1] != second["image_files"][1]
        assert Path(second["image_files"][1]).name == "shot-002_v002.png"
        assert len(calls) == 3
        assert calls[0][0] == image_agent.MAGNIFIC_REFERENCE_IMAGE_URL
        assert "Approved continuity context" in calls[0][1]["prompt"]
        assert "Original user request (highest priority): Selective regeneration" in calls[0][1]["prompt"]
        assert "Never infer status" in calls[0][1]["prompt"]
        assert not {"king", "queen", "royal"} & set(calls[0][1]["prompt"].lower().split())
        assert calls[0][1]["reference_images"][0]["image"] == base64.b64encode(b"x").decode("ascii")
        assert calls[0][1]["reference_images"][1]["image"] == base64.b64encode(b"x").decode("ascii")
        assert "make the lake brighter" in calls[-1][1]["prompt"]

    with_fake_images(run)


def test_reference_package_generates_one_isolated_sheet_per_character(tmp_path):
    def run(calls):
        state = {
            "topic": "A lion and mouse become friends",
            "tone": "playful",
            "characters": ["Leo — lion", "Pip — mouse"],
            "story": "Leo the lion helps Pip the mouse.",
            "production_bible": {
                "theme": "friendship",
                "characters": [
                    {"name": "Leo", "appearance": "golden lion", "wardrobe": "none", "props": []},
                    {"name": "Pip", "appearance": "brown mouse", "wardrobe": "none", "props": []},
                ],
            },
            "output_dir": str(tmp_path),
        }
        result = image_agent.create_reference_package(state)

        assert len(result["character_reference_files"]) == 2
        assert "exactly this one character: Leo" in calls[0][1]["prompt"]
        assert "exactly this one character: Pip" in calls[1][1]["prompt"]
        assert calls[2][1]["reference_images"][0]["image"]
        assert calls[2][1]["reference_images"][1]["image"]
        with Image.open(result["character_board_file"]) as board:
            assert board.size == (1024, 1024)

    with_fake_images(run)


def test_visual_shot_uses_only_declared_cast_references(tmp_path):
    def run(calls):
        lion = tmp_path / "lion.png"
        mouse = tmp_path / "mouse.png"
        mood = tmp_path / "mood.png"
        lion.write_bytes(b"lion")
        mouse.write_bytes(b"mouse")
        mood.write_bytes(b"mood")
        scene = {
            **shot(1, "Lion rests alone in the meadow."),
            "characters_present": ["lion_001"],
            "narration": "Lion rests.",
            "subject_motion": "Lion breathes slowly.",
            "continuity_in": "Lion enters alone.",
            "continuity_out": "Lion remains alone.",
        }
        state = {
            "topic": "A lion rests alone",
            "tone": "quiet",
            "story": "Lion rests alone.",
            "production_bible": {
                "theme": "rest",
                "characters": [
                    {"character_id": "lion_001", "name": "Lion", "appearance": "golden lion", "wardrobe": "none", "props": []},
                    {"character_id": "mouse_001", "name": "Mouse", "appearance": "brown mouse", "wardrobe": "none", "props": []},
                ],
            },
            "character_reference_files": [str(lion), str(mouse)],
            "mood_board_file": str(mood),
            "output_dir": str(tmp_path),
            "storyboard": [scene],
        }
        image_agent.create_visual_storyboard(state)

        payload = calls[0][1]
        assert payload["reference_images"][0]["image"] == base64.b64encode(b"lion").decode("ascii")
        assert payload["reference_images"][1]["image"] == base64.b64encode(b"mood").decode("ascii")
        assert len(payload["reference_images"]) == 2
        assert payload["aspect_ratio"] == "9:16"
        assert payload["resolution"] == "1K"
        assert "Depict exactly 1 recurring character" in payload["prompt"]
        assert "Exact narration for this frame: Lion rests." in payload["prompt"]
        assert "mouse_001" not in payload["prompt"]

    with_fake_images(run)


def test_local_mode_skips_paid_video_and_warns(tmp_path):
    old = os.environ.pop("MINIO_PUBLIC_ENDPOINT", None)
    try:
        files, warnings = image_agent.create_scene_videos(
            {"topic": "Local", "output_dir": str(tmp_path), "storyboard": [shot(1, "Raja walks.")]},
            [str(tmp_path / "shot.png")],
        )
    finally:
        if old is not None:
            os.environ["MINIO_PUBLIC_ENDPOINT"] = old
    assert files == [None]
    assert "FFmpeg" in warnings[0]


def test_public_mode_uses_kling_26_image_url_and_supported_duration(tmp_path):
    original_request = image_agent._json_request
    original_headers = image_agent._magnific_headers
    original_public_url = image_agent.public_artifact_url
    original_video_check = image_agent._video_is_usable
    original_sleep = image_agent.time.sleep
    old_endpoint = os.environ.get("MINIO_PUBLIC_ENDPOINT")
    payloads = []

    def fake_request(_url, payload, _headers):
        payloads.append(payload)
        return {"data": {"generated": [base64.b64encode(b"fake mp4").decode("ascii")]}}

    os.environ["MINIO_PUBLIC_ENDPOINT"] = "https://media.example.com"
    image_agent._json_request = fake_request
    image_agent._magnific_headers = lambda: {}
    image_agent.public_artifact_url = lambda _state, _path: "https://media.example.com/shot.png"
    image_agent._video_is_usable = lambda _path: True
    image_agent.time.sleep = lambda _seconds: None
    scene = shot(1, "Raja walks.")
    scene["duration_seconds"] = 7
    try:
        files, warnings = image_agent.create_scene_videos(
            {
                "thread_id": "thread", "topic": "Remote", "output_dir": str(tmp_path), "storyboard": [scene],
                "motion_plans": [{
                    "shot_id": "shot-001", "duration_seconds": 6,
                    "video_prompt": "Use only the approved gentle walking motion.",
                    "negative_prompt": "No face distortion or camera shake.",
                }],
            },
            [str(tmp_path / "shot.png")],
        )
    finally:
        image_agent._json_request = original_request
        image_agent._magnific_headers = original_headers
        image_agent.public_artifact_url = original_public_url
        image_agent._video_is_usable = original_video_check
        image_agent.time.sleep = original_sleep
        if old_endpoint is None:
            os.environ.pop("MINIO_PUBLIC_ENDPOINT", None)
        else:
            os.environ["MINIO_PUBLIC_ENDPOINT"] = old_endpoint

    assert warnings == []
    assert Path(files[0]).exists()
    assert payloads[0]["image"] == "https://media.example.com/shot.png"
    assert payloads[0]["duration"] == "5"
    assert payloads[0]["prompt"] == "Use only the approved gentle walking motion."
    assert payloads[0]["negative_prompt"] == "No face distortion or camera shake."
    assert payloads[0]["aspect_ratio"] == "social_story_9_16"
    assert payloads[0]["generate_audio"] is False


def test_improved_prompt_respects_both_magnific_limits():
    original_request = image_agent._json_request
    original_headers = image_agent._magnific_headers
    old_setting = os.environ.get("MAGNIFIC_IMPROVE_PROMPTS")
    sent = {}
    start, end = "CAST AND SCENE: ", " KEEP EXACT IDENTITIES AND ADD NO CHARACTERS"

    def fake_request(_url, payload, _headers):
        sent.update(payload)
        return {"data": {"generated": [start + "y" * 4000 + end]}}

    os.environ["MAGNIFIC_IMPROVE_PROMPTS"] = "true"
    image_agent._json_request = fake_request
    image_agent._magnific_headers = lambda: {}
    try:
        result = image_agent._improve_prompt(start + "x" * 4000 + end, "image")
    finally:
        image_agent._json_request = original_request
        image_agent._magnific_headers = original_headers
        if old_setting is None:
            os.environ.pop("MAGNIFIC_IMPROVE_PROMPTS", None)
        else:
            os.environ["MAGNIFIC_IMPROVE_PROMPTS"] = old_setting

    assert len(sent["prompt"]) == image_agent.IMPROVE_PROMPT_LIMIT
    assert len(result) == image_agent.GENERATION_PROMPT_LIMIT
    assert sent["prompt"].startswith(start) and sent["prompt"].endswith(end)
    assert result.startswith(start) and result.endswith(end)


if __name__ == "__main__":
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        test_reference_package_is_versioned_and_regenerated_together(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_regenerates_only_selected_shot(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_reference_package_generates_one_isolated_sheet_per_character(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_visual_shot_uses_only_declared_cast_references(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_local_mode_skips_paid_video_and_warns(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_public_mode_uses_wan_27_image_url_and_director_duration(Path(tmp))
    test_improved_prompt_respects_both_magnific_limits()


def test_character_specs_keep_story_description():
    state = {"characters": [{"name": "Sher", "description": "A businessman in his late 30s."}]}

    assert image_agent._character_specs(state) == ["Sher — A businessman in his late 30s."]


def test_reference_legend_names_each_attached_image():
    prompt = image_agent._with_reference_legend("Draw the shot.", 2, ["character sheet for Sher"])

    assert prompt == (
        "Attached reference images, in order. Image 1: character sheet for Sher. "
        "Image 2: reference image.\nDraw the shot."
    )
    assert image_agent._with_reference_legend("Draw the shot.", 0, None) == "Draw the shot."


def test_later_shots_reference_the_scene_anchor_so_they_render_in_parallel():
    def planned(number, scene_id, framing):
        return {
            "shot_id": f"shot-{number:03}", "scene_id": scene_id, "visual_beat_ids": [f"vb-{number:03}"],
            "source_segment_ids": [f"segment-{number:03}"], "characters_present": [], "primary_subject": "path",
            "visual_action": f"The path at moment {number}.", "visual_focus": f"path moment {number}",
            "framing": framing, "camera_angle": "eye_level", "composition": "The path recedes between trees.",
            "emotion": "calm", "location_id": "location-001", "estimated_duration_seconds": 2.0,
            "continuity_in": "", "continuity_out": "", "shot_purpose": "Set the place.",
        }

    result = image_agent.build_image_prompts({
        "shot_plan": [
            planned(1, "scene-001", "wide"), planned(2, "scene-001", "medium"),
            planned(3, "scene-001", "close_up"), planned(4, "scene-002", "wide"),
        ],
        "production_bible": {"locations": [{"location_id": "location-001", "description": "forest path"}]},
    })

    assert [item["previous_shot_id"] for item in result["image_prompt_requests"]] == [
        None, "shot-001", "shot-001", None,
    ]


def test_location_plate_is_attached_to_shots_in_that_location(tmp_path):
    def run(calls):
        plate = tmp_path / "location_plate.png"
        plate.write_bytes(base64.b64decode(PNG_1X1))
        state = {**_shot_image_state(tmp_path, [None]), "location_reference_files": {"location-001": str(plate)}}

        result = image_agent.create_visual_storyboard(state)

        assert result["generated_images"][0]["generation_status"] == "success"
        references = calls[0][1]["reference_images"]
        assert len(references) == 1 and "empty background plate" in references[0]["text"]

    with_fake_images(run)


def test_reference_package_renders_one_plate_per_location(tmp_path):
    def run(calls):
        result = image_agent.create_reference_package({
            "topic": "Raja returns home", "tone": "warm", "characters": ["Raja"], "story": "Raja returns home.",
            "output_dir": str(tmp_path), "visual_style": "storybook watercolor",
            "scenes": [
                {"scene_id": "scene-001", "location_id": "location-001", "location_description": "forest path"},
                {"scene_id": "scene-002", "location_id": "location-001"},
                {"scene_id": "scene-003", "location_id": "location-002", "location_description": "cottage door"},
            ],
        })

        plates = result["location_reference_files"]
        assert set(plates) == {"location-001", "location-002"} and all(Path(path).is_file() for path in plates.values())
        plate_prompts = [payload["prompt"] for _, payload in calls if "background plate" in payload["prompt"]]
        assert len(plate_prompts) == 2 and all("storybook watercolor" in prompt for prompt in plate_prompts)
        assert len(calls) == 4  # one character sheet, two plates, one mood board

    with_fake_images(run)


def test_prefetched_character_sheets_are_reused_by_the_reference_package(tmp_path):
    def run(calls):
        state = {
            "topic": "Prefetch run", "tone": "warm", "characters": ["Raja"], "story": "Raja returns home.",
            "output_dir": str(tmp_path),
        }
        image_agent.prefetch_character_sheets(state)
        image_agent.prefetch_character_sheets(state)  # a second start for the same story is ignored

        result = image_agent.create_reference_package(state)

        sheet_calls = [payload for _, payload in calls if "reference sheet" in payload["prompt"]]
        assert len(sheet_calls) == 1
        assert [Path(path).name for path in result["character_reference_files"]] == ["character_001_raja_v001.png"]
        assert len(calls) == 2  # one prefetched sheet and the mood board

    with_fake_images(run)
