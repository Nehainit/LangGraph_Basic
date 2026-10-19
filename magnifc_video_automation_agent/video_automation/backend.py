import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel, Field, field_validator


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
sys.path.insert(0, str(ROOT))

from video_automation.agent_graph import build_story_graph, persistent_checkpointer
from video_automation.models import validate_model_configuration
from video_automation.model_catalog import DEFAULT_MODELS, LEGACY_PROVIDER_MODELS, MODELS, model_entry
from video_automation.prompts import PIPELINE_CONFIG, VISUAL_STYLES
from video_automation.agents.story_agent import UnsafeContentError
from video_automation.agents.story_writer_agent import write_story
from video_automation.agents.video_validator import validate_generated_videos
from video_automation.artifact_store import archive_artifacts
from video_automation.cost_control import spend_summary
from video_automation.auth import public_auth_config, require_user
from video_automation.billing import film_seconds, hold_film, release_film, require_film_allowance, router as billing_router, settle_film
from video_automation.character_library import film_characters, router as character_router, saved_character_names
from video_automation.drafts import find_draft, forget_film, record_film, router as drafts_router


class VideoRequest(BaseModel):
    topic: str
    duration: int = Field(default=30, ge=5, le=120)
    language: str = "English"
    quality_mode: Literal["standard", "refine"] = "standard"
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "9:16"
    video_quality: Literal["standard", "high"] = "standard"
    image_provider: Literal["magnific", "fal"] = "fal"
    visual_style: Literal["realistic", "cinematic", "illustrated", "3d_animation", "anime"] | None = None
    # Library characters the user @-mentioned in the topic.
    character_ids: list[str] = Field(default_factory=list, max_length=6)
    narration_model: str | None = None
    image_model: str | None = None
    video_model: str | None = None
    # The My films story draft this film was made from; the film replaces it there.
    draft_id: str | None = None

    @field_validator("narration_model", "image_model", "video_model")
    @classmethod
    def supported_model(cls, value: str | None, info) -> str | None:
        if value is not None:
            model_entry(info.field_name.removesuffix("_model"), value)
        return value


class StoryChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class StoryWriterRequest(BaseModel):
    messages: list[StoryChatMessage] = Field(min_length=1, max_length=30)
    duration: int = Field(default=30, ge=5, le=120)
    language: str = Field(default="English", max_length=40)


class StoryReviewRequest(BaseModel):
    thread_id: str
    action: Literal["approve", "edit", "regenerate"]
    story: str | None = None
    note: str | None = None


class ReferenceReviewRequest(BaseModel):
    thread_id: str
    action: Literal["approve", "regenerate"]
    note: str | None = None


class ScenePlanReviewRequest(BaseModel):
    thread_id: str
    action: Literal["retry", "revise_narration"]
    note: str | None = None


class VisualPlanReviewRequest(BaseModel):
    thread_id: str
    action: Literal["retry", "revise_scenes"]
    note: str | None = None


class ShotPlanReviewRequest(BaseModel):
    thread_id: str
    action: Literal["retry", "revise_visuals"]
    note: str | None = None


class ShotFeedback(BaseModel):
    shot_id: str
    note: str


class DirectorReviewRequest(BaseModel):
    thread_id: str
    action: Literal["approve", "regenerate"]
    feedback: list[ShotFeedback] = Field(default_factory=list)


class VisualStoryboardReviewRequest(DirectorReviewRequest):
    # Per-shot video tier chosen on the storyboard, e.g. {"shot-003": "standard"} to animate a still shot.
    video_tiers: dict[str, Literal["static", "standard", "premium"]] = Field(default_factory=dict)


app = FastAPI()
app.mount("/static", StaticFiles(directory=FRONTEND), name="frontend")
app.mount("/examples", StaticFiles(directory=ROOT / "demo"), name="examples")
app.include_router(character_router)
app.include_router(drafts_router)
app.include_router(billing_router)
logger = logging.getLogger(__name__)
graph = build_story_graph(
    # Deployments point CHECKPOINT_DB into a mounted volume so paused films survive restarts.
    checkpointer=persistent_checkpointer(Path(os.getenv("CHECKPOINT_DB", ROOT / ".langgraph-checkpoints.sqlite"))),
    video_validator=validate_generated_videos,
)
MAX_AUTO_RETRIES_PER_STAGE = 2
MAX_AUTO_ADVANCE_STEPS = 12


def _output_url(file_path: str | None) -> str | None:
    if not file_path:
        return None
    try:
        return f"/output/{Path(file_path).resolve().relative_to(ROOT).as_posix()}"
    except ValueError:
        return None


def _archive(result: dict, thread_id: str, stage: str) -> dict | None:
    if not result.get("output_dir"):
        return None
    try:
        return archive_artifacts(result, thread_id, stage)
    except Exception as exc:
        logger.exception("MinIO artifact upload failed; local artifact history was retained")
        return {"backend": "local", "error": str(exc)}


def _artifacts(result: dict) -> dict:
    character_board_file = result.get("character_board_file")
    mood_board_file = result.get("mood_board_file")
    image_files = result.get("image_files", [])
    video_file = result.get("video_file")
    video_candidates = [
        {
            **candidate,
            "file_url": _output_url(candidate.get("file")),
        }
        for candidate in result.get("video_candidates", [])
    ]
    return {
        "story": result.get("story"),
        "story_outline": result.get("story_outline"),
        "parsed_requirements": result.get("parsed_requirements"),
        "llm_evaluations": result.get("llm_evaluations", []),
        "planning_attempts": result.get("planning_attempts", {}),
        "narration_script": result.get("narration_script"),
        "narration_segments": result.get("narration_segments", []),
        "estimated_narration_seconds": result.get("estimated_narration_seconds"),
        "actual_narration_seconds": result.get("actual_narration_seconds"),
        "narration_file": result.get("narration_file"),
        "narration_url": _output_url(result.get("narration_file")),
        "narration_alignment_file": result.get("narration_alignment_file"),
        "narration_alignment_url": _output_url(result.get("narration_alignment_file")),
        "narration_segment_timings": result.get("narration_segment_timings", []),
        "scene_timings": result.get("scene_timings", []),
        "needs_story_revision": result.get("needs_story_revision", False),
        "story_revision_reason": result.get("story_revision_reason"),
        "scene_analysis": result.get("scene_analysis", []),
        "scenes": result.get("scenes", []),
        "scene_plan_needs_revision": result.get("scene_plan_needs_revision", False),
        "scene_plan_revision_reason": result.get("scene_plan_revision_reason"),
        "scene_plan_issues": result.get("scene_plan_issues", []),
        "visual_beats": result.get("visual_beats", []),
        "visual_plan_needs_revision": result.get("visual_plan_needs_revision", False),
        "visual_plan_revision_reason": result.get("visual_plan_revision_reason"),
        "visual_plan_issues": result.get("visual_plan_issues", []),
        "shot_plan": result.get("shot_plan", []),
        "shot_plan_needs_revision": result.get("shot_plan_needs_revision", False),
        "shot_plan_revision_reason": result.get("shot_plan_revision_reason"),
        "shot_plan_issues": result.get("shot_plan_issues", []),
        "image_prompt_requests": result.get("image_prompt_requests", []),
        "generated_images": result.get("generated_images", []),
        "shot_image_qa_results": result.get("shot_image_qa_results", []),
        "motion_plans": result.get("motion_plans", []),
        "motion_plan_needs_revision": result.get("motion_plan_needs_revision", False),
        "motion_plan_revision_reason": result.get("motion_plan_revision_reason"),
        "generated_videos": result.get("generated_videos", []),
        "critic_issues": result.get("critic_issues", []),
        "story_beats": result.get("story_beats", []),
        "genre": result.get("genre", "general"),
        "genre_structure": result.get("genre_structure", []),
        "character_board_file": character_board_file,
        "character_board_url": _output_url(character_board_file),
        "mood_board_file": mood_board_file,
        "mood_board_url": _output_url(mood_board_file),
        "director_plan": result.get("director_plan", result.get("storyboard", [])),
        "storyboard": result.get("storyboard", []),
        "sound_design_plan": result.get("sound_design_plan", []),
        "music_file": result.get("music_file"),
        "music_url": _output_url(result.get("music_file")),
        "image_files": image_files,
        "image_urls": [_output_url(path) for path in image_files],
        "quality_mode": result.get("quality_mode", "standard"),
        "aspect_ratio": result.get("aspect_ratio", "9:16"),
        "video_quality": result.get("video_quality", "standard"),
        "video_candidates": video_candidates,
        "clips": [
            {
                "shot_id": item.get("shot_id"),
                "video_url": _output_url(item.get("video_file")),
                "image_url": _output_url(item.get("source_image_file")),
                "status": item.get("generation_status"),
                "provider": item.get("provider"),
                "model": item.get("model_used"),
                "video_tier": item.get("video_tier"),
                # planned_static: still motion chosen to save cost; generation_failed: fallback after an error.
                "reason": item.get("reason"),
            }
            for item in result.get("generated_videos", [])
        ],
        "video_validation_results": result.get("video_validation_results", []),
        "video_validation_retry_counts": result.get("video_validation_retry_counts", {}),
        "video_validation_exhausted_shots": result.get("video_validation_exhausted_shots", []),
        "edit_timeline": result.get("edit_timeline", []),
        "narration_duration_seconds": result.get("narration_duration_seconds"),
        "timeline_duration_seconds": result.get("timeline_duration_seconds"),
        "timeline_valid": result.get("timeline_valid"),
        "timeline_issues": result.get("timeline_issues", []),
        "timeline_retry_requests": result.get("timeline_retry_requests", []),
        "edit_timeline_retry_counts": result.get("edit_timeline_retry_counts", {}),
        "edit_timeline_exhausted_shots": result.get("edit_timeline_exhausted_shots", []),
        "compilation_status": result.get("compilation_status"),
        "rough_cut_file": result.get("rough_cut_file"),
        "rough_cut_url": _output_url(result.get("rough_cut_file")),
        "shot_timestamps": result.get("shot_timestamps", []),
        "sync_valid": result.get("sync_valid"),
        "compilation_issues": result.get("compilation_issues", []),
        "compilation_error": result.get("compilation_error"),
        "rough_cut_failure_source": result.get("rough_cut_failure_source"),
        "rough_cut_retry_count": result.get("rough_cut_retry_count", 0),
        "rough_cut_retry_exhausted": result.get("rough_cut_retry_exhausted", False),
        "combined_video_judge_report": result.get("combined_video_judge_report"),
        "combined_video_judge_approved": result.get("combined_video_judge_approved"),
        "combined_video_judge_status": result.get("combined_video_judge_status"),
        "combined_video_judge_error": result.get("combined_video_judge_error"),
        "combined_video_judge_error_count": result.get("combined_video_judge_error_count", 0),
        "combined_video_judge_error_exhausted": result.get("combined_video_judge_error_exhausted", False),
        "combined_video_judge_model": result.get("combined_video_judge_model"),
        "combined_video_judge_retry_target": result.get("combined_video_judge_retry_target"),
        "combined_video_judge_retry_shots": result.get("combined_video_judge_retry_shots", []),
        "video_judge_refinement_round": result.get("video_judge_refinement_round", 0),
        "video_judge_refinement_exhausted": result.get("video_judge_refinement_exhausted", False),
        "visual_fidelity_reports": result.get("visual_fidelity_reports", {}),
        "motion_temporal_reports": result.get("motion_temporal_reports", {}),
        "context_intent_reports": result.get("context_intent_reports", {}),
        "adversarial_reports": result.get("adversarial_reports", {}),
        "meta_judge_reports": result.get("meta_judge_reports", {}),
        "judge_retry_counts": result.get("judge_retry_counts", {}),
        "judge_exhausted_shots": result.get("judge_exhausted_shots", []),
        "judge_decision": result.get("judge_decision"),
        "pipeline_status": result.get("pipeline_status"),
        "cost_ledger": result.get("cost_ledger", []),
        "spent_usd": round(
            sum(float(entry["usd"]) for entry in result.get("cost_ledger", []))
            + sum(float(item.get("cost_usd") or 0) for item in result.get("llm_evaluations", [])),
            4,
        ),
        # Spend per category plus billed video seconds the edit threw away (the number cost work must shrink).
        "spend": spend_summary(
            result.get("cost_ledger", []),
            sum(float(item.get("cost_usd") or 0) for item in result.get("llm_evaluations", [])),
            result.get("edit_timeline", []),
        ),
        "budget_usd": PIPELINE_CONFIG["budget"]["max_usd_per_video"],
        "budget_exhausted": result.get("budget_exhausted", False),
        "story_feedback_history": result.get("story_feedback_history", []),
        "reference_feedback_history": result.get("reference_feedback_history", []),
        "director_feedback_history": result.get("director_feedback_history", []),
        "visual_feedback_history": result.get("visual_feedback_history", []),
        "count_adjustment": result.get("count_adjustment", ""),
        "warnings": result.get("warnings", []),
        "video_file": video_file,
        "video_url": _output_url(video_file),
    }


def _graph_response(result: dict, thread_id: str, archive: bool = True) -> dict:
    """`archive=False` rebuilds a paused review without writing another artifact snapshot."""
    _archive_stage = _archive if archive else (lambda *_args: None)
    artifacts = _artifacts(result)
    if "__interrupt__" in result:
        review = result["__interrupt__"][0].value
        logger.warning("GRAPH PAUSED: thread_id=%s stage=%s", thread_id, review.get("stage", "unknown"))
        if review.get("stage") == "scene_plan_review":
            return {
                "status": "scene_plan_review",
                "thread_id": thread_id,
                "revision_reason": review.get("revision_reason"),
                "issues": review.get("issues", []),
                "actions": review["actions"],
                "artifacts": artifacts,
            }
        if review.get("stage") == "visual_plan_review":
            return {
                "status": "visual_plan_review",
                "thread_id": thread_id,
                "revision_reason": review.get("revision_reason"),
                "issues": review.get("issues", []),
                "actions": review["actions"],
                "artifacts": artifacts,
            }
        if review.get("stage") == "shot_plan_review":
            return {
                "status": "shot_plan_review",
                "thread_id": thread_id,
                "revision_reason": review.get("revision_reason"),
                "issues": review.get("issues", []),
                "actions": review["actions"],
                "artifacts": artifacts,
            }
        if "image_files" in review:
            artifacts.update(
                {
                    "storyboard": review["storyboard"],
                    "director_plan": review["storyboard"],
                    "image_files": review["image_files"],
                    "image_urls": [_output_url(path) for path in review["image_files"]],
                }
            )
            artifacts["storage"] = _archive_stage({**result, **artifacts}, thread_id, "visual-storyboard-review")
            return {
                "status": "visual_storyboard_review",
                "thread_id": thread_id,
                "storyboard": artifacts["storyboard"],
                "image_urls": artifacts["image_urls"],
                "shot_image_qa_results": artifacts["shot_image_qa_results"],
                "actions": review["actions"],
                "feedback": review.get("feedback", []),
                "artifacts": artifacts,
            }
        if "character_board_file" in review:
            artifacts.update(
                {
                    "production_bible": review["production_bible"],
                    "character_board_file": review["character_board_file"],
                    "character_board_url": _output_url(review["character_board_file"]),
                    "mood_board_file": review.get("mood_board_file"),
                    "mood_board_url": _output_url(review.get("mood_board_file")),
                }
            )
            artifacts["storage"] = _archive_stage({**result, **artifacts}, thread_id, "reference-board-review")
            return {
                "status": "reference_board_review",
                "thread_id": thread_id,
                "production_bible": artifacts["production_bible"],
                "character_board_url": artifacts["character_board_url"],
                "mood_board_url": artifacts["mood_board_url"],
                "actions": review["actions"],
                "feedback": review.get("feedback", ""),
                "artifacts": artifacts,
            }
        if "director_plan" in review:
            artifacts["director_plan"] = review["director_plan"]
            artifacts["storyboard"] = review["director_plan"]
            artifacts["count_adjustment"] = review.get("count_adjustment", "")
            artifacts["storage"] = _archive_stage({**result, **artifacts}, thread_id, "director-plan-review")
            return {
                "status": "director_plan_review",
                "thread_id": thread_id,
                "director_plan": artifacts["director_plan"],
                "count_adjustment": artifacts["count_adjustment"],
                "actions": review["actions"],
                "feedback": review.get("feedback", []),
                "artifacts": artifacts,
            }

        artifacts["story"] = review["story"]
        artifacts["storage"] = _archive_stage({**result, **artifacts}, thread_id, "story-review")
        return {
            "status": "story_review",
            "thread_id": thread_id,
            "story": review["story"],
            "actions": review["actions"],
            "feedback": review.get("feedback", ""),
            "artifacts": artifacts,
        }

    artifacts["storage"] = _archive(result, thread_id, result.get("pipeline_status", "complete"))
    if result.get("pipeline_status") and not artifacts["video_file"]:
        logger.warning("GRAPH STOPPED INCOMPLETE: thread_id=%s stage=%s", thread_id, result.get("pipeline_status"))
        return {
            "status": "pipeline_incomplete",
            "thread_id": thread_id,
            "message": "Pipeline has not produced a final video yet.",
            "artifacts": artifacts,
        }
    return {
        "status": result.get("pipeline_status", "complete"),
        "thread_id": thread_id,
        "story": artifacts["story"],
        "storyboard": artifacts["storyboard"],
        "video_file": artifacts["video_file"],
        "video_url": artifacts["video_url"],
        "warnings": artifacts["warnings"],
        "artifacts": artifacts,
    }


# The storyboard is the one checkpoint people decide on: it is the last cheap step before paid video.
USER_REVIEW_STAGES = {"visual_storyboard_review"}
AUTO_ACTIONS = {
    "story_review": "approve",
    "scene_plan_review": "retry",
    "visual_plan_review": "retry",
    "reference_board_review": "approve",
    "director_plan_review": "approve",
    "shot_plan_review": "retry",
}
RECOVERY_ACTIONS = {
    "scene_plan_review": "revise_narration",
    "visual_plan_review": "revise_scenes",
    "shot_plan_review": "revise_visuals",
}


def _review_stage(review: dict) -> str:
    if review.get("stage"):
        return review["stage"]
    if "image_files" in review:
        return "visual_storyboard_review"
    if "character_board_file" in review:
        return "reference_board_review"
    if "director_plan" in review:
        return "director_plan_review"
    return "story_review"


def _auto_advance(result: dict, thread_id: str) -> dict:
    """Answer internal checkpoints until the graph finishes or reaches a checkpoint for the user."""
    config = {"configurable": {"thread_id": thread_id}}
    retry_counts: dict[str, int] = {}
    recovered_stages: set[str] = set()
    for _ in range(MAX_AUTO_ADVANCE_STEPS):
        if "__interrupt__" not in result:
            break
        review = result["__interrupt__"][0].value
        if not isinstance(review, dict):
            review = {}
        stage = _review_stage(review)
        if stage in USER_REVIEW_STAGES:
            break
        action = AUTO_ACTIONS.get(stage)
        if not action:
            logger.warning("AUTO-ADVANCING UNKNOWN CHECKPOINT: thread_id=%s", thread_id)
            action = "approve"
        if action == "retry":
            retry_count = retry_counts.get(stage, 0)
            if retry_count >= MAX_AUTO_RETRIES_PER_STAGE:
                action = RECOVERY_ACTIONS.get(stage)
                if not action or stage in recovered_stages:
                    logger.warning("AUTO-RECOVERY STOPPED: thread_id=%s stage=%s retries=%s", thread_id, stage, retry_count)
                    break
                recovered_stages.add(stage)
                logger.warning("AUTO-RECOVERY: thread_id=%s stage=%s action=%s", thread_id, stage, action)
            else:
                retry_counts[stage] = retry_count + 1
        result = graph.invoke(Command(resume={"action": action, "note": review.get("revision_reason", "")}), config)
    else:
        logger.warning("AUTO-ADVANCE STOPPED: thread_id=%s steps=%s", thread_id, MAX_AUTO_ADVANCE_STEPS)
    return result


def _track(user_id: str | None, response: dict, **film) -> dict:
    """Update My films drafts; a bookkeeping failure must never cost the user a film they paid for."""
    try:
        record_film(user_id, response, **film)
    except Exception:
        logger.exception("Could not update My films drafts for %s", response.get("thread_id"))
    return response


def _settle(user_id: str | None, thread_id: str, response: dict) -> dict:
    """Spend or release the film's held credit; a bookkeeping failure must never cost the user their film."""
    try:
        settle_film(user_id, thread_id, response)
    except Exception:
        logger.exception("Could not settle film credit for %s", thread_id)
    return response


def _resume_review(thread_id: str, node: str, command: dict, user_id: str | None) -> dict:
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = graph.get_state(config)
        # Approving the storyboard starts paid video; a film already past it resumes paid work.
        if (node == "review_visual_storyboard" and command.get("action") == "approve") or snapshot.values.get("visual_approved"):
            hold_film(user_id, thread_id, film_seconds(snapshot.values.get("duration")))
        if node in snapshot.next:
            value = Command(resume=command)
        elif (
            command.get("action") == "approve"
            and snapshot.next
            and not any(next_node.startswith("review_") for next_node in snapshot.next)
            and snapshot.values.get({"review_story": "approved", "review_visual_storyboard": "visual_approved"}.get(node, ""))
        ):
            value = None
        elif snapshot.next and any(getattr(task, "error", None) for task in getattr(snapshot, "tasks", ())):
            # The run crashed after this review (e.g. a provider or validation error); retrying resumes the failed step.
            logger.info("Retrying failed step %s for %s", list(snapshot.next), thread_id)
            value = None
        else:
            raise HTTPException(status_code=404, detail="Review session not found or already completed.")
        response = _graph_response(_auto_advance(graph.invoke(value, config), thread_id), thread_id)
        return _track(user_id, _settle(user_id, thread_id, response))
    except HTTPException:
        raise
    except UnsafeContentError as exc:
        release_film(user_id, thread_id)
        raise HTTPException(status_code=400, detail={"error": str(exc), "categories": exc.categories}) from exc
    except Exception as exc:
        logger.exception("Artifact review failed")
        release_film(user_id, thread_id)
        raise HTTPException(status_code=500, detail={"error": str(exc)}) from exc


def _selected_feedback(thread_id: str, node: str, feedback: list[ShotFeedback]) -> list[dict[str, str]]:
    cleaned = [{"shot_id": item.shot_id.strip(), "note": item.note.strip()} for item in feedback if item.note.strip()]
    if not cleaned:
        raise HTTPException(status_code=400, detail="Add feedback to at least one shot.")
    shot_ids = [item["shot_id"] for item in cleaned]
    if len(shot_ids) != len(set(shot_ids)):
        raise HTTPException(status_code=400, detail="Each shot can have only one feedback note.")
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    if node not in snapshot.next:
        raise HTTPException(status_code=404, detail="Review session not found or already completed.")
    known_shots = {shot["shot_id"] for shot in snapshot.values.get("storyboard", snapshot.values.get("director_plan", []))}
    if not set(shot_ids) <= known_shots:
        raise HTTPException(status_code=400, detail="Feedback contains an unknown shot ID.")
    return cleaned


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/characters")
def characters_page() -> FileResponse:
    return FileResponse(FRONTEND / "characters.html")


@app.get("/sign-in")
@app.get("/sign-up")
def auth_page() -> FileResponse:
    return FileResponse(FRONTEND / "auth.html")


@app.get("/api/auth-config")
def auth_config() -> dict:
    return public_auth_config()


@app.get("/api/model-catalog", dependencies=[Depends(require_user)])
def model_catalog() -> dict:
    return {"models": MODELS, "pricing_note": "Indicative public API rates; actual charges depend on provider billing."}


def resolve_request_models(request: VideoRequest) -> dict[str, str]:
    legacy = LEGACY_PROVIDER_MODELS[request.image_provider] if not (request.image_model or request.video_model) else {}
    return {
        "narration_model": request.narration_model or DEFAULT_MODELS["narration"],
        "image_model": request.image_model or legacy.get("image") or DEFAULT_MODELS["image"],
        "video_model": request.video_model or legacy.get("video") or DEFAULT_MODELS["video"],
    }


@app.post("/api/create-video")
def create_video(request: VideoRequest, user_id: str | None = Depends(require_user)) -> dict:
    if not request.topic.strip():
        raise HTTPException(status_code=400, detail="Topic is required.")
    require_film_allowance(user_id, request.duration)
    cast = film_characters(user_id, request.character_ids)
    topic = request.topic.strip()
    for character in cast:
        # "@Meera" is how the studio marks a saved character; the story model just needs the name.
        topic = topic.replace(f"@{character['name']}", character["name"])
    try:
        validate_model_configuration("shot-image-qa")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        thread_id = str(uuid.uuid4())
        result = graph.invoke(
            {
                "thread_id": thread_id,
                "topic": topic,
                "tone": "Infer the tone and visual style from the user's prompt.",
                "duration": f"{request.duration} seconds",
                "language": request.language.strip() or "English",
                "characters": [f"{character['name']}: {character['description']}" for character in cast],
                "library_characters": cast,
                "output_dir": str(ROOT / "outputs" / thread_id),
                "quality_mode": request.quality_mode,
                "aspect_ratio": request.aspect_ratio,
                "video_quality": request.video_quality,
                "image_provider": request.image_provider,
                **resolve_request_models(request),
                **({"visual_style": VISUAL_STYLES[request.visual_style]} if request.visual_style else {}),
                "warnings": [],
            },
            {"configurable": {"thread_id": thread_id}},
        )
        result = _auto_advance(result, thread_id)
        return _track(
            user_id, _graph_response(result, thread_id),
            request=request.model_dump(exclude={"draft_id"}), story_draft_id=request.draft_id,
        )
    except HTTPException:
        raise
    except UnsafeContentError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc), "categories": exc.categories}) from exc
    except Exception as exc:
        logger.exception("Video creation failed")
        raise HTTPException(status_code=500, detail={"error": str(exc)}) from exc


@app.post("/api/story-writer")
def story_writer(request: StoryWriterRequest, user_id: str | None = Depends(require_user)) -> dict:
    if request.messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="The last message must be yours.")
    try:
        result = write_story(
            [message.model_dump() for message in request.messages],
            seconds=request.duration, language=request.language.strip() or "English",
            saved_names=saved_character_names(user_id),
        )
    except UnsafeContentError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc), "categories": exc.categories}) from exc
    except Exception as exc:
        logger.exception("Story writer failed")
        raise HTTPException(status_code=502, detail={"error": f"The story writer is unavailable right now: {exc}"}) from exc
    return {
        "title": result["title"],
        "story": result["story"],
        "reply": result["reply"],
        "characters": result["characters"],
        "cost_usd": round(sum(float(item.get("cost_usd") or 0) for item in result["llm_evaluations"]), 6),
    }


@app.post("/api/review-story")
def submit_story_review(request: StoryReviewRequest, user_id: str | None = Depends(require_user)) -> dict:
    thread_id = request.thread_id.strip()
    command = {"action": request.action}
    if request.action == "edit":
        if not request.story or not request.story.strip():
            raise HTTPException(status_code=400, detail="Edited story is required.")
        command["story"] = request.story
    elif request.action == "regenerate":
        note = (request.note or "").strip()
        if not note:
            raise HTTPException(status_code=400, detail="Story feedback is required.")
        command["note"] = note
    return _resume_review(thread_id, "review_story", command, user_id)


@app.post("/api/review-reference-board")
def submit_reference_review(request: ReferenceReviewRequest, user_id: str | None = Depends(require_user)) -> dict:
    command = {"action": request.action}
    if request.action == "regenerate":
        note = (request.note or "").strip()
        if not note:
            raise HTTPException(status_code=400, detail="Reference package feedback is required.")
        command["note"] = note
    return _resume_review(request.thread_id.strip(), "review_reference_package", command, user_id)


@app.post("/api/review-scene-plan")
def submit_scene_plan_review(request: ScenePlanReviewRequest, user_id: str | None = Depends(require_user)) -> dict:
    command = {"action": request.action, "note": (request.note or "").strip()}
    return _resume_review(request.thread_id.strip(), "review_scene_plan", command, user_id)


@app.post("/api/review-visual-plan")
def submit_visual_plan_review(request: VisualPlanReviewRequest, user_id: str | None = Depends(require_user)) -> dict:
    command = {"action": request.action, "note": (request.note or "").strip()}
    return _resume_review(request.thread_id.strip(), "review_visual_plan", command, user_id)


@app.post("/api/review-shot-plan")
def submit_shot_plan_review(request: ShotPlanReviewRequest, user_id: str | None = Depends(require_user)) -> dict:
    command = {"action": request.action, "note": (request.note or "").strip()}
    return _resume_review(request.thread_id.strip(), "review_shot_plan", command, user_id)


@app.post("/api/review-director-plan")
def submit_director_review(request: DirectorReviewRequest, user_id: str | None = Depends(require_user)) -> dict:
    command = {"action": request.action}
    if request.action == "regenerate":
        command["feedback"] = _selected_feedback(request.thread_id.strip(), "review_director_plan", request.feedback)
    return _resume_review(request.thread_id.strip(), "review_director_plan", command, user_id)


@app.post("/api/review-visual-storyboard")
def submit_visual_storyboard_review(request: VisualStoryboardReviewRequest, user_id: str | None = Depends(require_user)) -> dict:
    command = {"action": request.action}
    if request.action == "regenerate":
        command["feedback"] = _selected_feedback(request.thread_id.strip(), "review_visual_storyboard", request.feedback)
    elif request.video_tiers:
        command["video_tiers"] = request.video_tiers
    return _resume_review(request.thread_id.strip(), "review_visual_storyboard", command, user_id)


@app.get("/api/drafts/{draft_id}/review")
def reopen_paused_film(draft_id: str, user_id: str | None = Depends(require_user)) -> dict:
    """The review a paused film is waiting on, rebuilt from its checkpoint so any device can pick it up."""
    draft = find_draft(user_id, draft_id)
    if draft["kind"] != "film":
        raise HTTPException(status_code=404, detail="Draft not found.")
    snapshot = graph.get_state({"configurable": {"thread_id": draft["thread_id"]}})
    interrupts = list(getattr(snapshot, "interrupts", ()))
    if not interrupts:
        forget_film(user_id, draft_id)
        raise HTTPException(status_code=410, detail="This film is no longer waiting for a review, so it was removed from your drafts.")
    response = _graph_response({**snapshot.values, "__interrupt__": interrupts}, draft["thread_id"], archive=False)
    return {**response, "request": draft["request"]}


@app.get("/output/{path:path}", dependencies=[Depends(require_user)])
def output_file(path: str) -> FileResponse:
    file_path = (ROOT / path).resolve()
    try:
        file_path.relative_to((ROOT / "outputs").resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="File not found.") from exc
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(file_path)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
