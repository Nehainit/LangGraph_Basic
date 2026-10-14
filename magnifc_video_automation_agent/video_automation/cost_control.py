"""Per-video spending control: a budgeted cost ledger, paid-redo limits, and resumable provider jobs."""

import hashlib
import json
import logging
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from video_automation.model_catalog import VIDEO_TIERS, billed_seconds, model_entry, selected_model
from video_automation.prompts import PIPELINE_CONFIG


logger = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """A paid call would push this video past its budget, so it was not started."""


class PendingJobError(RuntimeError):
    """A paid provider job is still running; the next attempt resumes it instead of paying again."""


PERMANENT_STATUS_CODES = {400, 401, 403, 404, 413, 422}
PERMANENT_MARKERS = (
    "content policy", "content_policy", "safety system", "moderation", "nsfw", "prohibited",
    "insufficient_quota", "invalid_request", "unprocessable",
)


def budget() -> dict:
    return PIPELINE_CONFIG["budget"]


def is_permanent_error(error: BaseException) -> bool:
    """True when retrying the same request cannot succeed, so the attempt should not be repeated."""
    if isinstance(error, BudgetExceeded):
        return True
    if isinstance(error, PendingJobError):
        return False
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status in PERMANENT_STATUS_CODES
    text = str(error)
    match = re.search(r"\bHTTP (\d{3})\b", text)
    if match:
        return int(match.group(1)) in PERMANENT_STATUS_CODES
    return any(marker in text.casefold() for marker in PERMANENT_MARKERS)


class CostLedger:
    """Thread-safe record of paid calls for one video run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self.llm_usd = 0.0

    def sync(self, entries: list[dict]) -> None:
        with self._lock:
            for entry in entries:
                self._entries.setdefault(entry["key"], entry)

    def spent(self) -> float:
        return round(sum(entry["usd"] for entry in self._entries.values()) + self.llm_usd, 6)

    def charge(self, key: str, stage: str, item: str, usd: float, **details: Any) -> None:
        cap = budget().get("max_usd_per_video")
        with self._lock:
            if key in self._entries:
                return
            spent = self.spent()
            if cap is not None and spent + usd > float(cap) + 1e-9:
                raise BudgetExceeded(
                    f"Budget of ${float(cap):.2f} per video reached (${spent:.2f} spent); "
                    f"{stage} for {item} (${usd:.2f}) was not started."
                )
            self._entries[key] = {"key": key, "stage": stage, "item": item, "usd": round(usd, 6), **details}

    def refund(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._entries.values())


_ledgers: dict[str, CostLedger] = {}
_ledgers_lock = threading.Lock()


def ledger_for(state: dict) -> CostLedger:
    """Return the run's shared ledger, so parallel workers and background prefetches charge one budget."""
    run = str(state.get("thread_id") or Path(str(state.get("output_dir", "outputs"))) / str(state.get("topic", "")))
    with _ledgers_lock:
        if run not in _ledgers:
            warn_unpriced_models(state)
        ledger = _ledgers.setdefault(run, CostLedger())
    ledger.sync(state.get("cost_ledger") or [])
    ledger.llm_usd = round(sum(float(item.get("cost_usd") or 0) for item in state.get("llm_evaluations") or []), 6)
    return ledger


@contextmanager
def paid_call(ledger: CostLedger, key: str, stage: str, item: str, usd: float, **details: Any):
    """Charge before a paid call; refund it when the call fails without a billable job left running."""
    ledger.charge(key, stage, item, usd, **details)
    try:
        yield
    except PendingJobError:
        raise
    except BaseException:
        ledger.refund(key)
        raise


def image_price(state: dict, model_id: str | None = None) -> float:
    entry = model_entry("image", model_id) if model_id else selected_model(state, "image")
    cost = entry["cost"]
    return float(cost.get("quality_rates", {}).get(state.get("video_quality", "standard"), cost["amount"]))


def image_model_for_role(state: dict, role: str) -> dict:
    """The model for one image role (character_sheet, location_plate, shot_frame); see images.role_models."""
    selected = selected_model(state, "image")
    model_id = (PIPELINE_CONFIG["images"].get("role_models") or {}).get(selected["provider"], {}).get(role)
    return model_entry("image", model_id) if model_id else selected


def video_price(state: dict, seconds: float, entry: dict | None = None) -> float:
    """Price of one clip, billed at the provider's clip length for the requested seconds."""
    entry = entry or selected_model(state, "video")
    cost = entry["cost"]
    seconds = billed_seconds(entry, seconds)
    if cost["unit"] == "USD":
        return float(cost["amount"]) * seconds
    per_credit = budget().get("magnific_usd_per_credit")
    return float(cost["amount"]) * seconds * float(per_credit) if per_credit is not None else 0.0


def video_model_for_tier(state: dict, tier: str) -> dict | None:
    """The video model for a shot's tier; None means free FFmpeg motion (static)."""
    if tier not in VIDEO_TIERS:
        raise ValueError(f"Unknown video tier: {tier!r}")
    if tier == "static":
        return None
    selected = selected_model(state, "video")
    if tier == "premium":
        return selected
    model_id = (PIPELINE_CONFIG["video_tiers"].get("standard_model") or {}).get(selected["provider"])
    return model_entry("video", model_id) if model_id else selected


def warn_unpriced_models(state: dict) -> None:
    """Log every paid call the budget cap cannot see, so the cap is never silently blind."""
    unpriced = []
    for stage in ("image", "video", "narration"):
        try:
            entry = selected_model(state, stage)
        except ValueError:
            continue
        if entry["cost"]["unit"] != "USD" and budget().get("magnific_usd_per_credit") is None:
            unpriced.append(f"{stage} ({entry['id']}, billed in {entry['cost']['unit']})")
    if PIPELINE_CONFIG["sound"]["enabled"] and PIPELINE_CONFIG["sound"].get("ambience") and budget().get("elevenlabs_sound_usd_per_call") is None:
        unpriced.append("ElevenLabs ambience")
    if PIPELINE_CONFIG["sound"]["enabled"] and PIPELINE_CONFIG["sound"].get("music") and budget().get("elevenlabs_music_usd_per_minute") is None:
        unpriced.append("ElevenLabs music")
    if unpriced:
        logger.warning("These paid calls count as $0 toward the budget cap; set their rates in config/pipeline.yml: %s", ", ".join(unpriced))


def spend_summary(entries: list[dict], llm_usd: float = 0.0, timeline: list[dict] | None = None) -> dict:
    """Spend per category and the billed video seconds the edit threw away."""
    totals = {"video_usd": 0.0, "image_usd": 0.0, "audio_usd": 0.0}
    for entry in entries:
        key = {"video": "video_usd", "image": "image_usd"}.get(entry.get("stage"), "audio_usd")
        totals[key] += float(entry.get("usd") or 0)
    used = {
        item.get("shot_id"): max(0.0, float(item.get("timeline_end", 0)) - float(item.get("timeline_start", 0)))
        for item in timeline or []
    }
    billed = {}
    for entry in entries:
        if entry.get("stage") == "video" and entry.get("billed_seconds") is not None:
            billed[entry.get("shot_id") or entry["item"]] = billed.get(entry.get("shot_id") or entry["item"], 0) + float(entry["billed_seconds"])
    waste = sum(seconds - used.get(shot_id, 0.0) for shot_id, seconds in billed.items())
    frames: dict[str, int] = {}
    for entry in entries:
        if entry.get("stage") == "image" and entry.get("role") == "shot_frame":
            frames[entry["item"]] = frames.get(entry["item"], 0) + 1
    return {
        **{key: round(value, 4) for key, value in totals.items()},
        "llm_usd": round(llm_usd, 4),
        "video_billed_seconds": round(sum(billed.values()), 3),
        "video_used_seconds": round(sum(used.get(shot_id, 0.0) for shot_id in billed), 3),
        "video_waste_seconds": round(max(0.0, waste), 3),
        # Paid shot-frame redraws (QA, reviewer notes, Magnific fallback): each costs one more image.
        "shot_frame_redraws": sum(count - 1 for count in frames.values()),
    }


def narration_price(state: dict, characters: int) -> float:
    cost = selected_model(state, "narration")["cost"]
    return float(cost["amount"]) * characters / 1000


def sound_price(kind: str, seconds: float) -> float:
    if kind == "fal_music":
        rate = budget().get("fal_music_usd_per_minute")
        return float(rate) * seconds / 60 if rate is not None else 0.0
    if kind == "music":
        rate = budget().get("elevenlabs_music_usd_per_minute")
        return float(rate) * seconds / 60 if rate is not None else 0.0
    rate = budget().get("elevenlabs_sound_usd_per_call")
    return float(rate) if rate is not None else 0.0


def take_paid_redo(counts: dict, shot_id: str, kind: str) -> bool:
    """Count one automatic paid redo for a shot; False once the per-shot limit is used up."""
    limit = int(budget()["max_paid_redos_per_shot"][kind])
    used = int(counts.get(shot_id, {}).get(kind, 0))
    if used >= limit:
        return False
    counts[shot_id] = {**counts.get(shot_id, {}), kind: used + 1}
    return True


def fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def file_identity(path: str | Path) -> list:
    path = Path(path)
    stat = path.stat() if path.is_file() else None
    return [str(path), stat.st_size if stat else None, stat.st_mtime_ns if stat else None]


def read_job(job_file: Path, request_fingerprint: str) -> dict | None:
    """Return a saved provider job for the same request, if one is still pending."""
    try:
        job = json.loads(job_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return job if job.get("fingerprint") == request_fingerprint else None


def write_job(job_file: Path, request_fingerprint: str, **details: Any) -> None:
    job_file.write_text(json.dumps({"fingerprint": request_fingerprint, **details}, indent=2), encoding="utf-8")


def clear_job(job_file: Path) -> None:
    job_file.unlink(missing_ok=True)
