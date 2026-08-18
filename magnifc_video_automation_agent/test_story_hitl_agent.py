from langgraph.types import Command

import agent_graph


def fake_story_creator(state):
    note = state.get("review_note", "")
    return {"story": f"Draft story. {note}".strip()}


def fake_storyboard_creator(state):
    return {"storyboard": [{"scene_number": 1, "start_time": "00:00", "end_time": "00:10", "visuals": state["story"], "camera": "wide", "narration": state["story"]}]}


def fake_image_creator(state):
    return {"image_files": ["outputs/test/images/scene_01.png"]}


def fake_narration_creator(state):
    return {"narration_file": "outputs/test/audio/narration.mp3"}


def fake_soundfx_creator(state):
    return {"sfx_files": ["outputs/test/audio/sfx_scene_01.mp3"]}


def fake_subtitle_creator(state):
    return {"subtitles": [{"scene_number": 1, "text": state["story"]}], "subtitle_file": "outputs/test/subtitles/story.srt"}


def fake_video_creator(state):
    return {"mixed_audio_file": "outputs/test/video/mixed_audio.mp3", "video_file": "outputs/test/video/final_reel.mp4"}


def test_interrupt_then_approve():
    graph = agent_graph.build_story_graph(
        fake_story_creator,
        fake_storyboard_creator,
        fake_image_creator,
        fake_narration_creator,
        fake_soundfx_creator,
        fake_subtitle_creator,
        fake_video_creator,
    )
    config = {"configurable": {"thread_id": "hitl-approve"}}
    state = {
        "topic": "Raja and Rani are trapped in a mystical forest by a devil",
        "tone": "mystical",
        "duration": "1 minute",
        "language": "English",
        "characters": ["Raja", "Rani", "Devil"],
    }

    first = graph.invoke(state, config)
    assert "__interrupt__" in first
    assert first["__interrupt__"][0].value["actions"] == ["approve", "edit", "regenerate"]

    final = graph.invoke(Command(resume={"action": "approve"}), config)
    assert final["approved"] is True
    assert final["story"] == "Draft story."
    assert final["storyboard"][0]["scene_number"] == 1
    assert final["image_files"] == ["outputs/test/images/scene_01.png"]
    assert final["narration_file"] == "outputs/test/audio/narration.mp3"
    assert final["sfx_files"] == ["outputs/test/audio/sfx_scene_01.mp3"]
    assert final["subtitle_file"] == "outputs/test/subtitles/story.srt"
    assert final["video_file"] == "outputs/test/video/final_reel.mp4"


def test_edit_resume_replaces_story():
    graph = agent_graph.build_story_graph(
        fake_story_creator,
        fake_storyboard_creator,
        fake_image_creator,
        fake_narration_creator,
        fake_soundfx_creator,
        fake_subtitle_creator,
        fake_video_creator,
    )
    config = {"configurable": {"thread_id": "hitl-edit"}}
    graph.invoke(
        {
            "topic": "x",
            "tone": "y",
            "duration": "1 minute",
            "language": "English",
            "characters": ["Raja"],
        },
        config,
    )

    edited = graph.invoke(Command(resume={"action": "edit", "story": "Edited story."}), config)
    assert "__interrupt__" in edited
    assert edited["approved"] is False
    assert edited["story"] == "Edited story."

    final = graph.invoke(Command(resume={"action": "approve"}), config)
    assert final["approved"] is True
    assert final["story"] == "Edited story."


def test_regenerate_routes_back_to_story():
    graph = agent_graph.build_story_graph(
        fake_story_creator,
        fake_storyboard_creator,
        fake_image_creator,
        fake_narration_creator,
        fake_soundfx_creator,
        fake_subtitle_creator,
        fake_video_creator,
    )
    config = {"configurable": {"thread_id": "hitl-regenerate"}}
    graph.invoke(
        {
            "topic": "x",
            "tone": "y",
            "duration": "1 minute",
            "language": "English",
            "characters": ["Raja"],
        },
        config,
    )

    second = graph.invoke(Command(resume={"action": "regenerate", "note": "make it warmer"}), config)
    assert "__interrupt__" in second
    assert second["story"] == "Draft story. make it warmer"


if __name__ == "__main__":
    test_interrupt_then_approve()
    test_edit_resume_replaces_story()
    test_regenerate_routes_back_to_story()
