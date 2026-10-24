"""End-of-utterance detection on PCM16 mono mic audio (contract §3.4): the backend ends user turns itself.

    seg = Segmenter()                       # 24 kHz
    for kind, pcm in seg.feed(frame):       # any frame size
        "start" → pcm = preroll + the speech so far (append it to Realtime first)
        "audio" → pcm = more of the utterance
        "end"   → pcm = None: end_ms of silence, or max_ms reached → commit

The audio is analysed in 10 ms frames. A frame is speech when its RMS level (dBFS) reaches the threshold
max(floor_db, noise floor + margin_db); the noise floor is the 20th percentile of the last 3 s of non-speech frame
levels outside utterances (with less than 300 ms of them: floor_db alone). Speech frames between utterances (onsets,
clicks) stay out of it, or back-to-back talk would lift the floor to the voice. An utterance starts once speech frames add
up to start_ms (a candidate is dropped after start_ms of silence: clicks never start one).

Noise floor shift: when the last 3 s *inside* an utterance are steady (20th to 80th percentile within
STEADY_SPREAD_DB; speech varies by 15 dB and more) and even their quietest fifth is above the threshold, the
"utterance" is steady noise (a fan, a louder room), not speech: the floor is re-estimated from those 3 s, so it ends
after end_ms instead of running to max_ms. Pure Python, no I/O, deterministic.
"""

from __future__ import annotations

import math
import sys
from array import array
from collections import deque

FRAME_MS = 10
HISTORY_MS = 3000  # noise floor window
MIN_HISTORY_MS = 300  # below this much history the threshold is floor_db alone
PERCENTILE = 0.2
STEADY_SPREAD_DB = 6.0  # 20th–80th percentile spread below which an utterance is steady noise, not speech
SILENCE_DB = -120.0  # the level of digital silence


def level_db(samples) -> float:
    """RMS level of PCM16 samples in dBFS (digital silence: SILENCE_DB)."""
    if not samples:
        return SILENCE_DB
    mean_square = math.sumprod(samples, samples) / len(samples)
    if mean_square <= 0:
        return SILENCE_DB
    return max(SILENCE_DB, 10 * math.log10(mean_square / (32768.0 * 32768.0)))


class Segmenter:
    def __init__(self, rate: int = 24000, *, start_ms: int = 200, end_ms: int = 700, max_ms: int = 20000,
                 preroll_ms: int = 300, floor_db: float = -50, margin_db: float = 12):
        if rate < 100 or rate % 100:
            raise ValueError(f"rate must be a multiple of 100 Hz, got {rate}")
        self.rate = rate
        self.frame_samples = rate * FRAME_MS // 1000
        self.frame_bytes = self.frame_samples * 2
        self.start_frames = max(1, math.ceil(start_ms / FRAME_MS))
        self.end_frames = max(1, math.ceil(end_ms / FRAME_MS))
        self.max_frames = max(self.start_frames + 1, max_ms // FRAME_MS)
        self.preroll_frames = max(0, preroll_ms // FRAME_MS)
        self.floor_db = float(floor_db)
        self.margin_db = float(margin_db)
        self._history: deque[float] = deque(maxlen=HISTORY_MS // FRAME_MS)  # frame levels outside utterances
        self.reset()

    def reset(self, keep_floor: bool = False) -> None:
        """Forget the current utterance and buffered audio (and, unless keep_floor, the noise floor history)."""
        self.in_speech = False
        self._pending = b""
        self._preroll: deque[bytes] = deque(maxlen=self.preroll_frames)
        self._candidate: list[bytes] = []  # frames since a possible speech onset
        self._candidate_speech = 0  # speech frames in the candidate
        self._quiet = 0  # consecutive non-speech frames (candidate or utterance)
        self._utterance_frames = 0
        self._inside: deque[float] = deque(maxlen=HISTORY_MS // FRAME_MS)  # levels in the current utterance
        if not keep_floor:
            self._history.clear()
        self.frames_seen = 0

    # --- levels

    @staticmethod
    def _percentile(levels) -> float:
        ordered = sorted(levels)
        return ordered[int(PERCENTILE * (len(ordered) - 1))]

    @property
    def noise_floor_db(self) -> float | None:
        """20th percentile of recent frame levels outside utterances (None without enough history)."""
        if len(self._history) * FRAME_MS < MIN_HISTORY_MS:
            return None
        return self._percentile(self._history)

    def _steady_noise(self) -> bool:
        ordered = sorted(self._inside)
        low, high = ordered[int(PERCENTILE * (len(ordered) - 1))], ordered[int((1 - PERCENTILE) * (len(ordered) - 1))]
        return low >= self.threshold_db and high - low <= STEADY_SPREAD_DB

    @property
    def speaking(self) -> bool:
        """In an utterance, or in a possible onset (speech heard, not yet start_ms of it): someone may be talking."""
        return self.in_speech or self._candidate_speech > 0

    @property
    def threshold_db(self) -> float:
        floor = self.noise_floor_db
        return self.floor_db if floor is None else max(self.floor_db, floor + self.margin_db)

    # --- feeding

    def feed(self, pcm: bytes) -> list[tuple[str, bytes | None]]:
        """Events for this chunk of PCM16 mono audio, in order. Partial frames wait for the next call."""
        data = self._pending + bytes(pcm)
        whole = len(data) - len(data) % self.frame_bytes
        self._pending = data[whole:]
        events: list[tuple[str, bytes | None]] = []
        for offset in range(0, whole, self.frame_bytes):
            self._frame(data[offset:offset + self.frame_bytes], events)
        merged: list[tuple[str, bytes | None]] = []
        for kind, payload in events:  # one "audio" event per run of frames
            if kind == "audio" and merged and merged[-1][0] == "audio":
                merged[-1] = ("audio", merged[-1][1] + payload)
            else:
                merged.append((kind, payload))
        return merged

    def _frame(self, frame: bytes, events: list) -> None:
        self.frames_seen += 1
        samples = array("h", frame)
        if sys.byteorder == "big":
            samples.byteswap()
        db = level_db(samples)
        speech = db >= self.threshold_db
        if self.in_speech:
            events.append(("audio", frame))
            self._utterance_frames += 1
            self._inside.append(db)
            if len(self._inside) == self._inside.maxlen and self._steady_noise():
                self._history.clear()  # a noise floor shift, not speech: learn the new floor from it
                self._history.extend(self._inside)
                self._inside.clear()
                speech = db >= self.threshold_db
            self._quiet = 0 if speech else self._quiet + 1
            if self._quiet >= self.end_frames or self._utterance_frames >= self.max_frames:
                events.append(("end", None))
                self._end()
            return
        if not speech:  # only quiet frames teach the floor: speech onsets between utterances would raise it
            self._history.append(db)
        if self._candidate or speech:
            self._candidate.append(frame)
            if speech:
                self._candidate_speech += 1
                self._quiet = 0
            else:
                self._quiet += 1
            if self._candidate_speech >= self.start_frames:
                payload = b"".join(self._preroll) + b"".join(self._candidate)
                self._utterance_frames = len(self._candidate)
                self._candidate, self._candidate_speech, self._quiet = [], 0, 0
                self._preroll.clear()
                self.in_speech = True
                events.append(("start", payload))
                if self._utterance_frames >= self.max_frames:
                    events.append(("end", None))
                    self._end()
            elif self._quiet >= self.start_frames:  # a click or a cough: not an utterance
                self._preroll.extend(self._candidate)
                self._candidate, self._candidate_speech, self._quiet = [], 0, 0
            return
        self._preroll.append(frame)

    def _end(self) -> None:
        self.in_speech = False
        self._quiet = 0
        self._utterance_frames = 0
        self._inside.clear()
        self._preroll.clear()
