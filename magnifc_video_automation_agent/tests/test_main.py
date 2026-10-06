from types import SimpleNamespace
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from video_automation import main
from video_automation.agent_graph import persistent_checkpointer


def _input_from(monkeypatch, answers, prompts):
    values = iter(answers)

    def fake_input(prompt):
        prompts.append(prompt)
        return next(values)

    monkeypatch.setattr("builtins.input", fake_input)


def test_cli_image_provider_blank_defaults_to_magnific(monkeypatch):
    prompts = []
    _input_from(monkeypatch, ["A story", "30", "English", "", "", ""], prompts)

    state = main.get_user_input()

    assert state["image_provider"] == "magnific"
    assert prompts[-1] == "Generation provider for images and videos (magnific or fal) [magnific]: "


def test_cli_image_provider_reprompts_invalid_value(monkeypatch, capsys):
    prompts = []
    _input_from(monkeypatch, ["A story", "30", "English", "", "", "unknown", "fal"], prompts)

    state = main.get_user_input()

    assert state["image_provider"] == "fal"
    assert prompts.count("Generation provider for images and videos (magnific or fal) [magnific]: ") == 2
    assert "Use magnific or fal." in capsys.readouterr().out


@pytest.mark.parametrize("stage,action", [
    ("scene_plan_review", "revise_narration"),
    ("visual_plan_review", "revise_scenes"),
    ("shot_plan_review", "revise_visuals"),
])
def test_cli_handles_failed_plan_reviews(monkeypatch, capsys, tmp_path, stage, action):
    review = {
        "stage": stage, "revision_reason": "Timing needs revision.",
        "issues": ["The plan exceeds the narration timing."],
        "actions": ["retry", action],
    }

    class FakeGraph:
        def __init__(self):
            self.calls = []

        def invoke(self, value, _config):
            self.calls.append(value)
            if len(self.calls) == 1:
                return {"__interrupt__": [SimpleNamespace(value=review)]}
            return {"story": "Finished"}

    graph = FakeGraph()
    monkeypatch.setattr(main, "build_story_graph", lambda **_kwargs: graph)
    monkeypatch.setattr(main, "persistent_checkpointer", lambda _path: object())
    monkeypatch.setattr(main, "CLI_LAST_RUN_FILE", tmp_path / "last-run")
    monkeypatch.setattr(main, "get_user_input", lambda: {"topic": "A story"})
    answers = iter([action, "Fix the timing"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    main.main([])

    assert graph.calls[1].resume == {"action": action, "note": "Fix the timing"}
    assert "Timing needs revision." in capsys.readouterr().out


def test_cli_resume_retries_failed_node_without_restarting(monkeypatch, tmp_path):
    calls = []
    attempts = 0

    def first(_state):
        calls.append("first")
        return {"first_done": True}

    def second(_state):
        nonlocal attempts
        attempts += 1
        calls.append("second")
        if attempts == 1:
            raise RuntimeError("temporary failure")
        return {"story": "Finished"}

    def graph_for(checkpointer):
        builder = StateGraph(dict)
        builder.add_node("first", first)
        builder.add_node("second", second)
        builder.add_edge(START, "first")
        builder.add_edge("first", "second")
        builder.add_edge("second", END)
        return builder.compile(checkpointer=checkpointer)

    checkpoint = tmp_path / "checkpoints.sqlite"
    thread_id = "saved-cli-run"
    config = {"configurable": {"thread_id": thread_id}}
    with pytest.raises(RuntimeError, match="temporary failure"):
        graph_for(persistent_checkpointer(checkpoint)).invoke({"topic": "A story"}, config)

    monkeypatch.setattr(main, "build_story_graph", graph_for)
    monkeypatch.setattr(main, "persistent_checkpointer", lambda _path: persistent_checkpointer(checkpoint), raising=False)
    last_run = tmp_path / "last-run"
    last_run.write_text(thread_id, encoding="utf-8")
    monkeypatch.setattr(main, "CLI_LAST_RUN_FILE", last_run, raising=False)
    monkeypatch.setattr(main, "get_user_input", lambda: (_ for _ in ()).throw(AssertionError("Asked for a new story")))
    monkeypatch.setattr("sys.argv", ["main", "--resume"])

    main.main()

    assert calls == ["first", "second", "second"]


def test_cli_resume_at_review_does_not_repeat_completed_node(monkeypatch, tmp_path):
    calls = []

    class State(TypedDict):
        topic: str
        story: str
        approved: bool

    def first(_state):
        calls.append("first")
        return {"story": "Raja returns home."}

    def review(_state):
        answer = interrupt({"stage": "story_review", "story": "Raja returns home."})
        return {"approved": answer["action"] == "approve"}

    def graph_for(checkpointer):
        builder = StateGraph(State)
        builder.add_node("first", first)
        builder.add_node("review", review)
        builder.add_edge(START, "first")
        builder.add_edge("first", "review")
        builder.add_edge("review", END)
        return builder.compile(checkpointer=checkpointer)

    checkpoint = tmp_path / "checkpoints.sqlite"
    thread_id = "paused-cli-run"
    config = {"configurable": {"thread_id": thread_id}}
    first_result = graph_for(persistent_checkpointer(checkpoint)).invoke({"topic": "A story"}, config)
    assert "__interrupt__" in first_result

    monkeypatch.setattr(main, "build_story_graph", graph_for)
    monkeypatch.setattr(main, "persistent_checkpointer", lambda _path: persistent_checkpointer(checkpoint))
    last_run = tmp_path / "last-run"
    last_run.write_text(thread_id, encoding="utf-8")
    monkeypatch.setattr(main, "CLI_LAST_RUN_FILE", last_run)
    monkeypatch.setattr(main, "get_user_input", lambda: (_ for _ in ()).throw(AssertionError("Asked for a new story")))
    monkeypatch.setattr("builtins.input", lambda _prompt: "approve")

    main.main(["--resume"])

    assert calls == ["first"]
