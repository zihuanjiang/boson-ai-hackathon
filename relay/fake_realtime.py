"""Offline stand-in for Higgs Realtime (design §9): the same surface as a websockets client connection.

    uv run python -m relay serve talks/test --fake-realtime     # or RELAY_FAKE_REALTIME=1
    RELAY_FAKE_DROP_AFTER=20                                    # close every connection after 20 s (reconnect test)

It speaks the event names and fields of doc/higgs-realtime.md §9 (OpenAI GA Realtime shapes), rejects the
`session.update` mistakes listed in doc/gotchas.md the way the real server does (`error`, then the socket closes),
and answers each user turn with ~1.2 s of quiet 24 kHz tone (20 ms deltas, ~6× faster than real time), a canned
transcript and one function call chosen from the session instructions:

    Phase: opening → start_presentation · Phase: qa → handoff_to_human ·
    user text naming a relay slide id → goto_slide · otherwise → resume_script

Mic (turn detection on): ≥ 0.3 s of appended audio since the last `clear` → `speech_started`; ≥ 1.5 s →
`speech_stopped`, `committed`, transcript "(fake) I heard you.", then the reply (one turn per `clear`). With
`turn_detection: null`, `commit` + `response.create` replies; so does `conversation.item.create` + `response.create`.

`response.cancel` with nothing active gets Higgs's documented `error` (code `response_not_active`).

Stricter than the real server on purpose (to catch our own bugs): a non-string `function_call_output.output`, an
unknown client event type, or a client event before the first `session.update` gets an `error` event.
Tests: `FakeConnector(sleep=..., speed=...)` fixes the timing knobs and keeps the connections it made;
`reuse_ids=True` numbers every id from 1 again on each connection (resp_fake000001, call_fake000001, …), like a server
whose ids are unique only within a session, so a reconnect test can check that nothing keys on ids across sessions.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import functools
import itertools
import json
import math
import os
import re
import struct
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Coroutine
from urllib.parse import parse_qs, urlsplit

from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from .config import PRESET_VOICES, RATE, REALTIME_MODEL, STT_MODEL

DELTA_MS = 20
DELTA_SAMPLES = RATE * DELTA_MS // 1000  # 480 samples per response.output_audio.delta
MAX_APPEND_B64 = 1 << 20  # Higgs: ≤ 1 MiB of base64 per input_audio_buffer.append
MIN_COMMIT_MS = 100
FAKE_TRANSCRIPT = "(fake) I heard you."
INVALID_SESSION_CLOSE = 1008
VAD_TYPES = ("server_vad", "semantic_vad")
PCM_RATES = (8000, 16000, 24000, 48000)
REJECTED_SESSION_KEYS = ("include", "prompt", "tracing")
REPLACED_KEYS = frozenset({"turn_detection", "transcription", "format", "noise_reduction", "tools", "tool_choice"})
PHASE_RE = re.compile(r"^Phase:\s*([A-Za-z_]+)", re.M)

REPLY_TEXT = {
    "start_presentation": "(fake) Thanks! Hi everyone, I'm Relay. Let's get started.",
    "handoff_to_human": "(fake) Great question. That is how it works. Back to you.",
    "goto_slide": "(fake) Sure, let's jump to {slide}.",
    "resume_script": "(fake) Good point. As I was saying…",
    None: "(fake) I heard you, but I have no tool to call.",
}

SleepFn = Callable[[float], Awaitable[None]]
_ids = itertools.count(1)


def _new_id(prefix: str) -> str:
    """Unique for the whole process, so ids never repeat across reconnects (the default; see `reuse_ids`)."""
    return f"{prefix}_fake{next(_ids):06d}"


@dataclass(frozen=True)
class FakeOptions:
    speed: float = 6.0  # reply audio arrives this many times faster than real time (Higgs: ~6×)
    reply_s: float = 1.2  # audio length of every reply
    think_s: float = 0.35  # end of the user turn → response.created
    speech_start_s: float = 0.3  # appended audio before speech_started
    speech_stop_s: float = 1.5  # appended audio before speech_stopped (end of turn)
    drop_after: float | None = None  # close each connection after this many seconds; None → RELAY_FAKE_DROP_AFTER
    sleep: SleepFn = asyncio.sleep  # every delay goes through this (tests inject their own)
    reuse_ids: bool = False  # number ids per connection and prefix from 1 (a server numbering ids per session)


def _env_drop_after() -> float | None:
    raw = (os.environ.get("RELAY_FAKE_DROP_AFTER") or "").strip()
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        return None
    return value if value > 0 else None


# --- session config -------------------------------------------------------------------------------------------

def default_session(model: str | None, session_id: str | None = None) -> dict:
    """What the server resolves before any update (doc/higgs-realtime.md §2, defaults per doc/gotchas.md)."""
    return {
        "type": "realtime", "object": "realtime.session", "id": session_id or _new_id("sess"), "model": model,
        "instructions": "You are a helpful AI assistant", "output_modalities": ["audio"],
        "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": RATE}, "noise_reduction": None,
                      "transcription": {"model": "", "language": None}, "turn_detection": None},
            "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": "default"},
        },
        "tools": [], "tool_choice": "auto", "temperature": 0.7, "max_output_tokens": "inf", "truncation": "auto",
        "expires_at": int(time.time()) + 3600,
    }


def _check_format(fmt: Any) -> str | None:
    if not isinstance(fmt, dict):
        return "format must be an object"
    kind = fmt.get("type")
    if kind == "audio/pcm":
        rate = fmt.get("rate", RATE)
        return None if rate in PCM_RATES else f"unsupported rate {rate!r} (use one of {PCM_RATES})"
    if kind in ("audio/pcmu", "audio/opus"):
        return None
    return f"unsupported audio format {kind!r}"


def check_session(update: dict, *, first: bool, url_model: str | None) -> tuple[str, str] | None:
    """(param, message) of the first thing the real server would reject in this `session`, else None."""
    for key in REJECTED_SESSION_KEYS:
        if key in update:
            return f"session.{key}", f"Unsupported field '{key}'"
    if update.get("type", "realtime") != "realtime":
        return "session.type", "session.type must be 'realtime'"
    model = update.get("model", url_model if first else REALTIME_MODEL)
    if model is None:
        return "session.model", "No model: set session.model or ?model= on the URL"
    if model != REALTIME_MODEL:
        return "session.model", f"Unknown model '{model}'"
    modalities = update.get("output_modalities")
    if modalities is not None and modalities not in (["audio"], ["text"]):
        return "session.output_modalities", 'output_modalities must be exactly ["audio"] or ["text"]'
    audio = update.get("audio", {})
    if not isinstance(audio, dict):
        return "session.audio", "audio must be an object"
    for side in ("input", "output"):
        part = audio.get(side, {})
        if not isinstance(part, dict):
            return f"session.audio.{side}", f"audio.{side} must be an object"
        if "format" in part:
            problem = _check_format(part["format"])
            if problem:
                return f"session.audio.{side}.format", problem
    inp, out = audio.get("input", {}), audio.get("output", {})
    voice = out.get("voice")
    if voice is not None and voice not in PRESET_VOICES and not (isinstance(voice, str) and voice.startswith("voice_")):
        return "session.audio.output.voice", f"Unknown voice '{voice}'"
    if inp.get("turn_detection") is not None:
        detection = inp["turn_detection"]
        if not isinstance(detection, dict) or detection.get("type") not in VAD_TYPES:
            kind = detection.get("type") if isinstance(detection, dict) else detection
            return "session.audio.input.turn_detection.type", f"Unknown turn_detection type {kind!r}"
        if "idle_timeout_ms" in detection:
            return "session.audio.input.turn_detection.idle_timeout_ms", "Unsupported field 'idle_timeout_ms'"
    transcription = inp.get("transcription")
    if transcription is not None:
        if not isinstance(transcription, dict) or transcription.get("model", "") not in ("", STT_MODEL):
            name = transcription.get("model") if isinstance(transcription, dict) else transcription
            return "session.audio.input.transcription.model", f"Unknown transcription model {name!r}"
    tools = update.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or any(
                not isinstance(tool, dict) or tool.get("type") != "function" or not tool.get("name") for tool in tools):
            return "session.tools", "tools must be a list of {type: function, name, description, parameters}"
    return None


def _merge_into(target: dict, update: dict) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict) and key not in REPLACED_KEYS:
            _merge_into(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def merge_session(base: dict, update: dict) -> dict:
    """`base` with `update` applied like a partial session.update: nested objects merge, the rest is replaced."""
    merged = copy.deepcopy(base)
    _merge_into(merged, update)
    return merged


# --- replies --------------------------------------------------------------------------------------------------

def _slide_ids(tools: list) -> list[str]:
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name") == "goto_slide":
            try:
                enum = tool["parameters"]["properties"]["slide_id"]["enum"]
            except (KeyError, TypeError):
                return []
            return [str(slide_id) for slide_id in enum or []]
    return []


def choose_reply(instructions: str, user_text: str, tools: list) -> tuple[str | None, dict, str]:
    """(tool name or None, arguments, transcript) for one reply, from the STATE block and what the user said."""
    names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
    match = PHASE_RE.search(instructions or "")
    phase = match.group(1).lower() if match else ""
    if phase == "opening" and "start_presentation" in names:
        return "start_presentation", {}, REPLY_TEXT["start_presentation"]
    if phase == "qa" and "handoff_to_human" in names:
        return "handoff_to_human", {}, REPLY_TEXT["handoff_to_human"]
    if "goto_slide" in names:
        for slide_id in _slide_ids(tools):
            if re.search(rf"(?<![\w-]){re.escape(slide_id)}(?![\w-])", user_text or "", re.I):
                return "goto_slide", {"slide_id": slide_id}, REPLY_TEXT["goto_slide"].format(slide=slide_id)
    if "resume_script" in names:
        return "resume_script", {}, REPLY_TEXT["resume_script"]
    return None, {}, REPLY_TEXT[None]


@functools.lru_cache(maxsize=512)
def tone_delta(index: int, count: int) -> str:
    """Delta `index` of `count`: 20 ms of a soft 220 Hz tone with a syllable-rate swell (base64 PCM16 LE).

    Phase-continuous across deltas, with 50 ms fades at both ends, so a reply sounds like one quiet hum.
    """
    total = count * DELTA_SAMPLES
    samples = []
    for k in range(DELTA_SAMPLES):
        n = index * DELTA_SAMPLES + k
        t = n / RATE
        fade = max(0.0, min(1.0, n / 1200, (total - n) / 1200))
        swell = 0.55 + 0.45 * math.sin(math.pi * 4.0 * t) ** 2
        samples.append(int(0.12 * fade * swell * math.sin(2 * math.pi * 220.0 * t) * 32767))
    return base64.b64encode(struct.pack(f"<{DELTA_SAMPLES}h", *samples)).decode("ascii")


@dataclass
class _Reply:
    id: str
    tool: str | None
    args: dict
    text: str
    item_id: str
    call_id: str | None
    call_item_id: str | None
    text_mode: bool = False
    created: bool = False  # response.created went out
    said: str = ""  # transcript sent so far
    call_announced: bool = False
    task: asyncio.Task | None = None


# --- the connection -------------------------------------------------------------------------------------------

class FakeRealtimeConn:
    """One fake Realtime session. Client side: `send(str)`, `recv() -> str`, `close()`, `close_code`.

    `recv()` raises `websockets.exceptions.ConnectionClosed` once the connection is closed (after any events that
    were already queued). `received` keeps every parsed client event, `session` the resolved config (tests).
    """

    def __init__(self, url: str = "wss://fake.invalid/v1/realtime", options: FakeOptions | None = None, *,
                 headers: Any = None) -> None:
        self.url = url
        self.options = options or FakeOptions()
        self.headers = dict(headers or {})
        self.received: list[dict] = []
        self.session: dict | None = None
        self.close_code: int | None = None
        self.close_reason = ""
        self._url_model = (parse_qs(urlsplit(url).query).get("model") or [None])[0]
        self._id_counters: dict[str, itertools.count] | None = {} if self.options.reuse_ids else None
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._closed: Callable[[], ConnectionClosed] | None = None
        self._tasks: set[asyncio.Task] = set()
        self._response: _Reply | None = None
        self._items: dict[str, dict] = {}
        self._item_audio_ms: dict[str, int] = {}
        self._last_item_id: str | None = None
        self._conversation_id = self._new_id("conv")
        self._user_text = ""  # what the "model" heard last: typed text or the fake transcript
        self._buffer_ms = 0.0  # audio appended since the last clear / commit
        self._audio_ms = 0.0  # all audio appended on this connection
        self._vad = "idle"  # idle → speaking → stopped: one turn per clear
        self._speech_item: str | None = None
        drop_after = self.options.drop_after if self.options.drop_after is not None else _env_drop_after()
        if drop_after and drop_after > 0:
            self._spawn(self._drop_later(drop_after))

    # --- websockets-like client API ---------------------------------------------------------------------------

    async def send(self, message: str | bytes) -> None:
        if self._closed is not None:
            raise self._closed()
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8", "replace")
        try:
            event = json.loads(message)
        except ValueError:
            self._error("invalid_json", "client event is not valid JSON")
            return
        if not isinstance(event, dict):
            self._error("invalid_event", "client event must be a JSON object")
            return
        self.received.append(event)
        kind = event.get("type")
        handler = self._HANDLERS.get(kind) if isinstance(kind, str) else None
        if handler is None:
            self._error("unknown_event", f"unknown client event type {kind!r}", event)
        elif self.session is None and kind != "session.update":
            self._error("session_not_started", f"send session.update before {kind}", event)
        else:
            getattr(self, handler)(event)

    async def recv(self) -> str:
        item = await self._queue.get()
        if item is None:
            self._queue.put_nowait(None)  # every later recv raises too
            assert self._closed is not None
            raise self._closed()
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed is None:
            frame = Close(code, reason)
            self._shutdown(lambda: ConnectionClosedOK(frame, frame, False), code, reason, flush=True)

    def __aiter__(self) -> FakeRealtimeConn:
        return self

    async def __anext__(self) -> str:
        try:
            return await self.recv()
        except ConnectionClosedOK:
            raise StopAsyncIteration from None

    async def __aenter__(self) -> FakeRealtimeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def closed(self) -> bool:
        return self._closed is not None

    # --- server-side closing ----------------------------------------------------------------------------------

    def server_close(self, code: int = 1000, reason: str = "") -> None:
        """The server sends a close frame (tests and invalid sessions)."""
        frame = Close(code, reason)
        cls = ConnectionClosedOK if code in (1000, 1001) else ConnectionClosedError
        self._shutdown(lambda: cls(frame, frame, True), code, reason, flush=False)

    def drop(self) -> None:
        """The network goes away: no close frame, close code 1006 (what RELAY_FAKE_DROP_AFTER does)."""
        self._shutdown(lambda: ConnectionClosedError(None, None), 1006, "", flush=False)

    def _shutdown(self, exc: Callable[[], ConnectionClosed], code: int, reason: str, *, flush: bool) -> None:
        if self._closed is not None:
            return
        self._closed, self.close_code, self.close_reason = exc, code, reason
        if flush:
            while not self._queue.empty():
                self._queue.get_nowait()
        self._queue.put_nowait(None)
        self._response = None
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current:
                task.cancel()

    async def _drop_later(self, seconds: float) -> None:
        await self.options.sleep(seconds)
        self.drop()

    # --- plumbing ---------------------------------------------------------------------------------------------

    def _new_id(self, prefix: str) -> str:
        """Unique for the whole process; with `reuse_ids`, unique only on this connection (1, 2, … per prefix)."""
        if self._id_counters is None:
            return _new_id(prefix)
        return f"{prefix}_fake{next(self._id_counters.setdefault(prefix, itertools.count(1))):06d}"

    def _emit(self, kind: str, **fields: Any) -> None:
        if self._closed is None:
            self._queue.put_nowait(json.dumps({"type": kind, "event_id": self._new_id("event"), **fields}))

    def _error(self, code: str, message: str, client_event: dict | None = None, *, param: str | None = None,
               kind: str = "invalid_request_error") -> None:
        self._emit("error", error={"type": kind, "code": code, "message": message, "param": param,
                                   "event_id": (client_event or {}).get("event_id")})

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:  # a bug in the fake: say so on the socket
            self._error("fake_internal_error", repr(task.exception()), kind="server_error")

    def _add_item(self, item: dict) -> None:
        self._items[item["id"]] = item
        self._last_item_id = item["id"]

    def _input(self) -> dict:
        return (self.session or {}).get("audio", {}).get("input", {})

    # --- client events ----------------------------------------------------------------------------------------

    _HANDLERS = {
        "session.update": "_on_session_update",
        "input_audio_buffer.append": "_on_append",
        "input_audio_buffer.commit": "_on_commit",
        "input_audio_buffer.clear": "_on_clear",
        "conversation.item.create": "_on_item_create",
        "conversation.item.truncate": "_on_truncate",
        "conversation.item.delete": "_on_item_delete",
        "conversation.item.retrieve": "_on_item_retrieve",
        "response.create": "_on_response_create",
        "response.cancel": "_on_response_cancel",
    }

    def _on_session_update(self, event: dict) -> None:
        update = event.get("session")
        if not isinstance(update, dict):
            self._reject(event, "session", "session.update needs a `session` object")
            return
        first = self.session is None
        problem = check_session(update, first=first, url_model=self._url_model)
        if problem:
            self._reject(event, *problem)
            return
        self.session = merge_session(self.session or default_session(self._url_model, self._new_id("sess")), update)
        self._emit("session.created" if first else "session.updated", session=self.session)

    def _reject(self, event: dict, param: str, message: str) -> None:
        """Invalid session.update: `error`, then the server closes the session (doc/gotchas.md)."""
        self._error("invalid_value", message, event, param=param)
        self.server_close(INVALID_SESSION_CLOSE, "invalid session configuration")

    def _on_append(self, event: dict) -> None:
        audio = event.get("audio")
        if not isinstance(audio, str) or not audio:
            self._error("missing_audio", "input_audio_buffer.append needs base64 `audio`", event, param="audio")
            return
        if len(audio) > MAX_APPEND_B64:
            self._error("audio_too_large", "input_audio_buffer.append is limited to 1 MiB of base64", event,
                        param="audio")
            return
        try:
            raw = base64.b64decode(audio, validate=True)
        except (binascii.Error, ValueError):
            self._error("invalid_audio", "`audio` is not valid base64", event, param="audio")
            return
        fmt = self._input().get("format") or {}
        rate = fmt.get("rate", RATE) if fmt.get("type", "audio/pcm") == "audio/pcm" else RATE
        ms = len(raw) / 2 / rate * 1000  # PCM16 mono
        self._buffer_ms += ms
        self._audio_ms += ms
        if self._input().get("turn_detection"):
            self._run_vad()

    def _run_vad(self) -> None:
        options = self.options
        if self._vad == "idle" and self._buffer_ms >= options.speech_start_s * 1000:
            self._vad = "speaking"
            self._speech_item = self._new_id("item")
            self._emit("input_audio_buffer.speech_started", audio_start_ms=int(self._audio_ms - self._buffer_ms),
                       item_id=self._speech_item)
        if self._vad == "speaking" and self._buffer_ms >= options.speech_stop_s * 1000:
            self._vad = "stopped"
            item_id = self._speech_item or self._new_id("item")
            self._emit("input_audio_buffer.speech_stopped", audio_end_ms=int(self._audio_ms), item_id=item_id)
            self._commit(item_id)
            if self._response is not None:  # a new user turn interrupts whatever was still being generated
                self._cancel_response()
            self._start_response()

    def _commit(self, item_id: str) -> None:
        previous = self._last_item_id
        item = {"id": item_id, "object": "realtime.item", "type": "message", "status": "completed",
                "role": "user", "content": [{"type": "input_audio", "transcript": None}]}
        self._add_item(item)
        self._buffer_ms = 0.0
        self._user_text = FAKE_TRANSCRIPT
        self._emit("input_audio_buffer.committed", previous_item_id=previous, item_id=item_id)
        self._emit("conversation.item.added", previous_item_id=previous, item=item)
        transcription = self._input().get("transcription") or {}
        if transcription.get("model"):
            self._emit("conversation.item.input_audio_transcription.completed", item_id=item_id, content_index=0,
                       transcript=FAKE_TRANSCRIPT)

    def _on_commit(self, event: dict) -> None:
        if self._response is not None:
            self._error("voice_output_task_ongoing", "a response is in progress", event)
            return
        if self._buffer_ms < MIN_COMMIT_MS:
            self._error("input_audio_buffer_commit_empty", f"buffer too small: expected at least {MIN_COMMIT_MS}ms "
                        f"of audio, got {self._buffer_ms:.0f}ms", event)
            return
        self._vad = "stopped"
        self._commit(self._new_id("item"))

    def _on_clear(self, event: dict) -> None:
        self._buffer_ms = 0.0
        self._vad = "idle"
        self._speech_item = None
        self._emit("input_audio_buffer.cleared")

    def _on_item_create(self, event: dict) -> None:
        item = event.get("item")
        if not isinstance(item, dict):
            self._error("invalid_value", "conversation.item.create needs an `item` object", event, param="item")
            return
        kind = item.get("type")
        stored = {**copy.deepcopy(item), "id": item.get("id") or self._new_id("item"), "object": "realtime.item",
                  "status": "completed"}
        if kind == "message":
            role = item.get("role")
            if role not in ("user", "assistant"):
                self._error("invalid_value", f"message role {role!r} is not supported", event, param="item.role")
                return
            content = [part for part in item.get("content") or []
                       if isinstance(part, dict) and part.get("type") in ("input_text", "text", "output_text")]
            stored["content"] = content
            text = " ".join(str(part.get("text") or "") for part in content).strip()
            if role == "user" and text:
                self._user_text = text
        elif kind == "function_call_output":
            if not item.get("call_id"):
                self._error("invalid_value", "function_call_output needs a call_id", event, param="item.call_id")
                return
            if not isinstance(item.get("output"), str):
                self._error("invalid_type", "function_call_output.output must be a string (json.dumps it)", event,
                            param="item.output")
                return
        elif kind != "function_call":
            self._error("invalid_value", f"unsupported item type {kind!r}", event, param="item.type")
            return
        previous = self._last_item_id
        self._add_item(stored)
        self._emit("conversation.item.added", previous_item_id=previous, item=stored)

    def _on_truncate(self, event: dict) -> None:
        item_id = event.get("item_id")
        item = self._items.get(item_id) if isinstance(item_id, str) else None
        if item is None or item.get("role") != "assistant":
            self._error("item_not_found", f"no assistant item {item_id!r} to truncate", event, param="item_id")
            return
        end = event.get("audio_end_ms")
        if not isinstance(end, int) or isinstance(end, bool) or end < 0:
            self._error("invalid_value", "audio_end_ms must be a whole number ≥ 0", event, param="audio_end_ms")
            return
        self._emit("conversation.item.truncated", item_id=item_id, content_index=event.get("content_index", 0),
                   audio_end_ms=min(end, self._item_audio_ms.get(item_id, 0)))

    def _on_item_delete(self, event: dict) -> None:
        item_id = event.get("item_id")
        if self._items.pop(item_id, None) is None:
            self._error("item_not_found", f"no item {item_id!r}", event, param="item_id")
            return
        self._emit("conversation.item.deleted", item_id=item_id)

    def _on_item_retrieve(self, event: dict) -> None:
        item = self._items.get(event.get("item_id"))
        if item is None:
            self._error("item_not_found", f"no item {event.get('item_id')!r}", event, param="item_id")
            return
        self._emit("conversation.item.retrieved", item=item)

    def _on_response_create(self, event: dict) -> None:
        if self._response is not None:
            self._error("voice_output_task_ongoing", "a response is in progress: cancel it or wait for response.done",
                        event)
            return
        overrides = event.get("response") if isinstance(event.get("response"), dict) else {}
        self._start_response(overrides.get("instructions"))

    def _on_response_cancel(self, event: dict) -> None:
        if self._response is None:  # Higgs's documented code (api-realtime-client-events.md, response.cancel)
            self._error("response_not_active", "no active response", event)
            return
        self._cancel_response()

    # --- generating a reply -----------------------------------------------------------------------------------

    def _start_response(self, instructions: str | None = None) -> None:
        session = self.session or {}
        tool, args, text = choose_reply(instructions or session.get("instructions") or "", self._user_text,
                                        session.get("tools") or [])
        reply = _Reply(id=self._new_id("resp"), tool=tool, args=args, text=text, item_id=self._new_id("item"),
                       call_id=self._new_id("call") if tool else None,
                       call_item_id=self._new_id("item") if tool else None,
                       text_mode=session.get("output_modalities") == ["text"])
        self._response = reply
        reply.task = self._spawn(self._play(reply))

    def _response_obj(self, reply: _Reply, status: str, output: list[dict]) -> dict:
        session = self.session or {}
        return {"object": "realtime.response", "id": reply.id, "status": status, "status_details": None,
                "output": output, "conversation_id": self._conversation_id,
                "output_modalities": session.get("output_modalities", ["audio"]),
                "max_output_tokens": session.get("max_output_tokens", "inf"),
                "audio": {"output": copy.deepcopy(session.get("audio", {}).get("output", {}))},
                "usage": None, "metadata": None}

    async def _play(self, reply: _Reply) -> None:
        options = self.options
        await options.sleep(options.think_s)
        reply.created = True
        self._emit("response.created", response=self._response_obj(reply, "in_progress", []))
        message = {"id": reply.item_id, "object": "realtime.item", "type": "message", "status": "in_progress",
                   "role": "assistant", "content": []}
        previous = self._last_item_id
        self._add_item(dict(message))
        self._emit("response.output_item.added", response_id=reply.id, output_index=0, item=message)
        self._emit("conversation.item.added", previous_item_id=previous, item=message)  # unknown to the controller
        where = {"response_id": reply.id, "item_id": reply.item_id, "output_index": 0, "content_index": 0}
        part = "text" if reply.text_mode else "audio"
        self._emit("response.content_part.added", **where, part={"type": part, "transcript": ""})
        words = reply.text.split()
        count = max(1, round(options.reply_s * 1000 / DELTA_MS))
        text_delta = "response.output_text.delta" if reply.text_mode else "response.output_audio_transcript.delta"
        sent = 0
        for index in range(count):
            if not reply.text_mode:
                self._emit("response.output_audio.delta", **where, delta=tone_delta(index, count))
                self._item_audio_ms[reply.item_id] = (index + 1) * DELTA_MS
            due = (index + 1) * len(words) // count  # transcript words spread evenly over the audio
            while sent < due:
                piece = words[sent] if sent == 0 else " " + words[sent]
                reply.said += piece
                self._emit(text_delta, **where, delta=piece)
                sent += 1
            await options.sleep(DELTA_MS / 1000 / max(options.speed, 1e-6))
        if reply.text_mode:
            self._emit("response.output_text.done", response_id=reply.id, output_index=0, content_index=0,
                       text=reply.text)  # Higgs: no item_id here (doc/gotchas.md)
            content = [{"type": "output_text", "text": reply.text}]
        else:
            self._emit("response.output_audio_transcript.length", **where, length_ms=count * DELTA_MS)  # Higgs ext
            self._emit("response.output_audio.done", **where)
            self._emit("response.output_audio_transcript.done", **where, transcript=reply.text)
            content = [{"type": "output_audio", "transcript": reply.text}]
        self._emit("response.content_part.done", **where, part={"type": part, "transcript": reply.text})
        done_message = {**message, "status": "completed", "content": content}
        self._items[reply.item_id] = done_message
        self._emit("response.output_item.done", response_id=reply.id, output_index=0, item=done_message)
        output = [done_message]
        if reply.tool:
            output.append(self._announce_call(reply))
        self._response = None
        self._emit("response.done", response=self._response_obj(reply, "completed", output))

    def _announce_call(self, reply: _Reply) -> dict:
        arguments = json.dumps(reply.args)
        item = {"id": reply.call_item_id, "object": "realtime.item", "type": "function_call",
                "status": "in_progress", "name": reply.tool, "call_id": reply.call_id, "arguments": "",
                "executor": "client"}
        where = {"response_id": reply.id, "item_id": reply.call_item_id, "output_index": 1, "call_id": reply.call_id}
        self._emit("response.output_item.added", response_id=reply.id, output_index=1, item=item)
        self._emit("response.function_call_arguments.delta", **where, delta=arguments)
        self._emit("response.function_call_arguments.done", **where, name=reply.tool, arguments=arguments)
        done = {**item, "status": "completed", "arguments": arguments}
        self._add_item(done)
        reply.call_announced = True
        self._emit("response.output_item.done", response_id=reply.id, output_index=1, item=done)
        return done

    def _cancel_response(self) -> None:
        reply, self._response = self._response, None
        if reply is None:
            return
        if reply.task is not None and reply.task is not asyncio.current_task():
            reply.task.cancel()
        if not reply.created:  # still "thinking": nothing was announced, so nothing to report
            return
        output = []
        message = self._items.get(reply.item_id)
        if message is not None:
            partial = {**message, "status": "incomplete",
                       "content": [{"type": "output_text" if reply.text_mode else "output_audio",
                                    "transcript": reply.said}]}
            self._items[reply.item_id] = partial
            output.append(partial)
        if reply.call_announced and reply.call_item_id in self._items:
            output.append(self._items[reply.call_item_id])
        self._emit("response.done", response=self._response_obj(reply, "cancelled", output))


class FakeConnector:
    """A `connect` function with fixed knobs that remembers its connections (tests).

        connect = FakeConnector(sleep=instant, speed=1000)
        link = RealtimeLink(settings, ..., connect=connect)
        connect.connections[-1].received        # what the link sent on the latest connection
    """

    def __init__(self, options: FakeOptions | None = None, **knobs: Any) -> None:
        self.options = replace(options or FakeOptions(), **knobs)
        self.connections: list[FakeRealtimeConn] = []

    async def __call__(self, url: str, **kwargs: Any) -> FakeRealtimeConn:
        conn = FakeRealtimeConn(url, self.options, headers=kwargs.get("additional_headers"))
        self.connections.append(conn)
        return conn


async def fake_connect(url: str, **kwargs: Any) -> FakeRealtimeConn:
    """Drop-in for `websockets.connect(url, additional_headers=..., max_size=None, open_timeout=10)`."""
    return FakeRealtimeConn(url, headers=kwargs.get("additional_headers"))
