"""relay.render: the narration pipeline with an injected synthesizer and a tmp out root (offline, no key)."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import pytest
import requests

import relay.render as render_mod
from relay.config import ROOT, Settings
from relay.render import (RenderReport, api_words_ms, build_rendered, chunk_json, clean_word, main, render_talk,
                          summary_lines, tag_word_names)
from relay.talk import Chunk, Talk
from relay.textutil import leading_tags, split_sentences
from relay.timeline import (RenderedChunk, chunk_hash, chunk_paths, load_rendered, scale_timing, synthetic_timing,
                            talk_out_dir)
from relay.tts import CREDITS_URL, TTSError, TTSResult, fake_synthesize, pcm_to_wav, wav_duration_ms

TEST_TALK = ROOT / "talks" / "test"
KEYS = {"slide", "index", "say", "text", "words", "sentences", "duration_ms", "hash", "voice", "fake", "estimated"}
CHUNKS = [("how", 0), ("how", 1), ("how", 2), ("qa", 0)]
LABELS = [f"{slide}/{n}" for slide, n in CHUNKS]
HOW0 = "<|emotion:enthusiasm|>Here is how this works."


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every synth here is injected or fake; a real TTS request would be a bug in the test (or the render)."""
    def refuse(*args, **kwargs):
        raise AssertionError("tests must never touch the network")

    monkeypatch.setattr(requests.sessions.Session, "request", refuse)


def api_like(say: str, voice: str) -> TTSResult:
    """What TTS 3 plausibly returns: the fake wav, word timings in seconds, leading tags glued to the first word."""
    result = fake_synthesize(say, voice)
    words = [dict(word) for word in result.words or []]
    lead = "".join(tag.raw for tag in leading_tags(say))
    if lead and words:
        words[0]["word"] = lead + words[0]["word"]
    return TTSResult(result.wav, words)


class Synth:
    """Thread-safe recording synthesizer. `fail` maps a substring of `say` to the exception to raise for it."""

    def __init__(self, fn: Callable[[str, str], TTSResult] = api_like, fail: dict[str, Exception] | None = None):
        self.fn = fn
        self.fail = fail or {}
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, say: str, voice: str) -> TTSResult:
        with self._lock:
            self.calls.append((say, voice))
        for needle, exc in self.fail.items():
            if needle in say:
                raise exc
        return self.fn(say, voice)


def render(talk: Talk, out: Path, **kwargs: Any) -> RenderReport:
    kwargs.setdefault("synth", Synth())
    return render_talk(talk, Settings(), out_root=out, **kwargs)


def labels(entries) -> list[str]:
    return [entry.label for entry in entries]


def meta(out: Path, slide: str, n: int) -> dict:
    return json.loads(chunk_paths("test", slide, n, out)[1].read_text(encoding="utf-8"))


def stamps(out: Path) -> dict[str, int]:
    """mtime_ns of every rendered file, to prove a cached run wrote nothing."""
    return {str(path): path.stat().st_mtime_ns for path in sorted(talk_out_dir("test", out).rglob("*.*"))}


def wav_seconds(seconds: float) -> bytes:
    return pcm_to_wav(bytes(2 * round(24_000 * seconds)))


def result_with(words: list | None, seconds: float = 3.0) -> TTSResult:
    return TTSResult(wav_seconds(seconds), words)


def chunk(say: str = HOW0, index: int = 0) -> Chunk:
    return Chunk(index=index, say=say)


def copy_talk(tmp_path: Path) -> Path:
    """A writable copy of talks/test (the folder name must stay `test`)."""
    dest = tmp_path / "talks" / "test"
    shutil.copytree(TEST_TALK, dest)
    return dest


# --- files and JSON ------------------------------------------------------------------------------------------------

def test_renders_every_relay_chunk_to_wav_and_design_json(test_talk, tmp_path):
    synth = Synth()
    report = render(test_talk, tmp_path, synth=synth)
    assert report.ok and report.fatal is None
    assert labels(report.rendered) == LABELS  # deck order, whatever order the threads finished in
    assert report.cached == report.failed == report.skipped == []
    assert sorted(synth.calls) == sorted((test_talk.chunk(s, n).say, "chloe") for s, n in CHUNKS)
    total = 0
    for slide_id, n in CHUNKS:
        source = test_talk.chunk(slide_id, n)
        wav, meta_path = chunk_paths("test", slide_id, n, tmp_path)
        assert wav.read_bytes() == fake_synthesize(source.say, "chloe").wav  # the wav as returned
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        assert set(data) == KEYS
        assert (data["slide"], data["index"], data["say"], data["text"]) == (slide_id, n, source.say, source.text)
        assert data["sentences"] == split_sentences(source.text)
        assert data["hash"] == chunk_hash(source.say, "chloe") and data["voice"] == "chloe"
        assert data["fake"] is False and data["estimated"] is False
        assert data["duration_ms"] == wav_duration_ms(wav.read_bytes())
        expected, _ = synthetic_timing(source.text)
        assert [(w["word"], w["start"], w["end"], w["sentence"]) for w in data["words"]] == \
            [(w.word, w.start, w.end, w.sentence) for w in expected]
        for word in data["words"]:
            assert set(word) == {"word", "start", "end", "sentence"}
            assert all(type(word[key]) is int for key in ("start", "end", "sentence"))
            assert 0 <= word["start"] <= word["end"] <= data["duration_ms"]
            assert 0 <= word["sentence"] < len(data["sentences"])
        total += data["duration_ms"]
    assert report.total_ms == total
    loaded = load_rendered(test_talk, tmp_path)  # what the server does
    assert sorted(loaded) == CHUNKS
    assert loaded[("how", 0)].words[0].word == "Here"


def test_json_is_one_word_per_line(test_talk, tmp_path):
    render(test_talk, tmp_path)
    text = chunk_paths("test", "how", 0, tmp_path)[1].read_text(encoding="utf-8")
    assert '  {"word": "Here", "start": 150, "end": 450, "sentence": 0},' in text.splitlines()
    assert text.endswith("}\n")


def test_chunk_json_round_trips():
    rendered = RenderedChunk("how", 0, "<|emotion:awe|>Wow “quotes” é.", "Wow “quotes” é.", [], ["Wow “quotes” é."],
                             1000, "abc", "chloe")
    for data in (rendered.to_json(), {**rendered.to_json(), "words": [{"word": "a", "start": 1, "end": 2,
                                                                       "sentence": 0}] * 3}):
        assert json.loads(chunk_json(data)) == data
    assert "é" in chunk_json(rendered.to_json())  # readable, not \u-escaped


def test_atomic_writes_leave_no_temp_files(test_talk, tmp_path):
    render(test_talk, tmp_path)
    names = sorted(path.name for path in talk_out_dir("test", tmp_path).rglob("*") if path.is_file())
    assert names == ["0.json", "0.json", "0.wav", "0.wav", "1.json", "1.wav", "2.json", "2.wav"]


def test_failed_rename_cleans_up_and_keeps_the_old_file(tmp_path, monkeypatch):
    target = tmp_path / "0.json"
    target.write_text("old")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(render_mod.os, "replace", broken_replace)
    with pytest.raises(OSError):
        render_mod._write_atomic(target, b"new")
    assert target.read_text() == "old"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["0.json"]


def test_jobs_do_not_change_the_output(test_talk, tmp_path):
    render(test_talk, tmp_path / "one", jobs=1)
    render(test_talk, tmp_path / "many", jobs=8)
    for slide_id, n in CHUNKS:
        for one, many in zip(chunk_paths("test", slide_id, n, tmp_path / "one"),
                             chunk_paths("test", slide_id, n, tmp_path / "many")):
            assert one.read_bytes() == many.read_bytes()


def test_progress_lines(test_talk, tmp_path):
    lines: list[str] = []
    render(test_talk, tmp_path, progress=lines.append)
    assert sorted(line.split()[1] for line in lines) == sorted(LABELS)
    assert all(line.strip().startswith("rendered") and line.endswith(" s") for line in lines)


# --- cache ---------------------------------------------------------------------------------------------------------

def test_second_run_is_all_cached_and_writes_nothing(test_talk, tmp_path):
    render(test_talk, tmp_path)
    before = stamps(tmp_path)
    synth = Synth()
    report = render(test_talk, tmp_path, synth=synth)
    assert synth.calls == []
    assert labels(report.cached) == LABELS and report.rendered == []
    assert report.ok and report.warnings == []
    assert stamps(tmp_path) == before
    assert report.total_ms == sum(meta(tmp_path, s, n)["duration_ms"] for s, n in CHUNKS)


def test_force_renders_everything_again(test_talk, tmp_path):
    render(test_talk, tmp_path)
    synth = Synth()
    report = render(test_talk, tmp_path, synth=synth, force=True)
    assert len(synth.calls) == 4 and labels(report.rendered) == LABELS and report.cached == []


def test_fake_render_is_replaced_by_a_real_render(test_talk, tmp_path):
    report = render(test_talk, tmp_path, fake=True, synth=None)  # default synth for --fake: fake_synthesize
    assert labels(report.rendered) == LABELS
    assert all(meta(tmp_path, s, n)["fake"] is True for s, n in CHUNKS)
    synth = Synth()
    report = render(test_talk, tmp_path, synth=synth)
    assert len(synth.calls) == 4 and labels(report.rendered) == LABELS
    assert all(meta(tmp_path, s, n)["fake"] is False for s, n in CHUNKS)


def test_fake_run_keeps_real_audio_unless_forced(test_talk, tmp_path):
    render(test_talk, tmp_path)
    synth = Synth(fake_synthesize)
    report = render(test_talk, tmp_path, fake=True, synth=synth)
    assert synth.calls == [] and labels(report.cached) == LABELS
    assert any("kept the real TTS 3 audio of 4 chunk(s)" in warning for warning in report.warnings)
    assert all(meta(tmp_path, s, n)["fake"] is False for s, n in CHUNKS)
    report = render(test_talk, tmp_path, fake=True, force=True, synth=synth)
    assert labels(report.rendered) == LABELS
    assert all(meta(tmp_path, s, n)["fake"] is True for s, n in CHUNKS)


def test_fake_cache_hits_for_another_fake_run(test_talk, tmp_path):
    render(test_talk, tmp_path, fake=True, synth=None)
    report = render(test_talk, tmp_path, fake=True, synth=None)
    assert labels(report.cached) == LABELS and report.warnings == []


def test_changed_text_rerenders_only_that_chunk(test_talk, tmp_path):
    render(test_talk, tmp_path)
    test_talk.slide("how").chunks[1].say = "Every word has a time stamp."
    synth = Synth()
    report = render(test_talk, tmp_path, synth=synth)
    assert synth.calls == [("Every word has a time stamp.", "chloe")]
    assert labels(report.rendered) == ["how/1"] and labels(report.cached) == ["how/0", "how/2", "qa/0"]
    assert meta(tmp_path, "how", 1)["text"] == "Every word has a time stamp."


def test_changed_voice_rerenders_everything(test_talk, tmp_path):
    render(test_talk, tmp_path)
    test_talk.voice = "marcus"
    synth = Synth()
    report = render(test_talk, tmp_path, synth=synth)
    assert {voice for _, voice in synth.calls} == {"marcus"} and labels(report.rendered) == LABELS
    assert all(meta(tmp_path, s, n)["voice"] == "marcus" for s, n in CHUNKS)


@pytest.mark.parametrize("damage", ["delete wav", "empty wav", "delete json", "bad json", "json list",
                                    "wrong index", "missing field"])
def test_damaged_chunks_are_rendered_again(test_talk, tmp_path, damage):
    render(test_talk, tmp_path)
    wav, meta_path = chunk_paths("test", "how", 2, tmp_path)
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    if damage == "delete wav":
        wav.unlink()
    elif damage == "empty wav":
        wav.write_bytes(b"")
    elif damage == "delete json":
        meta_path.unlink()
    elif damage == "bad json":
        meta_path.write_text("{", encoding="utf-8")
    elif damage == "json list":
        meta_path.write_text("[]", encoding="utf-8")
    elif damage == "wrong index":
        meta_path.write_text(json.dumps({**data, "index": 1}), encoding="utf-8")
    else:
        meta_path.write_text(json.dumps({key: value for key, value in data.items() if key != "duration_ms"}))
    synth = Synth()
    report = render(test_talk, tmp_path, synth=synth)
    assert labels(report.rendered) == ["how/2"] and len(report.cached) == 3
    assert meta(tmp_path, "how", 2) == data and wav.stat().st_size > 0


def test_a_crash_after_the_wav_never_leaves_an_old_json_vouching_for_new_audio(test_talk, tmp_path, monkeypatch):
    render(test_talk, tmp_path)  # real audio
    write = render_mod._write_atomic

    def crash_on_json(path: Path, data: bytes) -> None:
        if path.suffix == ".json":
            raise OSError("killed")
        write(path, data)

    monkeypatch.setattr(render_mod, "_write_atomic", crash_on_json)
    report = render(test_talk, tmp_path, only="qa", fake=True, force=True, synth=None)
    assert labels(report.failed) == ["qa/0"] and "killed" in report.failed[0].error
    monkeypatch.setattr(render_mod, "_write_atomic", write)
    synth = Synth()
    report = render(test_talk, tmp_path, synth=synth)  # the placeholder wav must not count as the real render
    assert labels(report.rendered) == ["qa/0"] and meta(tmp_path, "qa", 0)["fake"] is False


def test_everything_cached_needs_no_key(test_talk, tmp_path):
    render(test_talk, tmp_path)
    report = render_talk(test_talk, Settings(), out_root=tmp_path)  # no key, default synth, nothing to do
    assert report.ok and labels(report.cached) == LABELS


# --- --only and leftovers ------------------------------------------------------------------------------------------

def test_only_renders_one_slide(test_talk, tmp_path):
    synth = Synth()
    report = render(test_talk, tmp_path, only="qa", synth=synth)
    assert labels(report.rendered) == ["qa/0"] and len(synth.calls) == 1
    assert not (talk_out_dir("test", tmp_path) / "how").exists()


@pytest.mark.parametrize("only", ["title", "nope", ""])
def test_only_must_name_a_relay_slide(test_talk, tmp_path, only):
    with pytest.raises(ValueError, match="relay slides: how, qa"):
        render(test_talk, tmp_path, only=only)


def test_leftover_chunk_files_are_removed_and_nothing_else(test_talk, tmp_path):
    out = talk_out_dir("test", tmp_path)
    leftovers = ["how/3.wav", "how/3.json", "how/10.wav", "qa/1.json"]
    kept = ["how/notes.txt", "how/3.json.bak", "how/x3.wav", "how/3.txt", "how/-1.wav", "qa/1.5.wav"]
    elsewhere = ["title/0.wav", "../other/how/5.wav", "../../9.wav"]
    for name in leftovers + kept + elsewhere:
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    (out / "how" / "4.wav").mkdir()  # a folder with a chunk-like name is never touched
    report = render(test_talk, tmp_path)
    assert report.ok
    assert sorted(report.removed) == sorted(out / name for name in leftovers)
    assert not any((out / name).exists() for name in leftovers)
    assert all((out / name).exists() for name in kept + elsewhere)
    assert (out / "how" / "4.wav").is_dir()
    assert any("title/ belongs to no relay slide" in warning for warning in report.warnings)


def test_only_cleans_only_its_own_slide(test_talk, tmp_path):
    out = talk_out_dir("test", tmp_path)
    for name in ("how/7.wav", "qa/7.wav", "title/0.wav"):
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(b"x")
    report = render(test_talk, tmp_path, only="qa")
    assert report.removed == [out / "qa" / "7.wav"]
    assert (out / "how" / "7.wav").exists()
    assert not any("belongs to no relay slide" in warning for warning in report.warnings)


def test_leftovers_go_when_every_chunk_is_cached(test_talk, tmp_path):
    render(test_talk, tmp_path)
    stale = chunk_paths("test", "how", 3, tmp_path)
    for path in stale:
        path.write_bytes(b"x")
    report = render(test_talk, tmp_path)
    assert report.rendered == [] and sorted(report.removed) == sorted(stale)


def test_a_symlinked_slide_folder_is_never_cleaned(test_talk, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "7.wav").write_bytes(b"precious")
    out = talk_out_dir("test", tmp_path / "out")
    out.mkdir(parents=True)
    (out / "how").symlink_to(elsewhere, target_is_directory=True)
    report = render(test_talk, tmp_path / "out")
    assert report.removed == [] and (elsewhere / "7.wav").read_bytes() == b"precious"


@pytest.mark.parametrize("bad_id", ["../evil", "a/b", "..", "."])
def test_unsafe_slide_ids_never_become_paths(test_talk, tmp_path, bad_id):
    test_talk.slide("qa").id = bad_id
    out = tmp_path / "deep" / "out"
    report = render(test_talk, out)
    assert [entry.slide for entry in report.failed] == [bad_id]
    assert "not a safe folder name" in report.failed[0].error and not report.ok
    assert labels(report.rendered) == ["how/0", "how/1", "how/2"]
    written = {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()}
    assert all(str(path).startswith(os.path.join("deep", "out", "talks", "test", "how")) for path in written)


# --- TTS problems --------------------------------------------------------------------------------------------------

def test_fatal_error_stops_the_render(test_talk, tmp_path):
    error = TTSError("Invalid API key", status=401, type="authentication_error", fatal=True)
    synth = Synth(fail={"Every single": error})
    report = render(test_talk, tmp_path, synth=synth, jobs=1)  # one worker: chunks go in deck order
    assert report.fatal is error and not report.ok
    assert labels(report.rendered) == ["how/0"]
    assert labels(report.failed) == ["how/1"] and "check BOSON_API_KEY" in report.failed[0].error
    assert labels(report.skipped) == ["how/2", "qa/0"]
    assert len(synth.calls) == 2
    assert not chunk_paths("test", "how", 2, tmp_path)[0].exists()


def test_transient_error_fails_only_its_chunk(test_talk, tmp_path):
    synth = Synth(fail={"Every single": TTSError("overloaded", status=503, type="api_error")})
    report = render(test_talk, tmp_path, synth=synth)
    assert report.fatal is None and not report.ok
    assert labels(report.failed) == ["how/1"] and labels(report.rendered) == ["how/0", "how/2", "qa/0"]
    synth = Synth()
    report = render(test_talk, tmp_path, synth=synth)  # the re-run only does what is missing
    assert labels(report.rendered) == ["how/1"] and len(synth.calls) == 1


def test_unexpected_exceptions_fail_the_chunk_not_the_run(test_talk, tmp_path):
    report = render(test_talk, tmp_path, synth=Synth(fail={"whole pipeline": RuntimeError("surprise")}))
    assert labels(report.failed) == ["qa/0"] and report.failed[0].error == "RuntimeError: surprise"
    assert report.fatal is None and len(report.rendered) == 3


def test_empty_audio_fails_the_chunk(test_talk, tmp_path):
    report = render(test_talk, tmp_path, only="qa", synth=Synth(lambda say, voice: TTSResult(b"", None)))
    assert labels(report.failed) == ["qa/0"] and "empty" in report.failed[0].error
    assert not any(path.exists() for path in chunk_paths("test", "qa", 0, tmp_path))


def test_empty_word_list_means_no_speech_and_fails_the_chunk():
    # The API sends `timestamps: []` when its own audio has no speech to align (null = alignment unavailable)
    with pytest.raises(TTSError) as info:
        build_rendered("how", chunk(), "chloe", result_with([], 2.5), fake=False)
    error = info.value
    assert error.type == "no_speech" and not error.fatal
    assert str(error).startswith("TTS 3: no speech in the audio") and "re-run" in str(error)


def test_empty_word_list_is_fine_when_the_chunk_has_no_words():
    rendered, warnings = build_rendered("how", chunk("…"), "chloe", result_with([], 1.0), fake=False)
    assert rendered.words == [] and rendered.estimated and rendered.duration_ms == 1000


def test_no_speech_chunk_is_not_written_and_a_rerun_retries_it(capsys, test_talk, tmp_path):
    tries: list[str] = []

    def no_speech_once(say: str, voice: str) -> TTSResult:
        if "Every single word" in say:
            tries.append(say)
            if len(tries) == 1:
                return TTSResult(wav_seconds(6.0), [])
        return api_like(say, voice)

    synth = Synth(no_speech_once)
    code, out = run_main(capsys, str(TEST_TALK), "--out", str(tmp_path), synth=synth)
    assert code == 2
    assert "FAILED   how/1" in out and "no speech in the audio" in out
    assert "1 chunk(s) not rendered: re-run to retry them" in out
    assert not any(path.exists() for path in chunk_paths("test", "how", 1, tmp_path))
    report = render(test_talk, tmp_path, synth=synth)  # a plain re-run asks TTS 3 again for that chunk only
    assert report.ok and labels(report.rendered) == ["how/1"]
    assert labels(report.cached) == ["how/0", "how/2", "qa/0"]
    assert len(tries) == 2 and meta(tmp_path, "how", 1)["estimated"] is False


def test_no_speech_on_a_forced_rerender_keeps_the_good_render(test_talk, tmp_path):
    render(test_talk, tmp_path)
    before = stamps(tmp_path)
    silent = Synth(lambda say, voice: TTSResult(wav_seconds(2.0), []))
    report = render(test_talk, tmp_path, only="qa", force=True, synth=silent)
    assert labels(report.failed) == ["qa/0"] and not report.ok
    assert stamps(tmp_path) == before  # nothing written, nothing deleted
    assert sorted(load_rendered(test_talk, tmp_path)) == CHUNKS  # the talk still serves


def test_no_key_and_not_fake_is_fatal_before_any_request(test_talk, tmp_path):
    report = render_talk(test_talk, Settings(), out_root=tmp_path)
    assert report.fatal is not None and report.fatal.fatal and report.fatal.type == "missing_api_key"
    assert labels(report.skipped) == LABELS
    assert "BOSON_API_KEY" in str(report.fatal) and "--fake" in str(report.fatal)
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


# --- word timings --------------------------------------------------------------------------------------------------

def test_seconds_become_int_ms_and_tag_text_is_stripped():
    words = [
        {"word": "<|emotion:enthusiasm|>", "start": 0.0, "end": 0.0},
        {"word": "<|emotion:", "start": 0.0, "end": 0.01},
        {"word": "enthusiasm|>Here", "start": 0.1204, "end": 0.3},
        {"word": "is", "start": 0.35, "end": 0.5},
        {"word": "…", "start": 0.5, "end": 0.6},
        {"word": "", "start": 0.6, "end": 0.6},
        {"word": "how<|prosody:pause|>", "start": 0.6, "end": 0.9},
        {"word": "this", "start": 1.2, "end": 1.4},
        {"word": "works.", "start": 1.5, "end": 1.9},
    ]
    rendered, warnings = build_rendered("how", chunk(), "chloe", result_with(words), fake=False)
    assert [(w.word, w.start, w.end, w.sentence) for w in rendered.words] == [
        ("Here", 120, 300, 0), ("is", 350, 500, 0), ("how", 600, 900, 0), ("this", 1200, 1400, 0),
        ("works.", 1500, 1900, 0)]
    assert rendered.duration_ms == 3000 and not rendered.estimated and warnings == []
    assert rendered.text == "Here is how this works." and rendered.say == HOW0
    assert rendered.hash == chunk_hash(HOW0, "chloe") and rendered.voice == "chloe" and rendered.fake is False


def test_values_that_already_look_like_ms_are_kept():
    words = [{"word": "Here", "start": 150, "end": 450}, {"word": "is", "start": 540.4, "end": 750.6}]
    rendered, _ = build_rendered("how", chunk("Here is."), "chloe", result_with(words, 2.0), fake=False)
    assert [(w.start, w.end) for w in rendered.words] == [(150, 450), (540, 751)]


@pytest.mark.parametrize("largest,expected_ms", [(10.0, 2000), (10.5, 10)])
def test_ms_detection_threshold(largest, expected_ms):
    # 2 s of audio: anything up to 5 × 2 = 10 is read as seconds (then clamped), anything above as ms
    assert api_words_ms([{"word": "a", "start": largest, "end": largest}], 2000) == [("a", expected_ms, expected_ms)]


def test_api_words_ms_cleans_up_bad_entries():
    raw = [
        "not a dict", {"start": 0.1}, {"word": None, "start": 0.1}, {"word": "a", "start": None},
        {"word": "b", "start": "soon"}, {"word": "c", "start": float("nan")}, {"word": "d", "start": True},
        {"word": "e", "start": "0.2", "end": "0.4"},  # numeric strings are fine
        {"word": "f", "start": -0.5, "end": 0.1},  # negative → 0, but starts never go back
        {"word": "g", "start": 0.3},  # no end → ends where it starts
        {"word": "h", "start": 0.25, "end": 0.2},  # start before the previous start, end before start
        {"word": "i", "start": 3.0, "end": 3.5},  # past the 1 s of audio (but < 5 × 1 s, so still seconds) → clamped
        {"word": "j", "start": float("inf"), "end": 1},
    ]
    assert api_words_ms(raw, 1000) == [("e", 200, 400), ("f", 200, 200), ("g", 300, 300), ("h", 300, 300),
                                       ("i", 1000, 1000)]
    assert api_words_ms(None, 1000) == [] and api_words_ms([], 1000) == []


def test_api_words_ms_drops_bare_tag_names():
    raw = [{"word": "emotion:enthusiasm", "start": 0.0, "end": 0.1}, {"word": "Here", "start": 0.1, "end": 0.3}]
    assert api_words_ms(raw, 1000, drop=frozenset({"emotionenthusiasm"})) == [("Here", 100, 300)]


@pytest.mark.parametrize("raw,expected", [
    ("<|emotion:enthusiasm|>Here", "Here"), ("<|emotion:", ""), ("enthusiasm|>Here", "Here"),
    ("how<|prosody:pause|>", "how"), ("Here<|", "Here"), ("|>", ""), ("works.", "works."), ("—", ""),
    ("...", ""), ("", ""), ("  spaced  ", "spaced"), ("<|prosody:pause|>", ""), ("<|sfx:laughter|>Haha", "Haha"),
    (None, ""), (5, ""), ("you’ve", "you’ve"),
])
def test_clean_word(raw, expected):
    assert clean_word(raw) == expected


def test_tag_word_names():
    assert tag_word_names(HOW0) == {"emotion", "enthusiasm", "emotionenthusiasm"}
    assert tag_word_names("<|prosody:long_pause|>Wait.") == {"prosody", "longpause", "prosodylongpause", "long",
                                                             "pause"}
    assert "pause" not in tag_word_names("Let me pause here. <|prosody:pause|> Okay.")
    assert tag_word_names("No tags at all.") == frozenset()


def test_bare_tag_words_are_dropped_with_a_warning():
    words = [{"word": "emotion:enthusiasm", "start": 0.0, "end": 0.1}, {"word": "Here", "start": 0.1, "end": 0.3},
             {"word": "is", "start": 0.35, "end": 0.5}, {"word": "how", "start": 0.6, "end": 0.9},
             {"word": "this", "start": 1.0, "end": 1.2}, {"word": "works.", "start": 1.3, "end": 1.6}]
    rendered, warnings = build_rendered("how", chunk(), "chloe", result_with(words), fake=False)
    assert [w.word for w in rendered.words] == ["Here", "is", "how", "this", "works."]
    assert warnings == ["how/0: dropped tag text from the TTS word list: emotion:enthusiasm"]


def test_a_spoken_word_that_matches_a_tag_value_is_kept():
    say = "<|emotion:enthusiasm|>I have enthusiasm."
    words = [{"word": w, "start": 0.2 * k, "end": 0.2 * k + 0.1} for k, w in enumerate(["I", "have", "enthusiasm."])]
    rendered, warnings = build_rendered("how", chunk(say), "chloe", result_with(words), fake=False)
    assert [w.word for w in rendered.words] == ["I", "have", "enthusiasm."] and warnings == []


def test_sentence_index_comes_from_alignment_even_when_tts_splits_words():
    say = "It runs in real-time. Then it stops."
    words = [{"word": w, "start": 0.3 * k, "end": 0.3 * k + 0.2} for k, w in
             enumerate(["It", "runs", "in", "real", "time.", "Then", "it", "stops."])]
    rendered, _ = build_rendered("how", chunk(say), "chloe", result_with(words), fake=False)
    assert [w.sentence for w in rendered.words] == [0, 0, 0, 0, 0, 1, 1, 1]
    assert rendered.sentences == ["It runs in real-time.", "Then it stops."]


@pytest.mark.parametrize("words,reason", [
    (None, "no word timestamps (null)"),
    ([{"word": "<|emotion:enthusiasm|>", "start": 0.1, "end": 0.2}, {"word": "...", "start": 0.3}],
     "no usable word timestamps"),
    ([{"word": w, "start": 0, "end": 0} for w in ["Here", "is", "how", "this", "works."]], "every TTS 3 word starts"),
])
def test_unusable_timestamps_fall_back_to_estimated_timings(words, reason):
    rendered, warnings = build_rendered("how", chunk(), "chloe", result_with(words, 2.5), fake=False)
    synthetic, synthetic_ms = synthetic_timing("Here is how this works.")
    assert rendered.estimated is True and rendered.duration_ms == 2500
    assert rendered.words == scale_timing(synthetic, synthetic_ms, 2500)
    assert rendered.words[-1].end <= 2500
    assert len(warnings) == 1 and reason in warnings[0] and "estimated timings" in warnings[0]
    assert warnings[0].startswith("how/0: ")


def test_estimated_fallback_end_to_end(test_talk, tmp_path):
    synth = Synth(lambda say, voice: TTSResult(fake_synthesize(say, voice).wav, None))
    report = render(test_talk, tmp_path, synth=synth)
    assert report.ok and all(entry.estimated for entry in report.rendered)
    assert sum("estimated timings" in warning for warning in report.warnings) == 4
    assert all(meta(tmp_path, s, n)["estimated"] is True for s, n in CHUNKS)
    report = render(test_talk, tmp_path)
    assert all(entry.estimated for entry in report.cached)  # the flag survives the cache
    # ... and a later run says so again, instead of listing them as plain cached chunks
    assert len(report.warnings) == 1
    assert report.warnings[0].startswith("4 cached chunk(s) have estimated word timings (how/0, how/1, how/2, qa/0): "
                                         "captions and cut points are approximate")
    assert "--force --only" in report.warnings[0]


def test_word_count_mismatch_and_late_words_warn():
    say = "One two three four five six seven eight nine ten."
    words = [{"word": "One", "start": 0.1, "end": 0.2}, {"word": "two", "start": 5.0, "end": 5.5}]
    rendered, warnings = build_rendered("how", chunk(say), "chloe", result_with(words, 3.0), fake=False)
    assert [(w.start, w.end) for w in rendered.words] == [(100, 200), (3000, 3000)]
    assert "how/0: TTS 3 timed 2 words for 10 in the text: check the captions" in warnings
    assert "how/0: 1 word(s) start after the audio ends (3000 ms): clamped" in warnings


def _with_data_size(wav: bytes, size: int) -> bytes:
    at = wav.index(b"data")
    return wav[:at + 4] + struct.pack("<I", size) + wav[at + 8:]


def float_wav(seconds: float, rate: int = 24_000) -> bytes:
    """IEEE float32 mono wav (format 3 + a fact chunk): the layout scipy, soundfile and torchaudio write."""
    frames = round(rate * seconds)
    fmt = struct.pack("<HHIIHHH", 3, 1, rate, rate * 4, 4, 32, 0)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"fact" + struct.pack("<II", 4, frames)
            + b"data" + struct.pack("<I", 4 * frames) + bytes(4 * frames))
    return b"RIFF" + struct.pack("<I", len(body)) + body


THREE_SENTENCES = ("Ken holds the floor control. One tap stops playback within a tenth of a second. "
                   "Then the microphone opens.")


def test_float_wav_gets_its_real_duration_and_estimates_stay_inside_it():
    # was 6000 ms (the size guess assumed PCM16): estimated words ran past the audio, so cuts resumed mid-sentence
    rendered, warnings = build_rendered("how", chunk(THREE_SENTENCES), "chloe", TTSResult(float_wav(3.0), None),
                                        fake=False)
    assert rendered.duration_ms == 3000 and rendered.estimated
    assert not any("wav header" in warning for warning in warnings)
    synthetic, synthetic_ms = synthetic_timing(rendered.text)
    assert rendered.words == scale_timing(synthetic, synthetic_ms, 3000)
    assert rendered.words[-1].end <= 3000


def test_words_past_the_end_of_a_float_wav_are_clamped():
    words = [{"word": "One", "start": 0.1, "end": 0.5}, {"word": "two.", "start": 3.5, "end": 3.9}]
    rendered, warnings = build_rendered("how", chunk("One two."), "chloe", TTSResult(float_wav(3.0), words),
                                        fake=False)
    assert [(w.start, w.end) for w in rendered.words] == [(100, 500), (3000, 3000)]
    assert "how/0: 1 word(s) start after the audio ends (3000 ms): clamped" in warnings


@pytest.mark.parametrize("size", [0xFFFFFFFF, 0])
def test_streamed_wav_header_gives_the_real_duration_without_a_warning(size):
    wav = _with_data_size(wav_seconds(1.0), size)
    rendered, warnings = build_rendered("how", chunk(), "chloe", TTSResult(wav, None), fake=False)
    assert rendered.duration_ms == 1000
    assert not any("wav header" in warning for warning in warnings)


def test_unreadable_wav_header_guesses_the_duration_from_the_size_and_warns():
    rendered, warnings = build_rendered("how", chunk(), "chloe", TTSResult(b"ID3" + bytes(48_041), None), fake=False)
    assert rendered.duration_ms == 1000
    assert warnings[0] == ("how/0: unreadable wav header (not a RIFF/WAVE file); duration guessed from the file size "
                           "as 16-bit PCM mono 24 kHz (1000 ms): check the captions against the audio")


@pytest.mark.parametrize("wav", [b"", b"RIFF", pcm_to_wav(b"")])
def test_empty_audio_raises(wav):
    with pytest.raises(TTSError, match="empty"):
        build_rendered("how", chunk(), "chloe", TTSResult(wav, None), fake=False)


def test_fake_flag_and_default_fake_synth(test_talk, tmp_path):
    render(test_talk, tmp_path, fake=True, synth=None)
    data = meta(tmp_path, "how", 0)
    words, duration = synthetic_timing(test_talk.chunk("how", 0).text)
    assert data["fake"] is True and data["estimated"] is False and data["duration_ms"] == duration
    assert [(w["word"], w["start"], w["end"], w["sentence"]) for w in data["words"]] == \
        [(w.word, w.start, w.end, w.sentence) for w in words]


# --- summary -------------------------------------------------------------------------------------------------------

def test_summary_lines(test_talk, tmp_path):
    render(test_talk, tmp_path)
    chunk_paths("test", "qa", 0, tmp_path)[1].unlink()
    report = render(test_talk, tmp_path)
    lines = summary_lines(test_talk, report)
    ms = {slide: 0 for slide, _ in CHUNKS}
    for slide, n in CHUNKS:
        ms[slide] += meta(tmp_path, slide, n)["duration_ms"]
    assert lines[0].split() == ["how", "3", "chunks", f"{ms['how'] / 1000:.1f}", "s", "(0", "rendered,", "3", "cached)"]
    assert lines[1].split() == ["qa", "1", "chunk", f"{ms['qa'] / 1000:.1f}", "s", "(1", "rendered,", "0", "cached)"]
    total = (ms["how"] + ms["qa"]) / 1000
    assert lines[2] == f"total: 4 chunks, {total:.1f} s of narration (1 rendered, 3 cached)"
    assert len(lines) == 3


def test_summary_shows_failed_and_skipped_only_when_present(test_talk, tmp_path):
    error = TTSError("no credit", status=429, type="insufficient_quota", code="no_billing_account", fatal=True)
    report = render(test_talk, tmp_path, synth=Synth(fail={"Every single": error}), jobs=1)
    total = summary_lines(test_talk, report)[-1]
    assert total.startswith("total: 4 chunks,") and total.endswith("(1 rendered, 0 cached, 1 failed, 2 skipped)")


# --- CLI -----------------------------------------------------------------------------------------------------------

def run_main(capsys, *argv: str, settings: Settings | None = None, synth: Any = None) -> tuple[int, str]:
    code = main(list(argv), settings=settings or Settings(), synth=synth)
    return code, capsys.readouterr().out


def test_cli_fake_render(capsys, tmp_path):
    code, out = run_main(capsys, str(TEST_TALK), "--fake", "--out", str(tmp_path))
    assert code == 0
    assert all(meta(tmp_path, s, n)["fake"] is True for s, n in CHUNKS)
    assert "[fake audio (placeholder tones)]" in out and "total: 4 chunks" in out and "(4 rendered, 0 cached)" in out
    assert "note: fake audio is a placeholder" in out
    code, out = run_main(capsys, str(TEST_TALK), "--fake", "--out", str(tmp_path))
    assert code == 0 and "(0 rendered, 4 cached)" in out


def test_cli_real_render_with_injected_synth(capsys, tmp_path):
    synth = Synth()
    code, out = run_main(capsys, str(TEST_TALK), "--out", str(tmp_path), "--jobs", "2", synth=synth)
    assert code == 0 and len(synth.calls) == 4 and "[TTS 3, voice chloe]" in out
    assert "note: fake audio" not in out
    code, out = run_main(capsys, str(TEST_TALK), "--out", str(tmp_path), "--force", "--only", "qa", synth=synth)
    assert code == 0 and len(synth.calls) == 5 and "(1 rendered, 0 cached)" in out


def test_cli_check_validates_and_renders_nothing(capsys, tmp_path):
    synth = Synth()
    code, out = run_main(capsys, str(TEST_TALK), "--check", "--out", str(tmp_path), synth=synth)
    assert code == 0 and synth.calls == [] and list(tmp_path.iterdir()) == []
    assert "ok: test is valid (2 relay slides, 4 chunks); nothing rendered (--check)" in out


def test_cli_invalid_package_exits_1_and_prints_every_issue(capsys, tmp_path):
    root = copy_talk(tmp_path)
    script = (root / "script.yaml").read_text(encoding="utf-8")
    script = script.replace("voice: chloe", "voice: nobody").replace("Every single word", "Every 1 word")
    (root / "script.yaml").write_text(script, encoding="utf-8")
    synth = Synth()
    code, out = run_main(capsys, str(root), "--out", str(tmp_path / "out"), synth=synth)
    assert code == 1 and synth.calls == []
    assert "error: script.yaml: voice: 'nobody' is not a preset" in out
    assert "error: script.yaml: how.chunks[1]: spell numbers as words" in out
    assert "2 error(s)" in out
    assert not (tmp_path / "out").exists()
    code, _ = run_main(capsys, str(root), "--check", synth=synth)
    assert code == 1


def test_cli_refuses_slide_ids_that_differ_only_in_case(capsys, tmp_path):
    # On macOS `how` and `How` share out/talks/test/how/: the render deleted the other slide's chunks, reported
    # success, and `relay serve` then refused to start. Now the validator stops it before anything is written.
    root = copy_talk(tmp_path)
    script = (root / "script.yaml").read_text(encoding="utf-8")
    (root / "script.yaml").write_text(script.replace("  - id: qa\n", "  - id: How\n"), encoding="utf-8")
    slides = (root / "slides.md").read_text(encoding="utf-8")
    (root / "slides.md").write_text(slides.replace('id="qa"', 'id="How"'), encoding="utf-8")
    synth = Synth()
    code, out = run_main(capsys, str(root), "--out", str(tmp_path / "out"), synth=synth)
    assert code == 1 and synth.calls == []
    assert "error: slides.md: slide ids 'how' and 'How' differ only in case" in out
    assert not (tmp_path / "out").exists()


def test_cli_prints_warnings_and_still_renders(capsys, tmp_path):
    root = copy_talk(tmp_path)
    script = (root / "script.yaml").read_text(encoding="utf-8")
    (root / "script.yaml").write_text(script.replace("comes with a timestamp", "comes from TTS"), encoding="utf-8")
    code, out = run_main(capsys, str(root), "--fake", "--out", str(tmp_path / "out"))
    assert code == 0
    assert "warning: script.yaml: how.chunks[1]: acronyms TTS: check the pronunciation by ear" in out


@pytest.mark.parametrize("only", ["title", "nope"])
def test_cli_only_must_be_a_relay_slide(capsys, tmp_path, only):
    code, out = run_main(capsys, str(TEST_TALK), "--fake", "--only", only, "--out", str(tmp_path))
    assert code == 1 and "not a relay slide of 'test' (relay slides: how, qa)" in out
    assert list(tmp_path.iterdir()) == []


def test_cli_without_key_exits_2_with_a_hint(capsys, tmp_path):
    code, out = run_main(capsys, str(TEST_TALK), "--out", str(tmp_path))
    assert code == 2
    assert "stopped: TTS 3: BOSON_API_KEY is empty and 4 chunk(s) need TTS 3" in out
    assert "--fake" in out and "(0 rendered, 0 cached, 4 skipped)" in out


@pytest.mark.parametrize("error,hint", [
    (TTSError("Invalid API key", status=401, type="authentication_error", code="invalid_api_key", fatal=True),
     "check BOSON_API_KEY"),
    (TTSError("not linked to billing", status=429, type="insufficient_quota", code="no_billing_account", fatal=True),
     CREDITS_URL),
])
def test_cli_fatal_tts_error_exits_2_with_the_hint(capsys, tmp_path, error, hint):
    code, out = run_main(capsys, str(TEST_TALK), "--out", str(tmp_path), "--jobs", "1",
                         synth=Synth(fail={"Here is how": error}))
    assert code == 2
    stopped = [line for line in out.splitlines() if line.startswith("stopped: ")]
    assert len(stopped) == 1 and hint in stopped[0] and f"HTTP {error.status}" in stopped[0]


def test_cli_transient_failure_exits_2(capsys, tmp_path):
    synth = Synth(fail={"whole pipeline": TTSError("busy", status=503, type="api_error")})
    code, out = run_main(capsys, str(TEST_TALK), "--out", str(tmp_path), synth=synth)
    assert code == 2 and "1 chunk(s) not rendered: re-run to retry them" in out
    assert "FAILED   qa/0" in out


def test_cli_reports_removed_leftovers(capsys, tmp_path):
    stale = chunk_paths("test", "qa", 4, tmp_path)[0]
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    code, out = run_main(capsys, str(TEST_TALK), "--fake", "--out", str(tmp_path))
    assert code == 0 and f"removed leftover {stale}" in out and not stale.exists()


def test_cli_config_error_exits_2(capsys, tmp_path, monkeypatch):
    def broken() -> Settings:
        raise ValueError("MIC_MODE must be one of ('tap', 'hold'), got 'open'")

    monkeypatch.setattr(render_mod, "load_settings", broken)
    code = main([str(TEST_TALK), "--fake", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 2 and "config error: MIC_MODE" in out


def test_cli_relative_out_dir(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, _ = run_main(capsys, str(TEST_TALK), "--fake", "--out", "rel")
    assert code == 0 and chunk_paths("test", "qa", 0, tmp_path / "rel")[0].is_file()


def test_python_dash_m_entry_point(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "relay.render", str(TEST_TALK), "--fake", "--out", str(tmp_path)],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "total: 4 chunks" in proc.stdout
    assert all(path.is_file() for s, n in CHUNKS for path in chunk_paths("test", s, n, tmp_path))
    proc = subprocess.run([sys.executable, "-m", "relay.render", str(TEST_TALK), "--only", "title"],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
