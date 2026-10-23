import subprocess
from pathlib import Path

import pytest

from video_automation.agents import lip_sync_agent
from video_automation.agents.lip_sync_agent import lip_sync_shots, shot_speaker

LINES = [
    {"speaker": "character-001", "start": 0.5, "end": 2.0},
    {"speaker": "character-002", "start": 2.2, "end": 2.6},
    {"speaker": "character-002", "start": 5.5, "end": 7.5},
]


def test_the_character_who_speaks_most_in_a_shot_is_synced():
    assert shot_speaker(LINES, 0.0, 3.0, ["character-001", "character-002"], 0.4) == "character-001"
    assert shot_speaker(LINES, 5.0, 8.0, None, 0.4) == "character-002"


def test_an_off_screen_speaker_or_a_silent_shot_is_not_synced():
    assert shot_speaker(LINES, 5.0, 8.0, ["character-001"], 0.4) is None
    assert shot_speaker(LINES, 3.0, 5.0, None, 0.4) is None
    assert shot_speaker(LINES, 2.3, 2.6, None, 0.4) is None


def _state(tmp_path, **extra):
    clips = []
    for number in (1, 2):
        clip = tmp_path / f"shot-00{number}.mp4"
        clip.write_bytes(b"clip")
        clips.append(str(clip))
    return {
        "voice_mode": "lipsync", "topic": "race", "output_dir": str(tmp_path), "narration_file": str(tmp_path / "narration.mp3"),
        "narration_segments": [
            {"segment_id": "segment-001", "speaker_id": "character-001", "text": "Catch me!"},
            {"segment_id": "segment-002", "speaker_id": "character-002", "text": "Slow and steady."},
        ],
        "narration_segment_timings": [
            {"segment_id": "segment-001", "start_seconds": 0.5, "end_seconds": 2.0},
            {"segment_id": "segment-002", "start_seconds": 5.5, "end_seconds": 7.5},
        ],
        "shot_plan": [
            {"shot_id": "shot-001", "characters_present": ["character-001"]},
            {"shot_id": "shot-002", "characters_present": ["character-001"]},
        ],
        "edit_timeline": [
            {"shot_id": "shot-001", "video_file": clips[0], "source_in": 0.4, "source_out": 4.4, "timeline_start": 0.0, "timeline_end": 4.0},
            {"shot_id": "shot-002", "video_file": clips[1], "source_in": 0.0, "source_out": 4.0, "timeline_start": 4.0, "timeline_end": 8.0},
        ],
        "scene_video_files": clips,
        "warnings": [],
        **extra,
    }


def test_narrated_films_are_left_alone(tmp_path):
    assert lip_sync_shots(_state(tmp_path, voice_mode="narration")) == {}


def test_a_film_without_character_lines_keeps_its_clips(tmp_path):
    result = lip_sync_shots(_state(tmp_path, narration_segments=[]))
    assert "edit_timeline" not in result
    assert "no character lines" in result["warnings"][0]


def test_speaking_shots_get_synced_clips(tmp_path, monkeypatch):
    calls = []

    def fake_sync(state, item, speaker, lines, out):
        calls.append((item["shot_id"], speaker))
        synced = out / f"{item['shot_id']}_lipsync.mp4"
        synced.write_bytes(b"synced")
        return synced

    monkeypatch.setattr(lip_sync_agent, "_sync_shot", fake_sync)
    result = lip_sync_shots(_state(tmp_path))

    # Shot 2's speaker (character-002) is off screen, so only shot 1 is synced.
    assert calls == [("shot-001", "character-001")]
    assert result["lip_synced_shots"] == ["shot-001"]
    first, second = result["edit_timeline"]
    assert first["video_file"].endswith("shot-001_lipsync.mp4") and (first["source_in"], first["source_out"]) == (0.0, 4.0)
    assert second["video_file"].endswith("shot-002.mp4") and second["source_in"] == 0.0
    assert result["scene_video_files"][0] == first["video_file"]


def test_a_failed_sync_keeps_the_original_clip(tmp_path, monkeypatch):
    def failing(*_args):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(lip_sync_agent, "_sync_shot", failing)
    state = _state(tmp_path)
    result = lip_sync_shots(state)

    assert result["lip_synced_shots"] == []
    assert result["edit_timeline"][0]["video_file"] == state["edit_timeline"][0]["video_file"]
    assert "shot-001 kept its original clip" in result["warnings"][0]


def test_sync_cuts_the_shot_and_sends_only_the_speakers_words(tmp_path, monkeypatch):
    clip = tmp_path / "source.mp4"
    narration = tmp_path / "narration.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=5", "-pix_fmt", "yuv420p", str(clip)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=6", str(narration)], check=True)
    uploads, jobs = [], []

    class FakeFal:
        @staticmethod
        def upload_file(path):
            uploads.append(path)
            return f"https://fal.example/{Path(path).name}"

    def fake_job(model, arguments, job_file, request_fingerprint, timeout):
        jobs.append((model, arguments()))
        return {"video": {"url": "https://fal.example/out.mp4"}}

    monkeypatch.setattr(lip_sync_agent, "fal_client", FakeFal)
    monkeypatch.setattr(lip_sync_agent, "_fal_job", fake_job)
    monkeypatch.setattr(lip_sync_agent, "_save_generated_video", lambda url, path: path.write_bytes(clip.read_bytes()))
    state = {"topic": "race", "output_dir": str(tmp_path), "narration_file": str(narration), "thread_id": "t"}
    item = {"shot_id": "shot-001", "video_file": str(clip), "source_in": 0.5, "timeline_start": 1.0, "timeline_end": 4.0}
    out = tmp_path / "lip"
    out.mkdir()

    synced = lip_sync_agent._sync_shot(state, item, "character-001", [
        {"speaker": "character-001", "start": 1.2, "end": 2.5}, {"speaker": "character-002", "start": 2.8, "end": 3.6},
    ], out)

    assert synced.is_file()
    assert jobs[0][0] == "fal-ai/latentsync"
    assert jobs[0][1]["loop_mode"] == "loop"
    seconds = lambda path: float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True).stdout)
    assert seconds(out / "shot-001_clip.mp4") == pytest.approx(3.0, abs=0.1)
    assert seconds(out / "shot-001_line.wav") == pytest.approx(3.0, abs=0.05)
    # A retried edit reuses the paid sync instead of calling the model again.
    lip_sync_agent._sync_shot(state, item, "character-001", [{"speaker": "character-001", "start": 1.2, "end": 2.5}, {"speaker": "character-002", "start": 2.8, "end": 3.6}], out)
    assert len(jobs) == 1


def test_the_graph_runs_lip_sync_between_the_timeline_and_the_rough_cut():
    from video_automation.agent_graph import build_story_graph

    graph = build_story_graph(video_validator=lambda state: {})
    edges = set(graph.builder.edges)
    assert ("lip_sync", "compile_rough_video") in edges


def test_spoken_films_ask_the_story_agent_for_dialogue():
    from video_automation.prompts import story_prompt

    spoken = story_prompt(user_request={"topic": "race"}, requirements=None, previous_story=None, review_note="", feedback="", voice_mode="lipsync")
    narrated = story_prompt(user_request={"topic": "race"}, requirements=None, previous_story=None, review_note="", feedback="")
    assert "this film has no narrator" in spoken
    assert "this film has no narrator" not in narrated


def test_spoken_films_use_the_dialogue_soundtrack(monkeypatch):
    from video_automation.agents import preproduction_agents

    monkeypatch.setattr(preproduction_agents, "_dialogue_narration", lambda *_args: {"narration_segments": ["dialogue"]})
    state = {
        "voice_mode": "lipsync", "duration": "30 seconds", "language": "English",
        "story_outline": {"idea": "A race.", "structure": [{"beat_id": "beat-001", "description": 'Riley says, "Go!"'}]},
    }
    assert preproduction_agents.create_narration_script(state) == {"narration_segments": ["dialogue"]}
