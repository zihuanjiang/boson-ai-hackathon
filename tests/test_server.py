"""relay.server and `python -m relay` with FastAPI's TestClient and a stub controller (offline, in-process)."""

from __future__ import annotations

import asyncio
import json
import time
import wave
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from relay import __main__ as cli
from relay import server
from relay.config import Settings
from relay.server import DeckHub, create_app, talk_json
from relay.talk import TalkError, load_talk
from relay.timeline import RenderMissing, chunk_paths
from tests.conftest import TEST_TALK_DIR, synthetic_chunks

ENV_VARS = ("BOSON_API_KEY", "BOSON_BASE_URL", "BOSON_REALTIME_URL", "MIC_MODE", "RELAY_TURN_DETECTION",
            "RELAY_TEMPERATURE", "RELAY_FULL_SESSION_UPDATE", "RELAY_FAKE_REALTIME", "RELAY_HOST", "RELAY_PORT",
            "RELAY_FAKE_DROP_AFTER")
VALID_SESSION = {"type": "realtime", "model": "higgs-realtime", "instructions": "You are Relay.\n\nPhase: opening",
                 "output_modalities": ["audio"],
                 "audio": {"input": {"turn_detection": {"type": "semantic_vad"},
                                     "transcription": {"model": "higgs-stt-3.1", "language": "en"}},
                           "output": {"voice": "chloe"}},
                 "tools": [], "tool_choice": "auto"}


class StubController:
    """Records every call; greets on connect and echoes each deck message back through the BrowserPort."""

    def __init__(self, talk: Any, chunks: Any, settings: Settings, *, browser: Any, realtime: Any,
                 log: Callable[[str, dict], None]) -> None:
        self.talk, self.chunks, self.settings = talk, chunks, settings
        self.browser, self.realtime, self.log = browser, realtime, log
        self.calls: list[tuple] = []
        self.statuses: list[tuple[str, str | None]] = []
        self.events: list[dict] = []

    async def on_browser_connected(self) -> None:
        self.calls.append(("connected",))
        await self.browser.send({"type": "hello", "talk": self.talk.id, "snapshot": self.snapshot()})

    async def on_browser(self, msg: dict) -> None:
        self.calls.append(("msg", msg))
        if msg.get("type") == "boom":
            raise RuntimeError("controller bug")
        await self.browser.send({"type": "echo", "msg": msg})

    async def on_browser_disconnected(self) -> None:
        self.calls.append(("disconnected",))

    async def on_realtime(self, event: dict) -> None:
        self.events.append(event)

    async def on_realtime_status(self, status: str, detail: str | None = None) -> None:
        self.statuses.append((status, detail))

    def session_config(self) -> dict:
        return json.loads(json.dumps(VALID_SESSION))

    def snapshot(self) -> dict:
        return {"state": "IDLE", "phase": "opening"}

    def messages(self) -> list[dict]:
        return [call[1] for call in self.calls if call[0] == "msg"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No .env and no RELAY_*/BOSON_* variables: every setting comes from the test."""
    monkeypatch.setattr("relay.config.load_dotenv", lambda *args, **kwargs: False)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def web(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    (root / "js").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Relay deck</title>", encoding="utf-8")
    (root / "js" / "main.js").write_text("export const ok = true;\n", encoding="utf-8")
    return root


def write_wav(path: Path, ms: int = 100) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24_000)
        out.writeframes(b"\x00\x00" * (24 * ms))
    return path.read_bytes()


def build(tmp_path: Path, web: Path, settings: Settings | None = None, *, chunks: Any = "synthetic",
          **kwargs: Any) -> tuple[Any, StubController]:
    holder: dict[str, StubController] = {}

    def factory(*args: Any, **kw: Any) -> StubController:
        holder["controller"] = StubController(*args, **kw)
        return holder["controller"]

    if chunks == "synthetic":
        chunks = synthetic_chunks(load_talk(TEST_TALK_DIR))
    app = create_app(TEST_TALK_DIR, settings or Settings(), out_root=tmp_path / "out", controller_factory=factory,
                     chunks=chunks, web_dir=web, **kwargs)
    return app, holder["controller"]


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)


def log_records(tmp_path: Path) -> list[dict]:
    (path,) = (tmp_path / "out" / "logs").glob("test-*.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- HTTP ---------------------------------------------------------------------------------------------------

def test_deck_page_static_files_and_slides(tmp_path: Path, web: Path) -> None:
    app, _ = build(tmp_path, web)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200 and "Relay deck" in page.text
        assert page.headers["content-type"].startswith("text/html")
        assert page.headers["cache-control"] == "no-cache"
        assert client.get("/web/js/main.js").text == "export const ok = true;\n"
        assert client.get("/web/js/main.js").headers["cache-control"] == "no-cache"
        slides = client.get("/api/slides.md")
        assert slides.headers["content-type"] == "text/markdown; charset=utf-8"
        assert slides.text == (TEST_TALK_DIR / "slides.md").read_text(encoding="utf-8")
        assert client.get("/talk/kb.md").text == (TEST_TALK_DIR / "kb.md").read_text(encoding="utf-8")
        assert client.get("/favicon.ico").status_code == 204
        assert client.get("/nope.js").status_code == 404
        assert client.get("/docs").status_code == 404  # no OpenAPI pages (they load JS from a CDN)


def test_missing_deck_page_is_a_clear_404(tmp_path: Path) -> None:
    empty = tmp_path / "empty-web"
    empty.mkdir()
    app, _ = build(tmp_path, empty)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 404 and "index.html" in response.text


def test_api_talk_matches_the_design_shape(tmp_path: Path, web: Path) -> None:
    app, controller = build(tmp_path, web)
    with TestClient(app) as client:
        data = client.get("/api/talk").json()
    assert data == talk_json(controller.talk, controller.chunks, controller.settings)
    assert {key: data[key] for key in ("id", "presenter", "voice", "mic_mode", "fake_audio")} == {
        "id": "test", "presenter": "Ken", "voice": "chloe", "mic_mode": "tap", "fake_audio": True}
    title, how, qa = data["slides"]
    assert title == {"id": "title", "index": 0, "speaker": "human", "mode": None, "title": "Relay test talk",
                     "fragments": 0, "chunks": []}
    assert (how["id"], how["index"], how["speaker"], how["mode"], how["title"], how["fragments"]) == (
        "how", 1, "relay", None, "How it works", 3)
    assert [chunk["index"] for chunk in how["chunks"]] == [0, 1, 2]
    first = how["chunks"][0]
    assert set(first) == {"index", "text", "fragment", "duration_ms", "words", "sentences", "audio"}
    assert first["text"].startswith("Here is how this works.") and "<|" not in first["text"]
    assert first["fragment"] == 1 and first["audio"] == "/media/how/0.wav"
    assert first["duration_ms"] == controller.chunks[("how", 0)].duration_ms
    assert first["words"][0] == {"word": "Here", "start": 150, "end": 450, "sentence": 0}
    assert first["sentences"] == ["Here is how this works.",
                                  "Everything you hear from me right now was written and rendered ahead of time."]
    assert qa["mode"] == "qa" and qa["chunks"][0]["audio"] == "/media/qa/0.wav"


def test_fake_audio_is_false_when_every_chunk_is_real(tmp_path: Path, web: Path) -> None:
    chunks = synthetic_chunks(load_talk(TEST_TALK_DIR))
    for chunk in chunks.values():
        chunk.fake = False
    app, _ = build(tmp_path, web, chunks=chunks, settings=Settings(mic_mode="hold"))
    with TestClient(app) as client:
        data = client.get("/api/talk").json()
    assert data["fake_audio"] is False and data["mic_mode"] == "hold"


def test_media_serves_only_rendered_chunks_of_this_talk(tmp_path: Path, web: Path) -> None:
    chunks = synthetic_chunks(load_talk(TEST_TALK_DIR))
    wav = tmp_path / "renders" / "how-0.wav"
    body = write_wav(wav)
    chunks[("how", 0)].wav_path = wav
    chunks[("how", 1)].wav_path = tmp_path / "renders" / "missing.wav"  # listed but not on disk
    app, _ = build(tmp_path, web, chunks=chunks)
    with TestClient(app) as client:
        response = client.get("/media/how/0.wav")
        assert response.status_code == 200 and response.content == body
        assert response.headers["content-type"] == "audio/wav"
        for url in ("/media/how/1.wav",  # file missing
                    "/media/how/2.wav",  # no wav (synthetic)
                    "/media/how/9.wav", "/media/title/0.wav", "/media/nope/0.wav",  # not a rendered chunk
                    "/media/how/0.mp3", "/media/how/x.wav", "/media/how/-1.wav", "/media/how/0.wav.json"):
            assert client.get(url).status_code == 404, url


@pytest.mark.parametrize("url", [
    "/media/..%2F..%2Fpyproject.toml/0.wav",
    "/media/how/..%2F..%2F..%2Fpyproject.toml",
    "/media/%2e%2e/%2e%2e/pyproject.toml",
    "/talk/%2e%2e/%2e%2e/pyproject.toml",
    "/talk/..%2F..%2Fpyproject.toml",
    "/web/%2e%2e/%2e%2e/pyproject.toml",
    "/%2e%2e/%2e%2e/pyproject.toml",
])
def test_no_path_traversal(tmp_path: Path, web: Path, url: str) -> None:
    app, _ = build(tmp_path, web)
    with TestClient(app) as client:
        response = client.get(url)
    assert response.status_code == 404 and "[project]" not in response.text


def test_healthz_reports_link_and_controller(tmp_path: Path, web: Path) -> None:
    app, controller = build(tmp_path, web)  # no key, not fake: the link reports the missing key at startup
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True, "realtime": "error", "state": "IDLE",
                                                 "detail": "BOSON_API_KEY missing", "deck": False}
    assert controller.statuses[0] == ("error", "BOSON_API_KEY missing")
    assert controller.statuses[-1] == ("offline", None)  # stopped on shutdown


def test_create_app_rejects_bad_talks_and_missing_renders(tmp_path: Path, web: Path) -> None:
    with pytest.raises(TalkError):
        create_app(tmp_path / "no-such-talk", Settings(), out_root=tmp_path / "out", web_dir=web)
    with pytest.raises(RenderMissing):
        create_app(TEST_TALK_DIR, Settings(), out_root=tmp_path / "out", web_dir=web)


def test_create_app_loads_renders_from_out_root(tmp_path: Path, web: Path) -> None:
    out = tmp_path / "out"
    talk = load_talk(TEST_TALK_DIR)
    for (slide, index), chunk in synthetic_chunks(talk).items():
        wav, meta = chunk_paths(talk.id, slide, index, out)
        write_wav(wav)
        meta.write_text(json.dumps(chunk.to_json()), encoding="utf-8")
    holder: dict[str, StubController] = {}

    def factory(*args: Any, **kw: Any) -> StubController:
        holder["c"] = StubController(*args, **kw)
        return holder["c"]

    app = create_app(TEST_TALK_DIR, Settings(), out_root=out, controller_factory=factory, web_dir=web)
    with TestClient(app) as client:
        assert client.get("/media/qa/0.wav").status_code == 200
        assert client.get("/api/talk").json()["fake_audio"] is True
    assert len(holder["c"].chunks) == 4


# --- deck socket --------------------------------------------------------------------------------------------

def test_deck_socket_greets_forwards_messages_and_reports_the_disconnect(tmp_path: Path, web: Path) -> None:
    app, controller = build(tmp_path, web)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello == {"type": "hello", "talk": "test", "snapshot": {"state": "IDLE", "phase": "opening"}}
            assert controller.calls == [("connected",)]
            assert client.get("/healthz").json()["deck"] is True
            ws.send_json({"type": "hello", "client": "deck"})
            assert ws.receive_json() == {"type": "echo", "msg": {"type": "hello", "client": "deck"}}
            ws.send_text("not json")
            ws.send_text("[1, 2, 3]")
            ws.send_bytes(b"\xff\xfe")
            ws.send_bytes(b'{"type": "arm"}')
            assert ws.receive_json() == {"type": "echo", "msg": {"type": "arm"}}
            ws.send_json({"type": "key", "key": "space", "at": None, "reply": None, "reply_played_ms": None})
            assert ws.receive_json()["msg"]["key"] == "space"
            assert [msg["type"] for msg in controller.messages()] == ["hello", "arm", "key"]
        wait_for(lambda: controller.calls[-1] == ("disconnected",))
        assert client.get("/healthz").json()["deck"] is False
        client.portal.call(app.state.hub.send, {"type": "state"})  # no deck: dropped quietly
    kinds = [record["kind"] for record in log_records(tmp_path)]
    assert kinds.count("deck_bad_message") == 3
    assert {"server_start", "deck_connected", "deck_in", "deck_out", "deck_disconnected", "server_stop"} <= set(kinds)


def test_a_second_deck_replaces_the_first(tmp_path: Path, web: Path) -> None:
    app, controller = build(tmp_path, web)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as first:
            assert first.receive_json()["type"] == "hello"
            with client.websocket_connect("/ws") as second:
                assert second.receive_json()["type"] == "hello"
                with pytest.raises(WebSocketDisconnect) as closed:
                    first.receive_json()
                assert closed.value.code == 4000
                first.send_json({"type": "key", "key": "next"})  # a stale tab: ignored
                second.send_json({"type": "arm"})
                assert second.receive_json() == {"type": "echo", "msg": {"type": "arm"}}
                assert controller.calls[:3] == [("connected",), ("disconnected",), ("connected",)]
                assert controller.messages() == [{"type": "arm"}]
            wait_for(lambda: controller.calls[-1] == ("disconnected",))
        time.sleep(0.05)
    assert controller.calls.count(("disconnected",)) == 2  # once per deck, never twice for the replaced one
    assert controller.messages() == [{"type": "arm"}]


def test_controller_errors_do_not_close_the_socket(tmp_path: Path, web: Path) -> None:
    app, controller = build(tmp_path, web)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "boom"})
            ws.send_json({"type": "arm"})
            assert ws.receive_json() == {"type": "echo", "msg": {"type": "arm"}}
    assert any(record["kind"] == "server_error" and record["where"] == "on_browser" for record in log_records(tmp_path))


def test_mic_frames_and_reply_audio_are_summarised_in_the_log(tmp_path: Path, web: Path) -> None:
    app, controller = build(tmp_path, web)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            for _ in range(3):
                ws.send_json({"type": "mic", "pcm": "A" * 6400})
            ws.send_json({"type": "key", "key": "esc"})
            for _ in range(3):
                ws.receive_json()  # the three mic echoes
            ws.receive_json()
            for _ in range(4):
                client.portal.call(app.state.hub.send, {"type": "reply_audio", "reply": 1, "pcm": "B" * 100})
            client.portal.call(app.state.hub.send, {"type": "reply_end", "reply": 1})
            for _ in range(5):
                ws.receive_json()
    records = log_records(tmp_path)
    mic = [r for r in records if r["kind"] == "deck_mic"]
    assert len(mic) == 1 and mic[0]["frames"] == 3 and mic[0]["chars"] == 19200
    audio = [r for r in records if r["kind"] == "deck_reply_audio"]
    assert len(audio) == 1 and audio[0]["frames"] == 4 and audio[0]["chars"] == 400
    assert not any(r["kind"] == "deck_in" and r["msg"]["type"] == "mic" for r in records)
    assert not any(r["kind"] == "deck_out" and r["msg"]["type"] == "reply_audio" for r in records)


def test_deck_hub_without_a_deck_drops_sends() -> None:
    hub = DeckHub()
    assert asyncio.run(hub.send({"type": "state", "state": "IDLE"})) is None
    assert not hub.connected


class FakeSocket:
    """Just enough of starlette's WebSocket for DeckHub; `stall = True` makes it stop reading (a hung tab)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed: tuple[int, str] | None = None
        self.stall = False
        self.client = None

    async def accept(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        if self.stall:
            await asyncio.Event().wait()
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.stall:
            await asyncio.Event().wait()
        self.closed = (code, reason)

    async def receive(self) -> dict:
        return await self.inbox.get()

    def gone(self) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1001})


class HubController:
    def __init__(self, hub: DeckHub) -> None:
        self.hub = hub
        self.calls: list[str] = []

    async def on_browser_connected(self) -> None:
        self.calls.append("connected")
        await self.hub.send({"type": "hello"})

    async def on_browser(self, msg: dict) -> None:
        self.calls.append(f"msg {msg.get('type')}")

    async def on_browser_disconnected(self) -> None:
        self.calls.append("disconnected")


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.002)


async def test_a_deck_that_stops_reading_is_dropped_instead_of_blocking_the_controller() -> None:
    hub = DeckHub(send_timeout=0.05, close_timeout=0.05)
    controller, ws = HubController(hub), FakeSocket()
    serving = asyncio.create_task(hub.serve(ws, controller))
    await until(lambda: controller.calls == ["connected"])
    assert ws.sent == [{"type": "hello"}]

    ws.stall = True
    started = asyncio.get_running_loop().time()
    await hub.send({"type": "state"})  # bounded by send_timeout, never stuck
    assert asyncio.get_running_loop().time() - started < 1.0
    assert not hub.connected
    await until(lambda: controller.calls[-1] == "disconnected")
    await hub.send({"type": "state"})  # no deck now: dropped at once

    ws.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "key", "key": "next"})})
    ws.gone()
    await serving
    assert controller.calls == ["connected", "disconnected"]  # stale input ignored, one disconnect only

    ws.stall = False
    again = FakeSocket()  # the tab recovers and reconnects (4001 is not "replaced")
    serving = asyncio.create_task(hub.serve(again, controller))
    await until(lambda: controller.calls[-1] == "connected")
    assert again.sent == [{"type": "hello"}] and hub.connected
    again.gone()
    await serving


async def test_a_hung_deck_does_not_block_the_deck_that_replaces_it() -> None:
    hub = DeckHub(send_timeout=0.3, close_timeout=0.05)
    controller, old = HubController(hub), FakeSocket()
    serving_old = asyncio.create_task(hub.serve(old, controller))
    await until(lambda: controller.calls == ["connected"])
    old.stall = True
    stuck = asyncio.create_task(hub.send({"type": "play"}))  # e.g. the controller mid-send to the hung tab
    await asyncio.sleep(0.01)

    new = FakeSocket()
    serving_new = asyncio.create_task(hub.serve(new, controller))
    await until(lambda: controller.calls == ["connected", "disconnected", "connected"])
    assert new.sent == [{"type": "hello"}]  # greeted at once, not after the stuck send
    await stuck
    await asyncio.sleep(0.05)
    assert controller.calls == ["connected", "disconnected", "connected"] and hub.connected

    new.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "arm"})})
    await until(lambda: controller.calls[-1] == "msg arm")
    for ws in (old, new):
        ws.gone()
    await asyncio.gather(serving_old, serving_new)
    assert controller.calls[-1] == "disconnected" and controller.calls.count("disconnected") == 2


def test_fake_realtime_is_wired_to_the_controller(tmp_path: Path, web: Path) -> None:
    app, controller = build(tmp_path, web, settings=Settings(fake_realtime=True))
    with TestClient(app) as client:
        wait_for(lambda: client.get("/healthz").json()["realtime"] == "ready")
        wait_for(lambda: any(event["type"] == "session.created" for event in controller.events))
        assert controller.statuses[:2] == [("connecting", None), ("ready", None)]
        link = app.state.link
        assert client.portal.call(link.send, {"type": "response.create"}) is True
        wait_for(lambda: any(event["type"] == "response.done" for event in controller.events))
        done = [event for event in controller.events if event["type"] == "response.done"][0]
        assert done["response"]["status"] == "completed"
    assert controller.statuses[-1] == ("offline", None)
    kinds = [record["kind"] for record in log_records(tmp_path)]
    assert "realtime_out" in kinds and "realtime_in" in kinds and "realtime_audio" in kinds


def test_default_factory_builds_the_real_controller(tmp_path: Path, web: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default factory imports relay.controller.Controller lazily, with the design §5 signature."""
    seen: dict[str, Any] = {}

    class FakeModule:
        class Controller(StubController):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                seen["args"], seen["kwargs"] = args, kwargs
                super().__init__(*args, **kwargs)

    monkeypatch.setitem(__import__("sys").modules, "relay.controller", FakeModule)
    talk = load_talk(TEST_TALK_DIR)
    app = create_app(TEST_TALK_DIR, Settings(), out_root=tmp_path / "out", chunks=synthetic_chunks(talk), web_dir=web)
    assert isinstance(app.state.controller, FakeModule.Controller)
    assert [type(arg).__name__ for arg in seen["args"]] == ["Talk", "dict", "Settings"]
    assert set(seen["kwargs"]) == {"browser", "realtime", "log"}
    assert seen["kwargs"]["browser"] is app.state.hub and seen["kwargs"]["realtime"] is app.state.link


# --- python -m relay serve ----------------------------------------------------------------------------------

@pytest.fixture
def served(tmp_path: Path, web: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Patch create_app (test chunks, tmp out, stub controller) and uvicorn.run (never serve) for CLI tests."""
    calls: dict[str, Any] = {}
    real_create_app = server.create_app

    def fake_create_app(talk_dir: Any, settings: Settings, **kwargs: Any) -> Any:
        calls["settings"], calls["talk_dir"] = settings, talk_dir
        chunks = synthetic_chunks(load_talk(TEST_TALK_DIR)) if calls.get("chunks", True) else None
        return real_create_app(talk_dir, settings, out_root=tmp_path / "out", chunks=chunks,
                               controller_factory=StubController, web_dir=web)

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls["run"] = kwargs
        calls["app"] = app

    monkeypatch.setattr(server, "create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", fake_run)
    yield calls
    if "app" in calls:
        calls["app"].state.log.close()  # uvicorn never ran, so the lifespan never closed it


def test_cli_serves_with_flags(served: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["serve", "talks/test", "--port", "8123", "--fake-realtime", "--mic-mode", "hold"])
    assert code == 0
    assert served["run"] == {"host": "127.0.0.1", "port": 8123, "log_level": "info"}
    settings = served["settings"]
    assert settings.fake_realtime is True and settings.mic_mode == "hold" and settings.port == 8123
    assert Path(served["talk_dir"]).resolve() == TEST_TALK_DIR.resolve()
    out = capsys.readouterr().out
    assert "http://localhost:8123" in out and "fake Realtime" in out and "fake narration audio" in out
    assert "live replies disabled" not in out


def test_cli_warns_that_live_replies_need_a_key(served: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["serve", str(TEST_TALK_DIR)]) == 0
    out = capsys.readouterr().out
    assert "live replies disabled: Space gives the floor to Ken" in out
    assert served["run"] == {"host": "127.0.0.1", "port": 8000, "log_level": "info"}


def test_cli_flags_win_over_the_environment(served: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RELAY_PORT", "9000")
    monkeypatch.setenv("BOSON_API_KEY", "bai-from-env")
    assert cli.main(["serve", "test"]) == 0  # a bare talk name resolves under talks/
    assert served["settings"].port == 9000 and served["settings"].api_key == "bai-from-env"
    assert cli.main(["serve", "test", "--port", "9001"]) == 0
    assert served["run"]["port"] == 9001
    out = capsys.readouterr().out
    assert "bai-from-env" not in out and "live replies disabled" not in out


def test_cli_reports_talk_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("must not serve"))
    assert cli.main(["serve", str(tmp_path / "no-such-talk")]) == 1
    err = capsys.readouterr().err
    assert "talk package has errors" in err and "not a directory" in err


def test_cli_reports_missing_renders(served: dict[str, Any], capsys: pytest.CaptureFixture[str],
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("must not serve"))
    served["chunks"] = False  # create_app loads renders from an empty out dir
    assert cli.main(["serve", "talks/test"]) == 1
    err = capsys.readouterr().err
    assert "not fully rendered" in err and "relay.render talks/test" in err


def test_cli_reports_bad_settings(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("MIC_MODE", "open")
    assert cli.main(["serve", "talks/test"]) == 1
    assert "bad setting" in capsys.readouterr().err


def test_deck_url_and_notes() -> None:
    assert cli.deck_url(Settings()) == "http://localhost:8000"
    assert cli.deck_url(Settings(host="0.0.0.0", port=9)) == "http://localhost:9"
    assert cli.deck_url(Settings(host="192.168.1.5")) == "http://192.168.1.5:8000"
    talk = load_talk(TEST_TALK_DIR)
    notes = cli.startup_notes(talk, synthetic_chunks(talk), Settings(host="0.0.0.0", api_key="k"))
    assert any("mic only works" in note for note in notes)
    assert not any("live replies disabled" in note for note in notes)
