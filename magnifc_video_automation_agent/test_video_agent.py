import subprocess
from pathlib import Path

from subtitle_agent import create_subtitles
from video_agent import compile_video


def run(command):
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_compile_video(tmp_path):
    image_file = tmp_path / "scene_01.png"
    narration_file = tmp_path / "narration.mp3"
    sfx_file = tmp_path / "sfx_scene_01.mp3"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=1", "-frames:v", "1", str(image_file)])
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1", str(narration_file)])
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1", str(sfx_file)])

    state = {
        "topic": "Raja and Rani",
        "tone": "mystical",
        "duration": "1 second",
        "language": "English",
        "characters": "Raja, Rani",
        "output_dir": str(tmp_path),
        "storyboard": [
            {
                "scene_number": 1,
                "start_time": "00:00",
                "end_time": "00:01",
                "narration": "Raja and Rani enter.",
            }
        ],
        "image_files": [str(image_file)],
        "narration_file": str(narration_file),
        "sfx_files": [str(sfx_file)],
    }
    state.update(create_subtitles(state))

    result = compile_video(state)
    assert Path(result["mixed_audio_file"]).exists()
    assert Path(result["video_file"]).exists()


def test_requires_assets():
    try:
        compile_video(
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
    raise AssertionError("video compile should require generated assets")


if __name__ == "__main__":
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        test_compile_video(Path(tmp))
    test_requires_assets()
