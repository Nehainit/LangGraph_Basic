import json
from pathlib import Path

from video_automation.agents.subtitle_agent import create_subtitles


def test_create_subtitles(tmp_path):
    result = create_subtitles(
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
                    "narration": "Raja and Rani enter the forest.",
                },
                {
                    "scene_number": 2,
                    "start_time": "00:12",
                    "end_time": "00:24",
                    "narration": "The devil appears from the mist.",
                },
            ],
        }
    )

    subtitle_file = Path(result["subtitle_file"])
    assert subtitle_file.exists()
    assert "00:00:00,000 --> 00:00:12,000" in subtitle_file.read_text(encoding="utf-8")
    assert result["subtitles"][1]["text"] == "The devil appears from the mist."


def test_requires_storyboard():
    try:
        create_subtitles(
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
    raise AssertionError("subtitles should require storyboard")


def test_word_level_subtitles(tmp_path):
    alignment_file = tmp_path / "alignment.json"
    text = "Raja enters the forest"
    alignment_file.write_text(
        json.dumps(
            {
                "text": text,
                "alignment": {
                    "characters": list(text),
                    "character_start_times_seconds": [index * 0.1 for index in range(len(text))],
                    "character_end_times_seconds": [(index + 1) * 0.1 for index in range(len(text))],
                },
                "scene_timings": [{"scene_number": 1, "start_seconds": 0.0, "end_seconds": 2.2}],
            }
        ),
        encoding="utf-8",
    )
    result = create_subtitles(
        {
            "topic": "Raja and Rani",
            "tone": "mystical",
            "duration": "1 minute",
            "language": "English",
            "characters": "Raja, Rani",
            "output_dir": str(tmp_path),
            "storyboard": [{"scene_number": 1, "start_time": "00:00", "end_time": "00:03", "narration": text}],
            "narration_alignment_file": str(alignment_file),
        }
    )

    assert result["subtitles"][0]["text"] == "Raja enters the forest"
    assert result["subtitles"][0]["end_seconds"] == 2.2


if __name__ == "__main__":
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        test_create_subtitles(Path(tmp))
        test_word_level_subtitles(Path(tmp))
    test_requires_storyboard()


def _timed_alignment(spans):
    chars, starts, ends = [], [], []
    for text, start, end in spans:
        if chars:
            chars.append(" "); starts.append(starts[-1]); ends.append(ends[-1])
        step = (end - start) / len(text)
        for index, char in enumerate(text):
            chars.append(char); starts.append(start + index * step); ends.append(start + (index + 1) * step)
    return {"characters": chars, "character_start_times_seconds": starts, "character_end_times_seconds": ends}


def test_hindi_captions_end_with_their_sentence_and_leave_no_stray_word(tmp_path):
    import json
    from video_automation.agents.subtitle_agent import _word_subtitles

    words = [
        ("सब", 0.0, 0.3), ("सन्न", 0.35, 0.7), ("थे।", 0.75, 1.0),
        ("केला", 1.1, 1.4), ("भाई", 1.45, 1.7), ("ने", 1.75, 1.85), ("मैंगो", 1.9, 2.2), ("रानी", 2.25, 2.5), ("से", 2.55, 2.65), ("फुसफुसाया।", 2.7, 3.3),
        ("क्या", 5.0, 5.2), ("तुम", 5.25, 5.4), ("मुझे", 5.45, 5.7), ("मिस", 5.75, 5.95), ("कर", 6.0, 6.1), ("रहे", 6.15, 6.3), ("थे?", 6.35, 6.6),
    ]
    alignment_file = tmp_path / "alignment.json"
    alignment_file.write_text(json.dumps({"alignment": _timed_alignment(words)}, ensure_ascii=False), encoding="utf-8")

    captions = [item["text"] for item in _word_subtitles(str(alignment_file))]

    assert captions == [
        "सब सन्न थे।",
        # Seven words is over the six-word limit, so the sentence splits evenly instead of leaving one word over.
        "केला भाई ने मैंगो",
        "रानी से फुसफुसाया।",
        "क्या तुम मुझे मिस",
        "कर रहे थे?",
    ]
