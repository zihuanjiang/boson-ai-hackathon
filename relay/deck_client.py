"""The deck side of the `/ws` socket without a browser: what the Zoom bot (scripts/zoom_bot.py `ZoomDeck`) builds on.

The server treats one socket as "the deck": it sends the facilitator's replies there and reads the participants'
audio from it. `Deck` keeps the page's bookkeeping of that protocol: the socket reader, the replies and how much of the
current one has been heard, the handshake (`hello`, `arm`, Realtime ready) and `until`. Where the audio actually goes
is a hook: by default every reply "plays" silently on the wall clock; `ZoomDeck` plays it into the Zoom call.

- **Replies**: every reply (narration, answer, intervention, …) plays from its first audio; once all of it has played
  after `reply_end`, `reply_drained` goes out (never for a flushed reply). There is one current reply (the newest):
  from its `reply_start` until it drains or is flushed, the participants' `mic` frames carry it and how much of it has
  been heard (`reply`, `played_ms`), so a participant who talks over Alice cuts her exactly where the room was.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

RATE = 24_000
BYTES_PER_MS = RATE * 2 // 1000
FACILITATOR_KINDS = ("intervention", "decision", "summary", "handover")  # web/js/facilitator.js
LOGGED_KINDS = ("barge_in", "reply_start", "tool", "evidence", "transcript", "error")
# the alignment events worth a line in the run's log (legacy and model-based alike: both carry issue_type and topic)
ALIGNMENT_SHOWN = ("intervene", "suggestion", "resolved", "withdrawn", "decision_draft")


@dataclass
class Reply:
    number: int
    kind: str | None
    audio_bytes: int = 0
    first_audio: float | None = None
    ended: bool = False  # reply_end came
    flushed_at: float | None = None
    drained_at: float | None = None
    speed: float = 1.0  # the playback rate it plays at (the rate when it started)

    @property
    def audio_ms(self) -> int:
        return self.audio_bytes // BYTES_PER_MS

    def played_ms(self, now: float) -> int:
        """Audio ms heard (of the reply's own audio: at `speed`, that much per wall-clock ms)."""
        if self.first_audio is None:
            return 0
        until = self.flushed_at if self.flushed_at is not None else now
        return max(0, min(self.audio_ms, round((until - self.first_audio) * 1000 * self.speed)))


class Deck:
    """The deck side of one socket: what the page knows, without a browser.

    Subclasses decide where the facilitator's audio goes through four hooks: `_on_audio` (a reply's PCM arrived),
    `_on_reply_start` (a new reply), `_played_out` (wait until the reply has been heard to its end) and `_cut` (the
    reply stops playing here). The defaults play everything on the wall clock, silently.
    """

    def __init__(self, ws: Any, *, talk: dict | None = None, log: Callable[[str], None] | None = None,
                 keep_audio: bool = False) -> None:
        self.ws = ws
        self.talk = talk  # /api/talk
        self.log = log
        self.keep_audio = keep_audio  # keep every reply's PCM in `audio`
        self.audio: dict[int, bytearray] = {}
        self.t0 = time.monotonic()
        self.messages: list[tuple[float, str, dict]] = []  # (t, "in"/"out", msg); no audio payloads
        self.replies: dict[int, Reply] = {}
        self.state: dict = {}  # the latest `state` message
        self.connection: str = "offline"  # the Realtime link's status (`connection` messages)
        self.speed = 1.0  # Alice's playback rate (`speed` messages): every reply from now on plays at it
        self._new = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
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
                self._react(json.loads(raw))
                self._new.set()
        except Exception as exc:  # noqa: BLE001 - the socket closed: the run notices through its timeouts
            self.say(f"deck socket closed ({exc!r})")

    def _react(self, msg: dict) -> None:
        kind, now = msg.get("type"), time.monotonic()
        if kind == "reply_audio":
            reply = self.replies.setdefault(msg["reply"], Reply(msg["reply"], None, speed=self.speed))
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
            self.replies.setdefault(msg["reply"], Reply(msg["reply"], msg.get("kind"), speed=self.speed)).kind = \
                msg.get("kind")
            self._on_reply_start(self.replies[msg["reply"]])
        elif kind == "mic" and msg.get("open"):
            # the page silences the facilitator when the backend opens the floor, unless she is speaking up
            reply = self.current_reply()
            if reply is None or reply.kind not in FACILITATOR_KINDS:
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
        elif kind == "speed" and isinstance(msg.get("rate"), (int, float)):
            self.speed = float(msg["rate"])
            self._on_speed(self.speed)
        self._log_inbound(kind, msg)

    def _log_inbound(self, kind: str | None, msg: dict) -> None:
        if kind in LOGGED_KINDS or (kind == "state" and msg.get("state") is not None) or (
                kind == "alignment" and (msg.get("event") or {}).get("type") in ALIGNMENT_SHOWN) or (
                kind == "caption" and msg.get("final")):
            brief = {k: v for k, v in msg.items() if k not in ("type", "snapshot")}
            if kind == "alignment":  # one line per issue: its kind, type and topic (legacy: the slot label), no payload
                event = msg.get("event") or {}
                brief = {key: event.get(key) for key in ("type", "id", "issue_id", "issue_type", "topic", "status",
                                                        "decision", "owner", "silent") if event.get(key) is not None}
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

    def _on_speed(self, rate: float) -> None:
        """The playback rate changed (a subclass tells its player): the next reply plays at it."""

    async def _played_out(self, reply: Reply) -> None:
        """Wait until the reply has been heard to its end: on the wall clock from its first audio."""
        start = reply.first_audio if reply.first_audio is not None else time.monotonic()
        await asyncio.sleep(max(0.0, start + reply.audio_ms / 1000 / reply.speed - time.monotonic()))

    def _cut(self, reply: Reply) -> None:
        """The reply stops playing here (a subclass drops what its player still holds)."""

    async def _drain(self, reply: Reply) -> None:
        """The reply player ran empty: all of the reply was heard."""
        await self._played_out(reply)
        # (a newer reply started meanwhile: the page's player dropped this one, which never drains)
        if reply.flushed_at is None and self.current_reply() is reply:
            reply.drained_at = time.monotonic()
            await self.send({"type": "reply_drained", "reply": reply.number, "played_ms": reply.audio_ms})
            self._new.set()

    async def handshake(self, timeout: float = 20.0) -> dict:
        """The page's join: hello, then arm, then the link ready. Returns the server's hello."""
        hello = await self.until(lambda m: m.get("type") == "hello", "the server's hello", 0, timeout)
        await self.send({"type": "hello", "client": "deck"})
        await self.send({"type": "arm"})
        await self.until(lambda m: m.get("type") == "connection" and m.get("status") == "ready", "Realtime ready", 0,
                         timeout)
        return hello

    async def close(self) -> None:
        for task in [self._reader, *self._tasks]:
            task.cancel()

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
        """The page's current reply: the newest reply, from its reply_start (or first audio) until it drains or is
        flushed; None otherwise — an older reply never stands in for it."""
        if not self.replies:
            return None
        reply = self.replies[max(self.replies)]
        if reply.flushed_at is not None or reply.drained_at is not None:
            return None
        return reply

    def position(self, now: float) -> dict:
        """What a mic frame or a key carries about the facilitator: the current reply and how much of it has been
        heard (nulls without one)."""
        reply = self.current_reply()
        return {"reply": reply.number if reply is not None else None,
                "played_ms": reply.played_ms(now) if reply is not None else None, "at": None}

    def _flush_locally(self, reply: Reply | None) -> None:
        """The page's cutReply(): the reply stops playing here (it never drains)."""
        if reply is not None and reply.flushed_at is None and reply.drained_at is None:
            reply.flushed_at = time.monotonic()
            self._cut(reply)
