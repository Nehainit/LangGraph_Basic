"""My films drafts: per-user story drafts, and films paused at a review that any device can reopen. The graph is faked."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from video_automation import backend
from video_automation.auth import require_user
from video_automation.backend import app


STORY = {"topic": "A rabbit races a turtle.", "settings": {"duration": 30, "visual_style": "anime", "character_ids": []}}
SHOT = {"shot_id": "shot-001", "visuals": "The race begins."}
FRAME = str(backend.ROOT / "outputs/test/images/shot-001.png")


class PausingGraph:
    """Pauses at the storyboard review, then finishes the film when it is approved."""

    def __init__(self):
        self.paused = False

    def _review(self):
        return SimpleNamespace(value={"storyboard": [SHOT], "image_files": [FRAME], "actions": ["approve", "regenerate"]})

    def get_state(self, _config):
        return SimpleNamespace(
            next=("review_visual_storyboard",) if self.paused else (),
            values={"storyboard": [SHOT], "image_files": [FRAME]},
            interrupts=(self._review(),) if self.paused else (),
        )

    def invoke(self, value, _config):
        if isinstance(value, dict):
            self.paused = True
            return {"storyboard": [SHOT], "image_files": [FRAME], "__interrupt__": [self._review()]}
        self.paused = False
        return {"storyboard": [SHOT], "video_file": str(backend.ROOT / "outputs/test.mp4")}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(backend, "validate_model_configuration", lambda _agent: "vision-model")
    monkeypatch.setattr(backend, "graph", PausingGraph())
    monkeypatch.setattr(backend, "_archive", lambda *_args: None)
    app.dependency_overrides[require_user] = lambda: "user_a"
    yield TestClient(app)
    app.dependency_overrides.clear()


def drafts(client):
    return client.get("/api/drafts").json()["drafts"]


def test_story_drafts_are_saved_updated_and_deleted(client):
    draft = client.post("/api/drafts", json=STORY).json()
    assert draft["kind"] == "story" and draft["settings"]["visual_style"] == "anime"

    updated = client.put(f"/api/drafts/{draft['id']}", json={**STORY, "topic": "A turtle races a rabbit."}).json()
    assert updated["topic"] == "A turtle races a rabbit."
    assert [item["id"] for item in drafts(client)] == [draft["id"]]

    assert client.delete(f"/api/drafts/{draft['id']}").json() == {"deleted": draft["id"]}
    assert drafts(client) == []


def test_invalid_story_drafts_are_rejected(client):
    assert client.post("/api/drafts", json={**STORY, "topic": "   "}).status_code == 422
    assert client.put("/api/drafts/not-an-id", json=STORY).status_code == 404
    assert client.delete("/api/drafts/0123456789ab").status_code == 404


def test_drafts_are_private_to_each_user(client):
    draft_id = client.post("/api/drafts", json=STORY).json()["id"]

    app.dependency_overrides[require_user] = lambda: "user_b"
    assert drafts(client) == []
    assert client.put(f"/api/drafts/{draft_id}", json=STORY).status_code == 404
    assert client.delete(f"/api/drafts/{draft_id}").status_code == 404
    assert client.get(f"/api/drafts/{draft_id}/review").status_code == 404


def test_paused_film_replaces_its_story_draft_and_reopens_then_leaves_once_approved(client):
    story_id = client.post("/api/drafts", json=STORY).json()["id"]
    film = client.post("/api/create-video", json={"topic": STORY["topic"], "aspect_ratio": "1:1", "draft_id": story_id}).json()
    assert film["status"] == "visual_storyboard_review"

    [paused] = drafts(client)
    assert paused["kind"] == "film" and paused["thread_id"] == film["thread_id"]
    assert paused["status"] == "visual_storyboard_review"
    assert paused["poster"] == "/output/outputs/test/images/shot-001.png"
    assert paused["request"]["aspect_ratio"] == "1:1" and "draft_id" not in paused["request"]

    reopened = client.get(f"/api/drafts/{paused['id']}/review").json()
    assert reopened["status"] == "visual_storyboard_review"
    assert reopened["thread_id"] == film["thread_id"]
    assert reopened["image_urls"] == ["/output/outputs/test/images/shot-001.png"]
    assert reopened["request"]["topic"] == STORY["topic"]

    finished = client.post("/api/review-visual-storyboard", json={"thread_id": film["thread_id"], "action": "approve"}).json()
    assert finished["status"] == "complete"
    assert drafts(client) == []


def test_a_film_no_longer_waiting_is_removed_when_reopened(client):
    film = client.post("/api/create-video", json={"topic": STORY["topic"]}).json()
    [paused] = drafts(client)
    backend.graph.paused = False  # e.g. approved from another device, or its checkpoint is gone

    assert client.get(f"/api/drafts/{paused['id']}/review").status_code == 410
    assert drafts(client) == []
    assert film["thread_id"] == paused["thread_id"]


def test_story_draft_is_kept_when_the_film_fails(client, monkeypatch):
    story_id = client.post("/api/drafts", json=STORY).json()["id"]
    monkeypatch.setattr(backend.graph, "invoke", lambda *_args: {"pipeline_status": "pipeline_incomplete"})
    response = client.post("/api/create-video", json={"topic": STORY["topic"], "draft_id": story_id}).json()
    assert response["status"] == "pipeline_incomplete"
    assert [item["id"] for item in drafts(client)] == [story_id]
