"""Per-video spending control: a budgeted cost ledger, paid-redo limits, and resumable provider jobs."""

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from video_automation.model_catalog import selected_model
from video_automation.prompts import PIPELINE_CONFIG


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

    def charge(self, key: str, stage: str, item: str, usd: float) -> None:
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
            self._entries[key] = {"key": key, "stage": stage, "item": item, "usd": round(usd, 6)}

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
        ledger = _ledgers.setdefault(run, CostLedger())
    ledger.sync(state.get("cost_ledger") or [])
    ledger.llm_usd = round(sum(float(item.get("cost_usd") or 0) for item in state.get("llm_evaluations") or []), 6)
    return ledger


@contextmanager
def paid_call(ledger: CostLedger, key: str, stage: str, item: str, usd: float):
    """Charge before a paid call; refund it when the call fails without a billable job left running."""
    ledger.charge(key, stage, item, usd)
    try:
        yield
    except PendingJobError:
        raise
    except BaseException:
        ledger.refund(key)
        raise


def image_price(state: dict) -> float:
    cost = selected_model(state, "image")["cost"]
    return float(cost.get("quality_rates", {}).get(state.get("video_quality", "standard"), cost["amount"]))


def video_price(state: dict, seconds: float) -> float:
    cost = selected_model(state, "video")["cost"]
    if cost["unit"] == "USD":
        return float(cost["amount"]) * seconds
    per_credit = budget().get("magnific_usd_per_credit")
    return float(cost["amount"]) * seconds * float(per_credit) if per_credit is not None else 0.0


def narration_price(state: dict, characters: int) -> float:
    cost = selected_model(state, "narration")["cost"]
    return float(cost["amount"]) * characters / 1000


def sound_price(kind: str, seconds: float) -> float:
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
