import hashlib
import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from urllib import parse

from video_automation.agents.soundfx_agent import ELEVENLABS_SFX_URL, _bytes_request, _elevenlabs_headers, _out_dir
from video_automation.continuity_context import continuity_context, location_definitions
from video_automation.cost_control import fingerprint, ledger_for, paid_call, sound_price
from video_automation.prompts import PIPELINE_CONFIG
from video_automation.schema import AgentState


ELEVENLABS_MUSIC_URL = "https://api.elevenlabs.io/v1/music"
SFX_MAX_SECONDS = 30
logger = logging.getLogger(__name__)
# The sound bed only needs the approved scenes, so it renders while shot videos generate.
_SOUND_PREFETCH = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sound-prefetch")
_prefetched_beds: dict[str, Future] = {}


def _scene_spans(state: AgentState) -> list[tuple[dict, float, float]]:
    return [
        (scene, float(scene["start_sec"]), float(scene["end_sec"]))
        for scene in state.get("scenes") or []
        if scene.get("end_sec") is not None and float(scene["end_sec"]) > float(scene.get("start_sec", 0))
    ]


def _ambience_prompt(scene: dict, location: dict) -> str:
    return (
        f"Ambient environmental sound for this place: {location.get('description') or scene.get('location_id')}. "
        f"Mood: {scene.get('emotion') or 'calm'}. Natural, continuous background atmosphere only. "
        "No speech, no voices, no music, no sudden loud effects."
    )


def _music_prompt(state: AgentState) -> str:
    requirements = state.get("parsed_requirements") or {}
    idea = (state.get("story_outline") or {}).get("idea") or state.get("story", "")
    return (
        f"Instrumental cinematic underscore for a short {requirements.get('tone') or state.get('tone') or 'heartwarming'} film "
        f"in a {continuity_context(state)['visual_style']} style. Story: {idea} "
        "Gentle, emotionally supportive dynamics that sit under spoken narration, with a clear resolution at the end."
    )


def _jobs(state: AgentState, out: Path) -> tuple[list[dict], dict | None]:
    config = PIPELINE_CONFIG["sound"]
    locations = {location["location_id"]: location for location in location_definitions(state)}
    ambience = []
    if config["ambience"]:
        for scene, start, end in _scene_spans(state):
            ambience.append({
                "file": str(out / f"ambience_{scene['scene_id']}.mp3"),
                "start_seconds": start,
                "duration_seconds": round(end - start, 3),
                "price": sound_price("ambience", end - start),
                "request": {
                    "url": f"{ELEVENLABS_SFX_URL}?{parse.urlencode({'output_format': 'mp3_44100_128'})}",
                    "payload": {
                        "text": _ambience_prompt(scene, locations.get(scene.get("location_id"), {})),
                        "duration_seconds": min(SFX_MAX_SECONDS, max(0.5, end - start)),
                        "prompt_influence": 0.3,
                        "model_id": state.get("elevenlabs_sfx_model", "eleven_text_to_sound_v2"),
                    },
                },
            })
    music = None
    spans = _scene_spans(state)
    total = float(state.get("actual_narration_seconds") or (spans[-1][2] if spans else 0))
    if config["music"] and total > 0:
        music = {
            "file": str(out / "music_bed.mp3"),
            "price": sound_price("music", total + 1),
            "request": {
                "url": f"{ELEVENLABS_MUSIC_URL}?{parse.urlencode({'output_format': 'mp3_44100_128'})}",
                "payload": {
                    "prompt": _music_prompt(state),
                    # One extra second lets the music resolve under the final fade.
                    "music_length_ms": max(3000, int((total + 1) * 1000)),
                    "force_instrumental": True,
                },
            },
        }
    return ambience, music


def _render(job: dict, ledger) -> str:
    """Render one audio file, reusing it when the request is unchanged."""
    path = Path(job["file"])
    meta_file = path.with_suffix(".json")
    if path.is_file() and meta_file.is_file() and json.loads(meta_file.read_text(encoding="utf-8")) == job["request"]:
        return str(path)
    with paid_call(ledger, f"sound:{path}:{fingerprint(job['request'])}", "sound", path.stem, job["price"]):
        audio = _bytes_request(job["request"]["url"], job["request"]["payload"], _elevenlabs_headers())
    path.write_bytes(audio)
    meta_file.write_text(json.dumps(job["request"], indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _render_bed(state: AgentState) -> dict:
    out = _out_dir(state, "audio")
    out.mkdir(parents=True, exist_ok=True)
    ambience, music = _jobs(state, out)
    jobs = [*ambience, *([music] if music else [])]
    warnings = []
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(jobs))), thread_name_prefix="sound-bed") as pool:
        ledger = ledger_for(state)
        futures = [pool.submit(_render, job, ledger) for job in jobs]
    rendered = {}
    for job, future in zip(jobs, futures):
        try:
            rendered[job["file"]] = future.result()
        except Exception as exc:
            # Ambience and music enrich the cut; a missing track should not discard a finished video.
            warnings.append(f"Sound bed track {Path(job['file']).name} was skipped: {exc}")
    return {
        "ambience_tracks": [
            {key: track[key] for key in ("file", "start_seconds", "duration_seconds")}
            for track in ambience if track["file"] in rendered
        ],
        "music_file": rendered.get(music["file"]) if music else None,
        "sound_bed_warnings": warnings,
    }


def _bed_key(state: AgentState) -> str:
    out = _out_dir(state, "audio")
    ambience, music = _jobs(state, out)
    identity = [[job["file"], job["request"]] for job in [*ambience, *([music] if music else [])]]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


def prefetch_sound_bed(state: AgentState) -> None:
    if not PIPELINE_CONFIG["sound"]["enabled"]:
        return
    try:
        key = _bed_key(state)
        if key not in _prefetched_beds:
            _prefetched_beds[key] = _SOUND_PREFETCH.submit(_render_bed, dict(state))
    except Exception as exc:
        logger.warning("Sound bed prefetch was not started: %s", exc)


def create_sound_bed(state: AgentState) -> dict:
    if not PIPELINE_CONFIG["sound"]["enabled"]:
        return {}
    future = _prefetched_beds.pop(_bed_key(state), None)
    try:
        result = future.result() if future else _render_bed(state)
    except Exception as exc:
        result = {"ambience_tracks": [], "music_file": None, "sound_bed_warnings": [f"Sound bed was skipped: {exc}"]}
    warnings = result.pop("sound_bed_warnings")
    result["cost_ledger"] = ledger_for(state).snapshot()
    return {**result, "warnings": [*state.get("warnings", []), *warnings]} if warnings else result
