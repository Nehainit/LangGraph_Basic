import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from video_automation import billing

USER = "user_123"
FINISHED = {"status": "complete", "video_url": "/output/outputs/film.mp4"}


@pytest.fixture
def stripe_on(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")


def _status():
    return billing.billing_status(USER)


def test_billing_is_off_without_stripe_or_sign_in(stripe_on, monkeypatch):
    assert billing.billing_enabled(None) is False
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    assert billing.billing_enabled(USER) is False
    for _ in range(3):
        billing.require_film_allowance(USER, 120)
        billing.hold_film(USER, "thread", 120)


@pytest.mark.parametrize(("duration", "seconds"), [(30, 30), ("45 seconds", 45), ("unknown", 120), (None, 120)])
def test_film_length_is_read_from_request_or_state(duration, seconds):
    assert billing.film_seconds(duration) == seconds


@pytest.mark.parametrize(("seconds", "credits"), [(5, 1), (10, 1), (11, 2), (30, 3), (120, 12)])
def test_credits_round_up_to_started_ten_seconds(seconds, credits):
    assert billing.credits_for(seconds) == credits


def test_first_film_up_to_thirty_seconds_is_free_then_credits_are_required(stripe_on):
    assert _status()["free_film_left"] is True

    billing.require_film_allowance(USER, 30)
    billing.hold_film(USER, "film-1", 30)
    billing.settle_film(USER, "film-1", FINISHED)

    status = _status()
    assert (status["free_film_left"], status["credits"], status["spare_credits"]) == (False, 0, 0)
    with pytest.raises(HTTPException) as error:
        billing.require_film_allowance(USER, 10)
    assert error.value.status_code == 402
    assert error.value.detail["credits_needed"] == 1


def test_free_film_does_not_cover_a_longer_film(stripe_on):
    with pytest.raises(HTTPException) as error:
        billing.require_film_allowance(USER, 60)
    assert "free film can be up to 30 seconds" in error.value.detail["error"]
    assert error.value.detail["credits_needed"] == 6

    billing.add_credits(USER, 6, "cs_1")
    billing.hold_film(USER, "film-1", 60)
    billing.settle_film(USER, "film-1", FINISHED)

    status = _status()
    assert (status["free_film_left"], status["credits"]) == (True, 0)


def test_paid_film_spends_credits_for_its_length(stripe_on):
    billing.add_credits(USER, 10, "cs_1")
    billing.hold_film(USER, "free", 20)
    billing.settle_film(USER, "free", FINISHED)
    billing.hold_film(USER, "film-2", 45)
    billing.settle_film(USER, "film-2", FINISHED)

    assert _status()["credits"] == 5


def test_held_credits_cannot_fund_another_film(stripe_on):
    billing.add_credits(USER, 6, "cs_1")
    billing.hold_film(USER, "film-1", 60)
    billing.hold_film(USER, "film-1", 60)  # Retrying the same film keeps its hold.
    billing.hold_film(USER, "film-2", 30)  # The free film is still separate.

    assert _status()["spare_credits"] == 0
    with pytest.raises(HTTPException) as error:
        billing.hold_film(USER, "film-3", 10)
    assert error.value.status_code == 402


def test_film_without_video_releases_its_hold(stripe_on):
    billing.add_credits(USER, 4, "cs_1")
    billing.hold_film(USER, "film-1", 40)
    billing.settle_film(USER, "film-1", {"status": "pipeline_incomplete"})

    assert (_status()["credits"], _status()["spare_credits"]) == (4, 4)


def test_film_paused_at_a_review_keeps_its_hold(stripe_on):
    billing.hold_film(USER, "film-1", 30)
    billing.settle_film(USER, "film-1", {"status": "shot_plan_review"})

    assert _status()["free_film_left"] is False
    billing.settle_film(USER, "film-1", FINISHED)
    assert _load_account()["free_film_used"] is True


def test_film_is_charged_once(stripe_on):
    billing.add_credits(USER, 9, "cs_1")
    for _ in range(2):
        billing.hold_film(USER, "film-1", 40)
        billing.settle_film(USER, "film-1", FINISHED)

    assert _status()["credits"] == 5


def test_stale_hold_stops_blocking_new_films(stripe_on, monkeypatch):
    billing.hold_film(USER, "film-1", 30)
    later = time.time() + billing.HOLD_SECONDS + 1
    monkeypatch.setattr(billing.time, "time", lambda: later)

    billing.require_film_allowance(USER, 30)


def _load_account():
    return billing._load(USER)


def _webhook_client(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    app = FastAPI()
    app.include_router(billing.router)
    return TestClient(app)


def _signed(event: dict, secret: str = "whsec_test") -> tuple[bytes, dict]:
    payload = json.dumps(event).encode()
    timestamp = int(time.time())
    signature = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, {"stripe-signature": f"t={timestamp},v1={signature}", "content-type": "application/json"}


def _checkout_event(payment_status="paid", session_id="cs_test_1"):
    return {
        "id": "evt_1", "object": "event", "type": "checkout.session.completed",
        "data": {"object": {
            "id": session_id, "object": "checkout.session", "payment_status": payment_status,
            "metadata": {"user_id": USER, "pack_id": "starter", "credits": "3"},
        }},
    }


def test_paid_checkout_webhook_adds_credits_once(stripe_on, monkeypatch):
    client = _webhook_client(monkeypatch)
    payload, headers = _signed(_checkout_event())

    for _ in range(2):  # Stripe retries deliveries.
        assert client.post("/api/stripe/webhook", content=payload, headers=headers).status_code == 200

    assert _status()["credits"] == 3


def test_unpaid_checkout_adds_nothing(stripe_on, monkeypatch):
    client = _webhook_client(monkeypatch)
    payload, headers = _signed(_checkout_event(payment_status="unpaid"))

    assert client.post("/api/stripe/webhook", content=payload, headers=headers).status_code == 200
    assert _status()["credits"] == 0


def test_webhook_rejects_a_forged_signature(stripe_on, monkeypatch):
    client = _webhook_client(monkeypatch)
    payload, headers = _signed(_checkout_event(), secret="whsec_wrong")

    assert client.post("/api/stripe/webhook", content=payload, headers=headers).status_code == 400
    assert _status()["credits"] == 0


def test_checkout_rejects_unknown_pack(stripe_on):
    with pytest.raises(HTTPException) as error:
        billing._pack("missing")
    assert error.value.status_code == 404


def test_longest_film_on_any_pack_costs_more_than_the_video_budget():
    from video_automation.prompts import PIPELINE_CONFIG

    budget_cents = PIPELINE_CONFIG["budget"]["max_usd_per_video"] * 100
    for pack in billing.PRICING["packs"]:
        price_per_credit = pack["amount_cents"] / pack["credits"]
        assert price_per_credit * billing.credits_for(billing.MAX_FILM_SECONDS) > budget_cents, pack["id"]


def test_storyboard_approval_spends_the_free_film_then_asks_for_credits(stripe_on, monkeypatch):
    from types import SimpleNamespace

    from video_automation import backend

    class StoryboardGraph:
        def get_state(self, _config):
            return SimpleNamespace(next=("review_visual_storyboard",), values={"duration": "30 seconds"}, tasks=())

        def invoke(self, _value, _config):
            return {"story": "Story.", "storyboard": [], "warnings": [], "video_file": str(backend.ROOT / "outputs/film.mp4")}

    monkeypatch.setattr(backend, "graph", StoryboardGraph())
    monkeypatch.setattr(backend, "_archive", lambda *_args: None)
    approve = backend.VisualStoryboardReviewRequest(thread_id="film-1", action="approve")

    assert backend.submit_visual_storyboard_review(approve, user_id=USER)["video_url"]
    assert _status()["free_film_left"] is False

    with pytest.raises(HTTPException) as error:
        backend.submit_visual_storyboard_review(approve.model_copy(update={"thread_id": "film-2"}), user_id=USER)
    assert error.value.status_code == 402
