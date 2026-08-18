from pathlib import Path

import narration_agent


def test_create_narration(tmp_path):
    original_request = narration_agent._bytes_request
    original_headers = narration_agent._elevenlabs_headers
    calls = []

    def fake_request(url, payload, headers):
        calls.append(payload)
        return b"fake mp3"

    narration_agent._bytes_request = fake_request
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
                "storyboard": [
                    {"scene_number": 1, "narration": "Raja enters the forest."},
                    {"scene_number": 2, "narration": "Rani follows the golden light."},
                ],
            }
        )
    finally:
        narration_agent._bytes_request = original_request
        narration_agent._elevenlabs_headers = original_headers

    narration_file = Path(result["narration_file"])
    assert narration_file.exists()
    assert narration_file.read_bytes() == b"fake mp3"
    assert calls[0]["text"] == "Raja enters the forest.\nRani follows the golden light."


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
    test_requires_story_or_storyboard()
