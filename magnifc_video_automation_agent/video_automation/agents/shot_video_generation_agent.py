import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from video_automation.artifact_store import public_artifact_url
from video_automation.cost_control import is_permanent_error, ledger_for, take_paid_redo
from video_automation.agents.editor_agent import _clip, _resolution
from video_automation.agents.image_agent import _env, _generate_scene_video_file, _out_dir
from video_automation.prompts import SHOT_VIDEO_GENERATION_CONFIG
from video_automation.schema import AgentState
from video_automation.model_catalog import LEGACY_PROVIDER_MODELS, selected_model


def _video_provider(state: AgentState) -> str:
    return "fal" if selected_model(state, "video")["provider"] == "fal.ai" else "magnific"


def _failure_type(error: str) -> str:
    lowered = error.casefold()
    return "invalid_generation_output" if any(term in lowered for term in ("did not return", "empty", "missing", "corrupt", "unreadable")) else "video_generation_error"


def _record(state: AgentState, shot: dict, image_file: str | None, requested: float, generated: float | None, attempt: int, video_file: str | None, error: str | None) -> dict:
    return {
        "shot_id": shot["shot_id"],
        "scene_id": shot["scene_id"],
        "generation_status": "success" if video_file else "failed",
        "source_image_file": image_file,
        "video_file": video_file,
        "requested_duration_seconds": requested,
        "generated_duration_seconds": generated if video_file else None,
        "provider": _video_provider(state),
        "model_used": selected_model(state, "video")["id"],
        "selected_model_id": selected_model(state, "video")["id"],
        "aspect_ratio": state.get("aspect_ratio", SHOT_VIDEO_GENERATION_CONFIG["default_aspect_ratio"]),
        "video_quality": state.get("video_quality", "standard"),
        "generation_attempt": attempt,
        "failure_type": None if video_file else _failure_type(error or ""),
        "error": error,
    }


def _worker_count(job_count: int, provider: str) -> int:
    default = 5 if provider == "fal" else 3
    limit = 5 if provider == "fal" else 4
    try:
        configured = int(os.getenv("VIDEO_GENERATION_WORKERS", str(default)))
    except ValueError:
        configured = default
    return min(job_count, max(1, min(limit, configured)))


def _fallback_motion(plan: dict) -> str:
    camera = plan.get("camera_motion") or {}
    return {
        "subtle_push_in": "zoom_in",
        "subtle_pull_back": "zoom_out",
        "slow_pan": "pan_right",
        "gentle_tilt": "tilt_up",
        "small_lateral_tracking": "pan_right",
    }.get(camera.get("type"), "static")


def _generate_job(args: tuple) -> tuple[dict, dict | None, str | None]:
    state, shot, image_file, plan, out, index, round_number, provider_duration, start_image_url, provider_aspect_ratio, max_attempts = args
    shot_id = shot["shot_id"]
    candidate_id = f"{shot_id}-c{round_number + 1}"
    seed = int(SHOT_VIDEO_GENERATION_CONFIG["seed_base"]) * (round_number + 1) + index
    video_file = out / f"{candidate_id}.mp4"
    started = time.monotonic()
    worker = threading.current_thread().name
    prefix = f"[pipeline:{state.get('thread_id', '-')}] [video-worker:{worker}]"
    print(f"{prefix} START {shot_id} candidate={candidate_id}", flush=True)
    error = ""
    result = None
    attempt = 0
    scene = {**shot, "duration_seconds": provider_duration}
    for attempt in range(1, max_attempts + 1):
        try:
            result = _generate_scene_video_file(
                state,
                scene,
                image_file,
                video_file,
                plan["video_prompt"],
                start_image_url,
                plan["negative_prompt"],
                float(SHOT_VIDEO_GENERATION_CONFIG["cfg_scale"]),
                provider_aspect_ratio,
                bool(SHOT_VIDEO_GENERATION_CONFIG["generate_audio"]),
            )
            if not Path(result).is_file() or Path(result).stat().st_size == 0:
                raise RuntimeError("Provider returned an empty video asset.")
            break
        except Exception as exc:
            result, error = None, str(exc)
            if is_permanent_error(exc):
                # Content-policy, invalid-request, and budget failures repeat on every attempt.
                break
    provider_used = _video_provider(state)
    if not result and provider_used == "fal":
        # fal.ai is the default video provider; Magnific animates the shot when fal fails.
        magnific_state = {**state, "video_model": LEGACY_PROVIDER_MODELS["magnific"]["video"]}
        magnific_image_url = public_artifact_url(magnific_state, image_file) if os.getenv("MINIO_PUBLIC_ENDPOINT") else None
        if magnific_image_url:
            print(f"{prefix} fal.ai failed for {shot_id}; retrying on Magnific: {error}", flush=True)
            try:
                result = _generate_scene_video_file(
                    magnific_state, scene, image_file, video_file, plan["video_prompt"], magnific_image_url,
                    plan["negative_prompt"], float(SHOT_VIDEO_GENERATION_CONFIG["cfg_scale"]),
                    provider_aspect_ratio, bool(SHOT_VIDEO_GENERATION_CONFIG["generate_audio"]),
                )
                if not Path(result).is_file() or Path(result).stat().st_size == 0:
                    raise RuntimeError("Magnific returned an empty video asset.")
                provider_used = "magnific"
            except Exception as exc:
                result, error = None, f"fal.ai: {error}; Magnific fallback: {exc}"
        else:
            error = f"{error}; Magnific fallback skipped: it needs MINIO_PUBLIC_ENDPOINT for a public start image"
    requested = float(plan["duration_seconds"])
    warning = None
    generated_duration = float(provider_duration)
    if not result:
        try:
            width, height = _resolution(state)
            _clip(image_file, requested, _fallback_motion(plan), video_file, width, height)
            result, generated_duration = str(video_file), requested
            warning = f"{shot_id} image-to-video failed; used FFmpeg movement instead: {error}"
        except Exception as fallback_exc:
            error = f"{error}; FFmpeg fallback failed: {fallback_exc}"
    item = _record(state, shot, image_file, requested, generated_duration, attempt, result, None if result else error)
    item["provider_used"] = provider_used
    if provider_used == "magnific":
        item["model_used"] = LEGACY_PROVIDER_MODELS["magnific"]["video"]
        warning = f"{shot_id} was animated on Magnific because fal.ai failed."
    if result and warning and provider_used != "magnific":
        item["provider"] = item["provider_used"] = "ffmpeg"
        item["model_used"] = "ffmpeg"
    candidate = None
    if state.get("quality_mode") == "refine":
        candidate = {
            "shot_id": shot_id, "candidate_id": candidate_id, "round": round_number,
            "prompt": plan["video_prompt"], "negative_prompt": plan["negative_prompt"],
            "seed": seed, "file": result, "generation_status": item["generation_status"],
            "generation_attempt": attempt, "error": item["error"],
            "provider": item["provider"], "selected_model_id": item["selected_model_id"],
        }
    warning = warning or (None if result else f"{shot_id} video generation failed after {attempt} attempts: {error}")
    print(
        f"{prefix} {'DONE' if result else 'ERROR'} {shot_id} candidate={candidate_id} "
        f"elapsed={time.monotonic() - started:.1f}s",
        flush=True,
    )
    return item, candidate, warning


def _generate(state: AgentState) -> dict:
    shots = state.get("shot_plan") or []
    images = state.get("image_files") or []
    plans = state.get("motion_plans") or []
    if not shots or [item.get("shot_id") for item in plans] != [item.get("shot_id") for item in shots] or len(images) != len(shots):
        raise RuntimeError("Shot Video Generation needs one ordered image and motion plan per approved shot.")
    if _video_provider(state) == "fal":
        _env("FAL_KEY")

    out = _out_dir(state, "scene_videos")
    out.mkdir(parents=True, exist_ok=True)
    previous = {item["shot_id"]: item for item in state.get("generated_videos", [])}
    retry_targets = set(state.get("video_retry_shots", []))
    if state.get("video_model"):
        retry_targets.update(
            shot_id for shot_id, item in previous.items()
            if item.get("selected_model_id", item.get("model_used")) != state["video_model"]
            or (
                item.get("provider") in {"magnific", "fal"}
                and item["provider"] != _video_provider(state)
            )
        )
    retrying = bool(previous and retry_targets)
    targets = retry_targets if retrying else {shot["shot_id"] for shot in shots}
    required_durations = {
        item["shot_id"]: float(item["required_duration_seconds"])
        for item in state.get("timeline_issues", [])
        if item.get("type") == "insufficient_clip_duration"
        and item.get("shot_id") in targets
        and item.get("required_duration_seconds") is not None
    }
    round_number = int(state.get("video_refinement_round", 0)) + int(retrying)
    generated_by_shot = {item["shot_id"]: item for item in previous.values()}
    new_candidates = []
    jobs = []
    warnings = list(state.get("warnings", []))
    redo_counts = {shot_id: dict(counts) for shot_id, counts in (state.get("paid_redo_counts") or {}).items()}
    automatic_redos = set(state.get("video_retry_shots", []))
    allowed_durations = [int(value) for value in SHOT_VIDEO_GENERATION_CONFIG["allowed_duration_seconds"]]
    max_attempts = int(SHOT_VIDEO_GENERATION_CONFIG["max_retries"]) + 1
    requested_aspect_ratio = str(state.get("aspect_ratio", SHOT_VIDEO_GENERATION_CONFIG["default_aspect_ratio"]))
    provider_aspect_ratio = str(SHOT_VIDEO_GENERATION_CONFIG["aspect_ratios"][requested_aspect_ratio])

    for index, (shot, image_file, plan) in enumerate(zip(shots, images, plans), start=1):
        if shot["shot_id"] not in targets and shot["shot_id"] in previous:
            continue
        requested = max(float(plan["duration_seconds"]), required_durations.get(shot["shot_id"], 0))
        plan = {**plan, "duration_seconds": requested}
        provider_duration = next((value for value in sorted(allowed_durations) if value >= requested), max(allowed_durations))
        candidate_id = f"{shot['shot_id']}-c{round_number + 1}"
        seed = int(SHOT_VIDEO_GENERATION_CONFIG["seed_base"]) * (round_number + 1) + index
        source = Path(image_file) if image_file else None
        if not source or not source.is_file() or source.stat().st_size == 0:
            item = _record(state, shot, image_file, requested, None, 0, None, "The approved source image is missing or invalid.")
            item["failure_type"] = "missing_source_image"
            generated_by_shot[shot["shot_id"]] = item
            if state.get("quality_mode") == "refine":
                new_candidates.append({
                    "shot_id": shot["shot_id"], "candidate_id": candidate_id, "round": round_number,
                    "prompt": plan["video_prompt"], "negative_prompt": plan["negative_prompt"],
                    "seed": seed, "file": None, "generation_status": "failed",
                    "generation_attempt": 0, "error": item["error"],
                    "provider": item["provider"], "selected_model_id": item["selected_model_id"],
                })
            continue
        redo_blocked = (
            shot["shot_id"] in automatic_redos and shot["shot_id"] in previous
            and not take_paid_redo(redo_counts, shot["shot_id"], "video")
        )
        start_image_url = public_artifact_url(state, image_file) if _video_provider(state) != "fal" and os.getenv("MINIO_PUBLIC_ENDPOINT") and requested <= max(allowed_durations) else None
        if redo_blocked or (_video_provider(state) != "fal" and not start_image_url) or requested > max(allowed_durations):
            error = (
                "The shot already used its automatic paid video redo."
                if redo_blocked
                else f"The requested {requested:g}s duration exceeds the provider maximum of {max(allowed_durations)}s."
                if requested > max(allowed_durations)
                else "The approved source image has no provider-accessible public URL."
            )
            video_file = out / f"{candidate_id}.mp4"
            try:
                width, height = _resolution(state)
                _clip(str(source), requested, _fallback_motion(plan), video_file, width, height)
                item = _record(state, shot, image_file, requested, requested, 1, str(video_file), None)
                item["provider"] = "ffmpeg"
                item["model_used"] = "ffmpeg"
            except Exception as exc:
                item = _record(state, shot, image_file, requested, None, 1, None, f"{error}; FFmpeg fallback failed: {exc}")
            generated_by_shot[shot["shot_id"]] = item
            if state.get("quality_mode") == "refine":
                new_candidates.append({
                    "shot_id": shot["shot_id"], "candidate_id": candidate_id, "round": round_number,
                    "prompt": plan["video_prompt"], "negative_prompt": plan["negative_prompt"],
                    "seed": seed, "file": item["video_file"], "generation_status": item["generation_status"],
                    "generation_attempt": item["generation_attempt"], "error": item["error"],
                    "provider": item["provider"], "selected_model_id": item["selected_model_id"],
                })
            warnings.append(
                f"{shot['shot_id']} already used its automatic video redo; used free FFmpeg movement instead."
                if redo_blocked and item["video_file"]
                else f"{shot['shot_id']} image-to-video unavailable; used FFmpeg movement instead."
                if item["video_file"] else f"{shot['shot_id']} video generation failed: {item['error']}"
            )
            continue
        jobs.append((
            state, shot, image_file, plan, out, index, round_number, provider_duration,
            start_image_url, provider_aspect_ratio, max_attempts,
        ))

    if jobs:
        workers = _worker_count(len(jobs), _video_provider(state))
        print(f"[pipeline:{state.get('thread_id', '-')}] VIDEO WORKERS count={workers} jobs={len(jobs)}", flush=True)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="shot-video") as pool:
            for item, candidate, warning in pool.map(_generate_job, jobs):
                generated_by_shot[item["shot_id"]] = item
                if candidate:
                    new_candidates.append(candidate)
                if warning:
                    warnings.append(warning)

    generated_videos = [generated_by_shot[shot["shot_id"]] for shot in shots]

    result = {
        "cost_ledger": ledger_for(state).snapshot(),
        "paid_redo_counts": redo_counts,
        "generated_videos": generated_videos,
        "scene_video_files": [item["video_file"] for item in generated_videos],
        "warnings": list(dict.fromkeys(warnings)),
        "video_refinement_round": round_number,
    }
    if state.get("quality_mode") == "refine":
        result.update(
            video_candidates=[
                *(candidate for candidate in state.get("video_candidates", [])
                  if not state.get("video_model") or candidate.get("selected_model_id") == state["video_model"]),
                *new_candidates,
            ],
        )
    return result


def create_shot_videos(state: AgentState) -> dict:
    if not SHOT_VIDEO_GENERATION_CONFIG["enabled"]:
        raise RuntimeError("Shot Video Generation Agent is disabled in config/shot_video_generation.yml.")
    return _generate(state)
