import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib import error, parse, request

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = lambda *args, **kwargs: None

from video_automation.cost_control import fingerprint, ledger_for, narration_price, paid_call
from video_automation.prompts import PIPELINE_CONFIG
from video_automation.schema import AgentState
from video_automation.agents.story_agent import _duration_seconds


load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DEFAULT_ELEVENLABS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
DEFAULT_ELEVENLABS_TTS_MODEL = "eleven_multilingual_v2"
# ElevenLabs enforces language_code only on the v2.5 models; others reject or ignore it.
LANGUAGE_CODE_MODELS = ("eleven_flash_v2_5", "eleven_turbo_v2_5")


def _segment_separator() -> str:
    return str((PIPELINE_CONFIG.get("narration") or {}).get("segment_separator", "\n"))
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
MAX_NARRATION_TIMING_ATTEMPTS = 3


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "video"


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Set {name} in your environment or .env file.")
    return value.strip()


def _out_dir(state: AgentState, *parts: str) -> Path:
    return Path(state.get("output_dir", "outputs")) / _slug(state["topic"]) / Path(*parts)


def _elevenlabs_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "xi-api-key": os.getenv("ELEVENLABS_API_KEY") or _env("elevnlas"),
    }


def _json_request(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    req = request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed: HTTP {exc.code} {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"POST {url} failed: {exc.reason}") from exc


def _narration_items(state: AgentState) -> list[tuple[int | str, str]]:
    segments = state.get("narration_segments") or []
    if segments:
        return [(str(segment["segment_id"]), str(segment["text"]).strip()) for segment in segments]
    if state.get("narration_script"):
        return [(1, state["narration_script"])]
    storyboard = state.get("storyboard") or []
    items = [
        (int(scene.get("scene_number", index)), str(scene.get("narration", "")).strip())
        for index, scene in enumerate(storyboard, start=1)
        if scene.get("narration")
    ]
    if items:
        return items
    if state.get("story"):
        return [(1, state["story"])]
    raise RuntimeError("Narration agent needs story or storyboard narration.")


def _scene_timings(items: list[tuple[int | str, str]], alignment: dict[str, Any]) -> list[dict[str, float | int | str]]:
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    timings = []
    offset = 0
    previous_end = 0.0

    for item_id, line in items:
        indexes = [i for i in range(offset, min(offset + len(line), len(starts))) if not line[i - offset].isspace()]
        if indexes:
            start = float(starts[indexes[0]])
            end = float(ends[indexes[-1]])
        else:
            start = previous_end
            end = previous_end + 0.5
        if end <= start:
            end = start + 0.5
        timings.append({
            "segment_id" if isinstance(item_id, str) else "scene_number": item_id,
            "start_seconds": start,
            "end_seconds": end,
        })
        previous_end = end
        offset += len(line) + len(_segment_separator())

    return timings


def _narration_result(
    state: AgentState,
    narration_file: Path,
    alignment_file: Path,
    alignment: dict[str, Any],
    scene_timings: list[dict[str, float | int]],
) -> dict[str, object]:
    ends = alignment.get("character_end_times_seconds") or []
    spoken_seconds = float(ends[-1]) if ends else 0.0
    allowed_seconds = _duration_seconds(state["duration"])
    result = {
        "narration_file": str(narration_file),
        "narration_provider": "ElevenLabs",
        "narration_model_used": state.get("narration_model") or state.get("elevenlabs_tts_model") or os.getenv("ELEVENLABS_TTS_MODEL", DEFAULT_ELEVENLABS_TTS_MODEL),
        "narration_alignment_file": str(alignment_file),
        "narration_alignment": alignment,
        "narration_segment_timings": scene_timings,
        "scene_timings": scene_timings,
        "actual_narration_seconds": spoken_seconds,
    }
    if spoken_seconds > allowed_seconds:
        attempts = int(state.get("narration_timing_attempts", 0)) + 1
        if attempts >= MAX_NARRATION_TIMING_ATTEMPTS:
            return {
                **result,
                "narration_feedback": "",
                "narration_target_words": 0,
                "narration_timing_attempts": attempts,
                "warnings": [
                    *state.get("warnings", []),
                    f"Narration takes {spoken_seconds:.1f}s after {attempts} attempts; "
                    f"requested video length was {allowed_seconds:g}s. The video will follow the complete audio.",
                ],
            }
        items = _narration_items(state)
        words = sum(len(line.split()) for _, line in items)
        target_words = max(len(items), int(words * allowed_seconds / spoken_seconds * 0.85))
        note = (
            f"Shorten the narration to at most {target_words} words total while preserving the story beats. "
            f"The voice-over is {spoken_seconds:.1f}s but the movie is {allowed_seconds:g}s."
        )
        return {
            **result,
            "narration_feedback": note,
            "narration_target_words": target_words,
            "narration_timing_attempts": attempts,
        }
    return {**result, "narration_feedback": "", "narration_target_words": 0, "narration_timing_attempts": 0}


def _narration_config() -> dict:
    return PIPELINE_CONFIG.get("narration") or {}


def _character_voices(segments: list[dict], default_voice: str) -> dict[str, str]:
    """One voice per speaker, matching their gender, in order of first appearance so voices never repeat."""
    pools = _narration_config().get("character_voices") or {}
    assigned, used = {}, {}
    for segment in segments:
        speaker = str(segment.get("speaker_id") or "")
        if speaker in assigned:
            continue
        gender = segment.get("voice_gender") or "female"
        pool = list(pools.get(gender) or [])
        assigned[speaker] = pool[used.get(gender, 0) % len(pool)] if pool else default_voice
        used[gender] = used.get(gender, 0) + 1
    return assigned


def _audio_seconds(path: Path) -> float:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(output)


def _dialogue_gaps(segments: list[dict], durations: list[float], requested: float) -> list[float]:
    """Silence after each line: short inside a beat, longer between beats, padded toward the requested length."""
    config = _narration_config()
    line_gap = float(config.get("dialogue_gap_seconds", 0.4))
    beat_gap = float(config.get("beat_gap_seconds", 0.8))
    max_beat_gap = float(config.get("max_beat_gap_seconds", 2.5))
    max_hold = float(config.get("max_ending_hold_seconds", 3.0))
    gaps = [
        line_gap if segment.get("parent_beat_ids") == following.get("parent_beat_ids") else beat_gap
        for segment, following in zip(segments, segments[1:])
    ]
    beat_indexes = [index for index, gap in enumerate(gaps) if gap == beat_gap]
    extra = requested - sum(durations) - sum(gaps) - 0.3
    if extra > 0 and beat_indexes:
        grow = min(max_beat_gap - beat_gap, extra / len(beat_indexes))
        for index in beat_indexes:
            gaps[index] += max(0.0, grow)
        extra -= max(0.0, grow) * len(beat_indexes)
    # The last line holds on the picture: the suspense beat of the film.
    return [*gaps, 0.3 + min(max(0.0, extra), max_hold)]


def _line_audio(
    state: AgentState, out: Path, index: int, text: str, voice_id: str, default_voice: str, model_id: str,
    voice_settings: dict, language_code: str | None, ledger,
) -> tuple[Path, dict]:
    """One character's line, cached by its exact request so a rerun does not pay again."""
    audio_file, meta_file = out / f"line-{index:03}.mp3", out / f"line-{index:03}.json"
    meta = {"text": text, "voice_id": voice_id, "model_id": model_id, "language_code": language_code, "voice_settings": voice_settings}
    if audio_file.exists() and meta_file.exists():
        saved = json.loads(meta_file.read_text(encoding="utf-8"))
        if saved.get("request") == meta:
            return audio_file, saved["alignment"]
    for voice in dict.fromkeys([voice_id, default_voice]):
        request_meta = {**meta, "voice_id": voice}
        payload = {"text": text, "model_id": model_id}
        if voice_settings:
            payload["voice_settings"] = voice_settings
        if language_code:
            payload["language_code"] = language_code
        url = f"{ELEVENLABS_TTS_URL}/{voice}/with-timestamps?{parse.urlencode({'output_format': 'mp3_44100_128'})}"
        try:
            with paid_call(ledger, f"narration:{fingerprint(request_meta)}", "narration", "narration", narration_price(state, len(text))):
                data = _json_request(url, payload, _elevenlabs_headers())
        except RuntimeError as exc:
            # A premade voice the account cannot use falls back to the film's default voice.
            if voice != default_voice and re.search(r"HTTP (?:400|401|403|404)", str(exc)):
                continue
            raise
        alignment = data.get("alignment") or data.get("normalized_alignment")
        if not data.get("audio_base64") or not alignment:
            raise RuntimeError("Narration agent needs ElevenLabs audio and alignment data.")
        audio_file.write_bytes(base64.b64decode(data["audio_base64"]))
        meta_file.write_text(json.dumps({"request": meta, "voice_used": voice, "alignment": alignment}, ensure_ascii=False), encoding="utf-8")
        return audio_file, alignment
    raise RuntimeError(f"No ElevenLabs voice could speak line {index}.")


def _create_dialogue_narration(state: AgentState, segments: list[dict]) -> dict[str, object]:
    """Voice each line with its character's voice and join them into one soundtrack with timed pauses."""
    out = _out_dir(state, "audio")
    lines_dir = out / "dialogue"
    lines_dir.mkdir(parents=True, exist_ok=True)
    default_voice = state.get("elevenlabs_voice_id") or os.getenv("ELEVENLABS_VOICE_ID") or DEFAULT_ELEVENLABS_VOICE_ID
    model_id = state.get("narration_model") or state.get("elevenlabs_tts_model") or os.getenv("ELEVENLABS_TTS_MODEL", DEFAULT_ELEVENLABS_TTS_MODEL)
    language = state.get("language") or (state.get("parsed_requirements") or {}).get("language") or ""
    language_code = "hi" if str(language).strip().casefold() in {"hindi", "hi"} and model_id in LANGUAGE_CODE_MODELS else None
    voice_settings = dict(_narration_config().get("voice_settings") or {})
    voices = _character_voices(segments, default_voice)
    ledger = ledger_for(state)
    clips = [
        _line_audio(
            state, lines_dir, index, str(segment["text"]).strip(), voices[str(segment.get("speaker_id") or "")],
            default_voice, model_id, voice_settings, language_code, ledger,
        )
        for index, segment in enumerate(segments, start=1)
    ]
    durations = [_audio_seconds(path) for path, _ in clips]
    requested = float(_duration_seconds(state["duration"]))
    gaps = _dialogue_gaps(segments, durations, requested)

    narration_file = out / "narration.mp3"
    inputs, filters = [], []
    for index, ((path, _), gap) in enumerate(zip(clips, gaps)):
        inputs += ["-i", str(path)]
        filters.append(f"[{index}:a]aresample=44100,aformat=channel_layouts=mono,apad=pad_dur={gap:.3f}[a{index}]")
    labels = "".join(f"[a{index}]" for index in range(len(clips)))
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex",
         ";".join(filters) + f";{labels}concat=n={len(clips)}:v=0:a=1[out]", "-map", "[out]",
         "-c:a", "libmp3lame", "-b:a", "128k", str(narration_file)],
        check=True,
    )

    separator = _segment_separator()
    characters, starts, ends, timings, offset = [], [], [], [], 0.0
    for index, ((_, alignment), duration, gap, segment) in enumerate(zip(clips, durations, gaps, segments)):
        line_chars = list(alignment.get("characters") or [])
        line_starts = [offset + float(value) for value in alignment.get("character_start_times_seconds") or []]
        line_ends = [offset + float(value) for value in alignment.get("character_end_times_seconds") or []]
        spoken = [position for position, char in enumerate(line_chars) if not char.isspace()]
        timings.append({
            "segment_id": segment["segment_id"],
            "start_seconds": line_starts[spoken[0]] if spoken else offset,
            "end_seconds": line_ends[spoken[-1]] if spoken else offset + duration,
        })
        characters += line_chars
        starts += line_starts
        ends += line_ends
        offset += duration + gap
        if index + 1 < len(clips):
            characters += list(separator)
            starts += [ends[-1] if ends else offset] * len(separator)
            ends += [ends[-1] if ends else offset] * len(separator)
    alignment = {"characters": characters, "character_start_times_seconds": starts, "character_end_times_seconds": ends}
    alignment_file = out / "narration_alignment.json"
    alignment_file.write_text(
        json.dumps({"text": "".join(characters), "alignment": alignment, "narration_segment_timings": timings,
                    "voices": voices}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    total = _audio_seconds(narration_file)
    # The pause after a line belongs to its scene (the reaction on screen), so scenes cover the whole soundtrack
    # and shots are never stretched past their clips to fill silence.
    for timing, following in zip(timings, timings[1:]):
        timing["speech_end_seconds"] = timing["end_seconds"]
        timing["end_seconds"] = following["start_seconds"]
    if timings:
        timings[0]["start_seconds"] = 0.0
        timings[-1]["speech_end_seconds"] = timings[-1]["end_seconds"]
        timings[-1]["end_seconds"] = total
    alignment_file.write_text(
        json.dumps({"text": "".join(characters), "alignment": alignment, "narration_segment_timings": timings,
                    "voices": voices}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    result = {
        "narration_file": str(narration_file),
        "narration_provider": "ElevenLabs",
        "narration_model_used": model_id,
        "narration_alignment_file": str(alignment_file),
        "narration_alignment": alignment,
        "narration_segment_timings": timings,
        "scene_timings": timings,
        "actual_narration_seconds": total,
        # Dialogue lines are the story itself, so a long result is never shortened by rewriting them.
        "narration_feedback": "",
        "narration_target_words": 0,
        "narration_timing_attempts": 0,
        "cost_ledger": ledger.snapshot(),
    }
    if total > requested + 0.5:
        result["warnings"] = [
            *state.get("warnings", []),
            f"The dialogue takes {total:.1f}s; requested video length was {requested:g}s. The video will follow the dialogue.",
        ]
    return result


def create_narration(state: AgentState) -> dict[str, object]:
    segments = state.get("narration_segments") or []
    if segments and all(segment.get("speaker_id") for segment in segments):
        return _create_dialogue_narration(state, segments)
    out = _out_dir(state, "audio")
    out.mkdir(parents=True, exist_ok=True)
    narration_file = out / "narration.mp3"
    meta_file = out / "narration.json"
    alignment_file = out / "narration_alignment.json"

    voice_id = state.get("elevenlabs_voice_id") or os.getenv("ELEVENLABS_VOICE_ID") or DEFAULT_ELEVENLABS_VOICE_ID
    model_id = state.get("narration_model") or state.get("elevenlabs_tts_model") or os.getenv("ELEVENLABS_TTS_MODEL", DEFAULT_ELEVENLABS_TTS_MODEL)
    language = state.get("language") or (state.get("parsed_requirements") or {}).get("language") or ""
    language_code = "hi" if str(language).strip().casefold() in {"hindi", "hi"} else None
    items = _narration_items(state)
    if model_id not in LANGUAGE_CODE_MODELS:
        language_code = None
    text = _segment_separator().join(line for _, line in items)
    voice_settings = dict((PIPELINE_CONFIG.get("narration") or {}).get("voice_settings") or {})
    meta = {
        "text": text, "provider": "ElevenLabs", "voice_id": voice_id, "model_id": model_id,
        "language_code": language_code, "voice_settings": voice_settings, "timestamps": True,
    }

    if narration_file.exists() and meta_file.exists() and alignment_file.exists():
        if json.loads(meta_file.read_text(encoding="utf-8")) == meta:
            alignment_doc = json.loads(alignment_file.read_text(encoding="utf-8"))
            alignment = alignment_doc["alignment"]
            scene_timings = alignment_doc.get("narration_segment_timings") or alignment_doc.get("scene_timings") or _scene_timings(items, alignment)
            return _narration_result(state, narration_file, alignment_file, alignment, scene_timings)

    url = f"{ELEVENLABS_TTS_URL}/{voice_id}/with-timestamps?{parse.urlencode({'output_format': 'mp3_44100_128'})}"
    payload = {"text": text, "model_id": model_id}
    if voice_settings:
        payload["voice_settings"] = voice_settings
    if language_code:
        payload["language_code"] = language_code
    ledger = ledger_for(state)
    with paid_call(ledger, f"narration:{fingerprint(meta)}", "narration", "narration", narration_price(state, len(text))):
        data = _json_request(url, payload, _elevenlabs_headers())
    alignment = data.get("alignment") or data.get("normalized_alignment")
    if not data.get("audio_base64") or not alignment:
        raise RuntimeError("Narration agent needs ElevenLabs audio and alignment data.")

    scene_timings = _scene_timings(items, alignment)
    narration_file.write_bytes(base64.b64decode(data["audio_base64"]))
    meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    alignment_file.write_text(
        json.dumps({"text": text, "alignment": alignment, "narration_segment_timings": scene_timings}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        **_narration_result(state, narration_file, alignment_file, alignment, scene_timings),
        "cost_ledger": ledger.snapshot(),
    }
