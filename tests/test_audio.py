"""relay.audio: the Zoom bot's 48 kHz float32 tap → 24 kHz PCM16 100 ms frames (contract v5 §0), pure Python."""

from __future__ import annotations

import math
import struct

import pytest

from relay.audio import (FRAME_BYTES, Decimator, Framer, db_pcm16, floats_from_bytes, floats_from_pcm16,
                         lowpass_taps, pcm16_from_floats, rms_pcm16, sine, upsample_2x)


def zero_crossings(pcm: bytes) -> int:
    values = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return sum(1 for a, b in zip(values, values[1:]) if (a < 0) != (b < 0))


def test_the_lowpass_has_unity_gain_and_is_symmetric():
    taps = lowpass_taps()
    assert len(taps) == 31 and abs(sum(taps) - 1.0) < 1e-9
    assert all(abs(taps[k] - taps[-1 - k]) < 1e-12 for k in range(len(taps) // 2))
    with pytest.raises(ValueError):
        lowpass_taps(30)
    with pytest.raises(ValueError):
        lowpass_taps(31, 0.6)


def test_a_1_khz_sine_keeps_its_length_frequency_and_level_through_the_decimator():
    tone = sine(1000, 1.0, 48_000, amplitude=0.5)
    dec = Decimator(48_000)
    out = dec.process(tone)
    # one second in → ~24000 samples out (the filter holds its last 30 inputs = 15 output samples)
    assert 24_000 - 20 <= len(out) // 2 <= 24_000
    # the frequency: a 1 kHz sine crosses zero 2000 times a second
    assert abs(zero_crossings(out) - 2000) <= 3
    # the level: RMS of a 0.5 sine is 0.354
    assert abs(rms_pcm16(out) - 0.5 / math.sqrt(2)) < 0.02


def test_the_decimator_is_continuous_across_calls_in_odd_sized_pieces():
    tone = sine(440, 0.5, 48_000, amplitude=0.4)
    whole = Decimator(48_000).process(tone)
    piecewise = Decimator(48_000)
    parts = b"".join(piecewise.process(tone[i:i + 1001]) for i in range(0, len(tone), 1001))
    # piecewise processing gives the same samples (up to the few the filter still holds)
    assert abs(len(parts) - len(whole)) <= 4
    common = min(len(parts), len(whole))
    assert parts[:common] == whole[:common]


def test_a_tone_above_the_output_nyquist_does_not_alias_into_the_output():
    loud = sine(20_000, 0.5, 48_000, amplitude=0.8)  # would fold to 4 kHz without the low-pass
    out = Decimator(48_000).process(loud)
    assert rms_pcm16(out) < 0.02  # (−34 dBFS or quieter; the input is −3 dBFS)
    assert db_pcm16(out) < db_pcm16(Decimator(48_000).process(sine(1000, 0.5, 48_000, amplitude=0.8))) - 25


def test_silence_in_is_silence_out_and_empty_in_is_empty_out():
    dec = Decimator(48_000)
    assert dec.process([]) == b""
    out = dec.process([0.0] * 4800)  # 100 ms in → 100 ms out (the history starts as silence: a 15-sample delay)
    assert len(out) == 4800 and set(out) == {0}


def test_another_input_rate_falls_back_to_interpolation_and_keeps_the_duration():
    tone = sine(500, 1.0, 44_100, amplitude=0.5)
    dec = Decimator(44_100)
    out = b"".join(dec.process(tone[i:i + 4410]) for i in range(0, len(tone), 4410))
    assert not dec.exact
    assert abs(len(out) // 2 - 24_000) <= 2
    assert abs(zero_crossings(out) - 1000) <= 3


def test_the_framer_carries_the_remainder_to_the_next_call():
    framer = Framer()
    assert framer.feed(bytes(7000)) == [bytes(FRAME_BYTES)] and framer.carried == 2200
    frames = framer.feed(bytes(2600))
    assert len(frames) == 1 and framer.carried == 0
    assert framer.feed(bytes(100)) == [] and framer.carried == 100
    tail = framer.flush()
    assert tail is not None and len(tail) == FRAME_BYTES and framer.carried == 0 and framer.flush() is None
    assert framer.feed(bytes(3 * FRAME_BYTES + 2)) == [bytes(FRAME_BYTES)] * 3 and framer.carried == 2
    with pytest.raises(ValueError):
        Framer(0)


def test_float_and_pcm16_conversions_round_trip_and_clip():
    values = [0.0, 0.5, -0.5, 1.5, -1.5]
    pcm = pcm16_from_floats(values)
    assert struct.unpack("<5h", pcm) == (0, 16383, -16383, 32767, -32767)
    back = floats_from_pcm16(pcm)
    assert [round(v, 3) for v in back] == [0.0, 0.5, -0.5, 1.0, -1.0]
    raw = struct.pack("<3f", 0.25, -0.25, 1.0) + b"\x01"  # a trailing partial sample is ignored
    assert list(floats_from_bytes(raw)) == [0.25, -0.25, 1.0]


def test_upsample_2x_doubles_the_length_and_a_round_trip_keeps_the_tone():
    tone24 = pcm16_from_floats(sine(1000, 0.5, 24_000, amplitude=0.5))
    up = upsample_2x(tone24)
    assert len(up) == 2 * (len(tone24) // 2)
    down = Decimator(48_000).process(up)
    assert abs(zero_crossings(down) - 1000) <= 3
    assert abs(rms_pcm16(down) - rms_pcm16(tone24)) < 0.03
