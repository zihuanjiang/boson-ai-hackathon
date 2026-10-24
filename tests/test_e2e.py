"""Backend end to end: real uvicorn + server + Controller + RealtimeLink, the fake Realtime, and a scripted deck.

The deck below plays the browser's side of the protocol (design §4) over a real WebSocket: it answers `mic open`
with ~1.6 s of audio frames like a person talking, and `reply_end` with `reply_drained` like the reply player.
Narration is driven by the test: it sends `chunk_ended` when it wants rendered narration to move on, and, with
`hold_narration`, drains a live narration reply (D52) itself, so it can cut in while one plays. The runs narrate
from the renders unless they ask for `narration="live"`.
Browser code itself is covered by scripts/e2e_chrome.py (Chrome can't run inside the Claude Code sandbox).
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

from relay import controller as controller_module
from relay.config import ROOT, Settings
from relay.controller import SUMMARY_PREFIX
from relay.fake_realtime import FAKE_TRANSCRIPT, FakeConnector
from relay.render import render_talk
from relay.server import STALLED_CLOSE_CODE, create_app
from relay.talk import load_talk
from relay.timeline import cut_point
from relay.tts import wav_duration_ms
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
        self.hold_narration = False  # the test drains live narration replies itself (`drain`)
        self.reply_audio_ms: dict[int, int] = {}
        self.kinds: dict[int, str] = {}  # reply → its reply_start kind
        self._new = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            self.log.append(msg)
            self._new.set()
            kind = msg.get("type")
            if kind == "reply_start":
                self.kinds[msg["reply"]] = msg.get("kind")
            elif kind == "reply_audio":
                pcm = base64.b64decode(msg["pcm"])
                self.reply_audio_ms[msg["reply"]] = self.reply_audio_ms.get(msg["reply"], 0) + len(pcm) // 48
            elif kind == "reply_end" and not (self.hold_narration and self.kinds.get(msg["reply"]) == "narration"):
                await self.drain(msg["reply"])
            elif kind == "mic" and msg.get("open") and self.auto_speak:
                for _ in range(16):
                    await self.send({"type": "mic", "pcm": FRAME})

    async def send(self, msg: dict) -> None:
        await self.ws.send(json.dumps(msg))

    async def drain(self, reply: int) -> None:
        """The reply player ran empty after `reply_end`: all of the reply was heard."""
        await self.send({"type": "reply_drained", "reply": reply, "played_ms": self.reply_audio_ms.get(reply, 0)})

    async def key(self, key: str, at: dict | None = None, *, reply: int | None = None,
                  played: int | None = None) -> None:
        await self.send({"type": "key", "key": key, "at": at, "reply": reply, "reply_played_ms": played})

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


def is_type(kind: str, /, **fields):
    return lambda m: m.get("type") == kind and all(m.get(k) == v for k, v in fields.items())


def tool_applied(name: str):
    return lambda m: m.get("type") == "tool" and m.get("name") == name and m.get("status") == "applied"


@asynccontextmanager
async def running_app(tmp_path, connector: FakeConnector, *, talk_dir: Path = TALK_DIR, rendered: bool = False,
                      narration: str = "rendered"):
    """Real uvicorn on an ephemeral port. Narration: synthetic timings without audio, or (`rendered`) the renders in
    `tmp_path/talks/<id>/`, loaded like `relay serve` loads them; presented live with `narration="live"`."""
    talk = load_talk(talk_dir)
    app = create_app(talk_dir, Settings(fake_realtime=True, narration=narration), out_root=tmp_path,
                     connect=connector, chunks=None if rendered else synthetic_chunks(talk))
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


async def hello_version(port: int) -> str:
    """The `version` in the controller's hello to a deck that connects now."""
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
        hello = json.loads(await ws.recv())
    assert hello["type"] == "hello"
    return hello["version"]


async def opening(deck: Deck) -> dict:
    """Space on the title slide → 'take it away' → start_presentation → the first `play` of 'how'."""
    mark = deck.mark()
    await deck.key("space")
    await deck.until(is_type("mic", open=True), "mic opened for the opening turn", mark)
    await deck.until(tool_applied("start_presentation"), "start_presentation applied", mark)
    return await deck.until(lambda m: m.get("type") == "play" and m.get("slide") == "how", "narration of 'how'", mark)


def at(play: dict, t_ms: int) -> dict:
    """The deck's Position `t_ms` into the chunk that `play` started."""
    return {"play_id": play["play_id"], "slide": play["slide"], "chunk": play["chunk"], "t_ms": t_ms}


async def speak(deck: Deck, frames: int = 8) -> None:
    """Ken talks into the open mic (100 ms frames, sent in order before any later key)."""
    for _ in range(frames):
        await deck.send({"type": "mic", "pcm": FRAME})


def sent_types(conn, since: int) -> list[str]:
    """Event types the link sent on `conn` from index `since` on, mic audio left out (frames still in flight when
    `since` was taken land after it, but always before any later key's events)."""
    return [event["type"] for event in conn.received[since:] if event["type"] != "input_audio_buffer.append"]


def log_entries(tmp_path: Path) -> list[dict]:
    """The server run's event log (read after the app has stopped)."""
    logs = sorted((tmp_path / "logs").glob("test-*.jsonl"))
    assert logs, "no event log"
    return [json.loads(line) for path in logs for line in path.read_text(encoding="utf-8").splitlines()]


def is_narration_request(event: dict) -> bool:
    return ((event.get("response") or {}).get("metadata") or {}).get("relay") == "narration"


def check_tool_outputs(connector: FakeConnector) -> None:
    """Design §5.2 / D6, per Realtime session: every function_call the server announced got exactly one
    function_call_output (a JSON string), and an output is never followed by response.create unless a new user item
    (a question or a commit, not the reconnect summary) came in between, or it is a live narration request (it
    carries its own instructions)."""
    for number, conn in enumerate(connector.connections, 1):
        announced = sorted(item["call_id"] for item in conn._items.values() if item.get("type") == "function_call")
        outputs = [event["item"] for event in conn.received if event["type"] == "conversation.item.create"
                   and event["item"].get("type") == "function_call_output"]
        assert sorted(output["call_id"] for output in outputs) == announced, f"session {number}"
        assert all(isinstance(output["output"], str) for output in outputs), f"session {number}"
        armed = False
        for event in conn.received:
            item = event.get("item") or {}
            if event["type"] == "conversation.item.create" and item.get("type") == "function_call_output":
                armed = True
            elif event["type"] == "conversation.item.create" and item.get("type") == "message":
                armed = item["content"][0]["text"].startswith(SUMMARY_PREFIX)
            elif event["type"] == "input_audio_buffer.commit":
                armed = False
            elif event["type"] == "response.create" and not is_narration_request(event):
                assert not armed, f"session {number}: response.create right after a function_call_output"


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
        # One output per call, never followed by a response.create (Relay goes quiet, narration takes over).
        check_tool_outputs(connector)

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
        check_tool_outputs(connector)


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


async def test_the_nothing_to_cancel_error_of_the_fake_is_logged_not_shown(tmp_path):
    """Fake ↔ controller over the real link: a response.cancel with nothing active gets Higgs's documented error
    (code `response_not_active`, worded without "cancel"); the controller logs it and never shows it. A second, real
    error right behind it (the fake answers in order) proves the first one was handled by the time the deck hears."""
    connector = FakeConnector(think_s=0.05, speed=50.0)
    async with running_app(tmp_path, connector) as (app, port), armed_deck(port) as deck:
        await opening(deck)
        mark = deck.mark()
        link = app.state.link
        assert await link.send({"type": "response.cancel"})  # nothing is active now
        assert await link.send({"type": "conversation.item.retrieve", "item_id": "no-such-item"})  # shown
        sentinel = await deck.until(is_type("error"), "the error for the unknown item", mark)
        assert "no-such-item" in sentinel["message"]
        assert [m["message"] for m in deck.log[mark:] if m.get("type") == "error"] == [sentinel["message"]]
    benign = [e for e in log_entries(tmp_path) if e["kind"] == "realtime_error" and e["code"] == "response_not_active"]
    assert len(benign) == 1 and "cancel" not in benign[0]["message"]


async def test_a_deck_that_stops_reading_gets_4001_when_it_recovers_and_reconnects(tmp_path):
    """D21 over real uvicorn, whose websocket writes nothing (the close frame included) while the peer isn't
    reading: a tab that hangs during a reply is dropped; when it reads again it gets the 4001 and reconnects."""
    async with running_app(tmp_path, FakeConnector(think_s=0.05, speed=50.0)) as (app, port):
        hub = app.state.hub
        hub._send_timeout = 0.3  # D21's 5 s, shortened for the test
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024)  # a small window fills fast
        raw.connect(("127.0.0.1", port))
        hung = await websockets.connect(f"ws://127.0.0.1:{port}/ws", sock=raw, max_queue=1, ping_interval=None)
        try:
            assert json.loads(await hung.recv())["type"] == "hello"
            # The tab stops reading while a reply streams in: the hub gives up on it instead of blocking.
            frame = {"type": "reply_audio", "reply": 1, "pcm": "B" * 1280}
            for _ in range(50_000):
                if not hub.connected:
                    break
                await hub.send(frame)
            assert not hub.connected
            await asyncio.sleep(1.5)  # hung for longer than any close timeout
            with pytest.raises(ConnectionClosed) as closed:  # it reads again: its backlog, then the close
                async with asyncio.timeout(WAIT):
                    while True:
                        await hung.recv()
            assert closed.value.rcvd is not None and closed.value.rcvd.code == STALLED_CLOSE_CODE
        finally:
            await hung.close()
        async with armed_deck(port) as deck:  # the tab reconnects and drives the show again
            assert hub.connected
            mark = deck.mark()
            await deck.key("relay")
            await deck.until(is_type("play"), "R narrates again", mark)


async def test_a_rerender_under_a_running_server_changes_nothing_until_a_restart(tmp_path):
    """Design §3 over real HTTP: the server serves the narration it started with, so a re-render can't put new audio
    under old word timings; the next server run presents the new render under a new version."""
    talk_dir = tmp_path / "talks" / "test"
    shutil.copytree(TALK_DIR, talk_dir)
    assert render_talk(load_talk(talk_dir), Settings(), fake=True, out_root=tmp_path).ok
    async with running_app(tmp_path, FakeConnector(), talk_dir=talk_dir, rendered=True) as (app, port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            talk = (await http.get("/api/talk")).json()
            assert await hello_version(port) == talk["version"]  # the deck compares the two after a reconnect
            served = await http.get("/media/how/0.wav")
            assert served.status_code == 200

            script = talk_dir / "script.yaml"  # Ken edits a line and re-renders while the server runs
            text = script.read_text(encoding="utf-8")
            script.write_text(text.replace("Here is how this works.", "Here is how this works, step by step."),
                              encoding="utf-8")
            report = render_talk(load_talk(talk_dir), Settings(), fake=True, out_root=tmp_path)
            assert report.ok and [entry.label for entry in report.rendered] == ["how/0"]

            again = await http.get("/media/how/0.wav")
            assert again.content == served.content and again.headers["etag"] == served.headers["etag"]
            assert (await http.get("/api/talk")).json() == talk
            how0 = talk["slides"][1]["chunks"][0]
            assert wav_duration_ms(again.content) == how0["duration_ms"] == \
                app.state.controller.chunks[("how", 0)].duration_ms
            unchanged = await http.get("/media/how/0.wav", headers={"if-none-match": served.headers["etag"]})
            assert unchanged.status_code == 304
    async with running_app(tmp_path, FakeConnector(), talk_dir=talk_dir, rendered=True) as (app, port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            restarted = (await http.get("/api/talk")).json()
            fresh = await http.get("/media/how/0.wav")
        assert await hello_version(port) == restarted["version"]
    assert restarted["version"] != talk["version"]  # an open deck sees the new version in hello and reloads
    assert fresh.content != served.content
    assert wav_duration_ms(fresh.content) == restarted["slides"][1]["chunks"][0]["duration_ms"]


async def test_a_tool_call_after_a_reconnect_runs_when_the_server_reuses_ids(tmp_path):
    """A new Realtime session may number its response and call ids from 1 again (the docs promise uniqueness within
    a session at most): after a drop, the new session's goto_slide (call id = the opening's) must still run."""
    connector = FakeConnector(think_s=0.05, speed=50.0, reuse_ids=True)
    async with running_app(tmp_path, connector) as (app, port), armed_deck(port) as deck:
        await opening(deck)  # session 1: resp_fake000001 carrying call_fake000001 (start_presentation)
        mark = deck.mark()
        connector.connections[-1].drop()
        await deck.until(lambda m: m.get("type") == "connection" and m.get("status") == "reconnecting",
                         "link reports reconnecting", mark)
        await deck.until(lambda m: m.get("type") == "connection" and m.get("status") == "ready",
                         "link ready again", mark)
        current = [m for m in deck.log if m.get("type") == "play"][-1]
        mark = deck.mark()
        await deck.send({"type": "typed", "text": "Can we skip to qa now?", "reply": None, "reply_played_ms": None,
                         "at": {"play_id": current["play_id"], "slide": current["slide"],
                                "chunk": current["chunk"], "t_ms": 300}})
        await deck.until(tool_applied("goto_slide"), "goto_slide applied in the new session", mark)
        jumped = await deck.until(is_type("play"), "narration of the target slide", mark)
        assert (jumped["slide"], jumped["chunk"]) == ("qa", 0)
        sessions = [[e for e in conn.received if e["type"] == "conversation.item.create"
                     and e["item"]["type"] == "function_call_output"] for conn in connector.connections]
        assert [[e["item"]["call_id"] for e in outputs] for outputs in sessions] == [["call_fake000001"]] * 2
        check_tool_outputs(connector)


async def test_space_again_while_listening_sends_the_turn_when_turn_detection_never_ends_it(tmp_path):
    """Design §5.1 "send now" over the real stack. Room noise keeps turn detection from ever ending the turn (the
    fake's speech_stop_s is huge), so Ken taps Space again: the turn goes out by hand (commit + response.create), the
    reply plays and its tool runs. A double tap is one send: its second press neither cuts the coming reply nor opens
    another turn."""
    connector = FakeConnector(think_s=0.3, speed=50.0, speech_stop_s=1e9)
    async with running_app(tmp_path, connector) as (app, port), armed_deck(port) as deck:
        deck.auto_speak = False
        link = connector.connections[-1]

        # The opening, sent by hand.
        mark = deck.mark()
        await deck.key("space")
        await deck.until(is_type("mic", open=True), "mic opened for the opening", mark)
        # (the greeting that answers the deck's own hello, `mic {open: false}` included, may land after `mark`)
        opened = deck.log.index({"type": "mic", "open": True}, mark) + 1
        await speak(deck)
        await asyncio.sleep(0.2)  # turn detection would have ended a turn by now; this one never does
        assert {"type": "mic", "open": False} not in deck.log[opened:], "the mic closed by itself"
        sent_from = len(link.received)
        await deck.key("space")  # send now
        await deck.until(is_type("mic", open=False), "the mic closes on the send", opened)
        start = await deck.until(is_type("reply_start"), "the opening reply starts", mark)
        heard = await deck.until(lambda m: m.get("type") == "caption" and m.get("role") == "user",
                                 "the user caption (transcript matched by the committed item)", mark)
        assert (heard["text"], heard["reply"]) == (FAKE_TRANSCRIPT, start["reply"])
        await deck.until(tool_applied("start_presentation"), "start_presentation applied", mark)
        play = await deck.until(lambda m: m.get("type") == "play" and m.get("slide") == "how", "narration of 'how'",
                                mark)
        assert sent_types(link, sent_from)[:2] == ["input_audio_buffer.commit", "response.create"]

        # A cut-in on how/0, sent with a double tap.
        words = app.state.chunks[("how", 0)].words
        expected = cut_point(words, words[6].start + 5)
        mark = deck.mark()
        await deck.key("space", at(play, words[6].start + 5))
        await deck.until(is_type("mic", open=True), "mic opened for the cut-in", mark)
        await speak(deck)
        sent_from = len(link.received)
        await deck.key("space")  # send now
        await deck.key("space")  # the double tap's second press, before any reply audio: the same send
        await deck.until(tool_applied("resume_script"), "resume_script applied", mark)
        resumed = await deck.until(lambda m: m.get("type") == "play" and m.get("resume_word") is not None,
                                   "narration resumes at the cut sentence", mark)
        assert (resumed["slide"], resumed["chunk"], resumed["resume_word"], resumed["offset_ms"]) == \
            ("how", 0, expected.resume_word, expected.offset_ms)
        after = deck.log[mark:]
        assert [m.get("open") for m in after if m.get("type") == "mic"] == [True, False]  # one turn, one close
        assert sum(m.get("type") == "reply_start" for m in after) == 1
        assert not any(m.get("type") == "reply_flush" for m in after)
        sent = sent_types(link, sent_from)
        assert sent[:2] == ["input_audio_buffer.commit", "response.create"]
        assert "response.cancel" not in sent and sent.count("response.create") == 1
        assert [m["message"] for m in deck.log if m.get("type") == "error"] == []
        check_tool_outputs(connector)

    entries = log_entries(tmp_path)
    assert [e["how"] for e in entries if e["kind"] == "turn_sent"] == ["hand", "hand"]
    assert sum(e["kind"] == "double_send_ignored" for e in entries) == 1
    assert not any(e["kind"] == "speech_end" for e in entries)  # turn detection never ended a turn


async def test_spoken_replies_get_through_after_a_reconnect_to_a_session_that_reuses_ids(tmp_path):
    """Realtime ids are unique within a session at most: a new session may number its responses and calls from 1
    again. Session 1 ends with a reply cut off while it played (its response id is ignored from then on); after a
    drop, session 2 reuses that very id, and the call ids already answered, for ordinary replies, which must play in
    full and run their tools."""
    connector = FakeConnector(think_s=0.05, speed=2.0, reuse_ids=True)  # a reply streams for ~0.6 s: time to cut it
    async with running_app(tmp_path, connector) as (app, port), armed_deck(port) as deck:
        play = await opening(deck)  # session 1: resp_fake000001 carrying call_fake000001
        mark = deck.mark()
        await deck.key("space", at(play, 300))  # a cut-in: resp_fake000002
        start = await deck.until(is_type("reply_start"), "the cut-in's reply starts", mark)
        await deck.until(is_type("reply_audio", reply=start["reply"]), "its first audio", mark)
        mark = deck.mark()
        await deck.key("space")  # Ken cuts it off while it plays and asks again: resp_fake000003
        await deck.until(is_type("reply_flush", reply=start["reply"]), "the reply is cut", mark)
        await deck.until(tool_applied("resume_script"), "the new question's resume_script", mark)
        current = await deck.until(is_type("play"), "narration resumes", mark)

        mark = deck.mark()
        connector.connections[-1].drop()
        await deck.until(lambda m: m.get("type") == "connection" and m.get("status") == "reconnecting",
                         "link reports reconnecting", mark)
        await deck.until(lambda m: m.get("type") == "connection" and m.get("status") == "ready",
                         "link ready again", mark)
        assert len(connector.connections) == 2

        for turn in (1, 2):  # session 2: resp_fake000001 / call_fake000001, then resp_fake000002 / call_fake000002
            mark = deck.mark()
            await deck.key("space", at(current, 300))
            start = await deck.until(is_type("reply_start"), f"session 2, reply {turn} starts", mark)
            await deck.until(is_type("reply_end", reply=start["reply"]), f"session 2, reply {turn} ends", mark)
            assert deck.reply_audio_ms.get(start["reply"]) == 1200  # the whole reply (fake: 1.2 s), nothing cut
            await deck.until(tool_applied("resume_script"), f"session 2, reply {turn}: resume_script", mark)
            current = await deck.until(is_type("play"), f"session 2, narration resumes after reply {turn}", mark)
        second = [e["type"] for e in connector.connections[1].received]
        assert "response.cancel" not in second
        assert [m["message"] for m in deck.log if m.get("type") == "error"] == []
        check_tool_outputs(connector)

    entries = log_entries(tmp_path)
    bound = [e["response_id"] for e in entries if e["kind"] == "reply_start"]
    assert bound == ["resp_fake000001", "resp_fake000002", "resp_fake000003", "resp_fake000001", "resp_fake000002"]
    assert not any(e["kind"] == "response_rejected" for e in entries)


async def test_a_slow_reply_that_keeps_streaming_is_not_cut_by_the_stall_watchdog(tmp_path, monkeypatch):
    """The reply-stall watchdog ends a reply whose events stop (design §5.2 timers), never one that is merely slow:
    every event of the reply restarts it. Here the watchdog is shortened to 0.4 s and the opening reply streams for
    ~1.7 s, an event every ~170 ms."""
    monkeypatch.setattr(controller_module, "REPLY_STALL_S", 0.4)
    connector = FakeConnector(think_s=0.05, speed=0.12, reply_s=0.2)  # 10 deltas of 20 ms, one per ~167 ms
    async with running_app(tmp_path, connector) as (app, port), armed_deck(port) as deck:
        mark = deck.mark()
        await deck.key("space")
        start = await deck.until(is_type("reply_start"), "the opening reply starts", mark)
        await deck.until(is_type("reply_end", reply=start["reply"]), "the reply ends", mark)
        assert deck.reply_audio_ms.get(start["reply"]) == 200  # every delta arrived: nothing was cut
        await deck.until(tool_applied("start_presentation"), "start_presentation applied", mark)
        assert "response.cancel" not in [e["type"] for e in connector.connections[-1].received]
        assert [m["message"] for m in deck.log if m.get("type") == "error"] == []
        check_tool_outputs(connector)
    assert not any(e["kind"] == "reply_stalled" for e in log_entries(tmp_path))


async def test_live_narration_end_to_end(tmp_path):
    """D52 over the real stack: the fake Realtime presents every relay slide (a narration request each, streamed as a
    `reply_start` kind narration). A Space mid-narration truncates it at what the deck played; the answer's
    resume_script continues the same slide live, quoting what the room heard; the narrations then run on to the Q&A
    slide, and a question there ends with the hand-back. Nothing of the backup (`play`, `cut`) is used."""
    connector = FakeConnector(think_s=0.05, speed=50.0)
    async with running_app(tmp_path, connector, narration="live") as (app, port), armed_deck(port) as deck:
        deck.hold_narration = True
        assert next(m for m in deck.log if m.get("type") == "hello")["narration_mode"] == "live"
        link = connector.connections[-1]

        mark = deck.mark()
        await deck.key("space")  # the opening: "take it away" → start_presentation
        await deck.until(tool_applied("start_presentation"), "start_presentation applied", mark)
        shown = await deck.until(is_type("slide", slide="how"), "the first relay slide", mark)
        assert shown["fragment"] is None  # a live narration shows the whole slide
        first = await deck.until(is_type("reply_start", kind="narration"), "the narration of 'how'", mark)
        await deck.until(lambda m: m.get("type") == "state" and m.get("narration") == "live", "state: live", mark)
        await deck.until(is_type("reply_end", reply=first["reply"]), "its audio is all in", mark)
        assert deck.reply_audio_ms[first["reply"]] == 2400  # the fake's narration_s

        sent_from = len(link.received)
        mark = deck.mark()
        await deck.key("space", reply=first["reply"], played=1000)  # Ken cuts in a second into it
        await deck.until(is_type("reply_flush", reply=first["reply"]), "the narration is cut", mark)
        await deck.until(is_type("mic", open=True), "mic opened for the cut-in", mark)
        answer = await deck.until(is_type("reply_start", kind="answer"), "the answer", mark)
        await deck.until(tool_applied("resume_script"), "resume_script applied", mark)
        again = await deck.until(is_type("reply_start", kind="narration"), "the slide continues live", mark)
        assert first["reply"] < answer["reply"] < again["reply"]
        truncates = [e for e in link.received[sent_from:] if e["type"] == "conversation.item.truncate"]
        assert [e["audio_end_ms"] for e in truncates] == [1000]
        request = [e for e in link.received if is_narration_request(e)][-1]["response"]
        assert request["metadata"] == {"relay": "narration", "reply": str(again["reply"]), "slide": "how"}
        assert request["tool_choice"] == "none"
        # 1000 of 2400 ms of "(fake) Live narration of slide how.": the first two of its six words
        assert 'the room last heard you say: "(fake) Live"' in request["instructions"]
        assert 'Start with a short bridge like "As I was saying"' in request["instructions"]

        await deck.until(is_type("reply_end", reply=again["reply"]), "the rest of 'how' is in", mark)
        mark = deck.mark()
        await deck.drain(again["reply"])
        await deck.until(is_type("slide", slide="qa"), "on to the Q&A slide", mark)
        qa = await deck.until(is_type("reply_start", kind="narration"), "the Q&A slide's narration", mark)
        await deck.until(is_type("reply_end", reply=qa["reply"]), "its audio is all in", mark)
        mark = deck.mark()
        await deck.drain(qa["reply"])
        await deck.until(lambda m: m.get("type") == "state" and m.get("state") == "IDLE" and m.get("phase") == "qa",
                         "waiting for a question", mark)

        mark = deck.mark()
        await deck.key("space")
        await deck.until(tool_applied("handoff_to_human"), "handoff_to_human applied", mark)
        assert not any(m.get("type") in ("play", "cut", "chunk_ended") for m in deck.log)
        assert [e["response"]["metadata"]["slide"] for e in link.received if is_narration_request(e)] == \
            ["how", "how", "qa"]
        check_tool_outputs(connector)

    entries = log_entries(tmp_path)
    assert [e["slide"] for e in entries if e["kind"] == "narration_start"] == ["how", "how", "qa"]
    assert [e["played_ms"] for e in entries if e["kind"] == "narration_cut"] == [1000]
    assert not any(e["kind"] == "narration_failed" for e in entries)
