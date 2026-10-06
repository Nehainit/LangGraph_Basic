"""Supported generation models and indicative public API rates, checked 2026-09-29."""

MODELS = (
    {"stage": "narration", "provider": "ElevenLabs", "id": "eleven_flash_v2_5", "display_name": "Flash v2.5", "cost": {"amount": 0.05, "unit": "USD", "billing_unit": "1,000 characters"}},
    {"stage": "narration", "provider": "ElevenLabs", "id": "eleven_multilingual_v2", "display_name": "Multilingual v2", "cost": {"amount": 0.10, "unit": "USD", "billing_unit": "1,000 characters"}},
    {"stage": "image", "provider": "Magnific", "id": "nano-banana-pro-flash", "display_name": "Nano Banana Pro Flash", "cost": {"amount": 0.095, "unit": "USD", "billing_unit": "image", "quality_rates": {"standard": 0.095, "high": 0.143}}},
    {"stage": "image", "provider": "fal.ai", "id": "fal-ai/nano-banana-pro", "display_name": "Nano Banana Pro", "cost": {"amount": 0.15, "unit": "USD", "billing_unit": "image", "quality_rates": {"standard": 0.15, "high": 0.15}}},
    {"stage": "video", "provider": "Magnific", "id": "kling-v2-6-pro", "display_name": "Kling 2.6", "cost": {"amount": 45, "unit": "Magnific credits", "billing_unit": "second at 1080p"}},
    {"stage": "video", "provider": "fal.ai", "id": "fal-ai/kling-video/v2.6/pro/image-to-video", "display_name": "Kling 2.6 Pro", "cost": {"amount": 0.07, "unit": "USD", "billing_unit": "second, audio off"}},
)

DEFAULT_MODELS = {
    "narration": "eleven_flash_v2_5",
    "image": "nano-banana-pro-flash",
    "video": "kling-v2-6-pro",
}

LEGACY_PROVIDER_MODELS = {
    "magnific": {"image": "nano-banana-pro-flash", "video": "kling-v2-6-pro"},
    "fal": {"image": "fal-ai/nano-banana-pro", "video": "fal-ai/kling-video/v2.6/pro/image-to-video"},
}


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
