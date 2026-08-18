def story_prompt(
    *,
    topic: str,
    tone: str,
    duration: str,
    language: str,
    characters: str,
    max_words: int,
    identity_rule: str,
    review_note: str,
    feedback: str,
) -> str:
    return f"""
Return JSON only: {{"story": "..."}}.
Create only the story text, not frames, not scenes, not timestamps, not image prompts.
Topic: {topic}
Tone: {tone}
Duration: {duration}
Language: {language}
Characters: {characters}
Maximum words: {max_words}.
Use 3 or 4 sentences only:
1. Start: place the characters in the situation.
2. Turn: something goes wrong, changes, or creates a choice.
3. Landing ending: resolve the choice and make the final feeling clear.
4. Optional final beat only if needed.
Treat character entries as fixed names/descriptions; do not change them or invent new identities.
{identity_rule}
{review_note}
The story value must be a single narrative paragraph.
{feedback}
"""


def storyboard_prompt(*, duration: str, scene_count: int, story: str, feedback: str) -> str:
    return f"""
Return JSON only: {{"storyboard": [...]}}.
Create a video storyboard from this story.
Duration: {duration}
Scene count: exactly {scene_count}
Story: {story}

Each storyboard item must include:
- scene_number: integer
- start_time: timestamp like "00:00"
- end_time: timestamp like "00:12"
- visuals: concrete visual description for image/video generation
- camera: camera movement or framing
- narration: matching story/narration line

Keep scenes in chronological order and cover the full story arc.
{feedback}
"""


def image_prompt(scene: dict) -> str:
    return (
        f"{scene['visuals']} "
        f"Camera: {scene['camera']}. "
        "Vertical 9:16 cinematic story frame, rich lighting, consistent characters, "
        "no text, no watermark, no logo."
    )


def character_sheet_prompt(characters: list[str]) -> str:
    character_text = ", ".join(characters)
    return (
        f"Clean combined character reference sheet for: {character_text}. "
        "Show each character separately in the same image, full-body front view, "
        "consistent face, outfit, hairstyle, and body shape. Plain white background, "
        "clear spacing between characters, no text, no watermark, no logo."
    )
