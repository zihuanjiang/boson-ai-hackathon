"""A scripted deck that runs a talk's demo script (demo.yaml) the way the page's Demo Audio Controller does (contract
v3.1 §4), for tests/test_e2e.py and scripts/live_check.py. No browser: it speaks the deck protocol over the /ws socket.

`Deck` is the protocol-speaking base every scripted deck shares (contract v5 §2: the Zoom bot's `ZoomDeck` in
scripts/zoom_bot.py adds ears and a mouth to it): the socket reader, the replies and their position, the backup
narration, the handshake, `until`, and the page's bookkeeping of what is audible. Where the audio actually goes is a
hook: `DemoDeck` "plays" every reply on the wall clock, a subclass can hand it to a real player.

- **Facilitator playback**: every reply (narration, answer, intervention, …) "plays" in real time from its first
  audio; once all of it has played after `reply_end`, `reply_drained` goes out (never for a flushed reply). Like the
  page's ReplyPlayer there is one current reply (the newest): from its `reply_start` until it drains or is flushed,
  the clip frames carry it and how much of it has been heard (`reply`, `played_ms`); otherwise both are null. A
  backup narration chunk (`play`) plays for its length, counts as audible, and its position goes out as `at`.
- **Participants**: a clip step sends `speaker {name}`, then its PCM16 audio as 100 ms `mic` frames
  (`source: "clip"`, `speaker`, `clip`, `tail`, `reply`, `played_ms`, `at`), each frame when its audio starts
  playing (real time), then 1.2 s of silent frames flagged `tail: true` (the backend ends the participant's line
  there). A clip that starts cuts the previous clip's silent tail.
- **Cues** (`when`), as web/js/demo.js `cueStatus` evaluates them: every step waits for the previous one; then all
  the given cues must hold — `talking_point` + `heard_ms` (the checkpoint is that talking point, status presenting
  or resuming, and the current reply is a narration heard ≥ N ms, or with no reply the backup narration of that
  slide has played N ms while Realtime is ready), `phase` (+ `quiet_ms`: state ROOM/IDLE and the
  facilitator inaudible for N ms), `after` (+ `gap_ms` after that step ended: a clip's audio played out, an action
  sent, an unavailable clip skipped), `after_reply` (+ `gap_ms` after the first reply of that kind that drained since
  the previous step, among those with audio: a silent reply was never heard). With both `after` and `after_reply`,
  `gap_ms` counts from each. `timeout_ms` → the step is
  played anyway (the page's **Play next**, disclosed) and the result says so; `strict=True` raises instead.
- **Actions**: `start` = key `relay`, `phase` = key `phase` (the page first silences the facilitator's reply; the
  key carries the reply position taken before that).

Only the human side is scripted; everything the facilitator says and decides comes from the backend.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import time
import wave
from dataclasses import dataclass
from typing import Any, Callable

RATE = 24_000
BYTES_PER_MS = RATE * 2 // 1000
FRAME_MS = 100
FRAME_BYTES = FRAME_MS * BYTES_PER_MS
TAIL_MS = 1200  # silent frames after a clip (contract v3.1 §4)
TICK_S = 0.02  # cue evaluation step
QUIET_STATES = ("ROOM", "IDLE")  # a quiet_ms cue: the facilitator is not speaking in these states (web/js/demo.js)
PRESENTING = ("presenting", "resuming")  # a talking_point cue: the checkpoint statuses of presenting that point
ACTION_KEYS = {"start": "relay", "phase": "phase"}
FACILITATOR_KINDS = ("intervention", "decision", "summary", "handover")  # web/js/facilitator.js
LOGGED_KINDS = ("barge_in", "reply_start", "tool", "evidence", "transcript", "error")


def pcm_from_wav(body: bytes) -> bytes:
    """The PCM16 samples of a clip wav as /clips serves it (24 kHz mono 16-bit; anything else raises ValueError)."""
    with wave.open(io.BytesIO(body), "rb") as clip:
        if (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) != (1, 2, RATE):
            raise ValueError(f"not 24 kHz mono PCM16: {clip.getnchannels()} ch, {clip.getsampwidth() * 8}-bit, "
                             f"{clip.getframerate()} Hz")
        return clip.readframes(clip.getnframes())


async def fetch_clips(base_url: str, demo: dict, get: Callable[[str], Any]) -> dict[str, bytes]:
    """step id → PCM of every clip the demo payload says is available, through `get(url) -> bytes` (awaitable)."""
    clips: dict[str, bytes] = {}
    for step in demo.get("steps") or []:
        if step.get("audio"):
            clips[step["id"]] = pcm_from_wav(await get(base_url + step["audio"]))
    return clips


@dataclass
class Reply:
    number: int
    kind: str | None
    audio_bytes: int = 0
    first_audio: float | None = None
    ended: bool = False  # reply_end came
    flushed_at: float | None = None
    drained_at: float | None = None

    @property
    def audio_ms(self) -> int:
        return self.audio_bytes // BYTES_PER_MS

    def played_ms(self, now: float) -> int:
        if self.first_audio is None:
            return 0
        until = self.flushed_at if self.flushed_at is not None else now
        return max(0, min(self.audio_ms, round((until - self.first_audio) * 1000)))


@dataclass
class StepResult:
    id: str
    speaker: str | None
    action: str | None
    armed_at: float  # seconds since the run started: the previous step was done
    fired_at: float | None = None
    ended_at: float | None = None  # a clip: its audio played out; an action: when it was sent
    timed_out: bool = False  # the cue never held: played by hand (the page's Play next)
    unavailable: bool = False  # no audio for the clip (relay.clips was not run)

    def to_json(self) -> dict:
        return {key: (round(value, 3) if isinstance(value, float) else value) for key, value in self.__dict__.items()}


class Deck:
    """The deck side of one socket: what the page knows (contract v3.1 §4, v5 §2), without a browser.

    Subclasses decide where the facilitator's audio goes through four hooks: `_on_audio` (a reply's PCM arrived),
    `_played_out` (wait until the reply has been heard to its end), `_cut` (the reply stops playing here) and
    `_play_chunk` (a backup narration chunk). The defaults play everything on the wall clock, silently.
    """

    def __init__(self, ws: Any, *, talk: dict | None = None, log: Callable[[str], None] | None = None,
                 keep_audio: bool = False) -> None:
        self.ws = ws
        self.talk = talk  # /api/talk (backup narration durations)
        self.log = log
        self.keep_audio = keep_audio  # keep every reply's PCM in `audio` (the live check saves it)
        self.audio: dict[int, bytearray] = {}
        self.t0 = time.monotonic()
        self.messages: list[tuple[float, str, dict]] = []  # (t, "in"/"out", msg); no audio payloads
        self.replies: dict[int, Reply] = {}
        self.state: dict = {}  # the latest `state` message
        self.connection: str = "offline"  # the Realtime link's status (`connection` messages)
        self._new = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._chunk_player: asyncio.Task | None = None
        self._chunk: dict | None = None  # the backup narration chunk playing: play_id, slide, chunk, offset, start
        self._quiet_since: float | None = time.monotonic()  # the facilitator inaudible since (None: audible now)
        self._send_lock = asyncio.Lock()
        self._reader = asyncio.create_task(self._read())

    # --- clock and logging ------------------------------------------------------------------------------------

    def now(self) -> float:
        return time.monotonic() - self.t0

    def say(self, line: str) -> None:
        if self.log is not None:
            self.log(f"  [{self.now():6.2f}s] {line}")

    # --- the socket -------------------------------------------------------------------------------------------

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                self._react(msg)
                self._observe_quiet()
                self._new.set()
        except Exception as exc:  # noqa: BLE001 - the socket closed: the run notices through its timeouts
            self.say(f"deck socket closed ({exc!r})")

    def _react(self, msg: dict) -> None:
        kind, now = msg.get("type"), time.monotonic()
        if kind == "reply_audio":
            reply = self.replies.setdefault(msg["reply"], Reply(msg["reply"], None))
            if reply.first_audio is None:
                reply.first_audio = now
            pcm = base64.b64decode(msg["pcm"])
            reply.audio_bytes += len(pcm)
            if self.keep_audio:
                self.audio.setdefault(msg["reply"], bytearray()).extend(pcm)
            self._on_audio(reply, pcm)
            return
        self.messages.append((self.now(), "in", msg))
        if kind == "reply_start":
            self.replies.setdefault(msg["reply"], Reply(msg["reply"], msg.get("kind"))).kind = msg.get("kind")
            self._stop_chunk()  # (the page stops a backup narration for a reply)
            self._on_reply_start(self.replies[msg["reply"]])
        elif kind == "mic" and msg.get("open"):
            # the page silences everything when the backend opens the mic, unless the facilitator is speaking up
            reply = self.current_reply()
            if reply is None or reply.kind not in FACILITATOR_KINDS:
                self._stop_chunk()
                self._flush_locally(reply)
        elif kind == "reply_end":
            reply = self.replies.setdefault(msg["reply"], Reply(msg["reply"], None))
            reply.ended = True
            self._spawn(self._drain(reply))
        elif kind == "reply_flush":
            reply = self.replies.setdefault(msg["reply"], Reply(msg["reply"], None))
            if reply.flushed_at is None and reply.drained_at is None:
                reply.flushed_at = now
                self._cut(reply)
        elif kind == "state":
            self.state = msg
        elif kind == "connection":
            self.connection = str(msg.get("status") or "offline")
        elif kind == "play":  # the backup narration (a live one failed): the chunk plays out, then chunk_ended
            self._stop_chunk()
            self._flush_locally(self.current_reply())  # (nothing else sounds over narration)
            self._chunk_player = asyncio.create_task(self._play_chunk(msg))
        elif kind == "stop":
            self._stop_chunk()
        self._log_inbound(kind, msg)

    def _log_inbound(self, kind: str | None, msg: dict) -> None:
        if kind in LOGGED_KINDS or (kind == "state" and msg.get("state") is not None) or (
                kind == "alignment" and (msg.get("event") or {}).get("type") in ("intervene", "resolved")) or (
                kind == "caption" and msg.get("final")):
            brief = {k: v for k, v in msg.items() if k not in ("type", "snapshot")}
            self.say(f"<- {kind} {json.dumps(brief)[:150]}")

    async def send(self, msg: dict) -> None:
        if msg.get("type") != "mic":
            self.messages.append((self.now(), "out", msg))
        async with self._send_lock:
            await self.ws.send(json.dumps(msg))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- the player hooks -------------------------------------------------------------------------------------

    def _on_audio(self, reply: Reply, pcm: bytes) -> None:
        """A reply's audio arrived (a subclass plays it)."""

    def _on_reply_start(self, reply: Reply) -> None:
        """A reply began: the page's player resets for it (a subclass stops what an older reply still plays)."""

    async def _played_out(self, reply: Reply) -> None:
        """Wait until the reply has been heard to its end: on the wall clock from its first audio."""
        start = reply.first_audio if reply.first_audio is not None else time.monotonic()
        await asyncio.sleep(max(0.0, start + reply.audio_ms / 1000 - time.monotonic()))

    def _cut(self, reply: Reply) -> None:
        """The reply stops playing here (a subclass drops what its player still holds)."""

    async def _drain(self, reply: Reply) -> None:
        """The reply player ran empty: all of the reply was heard."""
        await self._played_out(reply)
        # (a newer reply started meanwhile: the page's player dropped this one, which never drains)
        if reply.flushed_at is None and self.current_reply() is reply:
            reply.drained_at = time.monotonic()
            self._observe_quiet()
            await self.send({"type": "reply_drained", "reply": reply.number, "played_ms": reply.audio_ms})
            self._new.set()

    def _observe_quiet(self) -> None:
        """Since when the facilitator has been inaudible, as the page's DemoController.observe keeps it."""
        if self.audible(time.monotonic()):
            self._quiet_since = None
        elif self._quiet_since is None:
            self._quiet_since = time.monotonic()

    def _stop_chunk(self) -> None:
        if self._chunk_player is not None:
            self._chunk_player.cancel()
            self._chunk_player = None
        self._chunk = None

    def chunk_duration_ms(self, slide: str | None, chunk: int | None) -> int:
        durations = {(s["id"], c["index"]): c["duration_ms"] for s in (self.talk or {}).get("slides", [])
                     for c in s["chunks"]}
        return durations.get((slide, chunk), 0)

    def _start_chunk(self, play: dict) -> dict:
        """Book-keeping of a backup chunk that starts playing now (its position rides along as `at`)."""
        offset = play.get("offset_ms") or 0
        chunk = {"play_id": play["play_id"], "slide": play.get("slide"), "chunk": play.get("chunk"), "offset": offset,
                 "start": time.monotonic()}
        self._chunk = chunk
        self._observe_quiet()
        return chunk

    def _end_chunk(self, chunk: dict) -> None:
        if self._chunk is chunk:
            self._chunk = None
            self._observe_quiet()
            self._new.set()

    async def _play_chunk(self, play: dict) -> None:
        """The backup narration player: the chunk plays from its offset to its end (on the wall clock here), then
        chunk_ended."""
        chunk = self._start_chunk(play)
        await asyncio.sleep(max(0, self.chunk_duration_ms(play.get("slide"), play.get("chunk")) - chunk["offset"])
                            / 1000)
        self._end_chunk(chunk)
        await self.send({"type": "chunk_ended", "play_id": play["play_id"]})

    def narration_position(self) -> dict | None:
        """The backup narration's position (the page's `narration.position()`, sent as `at`), or None."""
        chunk = self._chunk
        if chunk is None:
            return None
        return {"play_id": chunk["play_id"], "slide": chunk["slide"], "chunk": chunk["chunk"],
                "t_ms": max(0, round(chunk["offset"] + (time.monotonic() - chunk["start"]) * 1000))}

    async def handshake(self, timeout: float = 20.0) -> dict:
        """The page's join: hello, then arm, then the link ready. Returns the server's hello."""
        hello = await self.until(lambda m: m.get("type") == "hello", "the server's hello", 0, timeout)
        await self.send({"type": "hello", "client": "deck"})
        await self.send({"type": "arm"})
        await self.until(lambda m: m.get("type") == "connection" and m.get("status") == "ready", "Realtime ready", 0,
                         timeout)
        return hello

    async def close(self) -> None:
        for task in [self._reader, self._chunk_player, *self._tasks]:
            if task is not None:
                task.cancel()

    def mark(self) -> int:
        return len(self.messages)

    def inbound(self, since: int = 0) -> list[dict]:
        return [m for _, d, m in self.messages[since:] if d == "in"]

    async def until(self, pred: Callable[[dict], bool], what: str, since: int = 0, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        index = since
        while True:
            while index < len(self.messages):
                _, direction, msg = self.messages[index]
                index += 1
                if direction == "in" and pred(msg):
                    return msg
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                seen = [m.get("type") for m in self.inbound(since)][-30:]
                raise AssertionError(f"timed out waiting for {what}; last seen: {seen}")
            self._new.clear()
            try:
                await asyncio.wait_for(self._new.wait(), min(remaining, 0.25))
            except asyncio.TimeoutError:
                pass

    # --- what the page knows ----------------------------------------------------------------------------------

    def current_reply(self) -> Reply | None:
        """The page's current reply (its ReplyPlayer `snapshot()`): the newest reply, from its reply_start (or first
        audio) until it drains or is flushed; None otherwise — an older reply never stands in for it (a reply that
        played out is history, even while the next talking point is still being asked for)."""
        if not self.replies:
            return None
        reply = self.replies[max(self.replies)]
        if reply.flushed_at is not None or reply.drained_at is not None:
            return None
        return reply

    def playing_reply(self) -> Reply | None:
        """The current reply once its audio has started (what the room hears now)."""
        reply = self.current_reply()
        return reply if reply is not None and reply.first_audio is not None else None

    def position(self, now: float) -> dict:
        """What a clip frame or a key carries about the facilitator: the current reply and how much of it has been
        heard (nulls without one), and the backup narration's position."""
        reply = self.current_reply()
        return {"reply": reply.number if reply is not None else None,
                "played_ms": reply.played_ms(now) if reply is not None else None, "at": self.narration_position()}

    def _flush_locally(self, reply: Reply | None) -> None:
        """The page's cutReply(): the reply stops playing here (it never drains)."""
        if reply is not None and reply.flushed_at is None and reply.drained_at is None:
            reply.flushed_at = time.monotonic()
            self._cut(reply)
            self._observe_quiet()

    def silence(self) -> dict:
        """The page's silence() before a key that cuts the facilitator: stop the backup narration and flush the
        current reply; returns the position taken just before (what the key carries)."""
        where = self.position(time.monotonic())
        self._stop_chunk()
        self._flush_locally(self.current_reply())
        return where

    def audible(self, now: float) -> bool:
        """The page's relayAudible(): the backup narration plays, or the current reply plays and has audio."""
        if self._chunk is not None:
            return True
        reply = self.playing_reply()
        return reply is not None and (not reply.ended or now < reply.first_audio + reply.audio_ms / 1000)

    def quiet_ms(self, now: float) -> float:
        """How long the facilitator has been inaudible (since the run started, if it never spoke)."""
        if self.audible(now) or self._quiet_since is None:
            return 0.0
        return (now - self._quiet_since) * 1000


# --- the demo script's cues (web/js/demo.js cueStatus), on any Deck -----------------------------------------------


def cue_holds(deck: Deck, when: dict, armed: float, now: float, ended: dict[str, float]) -> bool:
    """web/js/demo.js `cueStatus` (ready or not) against what `deck` knows: `armed` = when the previous step ended
    (its prevAt), `ended` = step id → monotonic time that step ended."""
    state = deck.state
    gap_ms = when.get("gap_ms", 0)
    if "talking_point" in when:
        if state.get("talking_point") != when["talking_point"] or state.get("status") not in PRESENTING:
            return False
        reply = deck.current_reply()  # that point's narration (not the one before it)
        if reply is not None:
            if reply.kind != "narration" or reply.played_ms(now) < when.get("heard_ms", 0):
                return False
        else:  # the backup narration presenting that point's slide, while Realtime is up to hear the clip
            at = deck.narration_position()
            slide = (state.get("checkpoint") or {}).get("slide_id", state.get("slide"))
            if (at is None or deck.connection != "ready" or at["slide"] != slide
                    or at["t_ms"] < when.get("heard_ms", 0)):
                return False
    if "phase" in when:
        if state.get("agenda_phase") != when["phase"]:
            return False
        if "quiet_ms" in when and (state.get("state") not in QUIET_STATES
                                   or deck.quiet_ms(now) < when["quiet_ms"]):
            return False
    if "after" in when:
        done = ended.get(when["after"])
        if done is None or (now - done) * 1000 < gap_ms:
            return False
    if "after_reply" in when:
        # (no kind known: the page says answer; a reply with no audio, a silent tool call, was never heard and
        # never counts, as on the page: its spoken follow-up is the next reply)
        drained = [reply.drained_at for reply in deck.replies.values()
                   if (reply.kind or "answer") == when["after_reply"] and reply.drained_at is not None
                   and reply.drained_at >= armed and reply.audio_bytes > 0]
        if not drained or (now - min(drained)) * 1000 < gap_ms:
            return False
    return True


def describe_cue(when: dict) -> str:
    parts = []
    if "talking_point" in when:
        parts.append(f"the facilitator presents {when['talking_point']} ({when.get('heard_ms', 0)} ms heard)")
    if "phase" in when:
        parts.append(f"phase {when['phase']}" + (f", quiet {when['quiet_ms']} ms" if "quiet_ms" in when else ""))
    if "after" in when:
        parts.append(f"{when.get('gap_ms', 0)} ms after {when['after']}")
    if "after_reply" in when:
        parts.append(f"{when.get('gap_ms', 0)} ms after the {when['after_reply']} reply")
    return " and ".join(parts) or "the previous step"


class DemoDeck(Deck):
    """The deck side of one demo run. `clips`: step id → PCM16 mono 24 kHz samples (the very bytes /clips serves)."""

    def __init__(self, ws: Any, demo: dict, clips: dict[str, bytes], talk: dict | None = None,
                 log: Callable[[str], None] | None = None, strict: bool = False, timeout_scale: float = 1.0,
                 keep_audio: bool = False) -> None:
        super().__init__(ws, talk=talk, log=log, keep_audio=keep_audio)
        self.demo = demo
        self.clips = clips
        self.strict = strict
        self.timeout_scale = timeout_scale
        self.results: list[StepResult] = []
        self._player: asyncio.Task | None = None  # the clip being streamed (or its silent tail)
        # step id → monotonic time the step ended (a clip's audio played out, an action sent, a missing clip skipped)
        self._ended: dict[str, float] = {}

    async def close(self) -> None:
        await super().close()
        if self._player is not None:
            self._player.cancel()

    def cue_holds(self, when: dict, armed: float, now: float) -> bool:
        """web/js/demo.js `cueStatus` (ready or not): `armed` = when the previous step ended (its prevAt)."""
        return cue_holds(self, when, armed, now, self._ended)

    def describe(self, step: dict) -> str:
        return describe_cue(step.get("when") or {})

    # --- running the script -----------------------------------------------------------------------------------

    async def run(self) -> list[StepResult]:
        """Every step of the demo, in order; returns what happened to each. The last clip has played out (and its
        silent tail) when this returns."""
        for step in self.demo.get("steps") or []:
            result = StepResult(step["id"], step.get("speaker"), step.get("action"), armed_at=self.now())
            self.results.append(result)
            armed = time.monotonic()
            when = step.get("when") or {}
            timeout = when.get("timeout_ms", 90_000) / 1000 * self.timeout_scale
            self.say(f"next: {step['id']} — waits for {self.describe(step)}")
            while not self.cue_holds(when, armed, time.monotonic()):
                if time.monotonic() - armed > timeout:
                    if self.strict:
                        raise AssertionError(f"demo step {step['id']}: timed out waiting for {self.describe(step)} "
                                             f"(state {self.state.get('state')}, point "
                                             f"{self.state.get('talking_point')})")
                    result.timed_out = True
                    self.say(f"!! {step['id']}: waited {timeout:.0f} s for {self.describe(step)}: played by hand")
                    break
                self._new.clear()
                try:
                    await asyncio.wait_for(self._new.wait(), TICK_S)
                except asyncio.TimeoutError:
                    pass
            result.fired_at = self.now()
            if step.get("action"):
                key = ACTION_KEYS.get(step["action"], step["action"])
                self.say(f"-> {step['id']}: key {key} (the script's {step['action']} action)")
                where = self.position(time.monotonic())  # (taken before the page silences anything)
                if key == "phase":  # the page's phase key silences the facilitator first
                    self.silence()
                await self.send({"type": "key", "key": key, "at": where["at"], "reply": where["reply"],
                                 "reply_played_ms": where["played_ms"]})
                self._ended[step["id"]] = time.monotonic()
                result.ended_at = self.now()
                continue
            pcm = self.clips.get(step["id"])
            if not pcm:
                result.unavailable = True
                self.say(f"!! {step['id']}: no clip audio: skipped")
                self._ended[step["id"]] = time.monotonic()  # (the page's Play next skips it: it counts as ended)
                continue
            ends = await self.play_clip(step["id"], step.get("speaker"), pcm)
            await asyncio.sleep(max(0.0, ends - time.monotonic()))
            self._ended[step["id"]] = ends
            result.ended_at = ends - self.t0
            self._new.set()
        if self._player is not None:
            try:
                await self._player
            except asyncio.CancelledError:
                pass
        return self.results

    # --- the definition of done (doc/online_meeting_patch.md §8) -------------------------------------------------

    def assess(self, *, owner: str = "Bob", timely_s: float = 10.0) -> dict[str, tuple[bool, str]]:
        """name → (passed, what was seen): a genuine barge-in while the facilitator spoke, a lookup-grounded live
        answer, the presentation resumed at the interrupted talking point, a timely intervention before the
        correction, silence in the discussion until then (not on the first line), the corrected decision (owner
        `owner`) recorded after the correction and in the wrapped-up notes, and the discussion lines labelled by the
        clips' speakers."""
        entries = [(t, m) for t, d, m in self.messages if d == "in"]
        checks: dict[str, tuple[bool, str]] = {}
        steps = {step["id"]: step for step in self.demo.get("steps") or []}

        def first(pred: Callable[[dict], bool], after: float = -1.0) -> tuple[float, dict] | None:
            return next(((t, m) for t, m in entries if t >= after and pred(m)), None)

        barge = first(lambda m: m.get("type") == "barge_in" and m.get("state") == "NARRATING")
        point = None
        if barge is not None:
            point = ((steps.get(barge[1].get("clip")) or {}).get("when") or {}).get("talking_point")
            flushed = first(lambda m: m.get("type") == "reply_flush" and m.get("reply") == barge[1].get("reply"),
                            barge[0])
            checks["barge_in"] = (flushed is not None and (barge[1].get("played_ms") or 0) > 0,
                                  f"{barge[1].get('speaker')} cut the narration {barge[1].get('played_ms')} ms in"
                                  f" (point {point}); flushed: {flushed is not None}")
        else:
            checks["barge_in"] = (False, "no barge_in while the facilitator presented")
        since = barge[0] if barge else 0.0
        lookup = first(lambda m: m.get("type") == "evidence" and (m.get("lookup") or {}).get("by") == "model"
                       and (m.get("lookup") or {}).get("results"), since)
        answer = None
        if lookup is not None:
            answer = first(lambda m: m.get("type") == "caption" and m.get("role") == "assistant" and m.get("final")
                           and (self.replies.get(m.get("reply")) or Reply(0, None)).kind == "answer"
                           and bool((m.get("text") or "").strip()), lookup[0])
        top = [r.get("source_id") for r in (lookup[1]["lookup"]["results"] if lookup else [])][:3]
        checks["lookup_answer"] = (answer is not None, f"lookup {lookup[1]['lookup']['query']!r} → {top}; answer "
                                   f"{answer[1]['text']!r}" if answer else f"lookup: {lookup is not None}, no answer")
        resumed = None
        if answer is not None and point is not None:
            resumed = first(lambda m: m.get("type") == "state" and m.get("state") == "NARRATING"
                            and m.get("talking_point") == point, answer[0])
        checks["resumed"] = (resumed is not None, f"resumed at {point} ({resumed[1].get('status')})" if resumed
                             else f"no narration of {point} after the answer")
        intervention = first(lambda m: m.get("type") == "reply_start" and m.get("kind") == "intervention")
        correction = None  # when the first participant clip after the intervention started (the recorded correction)
        if intervention is not None:
            ended = [r.ended_at for r in self.results if r.ended_at is not None and r.speaker
                     and r.ended_at <= intervention[0]]
            later = [r.fired_at for r in self.results if r.speaker and r.fired_at is not None
                     and r.fired_at > intervention[0]]
            correction = min(later) if later else None
            delay = intervention[0] - max(ended) if ended else None
            ok = delay is not None and delay <= timely_s and bool(later)
            checks["intervention"] = (ok, f"{delay:.1f} s after the last line, before the correction: {bool(later)}"
                                      if delay is not None else "before any participant spoke")
        else:
            checks["intervention"] = (False, "the facilitator never spoke up")
        # Silent by default (patch §4 B.2): in the discussion the facilitator says nothing until it intervenes, and
        # not on the first line (the conflicting statement alone): only once a later line confirms it.
        discussion = first(lambda m: m.get("type") == "state" and m.get("agenda_phase") == "discussion"
                           and m.get("state") == "ROOM")
        if discussion is not None and intervention is not None:
            spoke = [m.get("kind") for t, m in entries if m.get("type") == "reply_start"
                     and discussion[0] <= t < intervention[0]]
            heard = [m["utterance"].get("speaker") for t, m in entries if m.get("type") == "transcript"
                     and t < intervention[0]]
            checks["silent"] = (not spoke and len(heard) >= 2,
                                f"before the question: {len(heard)} line(s) heard ({', '.join(map(str, heard))}), "
                                f"the facilitator said {spoke or 'nothing'}")
        else:
            checks["silent"] = (False, "no discussion" if discussion is None else "no intervention")
        decided = first(lambda m: m.get("type") == "meeting" and any(
            (d.get("owner") == owner) for d in m.get("decisions") or []))
        after_fix = decided is not None and correction is not None and decided[0] > correction
        checks["decision"] = (after_fix,
                              f"{[d.get('decision') for d in decided[1]['decisions']]}"
                              f"{'' if after_fix else ' (recorded before the correction)'}" if decided else
                              f"no decision owned by {owner}")
        notes = first(lambda m: m.get("type") == "meeting" and m.get("phase") == "wrapup" and m.get("notes"))
        in_notes = bool(notes) and any(d.get("owner") == owner for d in notes[1]["notes"].get("decisions") or [])
        checks["notes"] = (in_notes, "in the wrapped-up notes" if in_notes else "not in the notes")
        speakers = {step.get("speaker") for step in steps.values() if step.get("say")}
        lines = [m["utterance"] for _, m in entries if m.get("type") == "transcript"]
        labelled = [u for u in lines if u.get("source") == "clip" and u.get("speaker") in speakers]
        checks["labels"] = (bool(lines) and len(labelled) == len(lines),
                            "; ".join(f"{u.get('speaker')}: {u.get('text')}" for u in lines)[:300])
        return checks

    async def play_clip(self, step_id: str, speaker: str | None, pcm: bytes) -> float:
        """Start streaming a clip (in the background, in real time); returns when its audio will have played out."""
        if self._player is not None:  # the previous clip's silent tail stops here
            self._player.cancel()
            try:
                await self._player
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.send({"type": "speaker", "name": speaker})
        start = time.monotonic()
        self.say(f"-> {step_id}: {speaker} speaks ({len(pcm) // BYTES_PER_MS} ms, prerecorded)")
        self._player = asyncio.create_task(self._stream(step_id, speaker, pcm, start))
        return start + len(pcm) / BYTES_PER_MS / 1000

    async def _stream(self, step_id: str, speaker: str | None, pcm: bytes, start: float) -> None:
        audio = pcm + bytes(TAIL_MS * BYTES_PER_MS)
        for k, offset in enumerate(range(0, len(audio), FRAME_BYTES)):
            due = start + k * FRAME_MS / 1000  # each frame goes out when its audio starts playing
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            frame = audio[offset:offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\x00")
            where = self.position(time.monotonic())
            await self.send({"type": "mic", "source": "clip", "pcm": base64.b64encode(frame).decode("ascii"),
                             "speaker": speaker, "clip": step_id, "tail": offset >= len(pcm), **where})
