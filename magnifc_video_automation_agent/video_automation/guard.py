"""Keeps a deployed studio from spending its owner's provider credits: daily caps, per-account limits, film ownership.

- A daily spending cap across every account (DAILY_SPEND_LIMIT_USD, or budget.max_usd_per_day in pipeline.yml):
  every paid call is counted in a per-day file, and calls stop once the day's total would pass the cap.
- Per-account daily limits on films, character drawings, and story-writer messages (MAX_FILMS_PER_DAY,
  MAX_DRAWINGS_PER_DAY, MAX_STORY_MESSAGES_PER_DAY). They apply to signed-in accounts only.
- Film ownership: a film records the account that made it, and other accounts get "not found".

Days are UTC. With sign-in off (local development) the per-account limits and ownership checks do nothing.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from video_automation.character_library import _user_dir
from video_automation.cost_control import BudgetExceeded
from video_automation.prompts import PIPELINE_CONFIG


ROOT = Path(__file__).resolve().parents[1]
LIMIT_DEFAULTS = {"films": ("MAX_FILMS_PER_DAY", 5), "drawings": ("MAX_DRAWINGS_PER_DAY", 20), "story_messages": ("MAX_STORY_MESSAGES_PER_DAY", 100)}
LIMIT_NAMES = {"films": "films", "drawings": "character drawings", "story_messages": "story-writer messages"}
_spend_lock = threading.Lock()
_usage_lock = threading.Lock()
_owners: dict[str, str | None] = {}


class DailyCapReached(BudgetExceeded):
    """Today's spending cap would be passed, so the paid call was not started."""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------- Daily spending cap ----------
def daily_limit_usd() -> float | None:
    value = os.getenv("DAILY_SPEND_LIMIT_USD")
    if value is None or value.strip() == "":
        value = (PIPELINE_CONFIG.get("budget") or {}).get("max_usd_per_day")
    limit = float(value) if value not in (None, "") else 0.0
    return limit if limit > 0 else None


def _spend_file(day: str) -> Path:
    # Deployments point SPEND_DIR at a mounted volume so the day's total survives a restart.
    return Path(os.getenv("SPEND_DIR") or ROOT / "data" / "spend") / f"{day}.json"


def _read_spend(day: str) -> dict:
    path = _spend_file(day)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"total_usd": 0.0, "calls": {}}


def _write_spend(day: str, spend: dict) -> None:
    path = _spend_file(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(spend, indent=2), encoding="utf-8")
    temporary.replace(path)


def charge_today(key: str, usd: float) -> None:
    """Count one paid call against today's cap, once per key; refuse it when the cap would be passed."""
    day = _today()
    with _spend_lock:
        spend = _read_spend(day)
        if key in spend["calls"]:
            return
        limit = daily_limit_usd()
        if limit is not None and spend["total_usd"] + usd > limit + 1e-9:
            raise DailyCapReached(
                f"Today's spending cap of ${limit:.2f} is reached (${spend['total_usd']:.2f} spent). "
                "Paid generation resumes tomorrow, or raise DAILY_SPEND_LIMIT_USD."
            )
        spend["calls"][key] = {"usd": round(usd, 6), "at": time.time()}
        spend["total_usd"] = round(spend["total_usd"] + usd, 6)
        _write_spend(day, spend)


def refund_today(key: str) -> None:
    """A call that failed without a billable job is taken back off today's total."""
    day = _today()
    with _spend_lock:
        spend = _read_spend(day)
        call = spend["calls"].pop(key, None)
        if call:
            spend["total_usd"] = round(max(0.0, spend["total_usd"] - call["usd"]), 6)
            _write_spend(day, spend)


def spent_today() -> float:
    return _read_spend(_today())["total_usd"]


# ---------- Per-account daily limits ----------
def _limit(kind: str) -> int:
    name, default = LIMIT_DEFAULTS[kind]
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def use_quota(user_id: str | None, kind: str) -> None:
    """Count one use of a paid feature for today; refuse it past the account's daily limit (0 means unlimited)."""
    limit = _limit(kind)
    if user_id is None or limit <= 0:
        return
    path = _user_dir(user_id) / "usage.json"
    day = _today()
    with _usage_lock:
        usage = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        today = {key: value for key, value in usage.items() if key == day}.get(day, {})
        if today.get(kind, 0) >= limit:
            raise HTTPException(status_code=429, detail=f"You've reached today's limit of {limit} {LIMIT_NAMES[kind]}. It resets at midnight UTC.")
        today[kind] = today.get(kind, 0) + 1
        path.parent.mkdir(parents=True, exist_ok=True)
        # Only today's counts are kept.
        path.write_text(json.dumps({day: today}, indent=2), encoding="utf-8")


# ---------- Film ownership ----------
def _legacy_films(user_id: str) -> set[str]:
    """Films made before films recorded their owner: the account's Edits list and paused-film drafts."""
    folder = _user_dir(user_id)
    films = set(json.loads((folder / "films.json").read_text(encoding="utf-8"))) if (folder / "films.json").is_file() else set()
    if (folder / "drafts.json").is_file():
        films |= {item.get("thread_id") for item in json.loads((folder / "drafts.json").read_text(encoding="utf-8")) if item.get("thread_id")}
    return films


def owns(user_id: str | None, thread_id: str, values: dict | None) -> bool:
    if user_id is None:
        return True
    owner = (values or {}).get("owner_id")
    if owner:
        return owner == user_id
    return thread_id in _legacy_films(user_id)


def require_owner(user_id: str | None, thread_id: str, values: dict | None) -> None:
    """Another account's film looks exactly like a missing one."""
    if not owns(user_id, thread_id, values):
        raise HTTPException(status_code=404, detail="Film not found.")


def owner_of(thread_id: str, values_of) -> str | None:
    """The account that made a film, cached because every image and clip of a film asks."""
    if thread_id not in _owners:
        _owners[thread_id] = (values_of(thread_id) or {}).get("owner_id")
    return _owners[thread_id]
