"""Per-user character library: saved character details, a reference sheet, and an emotion board for each.

Layout on disk: library/<user>/<character id>/character.json, versioned sheet_v###.png and emotions_v###.png, and the
user's uploaded reference images as ref_v###.png. Earlier versions stay on disk; character.json points at the current ones.
"""

import base64
import binascii
import io
import json
import logging
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from video_automation.agents.image_agent import (
    _character_sheet_reference_files,
    _generate_unpriced_provider_image,
    _next_version,
)
from video_automation.auth import require_user
from video_automation.cost_control import PendingJobError, image_price
from video_automation.prompts import EMOTIONS, VISUAL_STYLES, emotion_board_prompt, library_sheet_prompt


LIBRARY_ROOT = Path(__file__).resolve().parents[1] / "library"
# fal.ai first, like the studio; image_agent redraws on Magnific when a fal call fails.
IMAGE_STATE = {"image_provider": "fal"}
BOARD_SIZE = "16:9"
CHARACTER_ID = re.compile(r"^[0-9a-f]{12}$")
FILE_NAME = re.compile(r"^(sheet|emotions|ref)_v\d{3}\.png$")
# Three keeps every drawing within Magnific's reference limit when fal fails over.
MAX_REFERENCES = 3
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_REFERENCE_SIDE = 2048
logger = logging.getLogger(__name__)
router = APIRouter()
_busy: set[str] = set()
_busy_lock = threading.Lock()


class CharacterDetails(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(min_length=1, max_length=200)
    appearance: str = Field(default="", max_length=1200)
    outfit: str = Field(default="", max_length=600)
    props: str = Field(default="", max_length=300)
    visual_style: Literal["realistic", "cinematic", "illustrated", "3d_animation", "anime"] = "illustrated"


class RedrawRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    note: str = Field(default="", max_length=500)
    # Without a note, an existing sheet is redrawn from the details keeping its face; fresh draws a new look.
    fresh: bool = False


class ReferenceUpload(BaseModel):
    # A data URL or bare base64; the browser shrinks images before sending them.
    image: str = Field(min_length=1, max_length=MAX_UPLOAD_BYTES * 4 // 3 + 100)


def _user_dir(user_id: str | None) -> Path:
    # require_user returns None when sign-in is off, so local development shares one library.
    return LIBRARY_ROOT / re.sub(r"[^A-Za-z0-9_-]", "_", user_id or "local")


def _character_dir(user_id: str | None, character_id: str) -> Path:
    folder = _user_dir(user_id) / character_id
    if not CHARACTER_ID.match(character_id) or not (folder / "character.json").is_file():
        raise HTTPException(status_code=404, detail="Character not found.")
    return folder


def _read(folder: Path) -> dict:
    return json.loads((folder / "character.json").read_text(encoding="utf-8"))


def _write(folder: Path, record: dict) -> dict:
    record["updated_at"] = time.time()
    temporary = folder / "character.json.tmp"
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(folder / "character.json")
    return record


def _public(record: dict) -> dict:
    def url(name: str | None) -> str | None:
        return f"/api/characters/{record['id']}/files/{name}" if name else None

    return {
        **record,
        "sheet_url": url(record.get("sheet")),
        "emotions_url": url(record.get("emotions")),
        "references": [{"name": name, "url": url(name)} for name in record.get("references", [])],
    }


@contextmanager
def _drawing(folder: Path):
    """One drawing per character at a time, so two clicks never pay twice or race on character.json."""
    key = str(folder)
    with _busy_lock:
        if key in _busy:
            raise HTTPException(status_code=409, detail="This character is already being drawn.")
        _busy.add(key)
    try:
        yield
    finally:
        with _busy_lock:
            _busy.discard(key)


def _draw(prompt: str, image_file: Path, references: list[str], labels: list[str], note: str) -> None:
    _generate_unpriced_provider_image(IMAGE_STATE, prompt, BOARD_SIZE, image_file, references, note, reference_labels=labels)


def _draw_or_fail(prompt: str, image_file: Path, references: list[str], labels: list[str], note: str) -> None:
    from video_automation.cost_control import image_price
    from video_automation.guard import DailyCapReached, charge_today, refund_today

    # Library drawings are paid like film frames, so they count toward the studio's daily cap.
    key = f"character:{image_file}"
    try:
        charge_today(key, image_price(IMAGE_STATE))
    except DailyCapReached as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    try:
        _draw(prompt, image_file, references, labels, note)
    except PendingJobError as exc:
        # Asking again with the same request resumes the provider job instead of paying for a new one.
        raise HTTPException(status_code=504, detail="Still drawing. Try again in a minute to pick up the same drawing.") from exc
    except Exception as exc:
        refund_today(key)
        logger.exception("Character drawing failed for %s", image_file)
        raise HTTPException(status_code=502, detail=f"The drawing failed: {exc}") from exc


def _save_reference(data: str, target: Path) -> None:
    try:
        raw = base64.b64decode(data.split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="That upload is not a valid image.") from exc
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Images must be 10 MB or smaller.")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((MAX_REFERENCE_SIDE, MAX_REFERENCE_SIDE))
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")
                flat = Image.new("RGB", image.size, "white")
                flat.paste(image, mask=image.getchannel("A"))
                image = flat
            # Re-encoding drops EXIF, so a phone photo's GPS location is never stored or sent to the image model.
            image.convert("RGB").save(target, "PNG")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="That file is not a supported image.") from exc


def saved_character_names(user_id: str | None) -> list[str]:
    """Names of the user's characters that have a drawn sheet, the ones a story can @-mention."""
    folder = _user_dir(user_id)
    records = [_read(path.parent) for path in folder.glob("*/character.json")] if folder.is_dir() else []
    return sorted(record["name"] for record in records if record.get("sheet"))


def film_characters(user_id: str | None, character_ids: list[str]) -> list[dict]:
    """The user's @-mentioned characters as the film pipeline needs them; each must already have a sheet."""
    characters = []
    for character_id in dict.fromkeys(character_ids):
        try:
            folder = _character_dir(user_id, character_id)
        except HTTPException as exc:
            raise HTTPException(status_code=400, detail="A mentioned character is no longer in your library.") from exc
        record = _read(folder)
        if not record.get("sheet"):
            raise HTTPException(status_code=400, detail=f"Draw {record['name']}'s character sheet before using them in a film.")
        details = [record["kind"], *(f"{label}: {record[key]}" for label, key in (
            ("Appearance", "appearance"), ("Outfit", "outfit"), ("Props", "props"),
        ) if record.get(key))]
        characters.append({
            "character_id": record["id"],
            "name": record["name"],
            "description": ". ".join(part.rstrip(". ") for part in details) + ".",
            "visual_style": record["visual_style"],
            "sheet_file": str(folder / record["sheet"]),
            "emotions_file": str(folder / record["emotions"]) if record.get("emotions") else None,
        })
    return characters


@router.get("/api/characters")
def list_characters(user_id: str | None = Depends(require_user)) -> dict:
    folder = _user_dir(user_id)
    records = [_read(path.parent) for path in folder.glob("*/character.json")] if folder.is_dir() else []
    records.sort(key=lambda record: record.get("updated_at", 0), reverse=True)
    return {
        "characters": [_public(record) for record in records],
        "emotions": list(EMOTIONS),
        "styles": list(VISUAL_STYLES),
        "image_cost_usd": image_price(IMAGE_STATE),
    }


@router.post("/api/characters")
def create_character(details: CharacterDetails, user_id: str | None = Depends(require_user)) -> dict:
    character_id = uuid.uuid4().hex[:12]
    folder = _user_dir(user_id) / character_id
    folder.mkdir(parents=True)
    record = {
        "id": character_id,
        **details.model_dump(),
        "sheet": None,
        "emotions": None,
        "references": [],
        "sheet_stale": False,
        "emotions_stale": False,
        "spent_usd": 0.0,
        "created_at": time.time(),
    }
    return _public(_write(folder, record))


@router.get("/api/characters/{character_id}")
def get_character(character_id: str, user_id: str | None = Depends(require_user)) -> dict:
    return _public(_read(_character_dir(user_id, character_id)))


@router.put("/api/characters/{character_id}")
def update_character(character_id: str, details: CharacterDetails, user_id: str | None = Depends(require_user)) -> dict:
    folder = _character_dir(user_id, character_id)
    with _drawing(folder):
        record = _read(folder)
        changed = any(record.get(key) != value for key, value in details.model_dump().items())
        record.update(details.model_dump())
        if changed and record.get("sheet"):
            record["sheet_stale"] = True
        return _public(_write(folder, record))


@router.delete("/api/characters/{character_id}")
def delete_character(character_id: str, user_id: str | None = Depends(require_user)) -> dict:
    folder = _character_dir(user_id, character_id)
    with _drawing(folder):
        shutil.rmtree(folder)
    return {"deleted": character_id}


def use_quota(user_id: str | None, kind: str) -> None:
    """The account's daily drawing limit (video_automation/guard.py imports this module, so it loads late)."""
    from video_automation.guard import use_quota as count_use

    count_use(user_id, kind)


@router.post("/api/characters/{character_id}/sheet")
def draw_sheet(character_id: str, request: RedrawRequest, user_id: str | None = Depends(require_user)) -> dict:
    folder = _character_dir(user_id, character_id)
    use_quota(user_id, "drawings")
    with _drawing(folder):
        record = _read(folder)
        current = folder / record["sheet"] if record.get("sheet") else None
        if request.note and not current:
            raise HTTPException(status_code=400, detail="Draw the character first, then describe a change.")
        uploads = [str(folder / name) for name in record.get("references", [])]
        # A change note edits the current sheet; otherwise uploaded images define the look, then the current sheet.
        from_current = bool(current) and (bool(request.note) or (not uploads and not request.fresh))
        if from_current:
            references, labels = [str(current)], [f"current reference sheet of {record['name']}"]
        elif uploads:
            references = uploads
            labels = [f"reference image {index} of {record['name']}, uploaded by the user" for index in range(1, len(uploads) + 1)]
        else:
            references = _character_sheet_reference_files()
            labels = ["layout reference only; do not copy its person"] * len(references)
        prompt = library_sheet_prompt(
            record, note=request.note, from_current=from_current,
            layout_reference=not from_current and not uploads and bool(references),
            uploaded_references=0 if from_current else len(uploads),
        )
        sheet = _next_version(folder, "sheet")
        _draw_or_fail(prompt, sheet, references, labels, request.note)
        record.update({
            "sheet": sheet.name,
            "sheet_stale": False,
            # The board shows the old face until it is redrawn from the new sheet.
            "emotions_stale": bool(record.get("emotions")),
            "spent_usd": round(record.get("spent_usd", 0) + image_price(IMAGE_STATE), 4),
        })
        return _public(_write(folder, record))


@router.post("/api/characters/{character_id}/emotions")
def draw_emotions(character_id: str, request: RedrawRequest, user_id: str | None = Depends(require_user)) -> dict:
    folder = _character_dir(user_id, character_id)
    use_quota(user_id, "drawings")
    with _drawing(folder):
        record = _read(folder)
        if not record.get("sheet"):
            raise HTTPException(status_code=400, detail="Draw the character sheet before the emotion board.")
        references, labels = [str(folder / record["sheet"])], [f"approved reference sheet of {record['name']}"]
        # A change note edits the current board; otherwise the board is drawn fresh from the sheet.
        from_current = bool(request.note and record.get("emotions") and not request.fresh)
        if from_current:
            references.append(str(folder / record["emotions"]))
            labels.append("current expression sheet")
        board = _next_version(folder, "emotions")
        _draw_or_fail(emotion_board_prompt(record, note=request.note, from_current=from_current), board, references, labels, request.note)
        record.update({
            "emotions": board.name,
            "emotions_stale": False,
            "spent_usd": round(record.get("spent_usd", 0) + image_price(IMAGE_STATE), 4),
        })
        return _public(_write(folder, record))


@router.post("/api/characters/{character_id}/references")
def add_reference(character_id: str, upload: ReferenceUpload, user_id: str | None = Depends(require_user)) -> dict:
    folder = _character_dir(user_id, character_id)
    with _drawing(folder):
        record = _read(folder)
        references = record.get("references", [])
        if len(references) >= MAX_REFERENCES:
            raise HTTPException(status_code=400, detail=f"A character can have at most {MAX_REFERENCES} reference images.")
        target = _next_version(folder, "ref")
        _save_reference(upload.image, target)
        record["references"] = [*references, target.name]
        if record.get("sheet"):
            record["sheet_stale"] = True
        return _public(_write(folder, record))


@router.delete("/api/characters/{character_id}/references/{name}")
def remove_reference(character_id: str, name: str, user_id: str | None = Depends(require_user)) -> dict:
    folder = _character_dir(user_id, character_id)
    with _drawing(folder):
        record = _read(folder)
        if name not in record.get("references", []):
            raise HTTPException(status_code=404, detail="Reference image not found.")
        (folder / name).unlink(missing_ok=True)
        record["references"] = [item for item in record["references"] if item != name]
        if record.get("sheet"):
            record["sheet_stale"] = True
        return _public(_write(folder, record))


@router.get("/api/characters/{character_id}/files/{name}")
def character_file(character_id: str, name: str, user_id: str | None = Depends(require_user)) -> FileResponse:
    folder = _character_dir(user_id, character_id)
    if not FILE_NAME.match(name) or not (folder / name).is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(folder / name)
