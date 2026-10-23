"""The presenter state machine (design §5): narration, live turns, deck tools, resume.

The server feeds a Controller every deck message (`on_browser`), every Higgs Realtime server event (`on_realtime`)
and every link status change (`on_realtime_status`); the Controller answers only through the BrowserPort and the
RealtimePort. All public handlers and timer callbacks run under one asyncio.Lock, never raise (a bug is logged and
shown as an `error` message), and ignore message/event types they don't know.

States (brief Appendix C): IDLE · NARRATING · PAUSED · LISTENING (mic open) · REPLYING (live reply, mic closed).
A live turn remembers its `origin` (narration / paused / idle) so it can go back there when it ends without a tool.
"""

from __future__ import annotations

import asyncio
import copy
import math
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from . import tools
from .config import RATE, STT_MODEL, Settings
from .ports import AsyncioClock, BrowserPort, Clock, LogFn, RealtimePort, Timer, null_log
from .prompts import TurnContext, build_instructions
from .talk import Slide, Talk
from .timeline import CutPoint, RenderedChunk, Word, cut_point, render_version

IDLE = "IDLE"
NARRATING = "NARRATING"
PAUSED = "PAUSED"
LISTENING = "LISTENING"
REPLYING = "REPLYING"
STATES = (IDLE, NARRATING, PAUSED, LISTENING, REPLYING)

ORIGIN_NARRATION = "narration"
ORIGIN_PAUSED = "paused"
ORIGIN_IDLE = "idle"

LISTEN_SILENCE_S = 8.0  # tap mode: no speech_started this long after the floor opens → as Esc
LISTEN_MAX_S = 30.0  # a turn that never ends: tap mode sends what was heard (speech started), else as Esc
REPLY_WAIT_S = 8.0  # end of the user's turn → response.created, else back to origin
REPLY_STALL_S = 8.0  # a bound reply with no event of its own this long (and no response.done) → treat it as done
ERROR_GRACE_S = 1.5  # after a non-benign `error` mid-reply: this long without an event of the reply → as a stall
DOUBLE_SEND_S = 1.0  # tap mode: Space this soon after a spoken turn ended (still thinking) was a "send": ignored
SEND_NOW_MIN_FRAMES = 5  # tap mode: "send now" needs this many 100 ms mic frames (a quick double tap sends nothing)
COUNTDOWN_S = 3  # "Resuming…" after a reply that called no tool
DRAIN_GRACE_S = 2.0  # drain fallback = remaining reply audio + this
BYTES_PER_MS = RATE * 2 // 1000  # PCM16 mono at 24 kHz: 48 bytes per ms
MAX_VAD_TAIL_MS = 5000  # speech_stopped.audio_end_ms this far behind the newest mic audio is not trusted
USER_ITEMS_KEPT = 16  # user audio item ids remembered to match late transcripts to their turn
SUMMARY_MAX_CHARS = 600
SUMMARY_PREFIX = "Context only, do not reply. Earlier live exchanges in this talk: "
TURN_TIMERS = ("listen_silence", "listen_max", "reply_wait", "reply_stall", "drain")
DROPPED = ("reconnecting", "error", "offline")
CUT_CALL_OUTPUT = tools.error_output("not run: the reply was cut off")  # calls of a cut/stalled/rejected reply
NOTHING_TO_CANCEL = frozenset({"response_not_active", "response_cancel_not_active"})  # Higgs docs; OpenAI
SEND_RACE_ERRORS = frozenset({"voice_output_task_ongoing", "input_audio_buffer_commit_empty"})

BANNER_HUMAN = "{presenter} has the floor"
BANNER_OPENING = "{presenter} has the floor — tap Space to hand over"
BANNER_QA = "Waiting for a question — tap Space"
BANNER_RESUMING = "Resuming…"
BANNER_PAUSED = "Paused — P to resume"
BANNER_PAUSED_R = "Paused — R to resume"
BANNER_OFFLINE = "Realtime offline — {presenter} has the floor (R resumes)"
BANNER_OFFLINE_BACK = "Realtime is back — {presenter} has the floor (R resumes)"
BANNER_END = "End of the talk"

ERR_SILENCE = "Didn't catch that"
ERR_TOO_LONG = "Turn too long — try MIC_MODE=hold"
ERR_TOO_LONG_HOLD = "Turn too long"
ERR_NO_REPLY = "No reply"
ERR_LINK_LOST = "Realtime connection lost"
ERR_OFFLINE_TYPED = "Realtime offline — the typed question was not sent"
ERROR_MAX_CHARS = 200


@dataclass(frozen=True)
class Cursor:
    """Where narration continues: chunk `chunk` of slide `slide`, from `offset_ms`. `resume_word` set ⇒ this is a
    resume after a cut, and the captions show the jump back to that word."""

    slide: str
    chunk: int
    offset_ms: int = 0
    resume_word: int | None = None


@dataclass
class Turn:
    """One live turn: the floor opened once (Space or a typed question) and at most one reply."""

    origin: str  # ORIGIN_*: where to go back to when the turn ends without a tool
    reply: int | None = None  # reply seq, allocated when the user's turn ends
    response_id: str | None = None  # the bound Realtime response
    audio_item_id: str | None = None  # for conversation.item.truncate
    expects_response: bool = False
    response_done: bool = False
    pending: list[dict] = field(default_factory=list)  # user item + response.create, held while a response is active
    queued: tools.ToolCall | None = None  # control tool applied on drained
    calls: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # call_id → (name, arguments)
    user_text: str = ""
    assistant_text: str = ""
    mic_frames: int = 0
    speech_started: bool = False  # turn detection heard speech in this turn
    user_items: set[str] = field(default_factory=set)  # the user audio item ids of this turn (transcripts)
    audio_bytes: int = 0
    ended_by: str | None = None  # how the user's turn ended: vad · hand (Space) · listen_max · hold · typed
    stream_start_ms: float = 0.0  # session input-stream position when this turn's floor opened
    marks: list[tuple[float, float]] = field(default_factory=list)  # (stream position after a mic frame, arrival)
    turn_end_at: float | None = None  # when the user's turn ended (event, key or send)
    speech_end_at: float | None = None  # end of the user's speech (latency metric): estimated, else turn_end_at
    speech_end_estimated: bool = False
    first_audio_at: float | None = None
    remembered: bool = False  # stored in the history for the reconnect summary
    history_index: int | None = None  # its entry in the history (a late transcript fills it in)


def summary_text(exchanges: list[tuple[str, str]], limit: int = SUMMARY_MAX_CHARS) -> str | None:
    """The single reconnect summary (≤ `limit` chars): the most recent exchanges that fit, oldest first.
    None when there is nothing to summarise."""
    parts = [_exchange(user.strip(), assistant.strip()) for user, assistant in exchanges]
    parts = [part for part in parts if part]
    if not parts:
        return None
    budget = limit - len(SUMMARY_PREFIX)
    kept: list[str] = []
    used = 0
    for part in reversed(parts):
        extra = len(part) + (1 if kept else 0)
        if used + extra > budget:
            if not kept:
                kept.append(_clip(part, budget))
            break
        kept.append(part)
        used += extra
    return SUMMARY_PREFIX + " ".join(reversed(kept))


def _exchange(user: str, assistant: str) -> str:
    said = []
    if user:
        said.append(f"Someone said {_quoted(user)}")
    if assistant:
        said.append(f"You answered {_quoted(assistant)}")
    return " ".join(said)


def _quoted(text: str) -> str:
    """"text" closed as a sentence: a full stop only when the text doesn't already end one."""
    return f'"{text}"' + ("" if text[-1:] in ".!?…" else ".")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _int(value: Any) -> int | None:
    """An int from a JSON number (bools and non-finite floats are not numbers here)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _b64_bytes(data: str) -> int:
    """Decoded length of a base64 string, without decoding it."""
    padding = 2 if data.endswith("==") else 1 if data.endswith("=") else 0
    return max(0, len(data) * 3 // 4 - padding)


def _user_message(text: str) -> dict:
    return {"type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


def _error_fields(event: dict) -> tuple[str, str, str]:
    error = event.get("error")
    if not isinstance(error, dict):
        return "", "", str(error or "")
    return str(error.get("type") or ""), str(error.get("code") or ""), str(error.get("message") or "")


SERVER_TOOLS = frozenset({"web_search"})  # Higgs runs these itself; the names are reserved (doc/higgs-realtime.md)


def _server_call(item: dict) -> bool:
    """A server-executed tool call (`executor: "server"`, e.g. web_search): the server runs it, adds its result and
    continues by itself, so the client must neither run it nor send a function_call_output. Relay declares no server
    tools today; this keeps a stray one from being rejected and answered. `function_call_arguments.done` carries no
    executor, so the reserved name decides there."""
    return item.get("executor") == "server" or item.get("name") in SERVER_TOOLS


def _benign_error(err_type: str, code: str, message: str) -> bool:
    """`response.cancel` with nothing to cancel: expected after a cut during "thinking"; log it, don't show it.
    Higgs answers it with code `response_not_active` (some identifiers ride in `type`); the wording check is a
    fallback for servers that phrase it differently."""
    if NOTHING_TO_CANCEL & {err_type.strip().lower(), code.strip().lower()}:
        return True
    text = f"{code} {message}".lower()
    return "cancel" in text and any(hint in text for hint in ("not_active", "not active", "no active", "nothing"))


class Controller:
    def __init__(self, talk: Talk, chunks: Mapping[tuple[str, int], RenderedChunk], settings: Settings, *,
                 browser: BrowserPort, realtime: RealtimePort, clock: Clock | None = None,
                 log: LogFn | None = None) -> None:
        self.talk = talk
        self.chunks = chunks
        self.settings = settings
        self.browser = browser
        self.realtime = realtime
        self.clock: Clock = clock or AsyncioClock()
        self._log = log or null_log
        self._lock = asyncio.Lock()

        self.state = IDLE
        self.slide_index = 0  # deck position of the slide on screen
        self.cursor: Cursor | None = None  # NARRATING: the current play; otherwise where narration continues
        self.play_id = 0
        self.started = False  # narration has started once (re-armed on a human slide before the first relay slide)
        self.floor_human = False
        self.floor_offline = False  # the presenter took the floor because Realtime was down: arrows move silently
        self.turn: Turn | None = None
        self.fragment: int | None = 0 if self.current_slide.is_relay else None  # visible on the current slide
        self.banner: str | None = None

        self._cut_info: tuple[str, str] | None = None  # (cut word, its sentence) for the STATE block
        self._mic_open = False
        self._timers: dict[str, tuple[object, Timer]] = {}
        self._reply_seq = 0
        # Per Realtime session (a new session numbers its ids afresh): forgotten on every `ready`.
        self._active: set[str] = set()  # responses created and not done yet (server side)
        self._ignored: set[str] = set()  # responses whose later events are ignored (cut, stalled or unexpected)
        self._answered: set[str] = set()  # call ids whose function_call_output went out (or waits in _owed)
        self._done: set[str] = set()  # responses whose response.done arrived
        self._owed: dict[str, list[dict]] = {}  # response id → function_call_outputs sent at its response.done
        self._call_items: dict[str, str] = {}  # call id → its function_call item id (outputs go right after it)
        self._user_items: dict[str, Turn] = {}  # user audio item id → its turn (late transcripts)
        self._stream_ms = 0.0  # mic audio appended to this session's input stream (latency estimate)
        self._history: list[tuple[str, str]] = []  # (user, assistant) per finished turn
        self._ready_count = 0
        self._connection: tuple[str, str | None] = (str(getattr(realtime, "status", "offline")), None)
        self.banner = self._phase_banner()
        self._last_state: dict | None = self._state_message()  # a connecting deck always gets it (forced)

        self._key_handlers: dict[str, Callable[[dict], Awaitable[None]]] = {
            "space": self._key_space, "space_up": self._key_space_up, "esc": self._key_esc,
            "next": self._key_next, "prev": self._key_prev, "pause": self._key_pause,
            "human": self._key_human, "relay": self._key_relay,
        }
        self._event_handlers: dict[str, Callable[[dict], Awaitable[None]]] = {
            "input_audio_buffer.speech_started": self._ev_speech_started,
            "input_audio_buffer.speech_stopped": self._ev_speech_stopped,
            "input_audio_buffer.committed": self._ev_committed,
            "conversation.item.input_audio_transcription.completed": self._ev_transcription,
            "response.created": self._ev_response_created,
            "response.output_audio.delta": self._ev_audio_delta,
            "response.output_audio_transcript.delta": self._ev_transcript_delta,
            "response.output_audio_transcript.done": self._ev_transcript_done,
            "response.function_call_arguments.done": self._ev_call_done,
            "response.done": self._ev_response_done,
            "error": self._ev_error,
            "session.created": self._ev_session,
            "session.updated": self._ev_session,
        }

    # --- public interface (design §5) -----------------------------------------------------------------------

    async def on_browser(self, msg: dict) -> None:
        """Every message from the deck."""
        async with self._lock:
            start_link = await self._guard("browser", self._browser_message, msg)
        if start_link:
            await self._start_link()  # outside the lock: the link reports its status back through on_realtime_status

    async def on_browser_connected(self) -> None:
        """A deck connected: hello + slide + state + connection."""
        async with self._lock:
            await self._guard("browser_connected", self._greet)

    async def on_browser_disconnected(self) -> None:
        """The deck went away: interrupt everything → IDLE (the cursor is kept, R resumes)."""
        async with self._lock:
            await self._guard("browser_disconnected", self._browser_gone)

    async def on_realtime(self, event: dict) -> None:
        """Every Higgs Realtime server event."""
        async with self._lock:
            await self._guard("realtime", self._realtime_event, event)

    async def on_realtime_status(self, status: str, detail: str | None = None) -> None:
        """offline | connecting | ready | reconnecting | error, from the RealtimeLink."""
        async with self._lock:
            await self._guard("realtime_status", self._realtime_status, status, detail)

    def session_config(self) -> dict:
        """The full session object (design §6) for the current context: the first session.update of a connection."""
        turn_detection = self.settings.session_turn_detection
        config: dict[str, Any] = {
            "type": "realtime",
            "model": self.settings.realtime_model,
            "instructions": self._instructions(),
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": RATE},
                    "transcription": {"model": STT_MODEL, "language": "en"},
                    "turn_detection": copy.deepcopy(turn_detection),
                },
                "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": self.talk.voice},
            },
            "tools": tools.tool_specs(self.talk),
            "tool_choice": "auto",
        }
        if self.settings.temperature is not None:
            config["temperature"] = self.settings.temperature
        return config

    def snapshot(self) -> dict:
        status, detail = self._connection
        return {
            "state": self.state, "phase": self.phase, "slide": self.current_slide.id, "banner": self.banner,
            "thinking": self.thinking, "can_resume": self.can_resume, "mic_mode": self.settings.mic_mode,
            "connection": {"status": status, "detail": detail}, "talk": self.talk.id,
        }

    # --- derived state ----------------------------------------------------------------------------------------

    @property
    def current_slide(self) -> Slide:
        return self.talk.slides[self.slide_index]

    @property
    def phase(self) -> str:
        if not self.started:
            return "opening"
        slide = self.current_slide
        if self.floor_human or not slide.is_relay:
            return "human"
        return "qa" if slide.is_qa else "presenting"

    @property
    def thinking(self) -> bool:
        return self.state == REPLYING and self.turn is not None and self.turn.first_audio_at is None

    @property
    def can_resume(self) -> bool:
        return self.cursor is not None and self.state != NARRATING

    # --- guard, logging, sending ------------------------------------------------------------------------------

    async def _guard(self, where: str, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """Run one handler: exceptions are logged and shown, never raised; the state goes out if it changed."""
        result = None
        try:
            result = await fn(*args)
        except Exception as exc:  # noqa: BLE001 - a controller bug must never take the show down
            self._note("controller_error", {"where": where, "error": repr(exc),
                                            "traceback": traceback.format_exc(limit=6)})
            await self._send_error(f"Relay error ({where}): {exc!r}")
        try:
            await self._sync_state()
        except Exception as exc:  # noqa: BLE001
            self._note("controller_error", {"where": "sync_state", "error": repr(exc)})
        return result

    def _note(self, kind: str, data: dict) -> None:
        try:
            self._log(kind, data)
        except Exception:  # noqa: BLE001 - logging must never break a handler
            pass

    async def _send(self, msg: dict) -> None:
        try:
            await self.browser.send(msg)
        except Exception as exc:  # noqa: BLE001
            self._note("browser_send_failed", {"type": msg.get("type"), "error": repr(exc)})

    async def _send_error(self, message: str) -> None:
        await self._send({"type": "error", "message": _clip(message, ERROR_MAX_CHARS)})

    async def _rt(self, event: dict) -> bool:
        """Send one Realtime client event; False (never an exception) when the link is down."""
        try:
            sent = bool(await self.realtime.send(event))
        except Exception as exc:  # noqa: BLE001
            self._note("realtime_send_failed", {"type": event.get("type"), "error": repr(exc)})
            return False
        if not sent and event.get("type") != "input_audio_buffer.append":
            self._note("realtime_not_sent", {"type": event.get("type")})
        return sent

    async def _start_link(self) -> None:
        try:
            await self.realtime.start()
        except Exception as exc:  # noqa: BLE001
            self._note("controller_error", {"where": "realtime.start", "error": repr(exc)})

    def _state_message(self) -> dict:
        return {"type": "state", "state": self.state, "phase": self.phase, "slide": self.current_slide.id,
                "banner": self.banner, "thinking": self.thinking, "can_resume": self.can_resume,
                "mic_mode": self.settings.mic_mode}

    async def _sync_state(self, *, force: bool = False) -> None:
        msg = self._state_message()
        if force or msg != self._last_state:
            self._last_state = msg
            await self._send(msg)

    def _set_state(self, state: str, banner: str | None) -> None:
        previous = self.state
        self.state, self.banner = state, banner
        if previous != state:
            self._note("state", {"from": previous, "to": state, "slide": self.current_slide.id,
                                 "phase": self.phase, "banner": banner})

    def _banner_human(self) -> str:
        return BANNER_HUMAN.format(presenter=self.talk.presenter)

    def _phase_banner(self) -> str:
        """Why Relay is silent in IDLE, by phase."""
        phase = self.phase
        if phase == "opening":
            return BANNER_OPENING.format(presenter=self.talk.presenter)
        if phase == "human":
            return self._banner_human()
        if phase == "qa":
            return BANNER_QA
        return BANNER_PAUSED_R

    # --- timers -----------------------------------------------------------------------------------------------

    def _start_timer(self, name: str, delay: float, fn: Callable[[], Awaitable[None]]) -> None:
        """(Re)start the timer `name`. Its callback runs under the lock, and only if it is still the current one
        (a cancelled timer's callback may already be queued behind the lock)."""
        self._cancel_timer(name)
        token = object()

        async def fire() -> None:
            async with self._lock:
                entry = self._timers.get(name)
                if entry is None or entry[0] is not token:
                    return
                del self._timers[name]
                await self._guard(f"timer {name}", fn)

        self._timers[name] = (token, self.clock.call_later(delay, fire))

    def _cancel_timer(self, name: str) -> bool:
        entry = self._timers.pop(name, None)
        if entry is None:
            return False
        entry[1].cancel()
        return True

    def _countdown_running(self) -> bool:
        return "countdown" in self._timers

    async def _stop_countdown(self) -> bool:
        if self._cancel_timer("countdown"):
            await self._send({"type": "countdown", "seconds": 0})
            return True
        return False

    # --- browser messages -------------------------------------------------------------------------------------

    async def _browser_message(self, msg: Any) -> bool:
        """Dispatch one deck message. Returns True when the Realtime link should be started (after the lock)."""
        if not isinstance(msg, dict):
            return False
        kind = msg.get("type")
        if kind == "mic":
            await self._on_mic(msg)
        elif kind == "key":
            await self._on_key(msg)
        elif kind == "log":
            self._note("client_log", {"level": msg.get("level"), "message": msg.get("message")})
        elif kind in ("hello", "arm", "chunk_ended", "reply_drained", "typed"):
            self._note("browser", dict(msg))
            if kind == "hello":
                await self._greet()
            elif kind == "arm":
                return True
            elif kind == "chunk_ended":
                await self._on_chunk_ended(msg)
            elif kind == "reply_drained":
                await self._on_reply_drained(msg)
            else:
                await self._on_typed(msg)
        return False

    async def _greet(self) -> None:
        """hello + slide + state + connection for a (re)connected deck. A deck that connects mid-turn or
        mid-narration starts silent, so Relay goes IDLE first (cursor kept: R resumes)."""
        if self.state in (NARRATING, LISTENING, REPLYING):
            await self._halt(None, cut=False)
            self._set_state(IDLE, BANNER_PAUSED_R)
        await self._send({"type": "hello", "talk": self.talk.id, "version": self._version(),
                          "mic_mode": self.settings.mic_mode, "snapshot": self.snapshot()})
        if self.state != NARRATING:
            await self._send({"type": "stop"})
        if not self._mic_open:
            await self._send({"type": "mic", "open": False})
        await self._send({"type": "slide", "slide": self.current_slide.id, "fragment": self.fragment})
        await self._sync_state(force=True)
        status, detail = self._connection
        await self._send({"type": "connection", "status": status, "detail": detail})

    def _version(self) -> str | None:
        """Identity of what this server run presents (`/api/talk` carries the same value): a deck that sees another
        one after a reconnect is talking to a restarted server with other narration, and reloads."""
        try:
            return render_version(self.talk, dict(self.chunks))
        except Exception as exc:  # noqa: BLE001 - the hello must go out whatever happens
            self._note("controller_error", {"where": "render_version", "error": repr(exc)})
            return None

    async def _browser_gone(self) -> None:
        await self._halt(None, cut=False)
        self._mic_open = False
        self._set_state(IDLE, BANNER_PAUSED_R if self.cursor is not None else self._phase_banner())

    async def _on_mic(self, msg: dict) -> None:
        turn = self.turn
        pcm = msg.get("pcm")
        if self.state != LISTENING or not self._mic_open or turn is None or not isinstance(pcm, str) or not pcm:
            return
        turn.mic_frames += 1
        if await self._rt({"type": "input_audio_buffer.append", "audio": pcm}):
            self._stream_ms += _b64_bytes(pcm) / BYTES_PER_MS
            turn.marks.append((self._stream_ms, self.clock.now()))  # where the stream was, when (latency)

    async def _on_key(self, msg: dict) -> None:
        key = msg.get("key")
        self._note("key", {"key": key, "state": self.state, "at": msg.get("at"), "reply": msg.get("reply"),
                           "reply_played_ms": msg.get("reply_played_ms")})
        handler = self._key_handlers.get(key) if isinstance(key, str) else None
        if handler is None:
            return
        if key == "space" and self._double_send(self.turn):
            self._note("double_send_ignored", {"reply": self.turn.reply if self.turn else None,
                                               "ended_by": self.turn.ended_by if self.turn else None})
            return
        if key != "space_up":  # presenter keys always win: any key press discards a queued model action
            await self._discard_queued(f"key {key}")
        await handler(msg)

    def _double_send(self, turn: Turn | None) -> bool:
        """Tap mode: a Space within DOUBLE_SEND_S of the end of a spoken turn, before any reply audio, was meant as
        "send" (turn detection, or a first Space, had just sent it): cutting the reply now would lose the question."""
        return (self.settings.mic_mode != "hold" and self.state == REPLYING and turn is not None
                and turn.ended_by in ("vad", "hand", "listen_max") and turn.first_audio_at is None
                and not turn.response_done and turn.turn_end_at is not None
                and self.clock.now() - turn.turn_end_at < DOUBLE_SEND_S)

    async def _key_space(self, msg: dict) -> None:
        if self.state == LISTENING:
            # Tap mode: Space again = "send now" (room noise can keep turn detection from ever ending the turn).
            # Under SEND_NOW_MIN_FRAMES of audio it is a bounce or a double tap: ignored, so near-silence is never
            # sent as a question. Hold mode: Space is already down.
            turn = self.turn
            if self.settings.mic_mode != "hold" and turn is not None:
                if turn.mic_frames >= SEND_NOW_MIN_FRAMES:
                    await self._send_turn(turn, "hand")
                else:
                    self._note("send_now_too_early", {"frames": turn.mic_frames})
            return
        if not self.realtime.ready:
            await self._offline_floor(msg)
            return
        turn = await self._take_floor(msg)
        await self._open_floor(turn)

    async def _key_space_up(self, msg: dict) -> None:
        turn = self.turn
        if self.settings.mic_mode != "hold" or self.state != LISTENING or turn is None:
            return
        await self._send_turn(turn, "hold")

    async def _key_esc(self, msg: dict) -> None:
        if self._countdown_running():
            await self._stop_countdown()
            self._set_state(IDLE, BANNER_PAUSED_R)
        elif self.state == LISTENING:
            await self._close_mic(clear=True)
            await self._back_to_origin()
        elif self.state == REPLYING:
            await self._cut_reply(msg)
            await self._back_to_origin()

    async def _key_next(self, msg: dict) -> None:
        await self._step(msg, +1)

    async def _key_prev(self, msg: dict) -> None:
        await self._step(msg, -1)

    async def _step(self, msg: dict, delta: int) -> None:
        target = self.slide_index + delta
        if not 0 <= target < len(self.talk.slides):
            await self._undo_local_stop(msg)
            return
        await self._halt(msg, cut=False)
        if self.floor_offline and self.state == IDLE:  # Realtime down, the presenter has the floor: no narration
            await self._show(target)
        else:
            await self._goto(target)

    async def _undo_local_stop(self, msg: dict) -> None:
        """The deck stops narration and flushes the reply on → ← before telling us; at the deck edge the key is
        ignored, so put narration back where it was, and end a flushed reply properly. A running countdown (which
        the deck hides on any key) is cancelled: presenter keys always win over the watchdog."""
        if self._countdown_running():
            await self._stop_countdown()
            self._set_state(IDLE, BANNER_PAUSED_R)
        elif self.state == NARRATING:
            await self._play(self._pause_cursor(self._position(msg.get("at"))))
        elif self.state == REPLYING:
            await self._cut_reply(msg)
            await self._back_to_origin()

    async def _key_pause(self, msg: dict) -> None:
        if self.state == NARRATING:
            await self._send({"type": "stop"})
            self.cursor = self._pause_cursor(self._position(msg.get("at")))
            self._set_state(PAUSED, BANNER_PAUSED)
        elif self.state == PAUSED and self.cursor is not None:
            await self._play(self.cursor)
        elif self._countdown_running():  # P during "Resuming…": stay paused at the resume point
            await self._stop_countdown()
            self._set_state(PAUSED, BANNER_PAUSED)

    async def _key_human(self, msg: dict) -> None:
        await self._halt(msg, cut=True)
        self.floor_human = True
        self._set_state(IDLE, self._banner_human())

    async def _key_relay(self, msg: dict) -> None:
        if self.state == NARRATING:  # Relay already has the floor; restarting the chunk would only jump back
            return
        await self._halt(msg, cut=False)
        await self._resume("key")

    async def _on_chunk_ended(self, msg: dict) -> None:
        cursor = self.cursor
        if self.state != NARRATING or cursor is None or _int(msg.get("play_id")) != self.play_id:
            self._note("chunk_ended_ignored", {"play_id": msg.get("play_id"), "current": self.play_id,
                                               "state": self.state})
            return
        slide = self.talk.slide(cursor.slide)
        if cursor.chunk + 1 < len(slide.chunks):
            await self._play(Cursor(slide.id, cursor.chunk + 1))
            return
        self.cursor = None
        if slide.is_qa:
            self._set_state(IDLE, BANNER_QA)
            return
        following = slide.index + 1
        if following < len(self.talk.slides) and self.talk.slides[following].is_relay:
            await self._play(Cursor(self.talk.slides[following].id, 0), show_slide=True)
        elif following < len(self.talk.slides):
            self._set_state(IDLE, self._banner_human())
        else:
            self._set_state(IDLE, BANNER_END)

    async def _on_reply_drained(self, msg: dict) -> None:
        turn = self.turn
        if self.state != REPLYING or turn is None or not turn.response_done or _int(msg.get("reply")) != turn.reply:
            return
        await self._drained("browser")

    async def _on_typed(self, msg: dict) -> None:
        text = " ".join(str(msg.get("text") or "").split())
        await self._discard_queued("typed question")
        if not text:
            await self._undo_local_stop(msg)
            return
        if not self.realtime.ready:
            await self._offline_floor(msg)
            await self._send_error(ERR_OFFLINE_TYPED)
            return
        turn = await self._take_floor(msg)
        self.turn = turn
        turn.user_text = text
        await self._rt(self._turn_update())
        await self._request_response(turn, _user_message(text))
        self._enter_replying(turn, "typed")
        await self._send({"type": "caption", "role": "user", "text": text, "final": True, "reply": turn.reply})

    # --- live turn mechanics ----------------------------------------------------------------------------------

    async def _take_floor(self, msg: dict) -> Turn:
        """Stop Relay for a new live turn (Space or typed): cut the narration or the reply, close the mic.
        Returns the new turn with its origin; a turn cut during its reply keeps that turn's origin."""
        countdown = await self._stop_countdown()
        previous = self.turn
        if self.state in (LISTENING, REPLYING) and previous is not None:
            origin = previous.origin
        elif self.state == NARRATING or countdown:
            origin = ORIGIN_NARRATION
        elif self.state == PAUSED:
            origin = ORIGIN_PAUSED
        else:
            origin = ORIGIN_IDLE
        await self._halt(msg, cut=True)
        self.floor_offline = False
        return Turn(origin=origin)

    async def _offline_floor(self, msg: dict) -> None:
        """Realtime is down: the tap gives the presenter the floor (narration stops at the cut; R resumes). Until
        narration starts again, → / ← only move the deck (the presenter is talking over it)."""
        await self._take_floor(msg)
        self.floor_offline = True
        self._set_state(IDLE, BANNER_OFFLINE.format(presenter=self.talk.presenter))

    async def _open_floor(self, turn: Turn) -> None:
        self.turn = turn
        turn.stream_start_ms = self._stream_ms
        await self._rt(self._turn_update())
        await self._rt({"type": "input_audio_buffer.clear"})
        self._mic_open = True
        await self._send({"type": "mic", "open": True})
        self._set_state(LISTENING, None)
        if self.settings.mic_mode != "hold":
            self._start_timer("listen_silence", LISTEN_SILENCE_S, self._on_listen_silence)
        self._start_timer("listen_max", LISTEN_MAX_S, self._on_listen_max)

    def _enter_replying(self, turn: Turn, ended_by: str) -> None:
        """The user's turn is over (speech_stopped, a commit or a typed question): wait for the reply."""
        self._reply_seq += 1
        turn.reply = self._reply_seq
        turn.expects_response = True
        turn.ended_by = ended_by
        turn.turn_end_at = turn.speech_end_at = self.clock.now()
        self._cancel_timer("listen_silence")
        self._cancel_timer("listen_max")
        self._start_timer("reply_wait", REPLY_WAIT_S, self._on_reply_wait)
        self._set_state(REPLYING, None)

    async def _send_turn(self, turn: Turn, how: str) -> None:
        """End the user's turn ourselves (hold-mode release, Space "send now", listen-max): commit what was heard
        and ask for the reply. Nothing reached us → clear, "Didn't catch that", back to origin (an empty commit is
        rejected)."""
        await self._close_mic(clear=False)
        if turn.mic_frames == 0:
            await self._rt({"type": "input_audio_buffer.clear"})
            await self._send_error(ERR_SILENCE)
            await self._back_to_origin()
            return
        await self._request_response(turn, {"type": "input_audio_buffer.commit"})
        self._enter_replying(turn, how)
        self._note("turn_sent", {"how": how, "frames": turn.mic_frames, "reply": turn.reply})

    async def _request_response(self, turn: Turn, first: dict) -> None:
        """`first` (the user's message, or the commit of their audio) and response.create — or, while another
        response is still active, cancel that one and send both only after its response.done: Higgs rejects a
        commit (and, without turn detection, a create) while a response is active, and the cut response's tool
        calls get their outputs before the new user item."""
        events = [first, {"type": "response.create"}]
        if not self._active:
            for event in events:
                await self._rt(event)
            return
        turn.pending = events
        if self._active - self._ignored:
            self._ignored |= self._active
            await self._rt({"type": "response.cancel"})

    async def _send_pending(self) -> None:
        """The held-back user item + response.create, once no response is active any more."""
        turn = self.turn
        if (turn is not None and turn.pending and not self._active and self.state == REPLYING
                and turn.response_id is None):
            events, turn.pending = turn.pending, []
            for event in events:
                await self._rt(event)

    async def _close_mic(self, *, clear: bool) -> None:
        if self._mic_open:
            self._mic_open = False
            await self._send({"type": "mic", "open": False})
        if clear:
            await self._rt({"type": "input_audio_buffer.clear"})

    async def _cut_reply(self, msg: dict | None) -> None:
        """Cut the live reply off: cancel it if still generating, truncate the model's memory of it to what was
        actually played, flush the deck's reply player; its later events are ignored, and the tool calls it already
        announced never run (each gets an error output at its response.done)."""
        turn = self.turn
        if turn is None:
            return
        if not turn.response_done and (turn.response_id is not None or not turn.pending):
            await self._rt({"type": "response.cancel"})
        if turn.response_id is not None:
            self._ignored.add(turn.response_id)
            if not turn.response_done:
                for call_id in turn.calls:
                    await self._owe_output(turn.response_id, call_id, CUT_CALL_OUTPUT)
        played = None
        if turn.audio_item_id and turn.audio_bytes > 0:
            played = self._played_ms(msg, turn)
            await self._rt({"type": "conversation.item.truncate", "item_id": turn.audio_item_id,
                            "content_index": 0, "audio_end_ms": played})
        if turn.reply is not None:
            await self._send({"type": "reply_flush", "reply": turn.reply})
        self._note("reply_cut", {"reply": turn.reply, "response_id": turn.response_id, "played_ms": played,
                                 "response_done": turn.response_done})

    def _played_ms(self, msg: dict | None, turn: Turn) -> int:
        """Reply audio the room actually heard: the deck's count if it is about this reply, else an estimate
        from the wall clock; never more than was received."""
        received = turn.audio_bytes // BYTES_PER_MS
        reported = _int(msg.get("reply_played_ms")) if msg else None
        reply = _int(msg.get("reply")) if msg else None
        if reported is not None and (reply is None or reply == turn.reply):
            played = reported
        elif turn.first_audio_at is not None:
            played = round((self.clock.now() - turn.first_audio_at) * 1000)
        else:
            played = 0
        return max(0, min(played, received))

    async def _back_to_origin(self) -> None:
        """End the turn and return to where it started: narration plays from the cursor (the cut sentence)."""
        origin = self.turn.origin if self.turn is not None else ORIGIN_IDLE
        self._end_turn()
        if origin == ORIGIN_NARRATION and self.cursor is not None:
            await self._play(self.cursor)
        elif origin == ORIGIN_PAUSED and self.cursor is not None:
            self._set_state(PAUSED, BANNER_PAUSED)
        else:
            self._set_state(IDLE, self._phase_banner())

    def _end_turn(self) -> None:
        if self.turn is not None:
            self._remember(self.turn)
        self.turn = None
        for name in TURN_TIMERS:
            self._cancel_timer(name)

    def _remember(self, turn: Turn) -> None:
        """Store the finished turn for the reconnect summary (empty exchanges included: a late transcript may still
        fill one in; summary_text skips them)."""
        if turn.remembered:
            return
        turn.remembered = True
        turn.history_index = len(self._history)
        self._history.append((turn.user_text.strip(), turn.assistant_text.strip()))

    async def _discard_queued(self, reason: str) -> None:
        turn = self.turn
        if turn is None or turn.queued is None:
            return
        call, turn.queued = turn.queued, None
        await self._tool_message(call.call_id, call.name, call.args, "discarded")
        self._note("tool", {"call_id": call.call_id, "name": call.name, "args": call.args, "status": "discarded",
                            "reason": reason})

    async def _halt(self, msg: dict | None, *, cut: bool) -> None:
        """Interrupt everything: queued action, countdown, turn timers, mic, reply, narration. With `cut`,
        narration that was playing or paused is cut (the cursor moves to the cut sentence's start). The caller
        sets the next state."""
        await self._discard_queued("interrupted")
        await self._stop_countdown()
        if self.state == LISTENING:
            await self._close_mic(clear=True)
        elif self.state == REPLYING:
            await self._cut_reply(msg)
        elif self.state == NARRATING:
            await self._send({"type": "stop"})
            if cut:
                await self._cut_narration(msg.get("at") if msg else None)
        elif self.state == PAUSED and cut:
            await self._cut_paused()
        self._end_turn()

    async def _drained(self, reason: str) -> None:
        """The reply has finished playing: run the queued tool, else count down back to the script (origin
        narration), else return to PAUSED / IDLE."""
        turn = self.turn
        if turn is None:
            return
        self._note("drained", {"reason": reason, "reply": turn.reply,
                               "queued": turn.queued.name if turn.queued else None})
        self._end_turn()
        call, turn.queued = turn.queued, None
        if call is not None:
            await self._apply(call)
            await self._tool_message(call.call_id, call.name, call.args, "applied")
            self._note("tool", {"call_id": call.call_id, "name": call.name, "args": call.args, "status": "applied"})
        elif turn.origin == ORIGIN_NARRATION:
            self._set_state(IDLE, BANNER_RESUMING)
            await self._send({"type": "countdown", "seconds": COUNTDOWN_S})
            self._start_timer("countdown", COUNTDOWN_S, self._on_countdown)
        elif turn.origin == ORIGIN_PAUSED and self.cursor is not None:
            self._set_state(PAUSED, BANNER_PAUSED)
        else:
            self._set_state(IDLE, self._phase_banner())

    # --- narration --------------------------------------------------------------------------------------------

    def _fragments(self, slide: Slide, chunk: int) -> int | None:
        """Fragments visible while `chunk` plays (cumulative max); human slides show all (None)."""
        return slide.fragment_at(chunk) if slide.is_relay else None

    async def _play(self, cursor: Cursor, *, show_slide: bool = False) -> None:
        slide = self.talk.slide(cursor.slide)
        fragment = self._fragments(slide, cursor.chunk)
        if show_slide or slide.index != self.slide_index:
            self.slide_index = slide.index
            await self._send({"type": "slide", "slide": slide.id, "fragment": fragment})
        self.play_id += 1
        self.fragment = fragment
        self.cursor = cursor
        self.started = True
        self.floor_human = False
        self.floor_offline = False
        self._cut_info = None
        await self._send({"type": "play", "play_id": self.play_id, "slide": slide.id, "chunk": cursor.chunk,
                          "offset_ms": cursor.offset_ms, "fragment": fragment, "resume_word": cursor.resume_word})
        self._set_state(NARRATING, None)

    async def _goto(self, index: int) -> None:
        """Show slide `index`: a relay slide narrates from chunk 0, a human slide leaves the presenter the floor
        (one before the first relay slide re-arms the opening)."""
        slide = self.talk.slides[index]
        if slide.is_relay and slide.chunks:
            await self._play(Cursor(slide.id, 0), show_slide=True)
            return
        self.slide_index = index
        self.cursor = None
        self._cut_info = None
        self.fragment = self._fragments(slide, 0)
        self._rearm_opening(index)
        await self._send({"type": "slide", "slide": slide.id, "fragment": self.fragment})
        self._set_state(IDLE, self._phase_banner())

    async def _show(self, index: int) -> None:
        """The offline floor's → / ←: show slide `index` without narrating; a relay slide waits at chunk 0 for R."""
        slide = self.talk.slides[index]
        self.slide_index = index
        self.cursor = Cursor(slide.id, 0) if slide.is_relay and slide.chunks else None
        self._cut_info = None
        self.fragment = self._fragments(slide, 0)
        self._rearm_opening(index)
        await self._send({"type": "slide", "slide": slide.id, "fragment": self.fragment})
        self._set_state(IDLE, self._offline_banner())

    def _offline_banner(self) -> str:
        """The offline floor's banner; once the link is back it says so (arrows stay silent until R or Space)."""
        banner = BANNER_OFFLINE_BACK if self.realtime.ready else BANNER_OFFLINE
        return banner.format(presenter=self.talk.presenter)

    def _rearm_opening(self, index: int) -> None:
        """Back on a human slide before the first relay slide (a sound check with R, a projector check with →, a
        stray clicker press): the talk hasn't been handed over yet, so the next tap is the opening again."""
        first = self.talk.next_relay_index(0)
        if first is None or index >= first or self.talk.slides[index].is_relay:
            return
        if self.started or self.floor_human:
            self._note("opening_rearmed", {"slide": self.talk.slides[index].id})
        self.started = False
        self.floor_human = False

    async def _start_presentation(self) -> None:
        first = self.talk.next_relay_index(0)
        if first is None:
            self._set_state(IDLE, self._phase_banner())
            return
        self.started = True
        await self._goto(first)

    async def _resume(self, source: str) -> None:
        """Continue narration: from the cursor if there is one. Without one, the R key picks the current relay
        slide, else the next relay slide, else the first; the model in `opening` starts the presentation;
        otherwise nothing happens (IDLE)."""
        if self.cursor is not None:
            await self._play(self.cursor)
        elif source == "key":
            current = self.current_slide
            index = current.index if current.is_relay else self.talk.next_relay_index(current.index + 1)
            if index is None:
                index = self.talk.next_relay_index(0)
            if index is None:
                self._set_state(IDLE, self._phase_banner())
            else:
                await self._goto(index)
        elif not self.started:
            await self._start_presentation()
        else:
            self._set_state(IDLE, self._phase_banner())

    def _words(self, slide: str, chunk: int) -> list[Word]:
        rendered = self.chunks.get((slide, chunk))
        return list(rendered.words) if rendered is not None else []

    def _sentence(self, slide: str, chunk: int, index: int) -> str:
        rendered = self.chunks.get((slide, chunk))
        if rendered is None:
            return ""
        if 0 <= index < len(rendered.sentences):
            return rendered.sentences[index]
        return " ".join(word.word for word in rendered.words if word.sentence == index)

    def _position(self, at: Any) -> tuple[str, int, int] | None:
        """(slide, chunk, t_ms) from a deck Position, or None if missing, malformed or from an older play."""
        if not isinstance(at, dict):
            return None
        slide_id, chunk, t_ms = at.get("slide"), _int(at.get("chunk")), _int(at.get("t_ms"))
        if not isinstance(slide_id, str) or chunk is None or t_ms is None:
            return None
        if at.get("play_id") is not None and _int(at.get("play_id")) != self.play_id:
            return None
        try:
            slide = self.talk.slide(slide_id)
        except KeyError:
            return None
        if not slide.is_relay or not 0 <= chunk < len(slide.chunks):
            return None
        return slide_id, chunk, max(0, t_ms)

    def _resumed_floor(self, slide: str, chunk: int, t_ms: int | None) -> Cursor | None:
        """The current play's cursor when it resumed at a sentence start and nothing of that sentence had been
        heard by `t_ms`: the cut stays on that sentence instead of jumping back another one."""
        current = self.cursor
        if current is None or current.resume_word is None or (current.slide, current.chunk) != (slide, chunk):
            return None
        words = self._words(slide, chunk)
        if not 0 <= current.resume_word < len(words):
            return None
        if t_ms is None or t_ms < words[current.resume_word].start:
            return current
        return None

    def _pause_cursor(self, position: tuple[str, int, int] | None) -> Cursor | None:
        """Exact position for a pause (no rewind); without a position, where the current play started."""
        if position is None:
            return self.cursor
        slide, chunk, t_ms = position
        return self._resumed_floor(slide, chunk, t_ms) or Cursor(slide, chunk, t_ms, None)

    async def _cut_narration(self, at: Any) -> None:
        position = self._position(at)
        if position is None:  # no usable position: narration continues from where the current play started
            self._cut_info = None
            return
        slide, chunk, t_ms = position
        await self._cut_at(slide, chunk, t_ms, self._resumed_floor(slide, chunk, t_ms))

    async def _cut_paused(self) -> None:
        cursor = self.cursor
        if cursor is None:
            return
        if cursor.resume_word is not None:  # already at a sentence start
            await self._cut_at(cursor.slide, cursor.chunk, None, cursor)
        else:
            await self._cut_at(cursor.slide, cursor.chunk, cursor.offset_ms, None)

    async def _cut_at(self, slide: str, chunk: int, t_ms: int | None, floor: Cursor | None) -> None:
        """mark_cut: cut word = last word started by `t_ms`, cursor = the start of its sentence; `cut` to the deck."""
        words = self._words(slide, chunk)
        if not words:
            self.cursor = Cursor(slide, chunk, 0, None)
            self._cut_info = None
            return
        if floor is not None and floor.resume_word is not None and 0 <= floor.resume_word < len(words):
            point = CutPoint(floor.resume_word, floor.resume_word, floor.offset_ms)
        else:
            point = cut_point(words, t_ms or 0)
        self.cursor = Cursor(slide, chunk, point.offset_ms, point.resume_word)
        word = words[point.cut_word]
        self._cut_info = (word.word, self._sentence(slide, chunk, word.sentence))
        await self._send({"type": "cut", "slide": slide, "chunk": chunk, "cut_word": point.cut_word,
                          "resume_word": point.resume_word})
        self._note("cut", {"slide": slide, "chunk": chunk, "t_ms": t_ms, "cut_word": point.cut_word,
                           "word": word.word, "resume_word": point.resume_word, "offset_ms": point.offset_ms})

    # --- tools ------------------------------------------------------------------------------------------------

    async def _tool_message(self, call_id: str, name: str, args: dict, status: str) -> None:
        await self._send({"type": "tool", "call_id": call_id, "name": name, "args": args, "status": status})

    def _register_call(self, turn: Turn, call_id: Any, name: Any, arguments: Any) -> bool:
        if not isinstance(call_id, str) or not call_id or call_id in turn.calls or call_id in self._answered:
            return False
        turn.calls[call_id] = (name, arguments)
        self._note("tool_call", {"call_id": call_id, "name": name, "arguments": arguments,
                                 "response_id": turn.response_id})
        return True

    def _remember_call_item(self, call_id: Any, item_id: Any) -> None:
        if isinstance(call_id, str) and call_id and isinstance(item_id, str) and item_id:
            self._call_items[call_id] = item_id

    async def _owe_output(self, response_id: str, call_id: Any, output: str) -> None:
        """One function_call_output for a call of a response that won't run normally (cut, stalled or rejected).
        It goes out at that response's response.done — at once if that already came — so it never lands while the
        response is still generating, inserted right after its own call item so it can't end up after a newer user
        item. Never followed by response.create."""
        if not isinstance(call_id, str) or not call_id or call_id in self._answered:
            return
        self._answered.add(call_id)
        event: dict[str, Any] = {"type": "conversation.item.create",
                                 "item": {"type": "function_call_output", "call_id": call_id, "output": output}}
        if call_id in self._call_items:
            event["previous_item_id"] = self._call_items[call_id]
        sent_now = response_id in self._done
        if sent_now:
            await self._rt(event)
        else:
            self._owed.setdefault(response_id, []).append(event)
        self._note("tool_output", {"call_id": call_id, "response_id": response_id, "output": output,
                                   "sent": "now" if sent_now else "at response.done"})

    def _plan(self, call: tools.ToolCall) -> tuple[bool, str]:
        """(runnable, function_call_output) for a parsed call, judged now; it runs later, on drained."""
        name = call.name
        if name == tools.START_PRESENTATION:
            if self.started:  # a second start would restart the talk: resume instead
                return self._plan_resume()
            first = self.talk.next_relay_index(0)
            if first is None:
                return False, tools.error_output("no relay slide")
            return True, tools.ok_output(slide=self.talk.slides[first].id)
        if name == tools.RESUME_SCRIPT:
            return self._plan_resume()
        if name == tools.GOTO_SLIDE:
            return True, tools.ok_output(slide=call.args["slide_id"])
        if name == tools.NEXT_SLIDE:
            following = self.slide_index + 1
            if following >= len(self.talk.slides):
                return False, tools.error_output(tools.NO_NEXT_SLIDE)
            return True, tools.ok_output(slide=self.talk.slides[following].id)
        if name == tools.HANDOFF_TO_HUMAN:
            return True, tools.ok_output()
        return False, tools.error_output(f"unknown tool '{name}'")

    def _plan_resume(self) -> tuple[bool, str]:
        if self.cursor is not None:
            return True, tools.ok_output(slide=self.cursor.slide)
        if not self.started:
            first = self.talk.next_relay_index(0)
            if first is not None:
                return True, tools.ok_output(slide=self.talk.slides[first].id)
        return True, tools.ok_output(note=tools.NOTHING_TO_RESUME)

    async def _settle_calls(self, turn: Turn, *, owe: bool = False) -> None:
        """Validate the response's calls, send one function_call_output per call (never response.create), and
        queue the last runnable one; earlier runnable ones are discarded. With `owe` (a stalled response, still
        open on the server) the outputs wait for its response.done."""
        plans: list[tuple[str, str, dict, tools.ToolCall | None, str]] = []
        for call_id, (name, arguments) in turn.calls.items():
            if call_id in self._answered:
                continue
            parsed = tools.parse_call(call_id, name, arguments, self.talk)
            if isinstance(parsed, str):
                plans.append((call_id, str(name), tools.parse_args(arguments), None, tools.error_output(parsed)))
                continue
            runnable, output = self._plan(parsed)
            plans.append((call_id, parsed.name, parsed.args, parsed if runnable else None, output))
        runnable_calls = [plan[3] for plan in plans if plan[3] is not None]
        winner = runnable_calls[-1] if runnable_calls else None
        if winner is not None and turn.queued is not None:
            await self._discard_queued(f"superseded by {winner.name}")
        for call_id, name, args, call, output in plans:
            if call is None:
                status = "rejected"
            elif call is winner:
                status = "queued"
            else:
                status = "discarded"
                output = tools.error_output(f"not run: {winner.name if winner else 'another call'} came after it")
            if owe and turn.response_id is not None:
                await self._owe_output(turn.response_id, call_id, output)
            else:
                self._answered.add(call_id)
                await self._rt({"type": "conversation.item.create",
                                "item": {"type": "function_call_output", "call_id": call_id, "output": output}})
            await self._tool_message(call_id, name, args, status)
            self._note("tool", {"call_id": call_id, "name": name, "args": args, "status": status, "output": output})
        if winner is not None:
            turn.queued = winner

    async def _apply(self, call: tools.ToolCall) -> None:
        name = call.name
        if name == tools.START_PRESENTATION:
            if self.started:
                await self._resume("tool")
            else:
                await self._start_presentation()
        elif name == tools.RESUME_SCRIPT:
            await self._resume("tool")
        elif name == tools.GOTO_SLIDE:
            await self._goto(self.talk.index_of(call.args["slide_id"]))
        elif name == tools.NEXT_SLIDE:
            following = self.slide_index + 1
            if following < len(self.talk.slides):
                await self._goto(following)
            else:
                self._set_state(IDLE, self._phase_banner())
        elif name == tools.HANDOFF_TO_HUMAN:
            self.floor_human = True
            self._set_state(IDLE, self._banner_human())

    # --- Realtime events --------------------------------------------------------------------------------------

    async def _realtime_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        turn = self._open_reply()
        if turn is not None and self._of_reply(event, turn):
            self._start_timer("reply_stall", REPLY_STALL_S, self._on_reply_stall)  # still streaming: not stalled
        handler = self._event_handlers.get(event.get("type"))  # anything else: ignore
        if handler is not None:
            await handler(event)

    def _open_reply(self) -> Turn | None:
        """The current turn while its bound response is still streaming (bound, no response.done yet)."""
        turn = self.turn
        if self.state == REPLYING and turn is not None and turn.response_id is not None and not turn.response_done:
            return turn
        return None

    @staticmethod
    def _of_reply(event: dict, turn: Turn) -> bool:
        """A streaming event of the turn's bound response (one without an id counts, as in _event_turn)."""
        response_id, kind = event.get("response_id"), event.get("type")
        if response_id is not None:
            return response_id == turn.response_id
        return isinstance(kind, str) and kind.startswith("response.") and kind not in ("response.created",
                                                                                     "response.done")

    def _event_turn(self, response_id: Any) -> Turn | None:
        """The current turn if `response_id` is its bound response (events without an id count as current)."""
        turn = self.turn
        if turn is None or turn.response_id is None:
            return None
        if response_id is None:
            return turn
        if response_id != turn.response_id or response_id in self._ignored:
            return None
        return turn

    def _remember_user_item(self, turn: Turn, item_id: Any) -> None:
        if not isinstance(item_id, str) or not item_id:
            return
        turn.user_items.add(item_id)
        self._user_items[item_id] = turn
        while len(self._user_items) > USER_ITEMS_KEPT:
            del self._user_items[next(iter(self._user_items))]

    async def _ev_speech_started(self, event: dict) -> None:
        turn = self.turn
        if self.state == LISTENING and turn is not None:
            turn.speech_started = True
            self._remember_user_item(turn, event.get("item_id"))
            self._cancel_timer("listen_silence")

    async def _ev_speech_stopped(self, event: dict) -> None:
        turn = self.turn
        if self.state != LISTENING or turn is None:
            if self.state == REPLYING and turn is not None and turn.ended_by in ("hand", "listen_max"):
                # turn detection ended a turn we had already sent (live check: does it also answer it?)
                self._note("vad_after_send", {"reply": turn.reply, "audio_end_ms": event.get("audio_end_ms")})
            return
        self._remember_user_item(turn, event.get("item_id"))
        await self._close_mic(clear=False)  # trailing audio could re-trigger VAD and cancel the reply (D5)
        self._enter_replying(turn, "vad")
        self._estimate_speech_end(turn, event.get("audio_end_ms"))

    async def _ev_committed(self, event: dict) -> None:
        """The user's audio became an item (our commit, or turn detection's): remember its id for the transcript."""
        turn = self.turn
        if turn is not None and self.state in (LISTENING, REPLYING) and turn.mic_frames > 0:
            self._remember_user_item(turn, event.get("item_id"))

    async def _ev_transcription(self, event: dict) -> None:
        """The user's words, matched to their turn by item_id. The current turn gets them as its caption; a
        finished turn's late transcript only fills in its history entry (never a caption over narration or over a
        newer turn, never the newer turn's text)."""
        text = " ".join(str(event.get("transcript") or "").split())
        if not text:
            return
        item_id = event.get("item_id")
        item_id = item_id if isinstance(item_id, str) and item_id else None
        current = self.turn
        owner = self._user_items.get(item_id) if item_id else None
        if (owner is None and current is not None and self.state == REPLYING and current.ended_by != "typed"
                and (item_id is None or not current.user_items)):
            owner = current  # a spoken turn that just ended, and nothing says otherwise: its own transcript
            self._remember_user_item(current, item_id)
        if owner is not None and owner is current:
            owner.user_text = text
            await self._send({"type": "caption", "role": "user", "text": text, "final": True, "reply": owner.reply})
            return
        if owner is not None:
            owner.user_text = text
            if owner.history_index is not None and owner.history_index < len(self._history):
                self._history[owner.history_index] = (text, self._history[owner.history_index][1])
        elif self._history and not self._history[-1][0]:
            self._history[-1] = (text, self._history[-1][1])  # unknown item: the last finished exchange is our guess
        self._note("transcript_late", {"item_id": item_id, "matched": owner is not None, "text": text})

    async def _ev_response_created(self, event: dict) -> None:
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        response_id = response.get("id")
        if isinstance(response_id, str) and response_id:
            self._active.add(response_id)
        else:
            response_id = None
        turn = self.turn
        if (self.state == REPLYING and turn is not None and turn.expects_response and turn.response_id is None
                and not turn.pending and response_id is not None and response_id not in self._ignored):
            # (a response created while our own question is still held back can't be the answer to it)
            turn.response_id = response_id
            self._cancel_timer("reply_wait")
            self._start_timer("reply_stall", REPLY_STALL_S, self._on_reply_stall)
            await self._send({"type": "reply_start", "reply": turn.reply})
            self._note("reply_start", {"reply": turn.reply, "response_id": response_id})
            return
        if response_id is not None:
            self._ignored.add(response_id)
        self._note("response_rejected", {"response_id": response_id, "state": self.state})
        await self._rt({"type": "response.cancel"})

    async def _ev_audio_delta(self, event: dict) -> None:
        turn = self._event_turn(event.get("response_id"))
        delta = event.get("delta")
        if turn is None or self.state != REPLYING or turn.response_done or not isinstance(delta, str):
            return
        if turn.first_audio_at is None:
            turn.first_audio_at = self.clock.now()
            await self._report_latency(turn)
        if turn.audio_item_id is None and isinstance(event.get("item_id"), str):
            turn.audio_item_id = event["item_id"]
        turn.audio_bytes += _b64_bytes(delta)
        await self._send({"type": "reply_audio", "reply": turn.reply, "pcm": delta})

    async def _ev_transcript_delta(self, event: dict) -> None:
        turn = self._event_turn(event.get("response_id"))
        delta = event.get("delta")
        if turn is None or self.state != REPLYING or not isinstance(delta, str) or not delta:
            return
        turn.assistant_text += delta
        await self._send({"type": "caption", "role": "assistant", "text": turn.assistant_text, "final": False,
                          "reply": turn.reply})

    async def _ev_transcript_done(self, event: dict) -> None:
        turn = self._event_turn(event.get("response_id"))
        if turn is None or self.state != REPLYING:
            return
        transcript = event.get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            turn.assistant_text = transcript.strip()
        await self._send({"type": "caption", "role": "assistant", "text": turn.assistant_text, "final": True,
                          "reply": turn.reply})

    async def _ev_call_done(self, event: dict) -> None:
        response_id, call_id = event.get("response_id"), event.get("call_id")
        if _server_call(event):  # the server runs it and answers it itself: never ours to run or to answer
            self._note("server_tool_call", {"call_id": call_id, "name": event.get("name"),
                                            "arguments": event.get("arguments"), "response_id": response_id})
            return
        self._remember_call_item(call_id, event.get("item_id"))
        if isinstance(response_id, str) and response_id in self._ignored:  # cut, stalled or rejected: never run
            await self._owe_output(response_id, call_id, CUT_CALL_OUTPUT)
            return
        turn = self._event_turn(response_id)
        if turn is None:
            return
        if self._register_call(turn, call_id, event.get("name"), event.get("arguments")) \
                and turn.response_done:  # a straggler after response.done: answer it now
            await self._settle_calls(turn)

    async def _ev_response_done(self, event: dict) -> None:
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        response_id = response.get("id")
        output = response.get("output") if isinstance(response.get("output"), list) else []
        calls = [item for item in output if isinstance(item, dict) and item.get("type") == "function_call"
                 and not _server_call(item)]  # server-executed calls (web_search) are the server's to answer
        for item in calls:
            self._remember_call_item(item.get("call_id"), item.get("id"))
        self._active.discard(response_id)
        if isinstance(response_id, str) and response_id:
            self._done.add(response_id)
            if response_id in self._ignored:  # cut, stalled or rejected: its calls get their outputs now, none runs
                for owed in self._owed.pop(response_id, []):
                    await self._rt(owed)
                for item in calls:
                    await self._owe_output(response_id, item.get("call_id"), CUT_CALL_OUTPUT)
        await self._send_pending()  # after those outputs: the user's held-back item comes last, then the create
        turn = self._event_turn(response_id)
        if turn is None or turn.response_done or self.state != REPLYING:
            return
        if (response.get("status") == "cancelled" and turn.first_audio_at is None and turn.queued is None
                and turn.ended_by in ("hand", "listen_max")):
            await self._displaced(turn, calls)
            return
        for item in calls:
            self._register_call(turn, item.get("call_id"), item.get("name"), item.get("arguments"))
        self._note("response_done", {"response_id": response_id, "status": response.get("status"),
                                     "calls": list(turn.calls)})
        await self._finish_response(turn)

    async def _displaced(self, turn: Turn, calls: list[dict]) -> None:
        """A turn we ended by hand (commit + response.create with turn detection on) got a response that the server
        cancelled before it said anything: turn detection's own response and ours displaced each other
        (`response.create` interrupts one in flight). The other one answers the same question: wait for it."""
        response_id = turn.response_id
        self._note("response_displaced", {"reply": turn.reply, "response_id": response_id})
        if response_id is not None:
            self._ignored.add(response_id)
            for call_id in [*turn.calls, *(item.get("call_id") for item in calls)]:
                await self._owe_output(response_id, call_id, CUT_CALL_OUTPUT)  # it is done: they go out now
        turn.response_id = None
        turn.calls.clear()
        turn.audio_item_id = None
        turn.assistant_text = ""
        self._cancel_timer("reply_stall")
        self._start_timer("reply_wait", REPLY_WAIT_S, self._on_reply_wait)

    async def _finish_response(self, turn: Turn, *, owe: bool = False) -> None:
        """No more reply audio: answer the calls, queue the action, reply_end, and arm the drain fallback."""
        turn.response_done = True
        self._cancel_timer("reply_wait")
        self._cancel_timer("reply_stall")
        await self._settle_calls(turn, owe=owe)
        if turn.reply is not None:
            await self._send({"type": "reply_end", "reply": turn.reply})
        self._start_timer("drain", self._remaining_audio_s(turn) + DRAIN_GRACE_S, self._on_drain_timeout)

    def _remaining_audio_s(self, turn: Turn) -> float:
        if turn.first_audio_at is None:
            return 0.0
        audio_ms = turn.audio_bytes / BYTES_PER_MS
        elapsed_ms = (self.clock.now() - turn.first_audio_at) * 1000
        return max(0.0, audio_ms - elapsed_ms) / 1000

    def _estimate_speech_end(self, turn: Turn, audio_end_ms: Any) -> None:
        """When the user actually stopped speaking: turn detection reports the end of a turn only after its silence
        window, so the speech_stopped event itself comes late. Its `audio_end_ms` is a position in the input stream;
        read it as session-wide or as since this turn's clear, whichever leaves the smaller (plausible) tail of
        audio after it, and map that position to wall-clock time through the arrival times of the mic frames.
        Without a usable position the turn's end event stays the start point."""
        end = _int(audio_end_ms)
        if end is None or not turn.marks:
            return
        turn_ms = self._stream_ms - turn.stream_start_ms  # this turn's audio so far
        tails = [tail for tail in (self._stream_ms - end, turn_ms - end)
                 if 0 <= tail <= min(turn_ms, MAX_VAD_TAIL_MS)]
        if not tails:
            self._note("speech_end_unknown", {"audio_end_ms": end, "turn_audio_ms": round(turn_ms),
                                              "stream_ms": round(self._stream_ms)})
            return
        tail = min(tails)
        position = self._stream_ms - tail
        for mark_position, arrived in turn.marks:  # the first frame that reached `position`
            if mark_position >= position:
                estimate = arrived - (mark_position - position) / 1000
                break
        else:  # unreachable: the last mark is the end of the stream
            return
        if turn.turn_end_at is not None:
            estimate = min(estimate, turn.turn_end_at)
        turn.speech_end_at = estimate
        turn.speech_end_estimated = True
        self._note("speech_end", {"reply": turn.reply, "audio_end_ms": end, "tail_ms": round(tail),
                                  "before_turn_end_ms": round(((turn.turn_end_at or estimate) - estimate) * 1000)})

    async def _report_latency(self, turn: Turn) -> None:
        """First reply audio: `reply_latency_ms` = end of the user's speech (estimated for a turn that turn
        detection ended, else the turn's end: send, release or Space) → first audio delta at the backend; the log
        also keeps `turn_end_latency_ms` = the turn's end event → first audio delta."""
        if turn.first_audio_at is None or turn.speech_end_at is None:
            return
        latency = round((turn.first_audio_at - turn.speech_end_at) * 1000)
        await self._send({"type": "metric", "name": "reply_latency_ms", "value": latency})
        start = "end of speech (from audio_end_ms)" if turn.speech_end_estimated else f"end of turn ({turn.ended_by})"
        self._note("metric", {"name": "reply_latency_ms", "value": latency, "reply": turn.reply,
                              "from": start, "to": "first reply audio"})
        if turn.turn_end_at is not None:
            self._note("metric", {"name": "turn_end_latency_ms",
                                  "value": round((turn.first_audio_at - turn.turn_end_at) * 1000),
                                  "reply": turn.reply, "from": f"end of turn ({turn.ended_by})",
                                  "to": "first reply audio"})

    async def _ev_error(self, event: dict) -> None:
        err_type, code, message = _error_fields(event)
        self._note("realtime_error", {"type": err_type, "code": code, "message": message})
        if _benign_error(err_type, code, message) or self._quiet_error(err_type, code):
            return
        await self._send_error(f"Realtime: {message or code or err_type or 'error'}")
        if "insufficient_quota" in (err_type, code):
            self._connection = ("error", "billing refused: claim the credits")
            await self._send({"type": "connection", "status": "error", "detail": self._connection[1]})
        if self._open_reply() is not None:  # mid-reply: if the reply now goes quiet, end it soon (no done may come)
            self._start_timer("reply_stall", ERROR_GRACE_S, self._on_reply_stall)

    def _quiet_error(self, err_type: str, code: str) -> bool:
        """Errors about our own housekeeping that the presenter can't act on (logged, not shown): an output inserted
        after a call item the server no longer has, or a hand-sent turn that turn detection had just ended itself
        (the server's own commit won the race; its reply still comes)."""
        kinds = {err_type, code}
        if "invalid_previous_item_id" in kinds:
            return True
        if not self.realtime.ready and "insufficient_quota" not in kinds:
            return True  # session setup failed (e.g. voice validation 429): the strip shows "reconnecting: …"
        turn = self.turn
        return (bool(kinds & SEND_RACE_ERRORS) and self.state == REPLYING and turn is not None
                and turn.ended_by in ("hand", "listen_max"))

    async def _ev_session(self, event: dict) -> None:
        session = event.get("session") if isinstance(event.get("session"), dict) else {}
        audio = session.get("audio") if isinstance(session.get("audio"), dict) else {}
        audio_in = audio.get("input") if isinstance(audio.get("input"), dict) else {}
        audio_out = audio.get("output") if isinstance(audio.get("output"), dict) else {}
        self._note("session", {"event": event.get("type"), "voice": audio_out.get("voice"),
                               "turn_detection": audio_in.get("turn_detection"),
                               "transcription": audio_in.get("transcription"),
                               "tools": len(session.get("tools") or []), "temperature": session.get("temperature")})

    # --- Realtime link status ---------------------------------------------------------------------------------

    async def _realtime_status(self, status: str, detail: str | None) -> None:
        self._connection = (status, detail)
        await self._send({"type": "connection", "status": status, "detail": detail})
        if status == "ready":
            self._new_session()
            self._ready_count += 1
            if self._ready_count > 1:
                await self._send_summary()
            if self.state == IDLE and self.floor_offline:
                self._set_state(IDLE, self._offline_banner())  # "Realtime is back …"
        elif status in DROPPED:
            await self._link_lost()

    def _new_session(self) -> None:
        """A new Realtime session: an empty conversation that numbers its ids afresh. Forget the old session's
        response and call ids (a reused id must be neither ignored nor deduped), its owed outputs, user items and
        input-stream position. The link reads one socket at a time, so no old event can follow `ready`. A turn
        still draining from the old session keeps its state, but its audio item is gone: nothing to truncate."""
        self._active.clear()
        self._ignored.clear()
        self._answered.clear()
        self._done.clear()
        self._owed.clear()
        self._call_items.clear()
        self._user_items.clear()
        self._stream_ms = 0.0
        if self.turn is not None:
            self.turn.audio_item_id = None

    async def _link_lost(self) -> None:
        """Mid-turn drop: LISTENING goes back to its origin; a reply without response.done counts as done."""
        self._active.clear()
        turn = self.turn
        if self.state == LISTENING:
            await self._close_mic(clear=False)
            await self._send_error(ERR_LINK_LOST)
            await self._back_to_origin()
        elif self.state == REPLYING and turn is not None and not turn.response_done:
            if turn.response_id is None:  # nothing was playing: drained already
                turn.response_done = True
                await self._drained("link lost")
            else:
                await self._finish_response(turn)

    async def _send_summary(self) -> None:
        exchanges = list(self._history)
        turn = self.turn
        if turn is not None and not turn.remembered and (turn.user_text.strip() or turn.assistant_text.strip()):
            exchanges.append((turn.user_text, turn.assistant_text))
        text = summary_text(exchanges)
        if text is None:
            return
        await self._rt(_user_message(text))
        self._note("summary", {"chars": len(text), "exchanges": len(exchanges)})

    # --- timer callbacks --------------------------------------------------------------------------------------

    async def _on_listen_silence(self) -> None:
        if self.state != LISTENING:
            return
        await self._send_error(ERR_SILENCE)
        await self._close_mic(clear=True)
        await self._back_to_origin()

    async def _on_listen_max(self) -> None:
        turn = self.turn
        if self.state != LISTENING or turn is None:
            return
        if self.settings.mic_mode != "hold" and turn.speech_started and turn.mic_frames > 0:
            await self._send_turn(turn, "listen_max")  # turn detection never ended it (room noise): answer it
            return
        await self._send_error(ERR_TOO_LONG_HOLD if self.settings.mic_mode == "hold" else ERR_TOO_LONG)
        await self._close_mic(clear=True)
        await self._back_to_origin()

    async def _on_reply_wait(self) -> None:
        turn = self.turn
        if self.state != REPLYING or turn is None or turn.response_id is not None:
            return
        if turn.pending:  # the response we waited on never reported done: don't let it block later turns
            self._note("stale_responses", {"ids": sorted(self._active)})
            self._active.clear()
            turn.pending = []
        await self._send_error(ERR_NO_REPLY)
        await self._back_to_origin()

    async def _on_reply_stall(self) -> None:
        """The bound reply went quiet: no event of its own for REPLY_STALL_S (or ERROR_GRACE_S after an error) and
        no response.done. End it the way a link drop does: cancel it, ignore its later events, treat it as done
        (reply_end, drain fallback, then the queued tool, the countdown or the origin). Its tool outputs wait for
        its response.done; "No reply" if nothing was heard."""
        turn = self._open_reply()
        if turn is None:
            return
        response_id = turn.response_id
        self._note("reply_stalled", {"reply": turn.reply, "response_id": response_id,
                                     "audio_ms": turn.audio_bytes // BYTES_PER_MS})
        await self._rt({"type": "response.cancel"})
        self._ignored.add(response_id)
        self._active.discard(response_id)  # a dead response must not hold back a later response.create
        if turn.first_audio_at is None:
            await self._send_error(ERR_NO_REPLY)
        await self._finish_response(turn, owe=True)

    async def _on_drain_timeout(self) -> None:
        if self.state == REPLYING and self.turn is not None and self.turn.response_done:
            await self._drained("fallback")

    async def _on_countdown(self) -> None:
        if self.state == IDLE:
            await self._resume("watchdog")

    # --- instructions -----------------------------------------------------------------------------------------

    def _context(self) -> TurnContext:
        word, sentence = self._cut_info if self._cut_info else (None, None)
        return TurnContext(phase=self.phase, slide_id=self.current_slide.id, cut_word=word, cut_sentence=sentence)

    def _instructions(self) -> str:
        return build_instructions(self.talk, self._context())

    def _turn_update(self) -> dict:
        """Per-turn session.update: instructions only, or the full config with RELAY_FULL_SESSION_UPDATE=1."""
        if self.settings.full_session_update:
            return {"type": "session.update", "session": self.session_config()}
        return {"type": "session.update", "session": {"type": "realtime", "instructions": self._instructions()}}
