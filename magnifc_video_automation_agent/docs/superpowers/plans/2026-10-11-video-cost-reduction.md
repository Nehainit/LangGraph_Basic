# Film Cost Reduction Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking. Run only the mocked test suite while implementing; any live fal/ElevenLabs run needs the owner's go-ahead first.

**Goal:** Cut the cost of a film from about $4.10 to roughly $1.10–1.50 without visibly hurting quality. Video is 60% of today's spend and images are 40%, so the plan covers both. Video has four levers: fewer, longer shots; FFmpeg motion for calm shots on purpose; cheaper 720p video models; and a per-shot router that picks the cheapest tier that fits. Images have three: no mood board, reused library characters, and a cheaper model for plates and shots (Task 5). Fewer shots also means fewer shot images, so Task 1 saves on both.

## Implementation status (2026-10-11)

Everything below is implemented except the paid A/B runs and the mocked tests. The owner asked for implementation only, with no testing. Differences from the plan:

- **Task 2/4 tiers come from code rules, not an LLM field.** `route_video_tiers` in `motion_planner_agent.py` reads each shot's motion intensities, framing, cast and style. It costs nothing and doesn't trip the strict motion-plan schema. A reviewer's choice (`tier_source: "user"`) is never overridden.
- **Task 3 standard tier = `fal-ai/kling-video/v2.5-turbo/standard/image-to-video`** ($0.042/s, 5/10 s, checked on fal.ai today). Kling 2.1 Standard and Seedance Lite are deprecated, and Hailuo-02 Standard ($0.045/s, 6 s) is slower. It is the default **without the A/B**. Revert it with `video_tiers.standard_model` in `config/pipeline.yml`.
- **Task 5c shot frames and plates = `fal-ai/nano-banana-2`** ($0.08 at 1K, checked today), also **without the A/B**. Revert with `images.role_models`.
- **Task 5b was already in place**: `_character_sheet_jobs` copies library sheets instead of paying for them.
- **Task 0 rates**: the ElevenLabs API rates are checked ($0.12 per sound effect, $0.15 per music minute). The Magnific credit rate ($0.000725) comes from the Premium subscription plan and still needs confirming against an API invoice.
- **Not yet updated:** tests that may assert the old behaviour, in `test_backend`, `test_image_agent`, `test_shot_image_qa_agent`, `test_story_hitl_agent` and `test_visual_fidelity_judge` (mood board, fixed video model, duration rounding).

**Where the money goes today (run `eb7256e4`, fal):** 11 images × $0.15 = $1.65 (2 character sheets, 1 location plate, 1 mood board, 7 shot frames; the character board is a free PIL composite). 7 videos × 5 s × $0.07 = $2.45. Narration costs about $0.02. LLM calls mostly go to free providers. ElevenLabs ambience and music aren't priced yet (see Task 0).

**Evidence (rabbit-and-turtle run `eb7256e4`):** 17.6 s of narration produced **7 shots of 1.4–3.2 s**. Each one was billed as a full 5 s Kling clip: 35 billed seconds for 17.6 used, so about half the video spend was trimmed away in the edit. The cause is in `_derived_visual_plan` (`preproduction_agents.py:916`): `moments = min(…, max(len(actions), round(seconds / target)))` never goes below the number of `visible_actions` the scene planner wrote. The planner only gets `visible_action_pacing` as a *suggestion*, and nothing checks it.

**Architecture:** Turn `target_shot_seconds` from a suggestion into a **shot budget** that the code enforces. Add a `video_tier` to each motion plan (`static` | `standard` | `premium`). Make the fal video call model-driven (catalog entry → argument builder, allowed durations, billing unit, price) instead of the hard-coded `FAL_IMAGE_TO_VIDEO_MODEL`. Charge the ledger per shot using that shot's model.

**Tech stack:** Python 3.10+, LangGraph, `fal-client`, FFmpeg (existing `_clip`/`_motion_filter`), pytest (mocked).

## Expected savings (17–20 s film, today ≈ $4.11)

| Phase | Change | Video | Images | Film total |
|---|---|---|---|---|
| — | Today | $2.45 (7 × 5 s) | $1.65 | ~$4.11 |
| 1 | Shot budget: ~4 shots of ~4.5–5 s (and 4 shot frames, not 7) | ~$1.40 | ~$1.20 | **~$2.60** |
| 5a | No mood board | ~$1.40 | ~$1.05 | **~$2.45** |
| 2 | One calm shot as FFmpeg motion | ~$1.05 | ~$1.05 | **~$2.10** |
| 5b | Library characters reused (repeat cast) | ~$1.05 | ~$0.75 | **~$1.80** |
| 3 | Cheaper 720p model as the standard tier | ~$0.50–0.80* | ~$0.75 | **~$1.25–1.55** |
| 5c | Cheaper image model for plate and shot frames | ~$0.50–0.80* | ~$0.45–0.60* | **~$1.00–1.40** |
| 4 | Router: premium only for hard shots | mix | mix | **~$1.10–1.50** |

\*Depends on the models chosen in Tasks 3 and 5c. Prices must be checked on each fal model page on the day of implementation, because third-party price lists disagree. 5b only helps films that reuse a saved cast. A new cast still pays $0.15 per sheet.

A real 30 s film (see the side finding below) needs about 6 shots, so cost scales up about 1.7×. Set pricing from the 30 s case.

## Global constraints

- **No silent quality drops.** Every shot that isn't AI-animated is labelled in the storyboard review and the final artifacts (`provider: "ffmpeg"`, `reason: "planned_static"`), and the reviewer can upgrade it.
- **Shot budget comes from the selected video model's minimum billed clip** (Kling 5 s, Hailuo 6 s, per-second models use `target_shot_seconds`), not a separate magic number.
- **Never below one shot per scene**, and never merge across scenes.
- **The first shot is never static.** It's the hook for Shorts and Reels.
- **At most 1 static shot per 4 shots**, and never two static shots in a row.
- **Keep the existing paths working:** Magnific fallback, the FFmpeg fallback on failure, job-file resume, the per-film budget cap, and the `redo` limits.
- **The ledger charges billed seconds** (the provider's clip length), not the requested seconds.
- **Any live run needs the owner's approval** and an estimate of its cost.

## Review focus

- Scene plan over budget → rejected and the scene LLM retried with a count message. Pin with a scene-planner validation test.
- 17.6 s narration with a 5 s target → 3–4 visual beats, not 7. Pin with a `_derived_visual_plan` test using the rabbit scenes.
- Scene with 3 actions but a budget of 1 → 1 beat using the scene's key action. Pin it.
- Static tier requested for the first shot or two shots in a row → downgraded to `standard`. Pin with a guard test.
- A `static` shot never calls fal: `_clip` runs and the ledger records $0. Pin with a mocked stage test.
- New video model with 6/10 s clips → a 5.2 s shot is billed as 6 s and the ledger shows 6 × rate. Pin with a price test.
- Per-second model → billed seconds = ceil(requested) and is never below the model's minimum. Pin it.
- Unknown `video_tier` or a model missing from the catalog → clear error before any paid call.

---

### Task 0: Baseline metrics (free)

**Files:** `video_automation/cost_control.py`, `video_automation/backend.py`, `tests/test_cost_control.py`

- [ ] Record for each shot: `requested_seconds`, `billed_seconds`, `used_seconds` (from the edit timeline), `video_tier` and `model`.
- [ ] Add film-level `video_waste_seconds = Σ(billed − used)` and `video_usd` to the `_artifacts()` payload. Keep it server-side once the SaaS work lands.
- [ ] Mocked test: the rabbit-shaped ledger (7 × 5 s billed, 17.6 s used) reports 17.4 s of waste.
- [ ] **Close the budget blind spots.** `config/pipeline.yml` sets `magnific_usd_per_credit`, `elevenlabs_sound_usd_per_call` and `elevenlabs_music_usd_per_minute` to `null`, so those calls count as $0. The default video model (`kling-v2-6-pro` on Magnific, billed in credits) therefore never touches the $5 cap. Fill in real rates from the Magnific and ElevenLabs billing pages, and log a warning at startup when a selected paid model has no USD rate.
- [ ] Add a film-level breakdown to the ledger (`video_usd`, `image_usd`, `audio_usd`, `llm_usd`) so each later task's saving shows up per category.

**Done when:** every film reports how many billed seconds were thrown away, and the cap counts every paid provider. Waste seconds are the number later phases must reduce.

### Task 1: Enforce a shot budget (fewer, longer shots)

**Files:** `video_automation/agents/preproduction_agents.py` (`_visible_action_pacing`, scene-plan validation, `_derived_visual_plan`), `config/scene_planning.yml`, `config/pipeline.yml` (`shot_grammar`), `tests/test_preproduction_agents.py`

- [ ] `shot_budget(state) = max(scene_count, round(narration_seconds / min_billed_seconds(video_model)))`, where `min_billed_seconds` is the smallest `allowed_duration` of the selected video model.
- [ ] Scene-plan validation: if Σ`visible_actions` > budget + 1, reject with "Plan at most N visible actions across the film (you planned M); keep each scene's single most important action." The existing retry loop re-asks the LLM, which costs a fraction of a cent.
- [ ] Rewrite the scene planner prompt so `visible_action_pacing` is a **maximum**, not a suggestion.
- [ ] `_derived_visual_plan`: remove the `max(len(actions), …)` floor. Split the film budget across scenes by their seconds, with at least 1 each. When a scene has more actions than its share, keep the first `share` actions. The validator above should make this rare.
- [ ] Shot-planning guard: merge any shot under `0.7 × min_billed_seconds` into its neighbour in the same scene.
- [ ] Replace `target_shot_seconds: 5` with `min_shot_seconds_ratio: 0.7`, and derive the target from the model.
- [ ] Free check: run the planning stages only (no images or video) on 3 fixed prompts (the rabbit race, a two-character Hindi story, a single-character fable) and compare shot counts before and after.

**Done when:** the rabbit prompt plans 3–4 shots, each ≥ 3.5 s, and the mocked suite passes.

**Risk:** Shorts usually cut every 2–3 s, so longer shots can feel slow. Mitigation is Task 1b.

### Task 1b (optional): Punch-in cuts inside one clip (free)

**Files:** `video_automation/agents/editor_agent.py`, `config/video_compilation.yml`, `tests/test_editor_agent.py`

- [ ] For a shot ≥ 4 s, the editor may cut halfway to a 1.25× centred crop of the **same clip**. It looks like a second camera angle at no cost.
- [ ] Only allowed when the motion plan's framing is wide or medium. Skip close-ups, which would crop faces.
- [ ] Config flag `punch_in_cuts: true`, plus a mocked FFmpeg filter test.

### Task 2: Planned FFmpeg motion for calm shots

**Files:** `config/motion_planning.yml`, `video_automation/agents/motion_planner_agent.py`, `video_automation/agents/shot_video_generation_agent.py` (`_generate`, ~line 197), `frontend/studio.js` (badge and upgrade toggle), `video_automation/backend.py` (accept the upgrade in storyboard review feedback), tests

- [ ] The motion plan output gains `video_tier: "static" | "standard" | "premium"` and `tier_reason`. Prompt rule: `static` only when no character performs an action (establishing or location shots, an object close-up, a still reaction) and camera-only motion is enough.
- [ ] Deterministic guard after the LLM: enforce first shot ≠ static, ≤ 1 static per 4 shots, and no two static shots in a row. Otherwise downgrade to `standard`.
- [ ] `_generate`: for `static`, call the existing `_clip(image, requested, _fallback_motion(plan), …)`, record `provider="ffmpeg"`, `reason="planned_static"` and charge $0. This reuses the fallback branch at `shot_video_generation_agent.py:239`.
- [ ] Storyboard review UI: a "Still motion" badge on those frames with an "Animate this shot" toggle. Review feedback can set `video_tier` back to `standard`.
- [ ] Mocked tests for the guard, a static shot that never calls fal, an upgrade from review that does call fal, and $0 in the ledger.

**Done when:** the rabbit storyboard marks 0–1 shots static, never the first, and the user can override it.

### Task 3: Model-driven video generation and a cheaper 720p tier

**Files:** `video_automation/model_catalog.py`, `video_automation/agents/image_agent.py` (fal video call ~lines 657–729), `video_automation/cost_control.py` (`video_price`), `config/shot_video_generation.yml`, tests

- [ ] Catalog entry per video model: `id`, `allowed_durations` (or `per_second` + `min_seconds`), `billing`, `usd_per_second`, `resolution`, `tier` (`standard` | `premium`) and an argument-builder key.
- [ ] Replace the hard-coded `FAL_IMAGE_TO_VIDEO_MODEL` with the model from the shot's tier. Each argument builder maps `duration`, `prompt`, `negative_prompt`, `cfg_scale`, `image_url` and the aspect ratio to that endpoint's fields.
- [ ] `video_price(state, seconds, model)` uses billed seconds from that model's rules.
- [ ] 720p models need no extra work at the end: `editor_agent._motion_filter` already scales and crops every clip to the output resolution.
- [ ] Shortlist 2–3 candidates (for example Kling standard/turbo 720p, Hailuo, PixVerse 720p), with prices **checked on the fal model pages** on the day of implementation.
- [ ] **Paid A/B test (needs approval):** reuse the approved storyboard frames from Task 1's 3 prompts, so there's no image cost. Animate them with each candidate and compare video-validator pass rate, judge score and a side-by-side watch. Estimated cost ≈ 4 shots × 3 prompts × 3 models × ~$0.25 ≈ **$9**.
- [ ] Pick the `standard` tier from the results. Kling 2.6 Pro stays `premium`.

**Done when:** the default tier is the chosen cheaper model, Kling Pro stays available, and the ledger prices both correctly.

### Task 4: Per-shot router

**Files:** `video_automation/agents/motion_planner_agent.py`, `config/motion_planning.yml`, `video_automation/backend.py` (plan policy hook), tests

- [ ] Rule-based tier upgrade (no extra model): `premium` when the style is realistic (`cinematic`), when 2+ characters interact, when there's a close-up of a face, or when there's fast or complex body action. Otherwise `standard`. `static` comes from Task 2.
- [ ] Plan policy hook: Starter = `static` + `standard` only; Creator = `premium` allowed. This is enforced on the server once logins exist.
- [ ] Log tier, model, validator outcome and judge score per shot. This becomes the data for a learned router later.
- [ ] Mocked tests for the rules table and policy clamping.

**Done when:** each shot's tier is chosen automatically and explained in `tier_reason`, and the film cost estimate before approval reflects the mix.

### Task 5: Image spend

**Files:** `video_automation/agents/image_agent.py` (`create_reference_package` ~line 917, `create_visual_storyboard` ~line 1531, `_fal_image_model` ~line 456), `video_automation/character_library.py`, `video_automation/model_catalog.py`, `config/shot_image_generation.yml`, tests

**5a: Drop the mood board ($0.15 per film, free to build).**
- [ ] Config flag `images.mood_board: false` (the default). When it's off, `create_reference_package` skips the `mood_board_prompt` call, and `create_visual_storyboard` stops requiring `mood_board_file`. Its job (shared palette and lighting) moves into the shot prompt as text from the production bible.
- [ ] Remove the mood board from the reference-review UI when the flag is off.
- [ ] Mocked test: the reference package generates sheets and plates only, and the storyboard runs without `mood_board_file`.
- [ ] One approved live film to confirm the look stays consistent across shots (the roadmap's verification step).

**5b: Reuse library characters (~$0.15 per reused character).**
- [ ] When a film's cast comes from `character_library.py`, copy the saved sheet into `characters/` and record `character_reference_model_id`, so `create_character_sheets` (~line 805) sees an existing file and doesn't pay again. Today a change of `image_model` regenerates every sheet, so a library sheet must stay valid for any shot model (the sheet is only a reference).
- [ ] Prefetch (`prefetch_character_sheets`) skips library characters.
- [ ] Mocked test: a two-character film where one character comes from the library makes exactly one paid sheet call.

**5c: A cheaper image tier for plates and shot frames (needs an approved A/B).**
- [ ] Add a second fal image entry to `model_catalog.py` (for example Nano Banana 2 or another edit model that takes several references), with its price **checked on the fal page**.
- [ ] Split image roles: `character_sheet` stays on Nano Banana Pro, because sheets anchor identity for the whole film. `location_plate` and `shot_frame` use the image tier set in config.
- [ ] Keep `quality_resolutions.standard: 1K`. Only the `high` quality setting pays for 2K.
- [ ] **Paid A/B test (needs approval):** regenerate the 4 shot frames from Task 1's 3 prompts using the approved sheets. Compare the shot-image QA pass rate and redraw count (a redraw costs a second image, so a cheap model that fails QA more often can cost more), then do a side-by-side review. Estimated cost ≈ 12 frames × 2 models ≈ **$2–3**.

**5d: Fewer paid image redraws (free).**
- [ ] Log the QA first-pass rate per film. Once the prompt enhancer from the roadmap lands, check whether `max_paid_redos_per_shot.image` can stay at 1 while redraws drop.

**Done when:** a film with a new cast pays for 2 sheets, 1 plate and 4 frames (about $1.05 on today's model), and a film with a library cast pays for the plate and frames only.

---

## Side finding (separate fix, affects pricing)

The rabbit run asked for **30 s** and produced **17.6 s** of narration. The narration script came out short, so the film was about 40% shorter than requested. Fixing it will raise per-film cost by roughly 1.7×. Track it separately and set "up to 30 s" plan prices from the corrected length.

## Order and effort

| Task | Paid testing | Effort |
|---|---|---|
| 0 Baseline metrics | none | small |
| 1 Shot budget | none (planning-only check) | medium |
| 1b Punch-in cuts | none | small |
| 2 Planned FFmpeg motion | none to build; one approved run to see it | medium |
| 3 Model-driven video + cheaper tier | ~$9 A/B, needs approval | medium–large |
| 4 Router | none | small–medium |
| 5a No mood board | one approved check film | small |
| 5b Library reuse | none | small (after the character library lands) |
| 5c Cheaper image tier | ~$2–3 A/B, needs approval | medium |
| 5d Fewer redraws | none | small |

**Recommended order:** 0 → 1 → 5a → 2 → 5b, then 3 and 5c together (one approved A/B budget of about $12), then 4. The free tasks take a film from about $4.10 to about $1.80–2.10 (roughly half). Tasks 3 and 5c are the only ones that need a spending decision.
