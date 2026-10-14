import pytest


@pytest.fixture(autouse=True)
def _no_real_provider_keys(monkeypatch):
    # The app loads .env on import; tests must never bill a real fal.ai or Magnific account.
    monkeypatch.setenv("FAL_KEY", "test-fal-key")
    monkeypatch.setenv("MAGNIFIC_API_KEY", "test-magnific-key")
    # Sign-in is off unless a test turns it on, so a real Clerk key in .env never changes API tests.
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY", raising=False)
