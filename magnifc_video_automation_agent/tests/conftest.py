import pytest


@pytest.fixture(autouse=True)
def _no_real_provider_keys(monkeypatch):
    # The app loads .env on import; tests must never bill a real fal.ai or Magnific account.
    monkeypatch.setenv("FAL_KEY", "test-fal-key")
    monkeypatch.setenv("MAGNIFIC_API_KEY", "test-magnific-key")
    # Sign-in is off unless a test turns it on, so a real Clerk key in .env never changes API tests.
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY", raising=False)
    # Billing is off unless a test turns it on, so a real Stripe key in .env never limits or charges API tests.
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)


@pytest.fixture(autouse=True)
def _private_library(monkeypatch, tmp_path):
    # Characters and My films drafts are written per user; tests must never touch the real library folder.
    from video_automation import character_library

    monkeypatch.setattr(character_library, "LIBRARY_ROOT", tmp_path / "library")
