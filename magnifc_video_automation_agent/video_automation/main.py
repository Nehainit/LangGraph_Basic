import argparse
import json
import uuid
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from video_automation.agent_graph import build_story_graph, persistent_checkpointer
from video_automation.schema import AgentState


ROOT = Path(__file__).resolve().parents[1]
CLI_LAST_RUN_FILE = ROOT / "outputs" / ".latest-cli-thread"


def get_user_input() -> AgentState:
    topic = input("Describe the story, characters, mood, and visual style: ").strip()
    duration = input("Duration in seconds: ").strip()
    language = input("Story language: ").strip()
    aspect_ratio = input("Aspect ratio (16:9, 9:16, or 1:1): ").strip() or "9:16"
    video_quality = input("Video quality (standard or high): ").strip().lower() or "standard"
    while True:
        image_provider = input("Generation provider for images and videos (magnific or fal) [magnific]: ").strip().lower() or "magnific"
        if image_provider in {"magnific", "fal"}:
            break
        print("Use magnific or fal.")

    return {
        "topic": topic,
        "tone": "Infer the tone and visual style from the user's prompt.",
        "duration": f"{duration} seconds",
        "characters": "",
        "language": language,
        "aspect_ratio": aspect_ratio,
        "video_quality": video_quality,
        "image_provider": image_provider,
    }

def get_review_command() -> Command:
    while True:
        action = input("Approve, edit, or regenerate? ").strip().lower()
        if action == "approve":
            return Command(resume={"action": "approve"})
        if action == "edit":
            edited_story = input("Edited story: ").strip()
            return Command(resume={"action":"edit","story":edited_story})
        if action == "regenerate":
            note=input("revision note : ").strip()
            return Command(resume={"action":"regenerate","note":note})
        print("Use approve, edit, or regenerate.")


def get_reference_review_command() -> Command:
    while True:
        action = input("Approve or regenerate the character and mood boards? ").strip().lower()
        if action == "approve":
            return Command(resume={"action": "approve"})
        if action == "regenerate":
            note = input("Reference package revision note: ").strip()
            return Command(resume={"action": "regenerate", "note": note})
        print("Use approve or regenerate.")


def get_shot_review_command(label: str) -> Command:
    while True:
        action = input(f"Approve {label} or regenerate selected shots? ").strip().lower()
        if action == "approve":
            return Command(resume={"action": "approve"})
        if action == "regenerate":
            feedback = []
            while shot_id := input("Shot ID such as shot-002 (blank when done): ").strip():
                note = input(f"Feedback for {shot_id}: ").strip()
                if note:
                    feedback.append({"shot_id": shot_id, "note": note})
            if feedback:
                return Command(resume={"action": "regenerate", "feedback": feedback})
        print("Use approve, or provide feedback for at least one shot.")


def get_plan_review_command(review: dict) -> Command:
    actions = review["actions"]
    while True:
        action = input(f"Choose {' or '.join(actions)} [{actions[0]}]: ").strip().lower() or actions[0]
        if action in actions:
            note = input("Revision note (optional): ").strip()
            return Command(resume={"action": action, "note": note})
        print(f"Use {' or '.join(actions)}.")

def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Create a video or resume a saved run.")
    parser.add_argument("--resume", nargs="?", const="latest", metavar="THREAD_ID", help="Resume the latest run or a specific run ID")
    args = parser.parse_args(argv)
    checkpointer = persistent_checkpointer(ROOT / ".langgraph-checkpoints.sqlite")
    if isinstance(checkpointer, InMemorySaver):
        parser.error("Install langgraph-checkpoint-sqlite to save and resume CLI runs.")
    graph = build_story_graph(checkpointer=checkpointer)
    if args.resume:
        thread_id = CLI_LAST_RUN_FILE.read_text(encoding="utf-8").strip() if args.resume == "latest" and CLI_LAST_RUN_FILE.is_file() else args.resume
        if thread_id == "latest":
            parser.error("No saved CLI run found. Start a new run first.")
    else:
        thread_id = str(uuid.uuid4())
        CLI_LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        CLI_LAST_RUN_FILE.write_text(thread_id, encoding="utf-8")
    print(f"Run ID: {thread_id}")
    print("To continue this run after an error, use: .venv/bin/python -m video_automation.main --resume")
    config = {"configurable": {"thread_id": thread_id}}
    if args.resume:
        snapshot = graph.get_state(config)
        if not snapshot.values:
            parser.error(f"No checkpoint found for run {thread_id}.")
        result = graph.invoke(None, config) if snapshot.next else snapshot.values
    else:
        initial_state = get_user_input()
        initial_state["thread_id"] = thread_id
        result = graph.invoke(initial_state, config)

    while "__interrupt__"  in result:
        review = result["__interrupt__"][0].value
        stage = review.get("stage")
        if stage in {"scene_plan_review", "visual_plan_review", "shot_plan_review"}:
            print(f"{stage.replace('_', ' ').title()}: {review.get('revision_reason') or 'Revision needed.'}")
            for issue in review.get("issues", []):
                print(f"- {issue}")
            result = graph.invoke(get_plan_review_command(review), config)
        elif "image_files" in review:
            print(json.dumps(list(zip(review["storyboard"], review["image_files"])), indent=2))
            result = graph.invoke(get_shot_review_command("visual storyboard"), config)
        elif "character_board_file" in review:
            print("Character board:", review["character_board_file"])
            print("Mood board:", review["mood_board_file"])
            result = graph.invoke(get_reference_review_command(), config)
        elif "director_plan" in review:
            print(json.dumps(review["director_plan"], indent=2))
            result = graph.invoke(get_shot_review_command("Director plan"), config)
        elif stage == "story_review":
            print(review["story"])
            result = graph.invoke(get_review_command(), config)
        else:
            raise RuntimeError(f"Unsupported review stage: {stage!r}")

    print(result["story"])
    print(json.dumps(result.get("storyboard", []), indent=2))
    print(json.dumps(result.get("image_files", []), indent=2))
    print(result.get("mood_board_file", ""))
    print(result.get("narration_file", ""))
    print(json.dumps(result.get("sfx_files", []), indent=2))
    print(result.get("subtitle_file", ""))
    print(result.get("video_file", ""))

if __name__ == "__main__":
    main()
