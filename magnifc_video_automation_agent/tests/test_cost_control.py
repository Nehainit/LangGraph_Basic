import base64
from pathlib import Path

import pytest

from video_automation import cost_control
from video_automation.agents import image_agent, shot_video_generation_agent
from video_automation.cost_control import BudgetExceeded, PendingJobError, is_permanent_error, ledger_for, paid_call

PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
)


@pytest.fixture(autouse=True)
def fresh_ledgers(monkeypatch):
    monkeypatch.setattr(cost_control, "_ledgers", {})
    monkeypatch.setitem(cost_control.PIPELINE_CONFIG["budget"], "max_usd_per_video", 5.0)


def test_ledger_stops_paid_calls_past_the_budget_and_refunds_failed_ones(monkeypatch):
    monkeypatch.setitem(cost_control.PIPELINE_CONFIG["budget"], "max_usd_per_video", 1.0)
    ledger = ledger_for({"thread_id": "run", "llm_evaluations": [{"cost_usd": 0.2}]})

    ledger.charge("image:a", "image", "shot-001", 0.5)
    ledger.charge("image:a", "image", "shot-001", 0.5)  # the same job is never charged twice
    with pytest.raises(RuntimeError), paid_call(ledger, "image:b", "image", "shot-002", 0.1):
        raise RuntimeError("provider unavailable")
    with pytest.raises(PendingJobError), paid_call(ledger, "video:c", "video", "shot-003", 0.2):
        raise PendingJobError("still running")
    with pytest.raises(BudgetExceeded, match=r"Budget of \$1.00 per video reached \(\$0.90 spent\)"):
        ledger.charge("image:d", "image", "shot-004", 0.15)

    assert [entry["key"] for entry in ledger.snapshot()] == ["image:a", "video:c"]
    assert ledger.spent() == 0.9


@pytest.mark.parametrize(("error", "permanent"), [
    (RuntimeError("POST https://api failed: HTTP 400 invalid prompt"), True),
    (RuntimeError("Request rejected by content_policy"), True),
    (RuntimeError("POST https://api failed: HTTP 429 rate limit"), False),
    (RuntimeError("POST https://api failed: HTTP 503 overloaded"), False),
    (TimeoutError("fal request timed out"), False),
    (PendingJobError("fal job still running"), False),
    (BudgetExceeded("budget"), True),
])
def test_permanent_errors_are_told_apart_from_retryable_ones(error, permanent):
    assert is_permanent_error(error) is permanent


class PendingFal:
    """fal queue fake whose first status check reports the job still running."""

    class Completed:
        pass

    def __init__(self):
        self.submitted = []
        self.polls = 0

    def submit(self, model, *, arguments):
        self.submitted.append(model)
        return type("Handle", (), {"request_id": f"request-{len(self.submitted)}"})()

    def status(self, _model, request_id):
        self.polls += 1
        return object() if self.polls == 1 else self.Completed()

    def result(self, _model, request_id):
        return {"images": [{"url": f"https://fal.example/{request_id}.png"}]}


def test_timed_out_fal_job_is_resumed_instead_of_paid_for_again(monkeypatch, tmp_path):
    fal = PendingFal()
    monkeypatch.setattr(image_agent, "fal_client", fal)
    job_file = tmp_path / "shot.job.json"

    with pytest.raises(PendingJobError, match="resumes it"):
        image_agent._fal_job("fal-ai/nano-banana-pro", lambda: {}, job_file, "same-request", timeout=0)
    assert job_file.is_file()

    result = image_agent._fal_job("fal-ai/nano-banana-pro", lambda: {}, job_file, "same-request", timeout=60)

    assert fal.submitted == ["fal-ai/nano-banana-pro"]
    assert result["images"][0]["url"].endswith("request-1.png")
    assert not job_file.exists()


def _image_state(tmp_path, existing_image=None):
    shot = {
        "shot_id": "shot-001", "scene_id": "scene-001", "visual_beat_ids": ["vb-001"],
        "source_segment_ids": ["segment-001"], "characters_present": [], "location_id": "location-001",
    }
    state = {
        "thread_id": "image-run", "topic": "Budget", "output_dir": str(tmp_path), "shot_plan": [shot],
        "image_prompt_requests": [{
            **{key: shot[key] for key in ("shot_id", "scene_id", "visual_beat_ids", "source_segment_ids", "location_id")},
            "image_prompt": "A still.", "character_reference_ids": [],
            "location_reference": {"location_id": "location-001"}, "previous_shot_id": None,
        }],
    }
    if existing_image:
        state.update(
            image_files=[str(existing_image)],
            generated_images=[{"shot_id": "shot-001", "image_path": str(existing_image), "generation_status": "success"}],
        )
    return state


def _fake_magnific(monkeypatch, outcome):
    calls = []

    def request(url, payload, _headers):
        calls.append(payload)
        if isinstance(outcome, Exception):
            raise outcome
        return {"data": [{"base64": PNG_1X1}]}

    monkeypatch.setattr(image_agent, "_json_request", request)
    monkeypatch.setattr(image_agent, "_magnific_headers", lambda: {})
    monkeypatch.setattr(image_agent.time, "sleep", lambda _seconds: None)
    return calls


def test_permanent_image_failure_is_not_retried(monkeypatch, tmp_path):
    calls = _fake_magnific(monkeypatch, RuntimeError("POST https://api failed: HTTP 400 content policy"))

    item = image_agent.create_visual_storyboard(_image_state(tmp_path))["generated_images"][0]

    assert item["generation_status"] == "failed" and item["generation_attempt"] == 1 and len(calls) == 1


def test_automatic_image_redo_is_limited_per_shot(monkeypatch, tmp_path):
    calls = _fake_magnific(monkeypatch, None)
    existing = tmp_path / "shot-001_v001.png"
    existing.write_bytes(base64.b64decode(PNG_1X1))
    state = {**_image_state(tmp_path, existing), "shot_image_qa_retry_shots": ["shot-001"]}

    first = image_agent.create_visual_storyboard(state)
    second = image_agent.create_visual_storyboard({**state, **first, "shot_image_qa_retry_shots": ["shot-001"]})

    assert len(calls) == 1 and first["paid_redo_counts"] == {"shot-001": {"image": 1}}
    assert second["image_files"] == first["image_files"]
    assert "already used its automatic image redo" in second["warnings"][-1]


def test_budget_keeps_the_current_image_on_a_redo_and_stops_a_first_image(monkeypatch, tmp_path):
    monkeypatch.setitem(cost_control.PIPELINE_CONFIG["budget"], "max_usd_per_video", 0.01)
    calls = _fake_magnific(monkeypatch, None)
    existing = tmp_path / "shot-001_v001.png"
    existing.write_bytes(base64.b64decode(PNG_1X1))

    redo = image_agent.create_visual_storyboard({**_image_state(tmp_path, existing), "shot_image_qa_retry_shots": ["shot-001"]})
    first = image_agent.create_visual_storyboard({**_image_state(tmp_path / "new"), "thread_id": "other-run"})

    assert calls == []
    assert redo["image_files"] == [str(existing)] and redo["budget_exhausted"] is False
    assert first["image_files"] == [None] and first["budget_exhausted"] is True
    assert "raise budget.max_usd_per_video" in first["warnings"][-1]


def test_automatic_video_redo_uses_free_ffmpeg_motion_once_its_redo_is_spent(monkeypatch, tmp_path):
    image = tmp_path / "shot-001.png"
    image.write_bytes(b"image")
    state = {
        "thread_id": "video-run", "topic": "Budget", "output_dir": str(tmp_path), "image_provider": "fal",
        "shot_plan": [{"shot_id": "shot-001", "scene_id": "scene-001"}], "image_files": [str(image)],
        "motion_plans": [{"shot_id": "shot-001", "duration_seconds": 5, "video_prompt": "Move.", "negative_prompt": "No."}],
        "generated_videos": [{"shot_id": "shot-001", "video_file": "first.mp4", "generation_status": "success", "provider": "fal"}],
        "video_retry_shots": ["shot-001"], "paid_redo_counts": {"shot-001": {"video": 1}},
    }
    monkeypatch.setenv("FAL_KEY", "test-key")
    monkeypatch.setattr(
        shot_video_generation_agent, "_generate_scene_video_file",
        lambda *_args: (_ for _ in ()).throw(AssertionError("paid video generated")),
    )
    monkeypatch.setattr(shot_video_generation_agent, "_clip", lambda _image, _seconds, _motion, path, *_args: Path(path).write_bytes(b"clip"))

    result = shot_video_generation_agent.create_shot_videos(state)

    assert result["generated_videos"][0]["provider"] == "ffmpeg"
    assert any("already used its automatic video redo" in warning for warning in result["warnings"])
