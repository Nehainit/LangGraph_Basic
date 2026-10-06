from pathlib import Path

from video_automation.agents import sound_bed_agent


def bed_state(tmp_path):
    return {
        "topic": "Sher and Chuha", "output_dir": str(tmp_path), "actual_narration_seconds": 9.5,
        "parsed_requirements": {"tone": "warm", "visual_style": "storybook watercolor"},
        "story_outline": {"idea": "A lion spares a mouse."},
        "scenes": [
            {"scene_id": "scene-001", "location_id": "location-001", "location_description": "jungle clearing",
             "start_sec": 0.0, "end_sec": 4.0, "emotion": "tense"},
            {"scene_id": "scene-002", "location_id": "location-001", "start_sec": 4.0, "end_sec": 9.5,
             "emotion": "grateful"},
        ],
    }


def test_sound_bed_renders_scene_ambience_and_timed_music(monkeypatch, tmp_path):
    requests = []

    def fake_request(url, payload, _headers):
        requests.append((url, payload))
        return b"audio"

    monkeypatch.setattr(sound_bed_agent, "_bytes_request", fake_request)
    monkeypatch.setattr(sound_bed_agent, "_elevenlabs_headers", lambda: {})

    result = sound_bed_agent.create_sound_bed(bed_state(tmp_path))
    again = sound_bed_agent.create_sound_bed(bed_state(tmp_path))

    assert [(track["start_seconds"], track["duration_seconds"]) for track in result["ambience_tracks"]] == [(0.0, 4.0), (4.0, 5.5)]
    assert all(Path(track["file"]).is_file() for track in result["ambience_tracks"])
    music = next(payload for url, payload in requests if "/v1/music" in url)
    assert music["music_length_ms"] == 10500 and music["force_instrumental"] is True
    assert "storybook watercolor" in music["prompt"]
    assert "jungle clearing" in next(payload["text"] for url, payload in requests if "sound-generation" in url)
    assert len(requests) == 3 and again == result  # unchanged requests reuse the rendered files


def test_failed_sound_track_is_reported_without_failing_the_cut(monkeypatch, tmp_path):
    def fake_request(url, payload, _headers):
        if "/v1/music" in url:
            raise RuntimeError("music unavailable")
        return b"audio"

    monkeypatch.setattr(sound_bed_agent, "_bytes_request", fake_request)
    monkeypatch.setattr(sound_bed_agent, "_elevenlabs_headers", lambda: {})

    result = sound_bed_agent.create_sound_bed({**bed_state(tmp_path), "warnings": []})

    assert result["music_file"] is None and len(result["ambience_tracks"]) == 2
    assert result["warnings"] == ["Sound bed track music_bed.mp3 was skipped: music unavailable"]
