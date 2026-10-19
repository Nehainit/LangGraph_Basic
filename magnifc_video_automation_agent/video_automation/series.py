"""Series: films made as episodes, each one opening where the previous episode ended.

A series starts from any finished film, which becomes episode 1. When an episode finishes, a continuity snapshot
is copied into library/<user>/series/<series id>/ — its story, ending, cast sheets, style, and final frame — so the
next episode can continue from it after the film's outputs folder is gone.

The next episode gets three things from the snapshots: the story agent is told what happened and where it ended,
the cast is reused through their approved sheets (the same path as saved library characters, so nothing is
redrawn), and the first shot uses the previous episode's final frame as its continuity reference.
"""

import json
import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from video_automation.auth import require_user
from video_automation.character_library import _user_dir
from video_automation.continuity_context import character_definitions, continuity_context, location_definitions


logger = logging.getLogger(__name__)
SERIES_ID = re.compile(r"^[0-9a-f]{12}$")
# Earlier episodes are summarised to their one-line ideas so the story prompt stays short as a series grows.
RECAP_EPISODES = 5
router = APIRouter()
_lock = threading.Lock()


class StartSeries(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    thread_id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=120)


def _file(user_id: str | None) -> Path:
    return _user_dir(user_id) / "series.json"


def _series_dir(user_id: str | None, series_id: str) -> Path:
    return _user_dir(user_id) / "series" / series_id


def _load(user_id: str | None) -> list[dict]:
    path = _file(user_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


@contextmanager
def _editing(user_id: str | None):
    """Read, change, and write one user's series under a lock; nothing is written if the change raises."""
    with _lock:
        series = _load(user_id)
        yield series
        path = _file(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(series, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def _find(series: list[dict], series_id: str) -> dict:
    record = next((item for item in series if item["id"] == series_id), None) if SERIES_ID.match(series_id) else None
    if record is None:
        raise HTTPException(status_code=404, detail="Series not found.")
    return record


def _series_of(series: list[dict], thread_id: str) -> tuple[dict, dict] | tuple[None, None]:
    for record in series:
        for episode in record["episodes"]:
            if episode["thread_id"] == thread_id:
                return record, episode
    return None, None


def _finished(record: dict) -> list[dict]:
    return [episode for episode in record["episodes"] if episode["status"] == "complete"]


def _final_frame(values: dict, target: Path) -> Path | None:
    """The last frame viewers see, taken from the raw clip at its edit out-point so no burned-in caption comes with it."""
    timeline = values.get("edit_timeline") or []
    last = timeline[-1] if timeline else None
    if last and last.get("video_file") and Path(last["video_file"]).is_file():
        moment = max(0.0, float(last.get("source_out") or 0) - 0.05)
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", f"{moment:.3f}", "-i", last["video_file"], "-frames:v", "1", str(target)],
                check=True, capture_output=True, timeout=60,
            )
            if target.is_file():
                return target
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Could not take the final frame from %s: %s", last["video_file"], exc)
    # A film without clips (all-still shots) ends on its last storyboard frame.
    images = [path for path in values.get("image_files") or [] if path and Path(path).is_file()]
    if images:
        shutil.copyfile(images[-1], target)
        return target
    return None


def _snapshot(values: dict, folder: Path) -> dict:
    """Copy what the next episode needs out of a finished film, so it survives the film's outputs being cleared."""
    folder.mkdir(parents=True, exist_ok=True)
    cast = []
    sheets = values.get("character_reference_files") or []
    for index, character in enumerate(character_definitions(values)):
        sheet = Path(sheets[index]) if index < len(sheets) else None
        if not sheet or not sheet.is_file():
            continue
        saved = folder / f"character_{index + 1:03}{sheet.suffix}"
        shutil.copyfile(sheet, saved)
        cast.append({"name": character["name"], "description": character.get("description") or character["name"], "sheet_file": str(saved)})
    outline = values.get("story_outline") or {}
    beats = values.get("story_beats") or outline.get("structure") or []
    frame = _final_frame(values, folder / "final_frame.png")
    return {
        "idea": outline.get("idea") or values.get("topic", ""),
        "story": values.get("story", ""),
        "ending": beats[-1]["description"] if beats else values.get("story", ""),
        "cast": cast,
        "locations": [location["description"] for location in location_definitions(values)],
        "visual_style": continuity_context(values)["visual_style"],
        "final_frame_file": str(frame) if frame else None,
    }


def _public(record: dict) -> dict:
    return {
        "id": record["id"],
        "title": record["title"],
        "aspect_ratio": record.get("aspect_ratio"),
        "next_episode": len(_finished(record)) + 1,
        "episodes": [
            {key: episode.get(key) for key in ("number", "thread_id", "status", "topic", "video_url", "finished_at")}
            for episode in record["episodes"]
        ],
    }


def start_series(user_id: str | None, thread_id: str, values: dict, video_url: str | None, title: str | None = None) -> dict:
    """Make a finished film episode 1 of a new series, or return the series it already belongs to."""
    if not video_url:
        raise HTTPException(status_code=409, detail="Only a finished film can start a series.")
    with _editing(user_id) as series:
        existing, _ = _series_of(series, thread_id)
        if existing:
            return _public(existing)
        series_id = uuid.uuid4().hex[:12]
        now = time.time()
        record = {
            "id": series_id,
            "title": title or (values.get("story_outline") or {}).get("idea", "")[:80] or "My series",
            "aspect_ratio": values.get("aspect_ratio"),
            "created_at": now,
            "episodes": [{
                "number": 1, "thread_id": thread_id, "status": "complete", "topic": values.get("topic", ""),
                "video_url": video_url, "finished_at": now,
                "continuity": _snapshot(values, _series_dir(user_id, series_id) / "ep01"),
            }],
        }
        series.insert(0, record)
    return _public(record)


def next_episode_state(user_id: str | None, series_id: str, thread_id: str, topic: str) -> dict:
    """Register the next episode and return the graph state that makes it continue the series."""
    with _editing(user_id) as series:
        record = _find(series, series_id)
        finished = _finished(record)
        if not finished:
            raise HTTPException(status_code=409, detail="This series has no finished episode to continue from.")
        # An earlier attempt at this episode that never finished is replaced by this one.
        record["episodes"] = [*finished, {"number": len(finished) + 1, "thread_id": thread_id, "status": "making", "topic": topic}]
    previous = finished[-1]["continuity"]
    cast = {}
    for episode in finished:
        # A character keeps the sheet from the latest episode they appeared in.
        for character in episode["continuity"]["cast"]:
            cast[character["name"].casefold()] = character
    return {
        "series_id": series_id,
        "episode_number": len(finished) + 1,
        "visual_style": previous["visual_style"],
        "library_characters": [
            {
                "character_id": f"series-{index:03}", "name": character["name"], "description": character["description"],
                "visual_style": previous["visual_style"], "sheet_file": character["sheet_file"], "emotions_file": None,
            }
            for index, character in enumerate(cast.values(), start=1)
            if Path(character["sheet_file"]).is_file()
        ],
        "previous_episode": {
            "episode_number": len(finished),
            "series_title": record["title"],
            "earlier_episodes": [episode["continuity"]["idea"] for episode in finished[-RECAP_EPISODES:-1]],
            "story": previous["story"],
            "ending": previous["ending"],
            "recurring_cast": [f"{character['name']}: {character['description']}" for character in cast.values()],
            "locations": previous["locations"],
            "final_frame_file": previous["final_frame_file"] if previous["final_frame_file"] and Path(previous["final_frame_file"]).is_file() else None,
        },
    }


def finish_episode(user_id: str | None, thread_id: str, values: dict, video_url: str | None) -> None:
    """Snapshot an episode once it has a video, so the next one can continue from it."""
    if not video_url:
        return
    with _editing(user_id) as series:
        record, episode = _series_of(series, thread_id)
        if not record or episode["status"] == "complete":
            return
        episode.update(
            status="complete", video_url=video_url, finished_at=time.time(),
            continuity=_snapshot(values, _series_dir(user_id, record["id"]) / f"ep{episode['number']:02}"),
        )


@router.get("/api/series")
def list_series(user_id: str | None = Depends(require_user)) -> dict:
    return {"series": [_public(record) for record in _load(user_id)]}


@router.get("/api/series/{series_id}")
def get_series(series_id: str, user_id: str | None = Depends(require_user)) -> dict:
    return _public(_find(_load(user_id), series_id))
