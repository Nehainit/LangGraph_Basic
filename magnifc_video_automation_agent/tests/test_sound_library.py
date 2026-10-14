import json
from types import SimpleNamespace

from video_automation import sound_library
from video_automation.agents import editor_agent


LIBRARY = [
    {"id": "suspense-1", "kind": "music", "tag": "suspense", "file": "music/suspense-1.mp3", "seconds": 60, "name": "Tense"},
    {"id": "romantic-2", "kind": "music", "tag": "romantic", "file": "music/romantic-2.mp3", "seconds": 60, "name": "Piano"},
    {"id": "sting-3", "kind": "sfx", "tag": "dramatic_sting", "file": "sfx/sting-3.mp3", "seconds": 2, "name": "Sting"},
    {"id": "gasp-4", "kind": "sfx", "tag": "crowd_gasp", "file": "sfx/gasp-4.mp3", "seconds": 2, "name": "Gasp"},
    {"id": "hit-5", "kind": "sfx", "tag": "cinematic_hit", "file": "sfx/hit-5.mp3", "seconds": 2, "name": "Hit"},
]


def _planner(monkeypatch, answer):
    prompts = []

    class Model:
        def invoke(self, prompt):
            prompts.append(prompt)
            return SimpleNamespace(content=json.dumps(answer))

    monkeypatch.setattr(sound_library, "load_model", lambda _name: Model())
    monkeypatch.setattr(sound_library, "invoke_with_evaluation", lambda model, prompt, **_: (model.invoke(prompt), {"agent": "sound-cues"}))
    return prompts


def test_cue_plan_keeps_valid_spaced_effects_and_the_chosen_music(monkeypatch):
    prompts = _planner(monkeypatch, {"music_id": "suspense-1", "cues": [
        {"sfx_id": "sting-3", "at_seconds": 3.0, "why": "everyone is shocked"},
        {"sfx_id": "gasp-4", "at_seconds": 3.8, "why": "too close to the sting"},
        {"sfx_id": "made-up", "at_seconds": 6.0, "why": "not in the library"},
        {"sfx_id": "suspense-1", "at_seconds": 7.0, "why": "music is not an effect"},
        {"sfx_id": "gasp-4", "at_seconds": 8.0, "why": "girlfriend reveal"},
        {"sfx_id": "hit-5", "at_seconds": 40.0, "why": "after the film ends"},
    ]})

    plan, evaluations = sound_library.plan_sound([{"text": "सब सन्न थे।", "start_seconds": 3.0, "end_seconds": 4.2}], 12.0, "story", LIBRARY)

    assert plan["music_id"] == "suspense-1"
    assert [(cue["sfx_id"], cue["start_seconds"]) for cue in plan["cues"]] == [("sting-3", 3.0), ("gasp-4", 8.0)]
    assert len(evaluations) == 1
    assert json.loads(prompts[0][1]["content"])["max_cues"] == 3  # one effect per 4 s of a 12 s film


def test_cue_plan_falls_back_to_music_when_the_planner_fails(monkeypatch):
    def broken(_name):
        raise RuntimeError("no model")

    monkeypatch.setattr(sound_library, "load_model", broken)

    plan, evaluations = sound_library.plan_sound([], 10.0, "story", LIBRARY)

    assert plan["music_id"] == "suspense-1" and plan["cues"] == [] and evaluations == []


def test_empty_library_plans_nothing():
    assert sound_library.plan_sound([], 10.0, "story", []) == ({"music": None, "cues": []}, [])


def test_freesound_search_keeps_only_popular_cc0_sounds(monkeypatch):
    seen = {}

    def fake_get(url):
        seen["url"] = url
        preview = {"preview-hq-mp3": "https://cdn.freesound.org/a.mp3"}
        cc0 = "http://creativecommons.org/publicdomain/zero/1.0/"
        return {"results": [
            {"id": 1, "name": "Dramatic Sting", "license": cc0, "num_downloads": 900, "previews": preview},
            {"id": 2, "name": "Dramatic sting", "license": "https://creativecommons.org/licenses/by/4.0/", "num_downloads": 900, "previews": preview},
            {"id": 3, "name": "Dramatic Sting", "license": "Creative Commons 0", "num_downloads": 3, "previews": preview},
            {"id": 4, "name": "Knock on door", "tags": ["dramatic"], "license": cc0, "num_downloads": 5000, "previews": preview},
            {"id": 5, "name": "Orchestra hit", "tags": ["sting", "dramatic"], "license": cc0, "num_downloads": 2000, "previews": preview},
        ]}

    monkeypatch.setattr(sound_library, "_get_json", fake_get)

    results = sound_library.search_freesound("dramatic sting", [1, 6], "key")

    # CC0 only, popular enough, every search word in the name or tags, most downloaded first.
    assert [item["id"] for item in results] == [5, 1]
    assert "Creative+Commons+0" in seen["url"] and "duration%3A%5B1+TO+6%5D" in seen["url"]


def test_editor_mixes_timed_effects_and_loops_library_music():
    state = {
        "narration_file": "narration.mp3", "music_file": "music.mp3",
        "sound_cues": [{"file": "sting.mp3", "start_seconds": 3.0, "volume": 0.6}],
    }

    command = editor_agent._audio_mix_command(state, [], 28.0, "mix.mp3")
    graph = command[command.index("-filter_complex") + 1]

    assert command[command.index("music.mp3") - 3:command.index("music.mp3")] == ["-stream_loop", "-1", "-i"]
    assert "adelay=3000|3000,volume=0.6[cue" in graph
    assert "atrim=0:28.000" in graph
