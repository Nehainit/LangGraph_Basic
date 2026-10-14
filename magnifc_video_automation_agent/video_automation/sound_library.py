"""Free CC0 sound effects and music from Freesound, kept in a local library and placed by a cheap cue planner.

The library is built once (`python -m video_automation.sound_library build`), so films pay nothing for sound.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib import error, parse, request

import yaml

from video_automation.agents.story_agent import _parse_json
from video_automation.models import invoke_with_evaluation, load_model

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "config" / "sound_library.yml").open(encoding="utf-8") as config_file:
    SOUND_LIBRARY_CONFIG = yaml.safe_load(config_file)["sound_library"]
FREESOUND_SEARCH_URL = "https://freesound.org/apiv2/search/text/"


def library_folder() -> Path:
    return ROOT / SOUND_LIBRARY_CONFIG["folder"]


def load_library() -> list[dict]:
    """Library entries whose audio file is present; an unbuilt library is simply empty."""
    index = library_folder() / "library.json"
    if not index.is_file():
        return []
    entries = json.loads(index.read_text(encoding="utf-8"))
    return [entry for entry in entries if (library_folder() / entry["file"]).is_file()]


def _get_json(url: str) -> dict:
    try:
        with request.urlopen(request.Request(url, headers={"Accept": "application/json"}), timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(f"Freesound request failed: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:300]}") from exc


def search_freesound(query: str, seconds: list[float], key: str) -> list[dict]:
    """CC0 sounds for a query within a duration range, best rated first."""
    params = {
        "query": query,
        "filter": f'{SOUND_LIBRARY_CONFIG["license_filter"]} duration:[{seconds[0]} TO {seconds[1]}]',
        "fields": "id,name,tags,duration,license,username,url,previews,avg_rating,num_downloads",
        # Relevance first: sorting by rating returned well-rated sounds that were not what was searched for.
        "sort": "score",
        "page_size": 50,
        "token": key,
    }
    results = _get_json(f"{FREESOUND_SEARCH_URL}?{parse.urlencode(params)}").get("results", [])
    minimum = int(SOUND_LIBRARY_CONFIG.get("min_downloads", 0))
    # Every meaningful search word must appear in the sound's name or tags ("record scratch" never "door knock").
    words = [word for word in query.casefold().split() if word not in {"music", "sound", "sfx", "effect"}]
    kept = [
        item for item in results
        if "creative commons 0" in str(item.get("license", "")).casefold() or "publicdomain/zero" in str(item.get("license", ""))
        if int(item.get("num_downloads") or 0) >= minimum and (item.get("previews") or {}).get("preview-hq-mp3")
        if all(word in " ".join([str(item.get("name", "")), *map(str, item.get("tags") or [])]).casefold() for word in words)
    ]
    # Among relevant sounds, the most downloaded are the ones creators found usable.
    return sorted(kept, key=lambda item: -int(item.get("num_downloads") or 0))


def _download(url: str, path: Path) -> None:
    with request.urlopen(url, timeout=120) as response:
        path.write_bytes(response.read())


def _duration(path: Path) -> float:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return round(float(output), 2)


def build_library(key: str | None = None) -> list[dict]:
    """Download the configured CC0 sounds once; existing entries are kept, so a rebuild only fills gaps."""
    # Freesound's "client secret" is the API key for token requests.
    key = key or os.getenv("FREESOUND_API_KEY") or os.getenv("FREE_SOUND_CLIENT_SECRET")
    if not key:
        raise RuntimeError("Set FREESOUND_API_KEY in .env (free key: https://freesound.org/apiv2/apply).")
    folder = library_folder()
    folder.mkdir(parents=True, exist_ok=True)
    index = folder / "library.json"
    entries = json.loads(index.read_text(encoding="utf-8")) if index.is_file() else []
    known = {entry["freesound_id"] for entry in entries}
    for kind in ("sfx", "music"):
        for spec in SOUND_LIBRARY_CONFIG[kind]:
            have = sum(1 for entry in entries if entry["tag"] == spec["tag"])
            for item in search_freesound(spec["query"], spec["seconds"], key):
                if have >= int(SOUND_LIBRARY_CONFIG["per_tag"]):
                    break
                if item["id"] in known:
                    continue
                file_name = f'{kind}/{spec["tag"]}-{item["id"]}.mp3'
                (folder / kind).mkdir(exist_ok=True)
                _download(item["previews"]["preview-hq-mp3"], folder / file_name)
                entries.append({
                    "id": f'{spec["tag"]}-{item["id"]}', "kind": kind, "tag": spec["tag"], "file": file_name,
                    "seconds": _duration(folder / file_name), "name": item.get("name", ""),
                    "freesound_id": item["id"], "author": item.get("username", ""), "source_url": item.get("url", ""),
                    "license": "CC0",
                })
                known.add(item["id"])
                have += 1
                logger.info("Added %s (%s)", file_name, item.get("name"))
            if have == 0:
                logger.warning("No CC0 sound found for %s (%s).", spec["tag"], spec["query"])
    index.write_text(json.dumps(entries, indent=1, ensure_ascii=False), encoding="utf-8")
    return entries


CUE_PLANNER_SYSTEM_PROMPT = """You are the sound designer of a short dramatic story film (YouTube Shorts, Instagram Reels).

Pick one background music bed and a few sound effects from the supplied library to make the story land harder.
- music_id: the music entry whose tag fits the film's mood.
- cues: sound effects that punctuate the big moments: a reveal, a shock, an arrival, a confession, the final
  cliffhanger. Use only sfx entries from the library. at_seconds is when the effect starts, read from the
  narration timings: place stings and hits right after the line that lands the moment, in the pause, never over
  the middle of a spoken line; place risers and heartbeats so they end at the moment they build to.
- Fewer, stronger effects beat many small ones. Never repeat the same effect twice in a row.
Return only JSON: {"music_id": "...", "cues": [{"sfx_id": "...", "at_seconds": 0.0, "why": "..."}]}"""


def plan_sound(lines: list[dict], film_seconds: float, story: str, library: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """Choose music and timed effects for a film; returns ({music, cues}, llm_evaluations). Empty when no library."""
    library = load_library() if library is None else library
    music = [entry for entry in library if entry["kind"] == "music"]
    effects = {entry["id"]: entry for entry in library if entry["kind"] == "sfx"}
    if not music and not effects:
        return {"music": None, "cues": []}, []
    config = SOUND_LIBRARY_CONFIG["cues"]
    maximum = max(1, int(film_seconds // float(config["seconds_per_cue"])))
    catalog = [
        {"id": entry["id"], "kind": entry["kind"], "tag": entry["tag"], "seconds": entry["seconds"], "name": entry["name"]}
        for entry in library
    ]
    prompt = json.dumps({
        "story": story, "film_seconds": film_seconds, "narration_lines": lines,
        "max_cues": maximum, "library": catalog,
    }, ensure_ascii=False)
    evaluations = []
    try:
        model = load_model("sound-cues")
        if hasattr(model, "format"):
            model.format = "json"
        response, evaluation = invoke_with_evaluation(
            model,
            [{"role": "system", "content": CUE_PLANNER_SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            agent_name="sound-cues", purpose="plan_sound_cues",
        )
        evaluations.append(evaluation)
        data = _parse_json(response.content) if isinstance(response.content, str) else {}
        data = data if isinstance(data, dict) else {}
    except Exception as exc:  # Sound only enriches the film; a failed plan falls back to music alone.
        logger.warning("Sound cue planning failed: %s", exc)
        data = {}
    by_id = {entry["id"]: entry for entry in music}
    chosen_music = by_id.get(str(data.get("music_id"))) or (music[0] if music else None)
    cues, last = [], None
    for cue in sorted(
        (item for item in data.get("cues") or [] if isinstance(item, dict)),
        key=lambda item: float(item.get("at_seconds") or 0),
    ):
        entry = effects.get(str(cue.get("sfx_id")))
        try:
            at = float(cue.get("at_seconds"))
        except (TypeError, ValueError):
            continue
        if not entry or not 0 <= at < film_seconds:
            continue
        if cues and (at - cues[-1]["start_seconds"] < float(config["min_gap_seconds"]) or entry["id"] == last):
            continue
        cues.append({
            "file": str(library_folder() / entry["file"]), "sfx_id": entry["id"], "start_seconds": round(at, 2),
            "volume": float(config["volume"]), "why": str(cue.get("why", "")),
        })
        last = entry["id"]
        if len(cues) >= maximum:
            break
    return {
        "music": str(library_folder() / chosen_music["file"]) if chosen_music else None,
        "music_id": chosen_music["id"] if chosen_music else None,
        "cues": cues,
    }, evaluations


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if sys.argv[1:] == ["build"]:
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env")
        except ModuleNotFoundError:
            pass
        built = build_library()
        print(f"{len(built)} sounds in {library_folder()}")
    else:
        print("Usage: python -m video_automation.sound_library build")
