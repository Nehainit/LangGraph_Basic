import re
import subprocess
from pathlib import Path

from schema import AgentState


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "video"


def _out_dir(state: AgentState, *parts: str) -> Path:
    return Path(state.get("output_dir", "outputs")) / _slug(state["topic"]) / Path(*parts)


def _seconds(value: str) -> float:
    parts = [float(part) for part in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0.0


def _scene_duration(scene: dict) -> float:
    return max(0.1, _seconds(str(scene["end_time"])) - _seconds(str(scene["start_time"])))


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr)


def compile_video(state: AgentState) -> dict[str, str]:
    for key in ("storyboard", "image_files", "narration_file", "sfx_files", "subtitle_file"):
        if not state.get(key):
            raise RuntimeError(f"Video agent needs {key}.")

    out = _out_dir(state, "video")
    out.mkdir(parents=True, exist_ok=True)
    concat_file = out / "images.txt"
    mixed_audio_file = out / "mixed_audio.mp3"
    video_file = out / "final_reel.mp4"

    durations = [_scene_duration(scene) for scene in state["storyboard"]]
    total_seconds = sum(durations)
    lines = []
    for image_file, duration in zip(state["image_files"], durations):
        lines.extend([f"file '{Path(image_file).resolve()}'", f"duration {duration}"])
    lines.append(f"file '{Path(state['image_files'][-1]).resolve()}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    mix_inputs = ["-i", state["narration_file"]]
    filter_parts = []
    labels = ["[0:a]"]
    start_ms = 0
    for index, (sfx_file, duration) in enumerate(zip(state["sfx_files"], durations), start=1):
        mix_inputs.extend(["-i", sfx_file])
        filter_parts.append(f"[{index}:a]adelay={start_ms}|{start_ms},volume=0.35[sfx{index}]")
        labels.append(f"[sfx{index}]")
        start_ms += int(duration * 1000)
    filter_parts.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=0[a]")

    _run_ffmpeg(
        ["ffmpeg", "-y", *mix_inputs, "-filter_complex", ";".join(filter_parts), "-map", "[a]", str(mixed_audio_file)]
    )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-i",
            str(mixed_audio_file),
            "-i",
            str(state["subtitle_file"]),
            "-t",
            str(total_seconds),
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:s:0",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-c:s",
            "mov_text",
            str(video_file),
        ]
    )
    return {"mixed_audio_file": str(mixed_audio_file), "video_file": str(video_file)}
