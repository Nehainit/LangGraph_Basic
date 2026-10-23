"""The Edits tab: music, sound effects, text, caption styles, filters, trim & speed, and voiceover for finished films.

The editor previews everything live in the browser from the film's separate parts (picture, narration, captions);
nothing is rendered until Export. Export re-renders over the film's existing picture track and narration
(editor_agent.remix_video), so it is FFmpeg only: nothing is regenerated and nothing is paid for.

Per film, an edits/ folder beside the final video holds the autosaved draft, recorded voiceovers, and every
exported version with the settings that made it. Per user, library/<user>/films.json lists their finished films,
so the Edits tab shows them on any device.
"""

import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from video_automation.agents.editor_agent import _resolution, _scene_timings, remix_video
from video_automation.agents.subtitle_agent import _words_from_alignment
from video_automation.character_library import _user_dir
from video_automation.edit_overlays import caption_overlays, caption_words, text_overlays
from video_automation.prompts import PIPELINE_CONFIG
from video_automation.sound_library import library_folder, load_library


# FFmpeg filters for the looks; None keeps the pipeline's own grade (finishing.color_grade).
FILTERS = {
    "original": None,
    "warm": "colorbalance=rs=0.08:gs=0.02:bs=-0.08,eq=saturation=1.2",
    "cool": "colorbalance=rs=-0.06:gs=0.0:bs=0.08,eq=brightness=0.02",
    "vivid": "eq=contrast=1.08:saturation=1.5",
    "mono": "hue=s=0,eq=contrast=1.1",
    "vintage": "curves=preset=vintage,eq=contrast=0.92:brightness=0.03",
    "fade": "eq=contrast=0.85:brightness=0.06:saturation=0.8",
    "dusk": "colorbalance=rs=0.1:gs=-0.02:bs=-0.06,eq=saturation=1.3:brightness=-0.03",
    "teal": "colorbalance=rs=0.08:bs=-0.08:rh=-0.08:bh=0.1,eq=saturation=1.25:contrast=1.1",
}
SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
MAX_CUES = 40
MAX_TEXTS = 20
MAX_VOICEOVERS = 10
MAX_VOICEOVER_BYTES = 25 * 1024 * 1024
# Renders are CPU-heavy; two at a time keeps the server responsive for everyone else.
_renders = threading.BoundedSemaphore(2)
_lock = threading.Lock()


class SoundCue(BaseModel):
    sound_id: str = Field(max_length=120)
    start_seconds: float = Field(ge=0, le=600)
    volume: float = Field(default=0.6, ge=0, le=1.5)


class TextItem(BaseModel):
    id: str = Field(max_length=40)
    text: str = Field(max_length=120)
    style: Literal["highlight", "bold", "serif", "outline", "typewriter", "label"] = "highlight"
    color: str = Field(default="#ffd23f", pattern=r"^#[0-9a-fA-F]{6}$")
    # Centre of the text as a fraction of the frame, and its size relative to the default.
    x: float = Field(default=0.5, ge=0, le=1)
    y: float = Field(default=0.15, ge=0, le=1)
    size: float = Field(default=1.0, ge=0.4, le=3)
    start_seconds: float = Field(ge=0, le=600)
    end_seconds: float = Field(gt=0, le=600)


class Voiceover(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{12}$")
    start_seconds: float = Field(ge=0, le=600)
    seconds: float = Field(gt=0, le=600)


class FilmEdits(BaseModel):
    # None plays the film without music.
    music_id: str | None = Field(default=None, max_length=120)
    music_volume: float = Field(default=float(PIPELINE_CONFIG["sound"]["music_volume"]), ge=0, le=1)
    narration_volume: float = Field(default=float(PIPELINE_CONFIG["sound"]["narration_volume"]), ge=0, le=2)
    duck_music: bool = True
    fade_music: bool = True
    sound_cues: list[SoundCue] = Field(default_factory=list, max_length=MAX_CUES)
    texts: list[TextItem] = Field(default_factory=list, max_length=MAX_TEXTS)
    caption_style: Literal["pop", "karaoke", "boxed", "minimal", "serif", "off"] = "boxed"
    caption_position: Literal["top", "middle", "bottom"] = "bottom"
    filter: Literal["original", "warm", "cool", "vivid", "mono", "vintage", "fade", "dusk", "teal"] = "original"
    filter_strength: float = Field(default=1.0, ge=0, le=1)
    speed: float = 1.0
    trim_start: float = Field(default=0, ge=0, le=600)
    trim_end: float | None = Field(default=None, gt=0, le=600)
    voiceovers: list[Voiceover] = Field(default_factory=list, max_length=MAX_VOICEOVERS)
    mute_narration_under_voice: bool = True
    clean_voice: bool = True


# ---------- Sounds ----------
def _sounds() -> dict[str, dict]:
    return {entry["id"]: entry for entry in load_library()}


def display_name(name: str) -> str:
    """A library sound's Freesound upload name as people should read it: "piano_cord.wav" → "Piano cord"."""
    stem = re.sub(r"\.(wav|mp3|aiff?|flac|ogg|m4a)$", "", name.strip(), flags=re.IGNORECASE)
    words = re.sub(r"[_\-]+", " ", stem)
    words = re.sub(r"\s+", " ", words).strip()
    return words[:1].upper() + words[1:] if words else name


def sound_catalog() -> dict:
    # Effects are named by what they are ("Crowd gasp 2"); Freesound upload names are often recorder jargon.
    seen: dict[str, int] = {}
    entries = []
    for entry in load_library():
        if entry["kind"] == "sfx":
            seen[entry["tag"]] = seen.get(entry["tag"], 0) + 1
            label = entry["tag"].replace("_", " ").capitalize()
            name = label if seen[entry["tag"]] == 1 else f"{label} {seen[entry['tag']]}"
        else:
            name = display_name(entry["name"])
        entries.append({key: entry[key] for key in ("id", "kind", "tag", "seconds")} | {"name": name, "url": f"/api/sounds/{entry['id']}"})
    return {
        "music": [entry for entry in entries if entry["kind"] == "music"],
        "sfx": [entry for entry in entries if entry["kind"] == "sfx"],
        "filters": list(FILTERS),
    }


def sound_file(sound_id: str) -> Path:
    entry = _sounds().get(sound_id)
    path = (library_folder() / entry["file"]).resolve() if entry else None
    if not path or not path.is_file() or library_folder().resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="Sound not found.")
    return path


# ---------- Films ----------
def _films_file(user_id: str | None) -> Path:
    return _user_dir(user_id) / "films.json"


def remember_films(user_id: str | None, thread_ids: list[str]) -> list[str]:
    """Add finished films to the user's list, newest first; returns the whole list."""
    with _lock:
        path = _films_file(user_id)
        known = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        added = [thread_id for thread_id in dict.fromkeys(thread_ids) if thread_id not in known]
        if added:
            known = [*added, *known]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(known, indent=2), encoding="utf-8")
    return known


def _edits_dir(values: dict) -> Path:
    # Beside the film's own final video, wherever the editor put it.
    if not values.get("video_file"):
        raise HTTPException(status_code=409, detail="Only a finished film can be edited.")
    return Path(values["video_file"]).parent / "edits"


def _versions(values: dict) -> list[Path]:
    """Every claimed version, including one still rendering."""
    folder = _edits_dir(values)
    return sorted(folder.glob("edit_v*.json")) if folder.is_dir() else []


def _finished_versions(values: dict) -> list[dict]:
    saved = [json.loads(path.read_text(encoding="utf-8")) for path in _versions(values)]
    return [item for item in saved if item.get("edits") and Path(item["video_file"]).is_file()]


def _draft(values: dict) -> dict | None:
    path = _edits_dir(values) / "draft.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def film_seconds(values: dict) -> float:
    """The film's length, read the way the editor times it: the edit timeline first, then scene timings."""
    ends = [float(item.get("timeline_end") or 0) for item in values.get("edit_timeline") or []] or [
        float(item.get("end_seconds") or 0) for item in values.get("scene_timings") or []
    ]
    return round(max(ends) if ends else float(values.get("actual_narration_seconds") or 0), 2)


def _picture_track(values: dict) -> Path | None:
    from video_automation.agents.editor_agent import _approved_rough_cut, _out_dir

    rough_cut = _approved_rough_cut(values)
    if rough_cut and rough_cut.is_file():
        return rough_cut
    if not values.get("topic"):
        return None
    track = _out_dir(values, "video") / "picture_track.mp4"
    return track if track.is_file() else None


def editable(values: dict) -> bool:
    return bool(
        values.get("video_file") and Path(values["video_file"]).is_file()
        and values.get("narration_file") and Path(values["narration_file"]).is_file()
        and _picture_track(values)
    )


def _title(values: dict) -> str:
    topic = " ".join(str(values.get("topic") or "Untitled film").split())
    return topic if len(topic) <= 60 else topic[:57].rsplit(" ", 1)[0] + "…"


def _latest_edits(values: dict) -> tuple[dict | None, dict | None, list[dict]]:
    draft = _draft(values)
    versions = _finished_versions(values)
    return (draft["edits"] if draft else versions[-1]["edits"] if versions else None), draft, versions


def film_card(values: dict, thread_id: str, url) -> dict:
    """What the Edits tab shows for a film; `url` turns a file path into the browser's URL for it."""
    latest, draft, versions = _latest_edits(values)
    added = []
    if latest:
        for key, label in (("music_id", "Music"), ("sound_cues", "Sound FX"), ("texts", "Text"), ("voiceovers", "Voiceover")):
            if latest.get(key):
                added.append(label)
    images = [path for path in values.get("image_files") or [] if path]
    edited_at = (draft or {}).get("saved_at") or (Path(versions[-1]["video_file"]).stat().st_mtime if versions else None)
    return {
        "thread_id": thread_id,
        "title": _title(values),
        "seconds": film_seconds(values),
        "aspect_ratio": values.get("aspect_ratio", "9:16"),
        "poster": url(images[0]) if images else None,
        "finished_at": Path(values["video_file"]).stat().st_mtime,
        "versions": len(versions),
        "edited_at": edited_at,
        "added": added,
        "sound_cues": len((latest or {}).get("sound_cues") or []),
    }


def _aligned_words(values: dict) -> list[dict]:
    path = values.get("narration_alignment_file")
    if not path or not Path(path).is_file():
        return []
    alignment = json.loads(Path(path).read_text(encoding="utf-8"))
    return _words_from_alignment(alignment.get("alignment") or alignment)


def editor_payload(values: dict, url) -> dict:
    """Everything the editor needs to preview the film live: its parts, timing, and the edit to open with."""
    if not editable(values):
        raise HTTPException(status_code=409, detail="This film's parts are no longer on the server, so it can't be edited.")
    latest, draft, versions = _latest_edits(values)
    edits = latest or FilmEdits(
        music_id=values.get("music_id") if values.get("music_file") else None,
        sound_cues=[
            SoundCue(sound_id=cue["sfx_id"], start_seconds=cue["start_seconds"], volume=cue.get("volume", 0.6))
            for cue in values.get("sound_cues") or [] if cue.get("sfx_id") in _sounds()
        ],
    ).model_dump()
    aligned = _aligned_words(values)
    timings = _scene_timings(values)
    images = values.get("image_files") or []
    width, height = _resolution(values)
    folder = _edits_dir(values)
    return {
        "edits": edits,
        "version": len(versions),
        "saved_at": (draft or {}).get("saved_at"),
        "video_url": url(versions[-1]["video_file"] if versions else values["video_file"]),
        "film": {
            "title": _title(values),
            "seconds": film_seconds(values),
            "aspect_ratio": values.get("aspect_ratio", "9:16"),
            "width": width,
            "height": height,
            "picture_url": url(str(_picture_track(values))),
            "narration_url": url(values["narration_file"]),
            "shots": [
                {
                    "image_url": url(images[index]) if index < len(images) else None,
                    "start": float(timing["start_seconds"]), "end": float(timing["end_seconds"]),
                }
                for index, timing in enumerate(timings)
            ],
            "captions": [
                {"text": item["text"], "start": float(item["start_seconds"]), "end": float(item["end_seconds"]), "words": caption_words(item, aligned)}
                for item in values.get("subtitles") or []
            ],
            "voiceover_urls": {
                path.stem.removeprefix("voice_"): url(str(path)) for path in sorted(folder.glob("voice_*")) if path.is_file()
            } if folder.is_dir() else {},
        },
    }


def _validate(values: dict, edits: FilmEdits) -> float:
    sounds = _sounds()
    seconds = film_seconds(values)
    if edits.music_id and sounds.get(edits.music_id, {}).get("kind") != "music":
        raise HTTPException(status_code=400, detail="Unknown music track.")
    if any(sounds.get(cue.sound_id, {}).get("kind") != "sfx" for cue in edits.sound_cues):
        raise HTTPException(status_code=400, detail="Unknown sound effect.")
    if seconds and any(cue.start_seconds >= seconds for cue in edits.sound_cues):
        raise HTTPException(status_code=400, detail=f"Sound effects must start before the film ends ({seconds} sec).")
    if any(item.end_seconds <= item.start_seconds for item in edits.texts):
        raise HTTPException(status_code=400, detail="Each text must end after it starts.")
    if edits.speed not in SPEEDS:
        raise HTTPException(status_code=400, detail="Unsupported speed.")
    end = edits.trim_end if edits.trim_end is not None else seconds
    if seconds and (end > seconds + 0.05 or end - edits.trim_start < 1):
        raise HTTPException(status_code=400, detail="Keep at least one second of the film when trimming.")
    for voiceover in edits.voiceovers:
        if not _voiceover_file(values, voiceover.id):
            raise HTTPException(status_code=400, detail="A recorded voiceover is missing. Record it again.")
    return seconds


def save_draft(values: dict, edits: FilmEdits) -> dict:
    """Autosave the editor's settings without rendering, so the edit survives a closed tab."""
    _validate(values, edits)
    folder = _edits_dir(values)
    folder.mkdir(parents=True, exist_ok=True)
    saved_at = time.time()
    temporary = folder / "draft.json.tmp"
    temporary.write_text(json.dumps({"edits": edits.model_dump(), "saved_at": saved_at}, indent=2), encoding="utf-8")
    temporary.replace(folder / "draft.json")
    return {"saved_at": saved_at}


# ---------- Voiceover ----------
def _voiceover_file(values: dict, voiceover_id: str) -> Path | None:
    folder = _edits_dir(values)
    return next((path for path in folder.glob(f"voice_{voiceover_id}.*") if path.is_file()), None) if folder.is_dir() else None


def _measure_seconds(path: Path) -> float:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        seconds = float(probe.stdout.strip())
        if seconds > 0:
            return seconds
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    # Browser recordings often carry no duration header; decoding the stream measures it.
    try:
        decoded = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True, timeout=60)
        stamps = [part.split()[0] for part in decoded.stderr.split("time=")[1:]]
        hours, minutes, seconds = stamps[-1].split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0.0


def save_voiceover(values: dict, data: bytes, content_type: str) -> dict:
    if not editable(values):
        raise HTTPException(status_code=409, detail="Only a finished film can be edited.")
    if not data:
        raise HTTPException(status_code=400, detail="The recording is empty.")
    if len(data) > MAX_VOICEOVER_BYTES:
        raise HTTPException(status_code=413, detail="That recording is too long. Keep voiceovers under a few minutes.")
    extension = {
        "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav",
    }.get(content_type.split(";")[0].strip())
    if not extension:
        raise HTTPException(status_code=415, detail="Unsupported recording format.")
    folder = _edits_dir(values)
    folder.mkdir(parents=True, exist_ok=True)
    voiceover_id = uuid.uuid4().hex[:12]
    path = folder / f"voice_{voiceover_id}.{extension}"
    path.write_bytes(data)
    seconds = _measure_seconds(path)
    if seconds <= 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The recording could not be read. Try again.")
    return {"id": voiceover_id, "seconds": round(seconds, 2), "file": str(path)}


# ---------- Export ----------
def render_edit(values: dict, edits: FilmEdits) -> dict:
    """Render the edit as the film's next version; the original and earlier versions stay."""
    if not values.get("video_file") or not values.get("narration_file"):
        raise HTTPException(status_code=409, detail="Only a finished film can be edited.")
    seconds = _validate(values, edits)
    voice_spans = [(voiceover.start_seconds, voiceover.start_seconds + voiceover.seconds) for voiceover in edits.voiceovers]
    state = {
        **values,
        "music_file": str(sound_file(edits.music_id)) if edits.music_id else None,
        "sound_cues": [
            {"file": str(sound_file(cue.sound_id)), "sfx_id": cue.sound_id, "start_seconds": cue.start_seconds, "volume": cue.volume}
            for cue in sorted(edits.sound_cues, key=lambda cue: cue.start_seconds)
        ],
        "voiceover_tracks": [
            {"file": str(_voiceover_file(values, voiceover.id)), "start_seconds": voiceover.start_seconds, "clean": edits.clean_voice}
            for voiceover in edits.voiceovers
        ],
        "narration_mute_spans": voice_spans if edits.mute_narration_under_voice else [],
    }
    if not _renders.acquire(timeout=1):
        raise HTTPException(status_code=429, detail="The editor is busy with other films. Try again in a minute.")
    try:
        folder = _edits_dir(values)
        folder.mkdir(parents=True, exist_ok=True)
        with _lock:
            version = len(_versions(values)) + 1
            # Claim the version number before the slow render so a parallel export cannot take it too.
            settings = folder / f"edit_v{version:03}.json"
            settings.write_text("{}", encoding="utf-8")
        video_file = folder / f"edit_v{version:03}.mp4"
        width, height = _resolution(values)
        overlays_dir = folder / f"overlays_v{version:03}"
        try:
            overlays = [
                *caption_overlays(
                    values.get("subtitles") or [], _aligned_words(values), edits.caption_style, edits.caption_position,
                    width, height, overlays_dir,
                ),
                *text_overlays([item.model_dump() for item in edits.texts], width, height, overlays_dir),
            ]
            remix_video(
                state, video_file, grade=FILTERS[edits.filter], captions=False,
                levels={
                    "music_volume": edits.music_volume, "narration_volume": edits.narration_volume,
                    "duck_under_narration": edits.duck_music, "music_fade": edits.fade_music,
                },
                overlays=overlays, grade_strength=edits.filter_strength,
                trim=(edits.trim_start, edits.trim_end if edits.trim_end is not None else seconds), speed=edits.speed,
            )
        except Exception:
            settings.unlink(missing_ok=True)
            raise
        finally:
            # The PNGs are only needed during the render.
            if overlays_dir.is_dir():
                for path in overlays_dir.iterdir():
                    path.unlink(missing_ok=True)
                overlays_dir.rmdir()
        settings.write_text(json.dumps({"edits": edits.model_dump(), "video_file": str(video_file)}, indent=2), encoding="utf-8")
        # The exported version now holds these settings; the next open starts from it.
        (folder / "draft.json").unlink(missing_ok=True)
    finally:
        _renders.release()
    return {"version": version, "video_file": str(video_file)}
