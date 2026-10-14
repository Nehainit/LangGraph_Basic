# Narration and Cuts Redesign Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking. Run only the mocked test suite while implementing; any live LLM comparison, ElevenLabs or fal run needs the owner's go-ahead first.

## Status (2026-10-14)

- **Task B (cuts, shots, motion) was reverted.** The owner confirmed the visuals were fine; only narration was the problem.
- **Task A changed after review.** The final direction is one narrator retelling events in reported speech, with no quoted dialogue. It opens on the dramatic moment and ends on suspense. The question hook and CTA are off by default. Character voices (`narration.dialogue_only`) exist but are off: without lip sync they looked wrong.
- **Also shipped:**
  - free CC0 music and timed sound effects from Freesound (`video_automation/sound_library.py`), replacing fal music;
  - the combined judge on OpenAI GPT-5, with frames taken inside spoken lines;
  - Hindi subtitle grouping fixes;
  - crash-retry from the studio.
- **Next:** Shorts packaging (bold captions, a hook in the first 1.5 s, push-ins in the edit, −14 LUFS audio, an end card).

**Goal:** Make films sound and cut like the narration Shorts that work. Today a film opens on scene-setting, has 3 slow shots of ~7 s, tags dialogue after the quote ("…, अर्जुन ने कहा"), and ends on a visual description ("they looked toward the city").

## Evidence (analysis of 2026-10-13)

Reference: The Harish Fact, Hindi mythology Shorts with AI visuals in our style. The three analyzed have 34M, 5.5M and 4.6M views and all follow one formula. The Divine Tales (21K views) is the weaker comparison.

| Part | Time | What they do |
|---|---|---|
| Cold open | 0–7 s | A dramatic dialogue line from the climax, spoken in character, over the climax image. Then a 1.5–2 s pause. |
| Hook | 7–17 s | One or two paradox questions: "क्या आप जानते हैं…" / "क्या आपने कभी सोचा है…" |
| Story start | ~17 s | Music sting, then "दरअसल एक बार…" / "कथा उस समय की है…" |
| Story | middle | Simple past, mostly dialogue. The speaker is named **before** the line ("सत्यभामा बोली, प्रभु…"). An action is described only when it means something ("Krishna smiled but said nothing"). |
| Ending | last ~8 s | One-line moral, then a CTA ("comment जय श्री कृष्णा", "like"). |

- **Pace:** 2.5–3.2 spoken words per second. Ours is 2.0–2.35.
- **Cuts land on names.** Every hard cut falls on the word naming a new character or place (कृष्ण 53.3 s, हिमालय 58.6 s, सत्यभामा 76.6 s, गरुड़ 82.8 s, हनुमान 103.7 s).
- **Shots are 3–5 s.** Every clip pushes in hard (wide → medium → extreme close-up), so the picture never sits still.
- **Transitions:** a dip to black or a dissolve between characters or places; hard cuts within a scene.
- **The cold-open image is the climax image**, shown first and again near the end.

Videos: PHkpVmSq6_E, Qt4tw4raQYY, 8j3eIGXX2hI (Harish Fact), fSdZIte4hog (Divine Tales).

## Task A — Narration formula

**Files:** `video_automation/prompts.py` (`NARRATION_SYSTEM_PROMPT`, `NARRATION_REVIEW_SYSTEM_PROMPT`), `video_automation/agents/preproduction_agents.py` (`NARRATION_OUTPUT_SCHEMA`, `create_narration_script`, style examples), `tests/test_preproduction_agents.py`.

- [ ] **A1. Structured script schema.** Replace the flat "one segment per beat" output with:
  ```json
  {
    "cold_open": {"text": "\"…climax line…\"", "speaker": "Garuda", "source_beat_id": "beat-004"},
    "hook": ["क्या आप जानते हैं…?"],
    "story": [{"beat_id": "beat-001", "text": "दरअसल एक बार…"}],
    "moral": "…",
    "cta": "…"
  }
  ```
  The backend still flattens these into `narration_segments` (`segment-001…`) with `parent_beat_ids`, so TTS, scene planning and subtitles keep working. The cold open and the moral point at the climax beat. The hook points at every beat it restates.
- [ ] **A2. Prompt rules from the evidence:**
  - Open on a quoted climax line in character.
  - Then 1–2 paradox questions.
  - Then "एक बार…" or the English equivalent ("Once…").
  - Name the speaker before the quote, never after it.
  - Describe an action only when it means something; no filler description.
  - Narrator in third person.
  - End with a one-line moral.
- [ ] **A3. Free code checks with retry feedback** (no LLM):
  - The cold open contains a quote.
  - The hook ends in "?".
  - No trailing tag after a closing quote (`["”]\s*(उसने|वह|\w+\s+ने)\s+(कहा|पूछा)`, and "he said" in English).
  - No Latin letters outside quotes in Hindi.
  - No मैं/हम outside quotes.
- [ ] **A4. Style examples.** Rewrite the Hindi and English examples (a different story) to follow the formula: cold open → hook → एक बार → moral.
- [ ] **A5. Pace.** Word range goes from `seconds × 2.0–2.35` to `seconds × 2.6–3.0`, and the ElevenLabs speed setting from 0.95 to 1.05–1.1 (`config/pipeline.yml` `narration.voice_settings`). This needs one paid TTS check to re-measure words per second (about $0.02).
- [ ] **A6. Reviewer prompt.** Allow the cold open (it repeats the climax line), the hook, the moral and the CTA. Still reject new events, characters or dialogue.
- [ ] **A7. CTA.** Add a toggle (default on) in requirements and the studio form; the text is generated in the narration language.
- [ ] **A8. Writer model** (needs approval, about 5–15¢): run the same 3 stories through gpt-5-mini, gpt-5, Gemini 3.6 Flash and Claude, then pick the `writing` chain head in `config/models.yml`. Claude needs `ANTHROPIC_API_KEY` and a small `AnthropicChat` provider in `models.py`.

## Task B — Cuts, shots and motion

**Files:** `preproduction_agents.py` (shot planning, `_shots_for_seconds`), `config/pipeline.yml` (`shot_grammar`), `video_automation/agents/narration_agent.py` (alignment), `motion_planner_agent.py` and `config/motion_planning.yml`, `editor_agent.py`, `rough_video_compiler.py`, `subtitle_agent.py`, `sound_bed_agent.py`.

- [ ] **B1. Word timings.** Turn ElevenLabs `character_start_times_seconds` (already used in `_scene_timings`) into per-word start and end times saved in state (`narration_word_timings`).
- [ ] **B2. Cut on the name.** After shot planning, snap each shot boundary to the start of the nearest word (±1 s) that first names a character, place or key object in that shot. This is code, not an LLM call, and uses the cast and location names from the approved characters and locations.
- [ ] **B3. Shot length 3–5 s.** Set a target of 4 s in `shot_grammar`, so a 30 s film gets about 7–8 shots instead of 3. The shot budget from the cost plan still decides the count.
- [ ] **B4. Cold-open shot reuses the climax frame.** Shot 1 uses the climax shot's image (no extra image cost) with its own motion, and the edit returns to it at the climax.
- [ ] **B5. Strong push-in on every shot.** Change the motion planning default from "subtle push-in" to a strong push-in (wide → close-up over the clip). The free static tier (FFmpeg) zooms 1.0 → 1.3 instead of a gentle drift.
- [ ] **B6. Transitions.** Dip to black (0.3 s) or a dissolve when the location or the character group changes; hard cuts inside a scene. Add a 1.5 s pause plus a music hit after the cold open, and a music sting where the story starts.
- [ ] **B7. Silent visual beats** (from notes item 8):
  - A beat may have empty speech and a `min_seconds` (shot length = max(speech, min_seconds)).
  - The duration check in `rough_video_compiler.py:170` counts the silence.
  - Films reach the requested length.
- [ ] **B8. Subtitles** break on phrase boundaries, never mid-sentence (found in the 2026-10-13 run).

## Expected cost (30 s film)

About 8 shots instead of 3. Using the cost plan's tiers: 4 standard Kling 5 s clips (~$0.21 each) plus 4 free FFmpeg push-in shots, about $0.85 of video. Shot frames are about 7 × $0.08 (the cold open reuses one), about $0.55. With sheets, narration and music, a film comes to roughly **$1.8–2.5**, under the $5 cap. Using premium Kling for every shot would be about $3.5.

## Order and checks

1. A1–A4 and A6 (prompt, schema, free checks): mocked tests only.
2. A8 writer-model comparison: **paid, ask first**.
3. B1–B3 (word timings, cut on name, shot length): mocked tests, including a fixture alignment where a name falls 0.6 s from a planned cut.
4. B4–B6 (cold-open reuse, push-in, transitions), then B7 and B8.
5. One full paid film of the Arjun–Draupadi story (~$2–3), **ask first**. Compare it with the 2026-10-13 run and the reference formula.

## Decisions for the owner

- **Writer model:** gpt-5 vs Gemini 3.6 Flash vs Claude (needs an API key).
- **CTA:** on by default? Fixed text or generated?
- **Default length:** 30 s or 45–60 s. The formula has more room at 45 s, and the references run about 2:50.
- **Voice speed** 1.05–1.1, and later a different voice per character for dialogue lines.
