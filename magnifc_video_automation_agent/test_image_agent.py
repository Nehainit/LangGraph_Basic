import base64
from pathlib import Path

import image_agent


PNG_1X1 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")


def test_create_scene_images(tmp_path):
    original_request = image_agent._json_request
    original_headers = image_agent._magnific_headers
    original_sleep = image_agent.time.sleep
    calls = []

    def fake_request(url, payload, headers):
        calls.append(payload["prompt"])
        return {"data": [{"base64": PNG_1X1}]}

    image_agent._json_request = fake_request
    image_agent._magnific_headers = lambda: {}
    image_agent.time.sleep = lambda seconds: None
    try:
        result = image_agent.create_scene_images(
            {
                "topic": "Raja and Rani",
                "tone": "mystical",
                "duration": "1 minute",
                "language": "English",
                "characters": "Raja, Rani",
                "output_dir": str(tmp_path),
                "storyboard": [
                    {
                        "scene_number": 1,
                        "start_time": "00:00",
                        "end_time": "00:12",
                        "visuals": "Raja and Rani in a glowing forest.",
                        "camera": "wide shot",
                        "narration": "They enter the forest.",
                    }
                ],
            }
        )
    finally:
        image_agent._json_request = original_request
        image_agent._magnific_headers = original_headers
        image_agent.time.sleep = original_sleep

    image_file = Path(result["image_files"][0])
    reference_files = [Path(path) for path in result["character_reference_files"]]
    assert len(reference_files) == 1
    assert all(path.exists() for path in reference_files)
    assert reference_files[0].name == "characters_reference.png"
    assert image_file.exists()
    assert image_file.name == "scene_01.png"
    assert "Clean combined character reference sheet for: Raja, Rani." in calls[0]
    assert "Raja and Rani in a glowing forest." in calls[1]


def test_requires_storyboard():
    try:
        image_agent.create_scene_images(
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
    raise AssertionError("image agent should require storyboard")


if __name__ == "__main__":
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        test_create_scene_images(Path(tmp))
    test_requires_storyboard()
