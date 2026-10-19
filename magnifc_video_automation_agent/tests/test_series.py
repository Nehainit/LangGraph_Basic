from pathlib import Path

import pytest
from fastapi import HTTPException

from video_automation import series
from video_automation.agents import image_agent
from video_automation.prompts import series_context, story_prompt, story_review_prompt

USER = "user_123"


def _finished_film(tmp_path, name="ep1", ending="Mira opens the glowing door.", characters=("Mira",)):
    folder = tmp_path / name
    folder.mkdir()
    sheets, frames = [], []
    for index, character in enumerate(characters, start=1):
        sheet = folder / f"sheet_{index}.png"
        sheet.write_bytes(f"{name}-{character}-sheet".encode())
        sheets.append(str(sheet))
    for index in (1, 2):
        frame = folder / f"shot_{index}.png"
        frame.write_bytes(f"{name}-frame-{index}".encode())
        frames.append(str(frame))
    return {
        "topic": f"{name} topic",
        "story": f"{name} story. {ending}",
        "story_outline": {
            "idea": f"{name} idea",
            "characters": [{"name": character, "description": f"{character}, a girl in a red coat"} for character in characters],
            "structure": [{"description": f"{name} start."}, {"description": ending}],
        },
        "story_beats": [{"description": f"{name} start."}, {"description": ending}],
        "parsed_requirements": {"visual_style": "soft watercolour"},
        "character_reference_files": sheets,
        "scenes": [{"location_id": "location-001", "location_description": "an attic full of clocks"}],
        "image_files": frames,
        "aspect_ratio": "9:16",
    }


def test_finished_film_starts_a_series_with_a_continuity_snapshot(tmp_path):
    values = _finished_film(tmp_path)

    record = series.start_series(USER, "thread-1", values, "/output/ep1.mp4", title="The Clockmaker")

    assert record["title"] == "The Clockmaker"
    assert record["next_episode"] == 2
    continuity = series._load(USER)[0]["episodes"][0]["continuity"]
    assert continuity["ending"] == "Mira opens the glowing door."
    assert continuity["visual_style"] == "soft watercolour"
    assert continuity["locations"] == ["an attic full of clocks"]
    # Copied out of the film's outputs, so clearing outputs/ never breaks the series.
    assert Path(continuity["cast"][0]["sheet_file"]).read_bytes() == b"ep1-Mira-sheet"
    assert not continuity["cast"][0]["sheet_file"].startswith(str(tmp_path / "ep1"))
    # Without clips, the last storyboard frame is where the film ends.
    assert Path(continuity["final_frame_file"]).read_bytes() == b"ep1-frame-2"


def test_starting_a_series_twice_returns_the_same_series(tmp_path):
    values = _finished_film(tmp_path)
    first = series.start_series(USER, "thread-1", values, "/output/ep1.mp4")
    again = series.start_series(USER, "thread-1", values, "/output/ep1.mp4")

    assert again["id"] == first["id"]
    assert len(series._load(USER)) == 1


def test_unfinished_film_cannot_start_a_series(tmp_path):
    with pytest.raises(HTTPException) as error:
        series.start_series(USER, "thread-1", _finished_film(tmp_path), None)
    assert error.value.status_code == 409


def test_next_episode_continues_from_the_previous_ending(tmp_path):
    record = series.start_series(USER, "thread-1", _finished_film(tmp_path), "/output/ep1.mp4", title="The Clockmaker")

    state = series.next_episode_state(USER, record["id"], "thread-2", "Mira steps through the door")

    assert state["episode_number"] == 2
    assert state["visual_style"] == "soft watercolour"
    assert [character["name"] for character in state["library_characters"]] == ["Mira"]
    assert Path(state["library_characters"][0]["sheet_file"]).is_file()
    previous = state["previous_episode"]
    assert previous["episode_number"] == 1
    assert previous["ending"] == "Mira opens the glowing door."
    assert Path(previous["final_frame_file"]).is_file()
    assert series._load(USER)[0]["episodes"][-1] == {
        "number": 2, "thread_id": "thread-2", "status": "making", "topic": "Mira steps through the door",
    }


def test_a_new_attempt_replaces_an_episode_that_never_finished(tmp_path):
    record = series.start_series(USER, "thread-1", _finished_film(tmp_path), "/output/ep1.mp4")
    series.next_episode_state(USER, record["id"], "failed-attempt", "first try")
    series.next_episode_state(USER, record["id"], "thread-2", "second try")

    episodes = series._load(USER)[0]["episodes"]
    assert [(episode["number"], episode["thread_id"]) for episode in episodes] == [(1, "thread-1"), (2, "thread-2")]


def test_finished_episode_becomes_the_starting_point_and_keeps_the_whole_cast(tmp_path):
    record = series.start_series(USER, "thread-1", _finished_film(tmp_path, characters=("Mira", "Tock")), "/output/ep1.mp4")
    series.next_episode_state(USER, record["id"], "thread-2", "Mira alone")
    series.finish_episode(USER, "thread-2", _finished_film(tmp_path, "ep2", ending="Mira lands in a snowy market."), "/output/ep2.mp4")

    state = series.next_episode_state(USER, record["id"], "thread-3", "The market")

    assert state["episode_number"] == 3
    assert state["previous_episode"]["ending"] == "Mira lands in a snowy market."
    assert state["previous_episode"]["earlier_episodes"] == ["ep1 idea"]
    sheets = {character["name"]: Path(character["sheet_file"]).read_bytes() for character in state["library_characters"]}
    # Mira uses her latest sheet; Tock, absent from episode 2, still comes back from episode 1.
    assert sheets == {"Mira": b"ep2-Mira-sheet", "Tock": b"ep1-Tock-sheet"}


def test_finishing_without_a_video_keeps_the_episode_open(tmp_path):
    record = series.start_series(USER, "thread-1", _finished_film(tmp_path), "/output/ep1.mp4")
    series.next_episode_state(USER, record["id"], "thread-2", "next")
    series.finish_episode(USER, "thread-2", _finished_film(tmp_path, "ep2"), None)

    assert series._load(USER)[0]["episodes"][-1]["status"] == "making"


def test_final_frame_is_taken_at_the_last_clips_edit_out_point(tmp_path, monkeypatch):
    clip = tmp_path / "shot-003.mp4"
    clip.write_bytes(b"clip")
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"frame")

    monkeypatch.setattr(series.subprocess, "run", fake_run)
    target = tmp_path / "final.png"

    assert series._final_frame({"edit_timeline": [{"video_file": str(clip), "source_out": 2.4}]}, target) == target
    assert calls[0][calls[0].index("-ss") + 1] == "2.350"
    assert calls[0][calls[0].index("-i") + 1] == str(clip)


def test_story_prompts_carry_series_continuity_only_for_episodes():
    previous = {
        "episode_number": 1, "series_title": "The Clockmaker", "story": "Mira finds a door.",
        "ending": "Mira opens the glowing door.", "recurring_cast": ["Mira: a girl in a red coat"], "locations": [],
    }
    assert series_context(None) == ""

    prompt = story_prompt(user_request={"topic": "next"}, requirements=None, previous_story=None, review_note="", feedback="", previous_episode=previous)
    assert "episode 2, the direct continuation of episode 1" in prompt
    assert "Mira opens the glowing door." in prompt
    assert "SERIES CONTINUITY" not in story_prompt(user_request={"topic": "next"}, requirements=None, previous_story=None, review_note="", feedback="")

    review = story_review_prompt(requirements={}, story={}, previous_episode=previous)
    assert "first beat does not continue" in review


def test_first_shot_of_an_episode_uses_the_previous_final_frame(tmp_path, monkeypatch):
    from tests.test_image_agent import _shot_image_state

    final_frame = tmp_path / "final_frame.png"
    final_frame.write_bytes(b"frame")
    state = {
        **_shot_image_state(tmp_path, [None, None]),
        "image_provider": "fal",
        "previous_episode": {"episode_number": 1, "final_frame_file": str(final_frame)},
    }
    jobs = []

    def fake_job(job):
        jobs.append(job)
        Path(job["image_file"]).write_bytes(b"image")
        return {
            "shot_id": job["shot"]["shot_id"], "scene_id": "scene-001", "image_path": str(job["image_file"]),
            "generation_status": "success", "generation_attempt": 1, "error": None,
        }

    monkeypatch.setattr(image_agent, "_generate_shot_image_job", fake_job)
    image_agent.create_visual_storyboard(state)

    by_shot = {job["shot"]["shot_id"]: job for job in jobs}
    assert str(final_frame) in by_shot["shot-001"]["reference_files"]
    assert any(label.startswith("final frame of the previous episode") for label in by_shot["shot-001"]["reference_labels"])
    assert str(final_frame) not in by_shot["shot-002"]["reference_files"]


def test_backend_makes_the_next_episode_and_saves_its_continuity(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from video_automation import backend

    record = series.start_series(None, "thread-1", _finished_film(tmp_path), "/output/ep1.mp4")
    episode_two = _finished_film(tmp_path, "ep2", ending="Mira lands in a snowy market.")

    class EpisodeGraph:
        initial = None

        def invoke(self, value, _config):
            if isinstance(value, dict):
                EpisodeGraph.initial = value
                return {"__interrupt__": [SimpleNamespace(value={"stage": "visual_storyboard_review", "storyboard": [], "image_files": [], "actions": ["approve"]})]}
            return {"story": "Story.", "storyboard": [], "warnings": [], "video_file": str(backend.ROOT / "outputs/ep2.mp4")}

        def get_state(self, _config):
            return SimpleNamespace(next=("review_visual_storyboard",), values=episode_two, tasks=())

    monkeypatch.setattr(backend, "graph", EpisodeGraph())
    monkeypatch.setattr(backend, "_archive", lambda *_args: None)
    monkeypatch.setattr(backend, "validate_model_configuration", lambda _agent: "vision-model")

    response = backend.create_video(backend.VideoRequest(topic="Mira steps through", series_id=record["id"]), user_id=None)
    initial = EpisodeGraph.initial
    assert initial["episode_number"] == 2
    assert initial["visual_style"] == "soft watercolour"
    assert initial["previous_episode"]["ending"] == "Mira opens the glowing door."
    assert [character["name"] for character in initial["library_characters"]] == ["Mira"]

    backend.submit_visual_storyboard_review(
        backend.VisualStoryboardReviewRequest(thread_id=response["thread_id"], action="approve"), user_id=None,
    )
    assert series.next_episode_state(None, record["id"], "thread-3", "next")["previous_episode"]["ending"] == "Mira lands in a snowy market."
