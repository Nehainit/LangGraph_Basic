import storyboard_agent


class FakeModel:
    def invoke(self, prompt):
        assert "Raja and Rani escaped the devil." in prompt
        scenes = []
        for scene_number in range(1, 6):
            scenes.append(
                (
                    f'{{"scene_number": {scene_number}, "start_time": "00:{(scene_number - 1) * 12:02d}", '
                    f'"end_time": "00:{scene_number * 12:02d}", '
                    f'"visuals": "Scene {scene_number} visual.", "camera": "wide shot", '
                    f'"narration": "Scene {scene_number} narration."}}'
                )
            )
        return type(
            "Response",
            (),
            {
                "content": '{"storyboard": [' + ",".join(scenes) + "]}"
            },
        )()


def test_create_storyboard():
    original = storyboard_agent.load_model
    storyboard_agent.load_model = lambda agent_name: FakeModel()
    try:
        result = storyboard_agent.create_storyboard(
            {
                "topic": "x",
                "tone": "mystical",
                "duration": "1 minute",
                "language": "English",
                "characters": "Raja, Rani, Devil",
                "story": "Raja and Rani escaped the devil.",
            }
        )
    finally:
        storyboard_agent.load_model = original

    assert len(result["storyboard"]) == 5
    assert result["storyboard"][0]["camera"] == "wide shot"


def test_requires_story():
    try:
        storyboard_agent.create_storyboard(
            {
                "topic": "x",
                "tone": "mystical",
                "duration": "1 minute",
                "language": "English",
                "characters": "Raja, Rani, Devil",
            }
        )
    except RuntimeError:
        return
    raise AssertionError("storyboard should require a story")


if __name__ == "__main__":
    test_create_storyboard()
    test_requires_story()
