"""Live end to end without a browser: real server + real Higgs Realtime + a scripted deck whose "microphone" plays
lines synthesized with TTS 3 (other voices than Relay's). Exercises live narration (D52: Higgs presents every slide,
the deck cuts in during a narration, and the slide continues live), turn detection, input transcription, the model's
tool calls with the real prompts, reply audio and latency. Runs on any talk; needs the key in .env and real renders
(the backup narration).

    uv run python scripts/live_check.py [--talk talks/test] [--narration live|rendered] [--port N]
    uv run python scripts/live_check.py --talk talks/relay-intro
    RELAY_TURN_DETECTION='{"type": "semantic_vad", "silence_duration_ms": 500}' uv run python scripts/live_check.py

The flow: the opening ("take it away") → the first relay slide's narration → a comment cut-in during it → the slide
continues live → a plain-language cut-in during that → the narrations run on to the Q&A slide (the deck plays each
one out in real time) → a question → the hand-back. The narration mode is the server's (`--narration`, else
RELAY_NARRATION), read from its hello; in rendered mode the cut-ins land on word positions of the renders instead.

Writes out/live/: the synthesized lines (*.pcm), everything Relay streamed (reply_<n>.wav: answers and live
narrations), frames.jsonl, summary.json. Each turn reports whether the model called the expected tool or the
watchdog did it ("auto"); each live narration its slide, length and what Relay said.
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
from relay.config import NARRATION_MODES, ROOT, load_settings  # noqa: E402

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
PRINTED = ("tool", "state", "cut", "play", "slide", "reply_start", "metric", "error", "connection")


def tts_pcm(settings, voice: str, text: str) -> bytes:
    path = OUT / f"{voice}-{hashlib.sha256(text.encode()).hexdigest()[:10]}.pcm"
    if path.exists():
        return path.read_bytes()
    resp = requests.post(f"{settings.base_url}/v1/audio/speech", headers=settings.headers, timeout=120,
                         json={"model": "higgs-tts-3", "input": text, "voice": voice, "response_format": "pcm"})
    resp.raise_for_status()
    path.write_bytes(resp.content)
    return resp.content


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
        self.narrations: dict[int, dict] = {}  # live narration reply → {reply, slide, said}
        self.backup_plays = 0  # rendered chunks played while the server narrates live (the backup stepped in)
        self.slide: str | None = None
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
        elif kind == "reply_start" and msg.get("kind") == "narration":
            self.narrations[msg["reply"]] = {"reply": msg["reply"], "slide": self.slide, "said": ""}
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


async def run(port: int, talk: dict, lines: dict[str, bytes]) -> dict:
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=None) as ws:
        deck = Deck(ws, talk)
        hello = await deck.until(lambda m: m.get("type") == "hello", "the server's hello", 0, 10)
        mode = hello.get("narration_mode") or "rendered"
        print(f"narration: {mode}")
        await deck.send({"type": "hello", "client": "deck"})
        await deck.send({"type": "arm"})
        await deck.until(lambda m: m.get("type") == "connection" and m.get("status") == "ready", "Realtime ready", 0, 20)
        turns = await (run_live(deck, lines) if mode == "live" else run_rendered(deck, talk, lines))
        await asyncio.sleep(1.0)
        (OUT / "frames.jsonl").write_text("\n".join(json.dumps({"t": round(t, 3), "dir": d, **m})
                                                     for t, d, m in deck.log) + "\n")
        for reply, pcm in deck.audio.items():
            write_wav(OUT / f"reply_{reply}.wav", bytes(pcm))
        return {"narration_mode": mode, "turns": turns, "narrations": deck.narration_report(),
                "backup_plays": deck.backup_plays}


def report(summary: dict) -> bool:
    """Print the summary; True when every turn ended as expected and no live narration needed the backup."""
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
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    if not settings.api_key:
        print("BOSON_API_KEY missing in .env")
        return 2
    lines = {key: tts_pcm(settings, voice, text) for key, (voice, text) in LINES.items()}
    port = args.port or free_port()
    command = [sys.executable, "-m", "relay", "serve", args.talk, "--port", str(port)]
    if args.narration:
        command += ["--narration", args.narration]
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
