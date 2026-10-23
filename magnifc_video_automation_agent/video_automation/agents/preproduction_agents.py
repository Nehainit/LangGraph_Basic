import hashlib
import json
import logging
import math
import re
from copy import deepcopy

from video_automation.continuity_context import continuity_context
from video_automation.model_catalog import min_billed_seconds
from video_automation.agents.director_agent import _narration_words
from video_automation.models import invoke_with_evaluation, load_model
from video_automation.prompts import (
    DIALOGUE_SYSTEM_PROMPT,
    NARRATION_REVIEW_SYSTEM_PROMPT,
    NARRATION_SYSTEM_PROMPT,
    PIPELINE_CONFIG,
    SCENE_FAITHFULNESS_REVIEW_SYSTEM_PROMPT,
    SCENE_PLANNING_CONFIG,
    SCENE_PLANNING_SYSTEM_PROMPT,
    SHOT_PLANNING_CONFIG,
    SHOT_PLANNING_SYSTEM_PROMPT,
    VISUAL_FAITHFULNESS_REVIEW_SYSTEM_PROMPT,
    VISUAL_PLANNING_CONFIG,
    VISUAL_PLANNING_SYSTEM_PROMPT,
)
from video_automation.schema import AgentState
from video_automation.agents.story_agent import _duration_seconds, _parse_json


logger = logging.getLogger(__name__)
PLANNING_CONTRACT_VERSION = 5

NARRATION_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["narration_segments", "needs_story_revision", "revision_reason"],
    "properties": {
        "narration_segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string", "minLength": 1}},
            },
        },
        "needs_story_revision": {"type": "boolean"},
        "revision_reason": {"type": ["string", "null"]},
    },
}
VISUAL_CREATIVE_FIELDS = {
    "visual_action", "visual_focus", "emotional_intent", "continuity_in", "continuity_out", "story_purpose",
}
SHOT_CREATIVE_FIELDS = {
    "characters_present", "primary_subject", "visual_action", "visual_focus",
    "framing", "camera_angle", "composition", "emotion", "estimated_duration_seconds",
    "continuity_in", "continuity_out", "shot_purpose",
}
SHOT_MODEL_FIELDS = SHOT_CREATIVE_FIELDS
SHOT_PLAN_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["needs_revision", "revision_reason", "shots"],
    "properties": {
        "needs_revision": {"type": "boolean"},
        "revision_reason": {"type": ["string", "null"]},
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(SHOT_MODEL_FIELDS),
                "properties": {
                    "characters_present": {"type": "array", "items": {"type": "string"}},
                    **{
                        key: {"type": "string"} for key in SHOT_CREATIVE_FIELDS
                        if key not in {"characters_present", "estimated_duration_seconds"}
                    },
                    "estimated_duration_seconds": {"type": "number", "exclusiveMinimum": 0},
                },
            },
        },
    },
}
SCENE_PLAN_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["needs_revision", "revision_reason", "scenes"],
    "properties": {
        "needs_revision": {"type": "boolean"},
        "revision_reason": {"type": ["string", "null"]},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source_segment_number", "location", "characters_present", "scene_goal",
                    "visible_actions", "emotion", "story_purpose",
                ],
                "properties": {
                    "source_segment_number": {"type": "integer", "minimum": 1},
                    "location": {"type": "string", "minLength": 1},
                    "characters_present": {"type": "array", "items": {"type": "string", "minLength": 1}},
                    "scene_goal": {"type": "string", "minLength": 1},
                    "visible_actions": {
                        "type": "array", "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "emotion": {"type": "string", "minLength": 1},
                    "story_purpose": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}
VISUAL_PLAN_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["needs_revision", "revision_reason", "scenes"],
    "properties": {
        "needs_revision": {"type": "boolean"},
        "revision_reason": {"type": ["string", "null"]},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["visual_beats"],
                "properties": {
                    "visual_beats": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": sorted(VISUAL_CREATIVE_FIELDS),
                            "properties": {
                                key: {"type": "string", "minLength": 1}
                                for key in VISUAL_CREATIVE_FIELDS
                            },
                        },
                    },
                },
            },
        },
    },
}


def _plan_payload(data: object, item_key: str, label: str) -> dict:
    required = {"needs_revision", "revision_reason", item_key}
    if not isinstance(data, dict) or not required <= set(data):
        raise RuntimeError(f"{label} must contain needs_revision, revision_reason, and {item_key}.")
    extra = set(data) - required
    if extra:
        logger.warning("%s ignored extra top-level fields: %s", label, ", ".join(sorted(extra)))
    return {key: data[key] for key in required}


def _cast(context: dict) -> dict[str, str]:
    result = {}
    for character in context.get("characters", []):
        character_id = str(character.get("character_id", "")).strip()
        name = str(character.get("name", "")).strip()
        if character_id:
            result[character_id.casefold()] = character_id
            if name:
                result[name.casefold()] = character_id
    return result


def _named_characters(text: str, cast: dict[str, str]) -> list[str]:
    """Approved character IDs whose ID or name appears as a whole word in the text."""
    return list(dict.fromkeys(
        character_id for alias, character_id in cast.items()
        if re.search(rf"\b{re.escape(alias)}\b", text, re.IGNORECASE)
    ))


def _context(state: AgentState) -> dict:
    return continuity_context(state)


def _planning_budget(state: AgentState, stage: str, inputs: dict, maximum: int) -> tuple[dict, str, int, int]:
    # Validation feedback describes the previous attempt; it is not new source input.
    fingerprint_inputs = {
        "contract_version": PLANNING_CONTRACT_VERSION,
        **{key: value for key, value in inputs.items() if key != "revision_feedback"},
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_inputs, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    attempts = dict(state.get("planning_attempts", {}))
    previous = attempts.get(stage, {})
    used = int(previous.get("used", 0)) if previous.get("input") == fingerprint else 0
    return attempts, fingerprint, used, max(0, maximum - used)


def _planning_attempts(attempts: dict, stage: str, fingerprint: str, used: int, error: str = "") -> dict:
    attempts[stage] = {"input": fingerprint, "used": used, "last_error": error}
    return attempts


def _partition_matches(parts: list[dict], source: str) -> bool:
    return _narration_words(" ".join(str(part.get("narration", "")) for part in parts)) == _narration_words(source)


def _review_narration_traceability(model, story: dict, segments: list[dict], evaluations: list[dict]) -> list[str]:
    if hasattr(model, "format"):
        model.format = "json"
    response, evaluation = invoke_with_evaluation(
        model,
        [
            {"role": "system", "content": NARRATION_REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"story_beats": story["structure"], "narration_segments": segments}, ensure_ascii=False)},
        ],
        agent_name="narration-review",
        purpose="review_narration_traceability",
    )
    evaluation["call_number"] = len(evaluations) + 1
    evaluations.append(evaluation)
    if not isinstance(response.content, str):
        raise RuntimeError("Narration traceability review must return JSON text.")
    review = _parse_json(response.content)
    if not isinstance(review, dict) or not {"approved", "issues"}.issubset(review) or type(review["approved"]) is not bool:
        raise RuntimeError("Narration traceability review returned an invalid decision.")
    issues = review["issues"]
    if not isinstance(issues, list) or any(not isinstance(issue, str) or not issue.strip() for issue in issues):
        raise RuntimeError("Narration traceability issues must be nonempty strings.")
    if review["approved"] != (not issues):
        raise RuntimeError("Narration traceability approval and issues disagree.")
    return issues


def _is_hindi_text(text: str) -> bool:
    devanagari = sum("\u0900" <= char <= "\u097f" for char in text)
    latin = sum("a" <= char.casefold() <= "z" for char in text)
    return devanagari > 0 and devanagari >= latin


# A heard sentence longer than this is hard to follow in a short film; the narration writer is asked to split it.
MAX_SPOKEN_SENTENCE_WORDS = 16


def _long_spoken_sentences(texts: list[str]) -> list[str]:
    """Spoken sentences over the length a listener can follow on the first hearing."""
    sentences = [
        part.strip() for text in texts
        for part in re.split(r'(?<=[.!?\u2026])["\u201d]?\s+|\n+', text) if part.strip()
    ]
    return [sentence for sentence in sentences if len(_narration_words(sentence)) > MAX_SPOKEN_SENTENCE_WORDS]


ENGLISH_STYLE_EXAMPLE = (
    "Why would a turtle race a rabbit?\nEveryone laughed when he said yes.\n"
    "The rabbit bragged that he would wait at the finish line.\nHe was so sure, he took a nap...\n"
    "The turtle never stopped.\nAnd that was enough."
)
# Natural spoken Hindi: one narrator in third person, what characters say is retold, never quoted.
HINDI_STYLE_EXAMPLE = (
    "एक कछुआ... खरगोश से रेस लगाएगा?\nसब हँस पड़े।\nखरगोश ने शेखी मारी कि वो आखिरी लाइन पर इंतज़ार करेगा।\n"
    "इतना भरोसा... कि वह रास्ते में ही सो गया।\nकछुआ रुका नहीं।\nऔर बस यही काफ़ी था।"
)
# The reels formula on a different story, so the model copies the shape and never the words.
ENGLISH_REELS_EXAMPLE = {
    "cold_open": {"text": "Suddenly, the whole forest was cheering... for the turtle.", "source_beat_number": 3},
    "hook": "So how did the slowest animal in the forest beat the fastest?",
    "narration_segments": [
        {"text": "Once, a rabbit laughed at a turtle for being slow.\nThe turtle calmly asked him for a race."},
        {"text": "The rabbit ran so far ahead, he stopped for a nap...\nThe turtle never stopped."},
        {"text": "The rabbit woke up to cheering.\nThe turtle had already crossed the line."},
    ],
    "moral": "Pride falls asleep. Effort keeps walking.",
    "call_to_action": "If you believe in steady effort, comment slow and steady and like the video.",
}
HINDI_REELS_EXAMPLE = {
    "cold_open": {"text": "अचानक पूरा जंगल तालियाँ बजाने लगा... कछुए के लिए।", "source_beat_number": 3},
    "hook": "जंगल का सबसे धीमा जानवर... सबसे तेज़ को कैसे हरा गया?",
    "narration_segments": [
        {"text": "एक बार खरगोश ने कछुए की धीमी चाल का मज़ाक उड़ाया।\nकछुए ने शांति से उसे रेस के लिए कहा।"},
        {"text": "खरगोश इतना आगे निकल गया... कि रास्ते में ही सो गया।\nकछुआ रुका नहीं।"},
        {"text": "खरगोश की आँख खुली, तो तालियाँ बज रही थीं।\nकछुआ लाइन पार कर चुका था।"},
    ],
    "moral": "घमंड सो जाता है... मेहनत चलती रहती है।",
    "call_to_action": "अगर आप भी मेहनत पर भरोसा करते हैं, तो कमेंट में लिखिए धीरे चलो पर चलते रहो, और वीडियो को लाइक करें।",
}

QUOTED_TEXT = re.compile(r"\"[^\"]*\"|“[^”]*”")
# A tag after the quote ('"...," उसने कहा।' / '"...," she said.') instead of a lead-in naming the speaker first.
TRAILING_DIALOGUE_TAG = re.compile(
    r"[\"”]\s*[,.।!?]?\s*(?:[^\s\"“”]+\s+){0,2}?(?:ने\s+)?(?:कहा|पूछा|बोला|बोली|बोले|said|asked|replied|whispered|shouted)"
    r"(?:\s+\w+){0,2}\s*(?:[.।!]|$)",
    re.MULTILINE | re.IGNORECASE,
)
DEVANAGARI = "ऀ-ॿ"
FIRST_PERSON = {
    True: re.compile(rf"(?<![{DEVANAGARI}])(?:मैं|मैंने|मुझे|मेरा|मेरी|मेरे|हम|हमने|हमें|हमारा|हमारी|हमारे)(?![{DEVANAGARI}])"),
    False: re.compile(r"\b(?:I|we|We|me|my|My|our|Our|us)\b"),
}


def _narration_formula(seconds: float) -> str:
    config = PIPELINE_CONFIG.get("narration") or {}
    if str(config.get("formula", "plain")) == "reels" and seconds >= float(config.get("formula_min_seconds", 20)):
        return "reels"
    return "plain"


def _call_to_action(state: AgentState) -> bool:
    value = state.get("call_to_action")
    if value is None:
        value = (PIPELINE_CONFIG.get("narration") or {}).get("call_to_action", True)
    return bool(value)


def _hook_question() -> bool:
    return bool((PIPELINE_CONFIG.get("narration") or {}).get("hook_question", False))


def _narration_schema(formula: str, call_to_action: bool) -> dict:
    if formula == "plain":
        return NARRATION_OUTPUT_SCHEMA
    schema = deepcopy(NARRATION_OUTPUT_SCHEMA)
    text = {"type": "string"}
    schema["properties"].update({
        "cold_open": {
            "type": "object", "additionalProperties": False, "required": ["text", "source_beat_number"],
            "properties": {"text": text, "source_beat_number": {"type": "integer", "minimum": 1}},
        },
        "moral": text,
    })
    schema["required"] = ["cold_open", *schema["required"], "moral"]
    if _hook_question():
        schema["properties"]["hook"] = text
        schema["required"].insert(1, "hook")
    if call_to_action:
        schema["properties"]["call_to_action"] = text
        schema["required"].append("call_to_action")
    return schema


def _reels_instructions(beat_count: int, call_to_action: bool, hindi: bool, total_words: int) -> str:
    caps = {
        "cold_open": max(6, round(total_words * 0.12)),
        "hook": max(6, round(total_words * 0.14)),
        "moral": max(4, round(total_words * 0.08)),
        "call_to_action": max(6, round(total_words * 0.12)),
    }
    example = dict(HINDI_REELS_EXAMPLE if hindi else ENGLISH_REELS_EXAMPLE)
    hook = _hook_question()
    if not call_to_action:
        example.pop("call_to_action")
    if not hook:
        example.pop("hook")
    hook_part = (
        "2. hook: one or two short questions on the story's paradox or stakes, so the viewer must stay for the answer\n"
        "   (\"Did you know...?\"; in Hindi \"क्या आप जानते हैं...?\"). Never reveal the answer.\n" if hook else ""
    )
    cta_part = (
        "5. call_to_action: one short line asking the viewer to comment a phrase from the story's world "
        "(a devotional story suits \"comment जय श्री कृष्णा\") and like the video.\n" if call_to_action else ""
    )
    return f"""Write the voice-over in the reels formula. The parts play in this order:
1. cold_open: the most dramatic moment of the climax, narrated in one short line ("Suddenly, ..." / "अचानक ...").
   source_beat_number is the 1-based approved beat it comes from. 3 to 14 words. The story later reaches this
   moment again.
{hook_part}3. narration_segments: exactly {beat_count}, one per approved story beat, in the same order. The first starts the
   story ("Once," / "एक बार" / "दरअसल एक बार"). Retell what the characters say in reported speech.
4. moral: one short closing line. If the story resolves, the lesson or payoff in a few words; if it ends unresolved,
   a suspense line that leaves the viewer waiting for the next part, revealing nothing.
{cta_part}Word budget for {total_words} words in total: cold_open at most {caps["cold_open"]},{f' hook at most {caps["hook"]},' if hook else ''}
moral at most {caps["moral"]}{f', call_to_action at most {caps["call_to_action"]}' if call_to_action else ''}, and the rest for the story segments.
Keep each story segment's dialogue complete; never cut a line off mid-sentence to save words.
Example of the formula (a different story; copy the shape and style, never the words):
{json.dumps(example, ensure_ascii=False, indent=1)}"""


def _spoken_style_issues(segments: list[dict], hindi: bool, story_has_dialogue: bool) -> list[str]:
    """Free checks for the habits the reference Shorts never show; each becomes retry feedback."""
    issues = []
    for segment in segments:
        text, role = segment["text"], segment.get("role", "story")
        if TRAILING_DIALOGUE_TAG.search(text):
            issues.append(
                f'{segment["segment_id"]} tags a line after the quote; name the speaker before it '
                '(Arjun said, "..." / अर्जुन बोला, "...").'
            )
        if hindi:
            latin = sorted(set(re.findall(r"[A-Za-z]{2,}", text)))
            if latin:
                issues.append(f'{segment["segment_id"]} has Latin letters ({", ".join(latin[:4])}); write every word in Devanagari.')
        if role in {"story", "cold_open"} and FIRST_PERSON[hindi].search(QUOTED_TEXT.sub(" ", text)):
            issues.append(f'{segment["segment_id"]} narrates in first person; the narrator says he, she, or they outside quotes.')
        if re.search(r"[\"“”]", text):
            issues.append(
                f'{segment["segment_id"]} quotes dialogue; one narrator retells it in reported speech, without quotation marks.'
            )
        unfinished = [line.strip() for line in text.splitlines() if line.strip() and not re.search(r"[.!?।…]$", line.strip())]
        if unfinished:
            # A spoken line without a full stop runs into the next one; the voice needs the pause.
            issues.append(f'{segment["segment_id"]} has lines without ending punctuation (। . ? !): "{unfinished[0]}".')
        if role == "hook" and "?" not in text:
            issues.append("hook must ask the viewer a question.")
    return issues


def _reels_segments(data: dict, beat_ids: list[str], call_to_action: bool) -> list[tuple[str, list[str], object]]:
    """Order the reels parts as they play; each item is (role, parent beat IDs, raw text)."""
    cold_open = data.get("cold_open")
    if not isinstance(cold_open, dict):
        raise RuntimeError("cold_open must be an object with text and source_beat_number.")
    number = cold_open.get("source_beat_number")
    if type(number) is not int or not 1 <= number <= len(beat_ids):
        raise RuntimeError(f"cold_open source_beat_number must be a beat number from 1 to {len(beat_ids)}.")
    closing = [data.get("moral")]
    if call_to_action:
        closing.append(data.get("call_to_action"))
    hook = _hook_question()
    if any(not isinstance(item, str) or not item.strip() for item in [*([data.get("hook")] if hook else []), *closing]):
        raise RuntimeError(("hook, " if hook else "") + "moral" + (", and call_to_action" if call_to_action else "") + " need spoken text.")
    segments = data["narration_segments"]
    if len(segments) != len(beat_ids):
        raise RuntimeError(
            f"Narration needs exactly one segment per approved story beat; expected {len(beat_ids)}, received {len(segments)}."
        )
    return [
        ("cold_open", [beat_ids[number - 1]], cold_open.get("text")),
        # The hook may restate any beat, so it traces to all of them.
        *([("hook", list(beat_ids), data["hook"])] if hook else []),
        *[
            ("story", [beat_id], segment.get("text") if isinstance(segment, dict) else None)
            for segment, beat_id in zip(segments, beat_ids)
        ],
        ("closing", [beat_ids[-1]], "\n".join(item.strip() for item in closing)),
    ]


QUOTED_LINE = re.compile(r'"([^"]+)"|“([^”]+)”')
DIALOGUE_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lines"],
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["beat_number", "speaker", "gender", "text"],
                "properties": {
                    "beat_number": {"type": "integer", "minimum": 1},
                    "speaker": {"type": "string"},
                    "gender": {"type": "string", "enum": ["female", "male"]},
                    "text": {"type": "string"},
                },
            },
        },
    },
}


def _story_dialogue(story: dict) -> list[dict]:
    """Quoted lines in the approved beats, in order, with the beat they belong to."""
    lines = []
    for number, beat in enumerate(story.get("structure", []), start=1):
        description = str(beat.get("description", ""))
        for match in QUOTED_LINE.finditer(description):
            line = (match.group(1) or match.group(2)).strip()
            if line:
                lines.append({"beat_number": number, "line": line, "context": description})
    return lines


def _dialogue_problems(data: object, expected: list[dict], cast: dict[str, str], hindi: bool) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        return ["Return JSON with a lines list."]
    lines = data["lines"]
    if len(lines) != len(expected):
        return [f"Return exactly {len(expected)} lines, one per quoted line, in order; received {len(lines)}."]
    problems, genders = [], {}
    for index, (line, source) in enumerate(zip(lines, expected), start=1):
        if not isinstance(line, dict):
            problems.append(f"Line {index} must be an object.")
            continue
        speaker = str(line.get("speaker", "")).strip()
        if not speaker or (cast and speaker.casefold() not in cast):
            names = sorted({name for name in cast if not re.fullmatch(r"character-\d+", name)})
            problems.append(f'Line {index} speaker "{speaker}" must be one of: {", ".join(names)}.')
        gender = line.get("gender")
        if gender not in {"female", "male"}:
            problems.append(f"Line {index} gender must be female or male.")
        elif genders.setdefault(cast.get(speaker.casefold(), speaker), gender) != gender:
            problems.append(f"{speaker} must keep one gender on every line.")
        text = str(line.get("text", "")).strip().strip("\"“”")
        if not text:
            problems.append(f"Line {index} needs spoken text.")
        elif hindi and (not _is_hindi_text(text) or re.search(r"[A-Za-z]{2,}", text)):
            problems.append(f"Line {index} must be spoken Hindi in Devanagari, names included.")
    return problems


def _dialogue_narration(state: AgentState, story: dict, language: str, hindi: bool) -> dict | None:
    """Speak only the story's quoted lines, each by its character; None when the story has no dialogue."""
    expected = _story_dialogue(story)
    if not expected:
        return None
    beat_ids = [beat["beat_id"] for beat in story["structure"]]
    cast = _cast(_context(state))
    names = {alias: character_id for alias, character_id in cast.items()}
    prompt = json.dumps({
        "language": language,
        "approved_characters": _context(state)["characters"],
        "quoted_lines": expected,
        "instruction": (
            f"Return one entry per quoted line, in order, translated into {language}. "
            + ("Write natural spoken Hindi in Devanagari, names included. " if hindi else "")
            + "Never add narration or describe actions."
        ),
    }, ensure_ascii=False)
    model = load_model("narration")
    evaluations = list(state.get("llm_evaluations", []))
    error, best = "", None
    for _ in range(3):
        if hasattr(model, "format"):
            model.format = DIALOGUE_OUTPUT_SCHEMA
        response, evaluation = invoke_with_evaluation(
            model,
            [
                {"role": "system", "content": DIALOGUE_SYSTEM_PROMPT},
                {"role": "user", "content": f"{prompt}\nPrevious error: {error}" if error else prompt},
            ],
            agent_name="narration",
            purpose="generate_dialogue_script",
        )
        evaluation["call_number"] = len(evaluations) + 1
        evaluations.append(evaluation)
        try:
            data = _parse_json(response.content) if isinstance(response.content, str) else None
        except (RuntimeError, ValueError, TypeError):
            data = None
        problems = _dialogue_problems(data, expected, cast, hindi)
        if not problems or (best is None and isinstance(data, dict) and len(data.get("lines") or []) == len(expected)):
            best = (data, problems)
        if not problems:
            break
        error = " ".join(problems)
    if best is None:
        logger.warning("Dialogue script failed; using the narrator formula instead: %s", error)
        return None
    data, problems = best
    segments = []
    for index, (line, source) in enumerate(zip(data["lines"], expected), start=1):
        speaker = str(line.get("speaker", "")).strip()
        text = str(line.get("text", "")).strip().strip("\"“”").strip() or source["line"]
        segments.append({
            "segment_id": f"segment-{index:03}",
            "role": "dialogue",
            "parent_beat_ids": [beat_ids[source["beat_number"] - 1]],
            "text": text,
            "speaker_id": names.get(speaker.casefold(), speaker),
            "speaker_name": speaker,
            "voice_gender": line.get("gender") if line.get("gender") in {"female", "male"} else "female",
        })
    # A beat without dialogue is pure picture: it plays in the pause of the line before it (or after, at the start).
    spoken_beats = {beat_id for segment in segments for beat_id in segment["parent_beat_ids"]}
    for position, beat_id in enumerate(beat_ids):
        if beat_id in spoken_beats:
            continue
        before = [segment for segment in segments if beat_ids.index(segment["parent_beat_ids"][0]) < position]
        target = before[-1] if before else segments[0]
        target["parent_beat_ids"] = sorted({*target["parent_beat_ids"], beat_id}, key=beat_ids.index)
    narration = " ".join(segment["text"] for segment in segments)
    result = {
        "narration_script": narration,
        "narration_segments": segments,
        "estimated_narration_seconds": round(len(_narration_words(narration)) / 1.5, 1),
        "needs_story_revision": False,
        "story_revision_reason": None,
        "narration_feedback": "",
        "director_feedback": [],
        "llm_evaluations": evaluations,
    }
    if problems:
        result["warnings"] = [*state.get("warnings", []), f"Dialogue script warning: {' '.join(problems)}"]
    return result


def create_narration_script(state: AgentState) -> dict:
    requirements = state.get("parsed_requirements") or {}
    story = state.get("story_outline") or {
        "idea": state.get("story", ""),
        "structure": state.get("story_beats", []),
    }
    seconds = requirements.get("duration_seconds") or _duration_seconds(state["duration"])
    language = state.get("language") or requirements.get("language") or "English"
    hindi = str(language).strip().casefold() in {"hindi", "hi"}
    formula = _narration_formula(float(seconds))
    call_to_action = _call_to_action(state)
    # A "Characters speak" film is voiced only by its characters' lines, like the dialogue_only setting.
    if (PIPELINE_CONFIG.get("narration") or {}).get("dialogue_only") or state.get("voice_mode") == "lipsync":
        dialogue = _dialogue_narration(state, story, language, hindi)
        if dialogue:
            return dialogue
    beat_ids = [beat["beat_id"] for beat in story["structure"]]
    story_has_dialogue = any(
        re.search(r"[\"“”]", str(text))
        for text in [story.get("idea", ""), *(beat.get("description", "") for beat in story["structure"])]
    )
    # Expressive delivery with pauses measured 2.36 words/second (Multilingual v2, speed 0.95); the range scales
    # with the configured voice speed so the script fills the requested length without running over.
    speed = float(((PIPELINE_CONFIG.get("narration") or {}).get("voice_settings") or {}).get("speed", 0.95))
    pace = speed / 0.95
    minimum, maximum = max(8, round(seconds * 2.0 * pace)), max(8, round(seconds * 2.35 * pace))
    target_words = int(state.get("narration_target_words") or 0)
    length_instruction = (
        f"Use at most {target_words} words total across all parts; measured TTS timing requires this shorter limit."
        if target_words else f"Suggested narration length: {minimum}-{maximum} words in total; TTS will determine exact timing."
    )
    feedback = state.get("narration_feedback", "") or " ".join(
        str(item.get("note", "")) for item in state.get("director_feedback", [])
    )
    if formula == "reels":
        structure = _reels_instructions(len(beat_ids), call_to_action, hindi, target_words or maximum)
        schema_text = json.dumps({
            "cold_open": {"text": "One narrated line about the climax.", "source_beat_number": len(beat_ids)},
            **({"hook": "A question for the viewer?"} if _hook_question() else {}),
            "narration_segments": [{"text": "Short spoken lines for the corresponding approved story beat."}],
            "moral": "One short line.",
            **({"call_to_action": "One short line."} if call_to_action else {}),
            "needs_story_revision": False,
            "revision_reason": None,
        }, indent=2)
    else:
        structure = (
            "Return exactly one narration segment per approved story beat, in the same order.\n"
            "Example of the voice-over style (a different story; copy the style, never the words). Note that the speaker\n"
            "is named before each quote and nothing describes what the picture already shows:\n"
            + json.dumps(HINDI_STYLE_EXAMPLE if hindi else ENGLISH_STYLE_EXAMPLE, ensure_ascii=False)
        )
        schema_text = json.dumps({
            "narration_segments": [{"text": "Short spoken lines for the corresponding approved story beat, one sentence per line."}],
            "needs_story_revision": False,
            "revision_reason": None,
        }, indent=2)
    prompt = f"""Return JSON using exactly this schema:
{schema_text}
When needs_story_revision is true, return an empty narration_segments list and a specific revision_reason.
{structure}
Target duration: approximately {seconds} seconds.
{length_instruction}
Language: {language}
{"Write every part in Hindi in Devanagari script. Translate English story beats and dialogue into Hindi; keep their meaning and event order." if hindi else "Write every part in the requested language."}
Approved story: {json.dumps(story, ensure_ascii=False)}
Fixed user requirements: {json.dumps(requirements, ensure_ascii=False)}
Revision requirement: {feedback or 'none'}
"""
    model = load_model("narration")
    error = ""
    best = None
    evaluations = list(state.get("llm_evaluations", []))
    for _ in range(3):
        if hasattr(model, "format"):
            model.format = _narration_schema(formula, call_to_action)
        response, evaluation = invoke_with_evaluation(
            model,
            [
                {"role": "system", "content": NARRATION_SYSTEM_PROMPT},
                {"role": "user", "content": f"{prompt}\nPrevious error: {error}" if error else prompt},
            ],
            agent_name="narration",
            purpose="generate_narration_script",
        )
        evaluation["call_number"] = len(evaluations) + 1
        evaluations.append(evaluation)
        try:
            if not isinstance(response.content, str):
                raise RuntimeError("Narration Agent must return JSON text.")
            data = _parse_json(response.content)
            if not isinstance(data, dict) or not {"narration_segments", "needs_story_revision", "revision_reason"}.issubset(data):
                raise RuntimeError("Narration response does not match the required schema.")
            if type(data["needs_story_revision"]) is not bool:
                raise RuntimeError("needs_story_revision must be a boolean.")
            segments = data["narration_segments"]
            reason = data["revision_reason"]
            # Ollama sometimes leaves the revision flag true while returning usable segments.
            if data["needs_story_revision"] and isinstance(segments, list) and segments:
                data["needs_story_revision"] = False
                reason = None
            if data["needs_story_revision"]:
                if segments != [] or not isinstance(reason, str) or not reason.strip():
                    raise RuntimeError("A story revision needs empty segments and a specific reason.")
                return {
                    "narration_script": "",
                    "narration_segments": [],
                    "estimated_narration_seconds": 0.0,
                    "needs_story_revision": True,
                    "story_revision_reason": reason.strip(),
                    "review_note": reason.strip(),
                    "llm_evaluations": evaluations,
                }
            if reason not in (None, "") or not isinstance(segments, list) or not segments:
                raise RuntimeError("Approved narration needs segments and no revision reason.")
            if formula == "reels":
                parts = _reels_segments(data, beat_ids, call_to_action)
            else:
                if len(segments) != len(beat_ids):
                    raise RuntimeError(
                        f"Narration needs exactly one segment per approved story beat; expected {len(beat_ids)}, received {len(segments)}."
                    )
                parts = [
                    ("story", [beat_id], segment.get("text") if isinstance(segment, dict) else None)
                    for segment, beat_id in zip(segments, beat_ids)
                ]
            normalized = []
            texts = []
            for index, (role, parents, text) in enumerate(parts, start=1):
                if not isinstance(text, str) or not text.strip():
                    raise RuntimeError("Every narration segment needs spoken text.")
                # A line ending in a comma sounds unfinished when spoken.
                text = re.sub(r",[ \t]*(?=\n|$)", ".", text.strip())
                if hindi and not _is_hindi_text(text):
                    raise RuntimeError("Narration must be Hindi in Devanagari script in every segment.")
                texts.append(text)
                normalized.append({
                    "segment_id": f"segment-{index:03}",
                    "role": role,
                    "parent_beat_ids": parents,
                    "text": text,
                })
            segments = normalized
            narration = " ".join(texts)
            words = len(_narration_words(narration))
            # Every problem goes back in one retry; reporting only the first left the others unfixed.
            problems = []
            if target_words and words > target_words:
                problems.append(f"Narration has {words} words; use at most {target_words} words to fit measured speech timing.")
            if not target_words and words > maximum:
                # A long script runs past the film and costs another paid voice-over to shorten after timing.
                problems.append(
                    f"Narration has {words} words; use {minimum}-{maximum} words. Cut repeated images and filler, "
                    "keep every line of dialogue."
                )
            if not target_words and words < minimum:
                # A short script leaves the film short; fill it with structure, never with filler.
                problems.append(
                    f"Narration has {words} words; use {minimum}-{maximum} words. Tell more of the story through its "
                    "dialogue. Do not add filler or repeat anything."
                )
            long_lines = _long_spoken_sentences(texts)
            if long_lines:
                problems.append(
                    "These sentences are too long to follow when heard: "
                    + " | ".join(f'"{line}"' for line in long_lines[:3])
                    + f". Split every sentence into short lines of at most {MAX_SPOKEN_SENTENCE_WORDS} words."
                )
            problems.extend(_spoken_style_issues(segments, hindi, story_has_dialogue))
            # The fallback keeps the draft with the fewest problems, then the one closest to the word budget.
            limit = target_words or maximum
            rank = (len(problems), max(0, words - limit) + max(0, (0 if target_words else minimum) - words))
            if best is None or rank < best[3]:
                best = (narration, segments, round(words / 1.5, 1), rank)
            if problems:
                raise RuntimeError(" ".join(problems))
            issues = (
                _review_narration_traceability(model, story, segments, evaluations)
                if PIPELINE_CONFIG["planning"]["narration_semantic_review"] else []
            )
            if issues:
                raise RuntimeError("; ".join(issues))
            result = {
                "narration_script": narration,
                "narration_segments": segments,
                "estimated_narration_seconds": round(words / 1.5, 1),
                "needs_story_revision": False,
                "story_revision_reason": None,
                "narration_feedback": "",
                "director_feedback": [],
                "llm_evaluations": evaluations,
            }
            if not target_words and not minimum <= words <= maximum:
                result["warnings"] = [
                    *state.get("warnings", []),
                    f"Narration estimate is outside {minimum}-{maximum} words; TTS timing will decide whether regeneration is needed.",
                ]
            return result
        except (RuntimeError, ValueError, TypeError) as exc:
            error = str(exc)
    if best:
        narration, segments, estimated, _ = best
        logger.warning("Narration review advisory; continuing with current narration: %s", error)
        return {
            "narration_script": narration,
            "narration_segments": segments,
            "estimated_narration_seconds": estimated,
            "needs_story_revision": False,
            "story_revision_reason": None,
            "narration_feedback": "",
            "director_feedback": [],
            "warnings": [*state.get("warnings", []), f"Narration review warning: {error}"],
            "llm_evaluations": evaluations,
        }
    fallback_texts = [
        str(beat.get("description", "")).strip()
        for beat in story.get("structure", [])
    ]
    if fallback_texts and all(fallback_texts) and (not hindi or all(_is_hindi_text(text) for text in fallback_texts)):
        segments = [
            {"segment_id": f"segment-{index:03}", "role": "story", "parent_beat_ids": [beat["beat_id"]], "text": text}
            for index, (beat, text) in enumerate(zip(story["structure"], fallback_texts), start=1)
        ]
        narration = " ".join(fallback_texts)
        return {
            "narration_script": narration,
            "narration_segments": segments,
            "estimated_narration_seconds": round(len(_narration_words(narration)) / 1.5, 1),
            "needs_story_revision": False,
            "story_revision_reason": None,
            "narration_feedback": "",
            "director_feedback": [],
            "warnings": [
                *state.get("warnings", []),
                f"Narration model returned invalid JSON; using approved story beats as narration. Last error: {error}",
            ],
            "llm_evaluations": evaluations,
        }
    raise RuntimeError(error or "Narration Agent returned no script.")


def _segment_timing(state: AgentState) -> dict[str, dict]:
    result = {}
    timings = state.get("narration_segment_timings", [])
    for index, segment in enumerate(state.get("narration_segments", [])):
        match = next((item for item in timings if item.get("segment_id") == segment["segment_id"]), None)
        if match is None and index < len(timings):
            match = timings[index]
        if match:
            result[segment["segment_id"]] = match
    return result


def _scene_time_bounds(source_groups: list[list[str]], timing: dict[str, dict]) -> list[tuple[float, float]]:
    totals = {}
    for source_ids in source_groups:
        if len(source_ids) == 1:
            totals[source_ids[0]] = totals.get(source_ids[0], 0) + 1
    used, bounds = {}, []
    for source_ids in source_groups:
        start = float(timing[source_ids[0]]["start_seconds"])
        end = float(timing[source_ids[-1]]["end_seconds"])
        if len(source_ids) == 1 and totals[source_ids[0]] > 1:
            segment_id = source_ids[0]
            position = used.get(segment_id, 0)
            width = (end - start) / totals[segment_id]
            piece_end = end if position + 1 == totals[segment_id] else start + width * (position + 1)
            bounds.append((start + width * position, piece_end))
            used[segment_id] = position + 1
        else:
            bounds.append((start, end))
    return bounds


def _complete_scene_plan(data: object, state: AgentState) -> dict:
    data = _plan_payload(data, "scenes", "Scene plan")
    if data["needs_revision"] and data["scenes"]:
        data = {**data, "needs_revision": False, "revision_reason": None}
    if data["needs_revision"]:
        return data

    raw_scenes = data["scenes"]
    segment_ids = [segment["segment_id"] for segment in state.get("narration_segments", [])]
    timing = _segment_timing(state)
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise RuntimeError("Scene Planning Agent returned no scenes.")
    uses_segment_numbers = any(isinstance(scene, dict) and "source_segment_number" in scene for scene in raw_scenes)
    source_groups = []
    if uses_segment_numbers:
        for scene in raw_scenes:
            number = scene.get("source_segment_number") if isinstance(scene, dict) else None
            if type(number) is not int or not 1 <= number <= len(segment_ids):
                raise RuntimeError("Every scene needs a valid 1-based source_segment_number.")
            source_groups.append([segment_ids[number - 1]])
    else:  # Accept segment_count responses produced by the previous prompt during migration.
        if len(raw_scenes) > len(segment_ids):
            raise RuntimeError("Scene count cannot exceed narration segment count.")
        offset = 0
        for index, scene in enumerate(raw_scenes, start=1):
            count = scene.get("segment_count") if isinstance(scene, dict) else None
            if count is None:
                source_ids = scene.get("source_segment_ids") if isinstance(scene, dict) else None
                count = len(source_ids) if isinstance(source_ids, list) else 1
            if type(count) is not int or count < 1:
                raise RuntimeError("Every scene needs a positive integer segment_count.")
            scenes_left = len(raw_scenes) - index
            count = len(segment_ids) - offset if not scenes_left else min(count, len(segment_ids) - offset - scenes_left)
            source_groups.append(segment_ids[offset:offset + count])
            offset += count
    try:
        time_bounds = _scene_time_bounds(source_groups, timing)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise RuntimeError("Scene Planning Agent needs every narration segment's actual TTS timing.") from exc

    completed, location_ids = [], {}
    creative_fields = {
        "characters_present", "scene_goal", "visible_actions", "emotion", "story_purpose",
    }
    for index, (scene, source_ids, (start, end)) in enumerate(zip(raw_scenes, source_groups, time_bounds), start=1):
        if not isinstance(scene, dict) or not creative_fields <= set(scene):
            raise RuntimeError("Every scene needs source segment mapping and all creative scene fields.")
        if not source_ids:
            raise RuntimeError("Every scene must cover at least one narration segment.")
        location = str(scene.get("location") or scene.get("location_description") or scene.get("location_id") or "").strip()
        if not location:
            raise RuntimeError("Every scene needs a nonempty location description.")
        location_key = " ".join(location.casefold().split())
        location_id = location_ids.setdefault(location_key, f"location-{len(location_ids) + 1:03}")
        completed.append({
            "scene_id": f"scene-{index:03}",
            "source_segment_ids": source_ids,
            "start_sec": start,
            "end_sec": end,
            "location_id": location_id,
            "location_description": location,
            **{field: scene[field] for field in creative_fields},
        })
    return {**data, "scenes": completed}


def _validate_scene_plan(data: object, state: AgentState) -> tuple[list[dict], list[dict]]:
    config = SCENE_PLANNING_CONFIG
    data = _plan_payload(data, "scenes", "Scene plan")
    if type(data["needs_revision"]) is not bool:
        raise RuntimeError("Scene plan needs_revision must be a boolean.")
    if data["needs_revision"]:
        if data["scenes"] != [] or not isinstance(data["revision_reason"], str) or not data["revision_reason"].strip():
            raise RuntimeError("A scene-plan revision needs empty scenes and a specific reason.")
        return [], []
    if data["revision_reason"] not in (None, ""):
        raise RuntimeError("An approved scene plan cannot include a revision reason.")

    segments = state.get("narration_segments") or []
    segment_ids = [segment["segment_id"] for segment in segments]
    segment_text = {segment["segment_id"]: segment["text"] for segment in segments}
    timing = _segment_timing(state)
    duration = float(state.get("actual_narration_seconds") or 0)
    cast = _cast(_context(state))
    if not segment_ids or set(timing) != set(segment_ids) or duration <= 0:
        raise RuntimeError("Scene Planning Agent needs every narration segment's actual TTS timing and audio duration.")

    scenes = data["scenes"]
    fields = {
        "scene_id", "source_segment_ids", "start_sec", "end_sec", "location_id", "location_description",
        "characters_present", "scene_goal", "visible_actions", "emotion", "story_purpose",
    }
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("Scene Planning Agent returned no scenes.")
    try:
        expected_bounds = _scene_time_bounds([scene["source_segment_ids"] for scene in scenes], timing)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise RuntimeError("Every scene needs valid supplied narration timing.") from exc
    normalized, traced = [], []
    tolerance = float(config["timing"]["timing_tolerance_seconds"])
    previous_end = 0.0
    forbidden_camera_terms = ("camera", "close-up", "wide shot", "lens", "zoom", "pan ", "dolly", "tracking shot")
    for index, (scene, (expected_start, expected_end)) in enumerate(zip(scenes, expected_bounds), start=1):
        if not isinstance(scene, dict) or set(scene) != fields or scene["scene_id"] != f"scene-{index:03}":
            raise RuntimeError("Every scene must match the required schema and use consecutive scene IDs.")
        source_ids = scene["source_segment_ids"]
        if not isinstance(source_ids, list) or not source_ids:
            raise RuntimeError("Every scene needs source_segment_ids.")
        if config["scene_boundaries"]["force_one_segment_per_scene"] and len(source_ids) != 1:
            raise RuntimeError("Every scene must map to exactly one narration segment.")
        if not config["traceability"]["allow_unknown_segment_ids"] and any(item not in timing for item in source_ids):
            raise RuntimeError("Scene plan references an unknown narration segment.")
        traced.extend(source_ids)
        try:
            start, end = float(scene["start_sec"]), float(scene["end_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Every scene needs valid supplied narration timing.") from exc
        if abs(start - expected_start) > tolerance or abs(end - expected_end) > tolerance:
            logger.warning(
                "%s timing adjusted to supplied narration timing: generated=%s-%s expected=%s-%s",
                scene["scene_id"], start, end, expected_start, expected_end,
            )
            start, end = expected_start, expected_end
            scene = {**scene, "start_sec": start, "end_sec": end}
        if config["timing"]["require_monotonic_timeline"] and start + tolerance < previous_end:
            raise RuntimeError("Scene timing is not monotonic.")
        if not config["timing"]["allow_scene_overlap"] and start < previous_end - tolerance:
            raise RuntimeError("Scene timing overlaps the previous scene.")
        if not config["timing"]["allow_outside_audio_duration"] and (start < -tolerance or end > duration + tolerance):
            raise RuntimeError("Scene timing falls outside the actual narration audio duration.")
        location_id = str(scene["location_id"]).strip()
        if location_id != f"location-{list(dict.fromkeys(item['location_id'] for item in scenes)).index(location_id) + 1:03}":
            raise RuntimeError("Scene location IDs must be assigned consecutively in first-appearance order.")
        if not isinstance(scene["location_description"], str) or not scene["location_description"].strip():
            raise RuntimeError("Every scene needs a location description.")
        if not isinstance(scene["characters_present"], list):
            raise RuntimeError("Every scene needs a characters_present list.")
        try:
            characters = list(dict.fromkeys(cast[str(item).casefold()] for item in scene["characters_present"]))
        except KeyError as exc:
            raise RuntimeError(f"Scene introduced a character outside the approved cast: {exc.args[0]}") from exc
        actions = scene["visible_actions"]
        if not isinstance(actions, list) or not actions or any(not isinstance(item, str) or not item.strip() for item in actions):
            raise RuntimeError("Every scene needs at least one visible action.")
        # A character named in the scene's own actions is on screen, so later shots may draw them.
        characters.extend(
            character_id for character_id in _named_characters(" ".join(actions), cast) if character_id not in characters
        )
        if not config["visible_actions"]["allow_camera_directions"] and any(
            term in action.casefold() for action in actions for term in forbidden_camera_terms
        ):
            raise RuntimeError("Visible actions cannot contain camera directions or shot types.")
        for key in ("scene_goal", "emotion", "story_purpose"):
            if not isinstance(scene[key], str) or not scene[key].strip():
                raise RuntimeError(f"Every scene needs a nonempty {key}.")
        normalized.append({
            **scene,
            "source_segment_ids": source_ids,
            "start_sec": expected_start,
            "end_sec": expected_end,
            "location_id": location_id,
            "characters_present": characters,
            "visible_actions": [item.strip() for item in actions],
        })
        previous_end = expected_end

    traced_once = list(dict.fromkeys(traced)) if config["scene_boundaries"]["force_one_segment_per_scene"] else traced
    if config["traceability"]["require_segments_in_original_order"] and traced_once != segment_ids:
        raise RuntimeError("Scenes must cover every narration segment in order.")
    if config["coverage"]["require_all_narration_segments_covered"] and set(traced) != set(segment_ids):
        raise RuntimeError("Scene plan does not cover every narration segment.")
    analysis = [{
        "scene_id": scene["scene_id"],
        "narration": " ".join(segment_text[item] for item in scene["source_segment_ids"]),
        "location": scene["location_description"],
        "characters_present": scene["characters_present"],
        "action": " ".join(scene["visible_actions"]),
        "emotion": scene["emotion"],
        "story_purpose": scene["story_purpose"],
    } for scene in normalized]
    return normalized, analysis


def _shots_for_seconds(seconds: float, state: AgentState) -> int:
    """Shot count for a stretch of narration: each shot fits one shortest billed clip and is never too short."""
    grammar = PIPELINE_CONFIG["shot_grammar"]
    clip = float(min_billed_seconds(state))
    min_shot = clip * float(grammar["min_shot_seconds_ratio"])
    slack = float(grammar["shot_overrun_slack_seconds"])
    count = math.ceil(max(0.0, seconds - slack) / clip)
    return max(1, min(count, int(seconds // min_shot)))


def _visible_action_pacing(state: AgentState) -> list[dict]:
    return [
        {
            "segment_id": timing["segment_id"],
            "max_visible_actions": _shots_for_seconds(float(timing["end_seconds"]) - float(timing["start_seconds"]), state),
        }
        for timing in state.get("narration_segment_timings", [])
        if isinstance(timing, dict) and timing.get("segment_id")
    ]


def _film_shot_budget(state: AgentState) -> int:
    """The most shots the film's narration can fill without paying for clip seconds the edit throws away."""
    return sum(item["max_visible_actions"] for item in _visible_action_pacing(state)) or 1


def _check_shot_budget(scenes: list[dict], state: AgentState) -> None:
    """Reject a scene plan with more visible moments than the film can use; every action becomes a paid shot."""
    budget = _film_shot_budget(state)
    planned = sum(max(1, len(scene["visible_actions"])) for scene in scenes)
    if planned > budget + 1:
        raise RuntimeError(
            f"Plan at most {budget} visible actions across the film (you planned {planned}). "
            "Keep each scene's single most important action and list it first; merge scenes that share a location."
        )


def plan_scenes(state: AgentState) -> dict:
    config = SCENE_PLANNING_CONFIG
    if not config["enabled"]:
        raise RuntimeError("Scene planning is disabled in config/scene_planning.yml.")
    context = _context(state)
    inputs = {
        "approved_story_beats": (state.get("story_outline") or {}).get("structure", state.get("story_beats", [])),
        "narration_segments": state.get("narration_segments", []),
        "narration_timing": state.get("narration_segment_timings", []),
        "visible_action_pacing": _visible_action_pacing(state),
        "audio_duration_seconds": state.get("actual_narration_seconds"),
        "approved_characters": context["characters"],
        "visual_style": context["visual_style"],
        "user_requirements": state.get("parsed_requirements", {}),
        "revision_feedback": state.get("scene_plan_feedback", ""),
    }
    model = load_model("scene")
    evaluations = list(state.get("llm_evaluations", []))
    error = state.get("scene_plan_feedback", "")
    issues = []
    best = None
    maximum = int(config["retry_policy"]["max_retries"]) + 1
    attempt_state, fingerprint, used, remaining = _planning_budget(state, "scene", inputs, maximum)
    previous_error = attempt_state.get("scene", {}).get("last_error", "") if used else ""
    error = error or previous_error
    issues = [error] if error else []
    for _ in range(remaining):
        used += 1
        if hasattr(model, "format"):
            model.format = SCENE_PLAN_OUTPUT_SCHEMA
        response, evaluation = invoke_with_evaluation(
            model,
            [
                {"role": "system", "content": SCENE_PLANNING_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({
                    **inputs,
                    "retry_feedback": error,
                    "retry_instruction": (
                        "Correct the previous scene-plan response and return every required field with a valid value. "
                        "An error in your previous output is not a reason to revise narration."
                        if error else ""
                    ),
                }, ensure_ascii=False)},
            ],
            agent_name="scene-planning",
            purpose="generate_scene_plan",
        )
        evaluation["call_number"] = len(evaluations) + 1
        evaluations.append(evaluation)
        try:
            data = _parse_json(response.content)
            completed = _complete_scene_plan(data, state)
            scenes, analysis = _validate_scene_plan(completed, state)
            if completed["needs_revision"]:
                reason = completed["revision_reason"].strip()
                return {
                    "scenes": [], "scene_analysis": [], "scene_plan_needs_revision": True,
                    "scene_plan_revision_reason": reason, "scene_plan_issues": [reason],
                    "planning_attempts": _planning_attempts(attempt_state, "scene", fingerprint, used, reason),
                    "llm_evaluations": evaluations,
                }
            best = (scenes, analysis)
            # The plan is kept as the fallback; visual beats are trimmed to the budget either way.
            _check_shot_budget(scenes, state)
            if config["story_faithfulness"]["semantic_review_enabled"]:
                if hasattr(model, "format"):
                    model.format = "json"
                review, evaluation = invoke_with_evaluation(
                    model,
                    [
                        {"role": "system", "content": SCENE_FAITHFULNESS_REVIEW_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps({**inputs, "scene_plan": completed}, ensure_ascii=False)},
                    ],
                    agent_name="scene-faithfulness-review",
                    purpose="review_scene_plan_faithfulness",
                )
                evaluation["call_number"] = len(evaluations) + 1
                evaluations.append(evaluation)
                if not isinstance(review.content, str):
                    raise RuntimeError("Scene faithfulness review must return JSON text.")
                verdict = _parse_json(review.content)
                if not isinstance(verdict, dict) or not {"approved", "issues"} <= set(verdict) or type(verdict["approved"]) is not bool:
                    raise RuntimeError("Scene faithfulness review returned an invalid decision.")
                issues = verdict["issues"]
                if not isinstance(issues, list) or any(not isinstance(item, str) or not item.strip() for item in issues):
                    raise RuntimeError("Scene faithfulness issues must be nonempty strings.")
                if verdict["approved"] != (not issues):
                    raise RuntimeError("Scene faithfulness approval and issues disagree.")
                if issues:
                    raise RuntimeError("; ".join(issues))
            return {
                "scenes": scenes,
                "scene_analysis": analysis,
                "scene_timings": [
                    {"scene_number": index, "start_seconds": scene["start_sec"], "end_seconds": scene["end_sec"]}
                    for index, scene in enumerate(scenes, start=1)
                ],
                "scene_plan_needs_revision": False,
                "scene_plan_revision_reason": None,
                "scene_plan_issues": [],
                "scene_plan_feedback": "",
                "planning_attempts": _planning_attempts(attempt_state, "scene", fingerprint, used),
                "llm_evaluations": evaluations,
            }
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            error, issues = str(exc), [str(exc)]
            if config["logging"]["log_validation_failures"]:
                logger.warning("Scene plan rejected: %s", error)
            if error == previous_error:
                used = maximum
                break
            previous_error = error
    if best:
        scenes, analysis = best
        logger.warning("Scene review advisory; continuing with current plan: %s", error)
        return {
            "scenes": scenes,
            "scene_analysis": analysis,
            "scene_timings": [
                {"scene_number": index, "start_seconds": scene["start_sec"], "end_seconds": scene["end_sec"]}
                for index, scene in enumerate(scenes, start=1)
            ],
            "scene_plan_needs_revision": False,
            "scene_plan_revision_reason": None,
            "scene_plan_issues": issues,
            "scene_plan_feedback": "",
            "planning_attempts": _planning_attempts(attempt_state, "scene", fingerprint, used, error),
            "warnings": [*state.get("warnings", []), f"Scene review warning: {error}"],
            "llm_evaluations": evaluations,
        }
    return {
        "scenes": [], "scene_analysis": [], "scene_plan_needs_revision": True,
        "scene_plan_revision_reason": error, "scene_plan_issues": issues,
        "planning_attempts": _planning_attempts(attempt_state, "scene", fingerprint, used, error),
        "llm_evaluations": evaluations,
    }


def _partition_ids(values: list[str], count: int) -> list[list[str]]:
    if not values or count < 1:
        raise RuntimeError("Every planned group needs at least one approved source item.")
    if count > len(values):
        return [[values[min(index * len(values) // count, len(values) - 1)]] for index in range(count)]
    size, extra = divmod(len(values), count)
    result, offset = [], 0
    for index in range(count):
        width = size + (1 if index < extra else 0)
        result.append(values[offset:offset + width])
        offset += width
    return result


def _complete_visual_plan(data: object, state: AgentState) -> dict:
    if not isinstance(data, dict) or not {"needs_revision", "revision_reason"} <= set(data):
        raise RuntimeError("Visual plan must contain needs_revision and revision_reason.")
    groups = data.get("scenes")
    if data["needs_revision"] and isinstance(groups, list) and any(
        isinstance(group, dict) and group.get("visual_beats") for group in groups
    ):
        data = {**data, "needs_revision": False, "revision_reason": None}
    if data["needs_revision"]:
        items = groups if groups is not None else data.get("visual_beats")
        return {"needs_revision": True, "revision_reason": data["revision_reason"], "visual_beats": items}

    scenes = state.get("scenes") or []
    if not isinstance(groups, list) or len(groups) != len(scenes):
        raise RuntimeError("Visual Planner must return one ordered visual-beat group per approved scene.")
    raw_by_scene = []
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"visual_beats"} or not isinstance(group["visual_beats"], list):
            raise RuntimeError("Every visual scene group must contain only visual_beats.")
        raw_by_scene.append(group["visual_beats"])

    completed = []
    for scene, raw_beats in zip(scenes, raw_by_scene):
        if not raw_beats:
            raise RuntimeError("Every approved scene needs at least one visual beat.")
        source_groups = _partition_ids(scene["source_segment_ids"], len(raw_beats))
        segment_parents = {
            segment["segment_id"]: segment.get("parent_beat_ids", [])
            for segment in state.get("narration_segments", [])
        }
        for raw, source_ids in zip(raw_beats, source_groups):
            if not isinstance(raw, dict) or not VISUAL_CREATIVE_FIELDS <= set(raw):
                raise RuntimeError("Every generated visual beat needs all creative fields.")
            completed.append({
                "visual_beat_id": f"vb-{len(completed) + 1:03}",
                "scene_id": scene["scene_id"],
                "source_segment_ids": source_ids,
                "parent_story_beat_ids": list(dict.fromkeys(
                    parent for segment_id in source_ids for parent in segment_parents.get(segment_id, [])
                )),
                **{key: raw[key] for key in VISUAL_CREATIVE_FIELDS},
            })
    return {"needs_revision": False, "revision_reason": data["revision_reason"], "visual_beats": completed}


def _validate_visual_plan(data: object, state: AgentState) -> list[dict]:
    config = VISUAL_PLANNING_CONFIG
    data = _plan_payload(data, "visual_beats", "Visual plan")
    if type(data["needs_revision"]) is not bool:
        raise RuntimeError("Visual plan needs_revision must be a boolean.")
    if data["needs_revision"]:
        if data["visual_beats"] != [] or not isinstance(data["revision_reason"], str) or not data["revision_reason"].strip():
            raise RuntimeError("A visual-plan revision needs empty visual_beats and a specific reason.")
        return []
    if data["revision_reason"] not in (None, ""):
        raise RuntimeError("An approved visual plan cannot include a revision reason.")

    scenes = state.get("scenes") or []
    scene_ids = [scene["scene_id"] for scene in scenes]
    scene_position = {scene_id: index for index, scene_id in enumerate(scene_ids)}
    scene_segments = {scene["scene_id"]: set(scene["source_segment_ids"]) for scene in scenes}
    segments = state.get("narration_segments") or []
    segment_ids = [segment["segment_id"] for segment in segments]
    segment_position = {segment_id: index for index, segment_id in enumerate(segment_ids)}
    segment_parents = {segment["segment_id"]: set(segment.get("parent_beat_ids", [])) for segment in segments}
    story_beat_ids = {beat.get("beat_id") for beat in (state.get("story_outline") or {}).get("structure", [])}
    beats = data["visual_beats"]
    fields = {
        "visual_beat_id", "scene_id", "source_segment_ids", "parent_story_beat_ids",
        "visual_action", "visual_focus", "emotional_intent", "continuity_in",
        "continuity_out", "story_purpose",
    }
    if not isinstance(beats, list) or not beats:
        raise RuntimeError("Visual Planner Agent returned no visual beats.")
    traced_segments, traced_scenes, normalized = [], [], []
    forbidden = re.compile(
        r"\b(?:camera|lens|zoom|pan|dolly|clip)\b|close[- ]up|wide shot|tracking shot|video prompt",
        re.IGNORECASE,
    )
    for index, beat in enumerate(beats, start=1):
        if not isinstance(beat, dict) or set(beat) != fields or beat["visual_beat_id"] != f"vb-{index:03}":
            raise RuntimeError("Every visual beat must match the required schema and use consecutive visual beat IDs.")
        scene_id = beat["scene_id"]
        if config["traceability"]["allow_unknown_scene_ids"] is False and scene_id not in scene_position:
            raise RuntimeError("Visual beat references an unknown scene_id.")
        source_ids = beat["source_segment_ids"]
        if not isinstance(source_ids, list) or not source_ids:
            raise RuntimeError("Every visual beat needs source_segment_ids.")
        if config["traceability"]["allow_unknown_segment_ids"] is False and any(item not in segment_position for item in source_ids):
            raise RuntimeError("Visual beat references an unknown narration segment.")
        if any(item not in scene_segments[scene_id] for item in source_ids):
            raise RuntimeError("Visual beat references a narration segment outside its scene.")
        if [segment_position[item] for item in source_ids] != sorted(segment_position[item] for item in source_ids):
            raise RuntimeError("Visual beat source_segment_ids changed narration order.")
        parents = beat["parent_story_beat_ids"]
        available_parents = set().union(*(segment_parents[item] for item in source_ids))
        if not isinstance(parents, list) or any(item not in story_beat_ids or item not in available_parents for item in parents):
            raise RuntimeError("Visual beat parent_story_beat_ids are not supported by its source segments.")
        if config["traceability"]["require_parent_story_beat_ids_when_available"] and available_parents and not parents:
            raise RuntimeError("Visual beat needs parent_story_beat_ids from its source segments.")
        for key in ("visual_action", "visual_focus", "emotional_intent", "continuity_in", "continuity_out", "story_purpose"):
            if not isinstance(beat[key], str) or not beat[key].strip():
                raise RuntimeError(f"Every visual beat needs a nonempty {key}.")
        if any(forbidden.search(beat[key]) for key in ("visual_action", "visual_focus")):
            raise RuntimeError("Visual beats cannot contain camera, shot, clip, or video-prompt decisions.")
        traced_segments.extend(source_ids)
        traced_scenes.append(scene_id)
        normalized.append({**beat, "source_segment_ids": source_ids, "parent_story_beat_ids": parents})

    if config["coverage"]["require_all_scenes_represented"] and set(traced_scenes) != set(scene_ids):
        raise RuntimeError("Visual plan must represent every approved scene.")
    if config["coverage"]["require_all_required_scene_actions_represented"]:
        for scene in scenes:
            required_actions = scene.get("visible_actions") or []
            # Every beat is a paid shot, so a short scene folds extra actions into fewer beats.
            shot_budget = _shots_for_seconds(float(scene.get("end_sec", 0)) - float(scene.get("start_sec", 0)), state)
            if traced_scenes.count(scene["scene_id"]) < min(len(required_actions), shot_budget):
                raise RuntimeError(
                    f'{scene["scene_id"]} needs at least one focused visual beat for each required visible action.'
                )
            if traced_scenes.count(scene["scene_id"]) > shot_budget + 1:
                raise RuntimeError(
                    f'{scene["scene_id"]} has {traced_scenes.count(scene["scene_id"])} visual beats; plan at most '
                    f"{shot_budget}, because every beat becomes a paid video shot."
                )
    if not config["coverage"]["allow_uncovered_narration_segments"] and set(traced_segments) != set(segment_ids):
        raise RuntimeError("Visual plan must cover every narration segment.")
    if config["ordering"]["require_scene_order"] and [scene_position[item] for item in traced_scenes] != sorted(scene_position[item] for item in traced_scenes):
        raise RuntimeError("Visual beats changed the approved scene order.")
    if config["ordering"]["require_visual_event_order"] and [segment_position[item] for item in traced_segments] != sorted(segment_position[item] for item in traced_segments):
        raise RuntimeError("Visual beats changed the narration event order.")
    return normalized


# Distinct views of one approved action, used when a scene needs more shots than it has actions.
COVERAGE_VIEWS = (
    ("{action}", "{focus} within its surroundings"),
    ("Faces and reactions as {focus}", "the characters' emotional reaction"),
    ("The key gesture or object as {focus}", "the most important gesture or object in this moment"),
)


def _derived_visual_plan(state: AgentState) -> dict:
    """Build visual beats from approved scene actions, without a model call.

    Each scene gets the shot count its narration can fill (one shortest billed clip per shot), so no paid
    clip seconds are trimmed away. Long scenes add reaction and detail views of the same approved action;
    scenes with more actions than shots fold the extra actions into the last beat, so nothing is invented.
    """
    groups = []
    for scene in state.get("scenes") or []:
        actions = [str(action).strip() for action in scene.get("visible_actions") or [] if str(action).strip()]
        if not actions:
            raise RuntimeError(f'{scene["scene_id"]} has no visible actions to derive visual beats from.')
        seconds = float(scene.get("end_sec", 0)) - float(scene.get("start_sec", 0))
        moments = min(len(actions) * len(COVERAGE_VIEWS), _shots_for_seconds(seconds, state))
        if len(actions) > moments:
            # The scene planner lists the key action first; the rest share the final beat's frame.
            actions = [*actions[:moments - 1], "; ".join(action.rstrip(".") for action in actions[moments - 1:]) + "."]
        views = [moments // len(actions) + (1 if index < moments % len(actions) else 0) for index in range(len(actions))]
        moments_text = []
        for action, count in zip(actions, views):
            focus = action.rstrip(".")
            moments_text.extend(
                (template.format(action=action, focus=focus), focus_template.format(focus=focus))
                for template, focus_template in COVERAGE_VIEWS[:count]
            )
        goal = str(scene.get("scene_goal") or "").strip()
        groups.append({"visual_beats": [
            {
                "visual_action": action,
                "visual_focus": focus,
                "emotional_intent": str(scene.get("emotion") or "").strip(),
                "continuity_in": f"Continues from: {moments_text[index - 1][0]}" if index else f"Scene opens toward: {goal}",
                "continuity_out": (
                    f"Leads into: {moments_text[index + 1][0]}" if index + 1 < len(moments_text) else f"Scene resolves: {goal}"
                ),
                "story_purpose": str(scene.get("story_purpose") or goal).strip(),
            }
            for index, (action, focus) in enumerate(moments_text)
        ]})
    return _complete_visual_plan({"needs_revision": False, "revision_reason": None, "scenes": groups}, state)


def create_visual_beats(state: AgentState) -> dict:
    config = VISUAL_PLANNING_CONFIG
    if not config["enabled"]:
        raise RuntimeError("Visual planning is disabled in config/visual_planning.yml.")
    if PIPELINE_CONFIG["planning"]["visual_beats_mode"] == "derive_from_scenes" and not state.get("visual_plan_feedback"):
        try:
            beats = _validate_visual_plan(_derived_visual_plan(state), state)
            return {
                "visual_beats": beats,
                "visual_plan_needs_revision": False,
                "visual_plan_revision_reason": None,
                "visual_plan_issues": [],
                "visual_plan_feedback": "",
                "count_adjustment": f"Derived {len(beats)} visual beats from {len(state['scenes'])} approved scenes.",
            }
        except (RuntimeError, KeyError) as exc:
            logger.warning("Derived visual beats failed validation; asking the visual planner: %s", exc)
    inputs = {
        "narration_segments": state.get("narration_segments", []),
        "narration_timing": state.get("narration_segment_timings", []),
        "scene_plan": state.get("scenes", []),
        "approved_characters": _context(state)["characters"],
        "visual_style": _context(state)["visual_style"],
        "revision_feedback": state.get("visual_plan_feedback", ""),
    }
    model = load_model("visual-beat")
    evaluations = list(state.get("llm_evaluations", []))
    error = state.get("visual_plan_feedback", "")
    issues = []
    maximum = int(config["retry_policy"]["max_retries"]) + 1
    attempt_state, fingerprint, used, remaining = _planning_budget(state, "visual", inputs, maximum)
    previous_error = attempt_state.get("visual", {}).get("last_error", "") if used else ""
    error = error or previous_error
    issues = [error] if error else []
    for _ in range(remaining):
        used += 1
        if hasattr(model, "format"):
            model.format = deepcopy(VISUAL_PLAN_OUTPUT_SCHEMA)
            model.format["properties"]["scenes"].update(
                minItems=len(state.get("scenes") or []),
                maxItems=len(state.get("scenes") or []),
            )
        response, evaluation = invoke_with_evaluation(
            model,
            [
                {"role": "system", "content": VISUAL_PLANNING_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({
                    **inputs,
                    "retry_feedback": error,
                    "retry_instruction": (
                        "Correct the previous visual-plan response and return every required field with a valid value. "
                        "An error in your previous output is not a reason to revise the approved scene plan."
                        if error else ""
                    ),
                }, ensure_ascii=False)},
            ],
            agent_name="visual-planning",
            purpose="generate_visual_plan",
        )
        evaluation["call_number"] = len(evaluations) + 1
        evaluations.append(evaluation)
        try:
            if not isinstance(response.content, str):
                raise RuntimeError("Visual Planner Agent must return JSON text.")
            data = _complete_visual_plan(_parse_json(response.content), state)
            beats = _validate_visual_plan(data, state)
            if data["needs_revision"]:
                reason = data["revision_reason"].strip()
                return {
                    "visual_beats": [], "visual_plan_needs_revision": True,
                    "visual_plan_revision_reason": reason, "visual_plan_issues": [reason],
                    "planning_attempts": _planning_attempts(attempt_state, "visual", fingerprint, used, reason),
                    "llm_evaluations": evaluations,
                }
            if config["faithfulness"]["semantic_review_enabled"]:
                if hasattr(model, "format"):
                    model.format = "json"
                review, evaluation = invoke_with_evaluation(
                    model,
                    [
                        {"role": "system", "content": VISUAL_FAITHFULNESS_REVIEW_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps({**inputs, "visual_plan": data}, ensure_ascii=False)},
                    ],
                    agent_name="visual-faithfulness-review",
                    purpose="review_visual_plan_faithfulness",
                )
                evaluation["call_number"] = len(evaluations) + 1
                evaluations.append(evaluation)
                if not isinstance(review.content, str):
                    raise RuntimeError("Visual faithfulness review must return JSON text.")
                verdict = _parse_json(review.content)
                if not isinstance(verdict, dict) or not {"approved", "issues"} <= set(verdict) or type(verdict["approved"]) is not bool:
                    raise RuntimeError("Visual faithfulness review returned an invalid decision.")
                issues = verdict["issues"]
                if not isinstance(issues, list) or any(not isinstance(item, str) or not item.strip() for item in issues):
                    raise RuntimeError("Visual faithfulness issues must be nonempty strings.")
                if verdict["approved"] != (not issues):
                    raise RuntimeError("Visual faithfulness approval and issues disagree.")
                if issues:
                    raise RuntimeError("; ".join(issues))
            return {
                "visual_beats": beats,
                "visual_plan_needs_revision": False,
                "visual_plan_revision_reason": None,
                "visual_plan_issues": [],
                "visual_plan_feedback": "",
                "count_adjustment": f"Planned {len(beats)} visual beats dynamically across {len(state['scenes'])} scenes.",
                "planning_attempts": _planning_attempts(attempt_state, "visual", fingerprint, used),
                "llm_evaluations": evaluations,
            }
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            error, issues = str(exc), [str(exc)]
            if config["logging"]["log_validation_failures"]:
                logger.warning("Visual plan rejected: %s", error)
            if error == previous_error:
                used = maximum
                break
            previous_error = error
    return {
        "visual_beats": [], "visual_plan_needs_revision": True,
        "visual_plan_revision_reason": error, "visual_plan_issues": issues,
        "planning_attempts": _planning_attempts(attempt_state, "visual", fingerprint, used, error),
        "llm_evaluations": evaluations,
    }


def _complete_shot_plan(data: object, state: AgentState) -> dict:
    data = _plan_payload(data, "shots", "Shot plan")
    if data["needs_revision"] and data["shots"]:
        data = {**data, "needs_revision": False, "revision_reason": None}
    if not isinstance(data["shots"], list) or data["needs_revision"]:
        return data
    scenes = {scene["scene_id"]: scene for scene in state.get("scenes", [])}
    beats = state.get("visual_beats", [])
    cast = _cast(_context(state))
    if len(data["shots"]) != len(beats):
        raise RuntimeError("Shot Planner must return one ordered shot per approved visual beat.")
    completed = []
    for index, (raw, beat) in enumerate(zip(data["shots"], beats), start=1):
        if not isinstance(raw, dict):
            completed.append(raw)
            continue
        scene_id = beat["scene_id"]
        beat_ids = [beat["visual_beat_id"]]
        source_segment_ids = list(dict.fromkeys(beat.get("source_segment_ids", [])))
        shot = {key: raw[key] for key in SHOT_CREATIVE_FIELDS if key in raw}
        shot.update({
            "shot_id": f"shot-{index:03}",
            "scene_id": scene_id,
            "visual_beat_ids": beat_ids,
            "source_segment_ids": source_segment_ids,
            "location_id": scenes.get(scene_id, {}).get("location_id", ""),
            "continuity_in": raw.get("continuity_in") or beat.get("continuity_in", ""),
            "continuity_out": raw.get("continuity_out") or beat.get("continuity_out", ""),
        })
        if isinstance(shot.get("characters_present"), list):
            shot["characters_present"] = list(dict.fromkeys(
                cast.get(str(value).strip().casefold(), value) for value in shot["characters_present"]
            ))
        primary = shot.get("primary_subject")
        if isinstance(primary, str):
            shot["primary_subject"] = cast.get(primary.strip().casefold(), primary)
        completed.append(shot)

    by_scene: dict[str, list[dict]] = {}
    for shot in completed:
        if isinstance(shot, dict):
            by_scene.setdefault(shot.get("scene_id", ""), []).append(shot)
    for scene_id, shots in by_scene.items():
        scene = scenes.get(scene_id)
        try:
            weights = [float(shot["estimated_duration_seconds"]) for shot in shots]
            duration = float(scene["end_sec"]) - float(scene["start_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if duration <= 0 or any(weight <= 0 for weight in weights):
            continue
        min_shot = min_billed_seconds(state) * float(PIPELINE_CONFIG["shot_grammar"]["min_shot_seconds_ratio"])
        if any(duration * weight / sum(weights) < min_shot for weight in weights) and duration >= min_shot * len(shots):
            # A very short shot still pays for a full clip, so share the scene evenly instead.
            weights = [1.0] * len(shots)
        allocated = 0.0
        for shot, weight in zip(shots[:-1], weights[:-1]):
            shot["estimated_duration_seconds"] = round(duration * weight / sum(weights), 3)
            allocated += shot["estimated_duration_seconds"]
        shots[-1]["estimated_duration_seconds"] = round(duration - allocated, 3)
    return {**data, "shots": completed}


def _validate_shot_plan(data: object, state: AgentState) -> list[dict]:
    data = _plan_payload(data, "shots", "Shot plan")
    if type(data["needs_revision"]) is not bool:
        raise RuntimeError("Shot plan needs_revision must be a boolean.")
    if data["needs_revision"]:
        if data["shots"] != [] or not isinstance(data["revision_reason"], str) or not data["revision_reason"].strip():
            raise RuntimeError("A shot-plan revision needs empty shots and a specific reason.")
        return []
    if data["revision_reason"] not in (None, ""):
        raise RuntimeError("An approved shot plan cannot include a revision reason.")

    scenes = state.get("scenes") or []
    scene_by_id = {scene["scene_id"]: scene for scene in scenes}
    scene_position = {scene["scene_id"]: index for index, scene in enumerate(scenes)}
    beats = state.get("visual_beats") or []
    beat_by_id = {beat["visual_beat_id"]: beat for beat in beats}
    beat_position = {beat["visual_beat_id"]: index for index, beat in enumerate(beats)}
    segment_ids = {segment["segment_id"] for segment in state.get("narration_segments", [])}
    cast = _cast(_context(state))
    fields = {
        "shot_id", "scene_id", "visual_beat_ids", "source_segment_ids", "characters_present",
        "primary_subject", "visual_action", "visual_focus", "framing", "camera_angle",
        "composition", "emotion", "location_id", "estimated_duration_seconds",
        "continuity_in", "continuity_out", "shot_purpose",
    }
    shots = data["shots"]
    if not isinstance(shots, list) or not shots:
        raise RuntimeError("Shot Planner Agent returned no shots.")

    traced_beats, scene_sequence, durations = [], [], {}
    camera_motion = re.compile(r"\b(?:push[- ]?in|pull[- ]?out|pan|tilt|orbit|track(?:ing)?|dolly|crane|zoom)\b", re.IGNORECASE)
    normalized = []
    for index, shot in enumerate(shots, start=1):
        if not isinstance(shot, dict) or set(shot) != fields or shot["shot_id"] != f"shot-{index:03}":
            raise RuntimeError("Every shot must match the required schema and use consecutive shot IDs.")
        scene_id = shot["scene_id"]
        if scene_id not in scene_by_id:
            raise RuntimeError("Shot references an unknown scene_id.")
        beat_ids = shot["visual_beat_ids"]
        if not isinstance(beat_ids, list) or not beat_ids or any(beat_id not in beat_by_id for beat_id in beat_ids):
            raise RuntimeError("Every shot needs known visual_beat_ids.")
        if any(beat_by_id[beat_id]["scene_id"] != scene_id for beat_id in beat_ids):
            raise RuntimeError("Shot visual_beat_ids must belong to its scene.")
        positions = [beat_position[beat_id] for beat_id in beat_ids]
        if positions != sorted(positions):
            raise RuntimeError("Shot visual_beat_ids changed the visual event order.")
        expected_segments = list(dict.fromkeys(
            segment_id for beat_id in beat_ids for segment_id in beat_by_id[beat_id]["source_segment_ids"]
        ))
        if shot["source_segment_ids"] != expected_segments or any(segment_id not in segment_ids for segment_id in expected_segments):
            raise RuntimeError("Shot source_segment_ids must match its visual beats.")
        if shot["location_id"] != scene_by_id[scene_id]["location_id"]:
            raise RuntimeError("Shot location_id must match its approved scene.")
        if not isinstance(shot["characters_present"], list):
            raise RuntimeError("Every shot needs a characters_present list.")
        try:
            raw_characters = [str(value).strip() for value in shot["characters_present"]]
            characters = list(dict.fromkeys(cast[value.casefold()] for value in raw_characters))
            scene_characters = {cast[str(value).strip().casefold()] for value in scene_by_id[scene_id]["characters_present"]}
        except KeyError as exc:
            raise RuntimeError(f"Shot introduced a character outside the approved cast: {exc.args[0]}") from exc
        if raw_characters != characters:
            raise RuntimeError("Shot characters_present must use exact approved character IDs, not display names.")
        # A character named in the visible action is drawn by the image model, so it needs its reference sheet.
        depicted = " ".join(str(shot.get(key, "")) for key in ("visual_action", "visual_focus", "composition"))
        characters.extend(
            character_id for character_id in _named_characters(depicted, cast) if character_id not in characters
        )
        outside = [character_id for character_id in characters if character_id not in scene_characters]
        if outside:
            # Name the shot and the character, so the retry can fix it instead of repeating it.
            names = {character_id: alias for alias, character_id in cast.items() if alias != character_id.casefold()}
            raise RuntimeError(
                f"{shot['shot_id']} shows {', '.join(f'{names.get(item, item)} ({item})' for item in outside)}, "
                f"who is not in {scene_id} (allowed: {', '.join(sorted(scene_characters)) or 'nobody'}). "
                "Remove them from characters_present and from visual_action, visual_focus, and composition; "
                "a line spoken to them does not put them on screen."
            )
        for key in (
            "primary_subject", "visual_action", "visual_focus", "framing", "camera_angle",
            "composition", "emotion", "continuity_in", "continuity_out", "shot_purpose",
        ):
            if not isinstance(shot[key], str) or not shot[key].strip():
                raise RuntimeError(f"Every shot needs a nonempty {key}.")
        primary = shot["primary_subject"].strip()
        if primary.casefold() in cast and primary != cast[primary.casefold()]:
            raise RuntimeError("Shot primary_subject must use the approved character ID, not its display name.")
        generic_phrases = {
            "visual_focus": ("in the approved location", "character in the scene", "main subject visible"),
            "composition": ("character is centered in the frame", "clearly visible at the center of the frame"),
            "continuity_in": ("enters the location",),
            "continuity_out": ("completes the visible moment",),
        }
        for key, phrases in generic_phrases.items():
            if any(phrase in shot[key].casefold() for phrase in phrases):
                raise RuntimeError(f"Shot {key} is too generic for image generation.")
        if any(camera_motion.search(shot[key]) for key in ("framing", "camera_angle", "composition")):
            raise RuntimeError("Shot Planner cannot specify camera movement.")
        try:
            duration = float(shot["estimated_duration_seconds"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Shot estimated_duration_seconds must be a number.") from exc
        if duration <= 0:
            raise RuntimeError("Shot estimated_duration_seconds must be positive.")
        durations[scene_id] = durations.get(scene_id, 0.0) + duration
        traced_beats.extend(beat_ids)
        scene_sequence.append(scene_id)
        normalized.append({**shot, "characters_present": characters, "estimated_duration_seconds": duration})

    if set(traced_beats) != set(beat_by_id):
        raise RuntimeError("Shot plan must cover every approved visual beat.")
    if [scene_position[item] for item in scene_sequence] != sorted(scene_position[item] for item in scene_sequence):
        raise RuntimeError("Shots changed the approved scene order.")
    if [beat_position[item] for item in traced_beats] != sorted(beat_position[item] for item in traced_beats):
        raise RuntimeError("Shots changed the approved visual event order.")
    tolerance = float(SHOT_PLANNING_CONFIG["timing_tolerance_seconds"])
    for scene_id, scene in scene_by_id.items():
        expected = float(scene["end_sec"]) - float(scene["start_sec"])
        if abs(durations.get(scene_id, 0.0) - expected) > tolerance:
            raise RuntimeError(f"Shots for {scene_id} do not preserve its narration timing.")
    return _apply_shot_grammar(normalized, beat_by_id)


# Example values from the shot planner prompt; small models sometimes copy them verbatim.
SHOT_PLACEHOLDERS = {
    key: value for key, value in re.findall(
        r'"(\w+)": "([^"]+)"', SHOT_PLANNING_SYSTEM_PROMPT.split("Successful shape:", 1)[-1].split("}]", 1)[0]
    )
    if key not in {"framing", "camera_angle", "primary_subject"}
}
# Approved visual-beat fields that can stand in for a copied placeholder.
BEAT_FALLBACKS = {
    "visual_action": "visual_action", "visual_focus": "visual_focus", "emotion": "emotional_intent",
    "continuity_in": "continuity_in", "continuity_out": "continuity_out", "shot_purpose": "story_purpose",
}


def _next_framing(framing: str) -> str:
    key = framing.casefold()
    if "close" in key:
        return "wide"
    return "medium" if "wide" in key else "close_up"


def _apply_shot_grammar(shots: list[dict], beat_by_id: dict[str, dict]) -> list[dict]:
    """Enforce shot variety; repairs what code can repair and rejects only genuine repeats."""
    grammar = PIPELINE_CONFIG["shot_grammar"]

    def action_key(text: str) -> str:
        return " ".join(re.findall(r"\w+", text.casefold()))

    result, seen = [], {}
    for shot in shots:
        shot = dict(shot)
        beat = beat_by_id.get(shot["visual_beat_ids"][0], {}) if shot.get("visual_beat_ids") else {}
        for key, placeholder in SHOT_PLACEHOLDERS.items():
            if str(shot.get(key, "")).strip() == placeholder:
                fallback = str(beat.get(BEAT_FALLBACKS.get(key, ""), "")).strip()
                if not fallback:
                    raise RuntimeError(f"{shot['shot_id']} copied the example {key}; describe this shot's own {key}.")
                shot[key] = fallback
        if grammar["reject_duplicate_actions"] and action_key(shot["visual_action"]) in seen:
            if not beat.get("visual_action") or action_key(beat["visual_action"]) in seen:
                raise RuntimeError(
                    f"{shot['shot_id']} repeats the visual action of {seen[action_key(shot['visual_action'])]}; "
                    "give every shot a distinct moment."
                )
            # The approved beat already names a distinct moment, so use it instead of the repeat.
            shot["visual_action"], shot["visual_focus"] = beat["visual_action"], beat.get("visual_focus") or beat["visual_action"]
        seen[action_key(shot["visual_action"])] = shot["shot_id"]
        previous = result[-1] if result else None
        if grammar["vary_repeated_framing"] and previous and previous["scene_id"] == shot["scene_id"] and (
            (previous["framing"], previous["camera_angle"]) == (shot["framing"], shot["camera_angle"])
        ):
            shot["framing"] = _next_framing(previous["framing"])
        result.append(shot)
    return result


def _shot_storyboard(shots: list[dict], state: AgentState) -> list[dict]:
    segments = {segment["segment_id"]: segment["text"] for segment in state.get("narration_segments", [])}
    beats = {beat["visual_beat_id"]: beat for beat in state.get("visual_beats", [])}
    used_segments, cursor, storyboard = set(), 0.0, []
    for index, shot in enumerate(shots, start=1):
        narration = " ".join(
            segments[segment_id] for segment_id in shot["source_segment_ids"]
            if segment_id not in used_segments
        )
        used_segments.update(shot["source_segment_ids"])
        duration = shot["estimated_duration_seconds"]
        camera = f"{shot['framing']} {shot['camera_angle']}; {shot['composition']}"
        storyboard.append({
            **shot,
            "shot_number": index,
            "scene_number": index,
            "story_purpose": beats[shot["visual_beat_ids"][0]]["story_purpose"],
            "duration_seconds": duration,
            "start_time": _clock(cursor),
            "end_time": _clock(cursor + duration),
            "narration": narration,
            "visuals": shot["visual_action"],
            "camera": camera,
            "motion": "static",
            "subject_motion": shot["visual_action"],
            "transition_to_next": {"type": "cut", "duration_seconds": 0.0, "audio_bridge": "none"},
            "sfx_prompt": "",
        })
        cursor += duration
    return storyboard


def _clock(seconds: float) -> str:
    minutes, remaining = divmod(seconds, 60)
    return f"{int(minutes):02}:{remaining:05.2f}".rstrip("0").rstrip(".")


def plan_shots(state: AgentState) -> dict:
    config = SHOT_PLANNING_CONFIG
    if not config["enabled"]:
        raise RuntimeError("Shot planning is disabled in config/shot_planning.yml.")
    inputs = {
        "scene_plan": state.get("scenes", []),
        "visual_beats": state.get("visual_beats", []),
        "approved_characters": _context(state)["characters"],
        "visual_style": _context(state)["visual_style"],
        "revision_feedback": state.get("shot_plan_feedback", ""),
    }
    model = load_model("shot-planner")
    if hasattr(model, "format"):
        model.format = deepcopy(SHOT_PLAN_OUTPUT_SCHEMA)
        model.format["properties"]["shots"].update(
            minItems=len(state.get("visual_beats") or []),
            maxItems=len(state.get("visual_beats") or []),
        )
    evaluations = list(state.get("llm_evaluations", []))
    error = state.get("shot_plan_feedback", "")
    maximum = int(config["max_retries"]) + 1
    attempt_state, fingerprint, used, remaining = _planning_budget(state, "shot", inputs, maximum)
    previous_error = attempt_state.get("shot", {}).get("last_error", "") if used else ""
    error = error or previous_error
    for _ in range(remaining):
        used += 1
        response, evaluation = invoke_with_evaluation(
            model,
            [
                {"role": "system", "content": SHOT_PLANNING_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({**inputs, "retry_feedback": error}, ensure_ascii=False)},
            ],
            agent_name="shot-planning",
            purpose="generate_shot_plan",
        )
        evaluation["call_number"] = len(evaluations) + 1
        evaluations.append(evaluation)
        try:
            if not isinstance(response.content, str):
                raise RuntimeError("Shot Planner Agent must return JSON text.")
            data = _complete_shot_plan(_parse_json(response.content), state)
            shots = _validate_shot_plan(data, state)
            if data["needs_revision"]:
                reason = data["revision_reason"].strip()
                return {
                    "shot_plan": [], "shot_plan_needs_revision": True,
                    "shot_plan_revision_reason": reason, "shot_plan_issues": [reason],
                    "planning_attempts": _planning_attempts(attempt_state, "shot", fingerprint, used, reason),
                    "llm_evaluations": evaluations,
                }
            storyboard = _shot_storyboard(shots, state)
            return {
                "shot_plan": shots,
                "director_plan": storyboard,
                "storyboard": storyboard,
                "shot_plan_needs_revision": False,
                "shot_plan_revision_reason": None,
                "shot_plan_issues": [],
                "shot_plan_feedback": "",
                "count_adjustment": f"Planned {len(shots)} image-ready shots from {len(state['visual_beats'])} visual beats.",
                "planning_attempts": _planning_attempts(attempt_state, "shot", fingerprint, used),
                "llm_evaluations": evaluations,
            }
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            error = str(exc)
            if error == previous_error:
                used = maximum
                break
            previous_error = error
    return {
        "shot_plan": [], "shot_plan_needs_revision": True,
        "shot_plan_revision_reason": error, "shot_plan_issues": [error],
        "planning_attempts": _planning_attempts(attempt_state, "shot", fingerprint, used, error),
        "llm_evaluations": evaluations,
    }


def critique_shots(state: AgentState) -> dict:
    if not PIPELINE_CONFIG["planning"]["director_critic"]:
        # Shot grammar and cast checks already run in _validate_shot_plan; this critic is advisory only.
        return {
            "director_plan": state["director_plan"],
            "storyboard": state["storyboard"],
            "director_approved": True,
            "critic_issues": [],
        }
    prompt = f"""
You are the Director/Critic Agent. Return JSON only: {{"approved":true,"issues":[]}}.
Check story, narration, visual-beat traceability, exact cast, location, still-image clarity, static framing,
continuity, timing, and absence of invented or filler shots. Do not rewrite the shot plan.
Narration: {state['narration_script']}
Visual beats: {state['visual_beats']}
Approved characters: {_context(state)['characters']}
Requested visual style: {_context(state)['visual_style']}
Shot plan: {state['shot_plan']}
"""
    model = load_model("director")
    response, evaluation = invoke_with_evaluation(
        model, prompt, agent_name="director-critic", purpose="review_shot_plan"
    )
    evaluations = [*state.get("llm_evaluations", []), {**evaluation, "call_number": len(state.get("llm_evaluations", [])) + 1}]
    review = _parse_json(response.content)
    issues = review.get("issues", [])
    if not isinstance(issues, list) or any(not isinstance(issue, str) or not issue.strip() for issue in issues):
        raise RuntimeError("Director/Critic Agent returned invalid issues.")
    if review.get("approved") is not True or issues:
        reason = "; ".join(issues) or "Director/Critic Agent rejected the shot plan."
        logger.warning("Director review advisory; continuing with current shot plan: %s", reason)
        return {
            "director_plan": state["director_plan"],
            "storyboard": state["storyboard"],
            "director_approved": True,
            "critic_issues": issues,
            "warnings": [*state.get("warnings", []), f"Director review warning: {reason}"],
            "llm_evaluations": evaluations,
        }
    return {
        "director_plan": state["director_plan"],
        "storyboard": state["storyboard"],
        "director_approved": True,
        "critic_issues": [],
        "llm_evaluations": evaluations,
    }
