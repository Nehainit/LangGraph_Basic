"""Character library API: per-user records, sheet and emotion-board drawing, and redraws. Image calls are mocked."""

import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from video_automation import character_library
from video_automation.auth import require_user
from video_automation.backend import app
from video_automation.cost_control import PendingJobError


MEERA = {
    "name": "Meera",
    "kind": "A curious 8-year-old girl from Jaipur",
    "appearance": "Two long black braids with red ribbons",
    "outfit": "Yellow kurta",
    "visual_style": "3d_animation",
}


@pytest.fixture
def draws(monkeypatch, tmp_path):
    monkeypatch.setattr(character_library, "LIBRARY_ROOT", tmp_path / "library")
    calls = []

    def fake_draw(prompt, image_file, references, labels, note):
        calls.append({"prompt": prompt, "file": image_file.name, "references": references, "labels": labels, "note": note})
        image_file.write_bytes(b"png")

    monkeypatch.setattr(character_library, "_draw", fake_draw)
    return calls


def photo_data_url(color="red", size=(3000, 1500)) -> str:
    image = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[0x8825] = {2: (12.0, 34.0, 56.0)}  # GPS info a phone photo would carry
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_new_character_draws_sheet_then_emotion_board_from_it(client, draws):
    created = client.post("/api/characters", json=MEERA).json()
    assert created["sheet"] is None and created["sheet_url"] is None

    sheet = client.post(f"/api/characters/{created['id']}/sheet", json={}).json()
    assert sheet["sheet"] == "sheet_v001.png"
    assert "Meera" in draws[0]["prompt"] and "Polished 3D animated" in draws[0]["prompt"]
    assert draws[0]["references"] == []

    board = client.post(f"/api/characters/{created['id']}/emotions", json={}).json()
    assert board["emotions"] == "emotions_v001.png"
    assert draws[1]["references"][0].endswith("sheet_v001.png")
    assert "Top row, left to right: Happy, Sad, Angry" in draws[1]["prompt"]
    assert board["spent_usd"] == pytest.approx(0.30)

    image = client.get(board["emotions_url"])
    assert image.status_code == 200 and image.content == b"png"
    listing = client.get("/api/characters").json()
    assert [item["id"] for item in listing["characters"]] == [created["id"]]
    assert listing["emotions"] == ["happy", "sad", "angry", "surprised", "scared", "thinking"]


def test_change_note_edits_current_sheet_and_marks_board_stale(client, draws):
    character_id = client.post("/api/characters", json=MEERA).json()["id"]
    client.post(f"/api/characters/{character_id}/sheet", json={})
    client.post(f"/api/characters/{character_id}/emotions", json={})

    tweaked = client.post(f"/api/characters/{character_id}/sheet", json={"note": "make her scarf red"}).json()
    assert tweaked["sheet"] == "sheet_v002.png"
    assert tweaked["emotions_stale"] is True
    assert draws[-1]["references"][0].endswith("sheet_v001.png")
    assert draws[-1]["prompt"].startswith("MANDATORY CHANGE: make her scarf red.")

    fresh = client.post(f"/api/characters/{character_id}/sheet", json={"fresh": True}).json()
    assert fresh["sheet"] == "sheet_v003.png" and draws[-1]["references"] == []

    board = client.post(f"/api/characters/{character_id}/emotions", json={"note": "softer anger"}).json()
    assert board["emotions_stale"] is False
    assert [path.rsplit("/", 1)[-1] for path in draws[-1]["references"]] == ["sheet_v003.png", "emotions_v001.png"]


def test_editing_details_marks_sheet_stale_and_redraw_keeps_the_face(client, draws):
    character_id = client.post("/api/characters", json=MEERA).json()["id"]
    unchanged = client.put(f"/api/characters/{character_id}", json=MEERA).json()
    assert unchanged["sheet_stale"] is False

    client.post(f"/api/characters/{character_id}/sheet", json={})
    edited = client.put(f"/api/characters/{character_id}", json={**MEERA, "outfit": "Green lehenga"}).json()
    assert edited["sheet_stale"] is True and edited["outfit"] == "Green lehenga"

    redrawn = client.post(f"/api/characters/{character_id}/sheet", json={}).json()
    assert redrawn["sheet_stale"] is False
    assert draws[-1]["references"][0].endswith("sheet_v001.png")
    assert "Green lehenga" in draws[-1]["prompt"] and "details were updated" in draws[-1]["prompt"]


def test_invalid_requests_are_rejected_without_drawing(client, draws):
    assert client.post("/api/characters", json={**MEERA, "name": "   "}).status_code == 422
    assert client.post("/api/characters", json={**MEERA, "visual_style": "watercolor"}).status_code == 422
    character_id = client.post("/api/characters", json=MEERA).json()["id"]
    assert client.post(f"/api/characters/{character_id}/emotions", json={}).status_code == 400
    assert client.post(f"/api/characters/{character_id}/sheet", json={"note": "red scarf"}).status_code == 400
    assert client.get("/api/characters/../../etc/files/sheet_v001.png").status_code == 404
    assert client.get(f"/api/characters/{character_id}/files/character.json").status_code == 404
    assert client.get("/api/characters/0123456789ab").status_code == 404
    assert draws == []


def test_characters_are_private_to_each_user(client, draws):
    app.dependency_overrides[require_user] = lambda: "user_a"
    character_id = client.post("/api/characters", json=MEERA).json()["id"]
    client.post(f"/api/characters/{character_id}/sheet", json={})

    app.dependency_overrides[require_user] = lambda: "user_b"
    assert client.get("/api/characters").json()["characters"] == []
    assert client.get(f"/api/characters/{character_id}/files/sheet_v001.png").status_code == 404
    assert client.delete(f"/api/characters/{character_id}").status_code == 404

    app.dependency_overrides[require_user] = lambda: "user_a"
    assert client.delete(f"/api/characters/{character_id}").json() == {"deleted": character_id}
    assert client.get("/api/characters").json()["characters"] == []


def test_failed_or_pending_drawings_keep_the_previous_sheet(client, draws, monkeypatch):
    character_id = client.post("/api/characters", json=MEERA).json()["id"]
    client.post(f"/api/characters/{character_id}/sheet", json={})

    def pending(*_args):
        raise PendingJobError("still running")

    monkeypatch.setattr(character_library, "_draw", pending)
    assert client.post(f"/api/characters/{character_id}/sheet", json={"note": "red scarf"}).status_code == 504

    def failing(*_args):
        raise RuntimeError("content policy")

    monkeypatch.setattr(character_library, "_draw", failing)
    response = client.post(f"/api/characters/{character_id}/sheet", json={"note": "red scarf"})
    assert response.status_code == 502 and "content policy" in response.json()["detail"]
    record = client.get(f"/api/characters/{character_id}").json()
    assert record["sheet"] == "sheet_v001.png" and record["spent_usd"] == pytest.approx(0.15)


def test_a_character_draws_one_image_at_a_time(client, draws, monkeypatch):
    character_id = client.post("/api/characters", json=MEERA).json()["id"]
    folder = character_library._user_dir(None) / character_id
    with character_library._drawing(folder):
        assert client.post(f"/api/characters/{character_id}/sheet", json={}).status_code == 409
        assert client.put(f"/api/characters/{character_id}", json=MEERA).status_code == 409
    assert client.post(f"/api/characters/{character_id}/sheet", json={}).status_code == 200


def test_characters_page_is_served(client):
    for path, content_type in (
        ("/characters", "text/html"),
        ("/static/characters.js", "javascript"),
        ("/static/characters.css", "text/css"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert content_type in response.headers["content-type"]


def test_uploaded_reference_images_shape_the_sheet(client, draws):
    character_id = client.post("/api/characters", json={**MEERA, "visual_style": "realistic"}).json()["id"]
    uploaded = client.post(f"/api/characters/{character_id}/references", json={"image": photo_data_url()}).json()
    assert [item["name"] for item in uploaded["references"]] == ["ref_v001.png"]
    assert uploaded["sheet_stale"] is False

    stored = client.get(uploaded["references"][0]["url"])
    with Image.open(io.BytesIO(stored.content)) as image:
        assert image.format == "PNG" and max(image.size) == 2048 and not image.getexif()

    sheet = client.post(f"/api/characters/{character_id}/sheet", json={}).json()
    assert [path.rsplit("/", 1)[-1] for path in draws[-1]["references"]] == ["ref_v001.png"]
    assert "uploaded by the user" in draws[-1]["labels"][0]
    assert "attached reference image shows what this character looks like" in draws[-1]["prompt"]
    assert "Photorealistic" in draws[-1]["prompt"]

    # A change note edits the drawn sheet, not the photos.
    client.post(f"/api/characters/{character_id}/sheet", json={"note": "add a red scarf"})
    assert [path.rsplit("/", 1)[-1] for path in draws[-1]["references"]] == [sheet["sheet"]]

    added = client.post(f"/api/characters/{character_id}/references", json={"image": photo_data_url("blue")}).json()
    assert added["sheet_stale"] is True
    client.post(f"/api/characters/{character_id}/sheet", json={})
    assert [path.rsplit("/", 1)[-1] for path in draws[-1]["references"]] == ["ref_v001.png", "ref_v002.png"]

    removed = client.delete(f"/api/characters/{character_id}/references/ref_v001.png").json()
    assert [item["name"] for item in removed["references"]] == ["ref_v002.png"] and removed["sheet_stale"] is True
    assert client.get(f"/api/characters/{character_id}/files/ref_v001.png").status_code == 404


def test_reference_uploads_are_validated(client, draws):
    character_id = client.post("/api/characters", json=MEERA).json()["id"]
    url = f"/api/characters/{character_id}/references"
    assert client.post(url, json={"image": "data:image/png;base64,not base64!"}).status_code == 400
    assert client.post(url, json={"image": base64.b64encode(b"plain text").decode()}).status_code == 400
    for _ in range(3):
        assert client.post(url, json={"image": photo_data_url(size=(64, 64))}).status_code == 200
    assert client.post(url, json={"image": photo_data_url(size=(64, 64))}).status_code == 400
    assert client.delete(f"{url}/sheet_v001.png").status_code == 404
    assert sorted(path.name for path in (character_library._user_dir(None) / character_id).glob("ref_*")) == [
        "ref_v001.png", "ref_v002.png", "ref_v003.png",
    ]


class CapturingGraph:
    def __init__(self):
        self.initial_state = None

    def invoke(self, value, _config):
        self.initial_state = value
        return {"story": "Done.", "storyboard": [], "video_file": None, "pipeline_status": "complete"}


def test_mentioned_characters_are_passed_to_the_film(client, draws, monkeypatch):
    from video_automation import backend

    monkeypatch.setattr(backend, "validate_model_configuration", lambda _agent: "vision-model")
    fake_graph = CapturingGraph()
    monkeypatch.setattr(backend, "graph", fake_graph)
    meera = client.post("/api/characters", json=MEERA).json()["id"]
    client.post(f"/api/characters/{meera}/sheet", json={})
    raju = client.post("/api/characters", json={**MEERA, "name": "Raju", "kind": "A grey donkey"}).json()["id"]

    response = client.post("/api/create-video", json={"topic": "@Meera and her donkey go to the fair", "character_ids": [raju]})
    assert response.status_code == 400 and "Raju" in response.json()["detail"]
    response = client.post("/api/create-video", json={"topic": "@Meera at the fair", "character_ids": ["0123456789ab"]})
    assert response.status_code == 400 and fake_graph.initial_state is None

    response = client.post("/api/create-video", json={"topic": "@Meera goes to the fair", "character_ids": [meera], "visual_style": "realistic"})
    assert response.status_code == 200
    state = fake_graph.initial_state
    assert state["topic"] == "Meera goes to the fair"
    assert state["characters"] == [
        "Meera: A curious 8-year-old girl from Jaipur. Appearance: Two long black braids with red ribbons. Outfit: Yellow kurta."
    ]
    assert state["library_characters"][0]["sheet_file"].endswith(f"{meera}/sheet_v001.png")
    assert state["visual_style"].startswith("Photorealistic")


def test_story_keeps_saved_descriptions_and_sheets_are_reused(monkeypatch, tmp_path):
    from video_automation.agents import image_agent, story_agent

    sheet = tmp_path / "sheet_v001.png"
    sheet.write_bytes(b"approved sheet")
    saved = {"name": "Meera", "description": "Saved description.", "sheet_file": str(sheet)}
    story = {
        "idea": "A day at the fair.",
        "characters": [
            {"character_id": "character-001", "name": "Meera", "description": "A girl, reworded by the model."},
            {"character_id": "character-002", "name": "Old Raju", "description": "A grey donkey."},
        ],
        "structure": [{"beat_id": "beat-001", "description": "Meera rides to the fair."}],
    }
    monkeypatch.setattr(story_agent, "create_story", lambda _state: {"story": story, "parsed_requirements": {}, "llm_evaluations": []})
    result = story_agent.create_story_node({"topic": "Meera at the fair", "library_characters": [saved]})
    assert [item["description"] for item in result["characters"]] == ["Saved description.", "A grey donkey."]

    state = {"topic": "Meera at the fair", "output_dir": str(tmp_path), "story": "Meera rides to the fair.", "story_outline": story,
             "library_characters": [saved], "image_provider": "fal"}
    out = tmp_path / "characters"
    out.mkdir()
    files, jobs = image_agent._character_sheet_jobs(state, out, "")
    assert files[0].read_bytes() == b"approved sheet"
    assert [job[2] for job in jobs] == [files[1]]


def test_studio_scripts_are_served(client):
    for path in ("/static/mentions.js", "/static/styles/realistic.jpg"):
        assert client.get(path).status_code == 200, path
