"""The presenter state machine (design §5): narration, live turns, deck tools, resume.

The server feeds a Controller every deck message (`on_browser`), every Higgs Realtime server event (`on_realtime`)
and every link status change (`on_realtime_status`); the Controller answers only through the BrowserPort and the
RealtimePort. All public handlers and timer callbacks run under one asyncio.Lock, never raise (a bug is logged and
shown as an `error` message), and ignore message/event types they don't know.

States (brief Appendix C): IDLE · NARRATING · PAUSED · LISTENING (mic open) · REPLYING (live reply, mic closed).
A live turn remembers its `origin` (narration / paused / idle) so it can go back there when it ends without a tool.

Narration (D52, D53): with `narration = "live"` every relay slide is presented by the one warm Realtime session, in
Relay's own words, from that slide's notes: one `response.create` per slide (a `LiveNarration`), streamed to the deck
as a reply. A cut truncates the model's memory to what the room heard and the slide continues live from there. When
the link is not ready as a slide starts, or a request never speaks, the slide plays from its TTS 3 render (the
backup: v1's chunk-by-chunk narration, which is all of `narration = "rendered"`).

Turns (contract v3 §5.1): with `turn_detection` null (the default) the backend ends a spoken turn itself, with a
per-turn `Segmenter` on the mic frames (its end = commit + response.create; Space again still sends at once). A JSON
`turn_detection` (RELAY_TURN_DETECTION) keeps the server-VAD path.

A meeting package (agenda.yaml: On the Same Page, doc/brief_updated.md) turns Relay into the meeting facilitator:
agenda phases presentation → discussion → wrap-up; one talking point (script chunk) per live narration response; the
facilitator's tools (lookup_evidence, record_clarification, save_decision, … ) with real round trips (a follow-up
`response.create` answers from a data tool's output, MAX_FOLLOWUPS per turn); then the discussion: state ROOM (the
room mic open, every utterance committed and transcribed, the facilitator silent), the alignment engine on every
utterance, and the facilitator speaking up only at its gate (state INTERVENING: one neutral question with the evidence
on screen), saving the corrected decision (validated against what was said), and summing up at the wrap-up. A talk
without agenda.yaml behaves exactly as before.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import copy
import dataclasses
import math
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from . import alignment, evidence, prompts, tools
from .config import RATE, STT_MODEL, Settings
from .meeting import MeetingState, load_meeting
from .ports import AsyncioClock, BrowserPort, Clock, LogFn, RealtimePort, Timer, null_log
from .prompts import TurnContext, build_instructions, narration_instructions
from .segmenter import Segmenter
from .talk import Slide, Talk
from .textutil import word_count
from .timeline import CutPoint, RenderedChunk, Word, cut_point, render_version

IDLE = "IDLE"
NARRATING = "NARRATING"
PAUSED = "PAUSED"
LISTENING = "LISTENING"
REPLYING = "REPLYING"
ROOM = "ROOM"  # a meeting's discussion: the room mic is open, every utterance is transcribed, the facilitator is silent
INTERVENING = "INTERVENING"  # the facilitator speaks up in the discussion (an alignment question, a decision)
STATES = (IDLE, NARRATING, PAUSED, LISTENING, REPLYING, ROOM, INTERVENING)
SPEAKING = (REPLYING, INTERVENING)  # a turn's response is bound and streams to the deck as a reply

ORIGIN_NARRATION = "narration"
ORIGIN_PAUSED = "paused"
ORIGIN_IDLE = "idle"
ORIGIN_ROOM = "room"  # back to listening to the room (the discussion)
ORIGIN_HANDOVER = "handover"  # the presentation is over: the discussion starts
ORIGIN_WRAPUP = "wrapup"  # the meeting is wrapped up

# What a turn is (the deck's reply_start `kind`, the per-response metadata `relay` of the facilitator's requests)
KIND_ANSWER = "answer"
KIND_INTERVENTION = "intervention"
KIND_DECISION = "decision"
KIND_HANDOVER = "handover"
KIND_SUMMARY = "summary"
KIND_FOLLOWUP = "followup"  # metadata only: a follow-up response of a turn (after a tool round trip)
TAGGED_KINDS = frozenset({KIND_FOLLOWUP, KIND_INTERVENTION, KIND_DECISION, KIND_HANDOVER, KIND_SUMMARY, "narration"})
NO_TOOL_KINDS = frozenset({KIND_INTERVENTION, KIND_HANDOVER, KIND_SUMMARY})

PRESENTATION, DISCUSSION, WRAPUP = "presentation", "discussion", "wrapup"  # agenda phases
MAX_FOLLOWUPS = 2  # follow-up responses per turn (after data tools, or to confirm a silent state call)
ROOM_ECHO_S = 0.3  # half duplex: the room mic is ignored this long after the facilitator's reply drained
POINT_MIN_S = 3  # the length a live talking point is asked for: its backup length, within these bounds
POINT_MAX_S = 30
POINT_CONTINUE_MIN_S = 3
DUPLICATE_DECISION_S = 60.0  # the same decision saved again this soon: "already saved"
AUTO_SOURCE_IDS = 2  # an automatic clarification cites the top results of the turn's lookups
DEFAULT_WINDOW_UTTERANCES = 3
APPEND_MAX_BYTES = 480_000  # one input_audio_buffer.append (Higgs: ≤ 1 MiB of base64)
SEGMENTER_FRAME_MS = 10  # relay.segmenter analyses 10 ms frames (its end_frames of silence end an utterance)

LISTEN_SILENCE_S = 8.0  # tap mode: no speech_started this long after the floor opens → as Esc
LISTEN_MAX_S = 30.0  # a turn that never ends: tap mode sends what was heard (speech started), else as Esc
REPLY_WAIT_S = 8.0  # end of the user's turn → response.created, else back to origin
REPLY_STALL_S = 8.0  # a bound reply with no event of its own this long (and no response.done) → treat it as done
ERROR_GRACE_S = 1.5  # after a non-benign `error` mid-reply: this long without an event of the reply → as a stall
DOUBLE_SEND_S = 1.0  # tap mode: Space this soon after a spoken turn ended (still thinking) was a "send": ignored
SEND_NOW_MIN_FRAMES = 5  # tap mode: "send now" needs this many 100 ms mic frames (a quick double tap sends nothing)
COUNTDOWN_S = 1.5  # "Resuming…" / "Starting…" after a reply that called no tool (the deck shows whole seconds: ceil)
DRAIN_GRACE_S = 2.0  # drain fallback = remaining reply audio + this
NARRATION_WAIT_S = 6.0  # a live narration request → its first audio, else it failed (live: 0.26–0.55 s)
NARRATION_RETRIES = 1  # a failed live narration is asked for once more, then the backup presents the slide
NARRATION_MIN_S = 6  # the length a live narration is asked for: the slide's backup length, within these bounds
NARRATION_MAX_S = 60
CONTINUE_MIN_S = 5  # …and at least this for the rest of a slide that was cut
WORDS_PER_SECOND = 2.6  # a slide without a backup render: its notes at a speaking pace
HEARD_MAX_CHARS = 160  # what the room last heard of a cut slide, quoted in the request that continues it
NARRATION_TIMERS = ("narration_wait", "narration_stall", "narration_drain")
BYTES_PER_MS = RATE * 2 // 1000  # PCM16 mono at 24 kHz: 48 bytes per ms
MAX_VAD_TAIL_MS = 5000  # speech_stopped.audio_end_ms this far behind the newest mic audio is not trusted
USER_ITEMS_KEPT = 16  # user audio item ids remembered to match late transcripts to their turn
SUMMARY_MAX_CHARS = 600
SUMMARY_PREFIX = "Context only, do not reply. Earlier live exchanges in this talk: "
TURN_TIMERS = ("listen_silence", "listen_max", "reply_wait", "reply_stall", "drain")
DROPPED = ("reconnecting", "error", "offline")
CUT_CALL_OUTPUT = tools.error_output("not run: the reply was cut off")  # calls of a cut/stalled/rejected reply
NARRATION_CALL_OUTPUT = tools.error_output("not now: you are presenting; the deck moves on by itself")
NOT_NOW_OUTPUT = tools.error_output("not now: just speak")  # a tool call in an intervention, hand-over or summary
LIMIT_OUTPUT = tools.error_output("answer now with what you have")  # data calls after MAX_FOLLOWUPS follow-ups
NOTHING_TO_CANCEL = frozenset({"response_not_active", "response_cancel_not_active"})  # Higgs docs; OpenAI
SEND_RACE_ERRORS = frozenset({"voice_output_task_ongoing", "input_audio_buffer_commit_empty"})

BANNER_HUMAN = "{presenter} has the floor"
BANNER_OPENING = "{presenter} has the floor — tap Space to hand over"
BANNER_QA = "Waiting for a question — tap Space"
BANNER_RESUMING = "Resuming…"
BANNER_STARTING = "Starting…"
BANNER_PAUSED = "Paused — P to resume"
BANNER_PAUSED_R = "Paused — R to resume"
BANNER_OFFLINE = "Realtime offline — {presenter} has the floor (R resumes)"
BANNER_OFFLINE_BACK = "Realtime is back — {presenter} has the floor (R resumes)"
BANNER_END = "End of the talk"
BANNER_DISCUSSION = "Discussion: {prompt} — the facilitator is listening"
BANNER_WRAPPED = "Meeting wrapped up"

ERR_SILENCE = "Didn't catch that"
ERR_TOO_LONG = "Turn too long — try MIC_MODE=hold"
ERR_TOO_LONG_HOLD = "Turn too long"
ERR_NO_REPLY = "No reply"
ERR_LINK_LOST = "Realtime connection lost"
ERR_OFFLINE_TYPED = "Realtime offline — the typed question was not sent"
ERR_OFFLINE_ROOM = "Realtime offline — the facilitator can't answer; typed lines still reach the alignment check"
ERROR_MAX_CHARS = 200


@dataclass(frozen=True)
class Cursor:
    """Where narration continues: chunk `chunk` of slide `slide`, from `offset_ms`. `resume_word` set ⇒ this is a
    resume after a cut, and the captions show the jump back to that word. `live` ⇒ the room heard part of the slide's
    live narration (`heard_ms` of it, ending in `heard`) and it continues live from there (chunk/offset unused).
    `Cursor(slide, 0)` is the slide's start."""

    slide: str
    chunk: int
    offset_ms: int = 0
    resume_word: int | None = None
    live: bool = False
    heard: str = ""
    heard_ms: int = 0

    @property
    def slide_start(self) -> bool:
        return not self.live and self.chunk == 0 and self.offset_ms == 0 and self.resume_word is None

    @property
    def point_start(self) -> bool:
        """The start of chunk `chunk` (a meeting presents one talking point per live narration)."""
        return not self.live and self.offset_ms == 0 and self.resume_word is None


@dataclass
class LiveNarration:
    """One live narration request: the Realtime session presents slide `slide` in its own words (D52) — in a
    meeting, only its talking point `chunk`. It reaches the deck as reply `reply` (the same numbering as live
    answers)."""

    slide: str
    reply: int
    continuing: bool  # the slide was cut: carry on from what the room heard (the cursor says what)
    chunk: int = 0  # the talking point (a meeting); a plain talk presents the whole slide (0)
    attempt: int = 0  # retries after requests that never spoke (NARRATION_RETRIES)
    response_id: str | None = None  # the bound Realtime response
    audio_item_id: str | None = None  # for conversation.item.truncate
    audio_bytes: int = 0
    first_audio_at: float | None = None
    response_done: bool = False
    incomplete: bool = False  # the link dropped or it stalled before response.done: the backup replays the slide
    pending: list[dict] = field(default_factory=list)  # its response.create, held while another response is active
    calls: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # call_id → (name, arguments): never run
    text: str = ""  # transcript so far


@dataclass
class Turn:
    """One live turn: the floor opened once (Space or a typed question) and at most one reply — in a meeting, a reply
    and its follow-ups (each response its own deck reply), or one of the facilitator's own responses (`kind`)."""

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
    # manual turns and meetings
    segmenter: Any = None  # tap mode without server VAD: ends the spoken turn (relay/segmenter.py)
    kind: str = KIND_ANSWER  # answer · intervention · decision · handover · summary
    phase: str | None = None  # the agenda phase it started in (a meeting)
    expect_tag: dict | None = None  # the metadata its current request carries (Higgs echoes it on response.created)
    followups: int = 0
    next_followup: str | None = None  # a follow-up waiting for this response's audio to finish playing
    heard: bool = False  # some response of the turn had audio
    latency_reported: bool = False
    lookups: list = field(default_factory=list)  # EvidenceLookups the model asked for in this turn
    clarified: bool = False  # the model called record_clarification
    answers: list[str] = field(default_factory=list)  # transcripts of its earlier responses
    followup_answers: list[str] = field(default_factory=list)  # … of its earlier follow-ups (they saw tool results)
    events: list[dict] = field(default_factory=list)  # the alignment events an intervention answers
    draft: dict | None = None  # a decision turn: the alignment check's decision draft
    decision_saved: bool = False


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


def heard_text(transcript: str, played_ms: int, audio_ms: int) -> str:
    """The words of a reply's `transcript` the room heard after `played_ms` of its `audio_ms` of audio, read at an
    even pace: whole words only, those fully played; "" when nothing was played."""
    words = transcript.split()
    if played_ms <= 0 or audio_ms <= 0:
        return ""
    return " ".join(words[: len(words) * min(played_ms, audio_ms) // audio_ms])


def _tail(text: str, limit: int) -> str:
    """The end of `text`: at most `limit` characters, from the start of a word."""
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    if not text[-limit - 1].isspace() and " " in tail:
        tail = tail.split(" ", 1)[1]  # drop the clipped first word
    return tail.strip()


def _narration_tag(response: dict) -> dict | None:
    """The metadata a narration request put on its response (Higgs echoes it on response.created), else None."""
    metadata = response.get("metadata")
    return metadata if isinstance(metadata, dict) and metadata.get("relay") == "narration" else None


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


def _relay_tag(response: dict) -> dict | None:
    """The metadata one of our tagged requests put on its response (narration, follow-up, intervention, …), else
    None."""
    metadata = response.get("metadata")
    return metadata if isinstance(metadata, dict) and metadata.get("relay") in TAGGED_KINDS else None


def _jsonable(value: Any) -> Any:
    """A dataclass of relay.meeting as a JSON-ready dict (anything else as it is)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


def _pcm(data: str) -> bytes | None:
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return None


def meeting_info(meeting: Any) -> dict | None:
    """The meeting as the deck shows it (`hello.meeting`, `/api/talk`'s `meeting`): title, host, participants, the
    agenda phases, the documents with every statement and its id, the discussion slide. None for a plain talk."""
    return meeting.to_json() if meeting is not None else None


_LOAD = object()  # Controller(meeting=…): load the talk's meeting package (agenda.yaml) unless one is given


class Controller:
    def __init__(self, talk: Talk, chunks: Mapping[tuple[str, int], RenderedChunk], settings: Settings, *,
                 browser: BrowserPort, realtime: RealtimePort, clock: Clock | None = None,
                 log: LogFn | None = None, meeting: Any = _LOAD) -> None:
        self.talk = talk
        self.chunks = chunks
        self.settings = settings
        self.browser = browser
        self.realtime = realtime
        self.clock: Clock = clock or AsyncioClock()
        self._log = log or null_log
        self._lock = asyncio.Lock()

        # A meeting (agenda.yaml): the facilitator. None: a plain talk, Relay exactly as before.
        self.meeting = load_meeting(talk) if meeting is _LOAD else meeting
        self._t0 = self.clock.now()  # the meeting's clock starts here (and again at a reset)
        self.mstate: MeetingState | None = None
        self.engine: alignment.Engine | None = None
        if self.meeting is not None:
            self.mstate = MeetingState(self.meeting, self._meeting_time)
            self.engine = alignment.Engine(self.meeting, self.mstate)
        self.speaker: str | None = None  # the participant the deck says is talking (None: the room)
        self._room_seg: Segmenter | None = None  # ROOM: ends each utterance
        self._room_quiet_until = 0.0  # half duplex: room frames before this are ignored
        self._room_commits: collections.deque[str | None] = collections.deque()  # speakers of room commits in flight
        self._room_items: dict[str, str | None] = {}  # room audio item id → speaker, until its transcript
        self._room_commit_held = False  # a room commit waits for the active response to end
        self._flows: list[tuple[str, Any]] = []  # interventions and decisions waiting for the facilitator
        self._last_point: tuple[str, int] | None = None  # the talking point presented last (slide, chunk)
        self._link_action: str | None = None  # "restart": a fresh Realtime session after this handler (reset)

        self.state = IDLE
        self.slide_index = 0  # deck position of the slide on screen
        self.cursor: Cursor | None = None  # NARRATING: the current play; otherwise where narration continues
        self.play_id = 0
        self.started = False  # narration has started once (re-armed on a human slide before the first relay slide)
        self.floor_human = False
        self.floor_offline = False  # the presenter took the floor because Realtime was down: arrows move silently
        self._autos = 0  # ids of the watchdog's own actions in the ticker ("auto")
        self.turn: Turn | None = None
        self._live_mode = settings.narration == "live"
        self.live: LiveNarration | None = None  # NARRATING live: the slide's narration request
        self.narration_source: str | None = None  # the current narration: live · backup (live mode) · rendered
        self._presented = False  # a live narration was heard since the presentation started (no greeting after it)
        self._echo = False  # Realtime echoes response metadata: a response without it is never a narration
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
        if self.meeting is not None:
            self._key_handlers.update({"phase": self._key_phase, "reset": self._key_reset})
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
            restart, self._link_action = self._link_action == "restart", None
        # outside the lock: the link reports its status back through on_realtime_status
        if restart:
            await self._restart_link()
        elif start_link:
            await self._start_link()

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

    @property
    def turn_detection(self) -> dict | None:
        """The session's `turn_detection`: the setting (null by default: manual turns; null in hold mode), and always
        null for a meeting, whose discussion must be transcribed without replies (it can't change mid-session)."""
        return None if self.meeting is not None else self.settings.session_turn_detection

    @property
    def _segmented(self) -> bool:
        """Tap mode with manual turns: a segmenter ends each spoken turn."""
        return self.settings.mic_mode != "hold" and self.turn_detection is None

    def session_config(self) -> dict:
        """The full session object (design §6) for the current context: the first session.update of a connection."""
        turn_detection = self.turn_detection
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
            "tools": tools.tool_specs(self.talk, self.meeting),
            "tool_choice": "auto",
        }
        if self.settings.temperature is not None:
            config["temperature"] = self.settings.temperature
        return config

    def snapshot(self) -> dict:
        status, detail = self._connection
        snap = {
            "state": self.state, "phase": self.phase, "slide": self.current_slide.id, "banner": self.banner,
            "thinking": self.thinking, "can_resume": self.can_resume, "mic_mode": self.settings.mic_mode,
            "narration_mode": self.settings.narration, "narration": self.narration,
            "connection": {"status": status, "detail": detail}, "talk": self.talk.id,
        }
        if self.mstate is not None:
            checkpoint = self.checkpoint()
            snap.update({"agenda_phase": self.mstate.phase, "status": checkpoint["status"],
                         "talking_point": checkpoint["talking_point_id"], "checkpoint": checkpoint,
                         "speaker": self.speaker})
        return snap

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
        return self.state in SPEAKING and self.turn is not None and self.turn.first_audio_at is None

    @property
    def can_resume(self) -> bool:
        return self.cursor is not None and self.state != NARRATING

    @property
    def _replying(self) -> bool:
        """A turn's response is (to be) bound and streams to the deck: REPLYING, or INTERVENING in a discussion."""
        return self.state in SPEAKING

    @property
    def agenda_phase(self) -> str | None:
        """presentation · discussion · wrapup (a meeting); None for a plain talk."""
        return self.mstate.phase if self.mstate is not None else None

    def _meeting_time(self) -> float:
        """Seconds since the meeting started (MeetingState's clock)."""
        return max(0.0, self.clock.now() - self._t0)

    @property
    def status(self) -> str:
        """The checkpoint status (contract §5.6): presenting · answering · resuming · paused · listening ·
        intervening · wrapping_up · idle."""
        state, turn = self.state, self.turn
        if state == NARRATING:
            return "resuming" if self.cursor is not None and self.cursor.live else "presenting"
        if state == ROOM:
            return "listening"
        if state == INTERVENING:
            return "intervening"
        if state in (LISTENING, REPLYING):
            if turn is not None and turn.kind == KIND_SUMMARY:
                return "wrapping_up"
            if turn is not None and turn.kind == KIND_HANDOVER:
                return "presenting"
            return "answering"
        if state == PAUSED:
            return "paused"
        if self._countdown_running() and self.cursor is not None:
            return "resuming"
        return "idle"

    def _point_id(self, slide_id: str, chunk: int) -> str | None:
        try:
            return self.talk.slide(slide_id).chunks[chunk].id
        except (KeyError, IndexError):
            return None

    def checkpoint(self) -> dict:
        """{slide_id, talking_point_id, status}: where the presentation is (or continues), and what the facilitator
        is doing. The discussion and the wrap-up have no talking point."""
        cursor = self.cursor
        point = None
        if self.agenda_phase in (None, PRESENTATION):
            where = (cursor.slide, cursor.chunk) if cursor is not None else self._last_point
            point = self._point_id(*where) if where is not None else None
        slide_id = cursor.slide if cursor is not None else self.current_slide.id
        return {"slide_id": slide_id, "talking_point_id": point, "status": self.status}

    @property
    def narration(self) -> str | None:
        """What the room hears while NARRATING: live (Higgs Realtime), backup (the renders standing in for it) or
        rendered (narration = "rendered"); None in every other state."""
        return self.narration_source if self.state == NARRATING else None

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

    async def _restart_link(self) -> None:
        """A fresh Realtime session (Restart meeting): the model's memory is empty too."""
        try:
            await self.realtime.restart()
        except Exception as exc:  # noqa: BLE001
            self._note("controller_error", {"where": "realtime.restart", "error": repr(exc)})

    def _state_message(self) -> dict:
        msg = {"type": "state", "state": self.state, "phase": self.phase, "slide": self.current_slide.id,
               "banner": self.banner, "thinking": self.thinking, "can_resume": self.can_resume,
               "mic_mode": self.settings.mic_mode, "narration": self.narration}
        if self.mstate is not None:
            checkpoint = self.checkpoint()
            msg.update({"agenda_phase": self.mstate.phase, "status": checkpoint["status"],
                        "talking_point": checkpoint["talking_point_id"], "checkpoint": checkpoint})
        return msg

    async def _send_meeting(self) -> None:
        """The deck's `meeting` message: the whole meeting state (after every change), with the checkpoint, and the
        notes once the meeting is wrapped up."""
        if self.mstate is None:
            return
        self.mstate.checkpoint = self.checkpoint()
        body = self.mstate.to_json()
        msg = {**(body if isinstance(body, dict) else {}), "type": "meeting", "checkpoint": self.checkpoint()}
        if self.mstate.phase == WRAPUP and "notes" not in msg:
            msg["notes"] = self.mstate.notes()
        await self._send(msg)

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

    def _idle_banner(self) -> str:
        """IDLE after an interruption: "Paused — R to resume" when there is something to resume, else why Relay is
        silent (a cancelled "Starting…" countdown had nothing to resume)."""
        return BANNER_PAUSED_R if self.cursor is not None else self._phase_banner()

    def _room_banner(self) -> str:
        return BANNER_DISCUSSION.format(prompt=prompts.discussion_prompt(self.meeting))

    def _phase_banner(self) -> str:
        """Why Relay is silent in IDLE, by phase."""
        if self.agenda_phase == WRAPUP:
            return BANNER_WRAPPED
        if self.agenda_phase == DISCUSSION:
            return self._room_banner()
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
        elif kind == "speaker" and self.meeting is not None:
            self._set_speaker(msg.get("name"))
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

    def _set_speaker(self, name: Any) -> None:
        """The deck's speaker selector: a participant's name labels the room's lines; anything else (`Room`, null)
        leaves them unlabelled."""
        names = {person.name.lower(): person.name for person in self.meeting.participants}
        speaker = names.get(name.strip().lower()) if isinstance(name, str) and name.strip() else None
        if speaker != self.speaker:
            self.speaker = speaker
            self._note("speaker", {"name": speaker, "raw": name})

    async def _greet(self) -> None:
        """hello + slide + state + connection for a (re)connected deck. A deck that connects mid-turn or
        mid-narration starts silent, so Relay goes IDLE first (cursor kept: R resumes; a live narration is cut at
        what the wall clock says the room heard). In a meeting's discussion it goes back to listening to the room
        (the new deck is told to stream the mic), and the bundle ends with the `meeting` message."""
        if self.state in (NARRATING, LISTENING, REPLYING, INTERVENING):
            await self._halt(None, cut=False)
            if self.agenda_phase == DISCUSSION:
                await self._resume_room(run_flows=False)
            else:
                self._set_state(IDLE, BANNER_PAUSED_R)
        await self._send({"type": "hello", "talk": self.talk.id, "version": self._version(),
                          "mic_mode": self.settings.mic_mode, "narration_mode": self.settings.narration,
                          "meeting": meeting_info(self.meeting), "snapshot": self.snapshot()})
        if self.state != NARRATING:
            await self._send({"type": "stop"})
        await self._send({"type": "mic", "open": self._mic_open})  # open only while listening to the room
        await self._send({"type": "slide", "slide": self.current_slide.id, "fragment": self.fragment})
        await self._sync_state(force=True)
        status, detail = self._connection
        await self._send({"type": "connection", "status": status, "detail": detail})
        await self._send_meeting()

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
        if self.agenda_phase == DISCUSSION:  # still the discussion: the next deck streams the room mic at once
            await self._resume_room(run_flows=False)
            return
        self._mic_open = False
        self._set_state(IDLE, self._idle_banner())

    async def _on_mic(self, msg: dict) -> None:
        turn = self.turn
        pcm = msg.get("pcm")
        if not self._mic_open or not isinstance(pcm, str) or not pcm:
            return
        if self.state == ROOM:
            await self._room_frame(pcm)
            return
        if self.state != LISTENING or turn is None:
            return  # (INTERVENING, or a reply in the discussion: half duplex, the facilitator never hears itself)
        turn.mic_frames += 1
        if await self._rt({"type": "input_audio_buffer.append", "audio": pcm}):
            self._stream_ms += _b64_bytes(pcm) / BYTES_PER_MS
            turn.marks.append((self._stream_ms, self.clock.now()))  # where the stream was, when (latency)
        if turn.segmenter is not None:
            await self._segment_turn(turn, pcm)

    async def _segment_turn(self, turn: Turn, pcm: str) -> None:
        """Manual turns (turn_detection null): the turn's segmenter hears the speech start (as speech_started would)
        and ends the turn after its silence (commit + response.create, as Space "send now" does). Its end is the
        latency metric's end of speech: the stream position `end_ms` before the frame that ended it."""
        audio = _pcm(pcm)
        if audio is None:
            return
        for kind, _ in turn.segmenter.feed(audio):
            if kind == "start" and not turn.speech_started:
                turn.speech_started = True
                self._cancel_timer("listen_silence")
            elif kind == "end":
                end_ms = getattr(turn.segmenter, "end_frames", 70) * SEGMENTER_FRAME_MS
                estimate = self._stream_time(turn, self._stream_ms - end_ms)
                await self._send_turn(turn, "vad")
                if self.turn is turn and turn.ended_by == "vad" and estimate is not None:
                    turn.speech_end_at = min(estimate, turn.turn_end_at or estimate)
                    turn.speech_end_estimated = True
                return

    @staticmethod
    def _stream_time(turn: Turn, position: float) -> float | None:
        """When the mic audio reached input-stream `position` (ms), from the arrival times of the turn's frames."""
        for mark_position, arrived in turn.marks:
            if mark_position >= position:
                return arrived - (mark_position - position) / 1000
        return None

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
        if not self.realtime.ready and self.agenda_phase == DISCUSSION:
            await self._send_error(ERR_OFFLINE_ROOM)  # the room keeps being listened to (typed lines still work)
            return
        if not self.realtime.ready:
            await self._offline_floor(msg)
            return
        adopted = None
        if self.state == ROOM and self._room_seg is not None and self._room_seg.in_speech:
            # A direct question in the discussion: the utterance being spoken becomes the turn (its audio is in the
            # input buffer already, and its segmenter ends the turn).
            adopted, self._room_seg = self._room_seg, None
        turn = await self._take_floor(msg)
        await self._open_floor(turn, segmenter=adopted)

    async def _key_space_up(self, msg: dict) -> None:
        turn = self.turn
        if self.settings.mic_mode != "hold" or self.state != LISTENING or turn is None:
            return
        await self._send_turn(turn, "hold")

    async def _key_esc(self, msg: dict) -> None:
        if self._countdown_running():
            await self._stop_countdown()
            self._set_state(IDLE, self._idle_banner())
        elif self.state == LISTENING:
            await self._close_mic(clear=True)
            await self._back_to_origin()
        elif self._replying:
            await self._cut_reply(msg)
            await self._back_to_origin()

    async def _key_next(self, msg: dict) -> None:
        await self._step(msg, +1)

    async def _key_prev(self, msg: dict) -> None:
        await self._step(msg, -1)

    async def _step(self, msg: dict, delta: int) -> None:
        target = self.slide_index + delta
        if self.agenda_phase in (DISCUSSION, WRAPUP):
            # After the presentation the arrows only show slides (for reference): no narration, the room keeps
            # being heard. A reply the deck just flushed ends properly.
            if self._replying:
                await self._cut_reply(msg)
                await self._back_to_origin()
            if 0 <= target < len(self.talk.slides):
                self.slide_index = target
                self.fragment = None
                await self._send({"type": "slide", "slide": self.current_slide.id, "fragment": None})
            return
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
        ignored, so put narration back where it was (a live narration, which the flush cut off, continues live from
        what was heard), and end a flushed reply properly. A running countdown (which the deck hides on any key) is
        cancelled: presenter keys always win over the watchdog."""
        if self._countdown_running():
            await self._stop_countdown()
            self._set_state(IDLE, self._idle_banner())
        elif self.state == NARRATING and self.live is not None:
            await self._cut_live(msg)
            await self._play(self.cursor)
        elif self.state == NARRATING:
            await self._play(self._pause_cursor(self._position(msg.get("at"))))
        elif self._replying:
            await self._cut_reply(msg)
            await self._back_to_origin()

    async def _key_pause(self, msg: dict) -> None:
        if self.agenda_phase in (DISCUSSION, WRAPUP):
            if self._replying:  # P stops the facilitator (the deck flushed the reply already)
                await self._cut_reply(msg)
                await self._back_to_origin()
            return
        if self.state == NARRATING:
            if self.live is not None:  # P again continues the slide live (or the backup replays it, if offline)
                await self._cut_live(msg)
            else:
                await self._send({"type": "stop"})
                self.cursor = self._pause_cursor(self._position(msg.get("at")))
            self._set_state(PAUSED, BANNER_PAUSED)
        elif self.state == PAUSED and self.cursor is not None:
            await self._play(self.cursor)
        elif self._countdown_running():
            await self._stop_countdown()
            if self.cursor is not None:  # P during "Resuming…": stay paused at the resume point
                self._set_state(PAUSED, BANNER_PAUSED)
            else:  # "Starting…": nothing to pause
                self._set_state(IDLE, self._phase_banner())

    async def _key_human(self, msg: dict) -> None:
        await self._halt(msg, cut=True)
        if self.agenda_phase == DISCUSSION:  # the room has the floor anyway: the facilitator just stops talking
            await self._resume_room()
            return
        self.floor_human = True
        self._set_state(IDLE, self._phase_banner() if self.agenda_phase == WRAPUP else self._banner_human())

    async def _key_relay(self, msg: dict) -> None:
        if self.state in (NARRATING, ROOM, INTERVENING):  # already presenting / listening / speaking up
            return
        await self._halt(msg, cut=False)
        await self._resume("key")

    async def _key_phase(self, msg: dict) -> None:
        """The agenda button (a disclosed fallback for the automatic transitions): presentation → the hand-over and
        the discussion; discussion → the wrap-up; wrap-up → nothing."""
        phase = self.agenda_phase
        turn = self.turn
        if phase == PRESENTATION:
            handing_over = turn is not None and turn.kind == KIND_HANDOVER
            await self._halt(msg, cut=True)
            if handing_over:  # pressed again during the hand-over line: straight to the discussion
                await self._enter_discussion()
            else:
                await self._handover()
        elif phase == DISCUSSION:
            await self._halt(msg, cut=False)
            await self._enter_wrapup()
        self._note("phase_key", {"from": phase, "to": self.agenda_phase})

    async def _key_reset(self, msg: dict) -> None:
        """Restart the meeting: everything stops, the meeting state and the alignment check start empty, the deck is
        back on the first slide in the presentation phase, and the Realtime session is replaced by a fresh one (after
        this handler) so the model forgets the meeting too."""
        await self._halt(msg, cut=False)
        await self._room_close(commit=False)
        await self._close_mic(clear=True)
        self._room_seg = None
        self._room_commits.clear()
        self._room_items.clear()
        self._room_commit_held = False
        self._flows.clear()
        self.mstate.reset()
        self.engine.reset()
        self._t0 = self.clock.now()
        self.cursor = None
        self.live = None
        self.narration_source = None
        self.started = False
        self.floor_human = False
        self.floor_offline = False
        self._presented = False
        self._cut_info = None
        self._last_point = None
        self._history.clear()
        self.speaker = None
        self.slide_index = 0
        self.fragment = self._fragments(self.current_slide, 0)
        await self._send({"type": "slide", "slide": self.current_slide.id, "fragment": self.fragment})
        self._set_state(IDLE, self._phase_banner())
        await self._send_meeting()
        self._link_action = "restart"
        self._note("meeting_reset", {})

    async def _on_chunk_ended(self, msg: dict) -> None:
        cursor = self.cursor
        if (self.state != NARRATING or self.live is not None or cursor is None
                or _int(msg.get("play_id")) != self.play_id):  # (a live narration has no chunks)
            self._note("chunk_ended_ignored", {"play_id": msg.get("play_id"), "current": self.play_id,
                                               "state": self.state})
            return
        slide = self.talk.slide(cursor.slide)
        if cursor.chunk + 1 < len(slide.chunks):
            if self.meeting is not None:  # every talking point decides again: live if the link is back
                await self._play(Cursor(slide.id, cursor.chunk + 1))
            else:
                await self._play_rendered(Cursor(slide.id, cursor.chunk + 1))
            return
        await self._after_slide(slide)

    async def _on_reply_drained(self, msg: dict) -> None:
        reply = _int(msg.get("reply"))
        live = self.live
        if self.state == NARRATING and live is not None:
            if live.response_done and reply == live.reply:
                await self._narration_drained("browser")
            return
        turn = self.turn
        if not self._replying or turn is None or not turn.response_done or reply != turn.reply:
            return
        await self._drained("browser")

    async def _on_typed(self, msg: dict) -> None:
        text = " ".join(str(msg.get("text") or "").split())
        await self._discard_queued("typed question")
        if not text:
            await self._undo_local_stop(msg)
            return
        if self.agenda_phase == DISCUSSION and self.state in (ROOM, INTERVENING, IDLE):
            # A typed line in the discussion is an utterance of the room (the typed fallback: no Realtime needed).
            if self._replying:  # the deck flushed the facilitator's reply as it sent the line
                await self._cut_reply(msg)
                await self._back_to_origin()
            await self._room_utterance(text, self.speaker, "typed")
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
        if self.state in (LISTENING, REPLYING, INTERVENING) and previous is not None:
            origin = previous.origin
        elif self.agenda_phase == DISCUSSION:
            origin = ORIGIN_ROOM  # a direct question in the discussion: back to listening afterwards
        elif self.state == NARRATING or (countdown and self.cursor is not None):  # not "Starting…": nothing to resume
            origin = ORIGIN_NARRATION
        elif self.state == PAUSED:
            origin = ORIGIN_PAUSED
        else:
            origin = ORIGIN_IDLE
        # (a question that cuts the hand-over or the summary keeps its origin: the discussion, or the wrapped-up
        # meeting, follows the answer)
        await self._halt(msg, cut=True)
        self.floor_offline = False
        return Turn(origin=origin, phase=self.agenda_phase)

    async def _offline_floor(self, msg: dict) -> None:
        """Realtime is down: the tap gives the presenter the floor (narration stops at the cut; R resumes). Until
        narration starts again, → / ← only move the deck (the presenter is talking over it)."""
        await self._take_floor(msg)
        self.floor_offline = True
        self._set_state(IDLE, BANNER_OFFLINE.format(presenter=self.talk.presenter))

    async def _open_floor(self, turn: Turn, *, segmenter: Segmenter | None = None) -> None:
        """Open the mic for the turn: LISTENING. Manual turns get a segmenter (which ends the turn); an `adopted`
        segmenter is the room's, mid-utterance: its audio stays in the input buffer and the speech has started."""
        self.turn = turn
        turn.stream_start_ms = self._stream_ms
        await self._rt(self._turn_update())
        if segmenter is None:
            await self._rt({"type": "input_audio_buffer.clear"})
        if segmenter is not None:
            turn.speech_started = True
            turn.mic_frames = SEND_NOW_MIN_FRAMES  # the utterance so far counts (send now, the hold release)
            turn.segmenter = segmenter if self.settings.mic_mode != "hold" else None
        elif self._segmented:
            turn.segmenter = Segmenter()
        self._mic_open = True
        await self._send({"type": "mic", "open": True})
        self._set_state(LISTENING, None)
        if self.settings.mic_mode != "hold" and not turn.speech_started:
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
        await self._request_events(turn, [first, {"type": "response.create"}])

    async def _request_events(self, turn: Turn, events: list[dict]) -> None:
        """Send the turn's request now, or hold it while another response is active (cancelling that one)."""
        if not self._active:
            for event in events:
                await self._rt(event)
            return
        turn.pending = events
        if self._active - self._ignored:
            self._ignored |= self._active
            await self._rt({"type": "response.cancel"})

    async def _send_pending(self) -> None:
        """What was held back, once no response is active any more: a room utterance's commit, then a question (the
        user item + response.create), a follow-up or a facilitator request, or a narration request."""
        if self._active:
            return
        if self._room_commit_held:
            self._room_commit_held = False
            if await self._rt({"type": "input_audio_buffer.commit"}):
                self._room_commits.append(self.speaker)
        turn, live = self.turn, self.live
        if turn is not None and turn.pending and self._replying and turn.response_id is None:
            events, turn.pending = turn.pending, []
            for event in events:
                await self._rt(event)
        elif live is not None and live.pending and self.state == NARRATING and live.response_id is None:
            events, live.pending = live.pending, []
            for event in events:
                if not await self._rt(event):
                    await self._narration_failed("not sent")
                    return

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
            played = self._played_ms(msg, turn.reply, turn.audio_bytes, turn.first_audio_at)
            await self._rt({"type": "conversation.item.truncate", "item_id": turn.audio_item_id,
                            "content_index": 0, "audio_end_ms": played})
        if turn.reply is not None:
            await self._send({"type": "reply_flush", "reply": turn.reply})
        self._note("reply_cut", {"reply": turn.reply, "response_id": turn.response_id, "played_ms": played,
                                 "response_done": turn.response_done})

    def _played_ms(self, msg: dict | None, reply: int | None, audio_bytes: int, first_audio_at: float | None) -> int:
        """Audio of reply `reply` the room actually heard: the deck's count if it is about this reply, else an
        estimate from the wall clock since its first audio; never more than was received."""
        received = audio_bytes // BYTES_PER_MS
        reported = _int(msg.get("reply_played_ms")) if msg else None
        about = _int(msg.get("reply")) if msg else None
        if reported is not None and (about is None or about == reply):
            played = reported
        elif first_audio_at is not None:
            played = round((self.clock.now() - first_audio_at) * 1000)
        else:
            played = 0
        return max(0, min(played, received))

    async def _back_to_origin(self) -> None:
        """End the turn and return to where it started: narration plays from the cursor (the cut sentence); in a
        meeting, the room is listened to again, the discussion starts (after the hand-over), or the meeting stays
        wrapped up."""
        origin = self.turn.origin if self.turn is not None else ORIGIN_IDLE
        await self._end_turn()
        if origin == ORIGIN_NARRATION and self.cursor is not None:
            await self._play(self.cursor)
        elif origin == ORIGIN_PAUSED and self.cursor is not None:
            self._set_state(PAUSED, BANNER_PAUSED)
        elif origin == ORIGIN_ROOM and self.agenda_phase == DISCUSSION:
            await self._resume_room()
        elif origin == ORIGIN_HANDOVER and self.agenda_phase == PRESENTATION:
            await self._enter_discussion()
        else:
            self._set_state(IDLE, self._phase_banner())

    async def _end_turn(self) -> None:
        turn = self.turn
        if turn is not None:
            self._remember(turn)
        self.turn = None
        for name in TURN_TIMERS:
            self._cancel_timer(name)
        if turn is not None and self.mstate is not None:
            await self._finish_meeting_turn(turn)

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
        narration that was playing or paused is cut (the cursor moves to the cut sentence's start); a live
        narration is cut either way (the cursor continues it from what was heard). The caller sets the next
        state."""
        await self._discard_queued("interrupted")
        await self._stop_countdown()
        if self.state == LISTENING:
            await self._close_mic(clear=True)
        elif self._replying:
            await self._cut_reply(msg)
        elif self.state == NARRATING and self.live is not None:
            await self._cut_live(msg)
        elif self.state == NARRATING:
            await self._send({"type": "stop"})
            if cut:
                await self._cut_narration(msg.get("at") if msg else None)
        elif self.state == PAUSED and cut:
            await self._cut_paused()
        elif self.state == ROOM:
            await self._room_close(commit=True)  # an utterance being spoken still gets transcribed
        await self._end_turn()

    async def _drained(self, reason: str) -> None:
        """The reply has finished playing: run the queued tool, else count down to the presentation (a greeting,
        D57) or back to the script (origin narration), else return to PAUSED / IDLE. In a meeting a follow-up may be
        waiting for this reply to finish (it goes out now, the turn goes on), and the facilitator's own turns go on to
        the discussion (hand-over), the room (intervention, decision, a direct question) or the end (summary)."""
        turn = self.turn
        if turn is None:
            return
        if turn.next_followup is not None:
            kind, turn.next_followup = turn.next_followup, None
            await self._send_followup(turn, kind)
            return
        self._note("drained", {"reason": reason, "reply": turn.reply,
                               "queued": turn.queued.name if turn.queued else None})
        await self._end_turn()
        call, turn.queued = turn.queued, None
        if self.mstate is not None and (turn.kind in (KIND_HANDOVER, KIND_SUMMARY)
                                        or turn.origin in (ORIGIN_HANDOVER, ORIGIN_WRAPUP)):
            if call is not None:  # the presentation is over (or the meeting): nothing of it to move any more
                await self._tool_message(call.call_id, call.name, call.args, "discarded")
                self._note("tool", {"call_id": call.call_id, "name": call.name, "status": "discarded",
                                    "reason": turn.kind})
            if turn.kind == KIND_SUMMARY or turn.origin == ORIGIN_WRAPUP:
                self._set_state(IDLE, BANNER_WRAPPED)
            else:
                await self._enter_discussion()
        elif call is not None:
            await self._apply(call)
            await self._tool_message(call.call_id, call.name, call.args, "applied")
            self._note("tool", {"call_id": call.call_id, "name": call.name, "args": call.args, "status": "applied"})
            if turn.origin == ORIGIN_ROOM and self.agenda_phase == DISCUSSION and self.state == IDLE:
                await self._resume_room()
        elif turn.origin == ORIGIN_ROOM and self.agenda_phase == DISCUSSION:
            await self._resume_room()
        elif self.current_slide.is_qa and (turn.first_audio_at is not None or turn.heard):
            # A Q&A answer was given but the model didn't hand back (live F5: it rarely does; D-watchdog).
            await self._auto(tools.HANDOFF_TO_HUMAN)
        elif self.phase == "opening" and (turn.first_audio_at is not None or turn.heard):
            # The greeting was heard but called no tool (F5 again): the watchdog starts the presentation (D57).
            self._set_state(IDLE, BANNER_STARTING)
            await self._send({"type": "countdown", "seconds": math.ceil(COUNTDOWN_S)})
            self._start_timer("countdown", COUNTDOWN_S, self._on_start_countdown)
        elif turn.origin == ORIGIN_NARRATION:
            self._set_state(IDLE, BANNER_RESUMING)
            await self._send({"type": "countdown", "seconds": math.ceil(COUNTDOWN_S)})
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
        """Narrate from `cursor`: live (D52) when the link is ready and the cursor starts a slide or continues a live
        one, else from the renders (rendered mode, or the backup, D53). A backup narration cut mid-slide continues
        from the renders even once the link is back; the next slide start decides again."""
        starts = cursor.point_start if self.meeting is not None else cursor.slide_start  # a meeting: per talking point
        if self._live_mode and self.realtime.ready and (cursor.live or starts):
            await self._narrate(cursor, show_slide=show_slide)
        else:  # the backup can't continue a live slide (talking point): it presents it again from its start
            backup = Cursor(cursor.slide, cursor.chunk if self.meeting is not None else 0) if cursor.live else cursor
            await self._play_rendered(backup, show_slide=show_slide)

    async def _play_rendered(self, cursor: Cursor, *, show_slide: bool = False) -> None:
        """Play the rendered chunk at `cursor` (the narration of rendered mode; the backup in live mode)."""
        slide = self.talk.slide(cursor.slide)
        fragment = self._fragments(slide, cursor.chunk)
        if show_slide or slide.index != self.slide_index:
            self.slide_index = slide.index
            await self._send({"type": "slide", "slide": slide.id, "fragment": fragment})
        self.play_id += 1
        self.fragment = fragment
        self.cursor = cursor
        self._last_point = (slide.id, cursor.chunk)
        self.started = True
        self.floor_human = False
        self.floor_offline = False
        self._cut_info = None
        self.narration_source = "backup" if self._live_mode else "rendered"
        await self._send({"type": "play", "play_id": self.play_id, "slide": slide.id, "chunk": cursor.chunk,
                          "offset_ms": cursor.offset_ms, "fragment": fragment, "resume_word": cursor.resume_word})
        self._set_state(NARRATING, None)

    async def _narrate(self, cursor: Cursor, *, show_slide: bool = False) -> None:
        """Present the cursor's slide live: the whole slide shows (all its fragments) and the Realtime session is
        asked to present it, from its start or, for a live cursor, from what the room heard. A meeting presents one
        talking point (the cursor's chunk) per request, and the slide reveals its fragments up to that point."""
        slide = self.talk.slide(cursor.slide)
        chunk = min(max(cursor.chunk, 0), len(slide.chunks) - 1) if self.meeting is not None else 0
        fragment = self._fragments(slide, chunk) if self.meeting is not None else None
        if show_slide or slide.index != self.slide_index or self.fragment != fragment:
            self.slide_index = slide.index
            await self._send({"type": "slide", "slide": slide.id, "fragment": fragment})
        self.fragment = fragment
        self.cursor = Cursor(slide.id, chunk, live=cursor.live, heard=cursor.heard, heard_ms=cursor.heard_ms)
        self._last_point = (slide.id, chunk)
        self.started = True
        self.floor_human = False
        self.floor_offline = False
        self._cut_info = None
        self._reply_seq += 1
        self.live = LiveNarration(slide.id, self._reply_seq, continuing=cursor.live, chunk=chunk)
        self.narration_source = "live"
        self._set_state(NARRATING, None)
        await self._request_narration(self.live)

    async def _after_slide(self, slide: Slide) -> None:
        """The slide's narration is over: wait for a question (Q&A slide), present the next relay slide, or leave
        the floor to the presenter (a human slide next, or the end of the deck). A meeting's presentation ends with
        the hand-over to the discussion once no relay slide follows."""
        self.cursor = None
        if slide.is_qa:
            self._set_state(IDLE, BANNER_QA)
            return
        following = slide.index + 1
        if self.agenda_phase == PRESENTATION and self.talk.next_relay_index(following) is None:
            await self._handover()
            return
        if following < len(self.talk.slides) and self.talk.slides[following].is_relay:
            await self._play(Cursor(self.talk.slides[following].id, 0), show_slide=True)
        elif following < len(self.talk.slides):
            self._set_state(IDLE, self._banner_human())
        else:
            self._set_state(IDLE, BANNER_END)

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
        self._presented = False

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
        otherwise nothing happens (IDLE). After a meeting's presentation: back to listening to the room (the
        discussion), or nothing (the wrap-up)."""
        if self.agenda_phase == DISCUSSION:
            await self._resume_room()
        elif self.agenda_phase == WRAPUP:
            self._set_state(IDLE, BANNER_WRAPPED)
        elif self.cursor is not None:
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
        if cursor is None or cursor.live or (self._live_mode and cursor.slide_start):
            return  # a live slide continues from what was heard; a slide start stays one (it may start live)
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

    # --- live narration (D52) --------------------------------------------------------------------------------

    def _narration_seconds(self, slide: Slide) -> int:
        """How long a live narration of `slide` should take: as long as its backup, else its notes at a speaking
        pace; within NARRATION_MIN_S…NARRATION_MAX_S."""
        rendered = [self.chunks.get((slide.id, chunk.index)) for chunk in slide.chunks]
        backup_ms = sum(chunk.duration_ms for chunk in rendered if chunk is not None)
        seconds = backup_ms / 1000 if backup_ms else word_count(slide.narration) / WORDS_PER_SECOND
        return round(min(NARRATION_MAX_S, max(NARRATION_MIN_S, seconds)))

    def _point_seconds(self, slide: Slide, chunk: int) -> int:
        """How long a live talking point should take: as long as its backup chunk, else its note at a speaking pace;
        within POINT_MIN_S…POINT_MAX_S."""
        rendered = self.chunks.get((slide.id, chunk))
        seconds = (rendered.duration_ms / 1000 if rendered is not None and rendered.duration_ms
                   else word_count(slide.chunks[chunk].text) / WORDS_PER_SECOND)
        return round(min(POINT_MAX_S, max(POINT_MIN_S, seconds)))

    async def _request_narration(self, live: LiveNarration) -> None:
        """Ask the Realtime session to present the slide: one response.create with its own instructions, tagged in
        `metadata` (echoed on response.created) and with tool_choice "none" (live findings 2026-10-23). Like a
        question, it is held back while another response is active, and that one is cancelled. Never preceded by a
        user message (a "stage direction" made the model say just "Hello."). No audio within NARRATION_WAIT_S →
        it failed."""
        slide = self.talk.slide(live.slide)
        cursor = self.cursor
        heard = None
        if self.meeting is not None:  # one talking point
            chunk = slide.chunks[live.chunk]
            seconds = self._point_seconds(slide, live.chunk)
            if live.continuing and cursor is not None and cursor.live:
                seconds = max(POINT_CONTINUE_MIN_S, round(seconds - cursor.heard_ms / 1000))
                heard = cursor.heard
            instructions = prompts.talking_point_instructions(
                self.talk, self.meeting, slide, chunk, seconds=seconds, continuing=live.continuing, heard=heard,
                first=not self._presented)
            metadata = {"relay": "narration", "reply": str(live.reply), "slide": slide.id, "talking_point": chunk.id}
        else:
            seconds = self._narration_seconds(slide)
            if live.continuing and cursor is not None and cursor.live:
                seconds = max(CONTINUE_MIN_S, round(seconds - cursor.heard_ms / 1000))
                heard = cursor.heard
            instructions = narration_instructions(self.talk, slide, seconds=seconds, continuing=live.continuing,
                                                  heard=heard, first=not self._presented)
            metadata = {"relay": "narration", "reply": str(live.reply), "slide": slide.id}
        event = {"type": "response.create", "response": {
            "instructions": instructions, "metadata": metadata, "tool_choice": "none"}}
        self._start_timer("narration_wait", NARRATION_WAIT_S, self._on_narration_wait)
        self._note("narration_request", {"slide": slide.id, "reply": live.reply, "attempt": live.attempt,
                                         "continuing": live.continuing, "seconds": seconds,
                                         "talking_point": metadata.get("talking_point"),
                                         "held": bool(self._active)})
        if self._active:
            live.pending = [event]
            if self._active - self._ignored:
                self._ignored |= self._active
                await self._rt({"type": "response.cancel"})
        elif not await self._rt(event):
            await self._narration_failed("not sent")

    def _answers_narration(self, response: dict, live: LiveNarration) -> bool:
        """Whether a response.created answers the narration request `live`: its echoed metadata names that request.
        A response without metadata may be it too, unless Realtime has been seen echoing metadata: then it is
        another one (e.g. the late response of a question that was cut while Relay was thinking)."""
        if not response.get("metadata"):
            return not self._echo
        tag = _narration_tag(response)
        return tag is not None and tag.get("reply") == str(live.reply)

    async def _finish_narration(self, live: LiveNarration) -> None:
        """No more narration audio: answer the tool calls it made (it may not drive the deck), reply_end, and arm
        the drain fallback. A narration that never spoke failed."""
        live.response_done = True
        self._cancel_timer("narration_stall")
        await self._reject_narration_calls(live)
        if live.first_audio_at is None:
            await self._narration_failed("no audio")
            return
        self._cancel_timer("narration_wait")
        await self._send({"type": "reply_end", "reply": live.reply})
        self._start_timer("narration_drain",
                          self._remaining_audio_s(live.first_audio_at, live.audio_bytes) + DRAIN_GRACE_S,
                          self._on_narration_drain)

    async def _reject_narration_calls(self, live: LiveNarration) -> None:
        """A narration was asked not to call tools, and the deck moves on by itself: every call it made anyway gets
        an error output (at its response.done, never followed by response.create) and shows as rejected."""
        if live.response_id is None:
            return
        for call_id, (name, arguments) in live.calls.items():
            if call_id in self._answered:
                continue
            await self._owe_output(live.response_id, call_id, NARRATION_CALL_OUTPUT)
            await self._tool_message(call_id, str(name), tools.parse_args(arguments), "rejected")
            self._note("tool", {"call_id": call_id, "name": name, "status": "rejected", "reason": "narrating"})

    async def _narration_drained(self, reason: str) -> None:
        """The live narration has finished playing: on to what follows the slide; a narration the link or a stall
        cut short is followed by the backup presenting the slide again from its start."""
        live = self.live
        if live is None:
            return
        self.live = None
        for name in NARRATION_TIMERS:
            self._cancel_timer(name)
        self._note("narration_drained", {"reason": reason, "reply": live.reply, "slide": live.slide,
                                         "chunk": live.chunk, "incomplete": live.incomplete})
        slide = self.talk.slide(live.slide)
        if live.incomplete:
            await self._play_rendered(Cursor(live.slide, live.chunk))
        elif self.meeting is not None and live.chunk + 1 < len(slide.chunks):
            await self._play(Cursor(live.slide, live.chunk + 1))  # the slide's next talking point
        else:
            await self._after_slide(slide)

    async def _drop_narration_response(self, live: LiveNarration) -> None:
        """Stop a narration's response for good: cancel it if it may still be generating, ignore its later events,
        and owe its tool calls the cut output. A request still held back is simply never sent."""
        if not live.response_done and (live.response_id is not None or not live.pending):
            await self._rt({"type": "response.cancel"})
        if live.response_id is not None:
            self._ignored.add(live.response_id)
            if not live.response_done:
                for call_id in live.calls:
                    await self._owe_output(live.response_id, call_id, CUT_CALL_OUTPUT)

    async def _narration_failed(self, why: str) -> None:
        """A narration request that never spoke: no audio in time, a response that ended or stalled silent, a
        request the link could not send. Ask once more while the link is ready, else the backup presents the slide
        (D53)."""
        live = self.live
        if live is None:
            return
        for name in NARRATION_TIMERS:
            self._cancel_timer(name)
        if live.pending:  # the response it waited on never reported done: don't let it hold back the retry
            self._note("stale_responses", {"ids": sorted(self._active)})
            self._active.clear()
        await self._drop_narration_response(live)
        await self._send({"type": "reply_flush", "reply": live.reply})
        retry = live.attempt < NARRATION_RETRIES and self.realtime.ready
        self._note("narration_failed", {"why": why, "slide": live.slide, "reply": live.reply,
                                        "attempt": live.attempt, "then": "retry" if retry else "backup"})
        if retry:
            self._reply_seq += 1
            self.live = LiveNarration(live.slide, self._reply_seq, live.continuing, attempt=live.attempt + 1,
                                      chunk=live.chunk)
            await self._request_narration(self.live)
        else:
            self.live = None
            await self._play_rendered(Cursor(live.slide, live.chunk))

    async def _cut_live(self, msg: dict | None) -> tuple[str, str, int] | None:
        """Cut the live narration off: cancel it if still generating, truncate the model's memory of it to what
        the room heard (the deck's count, else the wall clock), flush the deck's reply player. The cursor then
        continues the slide live from what was heard (a cut before any audio leaves it where it was), and STATE's
        cut line quotes it. Returns (slide, words heard, ms heard) of this narration, None if there was none."""
        live = self.live
        if live is None:
            return None
        self.live = None
        for name in NARRATION_TIMERS:
            self._cancel_timer(name)
        await self._drop_narration_response(live)
        played = 0
        if live.audio_bytes > 0:
            played = self._played_ms(msg, live.reply, live.audio_bytes, live.first_audio_at)
            if live.audio_item_id:
                await self._rt({"type": "conversation.item.truncate", "item_id": live.audio_item_id,
                                "content_index": 0, "audio_end_ms": played})
        await self._send({"type": "reply_flush", "reply": live.reply})
        heard = heard_text(live.text, played, live.audio_bytes // BYTES_PER_MS)
        same = self.cursor is not None and (self.cursor.slide, self.cursor.chunk) == (live.slide, live.chunk)
        cursor = self.cursor if same else Cursor(live.slide, live.chunk)
        if played > 0:  # what the room heard of this slide so far: earlier parts too, when this one continued it
            so_far = " ".join(part for part in (cursor.heard, heard) if part)
            cursor = Cursor(live.slide, live.chunk, live=True, heard=_tail(so_far, HEARD_MAX_CHARS),
                            heard_ms=cursor.heard_ms + played)
        self.cursor = cursor
        self._cut_info = (cursor.heard.split()[-1], cursor.heard) if cursor.heard else None
        self._note("narration_cut", {"slide": live.slide, "reply": live.reply, "played_ms": played, "heard": heard})
        return live.slide, heard, played

    # --- tools ------------------------------------------------------------------------------------------------

    async def _tool_message(self, call_id: str, name: str, args: dict, status: str) -> None:
        await self._send({"type": "tool", "call_id": call_id, "name": name, "args": args, "status": status})

    def _register_call(self, owner: Turn | LiveNarration, call_id: Any, name: Any, arguments: Any) -> bool:
        if not isinstance(call_id, str) or not call_id or call_id in owner.calls or call_id in self._answered:
            return False
        owner.calls[call_id] = (name, arguments)
        self._note("tool_call", {"call_id": call_id, "name": name, "arguments": arguments,
                                 "response_id": owner.response_id})
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
        if name == tools.SET_PRESENTATION_STATE:
            return self._plan_presentation(call)
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

    @staticmethod
    def _deck_call(call: tools.ToolCall) -> tools.ToolCall:
        """The deck tool a set_presentation_state action stands for (start, resume, next_slide, goto_slide)."""
        action = call.args.get("action")
        args = {"slide_id": call.args["slide_id"]} if action == tools.ACTION_GOTO else {}
        return tools.ToolCall(call.call_id, tools.ACTION_TOOLS[action], args)

    def _plan_presentation(self, call: tools.ToolCall) -> tuple[bool, str]:
        """set_presentation_state, judged now: the presentation moves like Relay's deck tools; end_presentation
        hands over to the discussion (or, in the discussion, wraps up); nothing moves after the presentation."""
        action = call.args.get("action")
        phase = self.agenda_phase
        if phase == WRAPUP:
            return False, tools.error_output("not now: the meeting is wrapping up")
        if phase == DISCUSSION:
            if action == tools.ACTION_END:
                return True, tools.ok_output(note="wrapping up the meeting")
            return False, tools.error_output("not now: the presentation is over and the group is discussing")
        if action == tools.ACTION_END:
            return True, tools.ok_output(note="handing over to the discussion")
        return self._plan(self._deck_call(call))

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
        if name == tools.SET_PRESENTATION_STATE:
            if call.args.get("action") != tools.ACTION_END:
                await self._apply(self._deck_call(call))
            elif self.agenda_phase == PRESENTATION:
                await self._handover()
            elif self.agenda_phase == DISCUSSION:
                await self._enter_wrapup()
            return
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

    # --- the facilitator's tools (a meeting, contract §4–§5.5) ------------------------------------------------

    async def _settle_meeting(self, turn: Turn, *, owe: bool = False, straggler: bool = False) -> str | None:
        """At a meeting response's response.done: data calls run now and their outputs carry the results; state
        calls are validated and applied now; flow calls (set_presentation_state) are planned now and the last
        runnable one waits for the reply to drain. One output per call, in call order. Returns the follow-up the
        turn needs (a data call ran, or a state call ran and nothing was said), else None. A stalled response
        (`owe`) runs nothing but its plan, and its outputs wait for its response.done."""
        data_ran = state_ran = False
        plans: list[list[Any]] = []  # [call_id, name, args, runnable ToolCall | None, output, status | None]
        for call_id, (name, arguments) in turn.calls.items():
            if call_id in self._answered:
                continue
            parsed = tools.parse_meeting_call(call_id, name, arguments, self.talk)
            if isinstance(parsed, str):
                plans.append([call_id, str(name), tools.parse_args(arguments), None, tools.error_output(parsed),
                              "rejected"])
                continue
            kind = tools.TOOL_KINDS[parsed.name]
            if turn.kind in NO_TOOL_KINDS:
                plans.append([call_id, parsed.name, parsed.args, None, NOT_NOW_OUTPUT, "rejected"])
            elif owe and kind != tools.FLOW:
                plans.append([call_id, parsed.name, parsed.args, None, CUT_CALL_OUTPUT, "rejected"])
            elif kind == tools.DATA:
                if turn.followups >= MAX_FOLLOWUPS:
                    plans.append([call_id, parsed.name, parsed.args, None, LIMIT_OUTPUT, "rejected"])
                    continue
                output = await self._run_data(parsed, turn)
                data_ran = True
                plans.append([call_id, parsed.name, parsed.args, None, output, "applied"])
            elif kind == tools.STATE:
                ok, output = await self._run_state(parsed, turn)
                state_ran = state_ran or ok
                plans.append([call_id, parsed.name, parsed.args, None, output, "applied" if ok else "rejected"])
            else:
                runnable, output = self._plan(parsed)
                plans.append([call_id, parsed.name, parsed.args, parsed if runnable else None, output, None])
        flows = [plan[3] for plan in plans if plan[3] is not None]
        winner = flows[-1] if flows else None
        if winner is not None and turn.queued is not None:
            await self._discard_queued(f"superseded by {winner.name}")
        for call_id, name, args, call, output, status in plans:
            if status is None:
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
        if owe or straggler:
            return None
        if turn.kind == KIND_DECISION and turn.draft and not turn.decision_saved:
            # no valid save_decision in the decision response: the alignment check's draft goes in (by=auto)
            await self._save_draft(turn.draft)
            turn.decision_saved = True
            state_ran = True
        confirm = prompts.FOLLOWUP_DECISION if turn.kind == KIND_DECISION else prompts.FOLLOWUP_CONFIRM
        if data_ran:
            return prompts.FOLLOWUP_DECISION if turn.kind == KIND_DECISION else prompts.FOLLOWUP_LOOKUP
        if state_ran and turn.audio_bytes == 0 and turn.followups < MAX_FOLLOWUPS:
            return confirm
        return None

    async def _run_data(self, call: tools.ToolCall, turn: Turn) -> str:
        """A data tool, now: its output is what the follow-up answers from."""
        if call.name == tools.LOOKUP_EVIDENCE:
            query = call.args["query"]
            results = evidence.search(query, evidence.entries(self.meeting, self.mstate))
            lookup = self.mstate.add_lookup(query, results, by="model",
                                            purpose="answer" if turn.kind == KIND_ANSWER else "other")
            turn.lookups.append(lookup)
            await self._send({"type": "evidence", "lookup": _jsonable(lookup)})
            return tools.json_output(results=results)
        if call.name == tools.GET_PRESENTATION_STATE:
            return tools.json_output(**self._presentation_state())
        notes = self.mstate.notes_text()  # generate_meeting_notes
        await self._send_meeting()
        return tools.json_output(notes=notes)

    def _presentation_state(self) -> dict:
        checkpoint = self.checkpoint()
        return {"agenda_phase": self.mstate.phase,
                "agenda": [{"id": phase.id, "title": phase.title} for phase in self.meeting.phases],
                "slide": checkpoint["slide_id"], "talking_point": checkpoint["talking_point_id"],
                "status": checkpoint["status"], "slides": [slide.id for slide in self.talk.relay_slides]}

    def _known_ids(self) -> set[str]:
        """Every id a tool may cite: the documents' statements, and what was clarified and decided."""
        ids = {statement.id for statement in self.meeting.statements}
        ids |= {clarification.id for clarification in self.mstate.clarifications}
        ids |= {decision.id for decision in self.mstate.decisions}
        return ids

    def _recent_texts(self, turn: Turn) -> list[str]:
        """What was said lately: the last `window_utterances` utterances of the room, and the turn's question."""
        checks = self.meeting.checks
        window = checks.window_utterances if checks is not None else DEFAULT_WINDOW_UTTERANCES
        texts = [utterance.text for utterance in self.mstate.transcript[-max(1, window):]]
        if turn.user_text.strip():
            texts.append(turn.user_text)
        return texts

    async def _run_state(self, call: tools.ToolCall, turn: Turn) -> tuple[bool, str]:
        """A state tool, validated and applied now (contract §5.5). (applied, function_call_output)."""
        known = self._known_ids()
        if call.name == tools.RECORD_CLARIFICATION:
            ids = [sid for sid in call.args["source_ids"] if sid in known]
            dropped = [sid for sid in call.args["source_ids"] if sid not in known]
            question = call.args["question"] or turn.user_text.strip() or "(question)"
            clarification = self.mstate.add_clarification(question, call.args["answer"], ids, by="model")
            turn.clarified = True
            await self._send_meeting()
            extra = {"dropped_source_ids": dropped} if dropped else {}
            return True, tools.json_output(ok=True, id=clarification.id, **extra)
        args = dict(call.args)  # save_decision
        if turn.draft:  # what the model left out comes from the alignment check's draft (it never overrides)
            for key in ("owner", "follow_up", "source_ids"):
                if not args.get(key) and turn.draft.get(key):
                    args[key] = list(turn.draft[key]) if key == "source_ids" else str(turn.draft[key])
        for existing in self.mstate.decisions:
            if (tools.same_decision(existing.decision, args["decision"])
                    and self._meeting_time() - float(existing.at_s) <= DUPLICATE_DECISION_S):
                turn.decision_saved = True
                return True, tools.ok_output(note="already saved")
        check = tools.check_decision(args, participants=[person.name for person in self.meeting.participants],
                                     draft=turn.draft, recent=self._recent_texts(turn), known_ids=known)
        if not check.ok:
            self._note("decision_rejected", {"args": args, "why": check.error, "draft": turn.draft})
            return False, tools.error_output(check.error or "rejected")
        saved = check.args
        decision = self.mstate.add_decision(saved["decision"], saved["owner"], saved["follow_up"],
                                            saved["source_ids"], by="model")
        turn.decision_saved = True
        await self._send_meeting()
        extra = {"dropped_source_ids": list(check.dropped)} if check.dropped else {}
        return True, tools.json_output(ok=True, id=decision.id, **extra)

    async def _save_draft(self, draft: dict) -> None:
        """The alignment check's decision draft goes into the notes as it is (`by: auto`, a watchdog action)."""
        args = {"decision": str(draft.get("decision") or "").strip(), "owner": str(draft.get("owner") or ""),
                "follow_up": str(draft.get("follow_up") or ""),
                "source_ids": [sid for sid in draft.get("source_ids") or [] if isinstance(sid, str)]}
        if not args["decision"]:
            return
        if any(tools.same_decision(existing.decision, args["decision"]) for existing in self.mstate.decisions):
            return
        decision = self.mstate.add_decision(args["decision"], args["owner"], args["follow_up"], args["source_ids"],
                                            by="auto")
        call_id = self._auto_id()
        await self._tool_message(call_id, tools.SAVE_DECISION, args, "auto")
        self._note("tool", {"call_id": call_id, "name": tools.SAVE_DECISION, "args": args, "status": "auto",
                            "id": decision.id})
        await self._send_meeting()

    async def _send_followup(self, turn: Turn, kind: str) -> None:
        """The turn's next response after a tool round trip: per-response instructions say what to do with the
        result (live 2026-10-23: without them the model repeated the tool call and said nothing). Its own deck
        reply; the turn goes on."""
        if turn.assistant_text.strip():
            turn.answers.append(turn.assistant_text.strip())
            if turn.followups:
                turn.followup_answers.append(turn.assistant_text.strip())
        turn.followups += 1
        if turn.response_id is not None:  # done: a call it announces late gets the cut output, never runs
            self._ignored.add(turn.response_id)
        self._reply_seq += 1
        turn.reply = self._reply_seq
        turn.response_id = None
        turn.audio_item_id = None
        turn.response_done = False
        turn.audio_bytes = 0
        turn.first_audio_at = None
        turn.assistant_text = ""
        turn.calls = {}
        turn.expects_response = True
        metadata = {"relay": KIND_FOLLOWUP, "reply": str(turn.reply)}
        turn.expect_tag = metadata
        phase = self.mstate.phase
        response: dict[str, Any] = {
            "instructions": prompts.followup_instructions(kind, meeting=self.meeting, agenda_phase=phase,
                                                          question=turn.user_text),
            "metadata": metadata}
        if (kind != prompts.FOLLOWUP_LOOKUP or phase != PRESENTATION or turn.kind != KIND_ANSWER
                or turn.followups >= MAX_FOLLOWUPS):
            # only an answer in the presentation may call a tool (resume), and never the last follow-up: it speaks
            response["tool_choice"] = "none"
        self._cancel_timer("drain")
        self._start_timer("reply_wait", REPLY_WAIT_S, self._on_reply_wait)
        self._note("followup", {"reply": turn.reply, "kind": kind, "turn": turn.kind, "n": turn.followups})
        await self._request_events(turn, [{"type": "response.create", "response": response}])

    async def _finish_meeting_turn(self, turn: Turn) -> None:
        """A meeting turn is over (drained, cut or failed). A decision turn that saved nothing saves its draft; a
        presentation question answered from a lookup leaves a clarification behind (by=auto) when the model did not
        record one: the question, the facilitator's final words, the top sources (contract §5.3)."""
        if turn.kind == KIND_DECISION and turn.draft and not turn.decision_saved:
            turn.decision_saved = True
            await self._save_draft(turn.draft)
        if turn.kind != KIND_ANSWER or turn.phase != PRESENTATION or not turn.lookups or turn.clarified:
            return
        # the answer is what a follow-up said (it had the lookup's results), never "Let me check" before them
        answer = (turn.assistant_text.strip() if turn.followups else "") or \
            (turn.followup_answers[-1] if turn.followup_answers else "")
        if not answer:
            return
        question = turn.user_text.strip() or str(getattr(turn.lookups[0], "query", "") or "")
        ids: list[str] = []
        for lookup in turn.lookups:
            for result in getattr(lookup, "results", None) or []:
                sid = result.get("source_id") if isinstance(result, dict) else None
                if isinstance(sid, str) and sid not in ids and len(ids) < AUTO_SOURCE_IDS:
                    ids.append(sid)
        turn.clarified = True
        clarification = self.mstate.add_clarification(question, answer, ids, by="auto")
        call_id = self._auto_id()
        args = {"question": question, "answer": answer, "source_ids": ids}
        await self._tool_message(call_id, tools.RECORD_CLARIFICATION, args, "auto")
        self._note("tool", {"call_id": call_id, "name": tools.RECORD_CLARIFICATION, "args": args, "status": "auto",
                            "id": clarification.id})
        await self._send_meeting()

    # --- the discussion (a meeting, contract §5.4) --------------------------------------------------------------

    async def _handover(self) -> None:
        """The presentation is over: one sentence of hand-over (per-response instructions), then the discussion.
        Without the link the discussion starts at once."""
        self.cursor = None
        self._cut_info = None
        if not self.realtime.ready:
            await self._enter_discussion()
            return
        turn = Turn(origin=ORIGIN_HANDOVER, kind=KIND_HANDOVER, phase=PRESENTATION)
        await self._flow_request(turn, prompts.handover_instructions(self.meeting), tool_choice="none",
                                 state=REPLYING)

    async def _enter_discussion(self) -> None:
        """Phase discussion: the discussion slide, the room mic open (the deck streams every frame), the
        facilitator listening (ROOM)."""
        self.cursor = None
        self._cut_info = None
        self._last_point = None
        self.floor_human = False
        self.floor_offline = False
        self.started = True
        self._flows.clear()
        self.mstate.set_phase(DISCUSSION)
        target = self.meeting.discussion_slide
        if target:
            try:
                index = self.talk.index_of(target)
            except KeyError:
                index = None
            if index is not None and (index != self.slide_index or self.fragment is not None):
                self.slide_index = index
                self.fragment = None
                await self._send({"type": "slide", "slide": target, "fragment": None})
        await self._rt({"type": "input_audio_buffer.clear"})
        await self._resume_room(run_flows=False)
        await self._send_meeting()
        self._note("discussion", {"slide": self.current_slide.id, "prompt": prompts.discussion_prompt(self.meeting)})

    async def _resume_room(self, *, run_flows: bool = True) -> None:
        """Listen to the room again (ROOM): a fresh segmenter, the mic open, and the first few hundred ms ignored
        (half duplex: the facilitator's own last words must not be transcribed). Then whatever the alignment check
        is waiting to say."""
        self._room_seg = Segmenter()
        self._room_quiet_until = self.clock.now() + ROOM_ECHO_S
        if not self._mic_open:
            self._mic_open = True
            await self._send({"type": "mic", "open": True})
        self._set_state(ROOM, self._room_banner())
        if run_flows:
            await self._run_flows()

    async def _room_frame(self, pcm: str) -> None:
        """A room mic frame: buffered locally by the segmenter, appended to Realtime only from an utterance's start
        (with its pre-roll) to its end, then committed: transcribed, never answered (turn detection is off)."""
        if self.clock.now() < self._room_quiet_until:
            return
        if self._room_seg is None:
            self._room_seg = Segmenter()
        audio = _pcm(pcm)
        if audio is None:
            return
        for kind, data in self._room_seg.feed(audio):
            if kind in ("start", "audio") and data:
                await self._room_append(data)
            elif kind == "end":
                await self._room_commit()

    async def _room_append(self, audio: bytes) -> None:
        for start in range(0, len(audio), APPEND_MAX_BYTES):
            piece = audio[start:start + APPEND_MAX_BYTES]
            if await self._rt({"type": "input_audio_buffer.append", "audio": base64.b64encode(piece).decode()}):
                self._stream_ms += len(piece) / BYTES_PER_MS

    async def _room_commit(self) -> None:
        """Commit the utterance (its transcript comes back, no response); held while a response is still active."""
        if self._active:
            self._room_commit_held = True
            return
        if await self._rt({"type": "input_audio_buffer.commit"}):
            self._room_commits.append(self.speaker)

    async def _room_close(self, *, commit: bool) -> None:
        """Stop listening to the room for now: an utterance being spoken is committed (or dropped)."""
        seg = self._room_seg
        if seg is None:
            return
        if seg.in_speech:
            if commit:
                await self._room_commit()
            else:
                await self._rt({"type": "input_audio_buffer.clear"})
        seg.reset()

    async def _room_utterance(self, text: str, speaker: str | None, source: str) -> None:
        """One line of the discussion (a transcript, or a typed line): into the meeting state and the transcript on
        screen, through the alignment check (its events to the deck), and on to what the check asks for."""
        utterance = self.mstate.add_utterance(text, speaker, source)
        await self._send({"type": "transcript", "utterance": _jsonable(utterance)})
        try:
            events = self.engine.observe(utterance) if self.engine is not None else []
        except Exception as exc:  # noqa: BLE001 - a check that fails must not lose the line
            self._note("controller_error", {"where": "alignment.observe", "error": repr(exc),
                                            "traceback": traceback.format_exc(limit=6)})
            events = []
        for event in events:
            await self._send({"type": "alignment", "event": event})
        self._note("utterance", {"id": utterance.id, "speaker": speaker, "source": source, "text": text,
                                 "events": [event.get("type") for event in events]})
        interventions = [event for event in events if event.get("type") == "intervene"]
        drafts = [event for event in events if event.get("type") == "decision_draft"]
        resolved = [event for event in events if event.get("type") == "resolved"]
        if interventions:
            self._flows.append((KIND_INTERVENTION, interventions))
        for draft in drafts:
            self._flows.append((KIND_DECISION, {"draft": draft, "resolved": resolved, "utterance": utterance}))
        if self.agenda_phase == DISCUSSION:
            await self._run_flows()

    def _pending_conflicts(self, events: list[dict]) -> list[dict]:
        """The intervene events with a conflict that has not been resolved meanwhile (the facilitator was busy)."""
        resolved = {event.get("conflict_id") for event in self.mstate.alerts if event.get("type") == "resolved"}
        return [event for event in events
                if set(event.get("conflict_ids") or [event.get("conflict_id")]) - resolved]

    async def _run_flows(self) -> None:
        """The next thing the alignment check asks the facilitator to say, once it is free (ROOM): an intervention or
        a decision. Without the link a decision is saved silently and an intervention stays on screen only."""
        while self._flows and self.state == ROOM and self.turn is None and self.agenda_phase == DISCUSSION:
            kind, payload = self._flows.pop(0)
            if kind == KIND_INTERVENTION:
                payload = self._pending_conflicts(payload)
                if not payload:
                    continue
            if not self.realtime.ready:
                if kind == KIND_DECISION:
                    await self._save_draft(payload["draft"])
                self._note("flow_without_voice", {"kind": kind})
                continue
            if kind == KIND_INTERVENTION:
                await self._intervene(payload)
            else:
                await self._decide(payload)
            return

    async def _intervene(self, events: list[dict]) -> None:
        """The alignment check's gate fired: look up the evidence for the conflict (shown on screen), then one
        neutral question by per-response instructions, no tools (INTERVENING)."""
        conflicts = [item for event in events for item in (event, *(event.get("related") or []))]
        query = " ".join(str(event.get("query") or "") for event in events).strip()
        if not query:  # the slot label and both values (contract §5.4)
            query = " ".join(f"{item.get('label') or ''} {item.get('claimed_label') or ''} "
                             f"{item.get('established_label') or ''}" for item in conflicts)
            query = " ".join(query.split())
        results = evidence.search(query, evidence.entries(self.meeting, self.mstate))
        lookup = self.mstate.add_lookup(query, results, by="engine", purpose="intervention")
        await self._send({"type": "evidence", "lookup": _jsonable(lookup)})
        wanted = {cid for item in conflicts for cid in item.get("clarification_ids") or []}
        clarifications = [item for item in self.mstate.clarifications if item.id in wanted]
        turn = Turn(origin=ORIGIN_ROOM, kind=KIND_INTERVENTION, phase=DISCUSSION, events=list(events))
        instructions = prompts.intervention_instructions(conflicts, lookup, meeting=self.meeting,
                                                         clarifications=clarifications)
        await self._flow_request(turn, instructions, tool_choice="none", state=INTERVENING)

    async def _decide(self, payload: dict) -> None:
        """The group resolved it: the facilitator saves the decision (its save_decision is validated against the
        draft and what was said; without a valid one the draft is saved by=auto) and confirms it."""
        draft = payload["draft"]
        quotes = [(utterance.speaker, utterance.text) for utterance in self.mstate.transcript[-3:]]
        turn = Turn(origin=ORIGIN_ROOM, kind=KIND_DECISION, phase=DISCUSSION, draft=dict(draft),
                    events=list(payload.get("resolved") or []))
        instructions = prompts.decision_instructions(draft, quotes, meeting=self.meeting)
        await self._flow_request(turn, instructions, tool_choice=None, state=INTERVENING)

    async def _flow_request(self, turn: Turn, instructions: str, *, tool_choice: str | None, state: str) -> None:
        """One of the facilitator's own responses (hand-over, intervention, decision, summary): per-response
        instructions, tagged in metadata (Higgs echoes it), bound and played like an answer."""
        await self._room_close(commit=True)  # half duplex from here: an utterance being spoken is committed
        self.turn = turn
        self._reply_seq += 1
        turn.reply = self._reply_seq
        turn.expects_response = True
        turn.ended_by = "flow"
        turn.turn_end_at = self.clock.now()
        metadata = {"relay": turn.kind, "reply": str(turn.reply)}
        turn.expect_tag = metadata
        response: dict[str, Any] = {"instructions": instructions, "metadata": metadata}
        if tool_choice is not None:
            response["tool_choice"] = tool_choice
        self._start_timer("reply_wait", REPLY_WAIT_S, self._on_reply_wait)
        self._set_state(state, None)
        self._note("facilitator_request", {"kind": turn.kind, "reply": turn.reply})
        await self._request_events(turn, [{"type": "response.create", "response": response}])

    async def _enter_wrapup(self) -> None:
        """Phase wrap-up: the room mic closes, what is still unresolved becomes open questions, the notes go to the
        deck, and the facilitator sums up (two or three sentences, no tools) → IDLE "Meeting wrapped up"."""
        await self._room_close(commit=True)
        self._room_seg = None
        await self._close_mic(clear=False)
        self._flows.clear()
        self.mstate.set_phase(WRAPUP)
        if self.engine is not None:
            for event in self.engine.flush():  # conflicts still pending: silent suggestions now
                await self._send({"type": "alignment", "event": event})
        for text in self._unresolved_questions():
            self.mstate.add_open_question(text)  # (the same text twice is kept once)
        await self._send_meeting()
        self._note("wrapup", {"decisions": len(self.mstate.decisions),
                              "open_questions": len(self.mstate.open_questions)})
        if not self.realtime.ready:
            self._set_state(IDLE, BANNER_WRAPPED)
            return
        turn = Turn(origin=ORIGIN_WRAPUP, kind=KIND_SUMMARY, phase=WRAPUP)
        await self._flow_request(turn, prompts.wrapup_instructions(self.mstate.notes_text(), meeting=self.meeting),
                                 tool_choice="none", state=REPLYING)

    def _unresolved_questions(self) -> list[str]:
        """Conflicts nobody settled (pending, suggested, asked or unanswered), as open questions for the notes."""
        if self.engine is None:
            return []
        return [str(item.get("text")) for item in self.engine.unresolved() if item.get("text")]

    # --- Realtime events --------------------------------------------------------------------------------------

    async def _realtime_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        turn = self._open_reply()
        if turn is not None and self._of_response(event, turn.response_id):
            self._start_timer("reply_stall", REPLY_STALL_S, self._on_reply_stall)  # still streaming: not stalled
        live = self._open_narration()
        if live is not None and self._of_response(event, live.response_id):
            self._start_timer("narration_stall", REPLY_STALL_S, self._on_narration_stall)
        handler = self._event_handlers.get(event.get("type"))  # anything else: ignore
        if handler is not None:
            await handler(event)

    def _open_reply(self) -> Turn | None:
        """The current turn while its bound response is still streaming (bound, no response.done yet)."""
        turn = self.turn
        if self._replying and turn is not None and turn.response_id is not None and not turn.response_done:
            return turn
        return None

    def _open_narration(self) -> LiveNarration | None:
        """The live narration while its bound response is still streaming (bound, no response.done yet)."""
        live = self.live
        if self.state == NARRATING and live is not None and live.response_id is not None and not live.response_done:
            return live
        return None

    @staticmethod
    def _of_response(event: dict, response_id: str | None) -> bool:
        """A streaming event of the bound response `response_id` (one without an id counts, as in _event_turn)."""
        of, kind = event.get("response_id"), event.get("type")
        if of is not None:
            return of == response_id
        return isinstance(kind, str) and kind.startswith("response.") and kind not in ("response.created",
                                                                                     "response.done")

    def _event_narration(self, response_id: Any) -> LiveNarration | None:
        """The live narration if `response_id` is its bound response (events without an id count as current)."""
        live = self.live
        if self.state != NARRATING or live is None or live.response_id is None:
            return None
        if response_id is None:
            return live
        if response_id != live.response_id or response_id in self._ignored:
            return None
        return live

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
        if self._segmented and turn is not None and turn.segmenter is not None:
            # manual turns: the server has no turn detection (this can't come), and our segmenter ends the turn
            self._note("speech_stopped_ignored", {"reply": turn.reply, "audio_end_ms": event.get("audio_end_ms")})
            return
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
        """The user's audio became an item (our commit, or turn detection's): remember its id for the transcript. A
        room utterance's commit comes first (the server acknowledges commits in order)."""
        item_id = event.get("item_id")
        if self._room_commits:
            speaker = self._room_commits.popleft()
            if isinstance(item_id, str) and item_id:
                self._room_items[item_id] = speaker
            return
        turn = self.turn
        if turn is not None and self.state in (LISTENING, REPLYING) and turn.mic_frames > 0:
            self._remember_user_item(turn, item_id)

    async def _ev_transcription(self, event: dict) -> None:
        """The user's words, matched to their turn by item_id. The current turn gets them as its caption; a
        finished turn's late transcript only fills in its history entry (never a caption over narration or over a
        newer turn, never the newer turn's text). A room utterance's transcript goes to the meeting (and the
        alignment check)."""
        text = " ".join(str(event.get("transcript") or "").split())
        item_id = event.get("item_id")
        item_id = item_id if isinstance(item_id, str) and item_id else None
        if self.mstate is not None and (item_id in self._room_items or (
                item_id is None and self._room_items and self.state not in (LISTENING, REPLYING))):
            speaker = self._room_items.pop(item_id if item_id in self._room_items else next(iter(self._room_items)))
            if text:
                await self._room_utterance(text, speaker, "voice")
            return
        if not text:
            return
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
        tag = _narration_tag(response)
        ours = _relay_tag(response)
        self._echo = self._echo or ours is not None
        live = self.live
        if (self.state == NARRATING and live is not None and live.response_id is None and not live.pending
                and response_id is not None and response_id not in self._ignored
                and self._answers_narration(response, live)):
            live.response_id = response_id
            self._start_timer("narration_stall", REPLY_STALL_S, self._on_narration_stall)
            await self._send({"type": "reply_start", "reply": live.reply, "kind": "narration"})
            self._note("narration_start", {"reply": live.reply, "slide": live.slide, "response_id": response_id,
                                           "attempt": live.attempt})
            return
        turn = self.turn
        if (self._replying and turn is not None and turn.expects_response and turn.response_id is None
                and not turn.pending and response_id is not None and response_id not in self._ignored
                and tag is None and self._answers_turn(response, turn)):
            # (a response created while our own question is still held back can't be the answer to it, and neither
            # can a narration's)
            turn.response_id = response_id
            self._cancel_timer("reply_wait")
            self._start_timer("reply_stall", REPLY_STALL_S, self._on_reply_stall)
            await self._send({"type": "reply_start", "reply": turn.reply, "kind": turn.kind})
            self._note("reply_start", {"reply": turn.reply, "response_id": response_id, "kind": turn.kind,
                                       "followup": turn.followups})
            return
        if response_id is not None:
            self._ignored.add(response_id)
        self._note("response_rejected", {"response_id": response_id, "state": self.state,
                                         "narration": tag is not None, "tag": ours})
        if ours is None:  # a stale tagged request's own cancel went out when it was dropped: another could hit the
            await self._rt({"type": "response.cancel"})  # current one

    def _answers_turn(self, response: dict, turn: Turn) -> bool:
        """Whether a response.created answers the turn's current request. In a meeting the facilitator's own
        requests (follow-ups, interventions, …) are tagged in `metadata`: a tagged response must carry the tag of the
        current request, and an untagged one answers only an untagged request (a question), unless Realtime has never
        been seen echoing metadata. A plain talk sends no tagged answers."""
        if self.meeting is None:
            return True
        ours = _relay_tag(response)
        expected = turn.expect_tag
        if ours is not None:
            return expected is not None and ours.get("relay") == expected.get("relay") \
                and ours.get("reply") == expected.get("reply")
        return expected is None or not self._echo

    async def _ev_audio_delta(self, event: dict) -> None:
        delta = event.get("delta")
        live = self._event_narration(event.get("response_id"))
        if live is not None:
            if not live.response_done and isinstance(delta, str) and delta:
                await self._narration_audio(live, event.get("item_id"), delta)
            return
        turn = self._event_turn(event.get("response_id"))
        if turn is None or not self._replying or turn.response_done or not isinstance(delta, str):
            return
        if turn.first_audio_at is None:
            turn.first_audio_at = self.clock.now()
            turn.heard = True
            if not turn.latency_reported:  # once per turn: its first audio, whichever response brings it
                turn.latency_reported = True
                await self._report_latency(turn)
        if turn.audio_item_id is None and isinstance(event.get("item_id"), str):
            turn.audio_item_id = event["item_id"]
        turn.audio_bytes += _b64_bytes(delta)
        await self._send({"type": "reply_audio", "reply": turn.reply, "pcm": delta})

    async def _narration_audio(self, live: LiveNarration, item_id: Any, delta: str) -> None:
        if live.first_audio_at is None:  # it speaks: no retry, and later narrations don't greet
            live.first_audio_at = self.clock.now()
            self._presented = True
            self._cancel_timer("narration_wait")
        if live.audio_item_id is None and isinstance(item_id, str):
            live.audio_item_id = item_id
        live.audio_bytes += _b64_bytes(delta)
        await self._send({"type": "reply_audio", "reply": live.reply, "pcm": delta})

    async def _ev_transcript_delta(self, event: dict) -> None:
        delta = event.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        live = self._event_narration(event.get("response_id"))
        if live is not None:
            live.text += delta
            await self._send({"type": "caption", "role": "assistant", "text": live.text, "final": False,
                              "reply": live.reply})
            return
        turn = self._event_turn(event.get("response_id"))
        if turn is None or not self._replying:
            return
        turn.assistant_text += delta
        await self._send({"type": "caption", "role": "assistant", "text": turn.assistant_text, "final": False,
                          "reply": turn.reply})

    async def _ev_transcript_done(self, event: dict) -> None:
        transcript = event.get("transcript")
        final = transcript.strip() if isinstance(transcript, str) else ""
        live = self._event_narration(event.get("response_id"))
        if live is not None:
            live.text = final or live.text
            await self._send({"type": "caption", "role": "assistant", "text": live.text, "final": True,
                              "reply": live.reply})
            return
        turn = self._event_turn(event.get("response_id"))
        if turn is None or not self._replying:
            return
        turn.assistant_text = final or turn.assistant_text
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
        live = self._event_narration(response_id)
        if live is not None:
            if self._register_call(live, call_id, event.get("name"), event.get("arguments")) \
                    and live.response_done:  # a straggler after response.done: answer it now
                await self._reject_narration_calls(live)
            return
        turn = self._event_turn(response_id)
        if turn is None:
            return
        if self._register_call(turn, call_id, event.get("name"), event.get("arguments")) \
                and turn.response_done:  # a straggler after response.done: answer it now
            if self.meeting is not None:
                await self._settle_meeting(turn, straggler=True)
            else:
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
        live = self._event_narration(response_id)
        if live is not None:
            if not live.response_done:
                for item in calls:
                    self._register_call(live, item.get("call_id"), item.get("name"), item.get("arguments"))
                self._note("narration_done", {"reply": live.reply, "response_id": response_id,
                                              "status": response.get("status"), "audio_ms":
                                              live.audio_bytes // BYTES_PER_MS, "calls": list(live.calls)})
                await self._finish_narration(live)
            return
        turn = self._event_turn(response_id)
        if turn is None or turn.response_done or not self._replying:
            return
        if (response.get("status") == "cancelled" and turn.first_audio_at is None and turn.queued is None
                and turn.ended_by in ("hand", "listen_max") and self.turn_detection is not None):
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
        """No more reply audio: answer the calls, queue the action, reply_end, and arm the drain fallback. In a
        meeting a tool round trip may follow (contract §5.3): at once after a silent response, else once its audio
        has played."""
        turn.response_done = True
        self._cancel_timer("reply_wait")
        self._cancel_timer("reply_stall")
        followup = None
        if self.meeting is not None:
            followup = await self._settle_meeting(turn, owe=owe)
        else:
            await self._settle_calls(turn, owe=owe)
        if turn.reply is not None:
            await self._send({"type": "reply_end", "reply": turn.reply})
        if followup is not None and turn.audio_bytes == 0:
            await self._send_followup(turn, followup)
            return
        turn.next_followup = followup
        self._start_timer("drain", self._remaining_audio_s(turn.first_audio_at, turn.audio_bytes) + DRAIN_GRACE_S,
                          self._on_drain_timeout)

    def _remaining_audio_s(self, first_audio_at: float | None, audio_bytes: int) -> float:
        """Reply audio still to play, if it started playing at its first delta."""
        if first_audio_at is None:
            return 0.0
        audio_ms = audio_bytes / BYTES_PER_MS
        elapsed_ms = (self.clock.now() - first_audio_at) * 1000
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
        if not turn.speech_end_estimated:
            start = f"end of turn ({turn.ended_by})"
        elif turn.segmenter is not None:
            start = "end of speech (segmenter)"
        else:
            start = "end of speech (from audio_end_ms)"
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
        elif self._open_narration() is not None:  # the same for a narration still streaming
            self._start_timer("narration_stall", ERROR_GRACE_S, self._on_narration_stall)

    def _quiet_error(self, err_type: str, code: str) -> bool:
        """Errors about our own housekeeping that the presenter can't act on (logged, not shown): an output inserted
        after a call item the server no longer has, or a hand-sent turn that turn detection had just ended itself
        (the server's own commit won the race; its reply still comes)."""
        kinds = {err_type, code}
        if "invalid_previous_item_id" in kinds:
            return True
        if not self.realtime.ready and "insufficient_quota" not in kinds:
            # Session setup failed (e.g. voice validation 429): the strip shows "Realtime reconnecting…" and the
            # reason sits in the dot's tooltip and the checklist's Realtime row; a toast would only alarm the room.
            return True
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
            if self.state == IDLE and self.floor_offline:  # down again while "Realtime is back" was showing
                self._set_state(IDLE, BANNER_OFFLINE.format(presenter=self.talk.presenter))

    def _new_session(self) -> None:
        """A new Realtime session: an empty conversation that numbers its ids afresh. Forget the old session's
        response and call ids (a reused id must be neither ignored nor deduped), its owed outputs, user items and
        input-stream position. The link reads one socket at a time, so no old event can follow `ready`. A turn or a
        narration still draining from the old session keeps its state, but its audio item is gone: nothing to
        truncate."""
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
        if self.live is not None:
            self.live.audio_item_id = None
        # the room's audio items (and an utterance being appended) were the old session's
        self._room_commits.clear()
        self._room_items.clear()
        self._room_commit_held = False
        if self._room_seg is not None:
            self._room_seg.reset()

    async def _link_lost(self) -> None:
        """Mid-turn drop: LISTENING goes back to its origin; a reply without response.done counts as done. Mid
        live narration: nothing heard yet → the backup presents the slide now; else what arrived plays out and
        then the backup presents the slide again."""
        self._active.clear()
        turn, live = self.turn, self.live
        if self.state == LISTENING:
            await self._close_mic(clear=False)
            await self._send_error(ERR_LINK_LOST)
            await self._back_to_origin()
        elif self._replying and turn is not None and not turn.response_done:
            if turn.response_id is None:  # nothing was playing: drained already
                turn.response_done = True
                await self._drained("link lost")
            else:
                await self._finish_response(turn)
        elif self.state == NARRATING and live is not None and not live.response_done:
            if live.first_audio_at is None:
                await self._narration_failed("link lost")
            else:
                live.incomplete = True
                await self._finish_narration(live)

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
        if not self._replying or turn is None or turn.response_id is not None:
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
        if self._replying and self.turn is not None and self.turn.response_done:
            await self._drained("fallback")

    async def _on_countdown(self) -> None:
        if self.state == IDLE:
            await self._resume("watchdog")
            if self.state == NARRATING:
                name, args = self._shown_as(tools.RESUME_SCRIPT)
                await self._tool_message(self._auto_id(), name, args, "auto")
                self._note("tool", {"name": name, "args": args, "status": "auto"})

    async def _on_start_countdown(self) -> None:
        """"Starting…" ran out: the greeting called no tool, so the watchdog starts the presentation (D57)."""
        if self.state == IDLE:
            await self._auto(tools.START_PRESENTATION)

    async def _on_narration_wait(self) -> None:
        live = self.live
        if self.state == NARRATING and live is not None and live.first_audio_at is None:
            await self._narration_failed("timeout")

    async def _on_narration_stall(self) -> None:
        """The bound narration went quiet: no event of its own for REPLY_STALL_S (or ERROR_GRACE_S after an error)
        and no response.done. Cancel it and ignore the rest of it: what was heard plays out, then the backup
        presents the slide again; a narration that never spoke failed."""
        live = self._open_narration()
        if live is None:
            return
        self._note("narration_stalled", {"reply": live.reply, "response_id": live.response_id,
                                         "audio_ms": live.audio_bytes // BYTES_PER_MS})
        self._active.discard(live.response_id)  # a dead response must not hold back the next request
        if live.first_audio_at is None:
            await self._narration_failed("stalled")
            return
        await self._rt({"type": "response.cancel"})
        self._ignored.add(live.response_id)
        live.incomplete = True
        await self._finish_narration(live)

    async def _on_narration_drain(self) -> None:
        live = self.live
        if self.state == NARRATING and live is not None and live.response_done:
            await self._narration_drained("fallback")

    def _auto_id(self) -> str:
        self._autos += 1
        return f"auto_{self._autos}"

    async def _auto(self, name: str) -> None:
        """Do what the model should have called (the watchdog's own action): applied like a tool call, shown in the
        ticker with status "auto" so it never pretends the model called it."""
        call = tools.ToolCall(self._auto_id(), name, {})
        await self._apply(call)
        shown, args = self._shown_as(name)
        await self._tool_message(call.call_id, shown, args, "auto")
        self._note("tool", {"call_id": call.call_id, "name": shown, "args": args, "status": "auto"})

    def _shown_as(self, name: str) -> tuple[str, dict]:
        """How the ticker names a watchdog action: a meeting's model knows set_presentation_state, not Relay's deck
        tools."""
        if self.meeting is not None:
            for action, tool in tools.ACTION_TOOLS.items():
                if tool == name:
                    return tools.SET_PRESENTATION_STATE, {"action": action}
        return name, {}

    # --- instructions -----------------------------------------------------------------------------------------

    def _context(self) -> TurnContext:
        word, sentence = self._cut_info if self._cut_info else (None, None)
        if self.mstate is None:
            return TurnContext(phase=self.phase, slide_id=self.current_slide.id, cut_word=word,
                               cut_sentence=sentence, live=self._live_mode)
        return TurnContext(phase=self.phase, slide_id=self.current_slide.id, cut_word=word, cut_sentence=sentence,
                           live=self._live_mode, agenda_phase=self.mstate.phase,
                           talking_point=self.checkpoint()["talking_point_id"],
                           context=tuple(self.mstate.context_lines()))

    def _instructions(self) -> str:
        return build_instructions(self.talk, self._context(), self.meeting)

    def _turn_update(self) -> dict:
        """Per-turn session.update: instructions only, or the full config with RELAY_FULL_SESSION_UPDATE=1."""
        if self.settings.full_session_update:
            return {"type": "session.update", "session": self.session_config()}
        return {"type": "session.update", "session": {"type": "realtime", "instructions": self._instructions()}}
