"""relay.timeline (foundation): rendered-chunk model, layout, cache hash, synthetic timings, cut/resume arithmetic.

Written by a different builder than the module, on purpose: these tests try to break it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import relay.timeline as timeline
from relay.config import OUT
from relay.textutil import sentence_words
from relay.timeline import (CutPoint, RenderedChunk, RenderMissing, Word, chunk_hash, chunk_paths, cut_point,
                            cut_word_index, load_rendered, resume_offset_ms, scale_timing, sentence_start_index,
                            synthetic_timing, talk_out_dir)

# --- hash and layout ------------------------------------------------------------------------------------------------

def test_chunk_hash_is_what_the_design_says():
    payload = json.dumps({"v": 1, "model": "higgs-tts-3", "voice": "chloe", "input": "Hi there.", "format": "wav",
                          "timestamps": True}, sort_keys=True)
    assert chunk_hash("Hi there.", "chloe") == hashlib.sha256(payload.encode()).hexdigest()[:16]


def test_chunk_hash_is_stable_and_short_hex():
    first = chunk_hash("<|emotion:awe|>Wow.", "chloe")
    assert first == chunk_hash("<|emotion:awe|>Wow.", "chloe")
    assert len(first) == 16 and int(first, 16) >= 0


@pytest.mark.parametrize("say,voice", [
    ("Hi there!", "chloe"), ("hi there.", "chloe"), ("Hi  there.", "chloe"), ("Hi there. ", "chloe"),
    ("<|emotion:awe|>Hi there.", "chloe"), ("Hi <|prosody:pause|>there.", "chloe"), ("Hi there.", "marcus"),
    ("Hi there.", "Chloe"),
])
def test_chunk_hash_changes_with_anything_that_changes_the_audio(say, voice):
    assert chunk_hash(say, voice) != chunk_hash("Hi there.", "chloe")


@pytest.mark.parametrize("name,value", [("RENDER_VERSION", 2), ("TTS_MODEL", "higgs-tts-4")])
def test_chunk_hash_covers_render_version_and_model(monkeypatch, name, value):
    before = chunk_hash("Hi there.", "chloe")
    monkeypatch.setattr(timeline, name, value)
    assert chunk_hash("Hi there.", "chloe") != before


def test_chunk_hash_handles_unicode():
    assert chunk_hash("Café “quotes” — é…", "chloe") != chunk_hash("Cafe \"quotes\" - e...", "chloe")


def test_layout(tmp_path):
    assert talk_out_dir("test") == OUT / "talks" / "test"
    assert talk_out_dir("test", tmp_path) == tmp_path / "talks" / "test"
    assert chunk_paths("test", "how", 2, tmp_path) == (tmp_path / "talks/test/how/2.wav",
                                                      tmp_path / "talks/test/how/2.json")
    assert chunk_paths("test", "qa", 0) == (OUT / "talks/test/qa/0.wav", OUT / "talks/test/qa/0.json")
    assert chunk_paths("test", "how", 10, tmp_path)[0].name == "10.wav"  # plain numbers, no zero padding


# --- RenderedChunk --------------------------------------------------------------------------------------------------

def sample_chunk(**changes) -> RenderedChunk:
    words = [Word("Here", 120, 300, 0), Word("is", 350, 500, 0), Word("Then.", 900, 1200, 1)]
    values = dict(slide="how", index=1, say="<|emotion:awe|>Here is. Then.", text="Here is. Then.", words=words,
                  sentences=["Here is.", "Then."], duration_ms=1500, hash="abc", voice="chloe")
    return RenderedChunk(**{**values, **changes})


def test_to_json_matches_the_design_schema():
    data = sample_chunk(wav_path=Path("/x/1.wav")).to_json()
    assert list(data) == ["slide", "index", "say", "text", "words", "sentences", "duration_ms", "hash", "voice",
                          "fake", "estimated"]
    assert data["words"][0] == {"word": "Here", "start": 120, "end": 300, "sentence": 0}
    assert data["fake"] is False and data["estimated"] is False
    json.dumps(data)  # serialisable as is


def test_json_round_trip_and_wav_path():
    chunk = sample_chunk(fake=True, estimated=True)
    back = RenderedChunk.from_json(json.loads(json.dumps(chunk.to_json())), Path("/x/1.wav"))
    assert back == chunk  # wav_path is not compared
    assert back.wav_path == Path("/x/1.wav") and back.fake and back.estimated
    assert all(isinstance(word, Word) for word in back.words)


def test_from_json_defaults_and_coercion():
    back = RenderedChunk.from_json({"slide": "how", "index": "2", "text": "Hi.", "duration_ms": 999.9, "hash": "h",
                                    "words": [{"word": "Hi.", "start": 1.9, "end": "300"}]})
    assert back.say == "Hi." and back.voice == "" and back.sentences == []
    assert back.index == 2 and back.duration_ms == 999
    assert back.words == [Word("Hi.", 1, 300, 0)]  # missing sentence → 0
    assert back.fake is False and back.estimated is False and back.wav_path is None
    assert RenderedChunk.from_json({"slide": "s", "index": 0, "text": "", "duration_ms": 0, "hash": "h",
                                    "words": None, "sentences": None}).words == []


@pytest.mark.parametrize("data,exc", [
    ({}, KeyError), ({"slide": "how", "index": 0, "text": "Hi.", "hash": "h"}, KeyError),
    ({"slide": "how", "index": "one", "text": "Hi.", "duration_ms": 1, "hash": "h"}, ValueError),
    ({"slide": "how", "index": 0, "text": "Hi.", "duration_ms": 1, "hash": "h", "words": ["Hi"]}, TypeError),
])
def test_from_json_rejects_broken_data(data, exc):
    with pytest.raises(exc):
        RenderedChunk.from_json(data)


# --- load_rendered --------------------------------------------------------------------------------------------------

def write_chunks(chunks: dict, out: Path, skip: tuple = ()) -> None:
    for (slide, n), chunk in chunks.items():
        if (slide, n) in skip:
            continue
        wav, meta = chunk_paths("test", slide, n, out)
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"RIFF")
        meta.write_text(json.dumps(chunk.to_json()), encoding="utf-8")


def test_load_rendered_ok(test_talk, test_chunks, tmp_path):
    write_chunks(test_chunks, tmp_path)
    loaded = load_rendered(test_talk, tmp_path)
    assert sorted(loaded) == [("how", 0), ("how", 1), ("how", 2), ("qa", 0)]  # human slides need nothing
    for key, chunk in loaded.items():
        assert chunk == test_chunks[key]
        assert chunk.wav_path == chunk_paths("test", *key, tmp_path)[0]
        assert chunk.fake is True  # loaded as is: warning about fake audio is the server's job


@pytest.mark.parametrize("missing", ["wav", "json"])
def test_load_rendered_missing_file(test_talk, test_chunks, tmp_path, missing):
    write_chunks(test_chunks, tmp_path)
    wav, meta = chunk_paths("test", "how", 1, tmp_path)
    (wav if missing == "wav" else meta).unlink()
    with pytest.raises(RenderMissing) as info:
        load_rendered(test_talk, tmp_path)
    assert info.value.problems == ["how/1: not rendered"]
    assert "uv run python -m relay.render talks/test" in str(info.value)


def test_load_rendered_nothing_rendered_lists_every_chunk(test_talk, tmp_path):
    with pytest.raises(RenderMissing) as info:
        load_rendered(test_talk, tmp_path)
    assert info.value.problems == ["how/0: not rendered", "how/1: not rendered", "how/2: not rendered",
                                   "qa/0: not rendered"]
    assert all(problem in str(info.value) for problem in info.value.problems)


def test_load_rendered_stale(test_talk, test_chunks, tmp_path):
    write_chunks(test_chunks, tmp_path)
    test_talk.slide("how").chunks[2].say = "Something new."
    with pytest.raises(RenderMissing) as info:
        load_rendered(test_talk, tmp_path)
    assert info.value.problems == ["how/2: stale (script text or voice changed since it was rendered)"]
    loaded = load_rendered(test_talk, tmp_path, allow_stale=True)
    assert loaded[("how", 2)].say != "Something new."  # the old render, knowingly


def test_load_rendered_voice_change_makes_everything_stale(test_talk, test_chunks, tmp_path):
    write_chunks(test_chunks, tmp_path)
    test_talk.voice = "marcus"
    with pytest.raises(RenderMissing) as info:
        load_rendered(test_talk, tmp_path)
    assert len(info.value.problems) == 4 and all("stale" in problem for problem in info.value.problems)


@pytest.mark.parametrize("content", [b"{", b"[]", b"null", b"42", b'"text"', b'{"slide": "how"}',
                                     b'{"slide": "how", "index": 0, "text": "x", "duration_ms": 1, "hash": "h", '
                                     b'"words": [{"word": "x"}]}',
                                     b"\xff\xfe not utf-8"])
def test_load_rendered_unreadable(test_talk, test_chunks, tmp_path, content):
    write_chunks(test_chunks, tmp_path)
    chunk_paths("test", "qa", 0, tmp_path)[1].write_bytes(content)
    with pytest.raises(RenderMissing) as info:
        load_rendered(test_talk, tmp_path)
    [problem] = info.value.problems
    assert problem.startswith("qa/0: unreadable 0.json (")


def test_load_rendered_reports_every_problem_at_once(test_talk, test_chunks, tmp_path):
    write_chunks(test_chunks, tmp_path, skip=(("how", 0),))
    chunk_paths("test", "qa", 0, tmp_path)[1].write_text("{", encoding="utf-8")
    test_talk.slide("how").chunks[1].say = "Changed."
    with pytest.raises(RenderMissing) as info:
        load_rendered(test_talk, tmp_path)
    assert [problem.split(":")[0] for problem in info.value.problems] == ["how/0", "how/1", "qa/0"]


# --- synthetic timings ----------------------------------------------------------------------------------------------

def test_synthetic_timing_pinned_example():
    words, duration = synthetic_timing("One two. Three four five.")
    assert words == [Word("One", 150, 405, 0), Word("two.", 495, 750, 0), Word("Three", 1100, 1445, 1),
                     Word("four", 1535, 1835, 1), Word("five.", 1925, 2225, 1)]
    assert duration == 2475


@pytest.mark.parametrize("text", [
    "Here is how this works. Everything you hear from me right now was written and rendered ahead of time.",
    "<|emotion:enthusiasm|>Wow! <|prosody:pause|> Really? Yes… indeed.", "One.", "A. B. C.",
    "Supercalifragilisticexpialidocious, “quoted” — and done.",
])
def test_synthetic_timing_properties(text):
    words, duration = synthetic_timing(text)
    pairs = sentence_words(text)
    assert [(w.word, w.sentence) for w in words] == pairs
    assert words[0].start == 150 and duration == words[-1].end + 250
    for word in words:
        assert word.end - word.start >= 120
    for a, b in zip(words, words[1:]):
        assert a.start < a.end < b.start  # strictly increasing, never overlapping
        assert b.start - a.end == (350 if b.sentence != a.sentence else 90)
    assert all(isinstance(value, int) for w in words for value in (w.start, w.end, w.sentence))


def test_synthetic_timing_longer_words_last_longer():
    words, _ = synthetic_timing("a abcdefgh")
    assert words[1].end - words[1].start > words[0].end - words[0].start


def test_synthetic_timing_empty_and_custom_margins():
    assert synthetic_timing("") == ([], 400)
    assert synthetic_timing("<|prosody:pause|> — …") == ([], 400)
    words, duration = synthetic_timing("Hi.", lead_ms=0, tail_ms=0)
    assert words[0].start == 0 and duration == words[0].end


def test_synthetic_timing_ignores_tags():
    assert synthetic_timing("<|emotion:awe|>Wow. <|prosody:pause|> Big.") == synthetic_timing("Wow. Big.")


# --- scale_timing ---------------------------------------------------------------------------------------------------

WORDS = [Word("One", 150, 405, 0), Word("two.", 495, 750, 0), Word("Three", 1100, 1445, 1)]


def test_scale_timing_stretches_and_keeps_words():
    scaled = scale_timing(WORDS, 1000, 2000)
    assert scaled == [Word("One", 300, 810, 0), Word("two.", 990, 1500, 0), Word("Three", 2200, 2890, 1)]
    assert scale_timing(WORDS, 2000, 1000)[0] == Word("One", 75, 202, 0)  # round half to even: 202.5 → 202


def test_scale_timing_identity_and_bad_source():
    assert scale_timing(WORDS, 1500, 1500) == WORDS
    copy = scale_timing(WORDS, 0, 9000)
    assert copy == WORDS and copy is not WORDS
    assert scale_timing(WORDS, -5, 9000) == WORDS
    assert scale_timing([], 100, 200) == []


def test_scale_timing_keeps_order_and_bounds():
    words, duration = synthetic_timing("Here is how this works. Every word has a timestamp.")
    for target in (1, 999, duration, 3 * duration + 7):
        scaled = scale_timing(words, duration, target)
        assert all(a.start <= b.start and a.end <= b.end for a, b in zip(scaled, scaled[1:]))
        assert all(0 <= w.start <= w.end <= target for w in scaled)


# --- cut / resume ---------------------------------------------------------------------------------------------------

# Two sentences: words 0-1 are sentence 0, words 2-4 sentence 1 (pinned example above).
CUT_WORDS, CUT_DURATION = synthetic_timing("One two. Three four five.")


@pytest.mark.parametrize("t,expected", [
    (-50, 0), (0, 0), (149, 0), (150, 0), (404, 0), (494, 0), (495, 1), (751, 1), (1099, 1), (1100, 2), (1540, 3),
    (2224, 4), (2475, 4), (99_999, 4),
])
def test_cut_word_index(t, expected):
    assert cut_word_index(CUT_WORDS, t) == expected


def test_cut_word_index_without_words():
    assert cut_word_index([], 500) == 0


@pytest.mark.parametrize("i,expected", [(0, 0), (1, 0), (2, 2), (3, 2), (4, 2), (99, 2), (-3, 0)])
def test_sentence_start_index(i, expected):
    assert sentence_start_index(CUT_WORDS, i) == expected


def test_sentence_start_index_edges():
    assert sentence_start_index([], 3) == 0
    one = [Word("a", 0, 10, 0), Word("b", 20, 30, 0)]
    assert sentence_start_index(one, 1) == 0


@pytest.mark.parametrize("j,expected", [
    (0, 0), (-1, 0),
    (1, 450),  # mid-sentence: middle of the 90 ms gap (405 + 495) / 2
    (2, 925),  # sentence start: middle of the 350 ms gap (750 + 1100) / 2
    (99, 1880),  # clamped to the last word: (1835 + 1925) / 2
])
def test_resume_offset_ms(j, expected):
    assert resume_offset_ms(CUT_WORDS, j) == expected


def test_resume_offset_without_a_gap_backs_off_40_ms():
    touching = [Word("a", 100, 300, 0), Word("b", 300, 500, 1), Word("c", 20, 30, 2)]
    assert resume_offset_ms(touching, 1) == 260
    overlapping = [Word("a", 0, 400, 0), Word("b", 350, 500, 1)]
    assert resume_offset_ms(overlapping, 1) == 310
    assert resume_offset_ms([Word("a", 0, 30, 0), Word("b", 10, 40, 1)], 1) == 0  # never negative
    assert resume_offset_ms([], 3) == 0


@pytest.mark.parametrize("t,expected", [
    (0, CutPoint(0, 0, 0)),  # before the first word: restart the chunk
    (300, CutPoint(0, 0, 0)),
    (751, CutPoint(1, 0, 0)),  # just after "two.": its sentence starts the chunk
    (1100, CutPoint(2, 2, 925)),  # on the first word of the second sentence
    (1540, CutPoint(3, 2, 925)),  # mid second sentence → its first word
    (CUT_DURATION, CutPoint(4, 2, 925)),  # at the very end
])
def test_cut_point(t, expected):
    assert cut_point(CUT_WORDS, t) == expected


def test_cut_point_resume_never_clips_the_resume_word():
    words, duration = synthetic_timing("Here is how this works. Everything you hear was rendered. Then we resume!")
    for t in range(0, duration + 1, 37):
        point = cut_point(words, t)
        resume = words[point.resume_word]
        assert point.resume_word <= point.cut_word
        assert words[point.resume_word].sentence == words[point.cut_word].sentence
        assert point.resume_word == 0 or words[point.resume_word - 1].sentence != resume.sentence
        assert point.offset_ms <= resume.start
        if point.resume_word:
            assert point.offset_ms >= words[point.resume_word - 1].end  # never replays the previous sentence


def test_cut_point_on_conftest_chunks(test_chunks):
    for chunk in test_chunks.values():
        last = chunk.words[-1]
        point = cut_point(chunk.words, last.start)
        assert point.cut_word == len(chunk.words) - 1
        assert chunk.words[point.resume_word].sentence == last.sentence


# --- render_version ------------------------------------------------------------------------------------------------

def test_render_version_is_stable_and_short(test_talk, test_chunks):
    version = timeline.render_version(test_talk, test_chunks)
    assert version == timeline.render_version(test_talk, dict(reversed(list(test_chunks.items()))))
    assert len(version) == 12 and int(version, 16) >= 0


@pytest.mark.parametrize("change", ["hash", "fake", "duration", "slides", "voice", "drop"])
def test_render_version_changes_with_what_the_deck_preloads(test_talk, test_chunks, change):
    from dataclasses import replace

    before = timeline.render_version(test_talk, test_chunks)
    chunks = dict(test_chunks)
    key = ("how", 1)
    talk = test_talk
    if change == "hash":
        chunks[key] = replace(chunks[key], hash="0" * 16)
    elif change == "fake":
        chunks[key] = replace(chunks[key], fake=not chunks[key].fake)
    elif change == "duration":
        chunks[key] = replace(chunks[key], duration_ms=chunks[key].duration_ms + 1)
    elif change == "slides":
        talk = replace(test_talk, slides_md=test_talk.slides_md + "\n")
    elif change == "voice":
        talk = replace(test_talk, voice="marcus")
    else:
        del chunks[key]
    assert timeline.render_version(talk, chunks) != before
