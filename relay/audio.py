"""Pure audio helpers for the Zoom bot (contract v5 §0, §2): the page's Web Audio tap is 48 kHz float32, the deck
protocol wants 24 kHz PCM16 in 100 ms frames.

- `Decimator`: 48 kHz → 24 kHz with a windowed-sinc low-pass (cut below the output's Nyquist, so nothing above 12 kHz
  folds back as aliasing) and 2:1 decimation, stateful across calls (the filter's history carries over). Any other
  input rate falls back to linear interpolation (no low-pass): a 44.1 kHz context would still be understood, not pretty.
- `Framer`: bytes in, whole 100 ms (4800-byte) frames out, the remainder carried to the next call.
- `upsample_2x`: 24 kHz PCM16 → 48 kHz float32 (linear interpolation), the dry run's stand-in for the browser's own
  resampling of the participants' clips into the tap.

No numpy, no audioop: `math.sumprod` does the dot products in C.
"""

from __future__ import annotations

import array
import math
import struct
import sys
from typing import Iterable, Sequence

RATE = 24_000
FRAME_MS = 100
BYTES_PER_MS = RATE * 2 // 1000
FRAME_BYTES = FRAME_MS * BYTES_PER_MS  # 4800
TAP_RATE = 48_000
DEFAULT_TAPS = 31
DEFAULT_CUTOFF = 0.23  # cycles per input sample: ~11 kHz at 48 kHz, a little under the output's 12 kHz Nyquist


def lowpass_taps(taps: int = DEFAULT_TAPS, cutoff: float = DEFAULT_CUTOFF) -> list[float]:
    """A Hamming-windowed sinc low-pass, unity gain at DC. `cutoff` in cycles per sample (0 < cutoff < 0.5)."""
    if taps < 3 or taps % 2 == 0:
        raise ValueError(f"taps must be an odd number ≥ 3, got {taps}")
    if not 0 < cutoff < 0.5:
        raise ValueError(f"cutoff must be in (0, 0.5), got {cutoff}")
    middle = taps // 2
    kernel = []
    for n in range(taps):
        k = n - middle
        sinc = 2 * cutoff if k == 0 else math.sin(2 * math.pi * cutoff * k) / (math.pi * k)
        window = 0.54 - 0.46 * math.cos(2 * math.pi * n / (taps - 1))
        kernel.append(sinc * window)
    gain = sum(kernel)
    return [h / gain for h in kernel]


def floats_from_bytes(data: bytes | bytearray | memoryview) -> array.array:
    """Little-endian float32 samples (what the page's Float32Array buffer holds) as an array of floats."""
    usable = len(data) - len(data) % 4
    values = array.array("f")
    values.frombytes(bytes(data[:usable]))
    if sys.byteorder == "big":
        values.byteswap()
    return values


def pcm16_from_floats(values: Iterable[float]) -> bytes:
    """Float samples in [-1, 1] (clipped) → little-endian PCM16 bytes."""
    out = array.array("h", (int(max(-1.0, min(1.0, v)) * 32767) for v in values))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def floats_from_pcm16(pcm: bytes) -> array.array:
    usable = len(pcm) - len(pcm) % 2
    values = array.array("h")
    values.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        values.byteswap()
    return array.array("f", (v / 32768 for v in values))


class Decimator:
    """Stateful resampler to 24 kHz. From 48 kHz: low-pass + decimate by 2 (the designed path). From any other rate:
    linear interpolation, stateless apart from the fractional position."""

    def __init__(self, in_rate: int = TAP_RATE, *, out_rate: int = RATE, taps: int = DEFAULT_TAPS,
                 cutoff: float = DEFAULT_CUTOFF) -> None:
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("rates must be positive")
        self.in_rate, self.out_rate = int(in_rate), int(out_rate)
        self.exact = self.in_rate == 2 * self.out_rate
        self._taps = lowpass_taps(taps, cutoff)
        self._history: list[float] = [0.0] * (len(self._taps) - 1)  # the last taps-1 input samples
        self._phase = 0.0  # the interpolation path: position inside the input between calls
        self._last = 0.0

    def process(self, samples: Sequence[float]) -> bytes:
        """The next input samples → PCM16 bytes at the output rate (some samples stay in the filter's history)."""
        if not samples:
            return b""
        if self.exact:
            return pcm16_from_floats(self._decimate(samples))
        return pcm16_from_floats(self._interpolate(samples))

    def _decimate(self, samples: Sequence[float]) -> list[float]:
        taps = self._taps
        n_taps = len(taps)
        x = self._history + list(samples)
        # output n = Σ_k h[k] · x[2n + (n_taps-1) - k]; the kernel is symmetric, so the slice order is irrelevant
        count = (len(x) - (n_taps - 1)) // 2
        sumprod = math.sumprod
        out = [sumprod(taps, x[2 * n:2 * n + n_taps]) for n in range(count)]
        consumed = 2 * count
        self._history = x[consumed:]  # (n_taps-1 or n_taps samples: the odd one is next call's first input)
        return out

    def _interpolate(self, samples: Sequence[float]) -> list[float]:
        step = self.in_rate / self.out_rate
        x = [self._last] + list(samples)  # index 0 is the previous call's last sample: position 0 is "before" input
        out = []
        pos = self._phase  # measured in input samples from x[0]
        while pos + 1 < len(x):
            i = int(pos)
            frac = pos - i
            out.append(x[i] + (x[i + 1] - x[i]) * frac)
            pos += step
        self._phase = pos - (len(x) - 1)
        self._last = x[-1]
        return out


class Framer:
    """Bytes in, whole frames of `frame_bytes` out; the remainder is carried over to the next call."""

    def __init__(self, frame_bytes: int = FRAME_BYTES) -> None:
        if frame_bytes <= 0 or frame_bytes % 2:
            raise ValueError(f"frame_bytes must be a positive even number, got {frame_bytes}")
        self.frame_bytes = frame_bytes
        self._carry = bytearray()

    @property
    def carried(self) -> int:
        return len(self._carry)

    def feed(self, pcm: bytes) -> list[bytes]:
        self._carry.extend(pcm)
        frames = []
        size = self.frame_bytes
        while len(self._carry) >= size:
            frames.append(bytes(self._carry[:size]))
            del self._carry[:size]
        return frames

    def flush(self) -> bytes | None:
        """The remainder padded with silence to a whole frame (None when nothing is carried)."""
        if not self._carry:
            return None
        frame = bytes(self._carry).ljust(self.frame_bytes, b"\x00")
        self._carry.clear()
        return frame


def upsample_2x(pcm: bytes) -> array.array:
    """24 kHz PCM16 → 48 kHz float32 by linear interpolation (the dry run's stand-in for the browser's resampler)."""
    source = floats_from_pcm16(pcm)
    out = array.array("f", bytes(8 * len(source)))
    n = len(source)
    for i in range(n):
        a = source[i]
        b = source[i + 1] if i + 1 < n else a
        out[2 * i] = a
        out[2 * i + 1] = (a + b) / 2
    return out


def sine(freq_hz: float, seconds: float, rate: int, amplitude: float = 0.5) -> array.array:
    """A test tone as float32 samples."""
    n = int(rate * seconds)
    step = 2 * math.pi * freq_hz / rate
    return array.array("f", (amplitude * math.sin(step * i) for i in range(n)))


def rms_pcm16(pcm: bytes) -> float:
    """RMS of PCM16 bytes as a fraction of full scale (0.0 for silence)."""
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    values = struct.unpack(f"<{count}h", pcm[:2 * count])
    return math.sqrt(sum(v * v for v in values) / count) / 32768


def db_pcm16(pcm: bytes) -> float:
    """Level of PCM16 bytes in dBFS (−120 for silence)."""
    return 20 * math.log10(max(rms_pcm16(pcm), 1e-6))
