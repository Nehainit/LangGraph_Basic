"""Lip sync for films where the characters speak: each shot that shows its speaker gets the mouth synced to the line.

Runs once the edit timeline is planned, so every shot's slice of the dialogue track is known. For each shot, the
character who speaks most in its slice is the one synced; other characters' lines in that slice are silenced in the
audio sent to the model, so only the speaker's mouth moves. A shot whose speaker is off screen keeps its clip.
The synced clip replaces the shot's clip in the timeline; a failed sync keeps the original and leaves a note.
"""

import logging
import subprocess
from pathlib import Path

from video_automation.agents.image_agent import (
    FAL_VIDEO_TIMEOUT_SECONDS, _fal_job, _out_dir, _save_generated_video, _video_is_usable, fal_client,
)
from video_automation.cost_control import file_identity, fingerprint, ledger_for, paid_call
from video_automation.prompts import PIPELINE_CONFIG
from video_automation.schema import AgentState


logger = logging.getLogger(__name__)


def _config() -> dict:
    return {
        "model": "fal-ai/latentsync", "usd_per_second": 0.005, "min_speech_seconds": 0.4,
        **(PIPELINE_CONFIG.get("lip_sync") or {}),
    }


def _spoken(state: AgentState) -> list[dict]:
    """Every spoken line with its speaker and time on the film's clock."""
    speakers = {
        segment.get("segment_id"): segment.get("speaker_id")
        for segment in state.get("narration_segments") or [] if segment.get("speaker_id")
    }
    return [
        {"speaker": speakers[timing["segment_id"]], "start": float(timing["start_seconds"]), "end": float(timing["end_seconds"])}
        for timing in state.get("narration_segment_timings") or []
        if timing.get("segment_id") in speakers
    ]


def _overlap(start: float, end: float, line: dict) -> float:
    return max(0.0, min(end, line["end"]) - max(start, line["start"]))


def shot_speaker(lines: list[dict], start: float, end: float, present: list[str] | None, minimum: float) -> str | None:
    """The character to sync in a shot: the one who speaks most inside it, when they are on screen."""
    seconds: dict[str, float] = {}
    for line in lines:
        seconds[line["speaker"]] = seconds.get(line["speaker"], 0.0) + _overlap(start, end, line)
    speaker = max(seconds, key=seconds.get, default=None)
    if not speaker or seconds[speaker] < minimum:
        return None
    if present and speaker not in present:
        return None
    return speaker


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, timeout=180)


def _sync_shot(state: AgentState, item: dict, speaker: str, lines: list[dict], out: Path) -> Path:
    config = _config()
    shot_id = item["shot_id"]
    start, end = float(item["timeline_start"]), float(item["timeline_end"])
    duration = round(end - start, 3)
    clip, audio, synced = out / f"{shot_id}_clip.mp4", out / f"{shot_id}_line.wav", out / f"{shot_id}_lipsync.mp4"
    source_in = float(item.get("source_in") or 0)
    # Only this speaker's words reach the model, so a listener in the same shot keeps a closed mouth.
    others = [
        (round(max(0.0, line["start"] - start), 3), round(min(duration, line["end"] - start), 3))
        for line in lines if line["speaker"] != speaker and _overlap(start, end, line) > 0
    ]
    # Keyed on the sources, not the cut files, so a retried edit finds the sync it already paid for.
    request_fingerprint = fingerprint(
        config["model"], file_identity(item["video_file"]), source_in, start, duration, speaker, others,
        file_identity(state["narration_file"]),
    )
    marker = synced.with_suffix(".fingerprint")
    if synced.is_file() and marker.is_file() and marker.read_text() == request_fingerprint:
        return synced
    _run([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{source_in:.3f}", "-t", f"{duration:.3f}",
        "-i", str(item["video_file"]), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
    ])
    mute = ",volume=0:enable='" + "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in others) + "'" if others else ""
    _run([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(state["narration_file"]),
        "-af", f"apad=whole_dur={duration:.3f}{mute}", "-ac", "1", "-ar", "16000", str(audio),
    ])
    if fal_client is None:
        raise RuntimeError("Install fal-client to use lip sync.")

    def arguments() -> dict:
        return {"video_url": fal_client.upload_file(str(clip)), "audio_url": fal_client.upload_file(str(audio)), "loop_mode": "loop"}

    with paid_call(
        ledger_for(state), f"lip_sync:{synced}", "lip_sync", shot_id, round(duration * float(config["usd_per_second"]), 4),
        shot_id=shot_id, model=config["model"], billed_seconds=duration, speaker=speaker,
    ):
        response = _fal_job(config["model"], arguments, out / f"{shot_id}_lipsync.job.json", request_fingerprint, FAL_VIDEO_TIMEOUT_SECONDS)
    url = (response.get("video") or {}).get("url") if isinstance(response, dict) else None
    if not url:
        raise RuntimeError("the lip sync model returned no video")
    _save_generated_video(url, synced)
    if not _video_is_usable(synced):
        raise RuntimeError("the lip sync model returned an unreadable video")
    marker.write_text(request_fingerprint)
    return synced


def lip_sync_shots(state: AgentState) -> dict:
    """Sync each speaking shot's mouth to its line; a no-op unless the film's voice is "Characters speak"."""
    if state.get("voice_mode") != "lipsync" or not state.get("edit_timeline"):
        return {}
    config = _config()
    lines = _spoken(state)
    warnings = list(state.get("warnings", []))
    if not lines:
        note = "Lip sync was skipped: the soundtrack has no character lines, so a narrator tells this film."
        return {"warnings": [*warnings, note] if note not in warnings else warnings}
    shots = {shot.get("shot_id"): shot for shot in state.get("shot_plan") or state.get("storyboard") or []}
    out = _out_dir(state, "lip_sync")
    out.mkdir(parents=True, exist_ok=True)
    timeline = [dict(item) for item in state["edit_timeline"]]
    clips = list(state.get("scene_video_files") or [])
    synced_shots = []
    for index, item in enumerate(timeline):
        present = (shots.get(item.get("shot_id")) or {}).get("characters_present")
        speaker = shot_speaker(lines, float(item["timeline_start"]), float(item["timeline_end"]), present, float(config["min_speech_seconds"]))
        if not speaker or not item.get("video_file") or not Path(item["video_file"]).is_file():
            continue
        try:
            synced = _sync_shot(state, item, speaker, lines, out)
        except Exception as exc:  # A failed sync keeps the shot's own clip; the film still finishes.
            logger.warning("Lip sync failed for %s: %s", item["shot_id"], exc)
            warnings.append(f"{item['shot_id']} kept its original clip: lip sync failed ({exc}).")
            continue
        duration = round(float(item["timeline_end"]) - float(item["timeline_start"]), 3)
        item.update(video_file=str(synced), source_in=0.0, source_out=duration)
        if index < len(clips):
            clips[index] = str(synced)
        synced_shots.append(item["shot_id"])
    result = {"edit_timeline": timeline, "lip_synced_shots": synced_shots, "cost_ledger": ledger_for(state).snapshot(), "warnings": warnings}
    if clips:
        result["scene_video_files"] = clips
    return result
