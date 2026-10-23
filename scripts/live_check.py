"""Live end to end without a browser: real server + real Higgs Realtime + a scripted deck whose "microphone" plays
lines synthesized with TTS 3 (other voices than Relay's). Exercises turn detection, input transcription, the model's
tool calls with the real prompts, reply audio and latency on talks/test. Needs the key in .env and real renders.

    uv run python scripts/live_check.py [--talk talks/test] [--port N]
    RELAY_TURN_DETECTION='{"type": "semantic_vad", "silence_duration_ms": 500}' uv run python scripts/live_check.py

Writes out/live/: the synthesized lines (*.pcm), every Relay reply (reply_<n>.wav), frames.jsonl, summary.json.
Each line reports whether the model called the expected tool or the watchdog did it ("auto").
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
from relay.config import ROOT, load_settings  # noqa: E402

OUT = ROOT / "out" / "live"
RATE = 24000
FRAME = RATE // 10 * 2  # 100 ms of PCM16 mono

LINES = {  # key: (voice, text)
    "opening": ("jake", "So I built a co-presenter. Relay, take it away."),
    "comment": ("jake", "Quick comment: that was literally my last demo day."),
    "plain": ("nora", "Sorry, can you explain that like I'm not an engineer?"),
    "question": ("nora", "How do you know where to pick up after someone interrupts you?"),
}


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
    def __init__(self, ws) -> None:
        self.ws = ws
        self.t0 = time.monotonic()
        self.log: list[tuple[float, str, dict]] = []
        self.audio: dict[int, bytearray] = {}
        self._new = asyncio.Event()
        self.mic_open = False
        self._reader = asyncio.create_task(self._read())

    def now(self) -> float:
        return time.monotonic() - self.t0

    async def _read(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "reply_audio":
                self.audio.setdefault(msg["reply"], bytearray()).extend(base64.b64decode(msg["pcm"]))
                continue
            self.log.append((self.now(), "in", msg))
            self._new.set()
            if kind == "mic":
                self.mic_open = bool(msg.get("open"))
            elif kind == "reply_end":
                asyncio.create_task(self._drain(msg["reply"]))
            if kind in ("caption", "tool", "state", "cut", "play", "metric", "error", "connection"):
                brief = {k: v for k, v in msg.items() if k not in ("type",)}
                print(f"  [{self.now():6.2f}s] <- {kind} {json.dumps(brief)[:160]}")

    async def _drain(self, reply: int) -> None:
        """Pretend the reply plays out in real time, like the browser's reply player."""
        played_ms = len(self.audio.get(reply, b"")) // 48
        await asyncio.sleep(played_ms / 1000 + 0.1)
        await self.send({"type": "reply_drained", "reply": reply, "played_ms": played_ms})

    async def send(self, msg: dict) -> None:
        self.log.append((self.now(), "out", msg))
        await self.ws.send(json.dumps(msg))

    async def key(self, key: str, at: dict | None = None) -> None:
        print(f"  [{self.now():6.2f}s] -> key {key} {json.dumps(at) if at else ''}")
        await self.send({"type": "key", "key": key, "at": at, "reply": None, "reply_played_ms": None})

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


def tool(name: str, status: str = "applied"):
    return lambda m: m.get("type") == "tool" and m.get("name") == name and m.get("status") == status


async def turn(deck: Deck, line: bytes, key_at: dict | None, expect_tool: str, label: str) -> dict:
    mark = deck.mark()
    await deck.key("space", key_at)
    await deck.until(lambda m: m.get("type") == "mic" and m.get("open"), f"{label}: mic open", mark, 10)
    speaking = asyncio.create_task(deck.speak(line))
    await deck.until(lambda m: m.get("type") == "reply_start", f"{label}: reply started", mark, 30)
    await speaking
    tools = []
    try:
        applied = await deck.until(lambda m: m.get("type") == "tool"
                                   and m.get("status") in ("applied", "rejected", "auto"),
                                   f"{label}: a tool ran", mark, 40)
        tools.append(applied)
    except AssertionError as exc:
        print(f"  !! {label}: {exc}")
    got = [m for _, d, m in deck.log[mark:] if d == "in" and m.get("type") == "tool"]
    user = [m["text"] for _, d, m in deck.log[mark:] if d == "in" and m.get("type") == "caption" and m.get("role") == "user"]
    said = [m["text"] for _, d, m in deck.log[mark:] if d == "in" and m.get("type") == "caption"
            and m.get("role") == "assistant" and m.get("final")]
    latency = [m["value"] for _, d, m in deck.log[mark:] if d == "in" and m.get("type") == "metric"]
    by_model = any(m.get("name") == expect_tool and m.get("status") == "applied" for m in got)
    by_watchdog = any(m.get("name") == expect_tool and m.get("status") == "auto" for m in got)
    ok = by_model or by_watchdog
    return {"label": label, "expected": expect_tool, "ok": ok, "by": "model" if by_model else
            ("watchdog" if by_watchdog else "-"), "tools": [(m["name"], m["status"]) for m in got],
            "heard": user[-1:] if user else [], "said": said[-1:] if said else [], "latency_ms": latency}


async def run(port: int, lines: dict[str, bytes]) -> list[dict]:
    results = []
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=None) as ws:
        deck = Deck(ws)
        await deck.send({"type": "hello", "client": "deck"})
        await deck.send({"type": "arm"})
        await deck.until(lambda m: m.get("type") == "connection" and m.get("status") == "ready", "Realtime ready", 0, 20)

        print("1. opening")
        results.append(await turn(deck, lines["opening"], None, "start_presentation", "opening"))
        play = await deck.until(lambda m: m.get("type") == "play" and m.get("slide") == "how", "narration of how", 0, 20)

        print("2. comment cut-in, mid-sentence on how/0")
        await asyncio.sleep(2.2)
        at = {"play_id": play["play_id"], "slide": "how", "chunk": 0, "t_ms": 2500}
        results.append(await turn(deck, lines["comment"], at, "resume_script", "comment"))
        resumed = await deck.until(lambda m: m.get("type") == "play" and m.get("resume_word") is not None,
                                   "resumed with a jump back", 0, 20)

        print("3. plain-language cut-in on how/0 again")
        await asyncio.sleep(1.5)
        at = {"play_id": resumed["play_id"], "slide": "how", "chunk": 0, "t_ms": resumed["offset_ms"] + 1200}
        results.append(await turn(deck, lines["plain"], at, "resume_script", "plain"))
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
        results.append(await turn(deck, lines["question"], None, "handoff_to_human", "question"))
        await asyncio.sleep(1.0)
        (OUT / "frames.jsonl").write_text("\n".join(json.dumps({"t": round(t, 3), "dir": d, **m})
                                                     for t, d, m in deck.log) + "\n")
        for reply, pcm in deck.audio.items():
            write_wav(OUT / f"reply_{reply}.wav", bytes(pcm))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--talk", default="talks/test")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    if not settings.api_key:
        print("BOSON_API_KEY missing in .env")
        return 2
    lines = {key: tts_pcm(settings, voice, text) for key, (voice, text) in LINES.items()}
    port = args.port or free_port()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with (OUT / "server.log").open("w") as log:
        server = subprocess.Popen([sys.executable, "-m", "relay", "serve", args.talk, "--port", str(port)],
                                  cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.2)
        results = asyncio.run(run(port, lines))
    except AssertionError as exc:
        print("FAIL:", exc)
        results = None
    finally:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
    if results is None:
        return 1
    (OUT / "summary.json").write_text(json.dumps(results, indent=2))
    print("\nsummary:")
    for r in results:
        print(f"  {'ok ' if r['ok'] else 'MISS'} {r['label']:<9} expected {r['expected']:<18} by {r['by']:<8} "
              f"tools {r['tools']} "
              f"latency {r['latency_ms']}\n       heard {r['heard']}\n       said  {r['said']}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
