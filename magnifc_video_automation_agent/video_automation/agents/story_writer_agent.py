"""Story writer: drafts and revises one short story with the user, chat style, for the studio's prompt box."""

from video_automation.agents.story_agent import _check_input_safety, _parse_json
from video_automation.models import invoke_with_evaluation, load_model
from video_automation.prompts import STORY_WRITER_SYSTEM_PROMPT

# The studio's prompt box holds 2000 characters, so a draft must fit it.
MAX_STORY_CHARS = 2000
MAX_ATTEMPTS = 3
MAX_CHARACTERS = 8
# Matches the character library's field limits, so a listed character can prefill a new library entry.
CHARACTER_LIMITS = {"name": 60, "kind": 200, "appearance": 1200}


def _target_words(seconds: int) -> int:
    # Narration runs at about 2.5 words a second; the story is the brief, so it may run a little longer.
    return max(60, min(300, round(seconds * 2.5)))


def _characters(value: object, saved_names: list[str]) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_CHARACTERS:
        raise ValueError(f"characters must be a list of at most {MAX_CHARACTERS} characters.")
    characters = []
    for character in value:
        if not isinstance(character, dict) or set(character) != set(CHARACTER_LIMITS):
            raise ValueError("Each character must contain exactly name, kind, and appearance.")
        if any(not isinstance(character[key], str) for key in CHARACTER_LIMITS) or not character["name"].strip() or not character["kind"].strip():
            raise ValueError("Each character needs a nonempty name and kind; appearance is a string.")
        cleaned = {key: character[key].strip()[:limit] for key, limit in CHARACTER_LIMITS.items()}
        cleaned["name"] = cleaned["name"].lstrip("@").strip()
        # Only a name that is really in the library is dropped; an @ the model or user typed never hides anyone.
        if cleaned["name"] and cleaned["name"].casefold() not in {name.casefold() for name in saved_names}:
            characters.append(cleaned)
    return characters


def _validated(value: object, saved_names: list[str]) -> dict:
    if not isinstance(value, dict) or set(value) != {"title", "story", "reply", "characters"}:
        raise ValueError("Return exactly title, story, reply, and characters.")
    if any(not isinstance(value[key], str) or not value[key].strip() for key in ("title", "story", "reply")):
        raise ValueError("title, story, and reply must be nonempty strings.")
    if len(value["story"].strip()) > MAX_STORY_CHARS:
        raise ValueError(f"The story is too long; keep it under {MAX_STORY_CHARS} characters (about 300 words).")
    return {**{key: value[key].strip() for key in ("title", "story", "reply")}, "characters": _characters(value["characters"], saved_names)}


def write_story(messages: list[dict], *, seconds: int = 30, language: str = "English", saved_names: list[str] = ()) -> dict:
    """Return {title, story, reply, characters, llm_evaluations}; the last message is the user's.

    saved_names are the user's library characters; characters lists the story's visible characters not among them.
    """
    # Raises UnsafeContentError for a blocked message, before any story is written.
    evaluations = [_check_input_safety({"story_writer_message": messages[-1]["content"]})]
    system = STORY_WRITER_SYSTEM_PROMPT.replace("{words}", str(_target_words(seconds))).replace(
        "{seconds}", str(seconds)).replace("{language}", language).replace(
        "{saved}", ", ".join(saved_names) if saved_names else "none yet")
    prompt = [{"role": "system", "content": system}, *messages]
    model = load_model("story-writer")
    feedback = ""
    for _ in range(MAX_ATTEMPTS):
        attempt = prompt + ([{"role": "user", "content": f"Your last answer was rejected: {feedback} Answer again as JSON."}] if feedback else [])
        response, evaluation = invoke_with_evaluation(model, attempt, agent_name="story-writer", purpose="write_story")
        evaluations.append(evaluation)
        try:
            if not isinstance(response.content, str):
                raise ValueError("Answer with JSON text.")
            return {**_validated(_parse_json(response.content), list(saved_names)), "llm_evaluations": evaluations}
        except (ValueError, TypeError) as exc:
            feedback = str(exc)
    raise RuntimeError(f"The story writer did not return a usable story: {feedback}")
