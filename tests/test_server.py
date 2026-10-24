"""relay.server and `python -m relay` with FastAPI's TestClient and a stub controller (offline, in-process)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
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
from relay.render import render_talk
from relay.server import (REPLACED_CLOSE_CODE, REPLACED_REASON, STALLED_CLOSE_CODE, STALLED_REASON, DeckHub,
                          create_app, talk_json)
from relay.talk import TalkError, load_talk
from relay.timeline import RenderMissing, chunk_paths, render_version
from relay.tts import wav_duration_ms
from tests.conftest import TEST_TALK_DIR, synthetic_chunks

ENV_VARS = ("BOSON_API_KEY", "BOSON_BASE_URL", "BOSON_REALTIME_URL", "MIC_MODE", "RELAY_TURN_DETECTION",
            "RELAY_TEMPERATURE", "RELAY_FULL_SESSION_UPDATE", "RELAY_FAKE_REALTIME", "RELAY_HOST", "RELAY_PORT",
            "RELAY_FAKE_DROP_AFTER", "RELAY_NARRATION", "RELAY_ALIGNMENT", "ANTHROPIC_API_KEY", "RELAY_ALIGNMENT_MODEL",
            "RELAY_ALIGNMENT_EFFORT", "RELAY_ALIGNMENT_TIMEOUT_S", "RELAY_ALIGNMENT_MAX_TOKENS")
LIVE_NOTE = "Relay: narration: live (Higgs Realtime presents every slide; the TTS 3 renders are the offline backup)"
BACKUP_ONLY = "live narration needs Higgs Realtime too: Relay will play the backup narration (the TTS 3 renders) only"
VALID_SESSION = {"type": "realtime", "model": "higgs-realtime", "instructions": "You are Relay.\n\nPhase: opening",
                 "output_modalities": ["audio"],
                 "audio": {"input": {"turn_detection": {"type": "semantic_vad"},
                                     "transcription": {"model": "higgs-stt-3.1", "language": "en"}},
                           "output": {"voice": "chloe"}},
                 "tools": [], "tool_choice": "auto"}


class StubController:
    """Records every call; greets on connect and echoes each deck message back through the BrowserPort."""

    def __init__(self, talk: Any, chunks: Any, settings: Settings, *, browser: Any, realtime: Any,
                 log: Callable[[str, dict], None], analyzer: Any = None) -> None:
        self.talk, self.chunks, self.settings = talk, chunks, settings
        self.browser, self.realtime, self.log, self.analyzer = browser, realtime, log, analyzer
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

    def greeting(self) -> list[dict]:
        """An observer's greeting bundle (read-only, no call recorded: observers never reach the controller)."""
        return [{"type": "hello", "talk": self.talk.id, "snapshot": self.snapshot()}, {"type": "stop"}]

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
          talk_dir: Path = TEST_TALK_DIR, **kwargs: Any) -> tuple[Any, StubController]:
    """The app with a stub controller. `chunks=None` loads the renders in `tmp_path/out` like `relay serve`."""
    holder: dict[str, StubController] = {}

    def factory(*args: Any, **kw: Any) -> StubController:
        holder["controller"] = StubController(*args, **kw)
        return holder["controller"]

    if chunks == "synthetic":
        chunks = synthetic_chunks(load_talk(talk_dir))
    app = create_app(talk_dir, settings or Settings(), out_root=tmp_path / "out", controller_factory=factory,
                     chunks=chunks, web_dir=web, **kwargs)
    return app, holder["controller"]


def copy_test_talk(tmp_path: Path) -> Path:
    """A private copy of talks/test (some tests edit its script) under `tmp_path/talks/test`."""
    target = tmp_path / "talks" / "test"
    shutil.copytree(TEST_TALK_DIR, target)
    return target


def edit_script(talk_dir: Path, old: str, new: str) -> None:
    script = talk_dir / "script.yaml"
    text = script.read_text(encoding="utf-8")
    assert old in text
    script.write_text(text.replace(old, new), encoding="utf-8")


def fake_render(talk_dir: Path, out: Path) -> Any:
    """`python -m relay.render <talk> --fake` into `out` (offline, deterministic)."""
    report = render_talk(load_talk(talk_dir), Settings(), fake=True, out_root=out)
    assert report.ok, report
    return report


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)


def log_records(tmp_path: Path, talk: str = "test") -> list[dict]:
    (path,) = (tmp_path / "out" / "logs").glob(f"{talk}-*.jsonl")
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
    assert {key: data[key] for key in ("id", "presenter", "voice", "mic_mode", "narration_mode", "fake_audio")} == {
        "id": "test", "presenter": "Ken", "voice": "chloe", "mic_mode": "tap", "narration_mode": "live",
        "fake_audio": True}
    # what this server run presents: the deck compares it with the controller's hello after a reconnect
    assert data["version"] == render_version(controller.talk, controller.chunks) == app.state.version
    assert re.fullmatch(r"[0-9a-f]{12}", data["version"])
    title, how, qa = data["slides"]
    assert title == {"id": "title", "index": 0, "speaker": "human", "mode": None, "title": "Relay test talk",
                     "fragments": 0, "chunks": []}
    assert (how["id"], how["index"], how["speaker"], how["mode"], how["title"], how["fragments"]) == (
        "how", 1, "relay", None, "How it works", 3)
    assert [chunk["index"] for chunk in how["chunks"]] == [0, 1, 2]
    assert [chunk["id"] for chunk in how["chunks"]] == ["how-1", "how-2", "how-3"]  # the talking point ids
    assert data["meeting"] is None  # a plain talk (no agenda.yaml)
    first = how["chunks"][0]
    assert set(first) == {"index", "id", "text", "fragment", "duration_ms", "words", "sentences", "audio"}
    assert first["text"].startswith("Here is how this works.") and "<|" not in first["text"]
    assert first["fragment"] == 1 and first["audio"] == "/media/how/0.wav"
    assert first["duration_ms"] == controller.chunks[("how", 0)].duration_ms
    assert first["words"][0] == {"word": "Here", "start": 150, "end": 450, "sentence": 0}
    assert first["sentences"] == ["Here is how this works.",
                                  "Everything you hear from me right now was written and rendered ahead of time."]
    assert qa["mode"] == "qa" and qa["chunks"][0]["audio"] == "/media/qa/0.wav"


def test_api_talk_carries_a_meeting_package(tmp_path: Path, web: Path) -> None:
    """Contract v3 §6: /api/talk's `meeting` (title, host, participants, phases, documents with statement ids, the
    discussion slide), the same object as the controller's hello; chunks carry their talking point ids."""
    from tests.test_meeting_flow import write_meeting

    talk_dir = write_meeting(tmp_path / "talks" / "meeting")
    app, _ = build(tmp_path, web, talk_dir=talk_dir)
    with TestClient(app) as client:
        data = client.get("/api/talk").json()
    meeting = data["meeting"]
    assert (meeting["title"], meeting["host"], meeting["discussion_slide"]) == (
        "Release 4.3 rollout review", "Alice", "discussion")
    assert meeting["participants"][1] == {"name": "Bob", "role": "Customer communications"}
    assert [phase["id"] for phase in meeting["phases"]] == ["presentation", "discussion", "wrapup"]
    assert meeting["phases"][1]["prompt"] == "What goes out on Friday, and who sends it?"
    assert [(doc["id"], doc["title"]) for doc in meeting["documents"]] == [("decisions", "Decisions log"),
                                                                         ("requirements", "Rollout plan v3")]
    assert [s["id"] for s in meeting["documents"][1]["statements"]] == ["REQ-1", "REQ-2", "REQ-3"]
    assert [chunk["id"] for chunk in data["slides"][1]["chunks"]] == ["friday-beta", "wednesday-production"]


def test_a_broken_meeting_package_refuses_to_start(tmp_path: Path, web: Path) -> None:
    from tests.test_meeting_flow import write_meeting

    talk_dir = write_meeting(tmp_path / "talks" / "meeting")
    agenda = talk_dir / "agenda.yaml"
    agenda.write_text(agenda.read_text(encoding="utf-8").replace("host: Alice", "host: Mallory"), encoding="utf-8")
    with pytest.raises(TalkError, match="not a participant"):
        create_app(talk_dir, Settings(), out_root=tmp_path / "out", web_dir=web,
                   chunks=synthetic_chunks(load_talk(TEST_TALK_DIR)))


def test_the_cli_describes_a_meeting_and_warns_that_server_vad_is_ignored(tmp_path: Path) -> None:
    from relay.meeting import load_meeting
    from tests.test_meeting_flow import write_meeting

    talk = load_talk(write_meeting(tmp_path / "meeting"))
    meeting = load_meeting(talk)
    note = cli.meeting_note(meeting)
    assert note.startswith('meeting: "Release 4.3 rollout review" hosted by Alice, 3 participants, 5 source statements '
                           "in 2 document(s); agenda presentation → discussion → wrapup")
    assert cli.meeting_note(None) is None
    vad = Settings(turn_detection={"type": "semantic_vad"}, fake_realtime=True)
    notes = cli.startup_notes(talk, synthetic_chunks(talk), vad, meeting)
    assert any("RELAY_TURN_DETECTION is ignored for a meeting" in line for line in notes)
    assert not any("RELAY_TURN_DETECTION" in line for line in cli.startup_notes(talk, synthetic_chunks(talk), vad))


def test_fake_audio_is_false_when_every_chunk_is_real(tmp_path: Path, web: Path) -> None:
    chunks = synthetic_chunks(load_talk(TEST_TALK_DIR))
    for chunk in chunks.values():
        chunk.fake = False
    app, _ = build(tmp_path, web, chunks=chunks, settings=Settings(mic_mode="hold", narration="rendered"))
    with TestClient(app) as client:
        data = client.get("/api/talk").json()
    assert data["fake_audio"] is False and data["mic_mode"] == "hold" and data["narration_mode"] == "rendered"


def test_media_serves_only_rendered_chunks_of_this_talk(tmp_path: Path, web: Path) -> None:
    chunks = synthetic_chunks(load_talk(TEST_TALK_DIR))
    wav = tmp_path / "renders" / "how-0.wav"
    body = write_wav(wav, chunks[("how", 0)].duration_ms)
    chunks[("how", 0)].wav_path = wav
    app, _ = build(tmp_path, web, chunks=chunks)
    with TestClient(app) as client:
        response = client.get("/media/how/0.wav")
        assert response.status_code == 200 and response.content == body
        assert response.headers["content-type"] == "audio/wav"
        assert response.headers["cache-control"] == "no-cache"  # the deck revalidates: cheap 304s
        etag = response.headers["etag"]
        for validator in (etag, f"W/{etag}", f'"other", {etag}', "*"):
            unchanged = client.get("/media/how/0.wav", headers={"if-none-match": validator})
            assert unchanged.status_code == 304 and unchanged.content == b"", validator
            assert unchanged.headers["etag"] == etag
        assert client.get("/media/how/0.wav", headers={"if-none-match": '"other"'}).content == body
        for url in ("/media/how/1.wav", "/media/how/2.wav",  # no wav (synthetic)
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
                                                 "detail": "BOSON_API_KEY missing", "deck": False, "observers": 0,
                                                 "alignment": {"mode": "claude", "model": "claude-opus-5-5",
                                                               "available": False}}
    assert controller.statuses[0] == ("error", "BOSON_API_KEY missing")
    assert controller.statuses[-1] == ("offline", None)  # stopped on shutdown


# --- the alignment check's wiring (contract v5 §0, §3) ------------------------------------------------------------

def test_claude_mode_without_a_key_builds_no_analyzer_and_says_unavailable(tmp_path: Path, web: Path) -> None:
    """No key: the server starts, the controller gets no analyzer, /api/talk and /healthz say `unavailable`; the
    legacy engine is never a fallback."""
    from tests.test_meeting_flow import write_meeting

    talk_dir = write_meeting(tmp_path / "talks" / "meeting")
    app, controller = build(tmp_path, web, talk_dir=talk_dir)
    assert controller.analyzer is None and app.state.analyzer is None
    with TestClient(app) as client:
        data = client.get("/api/talk").json()
        assert data["alignment"] == {"mode": "claude", "model": "claude-opus-5-5", "available": False}
        assert client.get("/healthz").json()["alignment"] == data["alignment"]
    start = next(record for record in log_records(tmp_path, "meeting") if record["kind"] == "server_start")
    assert start["alignment"] == {"mode": "claude", "model": "claude-opus-5-5", "key_set": False, "analyzer": None}
    assert "sk-ant" not in json.dumps(start)


def test_claude_mode_with_a_key_builds_the_claude_analyzer(tmp_path: Path, web: Path) -> None:
    from relay.semantic import ClaudeAnalyzer
    from tests.test_meeting_flow import write_meeting

    talk_dir = write_meeting(tmp_path / "talks" / "meeting")
    app, controller = build(tmp_path, web, Settings(anthropic_api_key="sk-ant-test", alignment_model="claude-opus-5-5"),
                            talk_dir=talk_dir)
    assert isinstance(controller.analyzer, ClaudeAnalyzer) and controller.analyzer.model == "claude-opus-5-5"
    with TestClient(app) as client:
        assert client.get("/api/talk").json()["alignment"] == {"mode": "claude", "model": "claude-opus-5-5",
                                                               "available": True}
    start = next(record for record in log_records(tmp_path, "meeting") if record["kind"] == "server_start")
    assert start["alignment"]["key_set"] is True and start["alignment"]["analyzer"] == "ClaudeAnalyzer"
    assert all("sk-ant-test" not in json.dumps(record) for record in log_records(tmp_path, "meeting"))  # never logged


def test_an_injected_analyzer_reaches_the_controller(tmp_path: Path, web: Path) -> None:
    from relay.semantic import FakeAnalyzer
    from tests.test_meeting_flow import write_meeting

    fake = FakeAnalyzer()
    talk_dir = write_meeting(tmp_path / "talks" / "meeting")
    app, controller = build(tmp_path, web, talk_dir=talk_dir, analyzer=fake)
    assert controller.analyzer is fake
    with TestClient(app) as client:
        assert client.get("/api/talk").json()["alignment"] == {"mode": "claude", "model": "claude-opus-5-5",
                                                               "available": True}
    app.state.log.close()


def test_legacy_and_off_modes_in_the_payloads(tmp_path: Path, web: Path) -> None:
    from tests.test_meeting_flow import write_meeting

    talk_dir = write_meeting(tmp_path / "talks" / "meeting")
    app, _ = build(tmp_path, web, Settings(alignment="legacy"), talk_dir=talk_dir)
    with TestClient(app) as client:
        assert client.get("/api/talk").json()["alignment"] == {"mode": "legacy", "model": None, "available": True}
    (talk_dir / "checks.yaml").unlink()  # legacy without a checks.yaml: nothing checks, and it says so (off)
    app, _ = build(tmp_path, web, Settings(alignment="legacy"), talk_dir=talk_dir)
    with TestClient(app) as client:
        assert client.get("/api/talk").json()["alignment"] == {"mode": "off", "model": None, "available": False}
    app, _ = build(tmp_path, web, Settings(alignment="off"), talk_dir=talk_dir)
    with TestClient(app) as client:
        assert client.get("/api/talk").json()["alignment"] == {"mode": "off", "model": None, "available": False}
    plain, _ = build(tmp_path, web)  # a plain talk: no alignment at all
    with TestClient(plain) as client:
        assert client.get("/api/talk").json()["alignment"] is None


def test_the_real_controller_reports_the_alignment_status(tmp_path: Path, web: Path) -> None:
    """The default factory passes the analyzer on; hello, /api/talk and /healthz carry the controller's own status."""
    from relay.semantic import FakeAnalyzer
    from tests.test_meeting_flow import write_meeting

    talk_dir = write_meeting(tmp_path / "talks" / "meeting")
    talk = load_talk(talk_dir)
    app = create_app(talk_dir, Settings(), out_root=tmp_path / "out", chunks=synthetic_chunks(talk), web_dir=web,
                     analyzer=FakeAnalyzer())
    controller = app.state.controller
    assert controller.alignment_mode == "claude" and controller.alignment_available
    with TestClient(app) as client:
        info = {"mode": "claude", "model": "fake-analyzer", "available": True}
        assert client.get("/api/talk").json()["alignment"] == info
        health = client.get("/healthz").json()["alignment"]
        assert {key: health[key] for key in info} == info and health["analyses"] == 0 and health["in_flight"] is False
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["alignment"] == info and hello["snapshot"]["alignment"]["mode"] == "claude"


ALIGNMENT_WIP = pytest.mark.xfail(strict=True, reason="alignment WIP (dae4f64, merged 2026-10-24): `relay serve` doesn't "
                                  "print the alignment note yet; the alignment-worker finishes it and drops this marker")


@ALIGNMENT_WIP
def test_the_cli_has_an_alignment_flag_and_warns_without_a_key(served: dict[str, Any],
                                                                capsys: pytest.CaptureFixture[str], tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_meeting_flow import write_meeting

    talk_dir = write_meeting(tmp_path / "talks" / "meeting")
    assert cli.main(["serve", str(talk_dir), "--fake-realtime"]) == 0
    assert served["settings"].alignment == "claude"
    out = capsys.readouterr().out
    assert f"  warning: {cli.NO_CLAUDE_KEY}" in out.splitlines()
    assert "Relay: alignment: Claude (claude-opus-5-5) unavailable: ANTHROPIC_API_KEY missing" in out.splitlines()
    assert cli.main(["serve", str(talk_dir), "--fake-realtime", "--alignment", "legacy"]) == 0
    assert served["settings"].alignment == "legacy"
    out = capsys.readouterr().out
    assert cli.NO_CLAUDE_KEY not in out and "alignment: legacy patterns (checks.yaml: 2 slot(s))" in out
    assert cli.main(["serve", str(talk_dir), "--fake-realtime", "--alignment", "off"]) == 0
    assert f"  warning: {cli.ALIGNMENT_OFF}" in capsys.readouterr().out.splitlines()
    monkeypatch.setenv("RELAY_ALIGNMENT", "legacy")
    assert cli.main(["serve", str(talk_dir), "--fake-realtime", "--alignment", "claude"]) == 0  # the flag wins
    assert served["settings"].alignment == "claude"
    capsys.readouterr()
    (talk_dir / "checks.yaml").unlink()
    assert cli.main(["serve", str(talk_dir), "--fake-realtime", "--alignment", "legacy"]) == 0
    assert f"  warning: {cli.NO_CHECKS}" in capsys.readouterr().out.splitlines()
    assert cli.main(["serve", "talks/test", "--fake-realtime"]) == 0  # a plain talk: no alignment line at all
    assert "alignment" not in capsys.readouterr().out


@ALIGNMENT_WIP
def test_the_cli_describes_claude_alignment_with_a_key(served: dict[str, Any], capsys: pytest.CaptureFixture[str],
                                                        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_meeting_flow import write_meeting

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    talk_dir = write_meeting(tmp_path / "talks" / "meeting")
    assert cli.main(["serve", str(talk_dir), "--fake-realtime"]) == 0
    out = capsys.readouterr().out
    assert "Relay: alignment: Claude (claude-opus-5-5, effort low, timeout 20 s) reads the documents" in out
    assert "separate Anthropic billing" in out and "sk-ant-test" not in out and "warning: alignment" not in out


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
        write_wav(wav, chunk.duration_ms)
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


HOW_0 = "Here is how this works. Everything"
HOW_0_LONGER = "Here is how this works, in a few more words than last time. Everything"
HOW_2 = ('      - say: "When someone cuts in, I stop on the word, answer live, and then pick up again from the '
         'start of that sentence."\n        fragment: 3\n')


def test_narration_is_a_snapshot_until_the_server_restarts(tmp_path: Path, web: Path) -> None:
    """Design §3: a re-render under a running server (an edited line, a slide cut short) changes nothing it serves.
    Audio, `/api/talk` and the controller's word timings stay one render until a restart presents the new one."""
    talk_dir, out = copy_test_talk(tmp_path), tmp_path / "out"
    fake_render(talk_dir, out)
    app, controller = build(tmp_path, web, chunks=None, talk_dir=talk_dir)
    with TestClient(app) as client:
        before = client.get("/api/talk").json()
        served = {n: client.get(f"/media/how/{n}.wav") for n in range(3)}

        edit_script(talk_dir, HOW_0, HOW_0_LONGER)  # Ken edits a line and drops another, then re-renders
        edit_script(talk_dir, HOW_2, "")
        report = fake_render(talk_dir, out)
        assert [entry.label for entry in report.rendered] == ["how/0"]
        assert not chunk_paths("test", "how", 2, out)[0].exists()  # the render removed the dropped chunk

        assert client.get("/api/talk").json() == before
        how = before["slides"][1]["chunks"]
        for n in range(3):
            again = client.get(f"/media/how/{n}.wav")
            assert again.status_code == 200 and again.content == served[n].content, f"how/{n}"
            assert again.headers["etag"] == served[n].headers["etag"]
            assert wav_duration_ms(again.content) == how[n]["duration_ms"] == controller.chunks[("how", n)].duration_ms
        revalidated = client.get("/media/how/0.wav", headers={"if-none-match": served[0].headers["etag"]})
        assert revalidated.status_code == 304

    restarted, controller = build(tmp_path, web, chunks=None, talk_dir=talk_dir)
    with TestClient(restarted) as client:
        after = client.get("/api/talk").json()
        assert after["version"] != before["version"]  # an open deck sees it in hello and reloads
        how = after["slides"][1]["chunks"]
        assert len(how) == 2 and how[0]["text"].startswith("Here is how this works, in a few more words")
        fresh = client.get("/media/how/0.wav", headers={"if-none-match": served[0].headers["etag"]})
        assert fresh.status_code == 200 and fresh.content != served[0].content
        assert wav_duration_ms(fresh.content) == how[0]["duration_ms"] == controller.chunks[("how", 0)].duration_ms
        assert client.get("/media/how/2.wav").status_code == 404


def test_a_wav_that_disagrees_with_its_timings_refuses_to_start(tmp_path: Path, web: Path) -> None:
    """A render that replaced a wav but not (yet) its timings must not reach the stage: the server refuses to start
    and says what to do."""
    talk_dir, out = copy_test_talk(tmp_path), tmp_path / "out"
    fake_render(talk_dir, out)
    wav, meta = chunk_paths("test", "how", 1, out)
    made_for = json.loads(meta.read_text(encoding="utf-8"))["duration_ms"]
    write_wav(wav, made_for + 1500)  # a new, longer take landed under the old timings
    with pytest.raises(RenderMissing) as refused:
        build(tmp_path, web, chunks=None, talk_dir=talk_dir)
    message = str(refused.value)
    assert f"how/1: the wav lasts {made_for + 1500} ms but its word timings were made for {made_for} ms" in message
    assert "relay.render talks/test --force --only how" in message
    assert "how/0" not in message and "qa/0" not in message

    write_wav(wav, made_for + 5)  # within the tolerance: same take, rounding
    app, _ = build(tmp_path, web, chunks=None, talk_dir=talk_dir)
    app.state.log.close()

    chunks = synthetic_chunks(load_talk(TEST_TALK_DIR))
    chunks[("qa", 0)].wav_path = tmp_path / "renders" / "missing.wav"  # listed, but gone from disk
    with pytest.raises(RenderMissing, match=r"qa/0: cannot read .*missing\.wav"):
        build(tmp_path, web, chunks=chunks)


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


def test_observers_get_the_decks_feed_and_never_drive_the_show(tmp_path: Path, web: Path) -> None:
    """`/ws/observe` (contract v5): the page the Zoom bot shares. It gets the greeting bundle, then a copy of every
    message the deck gets; its input is ignored but `hello`; it never replaces the deck or reaches the controller."""
    app, controller = build(tmp_path, web)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/observe") as observer:
            assert observer.receive_json() == {"type": "hello", "talk": "test",
                                               "snapshot": {"state": "IDLE", "phase": "opening"}}
            assert observer.receive_json() == {"type": "stop"}
            assert controller.calls == [] and client.get("/healthz").json() == {
                **client.get("/healthz").json(), "deck": False, "observers": 1}
            observer.send_json({"type": "key", "key": "space"})  # ignored: never reaches the controller
            observer.send_json({"type": "hello", "client": "observer"})  # greeted again
            assert observer.receive_json()["type"] == "hello"
            assert observer.receive_json() == {"type": "stop"}
            with client.websocket_connect("/ws") as deck:
                assert deck.receive_json()["type"] == "hello"
                assert controller.calls == [("connected",)]  # the observer did not count as a deck
                assert observer.receive_json()["type"] == "hello"  # the deck's greeting, copied like everything
                deck.send_json({"type": "arm"})
                assert deck.receive_json() == {"type": "echo", "msg": {"type": "arm"}}
                assert observer.receive_json() == {"type": "echo", "msg": {"type": "arm"}}  # the copy
                with client.websocket_connect("/ws/observe") as second:
                    assert second.receive_json()["type"] == "hello" and second.receive_json() == {"type": "stop"}
                    deck.send_json({"type": "key", "key": "next"})
                    assert deck.receive_json()["msg"]["key"] == "next"
                    assert observer.receive_json()["msg"]["key"] == "next"
                    assert second.receive_json()["msg"]["key"] == "next"
                    assert client.get("/healthz").json()["observers"] == 2
                wait_for(lambda: client.get("/healthz").json()["observers"] == 1)
            wait_for(lambda: controller.calls[-1] == ("disconnected",))  # the deck left; the observer stays
            client.portal.call(app.state.hub.send, {"type": "state", "state": "IDLE"})  # no deck: observers still get it
            assert observer.receive_json() == {"type": "state", "state": "IDLE"}
        wait_for(lambda: client.get("/healthz").json()["observers"] == 0)
    kinds = [record["kind"] for record in log_records(tmp_path)]
    assert {"observer_connected", "observer_disconnected"} <= set(kinds)
    assert [m["type"] for m in controller.messages()] == ["arm", "key"]  # nothing from the observers


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
    """Just enough of starlette's WebSocket on uvicorn's websocket protocol (sansio) for DeckHub.

    Like uvicorn, it writes nothing while the tab isn't reading, the close frame included: `stall = True` is a hung
    tab, `stall = False` a tab that reads again. Like Starlette, a close counts as sent the moment it starts, so a
    close that was cancelled half-way can never be sent again. Once the close is out, the app's `receive` ends.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed: tuple[int, str] | None = None
        self.client = None
        self._writable = asyncio.Event()
        self._writable.set()
        self._closing = False
        self._lost = False

    @property
    def stall(self) -> bool:
        return not self._writable.is_set()

    @stall.setter
    def stall(self, value: bool) -> None:
        if value:
            self._writable.clear()
        else:
            self._writable.set()

    async def accept(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        if self._closing:
            raise RuntimeError('Cannot call "send" once a close message has been sent.')
        await self._writable.wait()
        if self._lost:
            raise RuntimeError("the connection is gone")
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closing:
            raise RuntimeError('Cannot call "send" once a close message has been sent.')
        self._closing = True
        await self._writable.wait()
        if self._lost:
            raise RuntimeError("the connection is gone")
        self.closed = (code, reason)
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": code})

    async def receive(self) -> dict:
        return await self.inbox.get()

    def gone(self) -> None:
        """The tab is closed: the connection is lost, and writes waiting for it fail."""
        self._lost = True
        self._writable.set()
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


async def test_a_deck_that_stops_reading_is_dropped_and_gets_4001_whenever_it_reads_again() -> None:
    """D21: a hung tab never blocks the controller, which hears about it at once; the 4001 that makes the tab
    reconnect goes out when the tab reads again, however late, instead of being abandoned while it is hung."""
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
    await until(lambda: controller.calls[-1] == "disconnected", 1.0)  # while still hung: not after the close
    await hub.send({"type": "state"})  # no deck now: dropped at once

    await asyncio.sleep(0.3)  # hung for far longer than any close timeout
    ws.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "key", "key": "next"})})
    await asyncio.sleep(0.02)
    assert ws.closed is None  # nothing can be written to a hung tab
    ws.stall = False  # it reads again: its backlog, then the close
    await until(lambda: ws.closed == (STALLED_CLOSE_CODE, STALLED_REASON), 1.0)
    await serving
    assert controller.calls == ["connected", "disconnected"]  # its stale key was ignored, one disconnect only

    again = FakeSocket()  # so the tab reconnects (4001 is not "replaced") and drives the show again
    serving = asyncio.create_task(hub.serve(again, controller))
    await until(lambda: controller.calls[-1] == "connected")
    assert again.sent == [{"type": "hello"}] and hub.connected
    again.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "arm"})})
    await until(lambda: controller.calls[-1] == "msg arm")
    again.gone()
    await serving


async def test_a_dropped_deck_whose_close_failed_is_cut_off_when_it_talks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never a connected tab whose input is ignored: if the 4001 could not be sent, the handler ends the connection
    (the tab sees it close and reconnects) as soon as the dropped tab sends anything."""
    hub = DeckHub(send_timeout=0.05)
    controller, ws = HubController(hub), FakeSocket()
    serving = asyncio.create_task(hub.serve(ws, controller))
    await until(lambda: controller.calls == ["connected"])

    async def broken_close(code: int = 1000, reason: str = "") -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(ws, "close", broken_close)
    ws.stall = True
    await hub.send({"type": "state"})
    await until(lambda: controller.calls[-1] == "disconnected", 1.0)
    ws.stall = False
    await asyncio.sleep(0.02)
    ws.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "key", "key": "space"})})
    await asyncio.wait_for(serving, 1.0)  # the handler returned: uvicorn closes the connection
    assert controller.calls == ["connected", "disconnected"]


async def test_a_hung_deck_replaced_by_a_new_one_gets_4000_once_it_reads_again() -> None:
    hub = DeckHub(send_timeout=0.3, close_timeout=0.5)
    controller, old = HubController(hub), FakeSocket()
    serving_old = asyncio.create_task(hub.serve(old, controller))
    await until(lambda: controller.calls == ["connected"])
    old.stall = True

    new = FakeSocket()
    started = asyncio.get_running_loop().time()
    serving_new = asyncio.create_task(hub.serve(new, controller))
    await until(lambda: controller.calls == ["connected", "disconnected", "connected"])
    assert new.sent == [{"type": "hello"}]
    assert asyncio.get_running_loop().time() - started < 0.4  # greeted at once, not after the old deck's close
    await asyncio.sleep(0.6)  # the old tab stays hung past any close timeout
    assert old.closed is None
    old.stall = False
    await until(lambda: old.closed == (REPLACED_CLOSE_CODE, REPLACED_REASON), 1.0)  # it won't reconnect
    await serving_old
    assert hub.connected and controller.calls == ["connected", "disconnected", "connected"]
    new.gone()
    await serving_new


async def test_shutdown_gives_up_on_closes_still_waiting_for_a_hung_tab() -> None:
    hub = DeckHub(send_timeout=0.05, close_timeout=0.05)
    controller, ws = HubController(hub), FakeSocket()
    serving = asyncio.create_task(hub.serve(ws, controller))
    await until(lambda: controller.calls == ["connected"])
    ws.stall = True
    await hub.send({"type": "state"})
    await until(lambda: controller.calls[-1] == "disconnected", 1.0)
    await asyncio.wait_for(hub.close(), 1.0)  # bounded, even though the dropped tab never reads again
    assert ws.closed is None
    ws.gone()
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
    assert set(seen["kwargs"]) == {"browser", "realtime", "log", "analyzer"}
    assert seen["kwargs"]["browser"] is app.state.hub and seen["kwargs"]["realtime"] is app.state.link
    assert seen["kwargs"]["analyzer"] is None  # no key: no Claude adapter


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
    code = cli.main(["serve", "talks/test", "--port", "8123", "--fake-realtime", "--mic-mode", "hold",
                     "--narration", "rendered"])
    assert code == 0
    assert served["run"] == {"host": "127.0.0.1", "port": 8123, "log_level": "info"}
    settings = served["settings"]
    assert settings.fake_realtime is True and settings.mic_mode == "hold" and settings.port == 8123
    assert settings.narration == "rendered"
    assert Path(served["talk_dir"]).resolve() == TEST_TALK_DIR.resolve()
    out = capsys.readouterr().out
    assert "http://localhost:8123" in out and "fake Realtime" in out and "fake narration audio" in out
    assert "live replies disabled" not in out and "narration: live" not in out  # rendered: nothing new to say
    version = served["app"].state.version
    assert f"narration loaded into memory (0 wav files, 0.0 MB, version {version})" in out
    assert "a re-render takes effect at the next server restart" in out


def test_cli_says_relay_presents_live_by_default(served: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["serve", "talks/test", "--fake-realtime"]) == 0
    assert served["settings"].narration == "live"
    out = capsys.readouterr().out
    assert LIVE_NOTE in out.splitlines()
    assert BACKUP_ONLY not in out  # the fake Realtime presents the slides


def test_cli_warns_that_live_replies_need_a_key(served: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["serve", str(TEST_TALK_DIR)]) == 0
    out = capsys.readouterr().out
    assert "live replies disabled: Space gives the floor to Ken" in out
    assert f"  warning: {BACKUP_ONLY}" in out.splitlines()  # live narration without Realtime: the backup only
    assert served["run"] == {"host": "127.0.0.1", "port": 8000, "log_level": "info"}
    assert cli.main(["serve", str(TEST_TALK_DIR), "--narration", "rendered"]) == 0
    out = capsys.readouterr().out
    assert "live replies disabled" in out and BACKUP_ONLY not in out and "narration: live" not in out


def test_cli_narration_flag_wins_over_the_environment(served: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RELAY_NARRATION", "rendered")
    assert cli.main(["serve", "test", "--fake-realtime"]) == 0
    assert served["settings"].narration == "rendered" and LIVE_NOTE not in capsys.readouterr().out
    assert cli.main(["serve", "test", "--fake-realtime", "--narration", "live"]) == 0
    assert served["settings"].narration == "live" and LIVE_NOTE in capsys.readouterr().out
    monkeypatch.setenv("RELAY_NARRATION", "tts")
    assert cli.main(["serve", "test"]) == 1
    assert "bad setting: RELAY_NARRATION must be one of" in capsys.readouterr().err


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
