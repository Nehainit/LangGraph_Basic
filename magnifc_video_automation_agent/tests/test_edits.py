import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from video_automation import edits


@pytest.fixture
def library(tmp_path, monkeypatch):
    folder = tmp_path / "sounds"
    (folder / "music").mkdir(parents=True)
    (folder / "sfx").mkdir()
    entries = [
        {"id": "epic-1", "kind": "music", "tag": "epic", "name": "Epic", "seconds": 60.0, "file": "music/epic-1.mp3"},
        {"id": "calm-1", "kind": "music", "tag": "mystery", "name": "Calm", "seconds": 90.0, "file": "music/calm-1.mp3"},
        {"id": "whoosh-1", "kind": "sfx", "tag": "whoosh", "name": "Whoosh", "seconds": 1.2, "file": "sfx/whoosh-1.mp3"},
        {"id": "boing-1", "kind": "sfx", "tag": "comedy_boing", "name": "Boing", "seconds": 0.8, "file": "sfx/boing-1.mp3"},
    ]
    for entry in entries:
        (folder / entry["file"]).write_bytes(entry["id"].encode())
    monkeypatch.setattr(edits, "load_library", lambda: entries)
    monkeypatch.setattr(edits, "library_folder", lambda: folder)
    return folder


@pytest.fixture
def renders(monkeypatch):
    calls = []

    def fake_remix(state, video_file, **kwargs):
        calls.append({"state": state, "video_file": video_file, **kwargs})
        Path(video_file).write_bytes(b"video")

    monkeypatch.setattr(edits, "remix_video", fake_remix)
    return calls


def _film(tmp_path, library):
    return {
        "output_dir": str(tmp_path / "outputs" / "thread"),
        "video_file": str(tmp_path / "outputs" / "thread" / "video" / "final_reel.mp4"),
        "narration_file": "narration.mp3",
        "scene_timings": [{"scene_number": 1, "start_seconds": 0.0, "end_seconds": 6.0}, {"scene_number": 2, "start_seconds": 6.0, "end_seconds": 12.5}],
        "music_file": str(library / "music/epic-1.mp3"),
        "music_id": "epic-1",
        "sound_cues": [{"file": str(library / "sfx/whoosh-1.mp3"), "sfx_id": "whoosh-1", "start_seconds": 3.0, "volume": 0.6}],
    }


def test_catalog_lists_music_effects_and_filters(library):
    catalog = edits.sound_catalog()

    assert [item["id"] for item in catalog["music"]] == ["epic-1", "calm-1"]
    assert [item["id"] for item in catalog["sfx"]] == ["whoosh-1", "boing-1"]
    assert catalog["sfx"][0]["url"] == "/api/sounds/whoosh-1"
    assert "file" not in catalog["sfx"][0]
    assert "vintage" in catalog["filters"]


def test_unknown_sound_is_not_served(library):
    with pytest.raises(HTTPException) as error:
        edits.sound_file("../../etc/passwd")
    assert error.value.status_code == 404


def test_editor_opens_on_what_the_pipeline_chose(tmp_path, library, monkeypatch):
    film = _film(tmp_path, library)
    monkeypatch.setattr(edits, "editable", lambda _values: True)
    monkeypatch.setattr(edits, "_picture_track", lambda _values: Path("picture.mp4"))

    payload = edits.editor_payload(film, lambda path: f"/output/{Path(path).name}")

    assert payload["version"] == 0
    assert payload["film"]["seconds"] == 12.5
    assert payload["edits"]["music_id"] == "epic-1"
    assert payload["edits"]["sound_cues"] == [{"sound_id": "whoosh-1", "start_seconds": 3.0, "volume": 0.6}]
    assert payload["edits"]["filter"] == "original"
    assert payload["film"]["narration_url"] == "/output/narration.mp3"


def test_edit_renders_a_new_version_with_the_chosen_sound(tmp_path, library, renders):
    film = _film(tmp_path, library)
    choice = edits.FilmEdits(
        music_id="calm-1", music_volume=0.4, narration_volume=1.2, caption_style="off", filter="vintage", filter_strength=0.6,
        duck_music=False, fade_music=True, speed=1.5, trim_start=1.0, trim_end=11.0,
        sound_cues=[edits.SoundCue(sound_id="boing-1", start_seconds=9), edits.SoundCue(sound_id="whoosh-1", start_seconds=1, volume=1.0)],
        texts=[edits.TextItem(id="t1", text="RACE DAY!", start_seconds=2, end_seconds=5)],
    )

    result = edits.render_edit(film, choice)

    assert result["version"] == 1
    call = renders[0]
    assert call["state"]["music_file"] == str(library / "music/calm-1.mp3")
    assert [cue["sfx_id"] for cue in call["state"]["sound_cues"]] == ["whoosh-1", "boing-1"]
    assert call["grade"] == edits.FILTERS["vintage"]
    assert call["grade_strength"] == 0.6
    assert call["levels"] == {"music_volume": 0.4, "narration_volume": 1.2, "duck_under_narration": False, "music_fade": True}
    assert call["trim"] == (1.0, 11.0)
    assert call["speed"] == 1.5
    # Captions are off, so the only overlay is the text, shown for its own time span.
    assert [(start, end) for _, start, end in call["overlays"]] == [(2.0, 5.0)]
    # The editor reopens on the exported version, and the next export becomes version 2.
    current = edits.editor_payload(film, str) if edits.editable(film) else None
    assert current is None or current["version"] == 1
    assert edits._latest_edits(film)[0]["music_id"] == "calm-1"
    assert edits.render_edit(film, choice)["version"] == 2


def test_draft_is_saved_without_rendering_and_cleared_by_export(tmp_path, library, renders):
    film = _film(tmp_path, library)
    edits.save_draft(film, edits.FilmEdits(music_id="calm-1"))

    assert edits._latest_edits(film)[0]["music_id"] == "calm-1"
    assert renders == []
    edits.render_edit(film, edits.FilmEdits(music_id="epic-1"))
    assert edits._draft(film) is None
    assert edits._latest_edits(film)[0]["music_id"] == "epic-1"


def test_voiceovers_join_the_mix_and_silence_the_narrator(tmp_path, library, renders, monkeypatch):
    film = _film(tmp_path, library)
    folder = Path(film["video_file"]).parent / "edits"
    folder.mkdir(parents=True)
    (folder / "voice_0123456789ab.webm").write_bytes(b"voice")
    choice = edits.FilmEdits(voiceovers=[edits.Voiceover(id="0123456789ab", start_seconds=2, seconds=3)], clean_voice=True)

    edits.render_edit(film, choice)

    state = renders[0]["state"]
    assert state["voiceover_tracks"] == [{"file": str(folder / "voice_0123456789ab.webm"), "start_seconds": 2.0, "clean": True}]
    assert state["narration_mute_spans"] == [(2.0, 5.0)]


def test_missing_voiceover_is_rejected(tmp_path, library, renders):
    with pytest.raises(HTTPException) as error:
        edits.render_edit(_film(tmp_path, library), edits.FilmEdits(voiceovers=[edits.Voiceover(id="0123456789ab", start_seconds=0, seconds=2)]))
    assert "voiceover is missing" in error.value.detail


def test_saved_voiceover_is_measured(tmp_path, library, monkeypatch):
    film = _film(tmp_path, library)
    monkeypatch.setattr(edits, "editable", lambda _values: True)
    monkeypatch.setattr(edits, "_measure_seconds", lambda _path: 4.25)

    result = edits.save_voiceover(film, b"audio", "audio/webm;codecs=opus")

    assert result["seconds"] == 4.25
    assert Path(result["file"]).suffix == ".webm"
    with pytest.raises(HTTPException) as error:
        edits.save_voiceover(film, b"audio", "video/mp4")
    assert error.value.status_code == 415


def test_finished_films_are_remembered_newest_first():
    assert edits.remember_films("user_1", ["a", "b"]) == ["a", "b"]
    assert edits.remember_films("user_1", ["c", "a"]) == ["c", "a", "b"]
    assert edits.remember_films("user_2", []) == []


def test_no_music_and_original_grade(tmp_path, library, renders):
    edits.render_edit(_film(tmp_path, library), edits.FilmEdits(music_id=None))

    assert renders[0]["state"]["music_file"] is None
    assert renders[0]["grade"] is None
    assert renders[0]["speed"] == 1.0


@pytest.mark.parametrize(("choice", "message"), [
    (edits.FilmEdits(music_id="whoosh-1"), "Unknown music track"),
    (edits.FilmEdits(sound_cues=[edits.SoundCue(sound_id="epic-1", start_seconds=1)]), "Unknown sound effect"),
    (edits.FilmEdits(sound_cues=[edits.SoundCue(sound_id="whoosh-1", start_seconds=12.5)]), "before the film ends"),
    (edits.FilmEdits(speed=3.0), "Unsupported speed"),
    (edits.FilmEdits(trim_start=5, trim_end=5.5), "at least one second"),
])
def test_invalid_edits_are_rejected(tmp_path, library, renders, choice, message):
    with pytest.raises(HTTPException) as error:
        edits.render_edit(_film(tmp_path, library), choice)
    assert message in error.value.detail
    assert renders == []


def test_failed_render_frees_its_version(tmp_path, library, monkeypatch):
    film = _film(tmp_path, library)
    monkeypatch.setattr(edits, "remix_video", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ffmpeg failed")))

    with pytest.raises(RuntimeError):
        edits.render_edit(film, edits.FilmEdits())
    assert list((Path(film["video_file"]).parent / "edits").glob("*.json")) == []


def test_render_in_progress_is_not_reopened(tmp_path, library):
    film = _film(tmp_path, library)
    folder = Path(film["video_file"]).parent / "edits"
    folder.mkdir(parents=True)
    (folder / "edit_v001.json").write_text("{}")

    assert edits._finished_versions(film) == []


def test_only_finished_films_can_be_edited(tmp_path, library, renders):
    with pytest.raises(HTTPException) as error:
        edits.render_edit({**_film(tmp_path, library), "video_file": None}, edits.FilmEdits())
    assert error.value.status_code == 409


def test_film_length_follows_the_edit_timeline(tmp_path, library):
    film = {**_film(tmp_path, library), "edit_timeline": [{"timeline_end": 4.0}, {"timeline_end": 9.75}]}

    assert edits.film_seconds(film) == 9.75


def test_caption_words_use_the_narration_timing_when_it_matches():
    from video_automation.edit_overlays import caption_words

    subtitle = {"text": "Slow and steady", "start_seconds": 1.0, "end_seconds": 2.5}
    aligned = [
        {"text": "Slow", "start_seconds": 1.0, "end_seconds": 1.3}, {"text": "and", "start_seconds": 1.35, "end_seconds": 1.5},
        {"text": "steady", "start_seconds": 1.6, "end_seconds": 2.4}, {"text": "wins", "start_seconds": 2.6, "end_seconds": 2.9},
    ]
    assert [word["start_seconds"] for word in caption_words(subtitle, aligned)] == [1.0, 1.35, 1.6]
    # Without matching timing, the words share the caption's time evenly.
    assert [word["start_seconds"] for word in caption_words(subtitle, [])] == [1.0, 1.5, 2.0]


@pytest.mark.parametrize("style", ["pop", "karaoke", "boxed", "minimal", "serif"])
def test_every_caption_style_draws(tmp_path, style):
    from video_automation.edit_overlays import caption_overlays

    overlays = caption_overlays(
        [{"text": "Slow and steady wins the race", "start_seconds": 0.0, "end_seconds": 3.0}], [], style, "bottom", 540, 960, tmp_path,
    )
    expected = 6 if style in {"pop", "karaoke"} else 1
    assert len(overlays) == expected
    assert all(path.is_file() for path, _, _ in overlays)
    assert overlays[0][1] == 0.0 and overlays[-1][2] == 3.0


@pytest.mark.parametrize("style", ["highlight", "bold", "serif", "outline", "typewriter", "label"])
def test_every_text_style_draws(tmp_path, style):
    from PIL import Image

    from video_automation.edit_overlays import text_overlays

    [(path, start, end)] = text_overlays(
        [{"text": "RACE DAY!", "style": style, "color": "#ffd23f", "x": 0.5, "y": 0.2, "size": 1.0, "start_seconds": 1, "end_seconds": 4}],
        540, 960, tmp_path,
    )
    assert (start, end) == (1, 4)
    # Something is drawn around the text's position and nothing far from it.
    image = Image.open(path)
    assert image.getpixel((270, 192))[3] > 0 or image.crop((150, 150, 390, 240)).getbbox()
    assert image.getpixel((10, 900))[3] == 0
