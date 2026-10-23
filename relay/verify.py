"""Brief Appendix F checks against the live Higgs API (design §11). Needs BOSON_API_KEY in .env; never prints it.

    uv run python -m relay.verify voices [--voices chloe,marcus,eleanor] [--line TEXT]   # F1 voice match, by ear
    uv run python -m relay.verify tags [--voice chloe]                                  # F3 tags + timestamps
    uv run python -m relay.verify latency [--runs 5] [--voice chloe]                    # F2, server side
    uv run python -m relay.verify session [--talk talks/relay-intro]                    # F6 partial session.update
    uv run python -m relay.verify tools [--talk talks/relay-intro] [--runs 10]          # F5 tool reliability
    uv run python -m relay.verify idle [--wait 330] [--voice chloe]                     # F7 idle timeout + recovery
    uv run python -m relay.verify all [--voice V] [--talk PATH]                         # voices tags latency session tools

Each check prints a result block that ends with a dated line to paste into doc/gotchas.md, and writes its audio (plus
raw acks / turn logs) under out/verify/<check>/. Status: PASS / FAIL, or LISTEN when a human decides by ear. Exit
code: 0 = every check ran and none failed, 1 = a check failed, 2 = no key or bad arguments. All network access goes
through `Ctx`, so the tests drive every check with fakes.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import difflib
import inspect
import io
import json
import math
import statistics
import struct
import sys
import textwrap
import time
import traceback
import wave
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

import requests
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from .config import PRESET_VOICES, RATE, REALTIME_MODEL, ROOT, STT_MODEL, TTS_MODEL, Settings, load_settings
from .talk import MAX_SAY_CHARS, Slide, Talk, TalkError, load_talk
from .textutil import find_tags, has_digits, norm_word, split_sentences, strip_tags, word_count

CREDITS_URL = "https://www.boson.ai/workspace/api-key?open-credits=true"
NO_KEY_MESSAGE = f"BOSON_API_KEY missing in .env: add it and claim credits at {CREDITS_URL}"

DEFAULT_VOICE = "chloe"
DEFAULT_VOICES = ("chloe", "marcus", "eleanor")
DEFAULT_TALK = "talks/relay-intro"
FALLBACK_TALK = ROOT / "talks" / "test"
DEFAULT_LINE = ("Here's a pattern you've all seen. An engineer spends three months building something brilliant, "
                "then gets five minutes to explain it.")
# Two leading delivery tags and one positional pause between "works." and "Every" (no tag names in the words).
TAGGED_TEXT = ("<|emotion:enthusiasm|><|prosody:expressive_high|>Here is how this works. <|prosody:pause|> "
               "Every single word comes with a timestamp, so I always know where I am.")
REPEAT_INSTRUCTIONS = ("You read text aloud. When the user sends a message, say exactly that message, word for word, "
                       "in a natural speaking voice. Do not add, change, answer or comment on anything.")
CHAT_INSTRUCTIONS = "You are a concise voice assistant. Reply in one short spoken sentence."
LATENCY_PROMPTS = (
    "Say hello in one short sentence.",
    "Name one colour in one short sentence.",
    "Say good morning in one short sentence.",
    "Name one fruit in one short sentence.",
    "Say thank you in one short sentence.",
)
RECOVERY_PROMPT = "Say that you are back online, in one short sentence."
TOOL_NAMES = ("start_presentation", "resume_script", "goto_slide", "next_slide", "handoff_to_human")

AB_GAP_S = 0.7  # silence between the TTS clip and the Realtime clip of one voice
VOICE_GAP_S = 1.5  # silence between voices in all_voices.wav
LEVEL_GAP_DB = 3.0  # a loudness jump bigger than this can pass for a voice change: say so
CONNECT_TIMEOUT_S = 10.0
ACK_TIMEOUT_S = 10.0
TURN_TIMEOUT_S = 30.0
PROGRESS_EVERY_S = 30.0
MAX_EVENT_LINES = 30  # idle: events printed in the result block (all of them go to events.json)
VERBATIM_MATCH = 0.9  # share of the line's words the Realtime transcript must contain to count as "verbatim"
TOOL_PASS_RATE = 0.9  # brief F5: ≥ 9 of 10 turns
TARGET_FIRST_AUDIO_MS = 1500  # brief F2: end of speech → first reply audio (typed turns are a lower bound)
TARGET_RECONNECT_MS = 2000  # brief F2
PAUSE_MIN_EXTRA_MS = 150  # a working <|prosody:pause|> should widen the gap at least this much over no tag

ERROR_HINTS = {
    "insufficient_quota": f"billing: claim credits at {CREDITS_URL}",
    "rate_limit_error": "transient: retry with backoff",
    "authentication_error": "check BOSON_API_KEY in .env",
}
FATAL_ERROR_TYPES = frozenset({"insufficient_quota", "authentication_error"})
CLOSE_HINTS = {
    1000: "normal close (idle timeout or max duration when their events came first)",
    1013: "server concurrency cap: retry later with backoff",
    3000: "invalid or expired key: check BOSON_API_KEY in .env",
    4429: f"billing refused: claim credits at {CREDITS_URL}",
}
FATAL_CLOSE_CODES = frozenset({3000, 4429})

TITLES = {
    "voices": "F1 voice match (TTS 3 vs Realtime)",
    "latency": "F2 latency (server side)",
    "tags": "F3 tags + timestamps",
    "tools": "F5 tool reliability",
    "session": "F6 instructions-only session.update",
    "idle": "F7 idle timeout + recovery",
}
ALL_CHECKS = ("voices", "tags", "latency", "session", "tools")  # `all`: everything but the 5-minute idle wait


class VerifyError(Exception):
    """A check could not finish. `fatal` (billing / bad key) stops `all`: every later call would fail the same way."""

    def __init__(self, message: str, *, hint: str = "", fatal: bool = False):
        super().__init__(f"{message} → {hint}" if hint else message)
        self.hint = hint
        self.fatal = fatal


class VerifyTimeout(VerifyError):
    """The awaited event did not arrive in time (the socket is still open)."""


def _echo(text: str) -> None:
    print(text, flush=True)


@dataclass
class Ctx:
    """Everything a check touches outside the process, so tests can swap the network for fakes."""

    settings: Settings
    connect: Callable[..., Awaitable[Any]] = field(default_factory=lambda: websockets.connect)
    post: Callable[..., Any] = field(default_factory=lambda: requests.post)
    clock: Callable[[], float] = time.perf_counter
    echo: Callable[[str], None] = _echo

    @property
    def out_dir(self) -> Path:
        return self.settings.out_dir / "verify"


# ── results ──────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Result:
    check: str
    status: str  # PASS | FAIL | LISTEN (a human decides by ear)
    lines: list[str] = field(default_factory=list)
    summary: str = ""
    files: list[Path] = field(default_factory=list)
    fatal: bool = False

    @property
    def title(self) -> str:
        return TITLES.get(self.check, self.check)


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def gotcha_line(result: Result, now: datetime) -> str:
    return (f"- **{result.title}** ({now:%Y-%m-%d %H:%M}, `python -m relay.verify {result.check}`): "
            f"{result.status}. {result.summary}").rstrip()


def format_result(result: Result, now: datetime) -> str:
    out = [f"== {result.title}: {result.status} =="]
    out += [f"  {line}" for line in result.lines]
    out += [f"  wrote {rel(path)}" for path in result.files]
    out += ["paste into doc/gotchas.md:", gotcha_line(result, now)]
    return "\n".join(out)


def format_overview(results: Sequence[Result], now: datetime) -> str:
    out = ["== overview =="]
    out += [f"  {result.check:<8} {result.status}" for result in results]
    out += ["paste into doc/gotchas.md (all of the above):"]
    out += [gotcha_line(result, now) for result in results]
    return "\n".join(out)


def describe_ms(values: Sequence[int]) -> str:
    if not values:
        return "n/a"
    return f"median {statistics.median(values):.0f} ms (min {min(values)}, max {max(values)}, n={len(values)})"


def seconds(ms: int) -> str:
    return f"{ms / 1000:.1f} s"


def _ms(delta_s: float) -> int:
    return round(delta_s * 1000)


def _clip(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def word_match(expected: str, heard: str) -> float:
    """Share of words two texts have in common, in order (1.0 = the same words)."""
    a = [norm_word(word) for word in strip_tags(expected).split() if norm_word(word)]
    b = [norm_word(word) for word in heard.split() if norm_word(word)]
    if not a or not b:
        return 1.0 if a == b else 0.0
    matched = sum(block.size for block in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks())
    return matched / max(len(a), len(b))


# ── audio: WAV in, PCM16 mono 24 kHz out ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WavAudio:
    rate: int
    channels: int
    sample_width: int  # bytes per sample
    format_tag: int  # 1 = integer PCM, 3 = IEEE float
    data: bytes

    def describe(self) -> str:
        kind = "float" if self.format_tag == 3 else "PCM" if self.format_tag == 1 else f"format {self.format_tag}"
        channels = "mono" if self.channels == 1 else f"{self.channels} ch"
        return f"wav {self.rate} Hz {channels} {8 * self.sample_width}-bit {kind}"


def parse_wav(blob: bytes) -> WavAudio:
    """A RIFF/WAVE file, read leniently: a data size of 0, 0xFFFFFFFF or past the end means "to the end"."""
    if len(blob) < 12 or blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    fmt: bytes | None = None
    data: bytes | None = None
    pos = 12
    while pos + 8 <= len(blob) and data is None:
        chunk_id, size = blob[pos:pos + 4], int.from_bytes(blob[pos + 4:pos + 8], "little")
        start = pos + 8
        if chunk_id == b"data":
            data = blob[start:start + size] if 0 < size and start + size <= len(blob) else blob[start:]
        elif chunk_id == b"fmt ":
            fmt = blob[start:start + size]
        pos = start + size + (size & 1)
    if fmt is None or len(fmt) < 16:
        raise ValueError("no fmt chunk")
    if data is None:
        raise ValueError("no data chunk")
    tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", fmt[:16])
    if tag == 0xFFFE and len(fmt) >= 26:  # WAVE_FORMAT_EXTENSIBLE: the real format opens the sub-format GUID
        tag = int.from_bytes(fmt[24:26], "little")
    if channels < 1 or rate < 1 or bits < 8 or bits % 8:
        raise ValueError(f"odd header: {channels} channel(s), {rate} Hz, {bits} bits")
    return WavAudio(rate=rate, channels=channels, sample_width=bits // 8, format_tag=tag, data=data)


def _native(typecode: str, data: bytes) -> array.array:
    values = array.array(typecode)
    values.frombytes(data)
    if sys.byteorder == "big":
        values.byteswap()
    return values


def _le_bytes(values: array.array) -> bytes:
    if sys.byteorder == "big":
        values = array.array(values.typecode, values)
        values.byteswap()
    return values.tobytes()


def _clip16(value: float) -> int:
    return max(-32768, min(32767, round(value)))


def _samples16(audio: WavAudio) -> array.array:
    """Interleaved samples scaled to int16."""
    width, tag = audio.sample_width, audio.format_tag
    data = audio.data[:len(audio.data) - len(audio.data) % width]
    if tag == 1 and width == 2:
        return _native("h", data)
    if tag == 1 and width == 1:  # 8-bit WAV is unsigned
        return array.array("h", ((byte - 128) << 8 for byte in data))
    if tag == 1 and width == 3:
        return array.array("h", (int.from_bytes(data[i:i + 3], "little", signed=True) >> 8
                                 for i in range(0, len(data), 3)))
    if tag == 1 and width == 4:
        return array.array("h", (value >> 16 for value in _native("i", data)))
    if tag == 3 and width in (4, 8):
        return array.array("h", (_clip16(value * 32767) for value in _native("f" if width == 4 else "d", data)))
    raise ValueError(f"unsupported sample format ({audio.describe()})")


def _downmix(samples: array.array, channels: int) -> array.array:
    if channels == 1:
        return samples
    frames = len(samples) // channels
    return array.array("h", (sum(samples[f * channels:(f + 1) * channels]) // channels for f in range(frames)))


def resample(samples: Sequence[int], src_rate: int, dst_rate: int = RATE) -> array.array:
    """Linear interpolation without a low-pass filter: good enough for an audition file, nothing the model hears."""
    if src_rate == dst_rate or not samples:
        return array.array("h", samples)
    count = max(1, round(len(samples) * dst_rate / src_rate))
    step, last = src_rate / dst_rate, len(samples) - 1
    out = array.array("h", bytes(2 * count))
    for i in range(count):
        x = i * step
        k = int(x)
        a, b = samples[min(k, last)], samples[min(k + 1, last)]
        out[i] = _clip16(a + (b - a) * (x - k))
    return out


def to_pcm16_mono(audio: WavAudio, rate: int = RATE) -> tuple[bytes, list[str]]:
    """PCM16 mono at `rate`, plus a note when anything had to be converted."""
    if (audio.format_tag, audio.sample_width, audio.channels, audio.rate) == (1, 2, 1, rate):
        return audio.data[:len(audio.data) - len(audio.data) % 2], []
    samples = resample(_downmix(_samples16(audio), audio.channels), audio.rate, rate)
    return _le_bytes(samples), [f"converted {audio.describe()} → {rate} Hz mono 16-bit (linear, for listening only)"]


def silence(duration_s: float, rate: int = RATE) -> bytes:
    return bytes(2 * round(duration_s * rate))


def pcm_ms(pcm: bytes, rate: int = RATE) -> int:
    return round(len(pcm) * 1000 / (2 * rate))


def level_dbfs(pcm: bytes) -> float | None:
    """RMS level of PCM16 audio in dBFS; None for no audio or digital silence."""
    samples = _native("h", pcm[:len(pcm) - len(pcm) % 2])
    if not samples:
        return None
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    return 20 * math.log10(rms / 32768) if rms > 0 else None


def describe_level(pcm: bytes) -> str:
    level = level_dbfs(pcm)
    return "silent" if level is None else f"{level:.1f} dBFS"


def wav_bytes(pcm: bytes, rate: int = RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm[:len(pcm) - len(pcm) % 2])
    return buffer.getvalue()


def write_wav(path: Path, pcm: bytes, rate: int = RATE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_bytes(pcm, rate))
    return path


# ── errors from the API ──────────────────────────────────────────────────────────────────────────────────────


def error_kind(err: Any) -> str | None:
    """An API error's `type`; its `code` instead when only the code names a known case (e.g. insufficient_quota)."""
    if not isinstance(err, dict):
        return None
    kind, code = err.get("type"), err.get("code")
    return code if kind not in ERROR_HINTS and code in ERROR_HINTS else kind


def http_error(what: str, resp: Any) -> VerifyError:
    """A failed REST call → message + hint (billing vs rate limit vs key), like scripts/smoke/_common.py."""
    try:
        err = resp.json().get("error") or {}
    except (ValueError, AttributeError):
        err = {}
    if not isinstance(err, dict):
        err = {"message": str(err)}
    kind = error_kind(err)
    detail = err.get("message") or (getattr(resp, "text", "") or "")[:200]
    hint = ERROR_HINTS.get(kind, "") or {401: "check BOSON_API_KEY in .env",
                                         403: "the key may lack access to this model"}.get(resp.status_code, "")
    label = "/".join(str(part) for part in dict.fromkeys((err.get("type"), err.get("code"))) if part)
    return VerifyError(f"{what}: HTTP {resp.status_code}{' ' + label if label else ''}: {detail}", hint=hint,
                       fatal=kind in FATAL_ERROR_TYPES or resp.status_code == 401)


def realtime_error(event: dict) -> VerifyError:
    err = event.get("error") or {}
    if not isinstance(err, dict):
        err = {"message": str(err)}
    kind = error_kind(err)
    return VerifyError(f"Realtime error {err.get('type')}/{err.get('code')}: {err.get('message')}",
                       hint=ERROR_HINTS.get(kind, ""), fatal=kind in FATAL_ERROR_TYPES)


def closed_error(code: int | None, reason: str = "") -> VerifyError:
    detail = f" {reason!r}" if reason else ""
    return VerifyError(f"Realtime closed the socket (code {code}{detail})", hint=CLOSE_HINTS.get(code or 0, ""),
                       fatal=code in FATAL_CLOSE_CODES)


def handshake_error(exc: InvalidStatus) -> VerifyError:
    status = exc.response.status_code
    try:
        err = json.loads(exc.response.body or b"{}").get("error") or {}
    except (ValueError, AttributeError):
        err = {}
    kind = error_kind(err)
    hint = ERROR_HINTS.get(kind, "") or {401: "check BOSON_API_KEY in .env",
                                         429: f"billing or rate limit: claim credits at {CREDITS_URL}"}.get(status, "")
    return VerifyError(f"Realtime refused the WebSocket upgrade: HTTP {status}{' ' + kind if kind else ''}",
                       hint=hint, fatal=status in (401, 403) or kind in FATAL_ERROR_TYPES)


# ── TTS 3 ────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimedWord:
    word: str
    start: int  # ms
    end: int


def parse_timestamps(raw: Any) -> list[TimedWord] | None:
    """TTS `timestamps` (seconds) → ms. None stays None (alignment unavailable); malformed entries are skipped."""
    if raw is None:
        return None
    words = []
    for item in raw if isinstance(raw, list) else []:
        try:
            words.append(TimedWord(str(item["word"]), round(float(item["start"]) * 1000),
                                   round(float(item["end"]) * 1000)))
        except (KeyError, TypeError, ValueError):
            continue
    return words


@dataclass
class TtsResult:
    pcm: bytes  # PCM16 mono 24 kHz
    words: list[TimedWord] | None
    raw_timestamps: Any
    elapsed_ms: int
    source: str  # format of the WAV as returned
    notes: list[str]

    @property
    def audio_ms(self) -> int:
        return pcm_ms(self.pcm)


def tts_body(text: str, voice: str) -> dict:
    """The render's request (design §2): wav + word timestamps."""
    return {"model": TTS_MODEL, "input": text, "voice": voice, "response_format": "wav", "timestamps": True}


async def tts_request(ctx: Ctx, text: str, voice: str) -> TtsResult:
    url = f"{ctx.settings.base_url}/v1/audio/speech"
    started = ctx.clock()
    try:
        resp = await asyncio.to_thread(ctx.post, url, headers=ctx.settings.headers, json=tts_body(text, voice),
                                       timeout=120)
    except requests.RequestException as exc:
        raise VerifyError(f"TTS request failed: {exc}", hint="check the network / hotspot") from None
    elapsed = _ms(ctx.clock() - started)
    if not resp.ok:
        raise http_error("TTS", resp)
    try:
        payload = resp.json()
        blob = base64.b64decode(payload["audio"])
    except (ValueError, KeyError, TypeError) as exc:
        kind = (getattr(resp, "headers", None) or {}).get("content-type", "?")
        raise VerifyError(f"TTS answered {kind} without a base64 `audio` field ({exc})") from None
    try:
        audio = parse_wav(blob)
        pcm, notes = to_pcm16_mono(audio)
    except ValueError as exc:
        raise VerifyError(f"TTS audio is not a WAV this script can read: {exc}") from None
    raw = payload.get("timestamps")
    return TtsResult(pcm, parse_timestamps(raw), raw, elapsed, audio.describe(), notes)


# ── Realtime ─────────────────────────────────────────────────────────────────────────────────────────────────


def realtime_url(settings: Settings) -> str:
    return f"{settings.realtime_url}?model={REALTIME_MODEL}"


def session_config(settings: Settings, *, voice: str, instructions: str, tools: Sequence[dict] = ()) -> dict:
    """The app's full session object (design §6) with this voice, instructions and tools."""
    config = {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "instructions": instructions,
        "output_modalities": ["audio"],
        "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": RATE},
                      "transcription": {"model": STT_MODEL, "language": "en"},
                      "turn_detection": settings.session_turn_detection},
            "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": voice},
        },
        "tools": list(tools),
        "tool_choice": "auto",
    }
    if settings.temperature is not None:
        config["temperature"] = settings.temperature
    return config


def per_turn_session(settings: Settings, full: dict, instructions: str) -> dict:
    """What the app sends on every live turn: instructions only, or the full config if RELAY_FULL_SESSION_UPDATE=1."""
    if settings.full_session_update:
        return {**full, "instructions": instructions}
    return {"type": "realtime", "instructions": instructions}


ABSENT = "(not in the ack)"


def _pick(source: Any, key: str) -> Any:
    return source.get(key, ABSENT) if isinstance(source, dict) else ABSENT


def session_fields(session: dict) -> dict:
    """The parts of a session ack that a partial update must not reset (ABSENT where the ack leaves one out)."""
    audio = _pick(session, "audio")
    audio_in, audio_out = _pick(audio, "input"), _pick(audio, "output")
    tools = _pick(session, "tools")
    return {"voice": _pick(audio_out, "voice"), "turn_detection": _pick(audio_in, "turn_detection"),
            "transcription": _pick(audio_in, "transcription"),
            "tools": len(tools) if isinstance(tools, list) else 0 if tools is None else ABSENT,
            "output_modalities": _pick(session, "output_modalities")}


def _essentials(fields: dict) -> dict:
    """session_fields() without server-filled defaults: VAD type, transcription model."""
    detection, transcription = fields["turn_detection"], fields["transcription"]
    return {**fields,
            "turn_detection": detection.get("type") if isinstance(detection, dict) else detection,
            "transcription": transcription.get("model") if isinstance(transcription, dict) else transcription}


def diff_fields(before: dict, after: dict) -> list[str]:
    """`key: old → new` for every field that differs; a field either side leaves out is not a difference."""
    return [f"{key}: {before.get(key)!r} → {after.get(key)!r}" for key in before
            if ABSENT not in (before.get(key), after.get(key)) and before.get(key) != after.get(key)]


def absent_fields(fields: dict) -> list[str]:
    return [key for key, value in fields.items() if value == ABSENT]


def user_message(text: str) -> dict:
    return {"type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


def tool_output(call_id: str, output: dict) -> dict:
    return {"type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": json.dumps(output)}}


async def send_event(conn: Any, event: dict) -> None:
    await conn.send(json.dumps(event))


async def send_or_fail(conn: Any, event: dict) -> None:
    try:
        await send_event(conn, event)
    except ConnectionClosed as exc:
        raise closed_error(*close_info(exc, conn)) from None


def decode_event(raw: str | bytes) -> dict:
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    try:
        event = json.loads(raw)
    except ValueError:
        return {"type": "(not json)", "raw": raw[:200]}
    return event if isinstance(event, dict) else {"type": "(not an object)"}


def brief_event(event: dict) -> dict:
    """An event without bulky payloads, for logs: audio → its length, a session → its key fields."""
    out: dict = {}
    for key, value in event.items():
        if key in ("delta", "audio") and isinstance(value, str) and str(event.get("type", "")).endswith("audio.delta"):
            out[key] = f"<{len(value)} base64 chars>"
        elif key == "session" and isinstance(value, dict):
            out[key] = session_fields(value)
        else:
            out[key] = value
    return out


def close_info(exc: ConnectionClosed | None, conn: Any = None) -> tuple[int | None, str]:
    """(code, reason) of a closed socket: the close frame received, else what the connection recorded."""
    frame = getattr(exc, "rcvd", None)
    if frame is not None:
        return frame.code, frame.reason or ""
    return getattr(conn, "close_code", None), getattr(conn, "close_reason", None) or ""


async def close_quietly(conn: Any) -> None:
    try:
        await conn.close()
    except Exception:  # best effort: the result is already known
        pass


async def wait_for_event(conn: Any, wanted: set[str] | frozenset[str], *, timeout: float,
                         seen: list[dict] | None = None) -> dict:
    """Read until an event of a `wanted` type. An `error` event or a close fails the wait (the session is gone)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise VerifyTimeout(f"no {' / '.join(sorted(wanted))} within {max(timeout, 0):g} s")
        try:
            raw = await asyncio.wait_for(conn.recv(), remaining)
        except TimeoutError:
            continue
        except ConnectionClosed as exc:
            raise closed_error(*close_info(exc, conn)) from None
        event = decode_event(raw)
        if seen is not None:
            seen.append(event)
        if event.get("type") == "error":
            raise realtime_error(event)
        if event.get("type") in wanted:
            return event


@dataclass
class Opened:
    conn: Any
    session: dict  # session.created's resolved config
    handshake_ms: int  # TCP + TLS + WebSocket upgrade
    created_ms: int  # first session.update sent → session.created
    events: list[dict]  # everything received up to session.created

    @property
    def total_ms(self) -> int:
        return self.handshake_ms + self.created_ms


async def open_session(ctx: Ctx, config: dict) -> Opened:
    """Connect, send the full session.update, wait for session.created (the server sends nothing before it)."""
    started = ctx.clock()
    try:
        conn = await ctx.connect(realtime_url(ctx.settings), additional_headers=ctx.settings.headers,
                                 max_size=None, open_timeout=CONNECT_TIMEOUT_S)
    except InvalidStatus as exc:
        raise handshake_error(exc) from None
    except (OSError, TimeoutError, WebSocketException) as exc:
        raise VerifyError(f"cannot reach {ctx.settings.realtime_url}: {str(exc) or type(exc).__name__}",
                          hint="check the network / hotspot") from None
    connected = ctx.clock()
    events: list[dict] = []
    try:
        await send_or_fail(conn, {"type": "session.update", "session": config})
        created = await wait_for_event(conn, {"session.created"}, timeout=ACK_TIMEOUT_S, seen=events)
    except BaseException:
        await close_quietly(conn)
        raise
    return Opened(conn, created.get("session") or {}, _ms(connected - started), _ms(ctx.clock() - connected), events)


@dataclass
class ToolCallSeen:
    call_id: str
    name: str
    arguments: str  # JSON string, as the server sent it

    @property
    def args(self) -> dict:
        try:
            value = json.loads(self.arguments or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def __str__(self) -> str:
        return f"{self.name}({', '.join(f'{key}={value!r}' for key, value in self.args.items())})"


@dataclass
class TurnResult:
    """Everything one typed turn brought back. Times are ms after `response.create` was sent."""

    text: str
    pcm: bytearray = field(default_factory=bytearray)
    transcript_parts: list[str] = field(default_factory=list)
    transcript_done: str | None = None
    calls: list[ToolCallSeen] = field(default_factory=list)
    response_id: str | None = None
    status: str | None = None  # response.done status: completed | cancelled; None = never finished
    created_ms: int | None = None
    first_audio_ms: int | None = None
    first_text_ms: int | None = None
    done_ms: int | None = None
    acks: list[dict] = field(default_factory=list)  # session.updated sessions seen during the turn
    errors: list[dict] = field(default_factory=list)
    events: Counter = field(default_factory=Counter)
    timed_out: bool = False
    closed: bool = False
    close_code: int | None = None
    close_reason: str = ""

    @property
    def transcript(self) -> str:
        return self.transcript_done if self.transcript_done is not None else "".join(self.transcript_parts)

    @property
    def audio_ms(self) -> int:
        return pcm_ms(bytes(self.pcm))

    @property
    def fatal(self) -> bool:
        return self.closed and self.close_code in FATAL_CLOSE_CODES

    def problem(self) -> str:
        """Why the turn did not finish cleanly ("" if it did)."""
        errors = "; ".join(f"{err.get('type')}: {err.get('message')}" for err in self.errors)
        if self.closed:
            reason = f" {self.close_reason!r}" if self.close_reason else ""
            hint = CLOSE_HINTS.get(self.close_code or 0, "")
            return f"socket closed (code {self.close_code}{reason})" + (f" → {hint}" if hint else "")
        if self.timed_out:
            return "no response.done before the timeout" + (f" (error event: {errors})" if errors else "")
        if self.status not in (None, "completed"):
            return f"response {self.status}"
        if errors:
            return "error event: " + errors
        return ""


def _b64(value: Any) -> bytes:
    try:
        return base64.b64decode(value or "")
    except (ValueError, TypeError):
        return b""


def _ours(result: TurnResult, response_id: Any) -> bool:
    return not response_id or not result.response_id or response_id == result.response_id


def _call_key(call: ToolCallSeen) -> str:
    return call.call_id or f"{call.name}:{call.arguments}"


def _add_call(result: TurnResult, source: dict) -> None:
    """Register a call once: it is announced by function_call_arguments.done and again in response.done.

    The second announcement fills in what the first left out (a name, the arguments).
    """
    arguments = source.get("arguments")
    call = ToolCallSeen(str(source.get("call_id") or ""), str(source.get("name") or ""),
                        arguments if isinstance(arguments, str) else json.dumps(arguments or {}))
    for seen in result.calls:
        if _call_key(seen) == _call_key(call):
            seen.name = seen.name or call.name
            if seen.arguments.strip() in ("", "{}"):
                seen.arguments = call.arguments
            return
    result.calls.append(call)


def _absorb(result: TurnResult, event: dict, elapsed: int) -> None:
    kind = str(event.get("type", ""))
    result.events[kind] += 1
    if kind == "session.updated":
        result.acks.append(event.get("session") or {})
    elif kind == "error":
        result.errors.append(event.get("error") if isinstance(event.get("error"), dict) else {})
        if error_kind(result.errors[-1]) in FATAL_ERROR_TYPES:
            raise realtime_error(event)
    elif kind == "response.created":
        if result.response_id is None:
            result.response_id = str((event.get("response") or {}).get("id") or "")
            result.created_ms = elapsed
    elif kind == "response.output_audio.delta":
        if _ours(result, event.get("response_id")):
            if result.first_audio_ms is None:
                result.first_audio_ms = elapsed
            result.pcm.extend(_b64(event.get("delta")))
    elif kind == "response.output_audio_transcript.delta":
        if _ours(result, event.get("response_id")):
            if result.first_text_ms is None:
                result.first_text_ms = elapsed
            result.transcript_parts.append(str(event.get("delta") or ""))
    elif kind == "response.output_audio_transcript.done":
        if _ours(result, event.get("response_id")):
            result.transcript_done = str(event.get("transcript") or "")
    elif kind == "response.function_call_arguments.done":
        if _ours(result, event.get("response_id")):
            _add_call(result, event)
    elif kind == "response.done":
        response = event.get("response") or {}
        if _ours(result, response.get("id")):
            for item in response.get("output") or []:
                if isinstance(item, dict) and item.get("type") == "function_call":
                    _add_call(result, item)
            result.status = str(response.get("status") or "completed")
            result.done_ms = elapsed


async def typed_turn(conn: Any, text: str, *, session: dict | None = None, timeout: float = TURN_TIMEOUT_S,
                     clock: Callable[[], float] = time.perf_counter,
                     on_event: Callable[[int, dict], None] | None = None) -> TurnResult:
    """One typed user turn sent the way the app sends it (design §5.1 `typed`), and everything the reply brings.

    Sends `session.update` (when `session` is given), `conversation.item.create` and `response.create`, then reads
    until that response's `response.done`, a timeout or a close. An `insufficient_quota` / `authentication_error`
    event raises a fatal VerifyError. Never sends anything after the reply (tool outputs are the caller's job).
    """
    result = TurnResult(text=text)
    try:
        if session is not None:
            await send_event(conn, {"type": "session.update", "session": session})
        await send_event(conn, user_message(text))
        await send_event(conn, {"type": "response.create"})
    except ConnectionClosed as exc:
        result.closed = True
        result.close_code, result.close_reason = close_info(exc, conn)
        return result
    sent = clock()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while result.status is None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            result.timed_out = True
            break
        try:
            raw = await asyncio.wait_for(conn.recv(), remaining)
        except TimeoutError:
            result.timed_out = True
            break
        except ConnectionClosed as exc:
            result.closed = True
            result.close_code, result.close_reason = close_info(exc, conn)
            break
        event = decode_event(raw)
        elapsed = _ms(clock() - sent)
        if on_event is not None:
            on_event(elapsed, event)
        _absorb(result, event, elapsed)
    return result


# ── talk + scripted turns for F5 / F6 ────────────────────────────────────────────────────────────────────────


def resolve_talk(arg: str) -> tuple[Talk, str]:
    """(talk, note): `arg`, or talks/test when `arg` is missing or fails validation (the note says which)."""
    path = next((candidate for candidate in (Path(arg), ROOT / arg) if candidate.is_dir()), None)
    if path is None:
        reason = "not found"
    else:
        try:
            return load_talk(path), ""
        except TalkError as exc:
            errors = sum(issue.level == "error" for issue in exc.issues)
            reason = f"fails validation ({errors} error(s): `uv run python -m relay.talk {arg}`)"
        if path.resolve() == FALLBACK_TALK.resolve():
            raise VerifyError(f"{arg} {reason}")
    return load_talk(FALLBACK_TALK), f"{arg} {reason} → using talks/test"


@dataclass(frozen=True)
class Scenario:
    name: str  # opening | comment | plain | redirect | question
    phase: str  # TurnContext.phase
    slide_id: str
    say: str
    expect: str  # tool the turn should end with
    expect_slide: str | None = None  # goto_slide target
    also: tuple[str, ...] = ()  # equivalent calls (next_slide when the target is the next slide)
    cut_word: str | None = None
    cut_sentence: str | None = None

    def want(self) -> str:
        args = f"slide_id={self.expect_slide!r}" if self.expect_slide else ""
        return f"{self.expect}({args})" + "".join(f" or {name}()" for name in self.also)

    def accepts(self, checked: CheckedCall | None) -> bool:
        """Would the app end this turn the intended way? `checked` = the call it would apply (the last valid one)."""
        if checked is None or checked.error is not None:
            return False
        name = checked.call.name
        if name in self.also:
            return True
        return name == self.expect and (self.expect_slide is None or checked.args.get("slide_id") == self.expect_slide)


def cut_in(slide: Slide) -> tuple[str | None, str | None]:
    """A plausible cut: two thirds into the first sentence of the slide's second chunk (or of its only chunk)."""
    if not slide.chunks:
        return None, None
    sentences = split_sentences(slide.chunks[min(1, len(slide.chunks) - 1)].text)
    if not sentences:
        return None, None
    words = [word for word in sentences[0].split() if norm_word(word)]
    word = words[2 * len(words) // 3].strip(".,;:!?…\"'“”‘’()") if words else ""
    return word or None, sentences[0]


def spoken_name(slide: Slide) -> str:
    if slide.id == "demo":
        return "the demo"
    if slide.is_qa:
        return "the questions"
    return f"the {(slide.title or slide.id).rstrip('?!.:')} slide"


def build_scenarios(talk: Talk) -> list[Scenario]:
    """Brief Appendix A's live turns, mapped onto whatever slides `talk` has."""
    relay = talk.relay_slides
    talking = [slide for slide in relay if not slide.is_qa] or relay
    by_id = {slide.id: slide for slide in talking}
    comment = talking[0]
    plain = by_id.get("how") or next((slide for slide in talking if slide.plain), comment)
    source = by_id.get("details") or plain
    qa = next((slide for slide in relay if slide.is_qa), None)
    target = (next((slide for slide in relay if slide.id == "demo"), None) or qa
              or next((slide for slide in reversed(relay) if slide.id != source.id), source))
    following = talk.slides[source.index + 1] if source.index + 1 < len(talk.slides) else None
    scenarios = [
        Scenario("opening", "opening", talk.slides[0].id, "So that's why I built a co-presenter. Relay, take it away.",
                 "start_presentation"),
        Scenario("comment", "presenting", comment.id, "Quick comment: that was literally my last demo day.",
                 "resume_script", None, (), *cut_in(comment)),
        Scenario("plain", "presenting", plain.id, "Sorry, can you explain that like I'm not an engineer?",
                 "resume_script", None, (), *cut_in(plain)),
        Scenario("redirect", "presenting", source.id, f"That's a lot of detail, let's skip to {spoken_name(target)}.",
                 "goto_slide", target.id, ("next_slide",) if following is not None and following.id == target.id
                 else (), *cut_in(source)),
    ]
    if qa is not None:
        scenarios.append(Scenario("question", "qa", qa.id,
                                  "How do you know where to pick up after someone interrupts you?", "handoff_to_human"))
    return scenarios


@dataclass(frozen=True)
class CheckedCall:
    """A call as the app would take it."""

    call: ToolCallSeen
    error: str | None  # None = valid; else the error text its function_call_output carries
    args: dict  # the arguments the app acts on (relay.tools resolves slide ids)

    def __str__(self) -> str:
        return str(self.call) + (f" [rejected: {self.error}]" if self.error else "")


def local_call_error(call: ToolCallSeen, talk: Talk) -> str | None:
    if call.name not in TOOL_NAMES:
        return f"unknown tool '{call.name}'"
    if call.name == "goto_slide" and call.args.get("slide_id") not in {slide.id for slide in talk.relay_slides}:
        return f"unknown slide_id '{call.args.get('slide_id')}'"
    return None


def _run_parse_call(parse_call: Callable[..., Any], call: ToolCallSeen, talk: Talk) -> Any:
    """design §7 says parse_call(name, arguments, talk); relay.tools also takes the call_id first. Accept both."""
    try:
        takes_call_id = "call_id" in inspect.signature(parse_call).parameters
    except (TypeError, ValueError):
        takes_call_id = False
    if takes_call_id:
        return parse_call(call.call_id, call.name, call.arguments, talk)
    return parse_call(call.name, call.arguments, talk)


def check_call(call: ToolCallSeen, talk: Talk, parse_call: Callable[..., Any] | None = None) -> CheckedCall:
    """How the app would take `call`: relay.tools.parse_call when it imports and runs, else a local check."""
    if parse_call is not None:
        try:
            parsed = _run_parse_call(parse_call, call, talk)
        except Exception:  # drift in a module written in parallel must not kill a live check
            parsed = None
        if isinstance(parsed, str):
            return CheckedCall(call, parsed, call.args)
        if parsed is not None:
            args = getattr(parsed, "args", None)
            return CheckedCall(call, None, args if isinstance(args, dict) else call.args)
    return CheckedCall(call, local_call_error(call, talk), call.args)


def app_tool_output(checked: CheckedCall, talk: Talk, slide_id: str) -> dict:
    """The function_call_output the app would send after the action ran (design §5.3); `slide_id` = slide on screen."""
    if checked.error is not None:
        return {"error": checked.error}
    name = checked.call.name
    if name == "start_presentation":
        return {"ok": True, "slide": talk.relay_slides[0].id}
    if name == "goto_slide":
        return {"ok": True, "slide": checked.args.get("slide_id")}
    if name == "next_slide":
        index = talk.index_of(slide_id) + 1
        return {"ok": True, "slide": talk.slides[index].id} if index < len(talk.slides) else {"error": "no next slide"}
    if name == "resume_script":
        return {"ok": True, "slide": slide_id}
    return {"ok": True}


def said_tool_name(transcript: str) -> bool:
    spoken = transcript.lower()
    return any(name in spoken or name.replace("_", " ") in spoken for name in TOOL_NAMES)


# ── checks ───────────────────────────────────────────────────────────────────────────────────────────────────


def realtime_pcm(turn: TurnResult, session: dict) -> tuple[bytes, list[str]]:
    """The reply audio at 24 kHz, resampled if the session ack reports another output rate."""
    output = (session.get("audio") or {}).get("output") or {}
    rate = (output.get("format") or {}).get("rate")
    pcm = bytes(turn.pcm[:len(turn.pcm) - len(turn.pcm) % 2])
    if not isinstance(rate, int) or rate == RATE:
        return pcm, []
    audio = WavAudio(rate=rate, channels=1, sample_width=2, format_tag=1, data=pcm)
    converted, _ = to_pcm16_mono(audio)
    return converted, [f"Realtime output is {rate} Hz per session.created: resampled to {RATE} Hz"]


async def realtime_repeat(ctx: Ctx, voice: str, line: str) -> tuple[TurnResult, Opened]:
    """Realtime in `voice`, told to say the typed line verbatim (conversation.item.create + response.create)."""
    opened = await open_session(ctx, session_config(ctx.settings, voice=voice, instructions=REPEAT_INSTRUCTIONS))
    try:
        turn = await typed_turn(opened.conn, line, clock=ctx.clock)
    finally:
        await close_quietly(opened.conn)
    if not turn.pcm:
        raise VerifyError(f"Realtime sent no audio: {turn.problem() or 'empty response'}", fatal=turn.fatal)
    return turn, opened


@dataclass
class VoiceAB:
    voice: str
    clip: bytes  # TTS 3, AB_GAP_S of silence, Realtime: PCM16 mono 24 kHz
    lines: list[str]
    first_audio_ms: int | None
    verbatim: bool


async def voice_ab(ctx: Ctx, voice: str, line: str) -> VoiceAB:
    """One voice through TTS 3 (the render's request) and Realtime (repeat the typed line), back to back."""
    tts = await tts_request(ctx, line, voice)
    turn, opened = await realtime_repeat(ctx, voice, line)
    rt_pcm, notes = realtime_pcm(turn, opened.session)
    resolved = ((opened.session.get("audio") or {}).get("output") or {}).get("voice")
    if resolved and resolved != voice:
        notes.append(f"session.created says voice {resolved!r}, not {voice!r}")
    tts_level, rt_level = level_dbfs(tts.pcm), level_dbfs(rt_pcm)
    if tts_level is not None and rt_level is not None and abs(tts_level - rt_level) > LEVEL_GAP_DB:
        notes.append(f"loudness differs by {abs(tts_level - rt_level):.1f} dB: judge the voice, not the volume")
    match = word_match(line, turn.transcript)
    lines = [f"{voice}: TTS {seconds(tts.audio_ms)} of audio in {tts.elapsed_ms} ms ({tts.source}), "
             f"{describe_level(tts.pcm)}; Realtime session {opened.total_ms} ms, first audio {turn.first_audio_ms} ms, "
             f"{seconds(pcm_ms(rt_pcm))} of audio, {describe_level(rt_pcm)}",
             *(f"    {note}" for note in [*tts.notes, *notes]),
             f'    Realtime said: "{turn.transcript}" ({match:.0%} of the words match the line)']
    return VoiceAB(voice, tts.pcm + silence(AB_GAP_S) + rt_pcm, lines, turn.first_audio_ms, match >= VERBATIM_MATCH)


async def check_voices(ctx: Ctx, *, voices: Sequence[str] = DEFAULT_VOICES, line: str = DEFAULT_LINE) -> Result:
    """F1: per voice, TTS 3 of `line`, 0.7 s of silence, then Realtime repeating it → <voice>_ab.wav."""
    out_dir = ctx.out_dir / "voices"
    result = Result("voices", "LISTEN", [f'line ({word_count(line)} words): "{line}"'])
    if has_digits(line):
        result.lines.append("    the line has digits: TTS 3 reads them without text normalisation (timestamps on)")
    done: list[VoiceAB] = []
    failed: list[str] = []
    for voice in voices:
        ctx.echo(f"  … {voice}: TTS 3, then Realtime")
        try:
            ab = await voice_ab(ctx, voice, line)
        except VerifyError as exc:
            if exc.fatal:
                raise
            result.lines.append(f"{voice}: FAILED: {exc}")
            failed.append(voice)
            continue
        done.append(ab)
        result.files.append(write_wav(out_dir / f"{voice}_ab.wav", ab.clip))
        result.lines += ab.lines
    if not done:
        result.status = "FAIL"
        result.summary = "no voice produced both clips (errors above)."
        return result
    result.files.append(write_wav(out_dir / "all_voices.wav", silence(VOICE_GAP_S).join(ab.clip for ab in done)))
    result.lines.append(f"each clip: TTS 3, {AB_GAP_S:g} s of silence, then Realtime. Play them on the laptop "
                        "speakers; pass = you can't hear the switch.")
    first_audio = ", ".join(f"{ab.voice} {ab.first_audio_ms} ms" for ab in done)
    result.summary = (f"{len(done)} voice(s) through TTS 3 then Realtime (repeat-the-line prompt), clips in "
                      f"{rel(out_dir)}/; Realtime said the line word for word for {sum(ab.verbatim for ab in done)}/"
                      f"{len(done)}; Realtime first audio: {first_audio}"
                      + (f"; FAILED: {', '.join(failed)}" if failed else "")
                      + ". By ear: ___ (keep the voice whose switch you can't hear).")
    return result


def tag_leaks(text: str, words: Sequence[TimedWord]) -> tuple[list[str], list[str]]:
    """(words carrying raw tag text like "<|…|>", words that are bare tag names absent from the spoken text)."""
    spoken = {norm_word(word) for word in strip_tags(text).split()}
    names: set[str] = set()
    for tag in find_tags(text):
        for part in (tag.kind, tag.value, tag.kind + tag.value, *tag.value.split("_")):
            token = norm_word(part)
            if token and token not in spoken:
                names.add(token)
    raw = [w.word for w in words if any(ch in w.word for ch in "<|>")]
    named = [w.word for w in words if w.word not in raw and norm_word(w.word) in names]
    return raw, named


def pause_gap(text: str, words: Sequence[TimedWord]) -> tuple[int | None, str]:
    """Silence (ms) between the words around the first <|prosody:pause|>/<|prosody:long_pause|> in `text`."""
    tag = next((t for t in find_tags(text) if t.kind == "prosody" and t.value in ("pause", "long_pause")), None)
    if tag is None:
        return None, ""
    before, after = strip_tags(text[:tag.start]).split(), strip_tags(text[tag.end:]).split()
    if not before or not after:
        return None, ""
    left, right = norm_word(before[-1]), norm_word(after[0])
    label = f"{before[-1]} | {after[0]}"
    clean = [w for w in words if norm_word(w.word)]
    for i in range(len(clean) - 1):
        if norm_word(clean[i].word) == left and norm_word(clean[i + 1].word) == right:
            return clean[i + 1].start - clean[i].end, label
    index = sum(1 for word in before if norm_word(word))  # fall back to counting words
    if 0 < index < len(clean):
        return clean[index].start - clean[index - 1].end, label + " (by word count)"
    return None, label


def word_rows(words: Sequence[TimedWord] | None) -> list[str]:
    if not words:
        return []
    return textwrap.wrap(" ".join(f"{w.word}@{w.start}-{w.end}" for w in words), width=104)


def spoken_words(words: Sequence[TimedWord], named: Sequence[str] = ()) -> list[TimedWord]:
    """The word list as the render keeps it (tag text stripped, empty words dropped), minus bare tag names."""
    out = []
    for w in words:
        word = strip_tags(w.word)
        if norm_word(word) and w.word not in named:
            out.append(TimedWord(word, w.start, w.end))
    return out


@dataclass
class TagFindings:
    status: str  # PASS | LISTEN (pause not measurably wider) | FAIL (partial tag text the render can't strip)
    lines: list[str]
    leak: str
    pause: str


def tag_findings(text: str, tagged: Sequence[TimedWord], plain: Sequence[TimedWord] | None,
                 audio_ms: int) -> TagFindings:
    """What the tagged word list says: tag text in the words? Is the gap at <|prosody:pause|> wider than untagged?"""
    raw, named = tag_leaks(text, tagged)
    partial = [word for word in raw if any(ch in strip_tags(word) for ch in "<|>")]
    if partial:
        leak = f"partial tag text in {len(partial)} word(s): {', '.join(partial)} (the render can't strip it)"
    elif named:
        leak = (f"bare tag names show up as words: {', '.join(named)} (the render drops these with a warning; "
                "check that the real words' timings still line up)")
    elif raw:
        leak = f"raw tag text in {len(raw)} word(s): {', '.join(raw)} (the render strips <|…|> spans)"
    else:
        leak = "no tag text in the word list"
    clean = spoken_words(tagged, named)
    gap, label = pause_gap(text, clean)
    base, _ = pause_gap(text, spoken_words(plain or []))
    if gap is not None and base is not None:
        pause = f"pause gap [{label}] {gap} ms with the tag, {base} ms without ({gap - base:+d} ms)"
    else:
        pause = f"pause gap [{label}]: not found in the word lists ({gap} / {base})"
    last_end = max((w.end for w in tagged), default=0)
    lines = [leak, f"words: {len(clean)} from TTS (tags removed), {word_count(text)} in the text", pause,
             f"last word ends at {last_end} ms; audio is {audio_ms} ms"
             + (" (timings run past the audio!)" if last_end > audio_ms + 50 else "")]
    if partial:  # the only tag text the render can't remove; bare tag names are dropped by the render
        status = "FAIL"
    elif gap is None or base is None or gap - base < PAUSE_MIN_EXTRA_MS:
        status = "LISTEN"
    else:
        status = "PASS"
    return TagFindings(status, lines, leak, pause)


async def check_tags(ctx: Ctx, *, voice: str = DEFAULT_VOICE, text: str = TAGGED_TEXT) -> Result:
    """F3: leading delivery tags + a mid-text pause with `timestamps: true`, against the same text untagged."""
    out_dir = ctx.out_dir / "tags"
    plain_text = strip_tags(text)
    ctx.echo("  … TTS 3 with tags, then the same text without")
    tagged = await tts_request(ctx, text, voice)
    plain = await tts_request(ctx, plain_text, voice)
    result = Result("tags", "PASS")
    for name, tts, source in (("tagged", tagged, text), ("plain", plain, plain_text)):
        result.files.append(write_wav(out_dir / f"{name}.wav", tts.pcm))
        result.files.append(write_json(out_dir / f"{name}.json",
                                       {"input": source, "voice": voice, "timestamps": tts.raw_timestamps}))
        count = "timestamps null" if tts.words is None else f"{len(tts.words)} words"
        result.lines.append(f'{name}: "{source}"')
        result.lines.append(f"    {count}, {seconds(tts.audio_ms)} of audio, request {tts.elapsed_ms} ms ({tts.source})")
        result.lines += [f"    {row}" for row in word_rows(tts.words)]
    if not tagged.words:
        what = "null (alignment unavailable)" if tagged.words is None else "[] (no speech)"
        result.status = "FAIL"
        result.summary = f"TTS 3 with tags + timestamps returned timestamps {what}; the render would estimate timings."
        return result

    findings = tag_findings(text, tagged.words, plain.words, tagged.audio_ms)
    listen = f"listen to {rel(out_dir)}/tagged.wav vs plain.wav: emotion audible, timings right?"
    result.status = findings.status
    result.lines += [*findings.lines, listen]
    result.summary = (f"leading delivery tags + a mid-text pause with timestamps: {findings.leak}; {findings.pause}; "
                      f"{listen}")
    return result


@dataclass
class Latency:
    """F2 samples, all in ms."""

    connect: list[int] = field(default_factory=list)  # connect → session.created (upgrade + ack)
    upgrade: list[int] = field(default_factory=list)  # the WebSocket upgrade alone
    created: list[int] = field(default_factory=list)  # response.create → response.created
    first_audio: list[int] = field(default_factory=list)  # response.create → first audio delta
    close: list[int] = field(default_factory=list)  # our close handshake
    reconnect: list[int] = field(default_factory=list)  # old socket closed → new session.created
    problems: list[str] = field(default_factory=list)

    def opened(self, opened: Opened) -> Opened:
        self.connect.append(opened.total_ms)
        self.upgrade.append(opened.handshake_ms)
        return opened


async def latency_turn(ctx: Ctx, opened: Opened, config: dict, n: int, stats: Latency) -> Opened:
    """One typed turn; a session that closed or stalled is replaced (not counted as a reconnect)."""
    turn = await typed_turn(opened.conn, LATENCY_PROMPTS[n % len(LATENCY_PROMPTS)], clock=ctx.clock)
    if turn.fatal:
        raise VerifyError(turn.problem(), fatal=True)
    if turn.created_ms is not None:
        stats.created.append(turn.created_ms)
    if turn.first_audio_ms is None:
        stats.problems.append(f"turn {n + 1}: no audio ({turn.problem() or 'empty response'})")
    else:
        stats.first_audio.append(turn.first_audio_ms)
    if turn.closed or turn.timed_out:
        await close_quietly(opened.conn)
        return stats.opened(await open_session(ctx, config))
    return opened


async def latency_reconnect(ctx: Ctx, opened: Opened, config: dict, stats: Latency) -> Opened:
    """Close the session, then time a new one: after a drop the app reconnects at once (design §6)."""
    started = ctx.clock()
    await close_quietly(opened.conn)
    closed = ctx.clock()
    reopened = stats.opened(await open_session(ctx, config))
    stats.close.append(_ms(closed - started))
    stats.reconnect.append(_ms(ctx.clock() - closed))
    return reopened


def latency_result(voice: str, stats: Latency) -> Result:
    first, reconnect = describe_ms(stats.first_audio), describe_ms(stats.reconnect)
    result = Result("latency", "PASS", [
        f"connect → session.created: {describe_ms(stats.connect)}; WebSocket upgrade alone {describe_ms(stats.upgrade)}",
        f"typed turn → response.created: {describe_ms(stats.created)}",
        f"typed turn → first audio: {first}  (spoken target, end of speech → first audio: ≤ {TARGET_FIRST_AUDIO_MS} ms)",
        f"reconnect (old socket closed → new session.created): {reconnect}  (target ≤ {TARGET_RECONNECT_MS} ms); "
        f"closing the old socket took {describe_ms(stats.close)}",
        *stats.problems,
        "not measured here: tap → silence (< 100 ms) and end of speech → first reply audio (≤ 1.5 s); those are "
        "measured in the browser (status strip) and the app event log (metric reply_latency_ms, out/logs/).",
    ])
    slow_turn = not stats.first_audio or statistics.median(stats.first_audio) > TARGET_FIRST_AUDIO_MS
    slow_reconnect = not stats.reconnect or statistics.median(stats.reconnect) > TARGET_RECONNECT_MS
    if stats.problems or slow_turn or slow_reconnect:
        result.status = "FAIL"
    result.summary = (f"voice {voice}: typed turn → first audio {first}; connect → session.created "
                      f"{describe_ms(stats.connect)}; reconnect {reconnect} (target ≤ 2 s)"
                      + (f"; problems: {'; '.join(stats.problems)}" if stats.problems else "")
                      + ". Tap → silence and end of speech → reply are measured in the browser and the app event log.")
    return result


async def check_latency(ctx: Ctx, *, runs: int = 5, voice: str = DEFAULT_VOICE) -> Result:
    """F2, server side: connect → session.created, typed turn → first audio, close → a reopened session."""
    config = session_config(ctx.settings, voice=voice, instructions=CHAT_INSTRUCTIONS)
    stats = Latency()
    ctx.echo(f"  … connect, {runs} typed turn(s), {runs} reconnect(s)")
    opened = stats.opened(await open_session(ctx, config))
    try:
        for n in range(runs):
            opened = await latency_turn(ctx, opened, config, n, stats)
        for _ in range(runs):
            opened = await latency_reconnect(ctx, opened, config, stats)
    except VerifyError as exc:
        if exc.fatal:
            raise
        stats.problems.append(f"stopped early: {exc}")  # keep what was measured so far
    finally:
        await close_quietly(opened.conn)
    return latency_result(voice, stats)


def app_modules() -> tuple[Any, Any]:
    """relay.prompts and relay.tools, imported late: they are written in parallel and only F5/F6 need them."""
    try:
        from . import prompts, tools
    except Exception as exc:  # a broken module must fail this check with a clear line, not crash `all`
        raise VerifyError(f"cannot import relay.prompts / relay.tools: {type(exc).__name__}: {exc}") from None
    return prompts, tools


async def wait_for_update(conn: Any, instructions: str, *, timeout: float) -> dict:
    """The session.updated that acks `instructions`, skipping a late ack of an earlier update. If none matches in
    time, the last session.updated seen (the caller reports its old instructions)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: dict | None = None
    while True:
        try:
            event = await wait_for_event(conn, {"session.updated"}, timeout=deadline - loop.time())
        except VerifyTimeout:
            if last is None:
                raise
            return last
        session = event.get("session") if isinstance(event.get("session"), dict) else {}
        if session.get("instructions", instructions) == instructions:
            return session
        last = session


def session_result(talk: Talk, note: str, full: dict, created: dict, updated: dict, instructions: str) -> Result:
    """F6 verdict: did the first full update stick, and did the instructions-only update leave everything else?"""
    sent, before, after = session_fields(full), session_fields(created), session_fields(updated)
    not_kept = diff_fields(_essentials(sent), _essentials(before))
    changed = diff_fields(before, after)
    unknown = sorted(set(absent_fields(before)) | set(absent_fields(after)))
    applied = updated.get("instructions") == instructions if "instructions" in updated else None
    temperature = created.get("temperature")
    essentials = _essentials(sent)
    result = Result("session", "PASS", [note] if note else [])
    result.lines.append(f"talk {talk.id}: sent voice {sent['voice']}, turn_detection {essentials['turn_detection']}, "
                        f"transcription {essentials['transcription']}, {sent['tools']} tools")
    result.lines.append("session.created: " + ", ".join(f"{key} {value!r}" for key, value in before.items())
                        + f", temperature {temperature!r}")
    result.lines += [f"session.created differs from what was sent: {line}" for line in not_kept]
    result.lines += [f"changed by the instructions-only update: {line}" for line in changed]
    if unknown:
        result.lines.append(f"the acks leave out: {', '.join(unknown)}")
    elif not changed:
        result.lines.append(f"the instructions-only update changed none of: {', '.join(before)}")
    result.lines.append({True: "new instructions applied (echoed in session.updated)",
                         False: "session.updated still shows the OLD instructions",
                         None: "session.updated does not echo instructions"}[applied])
    problems = []
    if changed or applied is False:
        what = changed + (["the new instructions were not applied"] if applied is False else [])
        problems.append(f"an instructions-only session.update is NOT safe ({'; '.join(what)}): set "
                        "RELAY_FULL_SESSION_UPDATE=1")
    if not_kept:
        problems.append(f"the full session.update did not stick ({'; '.join(not_kept)}): compare design §6 with the "
                        "session.created ack (created.json)")
    if problems:
        result.status = "FAIL"
        result.summary = ". ".join(problems) + "."
    elif unknown:
        result.status = "LISTEN"
        result.summary = (f"the acks leave out {', '.join(unknown)}, so they can't prove an instructions-only "
                          "session.update keeps them: after one live turn, check by ear that the voice and the "
                          "turn-taking are unchanged.")
    else:
        result.summary = (f"an instructions-only session.update kept voice {before['voice']!r}, turn_detection "
                          f"{_essentials(before)['turn_detection']!r}, transcription, {before['tools']} tools and "
                          f"output_modalities (session.updated ack); resolved temperature {temperature!r}. Keep "
                          "RELAY_FULL_SESSION_UPDATE off.")
    return result


async def check_session(ctx: Ctx, *, talk: str = DEFAULT_TALK) -> Result:
    """F6: full session.update → session.created; instructions-only update → session.updated; nothing else moves."""
    prompts, tools = app_modules()
    loaded, note = resolve_talk(talk)
    opening = prompts.build_instructions(loaded, prompts.TurnContext(phase="opening", slide_id=loaded.slides[0].id))
    presenting = prompts.build_instructions(loaded, prompts.TurnContext(phase="presenting",
                                                                        slide_id=loaded.relay_slides[0].id))
    full = session_config(ctx.settings, voice=loaded.voice, instructions=opening, tools=tools.tool_specs(loaded))
    ctx.echo("  … full session.update, then an instructions-only one")
    opened = await open_session(ctx, full)
    try:
        await send_or_fail(opened.conn, {"type": "session.update",
                                         "session": {"type": "realtime", "instructions": presenting}})
        updated = await wait_for_update(opened.conn, presenting, timeout=ACK_TIMEOUT_S)
    finally:
        await close_quietly(opened.conn)
    out_dir = ctx.out_dir / "session"
    result = session_result(loaded, note, full, opened.session, updated, presenting)
    result.files += [write_json(out_dir / "created.json", opened.session), write_json(out_dir / "updated.json", updated)]
    return result


@dataclass
class ToolTurn:
    """One scripted F5 turn and what came back."""

    n: int
    scenario: Scenario
    turn: TurnResult
    checked: list[CheckedCall]
    prompt_words: int

    @property
    def applied(self) -> CheckedCall | None:
        """The call the app would act on: the last valid one (design §5.2)."""
        return next((checked for checked in reversed(self.checked) if checked.error is None), None)

    @property
    def ok(self) -> bool:
        return self.scenario.accepts(self.applied)

    def line(self) -> str:
        calls = ", ".join(str(checked) for checked in self.checked) or "no call"
        if not self.checked and said_tool_name(self.turn.transcript):
            calls += " (said a tool name aloud instead?)"
        timing = (f"first audio {self.turn.first_audio_ms} ms" if self.turn.first_audio_ms is not None
                  else self.turn.problem() or "no audio")
        return (f"{self.n:>2} {'ok  ' if self.ok else 'MISS'} {self.scenario.name:<8} @{self.scenario.slide_id}: "
                f'want {self.scenario.want()}; got {calls}; {timing}; "{_clip(self.turn.transcript, 60)}"')

    def log(self) -> dict:
        return {"turn": self.n, "scenario": self.scenario.name, "phase": self.scenario.phase,
                "slide": self.scenario.slide_id, "say": self.scenario.say, "want": self.scenario.want(), "ok": self.ok,
                "transcript": self.turn.transcript, "prompt_words": self.prompt_words,
                "calls": [{"call_id": c.call.call_id, "name": c.call.name, "arguments": c.call.arguments,
                           "error": c.error} for c in self.checked],
                "first_audio_ms": self.turn.first_audio_ms, "problem": self.turn.problem(),
                "events": dict(self.turn.events)}


async def send_tool_outputs(conn: Any, record: ToolTurn, talk: Talk) -> bool:
    """One function_call_output (a JSON string) per call and never a response.create after them: the app never sends
    one, so Relay goes quiet. False when the session can't take more turns."""
    if record.turn.closed or record.turn.timed_out:
        return False
    try:
        for checked in record.checked:
            if checked.call.call_id:
                output = app_tool_output(checked, talk, record.scenario.slide_id)
                await send_event(conn, tool_output(checked.call.call_id, output))
    except ConnectionClosed:
        return False
    return True


def tools_result(ctx: Ctx, talk: Talk, note: str, specs: Sequence[dict], turns: Sequence[ToolTurn], runs: int,
                 stopped: str) -> Result:
    correct = sum(turn.ok for turn in turns)
    called = sum(bool(turn.checked) for turn in turns)
    rate = correct / runs if runs else 0.0  # turns that never ran count as misses
    misses = Counter(turn.scenario.name for turn in turns if not turn.ok)
    miss_text = ", ".join(f"{name} ×{count}" for name, count in misses.items())
    words = [turn.prompt_words for turn in turns]
    by_scenario = {turn.scenario.name: turn.prompt_words for turn in turns}  # long prompts stop tool calls (gotchas)
    mode = "full config" if ctx.settings.full_session_update else "instructions only"
    out_dir = ctx.out_dir / "tools"
    result = Result("tools", "PASS" if rate >= TOOL_PASS_RATE else "FAIL", [note] if note else [])
    result.lines.append(f"talk {talk.id}, voice {talk.voice}, {len(specs)} tools, per-turn session.update: {mode}; "
                        "tool outputs sent without response.create")
    result.lines.append("prompt (BASE + STATE) words: " + ", ".join(f"{name} {count}"
                                                                    for name, count in by_scenario.items()))
    result.lines += [turn.line() for turn in turns]
    if stopped:
        result.lines.append(stopped)
    tally = (f"{correct}/{runs} typed turns ({rate:.0%}) ended with the right real function_call (pass ≥ "
             f"{TOOL_PASS_RATE:.0%}); {called}/{runs} made any call" + (f"; misses: {miss_text}" if miss_text else ""))
    result.lines.append(tally)
    result.lines.append(f"replies to listen to: {rel(out_dir)}/turnNN_<scenario>.wav")
    result.files.append(write_json(out_dir / "turns.json", [turn.log() for turn in turns]))
    result.summary = (f"{tally}; talk {talk.id}, BASE + STATE up to {max(words, default=0)} words, voice {talk.voice}"
                      + (f"; {stopped}" if stopped else "") + ". Typed, not spoken: confirm on a spoken rehearsal.")
    return result


async def check_tools(ctx: Ctx, *, talk: str = DEFAULT_TALK, runs: int = 10) -> Result:
    """F5: scripted typed turns across phases, set up like the app; pass = ≥ 90 % end with the right real call."""
    prompts, tools = app_modules()
    loaded, note = resolve_talk(talk)
    scenarios = build_scenarios(loaded)
    specs = tools.tool_specs(loaded)
    parse_call = getattr(tools, "parse_call", None)
    out_dir = ctx.out_dir / "tools"
    turns: list[ToolTurn] = []
    stopped = ""
    opened: Opened | None = None
    full: dict = {}
    ctx.echo(f"  … {runs} typed turn(s) on talk {loaded.id}, a fresh session every {len(scenarios)} turns")
    try:
        for n in range(runs):
            scenario = scenarios[n % len(scenarios)]
            instructions = prompts.build_instructions(loaded, prompts.TurnContext(
                phase=scenario.phase, slide_id=scenario.slide_id, cut_word=scenario.cut_word,
                cut_sentence=scenario.cut_sentence))
            if opened is None or n % len(scenarios) == 0:  # one session per pass through the talk, like the app
                if opened is not None:
                    await close_quietly(opened.conn)
                    opened = None
                full = session_config(ctx.settings, voice=loaded.voice, instructions=instructions, tools=specs)
                opened = await open_session(ctx, full)
            turn = await typed_turn(opened.conn, scenario.say, clock=ctx.clock,
                                    session=per_turn_session(ctx.settings, full, instructions))
            if turn.fatal:
                raise VerifyError(turn.problem(), fatal=True)
            record = ToolTurn(n + 1, scenario, turn, [check_call(call, loaded, parse_call) for call in turn.calls],
                              len(instructions.split()))
            turns.append(record)
            ctx.echo(f"  … turn {record.n}/{runs} {scenario.name}: {'ok' if record.ok else 'MISS'}")
            if turn.pcm:
                write_wav(out_dir / f"turn{record.n:02d}_{scenario.name}.wav", bytes(turn.pcm))
            if not await send_tool_outputs(opened.conn, record, loaded):
                await close_quietly(opened.conn)
                opened = None
    except VerifyError as exc:
        if exc.fatal:
            raise
        stopped = f"stopped after {len(turns)} turn(s): {exc}"  # report the turns that ran
    finally:
        if opened is not None:
            await close_quietly(opened.conn)
    return tools_result(ctx, loaded, note, specs, turns, runs, stopped)


@dataclass
class IdleWatch:
    events: list[tuple[float, dict]]  # (seconds after session.created, event)
    closed_at: float | None = None
    close_code: int | None = None
    close_reason: str = ""

    def log(self) -> list[dict]:
        log = [{"t_s": round(t, 2), **brief_event(event)} for t, event in self.events]
        if self.closed_at is not None:
            log.append({"t_s": round(self.closed_at, 2), "type": "(socket closed)", "code": self.close_code,
                        "reason": self.close_reason})
        return log


async def watch_idle(ctx: Ctx, opened: Opened, wait: float) -> IdleWatch:
    """Record every server event for `wait` s without sending anything, or until the server closes the socket."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    watch = IdleWatch([(0.0, event) for event in opened.events])
    next_note = PROGRESS_EVERY_S
    while (elapsed := loop.time() - start) < wait:
        try:
            raw = await asyncio.wait_for(opened.conn.recv(), min(wait, next_note) - elapsed)
        except TimeoutError:
            if loop.time() - start >= next_note:
                ctx.echo(f"  … {next_note:g}/{wait:g} s, socket open, {len(watch.events)} event(s)")
                next_note += PROGRESS_EVERY_S
            continue
        except ConnectionClosed as exc:
            watch.closed_at = loop.time() - start
            watch.close_code, watch.close_reason = close_info(exc, opened.conn)
            break
        watch.events.append((loop.time() - start, decode_event(raw)))
    return watch


async def recover(ctx: Ctx, config: dict) -> tuple[bool, str, TurnResult | None]:
    """F7's proof of recovery: a new session and one typed turn → (answered with audio, description, turn)."""
    started = ctx.clock()
    try:
        opened = await open_session(ctx, config)
    except VerifyError as exc:
        if exc.fatal:
            raise
        return False, f"the reconnect FAILED: {exc}", None
    reconnect_ms = _ms(ctx.clock() - started)
    try:
        turn = await typed_turn(opened.conn, RECOVERY_PROMPT, clock=ctx.clock)
    finally:
        await close_quietly(opened.conn)
    if not turn.pcm:
        return False, f"reconnect {reconnect_ms} ms, then the typed turn FAILED: {turn.problem() or 'no audio'}", turn
    return True, (f"reconnect {reconnect_ms} ms, then a typed turn answered (first audio {turn.first_audio_ms} ms, "
                  f'"{_clip(turn.transcript, 50)}")'), turn


def idle_result(watch: IdleWatch, wait: float, ok: bool, recovery: str) -> Result:
    result = Result("idle", "PASS" if ok else "FAIL",
                    [f"waited up to {wait:g} s after session.created without sending user audio"])
    result.lines += [f"{t:7.1f} s  {event.get('type')}" for t, event in watch.events[:MAX_EVENT_LINES]]
    if len(watch.events) > MAX_EVENT_LINES:
        result.lines.append(f"… {len(watch.events) - MAX_EVENT_LINES} more event(s) in events.json")
    if watch.closed_at is not None:
        hint = CLOSE_HINTS.get(watch.close_code or 0, "")
        reason = f" {watch.close_reason!r}" if watch.close_reason else ""
        result.lines.append(f"{watch.closed_at:7.1f} s  socket closed: code {watch.close_code}{reason}"
                            + (f" → {hint}" if hint else ""))
        what = f"the server closed the socket after {watch.closed_at:.0f} s (code {watch.close_code}{reason})"
    else:
        what = f"the socket was still open after {wait:g} s"
    later = [f"{event.get('type')} at {t:.0f} s" for t, event in watch.events if event.get("type") != "session.created"]
    events_text = (", ".join(later[:5]) + (f" and {len(later) - 5} more" if len(later) > 5 else "")
                   if later else "no events after session.created")
    result.lines.append(recovery)
    result.summary = f"no user audio for up to {wait:g} s: {what}; {events_text}; {recovery}."
    return result


async def check_idle(ctx: Ctx, *, wait: float = 330.0, voice: str = DEFAULT_VOICE) -> Result:
    """F7: a session with no user audio for `wait` s; log every event and the close; reconnect and run one turn."""
    config = session_config(ctx.settings, voice=voice, instructions=CHAT_INSTRUCTIONS)
    out_dir = ctx.out_dir / "idle"
    opened = await open_session(ctx, config)
    ctx.echo(f"  … waiting up to {wait:g} s without user audio (a note every {PROGRESS_EVERY_S:g} s)")
    try:
        watch = await watch_idle(ctx, opened, wait)
    finally:
        await close_quietly(opened.conn)
    ctx.echo("  … reconnecting for one typed turn")
    ok, recovery, turn = await recover(ctx, config)
    result = idle_result(watch, wait, ok, recovery)
    result.files.append(write_json(out_dir / "events.json", watch.log()))
    if turn is not None and turn.pcm:
        result.files.append(write_wav(out_dir / "recovery.wav", bytes(turn.pcm)))
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────────────────────

CHECKS: dict[str, Callable[..., Awaitable[Result]]] = {
    "voices": check_voices, "tags": check_tags, "latency": check_latency,
    "session": check_session, "tools": check_tools, "idle": check_idle,
}
CHECK_OPTIONS = {"voices": ("voices", "line"), "tags": ("voice",), "latency": ("runs", "voice"),
                 "session": ("talk",), "tools": ("talk", "runs"), "idle": ("wait", "voice")}


async def run_check(ctx: Ctx, name: str, options: dict) -> Result:
    try:
        return await CHECKS[name](ctx, **options)
    except VerifyError as exc:
        return Result(name, "FAIL", [str(exc)], summary=f"could not run: {exc}", fatal=exc.fatal)
    except Exception as exc:  # a live check reports and lets `all` carry on
        ctx.echo(traceback.format_exc().rstrip())
        return Result(name, "FAIL", [f"crashed: {type(exc).__name__}: {exc}"],
                      summary=f"crashed: {type(exc).__name__}: {exc}")


async def run_checks(ctx: Ctx, names: Sequence[str], options: dict[str, dict] | None = None,
                     now: Callable[[], datetime] = datetime.now) -> int:
    results: list[Result] = []
    for name in names:
        ctx.echo(f"── {TITLES[name]} …")
        result = await run_check(ctx, name, (options or {}).get(name, {}))
        ctx.echo(format_result(result, now()))
        results.append(result)
        if result.fatal and len(names) > 1:
            ctx.echo("stopping: billing / key problems fail every later check the same way")
            break
    if len(names) > 1:
        ctx.echo(format_overview(results, now()))
    failed = len(results) < len(names) or any(result.status == "FAIL" for result in results)
    return 1 if failed else 0


def _voice(value: str) -> str:
    voice = value.strip().lower()
    if voice not in PRESET_VOICES:
        raise argparse.ArgumentTypeError(f"{value!r} is not a preset voice ({', '.join(PRESET_VOICES)})")
    return voice


def _voice_list(value: str) -> tuple[str, ...]:
    voices = tuple(dict.fromkeys(_voice(part) for part in value.split(",") if part.strip()))
    if not voices:
        raise argparse.ArgumentTypeError("give at least one voice, e.g. chloe,marcus")
    return voices


def _line(value: str) -> str:
    line = " ".join(value.split())
    if not line or len(line) > MAX_SAY_CHARS:
        raise argparse.ArgumentTypeError(f"the line must be 1–{MAX_SAY_CHARS} characters (like a narration chunk)")
    return line


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a whole number ≥ 1, got {value!r}")
    return number


def _seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        number = -1.0
    if not (number >= 0 and math.isfinite(number)):
        raise argparse.ArgumentTypeError(f"expected seconds ≥ 0, got {value!r}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m relay.verify",
        description="Brief Appendix F checks against the live Higgs API (needs BOSON_API_KEY in .env). Each check "
                    "prints a block ending with a dated line for doc/gotchas.md; audio goes to out/verify/<check>/.")
    sub = parser.add_subparsers(dest="check", required=True, metavar="CHECK")

    def voice_option(command: argparse.ArgumentParser) -> None:
        command.add_argument("--voice", type=_voice, help=f"preset voice (default: {DEFAULT_VOICE})")

    def talk_option(command: argparse.ArgumentParser) -> None:
        command.add_argument("--talk", help=f"talk package (default: {DEFAULT_TALK}, else talks/test)")

    voices = sub.add_parser("voices", help="F1: each preset through TTS 3 then Realtime, back to back (judge by ear)")
    voices.add_argument("--voices", type=_voice_list, help=f"comma-separated presets (default: {','.join(DEFAULT_VOICES)})")
    voices.add_argument("--line", type=_line, help="the line both APIs say (default: a narration line from the brief)")
    voice_option(sub.add_parser("tags", help="F3: leading delivery tags + a pause, with word timestamps"))
    latency = sub.add_parser("latency", help="F2 (server side): connect, typed turn → first audio, reconnect")
    latency.add_argument("--runs", type=_positive_int, help="typed turns and reconnects (default: 5)")
    voice_option(latency)
    talk_option(sub.add_parser("session", help="F6: does an instructions-only session.update keep voice, VAD, tools?"))
    tools = sub.add_parser("tools", help="F5: scripted typed turns across phases → real function_call items")
    talk_option(tools)
    tools.add_argument("--runs", type=_positive_int, help="typed turns (default: 10)")
    idle = sub.add_parser("idle", help="F7: wait without user speech, log the close, reconnect + one turn")
    idle.add_argument("--wait", type=_seconds, help="seconds to wait (default: 330)")
    voice_option(idle)
    every = sub.add_parser("all", help="voices, tags, latency, session, tools (not idle)")
    voice_option(every)
    talk_option(every)
    return parser


def check_options(name: str, args: argparse.Namespace) -> dict:
    """The options `name` takes that were given on the command line (the check owns its defaults)."""
    return {key: getattr(args, key) for key in CHECK_OPTIONS[name] if getattr(args, key, None) is not None}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"bad setting in .env / the environment: {exc}", file=sys.stderr)
        return 2
    if not settings.api_key:
        print(NO_KEY_MESSAGE, file=sys.stderr)
        return 2
    names = ALL_CHECKS if args.check == "all" else (args.check,)
    try:
        return asyncio.run(run_checks(Ctx(settings), names, {name: check_options(name, args) for name in names}))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
