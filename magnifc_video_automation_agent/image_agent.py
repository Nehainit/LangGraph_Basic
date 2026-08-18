import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib import error, request

from dotenv import load_dotenv

from prompts import character_sheet_prompt, image_prompt
from schema import AgentState


load_dotenv()
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

MAGNIFIC_TEXT_TO_IMAGE_URL = "https://api.magnific.com/v1/ai/text-to-image"
MAGNIFIC_SEEDREAM_EDIT_URL = "https://api.magnific.com/v1/ai/text-to-image/seedream-v4-edit"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "video"


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Set {name} in your environment or .env file.")
    return value.strip()


def _out_dir(state: AgentState, *parts: str) -> Path:
    return Path(state.get("output_dir", "outputs")) / _slug(state["topic"]) / Path(*parts)


def _characters(characters: str | list[str]) -> list[str]:
    if isinstance(characters, str):
        return [character.strip() for character in characters.split(",") if character.strip()]
    return characters


def _magnific_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-magnific-api-key": _env("MAGNIFIC_API_KEY"),
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


def _get_bytes(url: str) -> bytes:
    with request.urlopen(url, timeout=120) as response:
        return response.read()


def _save_generated_image(value: str, image_file: Path) -> None:
    if value.startswith(("http://", "https://")):
        image_file.write_bytes(_get_bytes(value))
    else:
        image_file.write_bytes(base64.b64decode(value.split(",", 1)[-1]))


def _image_as_base64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _image_value(response: dict[str, Any]) -> str:
    data = response.get("data")
    if isinstance(data, list) and data:
        return data[0].get("base64") or data[0].get("url")
    if isinstance(data, dict):
        generated = data.get("generated") or []
        if generated:
            return generated[0]
    raise RuntimeError("Magnific did not return an image.")


def create_character_sheets(state: AgentState) -> dict[str, list[str]]:
    characters = _characters(state["characters"])
    if not characters:
        raise RuntimeError("Image agent needs characters for reference sheets.")

    out = _out_dir(state, "characters")
    out.mkdir(parents=True, exist_ok=True)
    image_file = out / "characters_reference.png"
    if not image_file.exists():
        response = _json_request(
            MAGNIFIC_TEXT_TO_IMAGE_URL,
            {
                "prompt": character_sheet_prompt(characters),
                "negative_prompt": "text, watermark, logo, extra limbs, distorted face, blurry",
                "guidance_scale": 2,
                "num_images": 1,
                "image": {"size": "square_1_1"},
                "filter_nsfw": True,
            },
            _magnific_headers(),
        )
        _save_generated_image(_image_value(response), image_file)
        time.sleep(1)

    return {"character_reference_files": [str(image_file)]}


def create_scene_images(state: AgentState) -> dict[str, list[str]]:
    storyboard = state.get("storyboard")
    if not storyboard:
        raise RuntimeError("Image agent needs storyboard scenes.")
    reference_files = state.get("character_reference_files") or create_character_sheets(state)["character_reference_files"]
    references = [_image_as_base64(path) for path in reference_files[:5]]

    out = _out_dir(state, "images")
    out.mkdir(parents=True, exist_ok=True)
    image_files = []
    for scene in storyboard:
        image_file = out / f"scene_{scene['scene_number']:02}.png"
        if not image_file.exists():
            response = _json_request(
                MAGNIFIC_SEEDREAM_EDIT_URL,
                {
                    "prompt": image_prompt(scene),
                    "aspect_ratio": "social_story_9_16",
                    "guidance_scale": 2,
                    "reference_images": references,
                },
                _magnific_headers(),
            )
            _save_generated_image(_image_value(response), image_file)
            time.sleep(1)
        image_files.append(str(image_file))

    return {"character_reference_files": reference_files, "image_files": image_files}
