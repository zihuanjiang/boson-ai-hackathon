"""relay.segmenter: end of utterance on synthetic PCM16 (contract §3.4): silence, tone bursts, clicks, gaps, max_ms,
noise floor shifts, frame sizes. 24 kHz mono: 48 bytes per millisecond, 10 ms analysis frames."""

from __future__ import annotations

import math
import random
import time
from array import array

import pytest

from relay.segmenter import SILENCE_DB, Segmenter, level_db

RATE = 24000
BPMS = RATE * 2 // 1000  # bytes per millisecond


def samples_to_pcm(samples) -> bytes:
    return array("h", samples).tobytes()


def amplitude(db: float) -> float:
    """RMS amplitude for a level in dBFS."""
    return 32768.0 * 10 ** (db / 20)


def silence(ms: int) -> list[int]:
    return [0] * (ms * RATE // 1000)


def tone(ms: int, db: float = -20.0, freq: float = 220.0) -> list[int]:
    peak = amplitude(db) * math.sqrt(2)
    return [int(round(peak * math.sin(2 * math.pi * freq * n / RATE))) for n in range(ms * RATE // 1000)]


def noise(ms: int, db: float, seed: int = 1) -> list[int]:
    rng = random.Random(seed)
    sigma = amplitude(db)
    return [max(-32768, min(32767, int(rng.gauss(0, sigma)))) for _ in range(ms * RATE // 1000)]


def syllables(ms: int, db: float = -20.0) -> list[int]:
    """Speech-like: a tone with a 4 Hz envelope that dips to zero (a wide level spread, unlike steady noise)."""
    base = tone(ms, db - 20 * math.log10(math.sqrt(3 / 8)), 180.0)  # same RMS as the plain tone after the envelope
    return [int(round(s * 0.5 * (1 - math.cos(2 * math.pi * 4 * n / RATE)))) for n, s in enumerate(base)]


def mix(*parts: list[int]) -> list[int]:
    length = max(len(part) for part in parts)
    return [max(-32768, min(32767, sum(part[n] if n < len(part) else 0 for part in parts))) for n in range(length)]


def pcm(*parts: list[int]) -> bytes:
    return samples_to_pcm([sample for part in parts for sample in part])


def run(seg: Segmenter, audio: bytes, chunk: int = 4800) -> list[tuple[str, bytes | None]]:
    events = []
    for offset in range(0, len(audio), chunk):
        events.extend(seg.feed(audio[offset:offset + chunk]))
    return events


def summary(events) -> list[tuple[str, int | None]]:
    """(kind, payload length), consecutive audio merged (also across feed calls)."""
    out: list[tuple[str, int | None]] = []
    for kind, payload in events:
        if kind == "audio" and out and out[-1][0] == "audio":
            out[-1] = ("audio", out[-1][1] + len(payload))
        else:
            out.append((kind, None if payload is None else len(payload)))
    return out


def utterances(events) -> list[bytes]:
    """The audio of each utterance: the start's preroll + its audio events."""
    out, current = [], None
    for kind, payload in events:
        if kind == "start":
            current = bytearray(payload)
        elif kind == "audio":
            current += payload
        elif kind == "end":
            out.append(bytes(current))
            current = None
    return out


# --- levels -------------------------------------------------------------------------------------------------------------

def test_level_db():
    assert level_db(array("h", silence(10))) == SILENCE_DB and level_db(array("h")) == SILENCE_DB
    assert level_db(array("h", tone(100, -20.0))) == pytest.approx(-20.0, abs=0.05)
    assert level_db(array("h", [32767, -32767] * 120)) == pytest.approx(0.0, abs=0.01)
    assert level_db(array("h", noise(100, -40.0))) == pytest.approx(-40.0, abs=0.3)


# --- the basics -----------------------------------------------------------------------------------------------------------

def test_silence_is_nothing():
    seg = Segmenter()
    assert run(seg, pcm(silence(3000))) == [] and not seg.in_speech
    assert seg.threshold_db == -50.0  # the noise floor (digital silence) + 12 is below floor_db


def test_a_tone_burst_is_one_utterance_with_its_preroll():
    seg = Segmenter()
    audio = pcm(silence(500), tone(500), silence(1000))
    events = run(seg, audio)
    # start after 200 ms of speech, carrying 300 ms of preroll; end after 700 ms of silence
    assert summary(events) == [("start", 500 * BPMS), ("audio", 1000 * BPMS), ("end", None)]
    assert utterances(events) == [audio[200 * BPMS:1700 * BPMS]]  # byte-exact: from 300 ms before the onset
    assert not seg.in_speech


def test_in_speech_follows_the_events():
    seg = Segmenter()
    assert run(seg, pcm(silence(300), tone(150))) == [] and not seg.in_speech  # 150 ms: not yet
    assert [kind for kind, _ in run(seg, pcm(tone(100)))] == ["start", "audio"] and seg.in_speech
    assert run(seg, pcm(silence(600)))[-1][0] == "audio" and seg.in_speech
    assert [kind for kind, _ in run(seg, pcm(silence(100)))] == ["audio", "end"] and not seg.in_speech


@pytest.mark.parametrize("chunk", [3, 7, 480, 1000, 4801, 10 ** 9])
def test_any_frame_size_gives_the_same_result(chunk):
    audio = pcm(silence(400), tone(600), silence(300), syllables(800), silence(1200), tone(300), silence(900))
    reference = run(Segmenter(), audio, chunk=480)
    events = run(Segmenter(), audio, chunk=chunk)
    assert summary(events) == summary(reference) and utterances(events) == utterances(reference)
    assert len(utterances(reference)) == 2


def test_odd_byte_boundaries():
    seg = Segmenter()
    audio = pcm(silence(300), tone(400), silence(800))
    events = seg.feed(audio[:1]) + seg.feed(audio[1:4801]) + seg.feed(audio[4801:])
    assert summary(events) == summary(run(Segmenter(), audio))


def test_deterministic():
    audio = pcm(noise(500, -60), mix(tone(700, -25), noise(700, -60, seed=2)), noise(1500, -60, seed=3))
    assert run(Segmenter(), audio) == run(Segmenter(), audio)


# --- what is not an utterance, and what is one -----------------------------------------------------------------------------

def test_a_click_is_not_an_utterance():
    assert run(Segmenter(), pcm(silence(500), tone(50, -6), silence(1000))) == []
    clicks = []
    for _ in range(6):
        clicks += tone(40, -6) + silence(300)
    assert run(Segmenter(), pcm(silence(300), clicks, silence(800))) == []


def test_a_quiet_voice_below_the_floor_is_not_speech():
    assert run(Segmenter(), pcm(silence(300), tone(800, -55), silence(800))) == []


def test_gaps_shorter_than_end_ms_stay_one_utterance():
    events = run(Segmenter(), pcm(silence(400), tone(500), silence(400), syllables(500), silence(650), tone(300),
                                  silence(1000)))
    assert [kind for kind, _ in summary(events)] == ["start", "audio", "end"]


def test_a_gap_of_end_ms_ends_the_utterance():
    audio = pcm(silence(400), tone(500), silence(800), tone(500), silence(1000))
    events = run(Segmenter(), audio)
    assert [kind for kind, _ in summary(events)] == ["start", "audio", "end", "start", "audio", "end"]
    first, second = utterances(events)
    assert len(first) == (300 + 500 + 700) * BPMS
    # only 100 ms passed after the first end: that is all the preroll the second can have
    assert summary(events)[3] == ("start", (100 + 200) * BPMS) and len(second) == (100 + 500 + 700) * BPMS


def test_end_ms_is_configurable():
    audio = pcm(silence(400), tone(500), silence(400), tone(500), silence(1000))
    assert len(utterances(run(Segmenter(end_ms=300), audio))) == 2
    assert len(utterances(run(Segmenter(end_ms=700), audio))) == 1


def test_max_ms_ends_a_long_utterance():
    events = run(Segmenter(max_ms=1000), pcm(tone(2200), silence(1000)))
    kinds = [kind for kind, _ in summary(events)]
    assert kinds == ["start", "audio", "end"] * 3  # cut at 1 s, 2 s; the rest ends after 700 ms of silence
    lengths = [len(u) // BPMS for u in utterances(events)]
    assert lengths == [1000, 1000, 200 + 700]
    # and max_ms also cuts the silent tail of a segment that reaches it
    assert [len(u) // BPMS for u in utterances(run(Segmenter(max_ms=1000), pcm(tone(2500), silence(1000))))] == \
        [1000, 1000, 1000]


def test_speech_right_at_the_start_is_caught():
    events = run(Segmenter(), pcm(tone(600), silence(900)))
    assert summary(events)[0] == ("start", 200 * BPMS)  # no preroll before the first frame


def test_back_to_back_talk_keeps_being_detected():
    # speech onsets between utterances must not teach the noise floor (it would climb to the voice)
    parts = []
    for n in range(12):
        parts += tone(900, -22) + silence(720)  # hard onsets, 20 ms of quiet between an end and the next onset
    events = run(Segmenter(), pcm(parts))
    assert len(utterances(events)) == 12


def test_preroll_ms_is_configurable():
    audio = pcm(silence(500), tone(400), silence(800))
    assert summary(run(Segmenter(preroll_ms=0), audio))[0] == ("start", 200 * BPMS)
    assert summary(run(Segmenter(preroll_ms=100), audio))[0] == ("start", 300 * BPMS)


def test_start_ms_is_configurable():
    audio = pcm(silence(500), tone(120), silence(800))
    assert run(Segmenter(), audio) == []
    assert summary(run(Segmenter(start_ms=100), audio))[0] == ("start", 400 * BPMS)


# --- noise floor ----------------------------------------------------------------------------------------------------------

def test_the_threshold_follows_a_slowly_rising_noise_floor():
    seg = Segmenter()
    assert run(seg, pcm(noise(1000, -60, seed=1))) == []
    assert seg.threshold_db == pytest.approx(-48, abs=1.0)  # floor -60 + 12 margin (above floor_db -50)
    assert run(seg, pcm(noise(3000, -51, seed=2))) == []  # below the threshold: it teaches the floor
    assert seg.threshold_db == pytest.approx(-39, abs=1.0)
    assert run(seg, pcm(noise(3000, -44, seed=3))) == []  # 5 dB louder again: still the room, not a voice
    assert seg.threshold_db == pytest.approx(-32, abs=1.0)
    # a voice at -40 dBFS would have been speech in the quiet room; in this one it is not
    assert run(seg, pcm(mix(tone(800, -40), noise(800, -44, seed=4)), noise(1000, -44, seed=5))) == []
    events = run(seg, pcm(mix(tone(800, -20), noise(800, -44, seed=6)), noise(1000, -44, seed=7)))
    assert [kind for kind, _ in summary(events)] == ["start", "audio", "end"]


def test_floor_db_is_the_lowest_threshold():
    seg = Segmenter(floor_db=-40)
    run(seg, pcm(noise(1000, -70)))
    assert seg.threshold_db == -40.0
    assert run(seg, pcm(tone(600, -45), silence(800))) == []
    assert len(utterances(run(seg, pcm(tone(600, -35), silence(800))))) == 1


def test_a_sudden_steady_noise_ends_within_seconds_not_at_max_ms():
    seg = Segmenter()
    run(seg, pcm(noise(1000, -70, seed=1)))
    events = run(seg, pcm(noise(8000, -30, seed=2)))  # a fan turns on, 40 dB louder
    kinds = [kind for kind, _ in summary(events)]
    assert kinds == ["start", "audio", "end"]  # one false utterance ...
    assert len(utterances(events)[0]) // BPMS < 4500  # ... cut once 3 s of it proved steady (not 20 s)
    assert seg.threshold_db == pytest.approx(-18, abs=1.0)  # the floor learned the fan
    assert run(seg, pcm(noise(3000, -30, seed=3))) == []
    events = run(seg, pcm(mix(syllables(1500, -10), noise(1500, -30, seed=4)), noise(1500, -30, seed=5)))
    assert len(utterances(events)) == 1  # a voice above the fan still counts


def test_long_speech_is_not_mistaken_for_steady_noise():
    events = run(Segmenter(), pcm(silence(500), syllables(6000, -20), silence(1000)))
    assert [kind for kind, _ in summary(events)] == ["start", "audio", "end"]
    # the envelope starts and ends at zero, so onset and end shift by a frame or two
    assert 300 + 6000 + 700 - 40 <= len(utterances(events)[0]) // BPMS <= 300 + 6000 + 700


def test_a_steady_tone_longer_than_three_seconds_is_noise():
    events = run(Segmenter(), pcm(silence(500), tone(6000, -20)))
    assert [kind for kind, _ in summary(events)] == ["start", "audio", "end"]


# --- reset, rates, speed -------------------------------------------------------------------------------------------------

def test_reset_mid_utterance():
    seg = Segmenter()
    run(seg, pcm(silence(500), tone(400)))
    assert seg.in_speech
    seg.reset()
    assert not seg.in_speech and run(seg, pcm(silence(1000))) == []


def test_reset_keeps_the_floor_on_request():
    seg = Segmenter()
    run(seg, pcm(noise(2000, -44)))
    raised = seg.threshold_db
    seg.reset(keep_floor=True)
    assert seg.threshold_db == raised
    seg.reset()
    assert seg.threshold_db == -50.0 and seg.noise_floor_db is None


def test_partial_frames_are_dropped_by_reset():
    seg = Segmenter()
    seg.feed(b"\x01")
    seg.reset()
    audio = pcm(silence(300), tone(400), silence(800))
    assert summary(seg.feed(audio)) == summary(run(Segmenter(), audio))


def test_other_rates():
    seg = Segmenter(rate=16000)
    assert seg.frame_bytes == 320
    sixteen = samples_to_pcm([0] * 8000 + [int(9000 * math.sin(2 * math.pi * 200 * n / 16000)) for n in range(8000)]
                             + [0] * 16000)
    assert [kind for kind, _ in summary(run(seg, sixteen, chunk=640))] == ["start", "audio", "end"]
    with pytest.raises(ValueError):
        Segmenter(rate=44)
    with pytest.raises(ValueError):
        Segmenter(rate=22050)


def test_faster_than_real_time():
    audio = pcm(noise(4000, -60), mix(syllables(3000, -20), noise(3000, -60, seed=2)), noise(3000, -60, seed=3))
    seg = Segmenter()
    start = time.perf_counter()
    run(seg, audio, chunk=4800)  # 10 s of audio in 100 ms frames
    assert time.perf_counter() - start < 1.0
