"""Per-user drafts for My films: unfinished stories, and films paused at a review step.

Stored as library/<user>/drafts.json beside the character library, so drafts follow the account to any device.
A paused film's frames and plans stay in the LangGraph checkpoint; its draft only remembers which thread to reopen.
"""

import json
import re
import threading
import time
import uuid
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from video_automation.auth import require_user
from video_automation.character_library import _user_dir


DRAFT_ID = re.compile(r"^[0-9a-f]{12}$")
MAX_STORY_DRAFTS = 50
router = APIRouter()
_lock = threading.Lock()


class StoryDraft(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    topic: str = Field(min_length=1, max_length=2000)
    # The studio's form settings (duration, style, models…), applied back to the form as they were saved.
    settings: dict[str, str | int | list[str]] = Field(default_factory=dict, max_length=20)


def _file(user_id: str | None):
    return _user_dir(user_id) / "drafts.json"


def _load(user_id: str | None) -> list[dict]:
    path = _file(user_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


@contextmanager
def _editing(user_id: str | None):
    """Read, change, and write one user's drafts under a lock; nothing is written if the change raises."""
    with _lock:
        drafts = _load(user_id)
        yield drafts
        path = _file(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(drafts, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def _find(drafts: list[dict], draft_id: str, kind: str | None = None) -> dict:
    draft = next((item for item in drafts if item["id"] == draft_id), None) if DRAFT_ID.match(draft_id) else None
    if draft is None or (kind and draft["kind"] != kind):
        raise HTTPException(status_code=404, detail="Draft not found.")
    return draft


def find_draft(user_id: str | None, draft_id: str) -> dict:
    return _find(_load(user_id), draft_id)


def _poster(response: dict) -> str | None:
    artifacts = response.get("artifacts") or {}
    frames = response.get("image_urls") or artifacts.get("image_urls") or []
    return next(filter(None, frames), None) or artifacts.get("character_board_url")


def record_film(user_id: str | None, response: dict, request: dict | None = None, story_draft_id: str | None = None) -> None:
    """Keep My films in step with a film: listed while it waits on a review, dropped once it moves past reviews.

    `request` is given when the film starts; a story draft it was made from is replaced by the film itself.
    """
    thread_id = response.get("thread_id")
    paused = str(response.get("status", "")).endswith("_review")
    with _editing(user_id) as drafts:
        if story_draft_id and (paused or response.get("video_url")):
            drafts[:] = [item for item in drafts if not (item["id"] == story_draft_id and item["kind"] == "story")]
        existing = next((item for item in drafts if item.get("thread_id") == thread_id), None)
        if not paused:
            drafts[:] = [item for item in drafts if item is not existing]
            return
        if existing is None:
            if request is None:
                return  # A film started before drafts were kept; there is no brief to show it with.
            existing = {"id": uuid.uuid4().hex[:12], "kind": "film", "thread_id": thread_id, "request": request, "created_at": time.time()}
            drafts.insert(0, existing)
        existing.update(status=response["status"], poster=_poster(response), updated_at=time.time())


def forget_film(user_id: str | None, draft_id: str) -> None:
    with _editing(user_id) as drafts:
        drafts[:] = [item for item in drafts if item["id"] != draft_id]


@router.get("/api/drafts")
def list_drafts(user_id: str | None = Depends(require_user)) -> dict:
    return {"drafts": sorted(_load(user_id), key=lambda item: item["updated_at"], reverse=True)}


@router.post("/api/drafts")
def create_draft(draft: StoryDraft, user_id: str | None = Depends(require_user)) -> dict:
    with _editing(user_id) as drafts:
        if sum(item["kind"] == "story" for item in drafts) >= MAX_STORY_DRAFTS:
            raise HTTPException(status_code=409, detail=f"You have {MAX_STORY_DRAFTS} story drafts. Delete one to save another.")
        now = time.time()
        record = {"id": uuid.uuid4().hex[:12], "kind": "story", **draft.model_dump(), "created_at": now, "updated_at": now}
        drafts.insert(0, record)
    return record


@router.put("/api/drafts/{draft_id}")
def update_draft(draft_id: str, draft: StoryDraft, user_id: str | None = Depends(require_user)) -> dict:
    with _editing(user_id) as drafts:
        record = _find(drafts, draft_id, kind="story")
        record.update(**draft.model_dump(), updated_at=time.time())
    return record


@router.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: str, user_id: str | None = Depends(require_user)) -> dict:
    with _editing(user_id) as drafts:
        record = _find(drafts, draft_id)
        drafts.remove(record)
    return {"deleted": draft_id}
