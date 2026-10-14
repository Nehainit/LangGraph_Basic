import base64
import os
from pathlib import Path

from video_automation.agents import narration_agent


def alignment_for(text):
    chars = list(text)
    starts = [index * 0.1 for index in range(len(chars))]
    ends = [(index + 1) * 0.1 for index in range(len(chars))]
    return {
        "characters": chars,
        "character_start_times_seconds": starts,
        "character_end_times_seconds": ends,
    }


def test_create_narration(tmp_path):
    original_request = narration_agent._json_request
    original_headers = narration_agent._elevenlabs_headers
    original_model = os.environ.pop("ELEVENLABS_TTS_MODEL", None)
    calls = []

    def fake_request(url, payload, headers):
        calls.append((url, payload))
        return {"audio_base64": base64.b64encode(b"fake mp3").decode("ascii"), "alignment": alignment_for(payload["text"])}

    narration_agent._json_request = fake_request
    narration_agent._elevenlabs_headers = lambda: {}
    try:
        result = narration_agent.create_narration(
            {
                "topic": "Raja and Rani",
                "tone": "mystical",
                "duration": "1 minute",
                "language": "English",
                "characters": "Raja, Rani",
                "output_dir": str(tmp_path),
                "story": "Fallback story.",
                    "narration_segments": [
                        {"segment_id": "segment-001", "parent_beat_ids": ["beat-001"], "text": "Raja enters the forest."},
                        {"segment_id": "segment-002", "parent_beat_ids": ["beat-002"], "text": "Rani follows the golden light."},
                    ],
                    "storyboard": [
                        {"shot_id": "shot-001", "scene_number": 1, "start_time": "00:00", "end_time": "00:30", "narration": "Raja enters the forest."},
                        {"shot_id": "shot-002", "scene_number": 2, "start_time": "00:30", "end_time": "01:00", "narration": "Rani follows the golden light."},
                    ],
                    "director_plan": [{"shot_id": "shot-001"}, {"shot_id": "shot-002"}],
            }
        )
    finally:
        narration_agent._json_request = original_request
        narration_agent._elevenlabs_headers = original_headers
        if original_model is not None:
            os.environ["ELEVENLABS_TTS_MODEL"] = original_model

    narration_file = Path(result["narration_file"])
    assert narration_file.exists()
    assert narration_file.read_bytes() == b"fake mp3"
    assert Path(result["narration_alignment_file"]).exists()
    assert calls[0][0] == (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        "JBFqnCBsd6RMkjVDRZzb/with-timestamps?output_format=mp3_44100_128"
    )
    assert calls[0][1]["text"] == "Raja enters the forest.\nRani follows the golden light."
    assert calls[0][1]["model_id"] == "eleven_flash_v2_5"
    assert result["actual_narration_seconds"] > 0
    assert result["narration_segment_timings"] == result["scene_timings"]
    assert result["scene_timings"][1]["segment_id"] == "segment-002"
    assert round(result["scene_timings"][1]["start_seconds"], 1) == 2.4


def test_narration_model_selection_invalidates_cached_audio(monkeypatch, tmp_path):
    calls = []

    def generate(_url, payload, _headers):
        calls.append(payload["model_id"])
        return {"audio_base64": base64.b64encode(b"audio").decode(), "alignment": alignment_for(payload["text"])}

    monkeypatch.setattr(narration_agent, "_json_request", generate)
    monkeypatch.setattr(narration_agent, "_elevenlabs_headers", lambda: {})
    state = {"topic": "A story", "duration": "30 seconds", "output_dir": str(tmp_path), "story": "Hello."}
    first = narration_agent.create_narration({**state, "narration_model": "eleven_flash_v2_5"})
    second = narration_agent.create_narration({**state, "narration_model": "eleven_multilingual_v2"})

    assert calls == ["eleven_flash_v2_5", "eleven_multilingual_v2"]
    assert first["narration_model_used"] == "eleven_flash_v2_5"
    assert second["narration_provider"] == "ElevenLabs"
    assert second["narration_model_used"] == "eleven_multilingual_v2"


def test_long_voiceover_requests_narration_revision(tmp_path):
    original_request = narration_agent._json_request
    original_headers = narration_agent._elevenlabs_headers

    def fake_request(_url, payload, _headers):
        alignment = alignment_for(payload["text"])
        alignment["character_end_times_seconds"][-1] = 12
        return {"audio_base64": base64.b64encode(b"fake mp3").decode("ascii"), "alignment": alignment}

    narration_agent._json_request = fake_request
    narration_agent._elevenlabs_headers = lambda: {}
    try:
        result = narration_agent.create_narration(
            {
                "topic": "short",
                "duration": "10 seconds",
                "output_dir": str(tmp_path),
                "storyboard": [{"shot_id": "shot-001", "scene_number": 1, "start_time": "00:00", "end_time": "00:10", "narration": "A complete short story."}],
            }
        )
    finally:
        narration_agent._json_request = original_request
        narration_agent._elevenlabs_headers = original_headers
    assert "12.0s" in result["narration_feedback"]
    assert result["narration_timing_attempts"] == 1
    assert result["narration_target_words"] > 0


def test_hindi_language_is_sent_to_elevenlabs(monkeypatch, tmp_path):
    calls = []

    def fake_request(url, payload, _headers):
        calls.append((url, payload))
        return {"audio_base64": base64.b64encode(b"audio").decode(), "alignment": alignment_for(payload["text"])}

    monkeypatch.setattr(narration_agent, "_json_request", fake_request)
    monkeypatch.setattr(narration_agent, "_elevenlabs_headers", lambda: {})
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "hindi-voice")
    state = {
        "topic": "Raja aur cuha", "duration": "10 seconds", "language": "Hindi",
        "parsed_requirements": {"language": "English"},
        "output_dir": str(tmp_path), "narration_script": "राजा और चूहा मिले।",
    }
    narration_agent.create_narration(state)
    assert calls[0][1]["language_code"] == "hi"
    assert calls[0][1]["text"] == "राजा और चूहा मिले।"
    assert "/hindi-voice/with-timestamps" in calls[0][0]


def test_overlong_narration_uses_complete_audio_after_three_tts_attempts(monkeypatch, tmp_path):
    def fake_request(_url, payload, _headers):
        alignment = alignment_for(payload["text"])
        alignment["character_end_times_seconds"][-1] = 12
        return {"audio_base64": base64.b64encode(b"fake mp3").decode("ascii"), "alignment": alignment}

    monkeypatch.setattr(narration_agent, "_json_request", fake_request)
    monkeypatch.setattr(narration_agent, "_elevenlabs_headers", lambda: {})
    result = narration_agent.create_narration({
        "topic": "short", "duration": "10 seconds", "output_dir": str(tmp_path),
        "narration_script": "A complete short story.", "narration_timing_attempts": 2,
    })

    assert result["actual_narration_seconds"] == 12
    assert result["narration_feedback"] == ""
    assert result["narration_timing_attempts"] == 3
    assert result["narration_target_words"] == 0
    assert "12.0s" in result["warnings"][-1]
    assert "10s" in result["warnings"][-1]
    assert Path(result["narration_file"]).is_file()


def test_requires_story_or_storyboard():
    try:
        narration_agent.create_narration(
            {
                "topic": "x",
                "tone": "y",
                "duration": "1 minute",
                "language": "English",
                "characters": "Raja",
            }
        )
    except RuntimeError:
        return
    raise AssertionError("narration should require story or storyboard")


if __name__ == "__main__":
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        test_create_narration(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_long_voiceover_requests_narration_revision(Path(tmp))
    test_requires_story_or_storyboard()


def _tone_mp3(path: Path, seconds: float) -> bytes:
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "libmp3lame", str(path)],
        check=True,
    )
    return path.read_bytes()


def test_dialogue_lines_get_character_voices_and_timed_pauses(tmp_path, monkeypatch):
    clip = _tone_mp3(tmp_path / "tone.mp3", 1.0)
    calls = []

    def fake_request(url, payload, headers):
        calls.append(url.split("/text-to-speech/")[1].split("/")[0])
        return {"audio_base64": base64.b64encode(clip).decode("ascii"), "alignment": alignment_for(payload["text"][:8])}

    monkeypatch.setattr(narration_agent, "_json_request", fake_request)
    monkeypatch.setattr(narration_agent, "_elevenlabs_headers", lambda: {})
    voices = narration_agent.PIPELINE_CONFIG["narration"]["character_voices"]
    segments = [
        {"segment_id": "segment-001", "parent_beat_ids": ["beat-001"], "text": "क्या तुमने देखा?", "speaker_id": "kela", "voice_gender": "male"},
        {"segment_id": "segment-002", "parent_beat_ids": ["beat-002"], "text": "कौन थी वह?", "speaker_id": "mango", "voice_gender": "female"},
        {"segment_id": "segment-003", "parent_beat_ids": ["beat-002"], "text": "मैं समझा सकता हूँ...", "speaker_id": "strawberry", "voice_gender": "male"},
        {"segment_id": "segment-004", "parent_beat_ids": ["beat-003"], "text": "हाय स्ट्रॉबेरी...", "speaker_id": "pineapple", "voice_gender": "female"},
    ]
    state = {"topic": "Fruit drama", "duration": "12 seconds", "language": "Hindi", "output_dir": str(tmp_path), "narration_segments": segments}

    result = narration_agent.create_narration(state)

    # Each speaker has their own gendered voice, and the two women and the two men never share one.
    assert calls == [voices["male"][0], voices["female"][0], voices["male"][1], voices["female"][1]]
    timings = result["narration_segment_timings"]
    assert [timing["segment_id"] for timing in timings] == ["segment-001", "segment-002", "segment-003", "segment-004"]
    gaps = [later["start_seconds"] - earlier["speech_end_seconds"] for earlier, later in zip(timings, timings[1:])]
    # Scenes cover the whole soundtrack: each line runs until the next one starts, the last one to the end.
    assert all(earlier["end_seconds"] == later["start_seconds"] for earlier, later in zip(timings, timings[1:]))
    assert timings[0]["start_seconds"] == 0.0 and timings[-1]["end_seconds"] == result["actual_narration_seconds"]
    # Lines of one beat sit close together; the picture gets longer pauses between beats.
    assert gaps[1] < gaps[0] and gaps[1] < gaps[2]
    # Four 1 s lines are padded toward the 12 s request, ending on a hold rather than running past it.
    assert 9.0 < result["actual_narration_seconds"] <= 12.6
    assert result["narration_feedback"] == "" and result["narration_target_words"] == 0
    assert Path(result["narration_alignment_file"]).exists()

    calls.clear()
    narration_agent.create_narration(state)
    assert calls == []  # every line is cached by its exact request


def test_unusable_premade_voice_falls_back_to_the_default_voice(tmp_path, monkeypatch):
    clip = _tone_mp3(tmp_path / "tone.mp3", 0.5)
    calls = []

    def fake_request(url, payload, headers):
        voice = url.split("/text-to-speech/")[1].split("/")[0]
        calls.append(voice)
        if voice != narration_agent.DEFAULT_ELEVENLABS_VOICE_ID:
            raise RuntimeError(f"POST {url} failed: HTTP 404 voice_not_found")
        return {"audio_base64": base64.b64encode(clip).decode("ascii"), "alignment": alignment_for(payload["text"])}

    monkeypatch.setattr(narration_agent, "_json_request", fake_request)
    monkeypatch.setattr(narration_agent, "_elevenlabs_headers", lambda: {})
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    result = narration_agent.create_narration({
        "topic": "Fruit drama", "duration": "5 seconds", "language": "English", "output_dir": str(tmp_path),
        "narration_segments": [
            {"segment_id": "segment-001", "parent_beat_ids": ["beat-001"], "text": "Who was she?", "speaker_id": "mango", "voice_gender": "female"},
        ],
    })

    assert calls[-1] == narration_agent.DEFAULT_ELEVENLABS_VOICE_ID
    assert Path(result["narration_file"]).exists()
