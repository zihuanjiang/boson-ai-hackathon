"""Higgs TTS 3 client (wav + word timestamps) and the offline fake synthesizer (design §2).

`synthesize` sends one narration chunk to `POST /v1/audio/speech` with `timestamps: true`. The response is then JSON:
the wav base64-encoded in `audio` and the word timings in **seconds** in `timestamps`: `null` when alignment is
unavailable, `[]` when the audio has no speech to align (the render fails such a chunk). `fake_synthesize` makes a
quiet tone-burst wav on exact synthetic timings, so rendering, serving and the tests work without a key or the
network. Never log `Settings.api_key`.
"""

from __future__ import annotations

import array
import base64
import binascii
import io
import math
import struct
import sys
import time
import wave
import zlib
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .config import RATE, TTS_MODEL, Settings
from .textutil import strip_tags
from .timeline import synthetic_timing

SPEECH_PATH = "/v1/audio/speech"
CREDITS_URL = "https://www.boson.ai/workspace/api-key?open-credits=true"
API_KEY_URL = "https://www.boson.ai/workspace/api-key"
BILLING_USAGE_URL = "https://www.boson.ai/workspace/billing/usage"
# `insufficient_quota` codes (doc/api-overview.md): billing refusals that no retry can fix.
QUOTA_CODES = frozenset({"no_billing_account", "insufficient_quota", "monthly_cap_reached", "contract_ended"})
MAX_BACKOFF_S = 30.0

FAKE_AMPLITUDE = 0.12  # of full scale: clearly audible, never startling
FAKE_FADE_MS = 8
FAKE_FREQ_HZ = (200, 260)


@dataclass
class TTSResult:
    wav: bytes
    words: list[dict] | None  # as the API returns them: [{"word", "start", "end"}] in seconds; None = no timings


class TTSError(Exception):
    """A failed TTS 3 request. `fatal` = retrying or rendering more chunks cannot help (key, billing, other 4xx)."""

    def __init__(self, message: str, *, status: int | None = None, type: str | None = None,
                 code: str | None = None, fatal: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.type = type
        self.code = code
        self.fatal = fatal

    @property
    def quota(self) -> bool:
        return self.type == "insufficient_quota" or self.code in QUOTA_CODES

    @property
    def hint(self) -> str:
        """What to do about it, in one line ("" when there is nothing useful to add)."""
        if self.quota:
            if self.code == "monthly_cap_reached":
                return f"billing: the monthly spend cap is reached; raise it at {BILLING_USAGE_URL}"
            return f"billing: claim the trial and hackathon credits at {CREDITS_URL}"
        if self.status == 401 or self.type == "authentication_error":
            return f"check BOSON_API_KEY in .env (a bai-… key from {API_KEY_URL})"
        if self.type == "missing_api_key":
            return "put BOSON_API_KEY=bai-… in .env (see .env.example), or render placeholder audio with --fake"
        if self.status == 403:
            return "the key was refused for higgs-tts-3: check BOSON_API_KEY and the key's model access"
        if self.status == 429:
            return "rate limited: wait a minute and re-run (finished chunks are cached)"
        if self.status is not None and self.status >= 500:
            return "server error: re-run in a minute (finished chunks are cached)"
        if self.type in ("timeout", "connection_error"):
            return "network: check the connection to api.boson.ai and re-run (finished chunks are cached)"
        if self.status == 400:
            return "check the chunk text (≤ 300 characters, known tags) and the voice name"
        if self.type == "bad_response":
            return "unexpected TTS 3 response: re-run; if it repeats, note it in doc/gotchas.md"
        if self.type == "no_speech":
            return "re-run to retry it (finished chunks are cached); if it repeats, rephrase the line"
        return ""

    def __str__(self) -> str:
        if self.status is None:  # raised locally (network, missing key): no API type/code to show
            text = f"TTS 3: {self.message}"
        else:
            detail = "".join(f" {name}={value}" for name, value in (("type", self.type), ("code", self.code)) if value)
            text = f"HTTP {self.status}{detail}: {self.message}"
        hint = self.hint
        return f"{text} → {hint}" if hint else text


def synthesize(text: str, voice: str, settings: Settings, *, session: Any = None, timeout: float = 120.0,
               retries: int = 3, sleep: Callable[[float], None] = time.sleep) -> TTSResult:
    """One TTS 3 request: wav + word timestamps for `text` (tags included) in preset `voice`.

    429 `rate_limit_error`, 5xx, timeouts and connection errors are retried `retries` times after the first try,
    waiting 1, 2, 4 … s. Key, billing and other 4xx errors raise a fatal TTSError at once.
    """
    url = settings.base_url + SPEECH_PATH
    payload = {"model": TTS_MODEL, "input": text, "voice": voice, "response_format": "wav", "timestamps": True}
    http = session if session is not None else requests
    error = TTSError("no attempt made", type="request_error")
    for attempt in range(1 + max(0, retries)):
        if attempt:
            sleep(min(MAX_BACKOFF_S, 2.0 ** (attempt - 1)))
        try:
            resp = http.post(url, headers=settings.headers, json=payload, timeout=timeout)
        except requests.Timeout as exc:
            error = TTSError(f"no response within {timeout:g} s ({_describe(exc)})", type="timeout")
            continue
        except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            error = TTSError(f"connection failed ({_describe(exc)})", type="connection_error")
            continue
        except requests.RequestException as exc:  # bad URL or similar: a configuration problem
            raise TTSError(f"request failed ({_describe(exc)})", type="request_error", fatal=True) from exc
        if 200 <= resp.status_code < 300:
            return _parse_success(resp)
        error = _http_error(resp)
        if error.fatal:
            raise error
    raise error


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


def _json_or_none(resp: Any) -> Any:
    try:
        return resp.json()
    except ValueError:  # requests' JSONDecodeError and UnicodeDecodeError are both ValueErrors
        return None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _http_error(resp: Any) -> TTSError:
    """TTSError from an error response; body `{"error": {"type", "code", "message"}}` (doc/api-overview.md)."""
    status = int(resp.status_code)
    body = _json_or_none(resp)
    err = body.get("error") if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    raw_text = getattr(resp, "text", "")
    snippet = " ".join(raw_text.split())[:200] if isinstance(raw_text, str) else ""
    detail = body.get("detail") if isinstance(body, dict) else None
    message = _text(err.get("message")) or _text(detail) or snippet or "no error message"
    error = TTSError(message, status=status, type=_text(err.get("type")), code=_text(err.get("code")))
    transient = status >= 500 or status == 408 or (status == 429 and not error.quota)
    error.fatal = not transient
    return error


def _parse_success(resp: Any) -> TTSResult:
    body = _json_or_none(resp)
    if not isinstance(body, dict):
        content = getattr(resp, "content", b"") or b""
        if content[:4] == b"RIFF":  # plain wav: the server ignored `timestamps`
            return TTSResult(bytes(content), None)
        raise TTSError("expected JSON with `audio` (timestamps: true)", status=resp.status_code, type="bad_response")
    stamps = body.get("timestamps")
    return TTSResult(_decode_audio(body.get("audio"), resp.status_code), stamps if isinstance(stamps, list) else None)


def _decode_audio(audio: Any, status: int) -> bytes:
    if not isinstance(audio, str) or not audio.strip():
        raise TTSError("the response has no `audio`", status=status, type="bad_response")
    if audio.startswith("data:") and "," in audio:
        audio = audio.split(",", 1)[1]
    try:
        wav = base64.b64decode(audio, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise TTSError(f"`audio` is not valid base64 ({exc})", status=status, type="bad_response") from exc
    if not wav:
        raise TTSError("`audio` is empty", status=status, type="bad_response")
    return wav


WAVE_PCM, WAVE_FLOAT, WAVE_EXTENSIBLE = 1, 3, 0xFFFE
_SAMPLE_BITS = {WAVE_PCM: (8, 16, 24, 32), WAVE_FLOAT: (32, 64)}
_STREAMED_SIZES = (0, 0xFFFFFFFF)  # a data size written before the length was known: the samples run to the end


@dataclass(frozen=True)
class WavInfo:
    """What a wav header says about its samples (`read_wav_info`)."""

    format_tag: int  # WAVE_PCM (integer) or WAVE_FLOAT; WAVE_FORMAT_EXTENSIBLE is unwrapped
    channels: int
    rate: int
    bits: int  # per sample
    data_bytes: int  # sample bytes present in the file

    @property
    def frames(self) -> int:
        return self.data_bytes // (self.channels * self.bits // 8)

    @property
    def duration_ms(self) -> int:
        return round(self.frames * 1000 / self.rate)

    def describe(self) -> str:
        layout = "mono" if self.channels == 1 else f"{self.channels} channels"
        return f"{self.bits}-bit {'float' if self.format_tag == WAVE_FLOAT else 'PCM'}, {layout}, {self.rate} Hz"


def read_wav_info(blob: bytes) -> WavInfo:
    """The sample format and data size of a RIFF/WAVE file, read leniently. Raises ValueError saying why not.

    Integer PCM (8/16/24/32-bit) and IEEE float (32/64-bit), plain or WAVE_FORMAT_EXTENSIBLE, any rate and channel
    count. Other chunks (LIST, fact …) are skipped. A data size of 0 or 0xFFFFFFFF (a streaming writer's header)
    means "to the end of the file"; a size past the end counts only the bytes that are there.
    """
    if len(blob) < 12 or blob[:4] not in (b"RIFF", b"RF64") or blob[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    fmt: bytes | None = None
    data_bytes: int | None = None
    pos = 12
    while pos + 8 <= len(blob) and (fmt is None or data_bytes is None):
        chunk_id, size = blob[pos:pos + 4], int.from_bytes(blob[pos + 4:pos + 8], "little")
        start = pos + 8
        if chunk_id == b"fmt ":
            fmt = blob[start:start + size]
        elif chunk_id == b"data":
            if size in _STREAMED_SIZES:
                data_bytes = len(blob) - start
                break  # everything after this header is samples
            data_bytes = min(size, len(blob) - start)
        pos = start + size + (size & 1)  # chunks are padded to an even size
    if fmt is None or len(fmt) < 16:
        raise ValueError("no fmt chunk")
    if data_bytes is None:
        raise ValueError("no data chunk")
    tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", fmt[:16])
    if tag == WAVE_EXTENSIBLE and len(fmt) >= 26:  # the real format tag opens the sub-format GUID
        tag = int.from_bytes(fmt[24:26], "little")
    if tag not in _SAMPLE_BITS:
        raise ValueError(f"format tag {tag:#06x} is neither integer PCM nor float")
    if channels < 1 or rate < 1 or bits not in _SAMPLE_BITS[tag]:
        raise ValueError(f"odd header: {channels} channel(s), {rate} Hz, {bits}-bit")
    return WavInfo(tag, channels, rate, bits, data_bytes)


def wav_duration_ms(data: bytes) -> int | None:
    """Duration from the wav header (`read_wav_info`; 0 = no samples), or None when the header can't be read."""
    try:
        return read_wav_info(data).duration_ms
    except ValueError:
        return None


def pcm_to_wav(pcm: bytes, rate: int = RATE) -> bytes:
    """Wrap little-endian PCM16 mono samples in a wav header."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()


def fake_synthesize(text: str, voice: str) -> TTSResult:
    """Offline, deterministic stand-in for `synthesize`: a quiet tone burst per word, silence elsewhere.

    Word timings are `timeline.synthetic_timing` in the API's shape (seconds); the wav lasts exactly the synthetic
    duration, so captions, cut points and resume offsets line up with what plays.
    """
    words, duration_ms = synthetic_timing(strip_tags(text))
    pcm = _tone_bursts([(word.start, word.end) for word in words], duration_ms, seed=voice)
    stamps = [{"word": word.word, "start": word.start / 1000, "end": word.end / 1000} for word in words]
    return TTSResult(pcm_to_wav(pcm), stamps)


def _tone_bursts(spans: list[tuple[int, int]], duration_ms: int, *, seed: str) -> bytes:
    """PCM16 mono at RATE: one faded sine burst (200–260 Hz) per (start_ms, end_ms) span."""
    total = duration_ms * RATE // 1000
    samples = array.array("h", bytes(2 * total))
    peak = FAKE_AMPLITUDE * 32767
    fade = FAKE_FADE_MS * RATE // 1000
    low, high = FAKE_FREQ_HZ
    for k, (start_ms, end_ms) in enumerate(spans):
        first, last = start_ms * RATE // 1000, min(total, end_ms * RATE // 1000)
        n = last - first
        if n <= 0:
            continue
        freq = low + zlib.crc32(f"{seed}:{k}".encode()) % (high - low + 1)
        step = 2 * math.pi * freq / RATE
        burst = [peak * math.sin(step * i) for i in range(n)]
        ramp = max(1, min(fade, n // 2))
        for i in range(ramp):
            burst[i] *= i / ramp
            burst[n - 1 - i] *= i / ramp
        samples[first:last] = array.array("h", (int(value) for value in burst))
    if sys.byteorder == "big":  # wav is little-endian
        samples.byteswap()
    return samples.tobytes()
