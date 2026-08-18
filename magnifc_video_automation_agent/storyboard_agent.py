from schema import AgentState
from models import load_model
from prompts import storyboard_prompt
from story_agent import _duration_seconds, _parse_json


def _scene_count(duration: str) -> int:
    return max(3, min(8, round(_duration_seconds(duration) / 12)))


def _validate_storyboard(storyboard: object, scene_count: int) -> None:
    if not isinstance(storyboard, list) or not storyboard:
        raise RuntimeError("Storyboard agent returned no scenes.")
    if len(storyboard) != scene_count:
        raise RuntimeError(f"Storyboard needs exactly {scene_count} scenes.")

    required = {"scene_number", "start_time", "end_time", "visuals", "camera", "narration"}
    for index, scene in enumerate(storyboard, start=1):
        if not isinstance(scene, dict) or not required <= scene.keys():
            raise RuntimeError("Storyboard scene is missing required fields.")
        if scene["scene_number"] != index:
            raise RuntimeError("Storyboard scene numbers must be sequential.")


def create_storyboard(state: AgentState) -> dict[str, list[dict]]:
    story = state.get("story")
    if not story:
        raise RuntimeError("Storyboard agent needs an approved story.")

    model = load_model("storyboard")
    scene_count = _scene_count(state["duration"])
    feedback = ""
    for attempt in range(3):
        response = model.invoke(
            storyboard_prompt(
                duration=state["duration"],
                scene_count=scene_count,
                story=story,
                feedback=feedback,
            )
        )
        storyboard = _parse_json(response.content).get("storyboard")
        try:
            _validate_storyboard(storyboard, scene_count)
            return {"storyboard": storyboard}
        except RuntimeError as exc:
            if attempt == 2:
                raise
            feedback = f"Previous attempt failed: {exc}. Fix it and return valid JSON only."

    raise RuntimeError("Storyboard agent failed.")



