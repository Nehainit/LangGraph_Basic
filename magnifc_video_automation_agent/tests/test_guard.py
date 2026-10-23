import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from video_automation import auth, guard
from video_automation.cost_control import BudgetExceeded, CostLedger

USER = "user_owner"
STRANGER = "user_stranger"


# ---------- Sign-in ----------
def test_a_server_that_requires_sign_in_locks_when_the_key_is_missing(monkeypatch):
    request = SimpleNamespace(headers={}, cookies={}, state=SimpleNamespace())
    assert auth.require_user(request) is None  # local development stays open

    monkeypatch.setenv("REQUIRE_SIGN_IN", "true")
    with pytest.raises(HTTPException) as error:
        auth.require_user(request)
    assert error.value.status_code == 503
    assert auth.public_auth_config()["enabled"] is False


def test_without_an_invite_list_every_signed_in_account_gets_in():
    auth.check_invited(STRANGER, {})


def test_invite_list_accepts_listed_ids_and_emails(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", f"{USER}, Owner@Example.com")
    auth.check_invited(USER, {})
    auth.check_invited("user_other", {"email": "owner@example.com"})
    with pytest.raises(HTTPException) as error:
        auth.check_invited(STRANGER, {"email": "someone@else.com"})
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "not_invited"


def test_invite_list_can_look_up_the_email_with_clerk(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "owner@example.com")
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_dummy")
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"email_addresses": [{"email_address": "Owner@Example.com"}]}).encode()

    def fake_urlopen(call, timeout):
        calls.append((call.full_url, call.headers["Authorization"]))
        return Response()

    monkeypatch.setattr(auth.urlrequest, "urlopen", fake_urlopen)
    monkeypatch.setattr(auth, "_emails", {})
    auth.check_invited("user_lookup", {})
    auth.check_invited("user_lookup", {})  # remembered, not asked twice
    assert calls == [("https://api.clerk.com/v1/users/user_lookup", "Bearer sk_test_dummy")]


# ---------- Daily spending cap ----------
def test_daily_cap_stops_paid_calls_and_refunds_failed_ones(monkeypatch):
    monkeypatch.setenv("DAILY_SPEND_LIMIT_USD", "1.00")
    guard.charge_today("video:a", 0.6)
    guard.charge_today("video:a", 0.6)  # the same call is counted once
    with pytest.raises(guard.DailyCapReached):
        guard.charge_today("video:b", 0.5)
    guard.refund_today("video:a")
    guard.charge_today("video:b", 0.5)
    assert guard.spent_today() == 0.5


def test_tomorrow_starts_a_fresh_day(monkeypatch):
    monkeypatch.setenv("DAILY_SPEND_LIMIT_USD", "1.00")
    guard.charge_today("video:a", 1.0)
    monkeypatch.setattr(guard, "_today", lambda: "2099-01-01")
    guard.charge_today("video:b", 1.0)


def test_film_ledgers_count_toward_the_daily_cap(monkeypatch):
    monkeypatch.setenv("DAILY_SPEND_LIMIT_USD", "0.50")
    first, second = CostLedger(), CostLedger()
    first.charge("image:film-1/shot-1", "image", "shot-1", 0.4)
    with pytest.raises(BudgetExceeded):
        second.charge("image:film-2/shot-1", "image", "shot-1", 0.4)
    assert second.snapshot() == []
    first.refund("image:film-1/shot-1")
    second.charge("image:film-2/shot-1", "image", "shot-1", 0.4)


def test_no_cap_when_it_is_zero(monkeypatch):
    monkeypatch.setenv("DAILY_SPEND_LIMIT_USD", "0")
    guard.charge_today("video:big", 500.0)


def test_character_drawings_count_toward_the_cap(monkeypatch, tmp_path):
    from video_automation import character_library

    monkeypatch.setenv("DAILY_SPEND_LIMIT_USD", "100")
    monkeypatch.setattr(character_library, "_draw", lambda *_args: (_ for _ in ()).throw(RuntimeError("provider down")))
    with pytest.raises(HTTPException) as error:
        character_library._draw_or_fail("prompt", tmp_path / "sheet.png", [], [], "")
    assert error.value.status_code == 502
    assert guard.spent_today() == 0  # the failed drawing was refunded

    monkeypatch.setenv("DAILY_SPEND_LIMIT_USD", "0.0001")
    with pytest.raises(HTTPException) as error:
        character_library._draw_or_fail("prompt", tmp_path / "sheet.png", [], [], "")
    assert error.value.status_code == 429


# ---------- Per-account limits ----------
def test_each_account_has_a_daily_film_limit(monkeypatch):
    monkeypatch.setenv("MAX_FILMS_PER_DAY", "2")
    guard.use_quota(USER, "films")
    guard.use_quota(USER, "films")
    with pytest.raises(HTTPException) as error:
        guard.use_quota(USER, "films")
    assert error.value.status_code == 429
    guard.use_quota(STRANGER, "films")  # another account has its own count
    for _ in range(5):
        guard.use_quota(None, "films")  # sign-in off: local development is unlimited
    monkeypatch.setattr(guard, "_today", lambda: "2099-01-01")
    guard.use_quota(USER, "films")


# ---------- Film ownership ----------
def test_only_the_owner_can_reach_a_film():
    assert guard.owns(USER, "film-1", {"owner_id": USER})
    assert not guard.owns(STRANGER, "film-1", {"owner_id": USER})
    assert guard.owns(None, "film-1", {"owner_id": USER})
    with pytest.raises(HTTPException) as error:
        guard.require_owner(STRANGER, "film-1", {"owner_id": USER})
    assert error.value.status_code == 404


def test_films_made_before_owners_were_recorded_belong_to_their_lists():
    from video_automation.edits import remember_films

    remember_films(USER, ["old-film"])
    assert guard.owns(USER, "old-film", {})
    assert not guard.owns(STRANGER, "old-film", {})


def test_a_film_records_its_owner_and_counts_toward_the_daily_limit(monkeypatch):
    from video_automation import backend

    class Graph:
        initial = None

        def invoke(self, value, _config):
            if isinstance(value, dict):
                Graph.initial = value
            return {"__interrupt__": [SimpleNamespace(value={"stage": "visual_storyboard_review", "storyboard": [], "image_files": [], "actions": ["approve"]})]}

    monkeypatch.setattr(backend, "graph", Graph())
    monkeypatch.setattr(backend, "validate_model_configuration", lambda _agent: "vision-model")
    monkeypatch.setattr(backend, "_archive", lambda *_args: None)
    monkeypatch.setenv("MAX_FILMS_PER_DAY", "1")
    backend.create_video(backend.VideoRequest(topic="A story"), user_id=USER)

    assert Graph.initial["owner_id"] == USER
    with pytest.raises(HTTPException) as error:
        backend.create_video(backend.VideoRequest(topic="Another story"), user_id=USER)
    assert error.value.status_code == 429


def test_film_files_are_served_only_to_their_owner(monkeypatch, tmp_path):
    from video_automation import backend

    monkeypatch.setattr(backend, "ROOT", tmp_path)
    clip = tmp_path / "outputs" / "film-9" / "video" / "final.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"film")
    monkeypatch.setattr(guard, "_owners", {})
    monkeypatch.setattr(backend, "graph", SimpleNamespace(get_state=lambda _config: SimpleNamespace(values={"owner_id": USER})))

    assert backend.output_file("outputs/film-9/video/final.mp4", user_id=USER).path == clip
    with pytest.raises(HTTPException) as error:
        backend.output_file("outputs/film-9/video/final.mp4", user_id=STRANGER)
    assert error.value.status_code == 404


def test_another_accounts_film_cannot_be_reviewed_or_edited(monkeypatch):
    from video_automation import backend

    snapshot = SimpleNamespace(next=("review_story",), values={"owner_id": USER, "video_file": "x.mp4"}, tasks=())
    monkeypatch.setattr(backend, "graph", SimpleNamespace(get_state=lambda _config: snapshot))
    with pytest.raises(HTTPException) as error:
        backend.submit_story_review(backend.StoryReviewRequest(thread_id="film-1", action="approve"), user_id=STRANGER)
    assert error.value.status_code == 404

    snapshot.next = ()
    with pytest.raises(HTTPException) as error:
        backend.film_edits("film-1", user_id=STRANGER)
    assert error.value.status_code == 404
