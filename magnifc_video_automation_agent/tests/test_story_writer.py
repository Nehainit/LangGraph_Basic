"""Story writer chat: prompt, validation retries, and the API. Model calls are mocked."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from video_automation import backend, character_library
from video_automation.agents import story_writer_agent
from video_automation.agents.story_agent import UnsafeContentError


class FakeModel:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.answers.pop(0), usage_metadata={})


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(story_writer_agent, "load_model", lambda _name: fake)
    monkeypatch.setattr(story_writer_agent, "_check_input_safety", lambda _content: {"cost_usd": 0.0001})
    return fake


SELLER = {"name": "Old flower seller", "kind": "An elderly woman who sells marigolds", "appearance": ""}


def answer(story="@Meera flies a kite.", title="The Kite", characters=None):
    return json.dumps({"title": title, "story": story, "reply": "Here is a first draft.", "characters": characters or []})


def test_writer_sizes_the_story_and_retries_a_story_that_is_too_long(model):
    model.answers = ["not json", answer("word " * 500), answer()]
    messages = [
        {"role": "user", "content": "@Meera and a kite"},
        {"role": "assistant", "content": "The Kite\n\nMeera flies a kite."},
        {"role": "user", "content": "make it windier"},
    ]
    result = story_writer_agent.write_story(messages, seconds=60, language="Hindi")
    assert result["story"] == "@Meera flies a kite." and len(result["llm_evaluations"]) == 4
    system = model.prompts[0][0]["content"]
    assert "about 150 words" in system and "60-second film" in system and "in Hindi" in system
    assert model.prompts[0][1:] == messages
    assert "too long" in model.prompts[2][-1]["content"]


def test_writer_gives_up_after_three_bad_answers(model):
    model.answers = ["{}", "{}", "{}"]
    with pytest.raises(RuntimeError, match="did not return a usable story"):
        story_writer_agent.write_story([{"role": "user", "content": "a fox"}])


def test_story_writer_api(monkeypatch, tmp_path):
    monkeypatch.setattr(character_library, "LIBRARY_ROOT", tmp_path / "library")
    client = TestClient(backend.app)
    calls = []

    def fake_write(messages, *, seconds, language, saved_names):
        calls.append((messages, seconds, language, saved_names))
        return {"title": "The Kite", "story": "Meera flies a kite.", "reply": "Done.", "characters": [SELLER], "llm_evaluations": [{"cost_usd": 0.0002}]}

    monkeypatch.setattr(backend, "write_story", fake_write)
    response = client.post("/api/story-writer", json={"messages": [{"role": "user", "content": "a kite"}], "duration": 45})
    assert response.json() == {"title": "The Kite", "story": "Meera flies a kite.", "reply": "Done.", "characters": [SELLER], "cost_usd": 0.0002}
    assert calls == [([{"role": "user", "content": "a kite"}], 45, "English", [])]

    assert client.post("/api/story-writer", json={"messages": [{"role": "assistant", "content": "hi"}]}).status_code == 400
    assert client.post("/api/story-writer", json={"messages": []}).status_code == 422

    def blocked(*_args, **_kwargs):
        raise UnsafeContentError(["violence"])

    monkeypatch.setattr(backend, "write_story", blocked)
    response = client.post("/api/story-writer", json={"messages": [{"role": "user", "content": "x"}]})
    assert response.status_code == 400 and response.json()["detail"]["categories"] == ["violence"]
    assert client.get("/static/writer.js").status_code == 200


def test_writer_lists_every_visible_character_that_is_not_saved(model):
    meera = {"name": "@Meera", "kind": "A girl", "appearance": ""}
    # The user typed @Krishna, but Krishna is not in the library, so he must still be listed.
    krishna = {"name": "@Krishna", "kind": "A young cowherd god with a flute", "appearance": "Blue skin, peacock feather"}
    long_name = {"name": "N" * 90, "kind": "A dog", "appearance": "Brown"}
    model.answers = [
        answer(characters=[{"name": "Raju"}]),
        answer(characters=[meera, krishna, SELLER, long_name]),
    ]
    result = story_writer_agent.write_story([{"role": "user", "content": "@Meera meets @Krishna"}], saved_names=["Meera", "Arjun"])
    assert result["characters"] == [{**krishna, "name": "Krishna"}, SELLER, {**long_name, "name": "N" * 60}]
    assert "exactly name, kind, and appearance" in model.prompts[1][-1]["content"]
    system = model.prompts[0][0]["content"]
    assert "saved characters are: Meera, Arjun" in system and "even if the user typed @" in system


def test_writer_knows_when_the_library_is_empty(model):
    model.answers = [answer()]
    story_writer_agent.write_story([{"role": "user", "content": "a fox"}])
    assert "saved characters are: none yet" in model.prompts[0][0]["content"]
