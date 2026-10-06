# Per-Run Shot Image Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each video run select Magnific or fal.ai for shot still images, with Magnific remaining the default.

**Architecture:** Carry a validated `image_provider` value from the browser or interactive CLI into LangGraph state; omitted values default to Magnific. fal.ai uses `fal-ai/nano-banana-pro` for shots with no reference images and `fal-ai/nano-banana-pro/edit` when references exist. Validate fal credentials before shot workers start, save results to the existing local per-shot path, and record provider/model provenance in all result paths. Keep reference-board generation and Magnific/Kling video generation unchanged.

**Tech Stack:** Python 3.10+, FastAPI/Pydantic, LangGraph, `fal-client`, existing vanilla HTML/JavaScript UI and pytest tests.

**Spec:** In-chat design from 2026-09-27: per-run shot-image provider selection; Magnific default; fal.ai selected only on request; no silent cross-provider fallback; leave video generation unchanged.

## Global Constraints

- Default provider remains `magnific` for API callers and UI runs that omit the new field.
- Accept only `magnific` and `fal` as provider values.
- Require `FAL_KEY` only when fal.ai is selected; never send it to the browser.
- Preserve existing output paths, retries, shot ordering, and reference count behavior.
- Do not silently retry a fal.ai failure through Magnific.
- Select fal.ai's text-to-image endpoint when a shot has no references; use its edit endpoint only when at least one reference image is supplied.
- Keep provider/model provenance accurate for new, failed, cached, and reused shot records.
- The browser and interactive CLI both expose the run-level shot-image provider.
- This choice controls shot still images only; reference boards and shot video generation keep their current providers.

## Review Focus

- Missing provider field → defaults to Magnific; pin with backend request/model default test.
- Invalid provider value → rejected at request validation; pin with `VideoRequest` test.
- fal selected without `FAL_KEY` → clear provider-specific error before worker retries; pin with stage test.
- fal shot without references → calls the text-to-image endpoint without `image_urls`; pin with adapter test.
- fal shot with references → uploads files and calls the edit endpoint with those URLs; pin with adapter test.
- fal output has no image URL → generation fails cleanly and is not sent to Magnific; pin with fal adapter test.
- cached, reused, dependency-cycle, and validation-failure records → carry provider and shot-appropriate model metadata; pin with branch tests.
- CLI provider prompt → blank input defaults to Magnific and invalid input is reprompted; pin with CLI input test.

---

### Task 1: Carry the per-run provider selection

**Files:**
- Modify: `video_automation/backend.py:VideoRequest` and `create_video`
- Modify: `video_automation/schema.py:AgentState`
- Modify: `frontend/index.html` run form and `startGeneration()` request body
- Modify: `video_automation/main.py:get_user_input`
- Test: `tests/test_backend.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Produces: `AgentState.image_provider: NotRequired[Literal["magnific", "fal"]]`; initial graph state always receives the validated request value.

- [ ] Add failing request tests asserting omitted `image_provider` defaults to `magnific`, unsupported values fail validation, and `create_video` passes the selected provider into the graph's initial state.
- [ ] Run the focused backend test and confirm it fails for the missing field/default.
- [ ] Add `image_provider: Literal["magnific", "fal"] = "magnific"` to `VideoRequest` and include it in the initial state passed to `graph.invoke`.
- [ ] Add a native select control labeled “Shot image provider” with Magnific selected by default and fal.ai as the alternate; include its value in the existing `/api/create-video` JSON body.
- [ ] Add a CLI prompt accepting `magnific` or `fal`, default blank input to `magnific`, reprompt invalid values, and include the choice in the `AgentState` returned by `get_user_input()`.
- [ ] Run focused backend and CLI input tests and inspect the frontend request construction for both values.

### Task 2: Add the fal.ai image adapter and provider dispatch

**Files:**
- Modify: `video_automation/agents/image_agent.py`
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `.env.example`
- Test: `tests/test_image_agent.py`

**Interfaces:**
- Consumes: `AgentState.image_provider` from Task 1.
- Produces: `_generate_fal_reference_image(prompt: str, size: str, image_file: Path, reference_files: list[str], resolution: str) -> str`; select fal's text endpoint when `reference_files` is empty and edit endpoint otherwise. `_generate_shot_image_job` invokes it only for fal; existing reference-board call sites retain Magnific.

- [ ] Add failing unit tests using a stubbed `fal_client` for the no-reference text endpoint, referenced edit endpoint, reference upload and image download, malformed output, and no Magnific fallback. Add stage tests proving a missing `FAL_KEY` errors once before worker retries, and tests for provider/model metadata on success, failure, cached, dependency-cycle, and validation-error records. Include a regression assertion that Magnific remains the default path.
- [ ] Run the focused image-agent tests and confirm these cases fail before implementation.
- [ ] Add `fal-client` to both dependency manifests and an `FAL_KEY=` placeholder/comment to `.env.example` without reading or writing the user's actual `.env` value.
- [ ] Before constructing or starting the worker pool in `_generate_requested_shot_images`, validate `FAL_KEY` once when fal is selected; let this clear configuration error leave the stage rather than be caught and retried per shot.
- [ ] In the fal adapter, upload each local reference through `fal_client.upload_file`; call `fal_client.subscribe("fal-ai/nano-banana-pro", arguments={...})` with no reference field when none exist, or `fal_client.subscribe("fal-ai/nano-banana-pro/edit", arguments={...})` with uploaded `image_urls` when references exist. Pass prompt, aspect ratio, resolution, and PNG output format supported by the selected endpoint; download `result["images"][0]["url"]` using the existing image-download helper.
- [ ] Derive provider and expected model for each shot, pass them through worker jobs, and set metadata on success, failure, dependency-cycle, validation-failure, and cached/reused paths. Preserve prior metadata for previously generated assets when present; use the current selected provider and expected endpoint model for legacy cached assets without provenance.
- [ ] Dispatch to the fal adapter only when `state.get("image_provider", "magnific") == "fal"`; retain the current Magnific flow otherwise. Record the actual endpoint/model selected for that shot.
- [ ] Run the focused image-agent tests and confirm every adapter/dispatch case passes.

### Task 3: Document configuration and user-visible behavior

**Files:**
- Modify: `README.md`
- Test: none (documentation-only)

- [ ] Document `FAL_KEY`, install/update instructions for dependencies, provider selection in both the web UI and interactive CLI, Magnific as the default, and that video generation continues to use its existing configuration.
- [ ] Check that setup instructions do not include a real key and accurately describe the selected-provider failure behavior.

### Task 4: Verify the integrated flow

**Files:**
- No additional files expected.

- [ ] Run `pytest tests/test_backend.py tests/test_image_agent.py tests/test_main.py` and confirm browser/API and CLI defaults, endpoint routing, credential preflight, metadata provenance, and existing Magnific behavior pass.
- [ ] Run the full project test suite and report any failures without changing unrelated behavior.
- [ ] Review the diff to confirm `.env` is untouched and no secret appears in tracked files or logs.
