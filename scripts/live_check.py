"""Live end to end without a browser: real server + real Higgs Realtime + a scripted deck whose "microphone" plays
lines synthesized with TTS 3 (other voices than Relay's). Exercises live narration (D52: Higgs presents every slide,
the deck cuts in during a narration, and the slide continues live), turn detection, input transcription, the model's
tool calls with the real prompts, reply audio and latency. Runs on any talk; needs the key in .env and real renders
(the backup narration).

    uv run python scripts/live_check.py [--talk talks/test] [--narration live|rendered] [--port N]
    uv run python scripts/live_check.py --talk talks/relay-intro
    uv run python scripts/live_check.py --talk talks/rollout-review      # the meeting (On the Same Page)
    RELAY_TURN_DETECTION='{"type": "semantic_vad", "silence_duration_ms": 500}' uv run python scripts/live_check.py

The flow: the opening ("take it away") → the first relay slide's narration → a comment cut-in during it → the slide
continues live → a plain-language cut-in during that → the narrations run on to the Q&A slide (the deck plays each
one out in real time) → a question → the hand-back. The narration mode is the server's (`--narration`, else
RELAY_NARRATION), read from its hello; in rendered mode the cut-ins land on word positions of the renders instead.

Writes out/live/: the synthesized lines (*.pcm), everything Relay streamed (reply_<n>.wav: answers and live
narrations), frames.jsonl, summary.json. Each turn reports whether the model called the expected tool or the
watchdog did it ("auto"); each live narration its slide, length and what Relay said.

A meeting package with a demo script (`--talk talks/rollout-review`: agenda.yaml + demo.yaml, input clips — the online
meeting of doc/online_meeting_patch.md) runs **the demo as the page would**: first the participant clips are generated
with real TTS 3 (`relay.clips`, cached; each speaker's voice from demo.yaml), then the server starts on them, and
`relay.demo_driver` plays the page's Demo Audio Controller over /ws — the facilitator's replies play out in real time,
each clip streams as 100 ms `source: "clip"` frames in real time when its cue holds (the same cues, from the deck
messages it receives), the microphone is never used. It then checks the patch's §8 definition of done: a genuine
barge-in while the facilitator spoke (Alice's clip cut the narration, no tap), a live answer grounded in a lookup, the
presentation resumed at the interrupted talking point, a timely intervention before the correction, and the corrected
decision with owner Bob in the decisions and the wrapped-up notes (plus the discussion lines labelled by the clips).

    uv run python scripts/live_check.py --talk talks/rollout-review               # the online meeting (clips)
    uv run python scripts/live_check.py --talk talks/rollout-review --input mic   # the in-room meeting (below)

With `--input mic` (or a meeting without demo.yaml) the meeting runs after its run of show with Space and the room
mic: Alice's opening → the talking points → her question during the rollback plan (lookup_evidence → the answer →
resume at that talking point, the clarification) → the hand-over → the discussion spoken into the room mic by three
synthesized voices (Alice, Bob, Carol: transcripts, the alignment events, the facilitator's intervention at Carol's
confirmation, the decision after her correction) → Alice asks for the wrap-up (else the phase button, said so in the
report) → the summary. Turns end by the backend's segmenter (manual turns). The report lists what the room heard,
which tools the model called, the evidence, the notes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import requests
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # `uv run python scripts/live_check.py` from anywhere
from relay.config import ALIGNMENT_MODES, INPUT_MODES, NARRATION_MODES, ROOT, input_mode, load_settings  # noqa: E402
from relay.demo_driver import DemoDeck, fetch_clips  # noqa: E402

OUT = ROOT / "out" / "live"
RATE = 24000
BYTES_PER_MS = RATE * 2 // 1000
FRAME = RATE // 10 * 2  # 100 ms of PCM16 mono
CUT_AFTER_S = 3.0  # a live cut-in lands this far into the narration the room is hearing
TURN_TIMEOUT_S = 40.0
RUN_TO_QA_S = 600.0  # every remaining slide presented live and played out in real time

LINES = {  # key: (voice, text)
    "opening": ("jake", "So I built a co-presenter. Relay, take it away."),
    "comment": ("jake", "Quick comment: that was literally my last demo day."),
    "plain": ("nora", "Sorry, can you explain that like I'm not an engineer?"),
    "question": ("nora", "How do you know where to pick up after someone interrupts you?"),
}
# The meeting (talks/rollout-review/run_of_show.md, "Lines for the attendees"): key → (voice, speaker, text)
MEETING_LINES = {
    "opening": ("eleanor", "Alice", "Hi everyone, this is our release four point three rollout review. Facilitator, "
                                    "please take us through the plan."),
    "question": ("eleanor", "Alice", "Wait, I thought Friday was production. Which one is it?"),
    "alice": ("eleanor", "Alice", "Production is next Wednesday, right? Bob, what goes out on Friday?"),
    "bob": ("jake", "Bob", "I'll draft the announcement for Friday's production launch to all customers."),
    "carol_confirm": ("nora", "Carol", "Sounds good. Let's finalize that."),
    "carol_fix": ("nora", "Carol", "Yes, beta users only. Bob owns the announcement."),
    "wrapup": ("eleanor", "Alice", "Thanks, everyone. Facilitator, please wrap us up."),
}
INTERRUPT_AT = "rollback-trigger"  # the run of show's cue: Alice asks as the rollback plan starts
ROOM_PAUSE_S = 1.0  # a beat before each discussion line (the facilitator's echo window is 0.3 s)
ROOM_ANALYSIS_S = 6.0  # claude mode: the Claude analysis of a line (contract v5) takes a few seconds; legacy: instant
PRINTED = ("tool", "state", "cut", "play", "slide", "reply_start", "metric", "error", "connection", "evidence",
           "transcript", "alignment")


def tts_pcm(settings, voice: str, text: str) -> bytes:
    path = OUT / f"{voice}-{hashlib.sha256(text.encode()).hexdigest()[:10]}.pcm"
    if path.exists():
        return path.read_bytes()
    resp = requests.post(f"{settings.base_url}/v1/audio/speech", headers=settings.headers, timeout=120,
                         json={"model": "higgs-tts-3", "input": text, "voice": voice, "response_format": "pcm"})
    resp.raise_for_status()
    path.write_bytes(resp.content)
    return resp.content


def synthesize(settings, talk: dict) -> dict[str, bytes]:
    """The mic lines for this talk: the meeting's run of show (three voices) for a meeting package, else Relay's."""
    if talk.get("meeting"):
        return {key: tts_pcm(settings, voice, text) for key, (voice, _, text) in MEETING_LINES.items()}
    return {key: tts_pcm(settings, voice, text) for key, (voice, text) in LINES.items()}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


class Deck:
    """The browser's side of the deck protocol, scripted. Like the reply player, it plays every reply (answers and
    live narrations) out in real time from its first audio and then sends reply_drained, never for a reply that was
    flushed. With `auto_play` it also plays rendered chunks (the backup) for their duration and sends chunk_ended."""

    def __init__(self, ws, talk: dict) -> None:
        self.ws = ws
        self.durations = {(slide["id"], chunk["index"]): chunk["duration_ms"]
                          for slide in talk["slides"] for chunk in slide["chunks"]}
        self.t0 = time.monotonic()
        self.log: list[tuple[float, str, dict]] = []
        self.audio: dict[int, bytearray] = {}
        self.first_audio: dict[int, float] = {}  # reply → when its first audio arrived (it starts playing then)
        self.ended: set[int] = set()  # replies whose reply_end came
        self.flushed: set[int] = set()
        self.narrations: dict[int, dict] = {}  # live narration reply → {reply, slide, point, said}
        self.backup_plays = 0  # rendered chunks played while the server narrates live (the backup stepped in)
        self.slide: str | None = None
        self.state: str | None = None
        self.point: str | None = None  # a meeting's talking point (the state message's `talking_point`)
        self.mic_open = False
        self.auto_play = False
        self._player: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._new = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    def now(self) -> float:
        return time.monotonic() - self.t0

    async def _read(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "reply_audio":
                self.first_audio.setdefault(msg["reply"], time.monotonic())
                self.audio.setdefault(msg["reply"], bytearray()).extend(base64.b64decode(msg["pcm"]))
                continue
            self.log.append((self.now(), "in", msg))
            self._new.set()
            self._react(msg)
            if kind in PRINTED or (kind == "caption" and msg.get("final")):
                brief = {k: v for k, v in msg.items() if k != "type"}
                print(f"  [{self.now():6.2f}s] <- {kind} {json.dumps(brief)[:160]}")

    def _react(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "mic":
            self.mic_open = bool(msg.get("open"))
        elif kind == "slide":
            self.slide = msg["slide"]
        elif kind == "state":
            self.state, self.point = msg.get("state"), msg.get("talking_point")
        elif kind == "reply_start" and msg.get("kind") == "narration":
            self.narrations[msg["reply"]] = {"reply": msg["reply"], "slide": self.slide, "point": self.point,
                                             "said": ""}
        elif kind == "reply_end":
            self.ended.add(msg["reply"])
            self._spawn(self._drain(msg["reply"]))
        elif kind == "reply_flush":
            self.flushed.add(msg["reply"])
        elif kind == "caption" and msg.get("final") and msg.get("reply") in self.narrations:
            self.narrations[msg["reply"]]["said"] = msg.get("text") or ""
        elif kind == "play":
            self.slide = msg["slide"]
            self._stop_player()
            if self.auto_play:
                self.backup_plays += 1
                self._player = asyncio.create_task(self._play_chunk(msg))
        elif kind == "stop":
            self._stop_player()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _stop_player(self) -> None:
        if self._player is not None:
            self._player.cancel()
            self._player = None

    async def _play_chunk(self, play: dict) -> None:
        """The narration player: the chunk plays from its offset to its end, then chunk_ended."""
        duration = self.durations.get((play["slide"], play["chunk"]), 0)
        await asyncio.sleep(max(0, duration - play.get("offset_ms", 0)) / 1000)
        await self.send({"type": "chunk_ended", "play_id": play["play_id"]})

    async def _drain(self, reply: int) -> None:
        """The reply player: the reply plays in real time from its first audio; once all of it has played,
        reply_drained (a flushed reply never drains)."""
        audio_ms = len(self.audio.get(reply, b"")) // BYTES_PER_MS
        ends = self.first_audio.get(reply, time.monotonic()) + audio_ms / 1000
        await asyncio.sleep(max(0.0, ends - time.monotonic()) + 0.1)
        if reply not in self.flushed:
            await self.send({"type": "reply_drained", "reply": reply, "played_ms": audio_ms})

    def played_ms(self, reply: int) -> int:
        """How much of `reply` the room has heard by now: the wall clock since its first audio, at most what came."""
        start = self.first_audio.get(reply)
        if start is None:
            return 0
        return min(len(self.audio.get(reply, b"")) // BYTES_PER_MS, round((time.monotonic() - start) * 1000))

    async def into(self, reply: int, seconds: float, timeout: float = 30.0) -> None:
        """Wait until `seconds` of `reply` have played (or half of it, if all of it is shorter)."""
        deadline = time.monotonic() + timeout
        while True:
            played = self.played_ms(reply)
            audio_ms = len(self.audio.get(reply, b"")) // BYTES_PER_MS
            if played >= seconds * 1000 or (reply in self.ended and played >= audio_ms / 2):
                return
            if time.monotonic() > deadline:
                raise AssertionError(f"reply {reply} never played {seconds:g} s")
            await asyncio.sleep(0.05)

    async def send(self, msg: dict) -> None:
        self.log.append((self.now(), "out", msg))
        await self.ws.send(json.dumps(msg))

    async def key(self, key: str, at: dict | None = None, *, reply: int | None = None,
                  played: int | None = None) -> None:
        """A presenter key, with what the deck plays (as the browser sends it). A reply it cuts is flushed here
        first, as the browser's reply player does."""
        if reply is not None:
            self.flushed.add(reply)
        print(f"  [{self.now():6.2f}s] -> key {key} {json.dumps(at) if at else ''}"
              f"{f' reply {reply} played {played} ms' if reply is not None else ''}")
        await self.send({"type": "key", "key": key, "at": at, "reply": reply, "reply_played_ms": played})

    async def speak(self, pcm: bytes, trailing_silence_s: float = 2.5) -> None:
        """Stream `pcm` as 100 ms mic frames in real time, then silence, while the backend keeps the mic open."""
        audio = pcm + b"\x00\x00" * int(RATE * trailing_silence_s)
        for i in range(0, len(audio), FRAME):
            if not self.mic_open:
                break
            frame = audio[i:i + FRAME].ljust(FRAME, b"\x00")
            await self.ws.send(json.dumps({"type": "mic", "pcm": base64.b64encode(frame).decode()}))
            await asyncio.sleep(0.1)

    def mark(self) -> int:
        return len(self.log)

    async def until(self, pred, what: str, since: int, timeout: float = 25.0) -> dict:
        deadline = time.monotonic() + timeout
        index = since
        while True:
            while index < len(self.log):
                _, direction, msg = self.log[index]
                index += 1
                if direction == "in" and pred(msg):
                    print(f"  [{self.now():6.2f}s] ok: {what}")
                    return msg
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for {what}")
            self._new.clear()
            try:
                await asyncio.wait_for(self._new.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    def narration_report(self) -> list[dict]:
        return [{**entry, "audio_ms": len(self.audio.get(reply, b"")) // BYTES_PER_MS,
                 "cut": reply in self.flushed} for reply, entry in self.narrations.items()]


def answer_start(m: dict) -> bool:
    return m.get("type") == "reply_start" and m.get("kind") != "narration"


def narration_start(after: int = 0):
    """A live narration starting, newer than reply `after`."""
    return lambda m: m.get("type") == "reply_start" and m.get("kind") == "narration" and m["reply"] > after


def qa_wait(m: dict) -> bool:
    return m.get("type") == "state" and m.get("state") == "IDLE" and m.get("phase") == "qa"


async def turn(deck: Deck, line: bytes, expect_tool: str, label: str, *, at: dict | None = None,
               reply: int | None = None, played: int | None = None) -> dict:
    """One spoken cut-in: Space (with where the deck is), the line into the open mic, Relay's answer, and the tool
    that ends the turn (the model's, or the watchdog's "auto")."""
    mark = deck.mark()
    await deck.key("space", at, reply=reply, played=played)
    await deck.until(lambda m: m.get("type") == "mic" and m.get("open"), f"{label}: mic open", mark, 10)
    speaking = asyncio.create_task(deck.speak(line))
    start = await deck.until(answer_start, f"{label}: reply started", mark, 30)
    await speaking
    try:
        await deck.until(lambda m: m.get("type") == "tool" and m.get("status") in ("applied", "rejected", "auto"),
                         f"{label}: a tool ran", mark, TURN_TIMEOUT_S)
    except AssertionError as exc:
        print(f"  !! {label}: {exc}")
    inbound = [m for _, d, m in deck.log[mark:] if d == "in"]
    got = [m for m in inbound if m.get("type") == "tool"]
    user = [m["text"] for m in inbound if m.get("type") == "caption" and m.get("role") == "user"]
    said = [m["text"] for m in inbound if m.get("type") == "caption" and m.get("role") == "assistant"
            and m.get("final") and m.get("reply") == start["reply"]]
    latency = [m["value"] for m in inbound if m.get("type") == "metric"]
    by_model = any(m.get("name") == expect_tool and m.get("status") == "applied" for m in got)
    by_watchdog = any(m.get("name") == expect_tool and m.get("status") == "auto" for m in got)
    return {"label": label, "expected": expect_tool, "ok": by_model or by_watchdog, "reply": start["reply"],
            "by": "model" if by_model else ("watchdog" if by_watchdog else "-"),
            "tools": [(m["name"], m["status"]) for m in got], "heard": user[-1:], "said": said[-1:],
            "latency_ms": latency, "cut_at_ms": played}


async def run_live(deck: Deck, lines: dict[str, bytes]) -> list[dict]:
    """D52: every slide is presented live; the cut-ins land during a narration, and the slide continues live."""
    deck.auto_play = True  # should the backup step in (a narration that failed), it plays out by itself
    results = []
    print("1. opening")
    results.append(await turn(deck, lines["opening"], "start_presentation", "opening"))
    current = await deck.until(narration_start(), "the first slide's live narration", 0, 30)
    slide = deck.narrations[current["reply"]]["slide"]
    for step, key in ((2, "comment"), (3, "plain")):
        await deck.into(current["reply"], CUT_AFTER_S)
        played = deck.played_ms(current["reply"])
        print(f"{step}. {key} cut-in, {played} ms into the narration of {slide}")
        result = await turn(deck, lines[key], "resume_script", key, reply=current["reply"], played=played)
        results.append(result)
        current = await deck.until(narration_start(after=result["reply"]), f"{slide} continues live", 0, 30)
        if deck.narrations[current["reply"]]["slide"] != slide:
            print(f"  !! the narration went on with {deck.narrations[current['reply']]['slide']}, not {slide}")
    print("4. the narrations run on to the Q&A slide, each played out in real time")
    await deck.until(qa_wait, "waiting for a question", deck.mark(), RUN_TO_QA_S)
    print("5. Q&A question")
    results.append(await turn(deck, lines["question"], "handoff_to_human", "question"))
    return results


async def run_rendered(deck: Deck, talk: dict, lines: dict[str, bytes]) -> list[dict]:
    """v1: the renders narrate; the cut-ins land on word positions and resume at the cut sentence."""
    first = next(slide for slide in talk["slides"] if slide["speaker"] == "relay")
    results = []
    print("1. opening")
    results.append(await turn(deck, lines["opening"], "start_presentation", "opening"))
    play = await deck.until(lambda m: m.get("type") == "play" and m.get("slide") == first["id"],
                            f"narration of {first['id']}", 0, 20)

    t_ms = min(2500, first["chunks"][0]["duration_ms"] * 2 // 5)
    print(f"2. comment cut-in, mid-sentence on {first['id']}/0 at {t_ms} ms")
    await asyncio.sleep(t_ms / 1000)
    at = {"play_id": play["play_id"], "slide": first["id"], "chunk": 0, "t_ms": t_ms}
    results.append(await turn(deck, lines["comment"], "resume_script", "comment", at=at))
    resumed = await deck.until(lambda m: m.get("type") == "play" and m.get("resume_word") is not None,
                               "resumed with a jump back", 0, 20)

    print(f"3. plain-language cut-in on {resumed['slide']}/{resumed['chunk']} again")
    await asyncio.sleep(1.5)
    at = {"play_id": resumed["play_id"], "slide": resumed["slide"], "chunk": resumed["chunk"],
          "t_ms": resumed["offset_ms"] + 1200}
    results.append(await turn(deck, lines["plain"], "resume_script", "plain", at=at))
    current = await deck.until(lambda m: m.get("type") == "play" and m.get("resume_word") is not None
                               and m.get("play_id", 0) > resumed["play_id"], "resumed again", 0, 20)

    print("4. narration runs to the Q&A slide")
    while True:
        mark = deck.mark()
        await deck.send({"type": "chunk_ended", "play_id": current["play_id"]})
        nxt = await deck.until(lambda m: (m.get("type") == "play") or
                               (m.get("type") == "state" and m.get("state") == "IDLE"), "next", mark, 10)
        if nxt.get("type") != "play":
            break
        current = nxt

    print("5. Q&A question")
    results.append(await turn(deck, lines["question"], "handoff_to_human", "question"))
    return results


# --- a meeting (On the Same Page) -------------------------------------------------------------------------------


def inbound(deck: Deck, since: int) -> list[dict]:
    return [m for _, d, m in deck.log[since:] if d == "in"]


def answer_texts(deck: Deck, messages: list[dict]) -> list[str]:
    """The facilitator's final words in `messages`, narrations left out."""
    return [m["text"] for m in messages if m.get("type") == "caption" and m.get("role") == "assistant"
            and m.get("final") and m.get("reply") not in deck.narrations]


async def meeting_turn(deck: Deck, line: bytes, label: str, expect: str, *, reply: int | None = None,
                       played: int | None = None) -> dict:
    """One spoken turn to the facilitator: Space, the line into the open mic (the backend's segmenter ends the turn
    after its silence), the answer and its follow-ups, and the tool that ends it (the model's, or "auto")."""
    mark = deck.mark()
    await deck.key("space", reply=reply, played=played)
    await deck.until(lambda m: m.get("type") == "mic" and m.get("open"), f"{label}: mic open", mark, 10)
    speaking = asyncio.create_task(deck.speak(line))
    await deck.until(answer_start, f"{label}: reply started", mark, 30)
    await speaking
    try:
        await deck.until(lambda m: m.get("type") == "tool" and m.get("name") == expect
                         and m.get("status") in ("applied", "auto"), f"{label}: {expect}", mark, TURN_TIMEOUT_S)
    except AssertionError as exc:
        print(f"  !! {label}: {exc}")
    await asyncio.sleep(0.5)
    got = inbound(deck, mark)
    tools_seen = [(m["name"], m["status"]) for m in got if m.get("type") == "tool"]
    by_model = (expect, "applied") in tools_seen
    by_watchdog = (expect, "auto") in tools_seen
    return {"label": label, "expected": expect, "ok": by_model or by_watchdog,
            "by": "model" if by_model else ("watchdog" if by_watchdog else "-"), "tools": tools_seen,
            "heard": [m["text"] for m in got if m.get("type") == "caption" and m.get("role") == "user"][-1:],
            "said": answer_texts(deck, got),
            "evidence": [{"query": m["lookup"]["query"], "by": m["lookup"]["by"],
                          "top": [r["source_id"] for r in m["lookup"]["results"]]} for m in got
                         if m.get("type") == "evidence"],
            "latency_ms": [m["value"] for m in got if m.get("type") == "metric"], "cut_at_ms": played}


async def wait_state(deck: Deck, state: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while deck.state != state:
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for state {state} (now {deck.state})")
        await asyncio.sleep(0.05)


async def room_line(deck: Deck, line: bytes, speaker: str, label: str, expected: str) -> dict:
    """One line of the discussion into the open room mic (never while the facilitator speaks: half duplex), with
    the speaker selected; what the room heard (the transcript) and the alignment events it caused."""
    await wait_state(deck, "ROOM", 60)
    await asyncio.sleep(ROOM_PAUSE_S)
    mark = deck.mark()
    print(f"  -- {speaker}: {expected}")
    await deck.send({"type": "speaker", "name": speaker})
    await deck.speak(line, trailing_silence_s=1.5)
    heard = None
    try:
        transcript = await deck.until(lambda m: m.get("type") == "transcript", f"{label}: transcribed", mark, 15)
        heard = transcript["utterance"]["text"]
    except AssertionError as exc:
        print(f"  !! {label}: {exc}")
    await asyncio.sleep(ROOM_ANALYSIS_S)  # claude mode: the analysis of the line comes back a few seconds later
    events = [f"{m['event']['type']}:{m['event'].get('issue_type') or '-'}" for m in inbound(deck, mark)
              if m.get("type") == "alignment"]  # type:issue_type (legacy events say evidence_conflict too)
    return {"label": label, "speaker": speaker, "expected": expected, "heard": heard, "events": events}


async def run_meeting(deck: Deck, lines: dict[str, bytes]) -> dict:
    """The run of show of talks/rollout-review, live: see the module docstring."""
    deck.auto_play = True  # should the backup step in (a narration that failed), it plays out by itself
    out: dict = {"turns": [], "room": [], "phase_button": False}
    print("1. opening (Alice)")
    out["turns"].append(await meeting_turn(deck, lines["opening"], "opening", "set_presentation_state"))
    print(f"2. the talking points, up to {INTERRUPT_AT}")
    current = await deck.until(lambda m: narration_start()(m) and deck.narrations.get(m["reply"], {}).get("point")
                               == INTERRUPT_AT, f"the narration of {INTERRUPT_AT}", 0, RUN_TO_QA_S)
    await deck.into(current["reply"], CUT_AFTER_S)
    played = deck.played_ms(current["reply"])
    print(f"3. Alice's question, {played} ms into {INTERRUPT_AT}")
    mark = deck.mark()
    question = await meeting_turn(deck, lines["question"], "question", "set_presentation_state",
                                  reply=current["reply"], played=played)
    question["lookup_by_model"] = ("lookup_evidence", "applied") in question["tools"]
    out["turns"].append(question)
    resumed = await deck.until(narration_start(after=current["reply"]), "the presentation resumes", mark, 60)
    out["resumed_at"] = deck.narrations[resumed["reply"]]["point"]
    clarified = [m for m in inbound(deck, mark) if m.get("type") == "meeting" and m.get("clarifications")]
    out["clarification"] = clarified[-1]["clarifications"][0] if clarified else None
    print("4. the rest of the presentation, the hand-over")
    await wait_state(deck, "ROOM", RUN_TO_QA_S)
    print("5. the discussion")
    mark = deck.mark()
    for key in ("alice", "bob", "carol_confirm"):
        _, speaker, text = MEETING_LINES[key]
        mark = deck.mark()  # (the intervention follows the last of these lines)
        out["room"].append(await room_line(deck, lines[key], speaker, key, text))
    try:
        spoke = await deck.until(lambda m: m.get("type") == "reply_start" and m.get("kind") == "intervention",
                                 "the intervention", mark, 20)
        await deck.until(lambda m: m.get("type") == "reply_end" and m.get("reply") == spoke["reply"],
                         "the intervention's audio", mark, 30)
        got = inbound(deck, mark)
        out["intervention"] = {
            "said": [m["text"] for m in got if m.get("type") == "caption" and m.get("final")
                     and m.get("reply") == spoke["reply"]][-1:],
            "evidence": [[r["source_id"] for r in m["lookup"]["results"]] for m in got
                         if m.get("type") == "evidence" and m["lookup"]["by"] == "engine"][-1:]}
    except AssertionError as exc:
        print(f"  !! {exc}")
        out["intervention"] = None
    _, speaker, text = MEETING_LINES["carol_fix"]
    mark = deck.mark()
    out["room"].append(await room_line(deck, lines["carol_fix"], speaker, "carol_fix", text))
    try:
        saved = await deck.until(lambda m: m.get("type") == "meeting" and m.get("decisions"), "the decision",
                                 mark, 30)
        out["decision"] = saved["decisions"][0]
    except AssertionError as exc:
        print(f"  !! {exc}")
        out["decision"] = None
    print("6. wrap-up (Alice)")
    await wait_state(deck, "ROOM", 60)
    await asyncio.sleep(ROOM_PAUSE_S)
    out["turns"].append(await meeting_turn(deck, lines["wrapup"], "wrapup", "set_presentation_state"))
    mark = deck.mark()
    try:
        await deck.until(lambda m: m.get("type") == "reply_start" and m.get("kind") == "summary", "the summary", 0, 10)
    except AssertionError:
        print("  !! no wrap-up from the question: pressing the phase button (disclosed fallback)")
        out["phase_button"] = True
        await deck.key("phase")
    await deck.until(lambda m: m.get("type") == "state" and m.get("banner") == "Meeting wrapped up",
                     "wrapped up", mark, 60)
    summaries = [m for m in inbound(deck, 0) if m.get("type") == "meeting" and m.get("phase") == "wrapup"]
    out["notes"] = summaries[-1].get("notes") if summaries else None
    out["summary"] = answer_texts(deck, inbound(deck, mark))[-1:]
    return out


def meeting_report(summary: dict) -> bool:
    """Print the meeting's summary; True when the run of show went as the brief wants it."""
    print("\nsummary (meeting):")
    for r in summary["turns"]:
        cut = f" cut in {r['cut_at_ms']} ms into the narration" if r["cut_at_ms"] is not None else ""
        print(f"  {'ok ' if r['ok'] else 'MISS'} {r['label']:<9} expected {r['expected']:<22} by {r['by']:<8} "
              f"tools {r['tools']} latency {r['latency_ms']}{cut}\n       heard {r['heard']}\n       said  {r['said']}"
              f"\n       evidence {r['evidence']}")
    question = next((r for r in summary["turns"] if r["label"] == "question"), {})
    checks = {
        "the question went through lookup_evidence": bool(question.get("lookup_by_model")),
        f"resumed at {INTERRUPT_AT}": summary.get("resumed_at") == INTERRUPT_AT,
        "a clarification was recorded": summary.get("clarification") is not None,
        "the facilitator spoke up at the confirmation": summary.get("intervention") is not None,
        "a decision with owner Bob": bool(summary.get("decision")) and summary["decision"].get("owner") == "Bob",
        "the wrap-up came from the question (no button)": not summary.get("phase_button"),
    }
    for line in summary["room"]:
        print(f"  room {line['label']:<13} {line['speaker']:<5} events {line['events']}\n       expected "
              f"{line['expected']}\n       heard    {line['heard']}")
    print(f"  clarification {summary.get('clarification')}")
    print(f"  intervention  {summary.get('intervention')}")
    print(f"  decision      {summary.get('decision')}")
    print(f"  summary       {summary.get('summary')}")
    for n in summary["narrations"]:
        print(f"  narration {n['reply']:>2} {str(n['point']):<22} {n['audio_ms'] / 1000:5.1f} s"
              f"{'  cut' if n['cut'] else ''}  {n['said'][:100]}")
    for name, passed in checks.items():
        print(f"  {'ok ' if passed else 'MISS'} {name}")
    ok = all(r["ok"] for r in summary["turns"]) and all(checks.values())
    if summary["backup_plays"]:
        print(f"  MISS the backup narration played {summary['backup_plays']} chunk(s): a live narration failed")
        ok = False
    return ok


# --- the online meeting (demo.yaml, input clips) ------------------------------------------------------------------

DEMO_WRAP_TIMEOUT_S = 120.0  # the last step (Wrap up) → the summary has played and the meeting is wrapped up


def is_demo_run(talk: dict) -> bool:
    return bool(talk.get("demo")) and talk.get("input") == "clips"


def demo_of(talk_dir: str):
    """The talk's demo script (None without one, or when the package doesn't load: the server will say why)."""
    from relay.__main__ import resolve_talk_dir
    from relay.meeting import load_demo
    from relay.talk import TalkError, load_talk

    try:
        return load_demo(load_talk(resolve_talk_dir(talk_dir)))
    except TalkError:
        return None


def generate_real_clips(talk_dir: str, settings) -> bool:
    """The participant clips with real TTS 3 (cached), before the server starts (it snapshots them then). False when
    the talk has no demo or not every clip could be generated (the output says why)."""
    from relay import clips as clip_store
    from relay.__main__ import resolve_talk_dir
    from relay.meeting import load_demo
    from relay.talk import load_talk

    talk = load_talk(resolve_talk_dir(talk_dir))
    demo = load_demo(talk)
    if demo is None:
        return False
    print(f"participant clips for {talk.id} (TTS 3, voices {demo.voices}):")
    try:
        report = clip_store.generate_clips(talk, demo, settings, out_root=settings.out_dir,
                                           progress=lambda line: print(line, flush=True))
    except ValueError as exc:  # a recording in the wrong format
        print(f"clips: {exc}")
        return False
    for entry in report.recorded + report.cached:
        print(f"  ok        {entry.step:<18} {entry.duration_ms / 1000:5.1f} s  {entry.speaker}")
    if not report.ok:
        print(f"clips not generated: {report.fatal or [entry.error for entry in report.failed]}")
        return False
    return True


async def run_demo(port: int, talk: dict, *, out: Path | None = None, timeout_scale: float = 1.0,
                   owner: str = "Bob") -> dict:
    """The demo script played by the scripted page (relay.demo_driver) against the server on `port`: every clip
    streamed in real time when its cue holds; then the definition of done. Returns the summary."""
    base = f"http://127.0.0.1:{port}"

    async def get(url: str) -> bytes:
        def fetch() -> bytes:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.read()
        return await asyncio.to_thread(fetch)

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=None) as ws:
        deck = DemoDeck(ws, talk["demo"], {}, talk=talk, log=print, timeout_scale=timeout_scale, keep_audio=True)
        hello = await deck.handshake()
        demo = hello.get("demo") or talk["demo"]
        deck.demo = demo
        missing = [step["id"] for step in demo["steps"] if step.get("say") and not step.get("audio")]
        fake = [step["id"] for step in demo["steps"] if step.get("fake")]
        if missing:
            print(f"  !! clips missing on the server: {missing} (run `uv run python -m relay.clips <talk>`)")
        if fake:
            print(f"  !! fake (tone) clips: {fake}: the model can't understand those (relay.clips --force)")
        deck.clips = await fetch_clips(base, demo, get)
        print(f"meeting: {(hello.get('meeting') or {}).get('title')}; input {hello.get('input')}; "
              f"{len(deck.clips)} participant clips; the microphone is off; alignment "
              f"{json.dumps(hello.get('alignment'))}")
        results = await deck.run()
        wrapped = True
        try:
            await deck.until(lambda m: m.get("type") == "state" and m.get("banner") == "Meeting wrapped up",
                             "the meeting wrapped up", 0, DEMO_WRAP_TIMEOUT_S * timeout_scale)
        except AssertionError as exc:
            print(f"  !! {exc}")
            wrapped = False
        await asyncio.sleep(1.0)
        checks = deck.assess(owner=owner)
        inbound = deck.inbound()
        summary = {
            "demo": True, "wrapped_up": wrapped, "missing_clips": missing, "fake_clips": fake,
            "checks": {name: {"ok": ok, "seen": seen} for name, (ok, seen) in checks.items()},
            "steps": [result.to_json() for result in results],
            "barge_ins": [m for m in inbound if m.get("type") == "barge_in"],
            "tools": [(m.get("name"), m.get("status")) for m in inbound if m.get("type") == "tool"],
            "evidence": [{"query": m["lookup"]["query"], "by": m["lookup"]["by"],
                          "top": [r["source_id"] for r in m["lookup"]["results"]]} for m in inbound
                         if m.get("type") == "evidence"],
            "said": [{"reply": m.get("reply"), "kind": getattr(deck.replies.get(m.get("reply")), "kind", None),
                      "text": m.get("text")} for m in inbound
                     if m.get("type") == "caption" and m.get("role") == "assistant" and m.get("final")],
            "heard": [{"speaker": m.get("speaker"), "text": m.get("text")} for m in inbound
                      if m.get("type") == "caption" and m.get("role") == "user"],
            "transcript": [m["utterance"] for m in inbound if m.get("type") == "transcript"],
            "latency_ms": [m["value"] for m in inbound if m.get("type") == "metric"],
            "errors": [m.get("message") for m in inbound if m.get("type") == "error"],
            "backup_plays": sum(1 for m in inbound if m.get("type") == "play"),
            "notes": next((m.get("notes") for m in reversed(inbound) if m.get("type") == "meeting"
                           and m.get("notes")), None),
        }
        if out is not None:
            (out / "frames.jsonl").write_text("\n".join(json.dumps({"t": round(t, 3), "dir": d, **m})
                                                        for t, d, m in deck.messages) + "\n")
            for reply, pcm in deck.audio.items():
                write_wav(out / f"reply_{reply}.wav", bytes(pcm))
        await deck.close()
        return summary


def demo_report(summary: dict) -> bool:
    """Print the online meeting's summary; True when the patch's §8 definition of done held."""
    print("\nsummary (online meeting, prerecorded participants, live facilitator):")
    for step in summary["steps"]:
        how = "PLAYED BY HAND (cue timed out)" if step["timed_out"] else (
            "no clip" if step["unavailable"] else "on its cue")
        print(f"  step {step['id']:<16} {str(step['speaker'] or step['action']):<6} armed {step['armed_at']:6.1f}s "
              f"fired {step['fired_at'] or 0:6.1f}s  {how}")
    for barge in summary["barge_ins"]:
        print(f"  barge-in: {barge.get('speaker')} cut the {barge.get('state')} reply {barge.get('reply')} at "
              f"{barge.get('played_ms')} ms")
    for line in summary["heard"]:
        print(f"  heard   {line['speaker']}: {line['text']}")
    for said in summary["said"]:
        print(f"  said    [{said['kind']}] {said['text']}")
    for line in summary["transcript"]:
        print(f"  room    {line.get('speaker')} ({line.get('source')}): {line.get('text')}")
    print(f"  evidence {summary['evidence']}")
    print(f"  tools    {summary['tools']}")
    print(f"  latency  {summary['latency_ms']}")
    if summary["errors"]:
        print(f"  errors   {summary['errors']}")
    decisions = (summary.get("notes") or {}).get("decisions") or []
    print(f"  notes    decisions {[(d.get('decision'), d.get('owner'), d.get('by')) for d in decisions]}")
    names = {"barge_in": "a genuine barge-in while the facilitator spoke (no tap, mic off)",
             "lookup_answer": "a live answer grounded in a lookup",
             "resumed": "the presentation resumed at the interrupted talking point",
             "intervention": "a timely intervention before the correction",
             "silent": "silent in the discussion until then (not on the conflicting line alone)",
             "decision": "the corrected decision with owner Bob, after the correction",
             "notes": "… in the wrapped-up notes",
             "labels": "the discussion lines labelled by the clips' speakers"}
    ok = True
    for name, check in summary["checks"].items():
        print(f"  {'ok ' if check['ok'] else 'MISS'} {names.get(name, name)}: {check['seen']}")
        ok = ok and check["ok"]
    for problem, failed in (("a cue timed out (played by hand)", any(s["timed_out"] for s in summary["steps"])),
                            ("participant clips missing", bool(summary["missing_clips"])),
                            ("fake clips (tones) instead of TTS 3", bool(summary["fake_clips"])),
                            ("the meeting did not wrap up", not summary["wrapped_up"]),
                            ("the backup narration played (a live narration failed)", bool(summary["backup_plays"]))):
        if failed:
            print(f"  MISS {problem}")
            ok = False
    return ok


async def run(port: int, talk: dict, lines: dict[str, bytes]) -> dict:
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=None) as ws:
        deck = Deck(ws, talk)
        hello = await deck.until(lambda m: m.get("type") == "hello", "the server's hello", 0, 10)
        mode = hello.get("narration_mode") or "rendered"
        meeting = hello.get("meeting")
        print(f"narration: {mode}" + (f"; meeting: {meeting['title']}" if meeting else "")
              + (f"; alignment: {json.dumps(hello.get('alignment'))}" if meeting else ""))
        await deck.send({"type": "hello", "client": "deck"})
        await deck.send({"type": "arm"})
        await deck.until(lambda m: m.get("type") == "connection" and m.get("status") == "ready", "Realtime ready", 0, 20)
        if meeting:
            result = await run_meeting(deck, lines)
        else:
            result = {"turns": await (run_live(deck, lines) if mode == "live" else run_rendered(deck, talk, lines))}
        await asyncio.sleep(1.0)
        (OUT / "frames.jsonl").write_text("\n".join(json.dumps({"t": round(t, 3), "dir": d, **m})
                                                     for t, d, m in deck.log) + "\n")
        for reply, pcm in deck.audio.items():
            write_wav(OUT / f"reply_{reply}.wav", bytes(pcm))
        return {"narration_mode": mode, "meeting": bool(meeting), **result, "narrations": deck.narration_report(),
                "backup_plays": deck.backup_plays}


def report(summary: dict) -> bool:
    """Print the summary; True when every turn ended as expected and no live narration needed the backup."""
    if summary.get("demo"):
        return demo_report(summary)
    if summary.get("meeting"):
        return meeting_report(summary)
    print(f"\nsummary (narration {summary['narration_mode']}):")
    for r in summary["turns"]:
        cut = f" cut in {r['cut_at_ms']} ms into the narration" if r["cut_at_ms"] is not None else ""
        print(f"  {'ok ' if r['ok'] else 'MISS'} {r['label']:<9} expected {r['expected']:<18} by {r['by']:<8} "
              f"tools {r['tools']} latency {r['latency_ms']}{cut}\n       heard {r['heard']}\n       said  {r['said']}")
    for n in summary["narrations"]:
        print(f"  narration {n['reply']:>2} {str(n['slide']):<10} {n['audio_ms'] / 1000:5.1f} s"
              f"{'  cut' if n['cut'] else ''}  {n['said'][:110]}")
    ok = all(r["ok"] for r in summary["turns"])
    if summary["narration_mode"] == "live" and summary["backup_plays"]:
        print(f"  MISS the backup narration played {summary['backup_plays']} chunk(s): a live narration failed "
              "(see narration_failed in out/logs)")
        ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Relay end to end against the real Higgs Realtime (needs the key).")
    parser.add_argument("--talk", default="talks/test")
    parser.add_argument("--narration", choices=NARRATION_MODES, default=None,
                        help="the server's narration mode (default: RELAY_NARRATION, else live)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--input", choices=INPUT_MODES, default=None,
                        help="the server's input (default: clips for a talk with demo.yaml, else mic)")
    parser.add_argument("--alignment", choices=ALIGNMENT_MODES, default=None,
                        help="the server's alignment check (default: RELAY_ALIGNMENT, else claude; claude needs "
                             "ANTHROPIC_API_KEY in .env, legacy a checks.yaml)")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings(input=args.input, alignment=args.alignment)
    if settings.alignment == "claude" and not settings.anthropic_api_key:
        print("warning: ANTHROPIC_API_KEY missing in .env: claude alignment is unavailable (the discussion is "
              "transcribed, nobody checks it); use --alignment legacy for the regex check")
    if not settings.api_key:
        print("BOSON_API_KEY missing in .env")
        return 2
    if input_mode(settings, demo_of(args.talk)) == "clips" and not generate_real_clips(args.talk, settings):
        return 2  # (the server snapshots the clips at startup: they must exist first)
    port = args.port or free_port()
    command = [sys.executable, "-m", "relay", "serve", args.talk, "--port", str(port)]
    if args.narration:
        command += ["--narration", args.narration]
    if args.input:
        command += ["--input", args.input]
    if args.alignment:
        command += ["--alignment", args.alignment]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with (OUT / "server.log").open("w") as log:
        server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.2)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/talk", timeout=5) as resp:
            talk = json.loads(resp.read())
        if is_demo_run(talk):
            summary = asyncio.run(run_demo(port, talk, out=OUT))
        else:
            lines = synthesize(settings, talk)
            summary = asyncio.run(run(port, talk, lines))
    except AssertionError as exc:
        print("FAIL:", exc)
        summary = None
    finally:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
    if summary is None:
        return 1
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0 if report(summary) else 1


if __name__ == "__main__":
    sys.exit(main())
