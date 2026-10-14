"""Supported generation models and indicative public API rates, checked 2026-09-29 (fal.ai rates rechecked 2026-10-11)."""

MODELS = (
    # Listed first so the studio defaults to it: the most expressive storytelling voice (~$0.04 per 30 s film).
    {"stage": "narration", "provider": "ElevenLabs", "id": "eleven_multilingual_v2", "display_name": "Multilingual v2 · expressive", "cost": {"amount": 0.10, "unit": "USD", "billing_unit": "1,000 characters"}},
    {"stage": "narration", "provider": "ElevenLabs", "id": "eleven_flash_v2_5", "display_name": "Flash v2.5 · fastest", "cost": {"amount": 0.05, "unit": "USD", "billing_unit": "1,000 characters"}},
    {"stage": "image", "provider": "Magnific", "id": "nano-banana-pro-flash", "display_name": "Nano Banana Pro Flash", "cost": {"amount": 0.095, "unit": "USD", "billing_unit": "image", "quality_rates": {"standard": 0.095, "high": 0.143}}},
    {"stage": "image", "provider": "fal.ai", "id": "fal-ai/nano-banana-pro", "display_name": "Nano Banana Pro", "cost": {"amount": 0.15, "unit": "USD", "billing_unit": "image", "quality_rates": {"standard": 0.15, "high": 0.15}}},
    # 1K is $0.08 and 2K is 1.5x; used for location plates and shot frames (config: images.role_models).
    {"stage": "image", "provider": "fal.ai", "id": "fal-ai/nano-banana-2", "display_name": "Nano Banana 2", "cost": {"amount": 0.08, "unit": "USD", "billing_unit": "image", "quality_rates": {"standard": 0.08, "high": 0.12}}},
    # durations: the clip lengths the endpoint renders; a shot is billed at the shortest one that covers it.
    {"stage": "video", "provider": "Magnific", "id": "kling-v2-6-pro", "display_name": "Kling 2.6", "tier": "premium", "durations": [5, 10], "cost": {"amount": 45, "unit": "Magnific credits", "billing_unit": "second at 1080p"}},
    {"stage": "video", "provider": "fal.ai", "id": "fal-ai/kling-video/v2.6/pro/image-to-video", "display_name": "Kling 2.6 Pro", "tier": "premium", "durations": [5, 10], "arguments": "kling_pro", "cost": {"amount": 0.07, "unit": "USD", "billing_unit": "second, audio off"}},
    {"stage": "video", "provider": "fal.ai", "id": "fal-ai/kling-video/v2.5-turbo/standard/image-to-video", "display_name": "Kling 2.5 Turbo Standard (720p)", "tier": "standard", "durations": [5, 10], "arguments": "kling_standard", "cost": {"amount": 0.042, "unit": "USD", "billing_unit": "second at 720p"}},
)

DEFAULT_MODELS = {
    "narration": "eleven_multilingual_v2",
    "image": "nano-banana-pro-flash",
    "video": "kling-v2-6-pro",
}

LEGACY_PROVIDER_MODELS = {
    "magnific": {"image": "nano-banana-pro-flash", "video": "kling-v2-6-pro"},
    "fal": {"image": "fal-ai/nano-banana-pro", "video": "fal-ai/kling-video/v2.6/pro/image-to-video"},
}

VIDEO_TIERS = ("static", "standard", "premium")


def model_entry(stage: str, model_id: str) -> dict:
    for entry in MODELS:
        if entry["stage"] == stage and entry["id"] == model_id:
            return entry
    raise ValueError(f"Unknown {stage} model: {model_id}")


def selected_model(state: dict, stage: str) -> dict:
    model_id = state.get(f"{stage}_model")
    if not model_id:
        legacy = not (state.get("image_model") or state.get("video_model"))
        model_id = (LEGACY_PROVIDER_MODELS.get(state.get("image_provider"), {}).get(stage) if legacy else None) or DEFAULT_MODELS[stage]
    return model_entry(stage, model_id)


def billed_seconds(entry: dict, requested: float) -> int:
    """The clip length the provider renders and bills for a requested shot length."""
    durations = sorted(int(value) for value in entry.get("durations") or [5, 10])
    if requested > durations[-1] + 1e-9:
        raise ValueError(f"{entry['id']} renders at most {durations[-1]}s; {requested:g}s was requested.")
    return next(value for value in durations if value + 1e-9 >= requested)


def min_billed_seconds(state: dict) -> int:
    """The shortest clip the film's video model bills; shots shorter than this waste paid seconds."""
    return min(int(value) for value in selected_model(state, "video").get("durations") or [5])
