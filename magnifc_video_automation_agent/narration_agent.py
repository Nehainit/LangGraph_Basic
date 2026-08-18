import json
import os
import re
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from dotenv import load_dotenv

from schema import AgentState


load_dotenv()
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DEFAULT_ELEVENLABS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
DEFAULT_ELEVENLABS_TTS_MODEL = "eleven_multilingual_v2"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"


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
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": _env("ELEVENLABS_API_KEY"),
    }


def _bytes_request(url: str, payload: dict[str, Any], headers: dict[str, str]) -> bytes:
    req = request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=180) as response:
            return response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed: HTTP {exc.code} {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"POST {url} failed: {exc.reason}") from exc


def _narration_text(state: AgentState) -> str:
    storyboard = state.get("storyboard") or []
    lines = [str(scene.get("narration", "")).strip() for scene in storyboard if scene.get("narration")]
    if lines:
        return "\n".join(lines)
    if state.get("story"):
        return state["story"]
    raise RuntimeError("Narration agent needs story or storyboard narration.")


def create_narration(state: AgentState) -> dict[str, str]:
    out = _out_dir(state, "audio")
    out.mkdir(parents=True, exist_ok=True)
    narration_file = out / "narration.mp3"
    meta_file = out / "narration.json"

    voice_id = state.get("elevenlabs_voice_id", DEFAULT_ELEVENLABS_VOICE_ID)
    model_id = state.get("elevenlabs_tts_model", DEFAULT_ELEVENLABS_TTS_MODEL)
    text = _narration_text(state)
    meta = {"text": text, "voice_id": voice_id, "model_id": model_id}

    if narration_file.exists() and meta_file.exists():
        if json.loads(meta_file.read_text(encoding="utf-8")) == meta:
            return {"narration_file": str(narration_file)}

    url = f"{ELEVENLABS_TTS_URL}/{voice_id}?{parse.urlencode({'output_format': 'mp3_44100_128'})}"
    audio = _bytes_request(url, {"text": text, "model_id": model_id}, _elevenlabs_headers())
    narration_file.write_bytes(audio)
    meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"narration_file": str(narration_file)}
