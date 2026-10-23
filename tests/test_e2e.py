"""Backend end to end: real uvicorn + server + Controller + RealtimeLink, the fake Realtime, and a scripted deck.

The deck below plays the browser's side of the protocol (design §4) over a real WebSocket: it answers `mic open`
with ~1.6 s of audio frames like a person talking, and `reply_end` with `reply_drained` like the reply player.
Chunk playback is driven by the test (it sends `chunk_ended` when it wants narration to move on).
Browser code itself is covered by scripts/e2e_chrome.py (Chrome can't run inside the Claude Code sandbox).
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn
import websockets

from relay.config import ROOT, Settings
from relay.fake_realtime import FakeConnector
from relay.server import create_app
from relay.talk import load_talk
from relay.timeline import cut_point
from tests.conftest import synthetic_chunks

TALK_DIR = ROOT / "talks" / "test"
FRAME = base64.b64encode(b"\x00\x00" * 2400).decode("ascii")  # 100 ms of 24 kHz PCM16 silence
WAIT = 10.0  # seconds; generous, the fake replies in well under one


def _can_bind() -> bool:
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _can_bind(), reason="local port binding is not allowed here")


class Deck:
    """A scripted stand-in for the browser deck."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.log: list[dict] = []
        self.auto_speak = True
        self.reply_audio_ms: dict[int, int] = {}
        self._new = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            self.log.append(msg)
            self._new.set()
            kind = msg.get("type")
            if kind == "reply_audio":
                pcm = base64.b64decode(msg["pcm"])
                self.reply_audio_ms[msg["reply"]] = self.reply_audio_ms.get(msg["reply"], 0) + len(pcm) // 48
            elif kind == "reply_end":
                await self.send({"type": "reply_drained", "reply": msg["reply"],
                                 "played_ms": self.reply_audio_ms.get(msg["reply"], 0)})
            elif kind == "mic" and msg.get("open") and self.auto_speak:
                for _ in range(16):
                    await self.send({"type": "mic", "pcm": FRAME})

    async def send(self, msg: dict) -> None:
        await self.ws.send(json.dumps(msg))

    async def key(self, key: str, at: dict | None = None) -> None:
        await self.send({"type": "key", "key": key, "at": at, "reply": None, "reply_played_ms": None})

    def mark(self) -> int:
        return len(self.log)

    async def until(self, pred, what: str, since: int = 0, timeout: float = WAIT) -> dict:
        """First message from index `since` on that matches `pred`, waiting for new ones up to `timeout`."""
        async def scan() -> dict:
            index = since
            while True:
                while index < len(self.log):
                    if pred(self.log[index]):
                        return self.log[index]
                    index += 1
                self._new.clear()
                await self._new.wait()

        try:
            return await asyncio.wait_for(scan(), timeout)
        except asyncio.TimeoutError:
            seen = [m.get("type") for m in self.log[since:] if m.get("type") != "reply_audio"]
            raise AssertionError(f"timed out waiting for {what}; seen since mark: {seen}") from None

    async def close(self) -> None:
        self._reader.cancel()
        await self.ws.close()


def is_type(kind: str, **fields):
    return lambda m: m.get("type") == kind and all(m.get(k) == v for k, v in fields.items())


def tool_applied(name: str):
    return lambda m: m.get("type") == "tool" and m.get("name") == name and m.get("status") == "applied"


@asynccontextmanager
async def running_app(tmp_path, connector: FakeConnector):
    talk = load_talk(TALK_DIR)
    app = create_app(TALK_DIR, Settings(fake_realtime=True), out_root=tmp_path, connect=connector,
                     chunks=synthetic_chunks(talk))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield app, port
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


@asynccontextmanager
async def armed_deck(port: int):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws")
    deck = Deck(ws)
    try:
        await deck.until(is_type("hello"), "the controller's hello")
        await deck.send({"type": "hello", "client": "deck"})
        await deck.send({"type": "arm"})
        await deck.until(lambda m: m.get("type") == "connection" and m.get("status") == "ready", "Realtime ready")
        yield deck
    finally:
        await deck.close()


async def opening(deck: Deck) -> dict:
    """Space on the title slide → 'take it away' → start_presentation → the first `play` of 'how'."""
    mark = deck.mark()
    await deck.key("space")
    await deck.until(is_type("mic", open=True), "mic opened for the opening turn", mark)
    await deck.until(tool_applied("start_presentation"), "start_presentation applied", mark)
    return await deck.until(lambda m: m.get("type") == "play" and m.get("slide") == "how", "narration of 'how'", mark)


async def test_talk_flow_end_to_end(tmp_path):
    connector = FakeConnector(think_s=0.05, speed=50.0)
    async with running_app(tmp_path, connector) as (app, port), armed_deck(port) as deck:
        chunks = app.state.chunks
        play = await opening(deck)
        assert (play["chunk"], play["offset_ms"], play["resume_word"], play["fragment"]) == (0, 0, None, 1)

        # Cut mid-sentence in the second sentence of how/0: resume must restart at that sentence's first word.
        words = chunks[("how", 0)].words
        t_ms = words[6].start + 5
        expected = cut_point(words, t_ms)
        assert expected.resume_word > 0
        mark = deck.mark()
        await deck.key("space", {"play_id": play["play_id"], "slide": "how", "chunk": 0, "t_ms": t_ms})
        cut = await deck.until(is_type("cut"), "cut marked", mark)
        assert (cut["slide"], cut["chunk"], cut["cut_word"], cut["resume_word"]) == \
            ("how", 0, expected.cut_word, expected.resume_word)
        await deck.until(is_type("mic", open=True), "mic opened for the cut-in", mark)
        await deck.until(tool_applied("resume_script"), "resume_script applied", mark)
        resumed = await deck.until(lambda m: m.get("type") == "play" and m.get("resume_word") is not None,
                                   "narration resumed with a jump back", mark)
        assert (resumed["slide"], resumed["chunk"], resumed["resume_word"], resumed["offset_ms"]) == \
            ("how", 0, expected.resume_word, expected.offset_ms)

        # Narration runs to the end of 'how' and onto the Q&A slide, then waits for a question.
        current = resumed
        for slide, chunk in (("how", 1), ("how", 2), ("qa", 0)):
            mark = deck.mark()
            await deck.send({"type": "chunk_ended", "play_id": current["play_id"]})
            current = await deck.until(lambda m, s=slide, c=chunk: m.get("type") == "play" and m.get("slide") == s
                                       and m.get("chunk") == c, f"play {slide}/{chunk}", mark)
        assert current["fragment"] == 0
        mark = deck.mark()
        await deck.send({"type": "chunk_ended", "play_id": current["play_id"]})
        await deck.until(lambda m: m.get("type") == "state" and m.get("state") == "IDLE" and m.get("phase") == "qa",
                         "IDLE on the Q&A slide", mark)

        # Q&A: a question gets an answer and the floor goes back to the presenter.
        mark = deck.mark()
        await deck.key("space")
        await deck.until(tool_applied("handoff_to_human"), "handoff_to_human applied", mark)
        await deck.until(lambda m: m.get("type") == "state" and m.get("phase") == "human"
                         and "floor" in str(m.get("banner") or ""), "presenter has the floor", mark)

        # R with nothing to resume restarts the current relay slide.
        mark = deck.mark()
        await deck.key("relay")
        replay = await deck.until(is_type("play"), "R plays the current relay slide", mark)
        assert (replay["slide"], replay["chunk"], replay["offset_ms"]) == ("qa", 0, 0)

        # ← goes back a slide and narration follows.
        mark = deck.mark()
        await deck.key("prev", {"play_id": replay["play_id"], "slide": "qa", "chunk": 0, "t_ms": 300})
        back = await deck.until(is_type("play"), "← narrates the previous slide", mark)
        assert (back["slide"], back["chunk"], back["offset_ms"]) == ("how", 0, 0)

        # A typed question naming a slide makes the (fake) model jump there.
        mark = deck.mark()
        await deck.send({"type": "typed", "text": "Can we skip to qa now?", "reply": None, "reply_played_ms": None,
                         "at": {"play_id": back["play_id"], "slide": "how", "chunk": 0, "t_ms": 300}})
        await deck.until(lambda m: m.get("type") == "caption" and m.get("role") == "user", "typed caption", mark)
        await deck.until(tool_applied("goto_slide"), "goto_slide applied", mark)
        jumped = await deck.until(is_type("play"), "narration of the target slide", mark)
        assert (jumped["slide"], jumped["chunk"]) == ("qa", 0)

        sent = [event["type"] for conn in connector.connections for event in conn.received]
        assert sent[0] == "session.update"
        assert "response.create" in sent  # the typed question
        # A function_call_output is never followed by a response.create (Relay goes quiet, narration takes over).
        for conn in connector.connections:
            types = [event["type"] for event in conn.received]
            for i, event in enumerate(conn.received):
                if event["type"] == "conversation.item.create" and event["item"]["type"] == "function_call_output":
                    assert "response.create" not in types[i + 1:i + 2]

    logs = list((tmp_path / "logs").glob("test-*.jsonl"))
    assert len(logs) == 1
    kinds = {json.loads(line)["kind"] for line in logs[0].read_text().splitlines()}
    assert {"state", "tool", "key"} <= kinds


async def test_realtime_drop_reconnects_with_one_summary(tmp_path):
    connector = FakeConnector(think_s=0.05, speed=50.0)
    async with running_app(tmp_path, connector) as (app, port), armed_deck(port) as deck:
        await opening(deck)  # one live exchange to summarise
        first = connector.connections[-1]
        mark = deck.mark()
        first.drop()
        await deck.until(lambda m: m.get("type") == "connection" and m.get("status") == "reconnecting",
                         "link reports reconnecting", mark)
        await deck.until(lambda m: m.get("type") == "connection" and m.get("status") == "ready",
                         "link ready again", mark)
        second = connector.connections[-1]
        assert second is not first
        for _ in range(100):  # the summary goes out right after `ready`
            if any(e["type"] == "conversation.item.create" for e in second.received):
                break
            await asyncio.sleep(0.02)
        types = [event["type"] for event in second.received]
        assert types[0] == "session.update"
        summaries = [e for e in second.received if e["type"] == "conversation.item.create"]
        assert len(summaries) == 1
        text = summaries[0]["item"]["content"][0]["text"]
        assert text.startswith("Context only") and len(text) <= 600
        assert "response.create" not in types


async def test_esc_while_listening_returns_to_narration(tmp_path):
    connector = FakeConnector(think_s=0.05, speed=50.0)
    async with running_app(tmp_path, connector) as (app, port), armed_deck(port) as deck:
        play = await opening(deck)
        deck.auto_speak = False
        mark = deck.mark()
        await deck.key("space", {"play_id": play["play_id"], "slide": "how", "chunk": 0, "t_ms": 200})
        await deck.until(is_type("mic", open=True), "mic opened", mark)
        mark = deck.mark()
        await deck.key("esc")
        await deck.until(is_type("mic", open=False), "mic closed", mark)
        back = await deck.until(is_type("play"), "narration resumes at once", mark)
        assert (back["slide"], back["chunk"], back["resume_word"]) == ("how", 0, 0)
        assert not any(m.get("type") == "reply_start" for m in deck.log[mark:])
        assert "input_audio_buffer.clear" in [e["type"] for e in connector.connections[-1].received]
