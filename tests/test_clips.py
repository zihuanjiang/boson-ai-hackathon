"""relay.clips (contract v3.1 §2): generating the prerecorded participant clips (fake TTS, cache, recordings), the
server's snapshot and `/clips` route, the `demo` object of /api/talk and hello, and the input setting (--input,
RELAY_INPUT)."""

from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay import __main__ as cli
from relay import clips as clip_store
from relay.config import Settings, input_mode, load_settings
from relay.meeting import load_demo
from relay.server import create_app, talk_json
from relay.talk import load_talk
from relay.tts import TTSError, fake_synthesize, pcm_to_wav
from tests.conftest import TEST_TALK_DIR, synthetic_chunks
from tests.test_demo_e2e import DEMO_YAML, write_demo_meeting

CLIPS = ["alice-question", "bob-announce", "carol-finalize", "carol-correct"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("relay.config.load_dotenv", lambda *args, **kwargs: False)
    for name in ("BOSON_API_KEY", "RELAY_INPUT", "RELAY_FAKE_REALTIME", "RELAY_NARRATION", "MIC_MODE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def talk_dir(tmp_path: Path) -> Path:
    return write_demo_meeting(tmp_path / "packages" / "meeting")


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "out"


def wav_info(data: bytes) -> tuple[int, int, int, int]:
    with wave.open(io.BytesIO(data), "rb") as clip:
        return clip.getnchannels(), clip.getsampwidth(), clip.getframerate(), clip.getnframes()


def real_synth(calls: list[tuple[str, str]]):
    def synth(say: str, voice: str) -> bytes:
        calls.append((say, voice))
        return fake_synthesize(say, voice).wav
    return synth


def generate(talk_dir: Path, out: Path, **kwargs) -> clip_store.ClipsReport:
    talk = load_talk(talk_dir)
    return clip_store.generate_clips(talk, load_demo(talk), Settings(), out_root=out, **kwargs)


# --- generating ------------------------------------------------------------------------------------------------


def test_fake_clips_are_24k_mono_pcm16_with_their_metadata_and_cached(talk_dir: Path, out: Path):
    report = generate(talk_dir, out, fake=True)
    assert report.ok and sorted(entry.step for entry in report.generated) == sorted(CLIPS)
    for step in CLIPS:
        wav_path, meta_path = clip_store.clip_paths("meeting", step, out)
        channels, width, rate, frames = wav_info(wav_path.read_bytes())
        assert (channels, width, rate) == (1, 2, 24_000) and frames > 24_000 // 2  # a plausible length
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["step"] == step and meta["fake"] is True and meta["voice"] in ("chloe", "jake", "eleanor")
        assert meta["hash"] == clip_store.clip_hash(meta["say"], meta["voice"])
    again = generate(talk_dir, out, fake=True)
    assert (len(again.generated), len(again.cached)) == (0, 4)


def test_a_real_run_replaces_fake_clips_and_a_fake_run_keeps_real_ones(talk_dir: Path, out: Path):
    generate(talk_dir, out, fake=True)
    calls: list[tuple[str, str]] = []
    report = generate(talk_dir, out, synth=real_synth(calls))
    assert len(report.generated) == 4 and sorted(voice for _, voice in calls) == ["chloe", "eleanor", "eleanor",
                                                                                   "jake"]
    assert not json.loads(clip_store.clip_paths("meeting", "bob-announce", out)[1].read_text())["fake"]
    kept = generate(talk_dir, out, fake=True)
    assert len(kept.cached) == 4 and any("kept the real TTS 3 audio of 4 clip(s)" in w for w in kept.warnings)
    forced = generate(talk_dir, out, fake=True, force=True)
    assert len(forced.generated) == 4


def test_a_changed_line_or_voice_is_generated_again(talk_dir: Path, out: Path):
    generate(talk_dir, out, fake=True)
    (talk_dir / "demo.yaml").write_text(DEMO_YAML.replace("Bob: jake", "Bob: marcus"), encoding="utf-8")
    report = generate(talk_dir, out, fake=True)
    assert [entry.step for entry in report.generated] == ["bob-announce"] and len(report.cached) == 3


def test_a_recording_overrides_the_generated_clip(talk_dir: Path, out: Path):
    (talk_dir / "clips").mkdir()
    recording = pcm_to_wav(b"\x10\x00" * 24_000)  # 1 s, mono 16-bit 24 kHz
    (talk_dir / "clips" / "alice-question.wav").write_bytes(recording)
    calls: list[tuple[str, str]] = []
    report = generate(talk_dir, out, synth=real_synth(calls))
    assert [(entry.step, entry.duration_ms) for entry in report.recorded] == [("alice-question", 1000)]
    assert "alice-question" not in [entry.step for entry in report.generated] and len(calls) == 3
    talk = load_talk(talk_dir)
    clips, warnings = clip_store.load_clips(talk, load_demo(talk), out)
    assert clips["alice-question"].body == recording and clips["alice-question"].recorded
    assert not clips["bob-announce"].recorded and warnings == []


def test_a_recording_in_the_wrong_format_is_an_error_naming_the_file(talk_dir: Path, out: Path, capsys):
    (talk_dir / "clips").mkdir()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setnchannels(2)
        clip.setsampwidth(2)
        clip.setframerate(48_000)
        clip.writeframes(b"\x00\x00" * 9600)
    (talk_dir / "clips" / "bob-announce.wav").write_bytes(buffer.getvalue())
    with pytest.raises(ValueError, match="bob-announce.wav.*mono 16-bit PCM at 24000 Hz"):
        generate(talk_dir, out, fake=True)
    assert clip_store.main([str(talk_dir), "--fake", "--out", str(out)], settings=Settings()) == 1
    assert "bob-announce.wav" in capsys.readouterr().out
    talk = load_talk(talk_dir)
    clips, warnings = clip_store.load_clips(talk, load_demo(talk), out)
    assert "bob-announce" not in clips and any("bob-announce" in w for w in warnings)


def test_tts_trouble_never_raises(talk_dir: Path, out: Path):
    report = generate(talk_dir, out)  # no key, no synth
    assert report.fatal is not None and report.fatal.type == "missing_api_key" and len(report.skipped) == 4

    def wrong_rate(say: str, voice: str) -> bytes:
        return pcm_to_wav(b"\x00\x00" * 4800, rate=48_000)

    report = generate(talk_dir, out, synth=wrong_rate)
    assert len(report.failed) == 4 and "24000 Hz" in report.failed[0].error and not report.ok

    def billing(say: str, voice: str) -> bytes:
        raise TTSError("no credit", status=429, type="insufficient_quota", fatal=True)

    report = generate(talk_dir, out, synth=billing)
    assert report.fatal is not None and not report.ok


def test_tts_asks_for_a_plain_wav_and_falls_back_to_the_verified_timestamps_wav(monkeypatch: pytest.MonkeyPatch):
    from relay.tts import TTSResult

    calls: list[bool] = []

    def synthesize(say: str, voice: str, settings: Settings, *, timestamps: bool = True) -> TTSResult:
        calls.append(timestamps)
        rate = 24_000 if timestamps else 44_100  # what if the plain wav came back at another rate
        return TTSResult(pcm_to_wav(b"\x00\x00" * 2400, rate=rate), None)

    monkeypatch.setattr(clip_store, "synthesize", synthesize)
    wav = clip_store._tts_clip("Hi.", "jake", settings=Settings(api_key="bai-x"))
    assert calls == [False, True] and wav_info(wav)[2] == 24_000
    calls.clear()

    def plain(say: str, voice: str, settings: Settings, *, timestamps: bool = True) -> TTSResult:
        calls.append(timestamps)
        return TTSResult(pcm_to_wav(b"\x00\x00" * 2400), None)

    monkeypatch.setattr(clip_store, "synthesize", plain)
    clip_store._tts_clip("Hi.", "jake", settings=Settings(api_key="bai-x"))
    assert calls == [False]  # a plain 24 kHz wav is used as it is


def test_the_cli_exit_codes(talk_dir: Path, out: Path, tmp_path: Path, capsys):
    assert clip_store.main([str(talk_dir), "--fake", "--out", str(out)], settings=Settings()) == 0
    printed = capsys.readouterr().out
    assert "total: 4/4 clips (4 generated, 0 cached, 0 recorded)" in printed
    assert clip_store.main([str(talk_dir), "--out", str(out)], settings=Settings()) == 2  # fake ones need TTS 3
    assert "BOSON_API_KEY is empty" in capsys.readouterr().out
    calls: list[tuple[str, str]] = []
    assert clip_store.main([str(talk_dir), "--out", str(out)], settings=Settings(), synth=real_synth(calls)) == 0
    assert clip_store.main([str(TEST_TALK_DIR), "--out", str(out)], settings=Settings()) == 1  # no demo.yaml
    assert "no demo.yaml" in capsys.readouterr().out
    (talk_dir / "demo.yaml").write_text(DEMO_YAML.replace("speaker: Bob", "speaker: Mallory"), encoding="utf-8")
    assert clip_store.main([str(talk_dir), "--fake", "--out", str(out)], settings=Settings()) == 1


# --- the snapshot, /clips and the demo payload -------------------------------------------------------------------


def test_missing_and_stale_clips_are_left_out_with_warnings(talk_dir: Path, out: Path):
    talk = load_talk(talk_dir)
    clips, warnings = clip_store.load_clips(talk, load_demo(talk), out)
    assert clips == {} and any("4 participant clip(s) not generated" in w for w in warnings)
    generate(talk_dir, out, fake=True)
    (talk_dir / "demo.yaml").write_text(DEMO_YAML.replace("Carol: eleanor", "Carol: nora"), encoding="utf-8")
    talk = load_talk(talk_dir)
    clips, warnings = clip_store.load_clips(talk, load_demo(talk), out)
    assert sorted(clips) == ["alice-question", "bob-announce"] and any("stale" in w for w in warnings)


def test_the_demo_payload_describes_every_step_and_its_clip(talk_dir: Path, out: Path):
    generate(talk_dir, out, fake=True)
    talk = load_talk(talk_dir)
    demo = load_demo(talk)
    clips, _ = clip_store.load_clips(talk, demo, out)
    payload = clip_store.demo_json(demo, clips, "clips")
    assert payload["input"] == "clips" and payload["voices"]["Bob"] == "jake"
    start, alice = payload["steps"][:2]
    assert start == {"id": "start", "speaker": None, "say": None, "action": "start",
                     "when": {"phase": "presentation", "timeout_ms": 90_000}, "audio": None, "duration_ms": 0,
                     "recorded": False, "fake": False}
    assert (alice["audio"], alice["recorded"], alice["fake"]) == ("/clips/alice-question.wav", False, True)
    assert alice["duration_ms"] == clips["alice-question"].duration_ms > 0
    assert clip_store.demo_json(None, {}, "mic") is None


def app_for(talk_dir: Path, out: Path, web: Path, **settings) -> object:
    talk = load_talk(talk_dir)
    return create_app(talk_dir, Settings(**settings), out_root=out, chunks=synthetic_chunks(talk), web_dir=web)


@pytest.fixture
def web(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>deck</title>", encoding="utf-8")
    return root


def test_the_server_serves_the_clip_snapshot_and_describes_it(talk_dir: Path, out: Path, web: Path):
    generate(talk_dir, out, fake=True)
    app = app_for(talk_dir, out, web, fake_realtime=True)
    assert app.state.input == "clips" and app.state.settings.input == "clips"
    connect = app.state.link._connect
    assert connect.options.transcripts == tuple(step["say"] for step in app.state.demo["steps"] if step["say"])
    with TestClient(app) as client:
        talk = client.get("/api/talk").json()
        assert talk["input"] == "clips" and talk["demo"] == app.state.demo
        alice = next(step for step in talk["demo"]["steps"] if step["id"] == "alice-question")
        served = client.get(alice["audio"])
        assert served.status_code == 200 and served.headers["content-type"] == "audio/wav"
        assert served.content == clip_store.clip_paths("meeting", "alice-question", out)[0].read_bytes()
        assert client.get(alice["audio"], headers={"if-none-match": served.headers["etag"]}).status_code == 304
        assert client.get("/clips/nobody.wav").status_code == 404
        assert client.get("/clips/..%2Fsecret.wav").status_code == 404
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["demo"] == talk["demo"] and hello["input"] == "clips"
    # the snapshot: a clip generated again while the server runs changes nothing it serves
    assert talk_json(app.state.talk, app.state.chunks, app.state.settings, demo=app.state.demo) == talk


def test_a_demo_with_missing_clips_still_starts(talk_dir: Path, out: Path, web: Path):
    app = app_for(talk_dir, out, web)
    assert app.state.clips == {} and any("not generated" in w for w in app.state.clip_warnings)
    assert all(step["audio"] is None for step in app.state.demo["steps"])


def test_input_mic_on_a_demo_talk(talk_dir: Path, out: Path, web: Path):
    generate(talk_dir, out, fake=True)
    app = app_for(talk_dir, out, web, input="mic", fake_realtime=True)
    assert app.state.input == "mic" and app.state.controller.input_mode == "mic"
    assert app.state.demo["input"] == "mic"  # the script is still described; the page doesn't play it
    assert not hasattr(app.state.link._connect, "options")  # the plain fake (no participant transcripts)


def test_a_plain_talk_has_no_demo(tmp_path: Path, web: Path):
    app = create_app(TEST_TALK_DIR, Settings(), out_root=tmp_path / "out", web_dir=web,
                     chunks=synthetic_chunks(load_talk(TEST_TALK_DIR)))
    assert (app.state.demo, app.state.input, app.state.clips) == (None, "mic", {})


# --- the input setting -------------------------------------------------------------------------------------------


def test_the_input_setting(monkeypatch: pytest.MonkeyPatch):
    assert Settings().input is None and load_settings().input is None
    assert input_mode(Settings(), object()) == "clips" and input_mode(Settings(), None) == "mic"
    assert input_mode(Settings(input="mic"), object()) == "mic"
    monkeypatch.setenv("RELAY_INPUT", "MIC")
    assert load_settings().input == "mic"
    assert load_settings(input="clips").input == "clips"  # --input beats the environment
    monkeypatch.setenv("RELAY_INPUT", "loopback")
    with pytest.raises(ValueError, match="RELAY_INPUT"):
        load_settings()


def test_the_serve_command_takes_input_and_says_where_the_voices_come_from():
    args = cli.build_parser().parse_args(["serve", "talks/rollout-review", "--input", "mic"])
    assert args.input == "mic"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["serve", "x", "--input", "loopback"])
    demo = {"steps": [{"id": "a", "speaker": "Alice", "say": "Hi.", "audio": "/clips/a.wav"},
                      {"id": "b", "speaker": "Bob", "say": "Ho.", "audio": None}, {"id": "c", "action": "phase"}]}
    note = cli.input_note(demo, "clips")
    assert note.startswith("input: clips — an online meeting with prerecorded participants (Alice, Bob; 1/2 clips")
    assert "the microphone stays off" in note
    assert cli.input_note(demo, "mic").startswith("input: mic")
    assert cli.input_note(None, "mic") is None and "no demo.yaml" in cli.input_note(None, "clips")
