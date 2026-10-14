import hashlib
import json
import logging
import math
import re
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
FAL_AUDIO_TIMEOUT_SECONDS = 180
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


# Words that place a story in India; its music then uses Indian instruments instead of a Western orchestra.
INDIAN_SETTING = re.compile(
    r"\b(mahabharat\w*|ramayan\w*|krishna|arjun\w*|draupadi|pandava\w*|kaurava\w*|rama|sita|hanuman|shiva|"
    r"ganesh\w*|dharma|svayamvara|panchala|hastinapur\w*|ayodhya|vrindavan|raja|rani|maharaja|india\w*|hindi)\b",
    re.IGNORECASE,
)
INDIAN_INSTRUMENTS = "Indian classical instrumentation: bansuri flute, sitar, tanpura drone and soft tabla."


def _music_prompt(state: AgentState) -> str:
    requirements = state.get("parsed_requirements") or {}
    idea = (state.get("story_outline") or {}).get("idea") or state.get("story", "")
    # state["tone"] holds an "Infer the tone..." placeholder when the user names none, so the scenes'
    # planned emotions give the mood instead.
    emotions = list(dict.fromkeys(
        str(scene.get("emotion")).strip().casefold() for scene in state.get("scenes") or [] if scene.get("emotion")
    ))
    tone = requirements.get("tone") or ", ".join(emotions[:3]) or "heartwarming"
    setting = " ".join(location.get("description", "") for location in location_definitions(state))
    language = str(state.get("language") or requirements.get("language") or "")
    indian = INDIAN_SETTING.search(f"{state.get('topic', '')} {idea} {setting} {language}")
    return (
        f"Instrumental background score for a short {tone} story film. "
        + (INDIAN_INSTRUMENTS if indian else "Instruments that suit the story's setting and era.")
        + f" Setting: {setting or idea}. Story: {idea} "
        "Slow to moderate tempo. Soft and sparse so a spoken narrator stays clear; no vocals, no drums that hit "
        "hard. Builds gently and ends on a resolved, warm final chord."
    )


def _location_spans(state: AgentState) -> dict[str, list[tuple[dict, float, float]]]:
    """Scenes grouped by location, so each place gets one ambience clip that every scene there reuses."""
    spans: dict[str, list[tuple[dict, float, float]]] = {}
    for scene, start, end in _scene_spans(state):
        spans.setdefault(str(scene.get("location_id") or scene["scene_id"]), []).append((scene, start, end))
    return spans


def _ambience_jobs(state: AgentState, out: Path) -> tuple[list[dict], list[dict]]:
    """One generated clip per location (the editor loops it), and one timed track per scene."""
    locations = {location["location_id"]: location for location in location_definitions(state)}
    jobs, tracks = [], []
    for location_id, spans in _location_spans(state).items():
        longest = max(end - start for _, start, end in spans)
        file = str(out / f"ambience_{location_id}.mp3")
        scene = spans[0][0]
        jobs.append({
            "file": file,
            "provider": "elevenlabs",
            "price": sound_price("ambience", longest),
            "request": {
                "url": f"{ELEVENLABS_SFX_URL}?{parse.urlencode({'output_format': 'mp3_44100_128'})}",
                "payload": {
                    "text": _ambience_prompt(scene, locations.get(location_id, {})),
                    "duration_seconds": min(SFX_MAX_SECONDS, max(0.5, longest)),
                    "prompt_influence": 0.3,
                    "model_id": state.get("elevenlabs_sfx_model", "eleven_text_to_sound_v2"),
                },
            },
        })
        tracks.extend({"file": file, "start_seconds": start, "duration_seconds": round(end - start, 3)} for _, start, end in spans)
    return jobs, tracks


def _music_candidates(state: AgentState, out: Path) -> list[dict]:
    """Music requests in provider order (sound.music_providers); the first that succeeds is used."""
    config = PIPELINE_CONFIG["sound"]
    spans = _scene_spans(state)
    total = float(state.get("actual_narration_seconds") or (spans[-1][2] if spans else 0))
    if not config["music"] or total <= 0:
        return []
    prompt = _music_prompt(state)
    # One extra second lets the music resolve under the final fade.
    seconds = total + 1
    candidates = []
    for provider in config.get("music_providers") or ["elevenlabs"]:
        if provider == "library":
            # Free CC0 music from the local sound library is chosen with the effects in create_sound_bed.
            continue
        if provider == "fal":
            candidates.append({
                "file": str(out / "music_bed_fal.wav"),
                "provider": "fal",
                "price": sound_price("fal_music", seconds),
                "request": {"model": config["fal_music_model"], "payload": {"prompt": prompt, "duration": max(5, math.ceil(seconds))}},
            })
        elif provider == "elevenlabs":
            candidates.append({
                "file": str(out / "music_bed.mp3"),
                "provider": "elevenlabs",
                "price": sound_price("music", seconds),
                "request": {
                    "url": f"{ELEVENLABS_MUSIC_URL}?{parse.urlencode({'output_format': 'mp3_44100_128'})}",
                    "payload": {"prompt": prompt, "music_length_ms": max(3000, int(seconds * 1000)), "force_instrumental": True},
                },
            })
        else:
            raise ValueError(f"Unknown music provider in config/pipeline.yml: {provider!r}")
    return candidates


def _jobs(state: AgentState, out: Path) -> tuple[list[dict], list[dict], list[dict]]:
    """Ambience generation jobs, the per-scene ambience tracks they feed, and the music candidates."""
    ambience_jobs, tracks = _ambience_jobs(state, out) if PIPELINE_CONFIG["sound"]["ambience"] else ([], [])
    return ambience_jobs, tracks, _music_candidates(state, out)


def _fal_audio(job: dict) -> bytes:
    from video_automation.agents.image_agent import _env, _fal_job, _get_bytes

    _env("FAL_KEY")
    request_fingerprint = fingerprint(job["request"])
    result = _fal_job(
        job["request"]["model"], lambda: job["request"]["payload"], Path(job["file"]).with_suffix(".job.json"),
        request_fingerprint, FAL_AUDIO_TIMEOUT_SECONDS,
    )
    try:
        url = result["audio_file"]["url"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("fal.ai response did not include an audio file.") from exc
    return _get_bytes(url)


def _render(job: dict, ledger) -> str:
    """Render one audio file, reusing it when the request is unchanged."""
    path = Path(job["file"])
    meta_file = path.with_suffix(".json")
    if path.is_file() and meta_file.is_file() and json.loads(meta_file.read_text(encoding="utf-8")) == job["request"]:
        return str(path)
    with paid_call(ledger, f"sound:{path}:{fingerprint(job['request'])}", "sound", path.stem, job["price"]):
        if job["provider"] == "fal":
            audio = _fal_audio(job)
        else:
            audio = _bytes_request(job["request"]["url"], job["request"]["payload"], _elevenlabs_headers())
    if not audio:
        raise RuntimeError(f"{job['provider']} returned an empty audio file.")
    path.write_bytes(audio)
    meta_file.write_text(json.dumps(job["request"], indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _render_music(candidates: list[dict], ledger) -> tuple[str | None, list[str]]:
    """The first music provider that succeeds; earlier failures become warnings only if every provider fails."""
    errors = []
    for job in candidates:
        try:
            return _render(job, ledger), []
        except Exception as exc:
            errors.append(f"{job['provider']}: {exc}")
    return None, [f"Background music was skipped: {'; '.join(errors)}"] if errors else []


def _render_bed(state: AgentState) -> dict:
    out = _out_dir(state, "audio")
    out.mkdir(parents=True, exist_ok=True)
    ambience, tracks, music = _jobs(state, out)
    warnings = []
    ledger = ledger_for(state)
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(ambience) + 1)), thread_name_prefix="sound-bed") as pool:
        music_future = pool.submit(_render_music, music, ledger)
        futures = [pool.submit(_render, job, ledger) for job in ambience]
    rendered = {}
    for job, future in zip(ambience, futures):
        try:
            rendered[job["file"]] = future.result()
        except Exception as exc:
            # Ambience and music enrich the cut; a missing track should not discard a finished video.
            warnings.append(f"Sound bed track {Path(job['file']).name} was skipped: {exc}")
    music_file, music_warnings = music_future.result()
    return {
        "ambience_tracks": [track for track in tracks if track["file"] in rendered],
        "music_file": music_file,
        "sound_bed_warnings": [*warnings, *music_warnings],
    }


def _bed_key(state: AgentState) -> str:
    out = _out_dir(state, "audio")
    ambience, _, music = _jobs(state, out)
    identity = [[job["file"], job["request"]] for job in [*ambience, *music]]
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


def _spoken_lines(state: AgentState) -> list[dict]:
    """Each narration line with the seconds it is spoken, for placing sound effects between lines."""
    timings = {item.get("segment_id"): item for item in state.get("narration_segment_timings", []) if isinstance(item, dict)}
    lines = []
    for segment in state.get("narration_segments", []):
        timing = timings.get(segment.get("segment_id"))
        if timing:
            lines.append({
                "text": segment.get("text", ""),
                "start_seconds": round(float(timing["start_seconds"]), 2),
                "end_seconds": round(float(timing.get("speech_end_seconds", timing["end_seconds"])), 2),
            })
    return lines


def _library_sound(state: AgentState) -> dict:
    """Free library music and timed effects, when the library is a configured music provider."""
    from video_automation.sound_library import plan_sound

    config = PIPELINE_CONFIG["sound"]
    total = float(state.get("actual_narration_seconds") or 0)
    if "library" not in (config.get("music_providers") or []) or total <= 0:
        return {}
    story = state.get("story_outline") or {}
    plan, evaluations = plan_sound(_spoken_lines(state), total, json.dumps(story, ensure_ascii=False)[:2000])
    result = {"sound_cues": plan["cues"] if config.get("sound_effects", True) else []}
    if config["music"] and plan.get("music"):
        result.update(music_file=plan["music"], music_id=plan.get("music_id"))
    if evaluations:
        result["llm_evaluations"] = [*state.get("llm_evaluations", []), *evaluations]
    return result


def create_sound_bed(state: AgentState) -> dict:
    if not PIPELINE_CONFIG["sound"]["enabled"]:
        return {}
    future = _prefetched_beds.pop(_bed_key(state), None)
    try:
        result = future.result() if future else _render_bed(state)
    except Exception as exc:
        result = {"ambience_tracks": [], "music_file": None, "sound_bed_warnings": [f"Sound bed was skipped: {exc}"]}
    warnings = result.pop("sound_bed_warnings")
    try:
        library = _library_sound(state)
    except Exception as exc:  # Free sound only enriches the film.
        library, warnings = {}, [*warnings, f"Sound library was skipped: {exc}"]
    if library.get("music_file") or not result.get("music_file"):
        result.update(library)
    else:
        result.update({key: value for key, value in library.items() if key != "music_file"})
    result["cost_ledger"] = ledger_for(state).snapshot()
    return {**result, "warnings": [*state.get("warnings", []), *warnings]} if warnings else result
