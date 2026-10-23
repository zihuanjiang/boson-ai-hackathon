"""relay.verify, offline: argument parsing, the no-key exit, WAV/PCM helpers, result formatting, the typed-turn
collector, error hints, and every Appendix F check driven end to end by a fake TTS endpoint and a fake Realtime
socket.

Nothing here touches the network: an autouse fixture makes `requests` and `websockets.connect` raise.
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
import math
import struct
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pytest
import requests
import websockets
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

import relay.config
from relay import verify
from relay.config import RATE, Settings
from relay.talk import load_talk
from relay.textutil import strip_tags
from relay.verify import CREDITS_URL, NO_KEY_MESSAGE, Ctx, Result, TimedWord, ToolCallSeen, VerifyError

KEY = "bai-secret-test-key-0123456789"
NOW = datetime(2026, 10, 23, 18, 5)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("tests must never touch the network")

    monkeypatch.setattr(requests.sessions.Session, "request", refuse)
    monkeypatch.setattr(websockets, "connect", refuse)


@pytest.fixture
def live_settings(tmp_path: Path) -> Settings:
    """A key (never sent anywhere: every check below gets fake connect/post) and a temporary out/ folder."""
    return Settings(api_key=KEY, out_dir=tmp_path)


def quiet(_: str) -> None:
    """Ctx.echo for tests."""


def tone(ms: int, *, rate: int = RATE, amplitude: int = 8000, hz: int = 220) -> bytes:
    count = rate * ms // 1000
    return verify._le_bytes(array.array(
        "h", (round(amplitude * math.sin(2 * math.pi * hz * i / rate)) for i in range(count))))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class TickClock:
    """Each call is `step` seconds after the previous one."""

    def __init__(self, step: float = 0.01):
        self.n, self.step = 0, step

    def __call__(self) -> float:
        self.n += 1
        return self.n * self.step


# ── fakes: TTS 3 endpoint ────────────────────────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, *, text: str | None = None):
        self.status_code = status
        self.ok = status < 400
        self._body = body
        self.text = text if text is not None else ("" if body is None else json.dumps(body))
        self.headers = {"content-type": "application/json" if body is not None else "text/plain"}

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def timed_words(text: str, *, pause_after: int | None = None, pause_ms: int = 80, word_ms: int = 200,
                gap_ms: int = 60) -> list[dict]:
    """TTS 3 `timestamps` (seconds) for the tag-free words of `text`, with `pause_ms` after word `pause_after`."""
    words, t = [], 100
    for i, word in enumerate(strip_tags(text).split()):
        words.append({"word": word, "start": t / 1000, "end": (t + word_ms) / 1000})
        t += word_ms + (pause_ms if i == pause_after else gap_ms)
    return words


def tts_body(text: str, *, audio_ms: int = 500, words: Any = "auto", wav: bytes | None = None) -> dict:
    return {"audio": b64(wav if wav is not None else verify.wav_bytes(tone(audio_ms))), "response_format": "wav",
            "input": text, "timestamps": timed_words(text) if words == "auto" else words}


class FakeTTS:
    """POST /v1/audio/speech with timestamps: a tone WAV + word timings, or whatever `respond` returns."""

    def __init__(self, respond: Callable[[dict], FakeResponse] | None = None, *, audio_ms: int = 500):
        self.calls: list[tuple[str, dict]] = []
        self.respond = respond or (lambda body: FakeResponse(200, tts_body(body["input"], audio_ms=audio_ms)))

    def __call__(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.respond(kwargs["json"])


# ── fakes: Higgs Realtime socket ─────────────────────────────────────────────────────────────────────────────


def merged(base: dict, update: dict) -> dict:
    out = json.loads(json.dumps(base))
    for key, value in update.items():
        out[key] = merged(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


class FakeRealtime:
    """One socket with the surface verify uses: send(str), recv() -> str, close(), close_code / close_reason."""

    def __init__(self, server: FakeServer, index: int):
        self.server, self.index = server, index
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.session: dict = {}
        self.user_texts: list[str] = []
        self.responses = 0
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.client_closed = False

    async def send(self, raw: str) -> None:
        if self.close_code is not None:
            raise ConnectionClosed(Close(self.close_code, self.close_reason or ""), None)
        event = json.loads(raw)
        self.sent.append(event)
        self.server.handle(self, event)

    async def recv(self) -> str:
        item = await self.inbox.get()
        if isinstance(item, tuple):  # ("closed", code, reason): every later recv fails the same way
            self.inbox.put_nowait(item)
            raise ConnectionClosed(Close(item[1], item[2]), None)
        return item

    async def close(self) -> None:
        self.client_closed = True
        if self.close_code is None:
            self.drop(1000)

    def push(self, event: dict) -> None:
        self.inbox.put_nowait(json.dumps(event))

    def drop(self, code: int, reason: str = "") -> None:
        """The server closes the socket."""
        self.close_code, self.close_reason = code, reason
        self.inbox.put_nowait(("closed", code, reason))

    def types(self) -> list[str]:
        return [event["type"] for event in self.sent]


class FakeServer:
    """Hands out FakeRealtime sockets and plays Higgs: acks session.update (created, then updated) and answers each
    response.create with audio deltas, a transcript and, if `tool` says so, one function_call."""

    def __init__(self, *, reply_ms: int = 600, transcript: Callable[[FakeRealtime, str], str] | None = None,
                 tool: Callable[[FakeRealtime, str], tuple[str, dict] | None] | None = None,
                 resolve: Callable[[dict], dict] | None = None, partial: Callable[[dict, dict], dict] | None = None,
                 ack: Callable[[dict], dict] | None = None, on_created: Callable[[FakeRealtime], None] | None = None,
                 refuse: Callable[[int], BaseException | None] | None = None, stale_ack: bool = False):
        self.reply_ms = reply_ms
        self.transcript = transcript or (lambda conn, said: "Sure, happy to.")
        self.tool = tool
        self.resolve = resolve or (lambda session: session)
        self.partial = partial or merged
        self.ack = ack or (lambda session: session)
        self.on_created = on_created
        self.refuse = refuse
        self.stale_ack = stale_ack
        self.connections: list[FakeRealtime] = []
        self.calls: list[tuple[str, dict]] = []

    async def connect(self, url: str, **kwargs: Any) -> FakeRealtime:
        self.calls.append((url, kwargs))
        error = self.refuse(len(self.calls)) if self.refuse else None
        if error is not None:
            raise error
        conn = FakeRealtime(self, len(self.connections))
        self.connections.append(conn)
        return conn

    def handle(self, conn: FakeRealtime, event: dict) -> None:
        kind = event["type"]
        if kind == "session.update":
            self.session_update(conn, event["session"])
        elif kind == "conversation.item.create":
            item = event["item"]
            if item.get("type") == "message":
                conn.user_texts.append(item["content"][0]["text"])
            conn.push({"type": "conversation.item.added", "item": {"type": item.get("type")}})
        elif kind == "response.create":
            self.reply(conn)

    def session_update(self, conn: FakeRealtime, update: dict) -> None:
        if not conn.session:
            conn.session = self.resolve(merged({"id": "sess_fake", "temperature": 0.7}, update))
            conn.push({"type": "session.created", "session": conn.session})
            if self.stale_ack:  # a late ack of the first update, arriving after session.created
                conn.push({"type": "session.updated", "session": conn.session})
            if self.on_created:
                self.on_created(conn)
            return
        conn.session = self.partial(conn.session, update)
        conn.push({"type": "session.updated", "session": self.ack(conn.session)})

    def reply(self, conn: FakeRealtime) -> None:
        conn.responses += 1
        rid = f"resp_{conn.index}_{conn.responses}"
        said = conn.user_texts[-1] if conn.user_texts else ""
        text = self.transcript(conn, said)
        conn.push({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
        conn.push({"type": "response.output_item.added", "response_id": rid})  # verify ignores it
        pcm = tone(self.reply_ms)
        for start in range(0, len(pcm), 960):  # 20 ms deltas
            conn.push({"type": "response.output_audio.delta", "response_id": rid, "item_id": f"item_{rid}",
                       "delta": b64(pcm[start:start + 960])})
        for word in text.split():
            conn.push({"type": "response.output_audio_transcript.delta", "response_id": rid, "delta": word + " "})
        conn.push({"type": "response.output_audio_transcript.done", "response_id": rid, "transcript": text})
        output: list[dict] = [{"type": "message", "role": "assistant"}]
        call = self.tool(conn, said) if self.tool else None
        if call is not None:
            name, args = call
            item = {"type": "function_call", "call_id": f"call_{rid}", "name": name, "arguments": json.dumps(args)}
            conn.push({"type": "response.function_call_arguments.done", "response_id": rid,
                       "call_id": item["call_id"], "name": name, "arguments": item["arguments"]})
            output.append(item)
        conn.push({"type": "response.done", "response": {"id": rid, "status": "completed", "output": output}})


def app_policy(target: str) -> Callable[[FakeRealtime, str], tuple[str, dict]]:
    """The model the app hopes for: the phase line in the current instructions and the request pick the tool."""

    def choose(conn: FakeRealtime, said: str) -> tuple[str, dict]:
        instructions = conn.session.get("instructions", "")
        if "Phase: opening" in instructions:
            return "start_presentation", {}
        if "Phase: qa" in instructions:
            return "handoff_to_human", {}
        if "skip to" in said:
            return "goto_slide", {"slide_id": target}
        return "resume_script", {}

    return choose


class ScriptedConn:
    """Replays server events (dicts, raw strings, or (code, reason) for a close) and records what was sent."""

    def __init__(self, *events: Any):
        self.events = list(events)
        self.sent: list[dict] = []
        self.send_error: BaseException | None = None
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send(self, raw: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self.events:
            await asyncio.Event().wait()  # nothing more: block until the caller's timeout
        item = self.events.pop(0)
        if isinstance(item, tuple):
            raise ConnectionClosed(Close(*item), None)
        return item if isinstance(item, str) else json.dumps(item)

    async def close(self) -> None:
        pass


def wav_info(path: Path) -> tuple[int, int, int, int]:
    with wave.open(str(path)) as wav:
        return wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()


# ── argument parsing ─────────────────────────────────────────────────────────────────────────────────────────


def parse(*argv: str):
    return verify.build_parser().parse_args(list(argv))


def test_parse_voices_options():
    args = parse("voices", "--voices", "Chloe, marcus,chloe", "--line", "  Hello   there.  ")
    assert args.check == "voices"
    assert verify.check_options("voices", args) == {"voices": ("chloe", "marcus"), "line": "Hello there."}


def test_parse_leaves_defaults_to_the_check():
    for check in ("voices", "tags", "latency", "session", "tools", "idle"):
        assert verify.check_options(check, parse(check)) == {}


def test_parse_numbers_voices_and_talks():
    assert verify.check_options("latency", parse("latency", "--runs", "3", "--voice", "Marcus")) == {
        "runs": 3, "voice": "marcus"}
    assert verify.check_options("tools", parse("tools", "--talk", "talks/test", "--runs", "4")) == {
        "talk": "talks/test", "runs": 4}
    assert parse("idle", "--wait", "12.5").wait == 12.5
    assert parse("idle", "--wait", "0").wait == 0.0


def test_parse_all_routes_voice_and_talk_to_the_checks_that_take_them():
    args = parse("all", "--voice", "marcus", "--talk", "talks/test")
    assert {name: verify.check_options(name, args) for name in verify.ALL_CHECKS} == {
        "voices": {}, "tags": {"voice": "marcus"}, "latency": {"voice": "marcus"},
        "session": {"talk": "talks/test"}, "tools": {"talk": "talks/test"}}
    assert "idle" not in verify.ALL_CHECKS


@pytest.mark.parametrize("argv", [
    [], ["banana"], ["voices", "--voices", "alloy"], ["voices", "--voices", " , "], ["voices", "--line", "x" * 301],
    ["voices", "--line", "   "], ["latency", "--runs", "0"], ["latency", "--runs", "two"], ["tools", "--runs", "-3"],
    ["idle", "--wait", "-1"], ["idle", "--wait", "nan"], ["idle", "--wait", "inf"], ["tags", "--talk", "x"],
    ["all", "--runs", "3"],
])
def test_parse_rejects_bad_arguments(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        parse(*argv)
    assert exc.value.code == 2


# ── main: the no-key exit and dispatch ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def env_without_dotenv(monkeypatch):
    """load_settings() on the environment only (a real .env on this machine must not leak into the test)."""
    monkeypatch.setattr(relay.config, "load_dotenv", lambda *args, **kwargs: False)
    for name in ("MIC_MODE", "RELAY_TURN_DETECTION", "RELAY_TEMPERATURE", "RELAY_PORT"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_main_without_a_key_prints_one_line_and_exits_2(env_without_dotenv, capsys):
    env_without_dotenv.setenv("BOSON_API_KEY", "")
    assert verify.main(["voices"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ("BOSON_API_KEY missing in .env: add it and claim credits at "
                            "https://www.boson.ai/workspace/api-key?open-credits=true\n")
    assert captured.err.strip() == NO_KEY_MESSAGE
    assert captured.out == ""


def test_main_with_a_bad_setting_exits_2_without_printing_the_key(env_without_dotenv, capsys):
    env_without_dotenv.setenv("BOSON_API_KEY", KEY)
    env_without_dotenv.setenv("MIC_MODE", "banana")
    assert verify.main(["tags"]) == 2
    captured = capsys.readouterr()
    assert "MIC_MODE" in captured.err
    assert KEY not in captured.err + captured.out


def test_main_runs_the_named_checks_with_their_options(monkeypatch):
    seen: dict = {}

    async def fake_run_checks(ctx, names, options):
        seen.update(ctx=ctx, names=names, options=options)
        return 0

    monkeypatch.setattr(verify, "load_settings", lambda: Settings(api_key=KEY))
    monkeypatch.setattr(verify, "run_checks", fake_run_checks)
    assert verify.main(["all", "--voice", "marcus"]) == 0
    assert seen["names"] == verify.ALL_CHECKS
    assert seen["options"]["latency"] == {"voice": "marcus"}
    assert seen["options"]["voices"] == {}
    assert seen["ctx"].settings.api_key == KEY
    assert verify.main(["idle", "--wait", "1"]) == 0
    assert seen["names"] == ("idle",) and seen["options"] == {"idle": {"wait": 1.0}}


# ── WAV / PCM helpers ────────────────────────────────────────────────────────────────────────────────────────


def fmt_chunk(tag: int, channels: int, rate: int, bits: int, extra: bytes = b"") -> bytes:
    block = channels * bits // 8
    return struct.pack("<HHIIHH", tag, channels, rate, rate * block, block, bits) + extra


def riff(fmt: bytes, data: bytes, *, data_size: int | None = None) -> bytes:
    size = len(data) if data_size is None else data_size
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"LIST" + struct.pack("<I", 4) + b"INFO"
            + b"data" + struct.pack("<I", size) + data)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def pcm16(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def test_wav_round_trip_needs_no_conversion():
    pcm = tone(100)
    audio = verify.parse_wav(verify.wav_bytes(pcm))
    assert (audio.rate, audio.channels, audio.sample_width, audio.format_tag) == (RATE, 1, 2, 1)
    assert verify.to_pcm16_mono(audio) == (pcm, [])


def test_wav_48k_stereo_becomes_24k_mono_with_a_note():
    frames = 4800  # 100 ms at 48 kHz
    data = pcm16(*([1000, 3000] * frames))
    pcm, notes = verify.to_pcm16_mono(verify.parse_wav(riff(fmt_chunk(1, 2, 48_000, 16), data)))
    samples = verify._native("h", pcm)
    assert len(samples) == 2400 and set(samples) == {2000}
    assert notes and "48000 Hz 2 ch 16-bit PCM" in notes[0] and "24000 Hz" in notes[0]


def test_wav_8_24_and_float_samples_scale_to_int16():
    eight = verify.parse_wav(riff(fmt_chunk(1, 1, RATE, 8), bytes([128, 255, 0])))
    assert list(verify._native("h", verify.to_pcm16_mono(eight)[0])) == [0, 32512, -32768]
    data24 = b"".join(value.to_bytes(3, "little", signed=True) for value in (0x7FFFFF, -1, -0x800000))
    twenty_four = verify.parse_wav(riff(fmt_chunk(1, 1, RATE, 24), data24))
    assert list(verify._native("h", verify.to_pcm16_mono(twenty_four)[0])) == [32767, -1, -32768]
    floats = verify.parse_wav(riff(fmt_chunk(3, 1, RATE, 32), struct.pack("<4f", 0.5, -1.0, 1.5, 0.0)))
    assert list(verify._native("h", verify.to_pcm16_mono(floats)[0])) == [16384, -32767, 32767, 0]


def test_wav_extensible_and_open_ended_data_sizes():
    guid_pcm = struct.pack("<H", 1) + b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    extensible = fmt_chunk(0xFFFE, 1, RATE, 16, struct.pack("<HHI", 22, 16, 4) + guid_pcm)
    audio = verify.parse_wav(riff(extensible, pcm16(1, 2, 3)))
    assert audio.format_tag == 1 and verify.to_pcm16_mono(audio) == (pcm16(1, 2, 3), [])
    streamed = verify.parse_wav(riff(fmt_chunk(1, 1, RATE, 16), pcm16(4, 5), data_size=0xFFFFFFFF))
    assert streamed.data == pcm16(4, 5)


@pytest.mark.parametrize("blob", [b"", b"ID3 not a wav at all", b"RIFF\x04\x00\x00\x00WAVE",
                                  riff(fmt_chunk(1, 1, RATE, 16), b"")[:36]])
def test_parse_wav_rejects_what_it_cannot_read(blob):
    with pytest.raises(ValueError):
        verify.parse_wav(blob)


def test_silence_lengths_and_written_wavs(tmp_path):
    assert len(verify.silence(0.7)) == 2 * 16_800
    assert verify.pcm_ms(verify.silence(0.7)) == 700
    path = verify.write_wav(tmp_path / "deep" / "clip.wav", tone(250) + b"\x00")  # odd byte dropped
    assert wav_info(path) == (1, 2, RATE, 6000)


def test_resample_and_levels():
    assert list(verify.resample([0, 100], 12_000, 24_000)) == [0, 50, 100, 100]
    assert list(verify.resample([5, 6], RATE, RATE)) == [5, 6]
    assert verify.level_dbfs(b"") is None
    assert verify.level_dbfs(bytes(200)) is None
    assert verify.describe_level(bytes(200)) == "silent"
    full = pcm16(*([32767, -32767] * 100))
    assert abs(verify.level_dbfs(full)) < 0.01
    assert verify.describe_level(pcm16(*([3277, -3277] * 100))) == "-20.0 dBFS"


# ── result formatting ────────────────────────────────────────────────────────────────────────────────────────


def test_result_block_ends_with_a_dated_gotchas_line(tmp_path):
    result = Result("latency", "PASS", ["connect → session.created: median 400 ms"], summary="all fast.",
                    files=[tmp_path / "x.wav"])
    block = verify.format_result(result, NOW)
    lines = block.splitlines()
    assert lines[0] == "== F2 latency (server side): PASS =="
    assert lines[1] == "  connect → session.created: median 400 ms"
    assert lines[2] == f"  wrote {tmp_path / 'x.wav'}"
    assert lines[-2] == "paste into doc/gotchas.md:"
    assert lines[-1] == ("- **F2 latency (server side)** (2026-10-23 18:05, `python -m relay.verify latency`): "
                         "PASS. all fast.")


def test_overview_lists_every_check_and_its_line():
    results = [Result("voices", "LISTEN", summary="by ear."), Result("tools", "FAIL", summary="8/10.")]
    text = verify.format_overview(results, NOW)
    assert "  voices   LISTEN" in text and "  tools    FAIL" in text
    assert text.endswith("- **F5 tool reliability** (2026-10-23 18:05, `python -m relay.verify tools`): FAIL. 8/10.")


def test_small_formatters():
    assert verify.describe_ms([]) == "n/a"
    assert verify.describe_ms([300, 100, 200]) == "median 200 ms (min 100, max 300, n=3)"
    assert verify.rel(verify.ROOT / "out" / "verify" / "a.wav") == "out/verify/a.wav"
    assert verify.word_match("<|emotion:awe|>Here is how, this works.", "here is how this works") == 1.0
    assert verify.word_match("one two three four", "one two") == 0.5
    assert verify.word_match("", "") == 1.0


# ── the typed-turn collector ─────────────────────────────────────────────────────────────────────────────────


async def test_typed_turn_sends_the_typed_path_and_collects_the_reply():
    audio = tone(40)
    call = {"call_id": "c1", "name": "resume_script", "arguments": "{}"}
    conn = ScriptedConn(
        {"type": "session.updated", "session": {"type": "realtime", "instructions": "new"}},
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.output_item.added", "response_id": "r1"},
        {"type": "response.output_audio.delta", "response_id": "r1", "delta": b64(audio[:960])},
        {"type": "response.output_audio_transcript.delta", "response_id": "r1", "delta": "Back "},
        {"type": "response.output_audio.delta", "response_id": "r1", "delta": b64(audio[960:])},
        {"type": "response.output_audio_transcript.delta", "response_id": "r1", "delta": "to it"},
        {"type": "response.output_audio_transcript.done", "response_id": "r1", "transcript": "Back to it."},
        {"type": "response.function_call_arguments.done", "response_id": "r1", **call},
        "not json at all",
        {"type": "response.done", "response": {"id": "r1", "status": "completed",
                                               "output": [{"type": "message"}, {"type": "function_call", **call}]}},
        {"type": "response.created", "response": {"id": "r2"}},
    )
    seen: list[tuple[int, str]] = []
    turn = await verify.typed_turn(conn, "Quick comment.", session={"type": "realtime", "instructions": "new"},
                                   clock=TickClock(), on_event=lambda ms, event: seen.append((ms, event["type"])))
    assert [event["type"] for event in conn.sent] == ["session.update", "conversation.item.create", "response.create"]
    assert conn.sent[0]["session"] == {"type": "realtime", "instructions": "new"}
    assert conn.sent[1]["item"] == {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "Quick comment."}]}
    assert bytes(turn.pcm) == audio and turn.audio_ms == 40
    assert turn.transcript == "Back to it."
    assert [(c.call_id, c.name, c.args) for c in turn.calls] == [("c1", "resume_script", {})]
    assert (turn.status, turn.problem(), turn.timed_out, turn.closed) == ("completed", "", False, False)
    assert (turn.created_ms, turn.first_audio_ms, turn.first_text_ms) == (20, 40, 50)
    assert turn.acks == [{"type": "realtime", "instructions": "new"}]
    assert turn.events["response.output_item.added"] == 1 and turn.events["(not json)"] == 1
    assert seen[0] == (10, "session.updated") and seen[-1][1] == "response.done"
    assert len(conn.events) == 1  # stopped reading at its own response.done


async def test_typed_turn_ignores_another_responses_events():
    conn = ScriptedConn(
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.output_audio.delta", "response_id": "r0", "delta": b64(pcm16(9, 9))},
        {"type": "response.output_audio_transcript.done", "response_id": "r0", "transcript": "stale"},
        {"type": "response.done", "response": {"id": "r0", "status": "cancelled", "output": []}},
        {"type": "response.output_audio.delta", "response_id": "r1", "delta": b64(pcm16(1, 2))},
        {"type": "response.done", "response": {"id": "r1", "status": "completed", "output": []}},
    )
    turn = await verify.typed_turn(conn, "hi")
    assert bytes(turn.pcm) == pcm16(1, 2) and turn.transcript == "" and turn.status == "completed"


async def test_typed_turn_dedupes_a_call_and_fills_in_its_name():
    arguments = json.dumps({"slide_id": "qa"})
    conn = ScriptedConn(
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.function_call_arguments.done", "response_id": "r1", "call_id": "c1", "arguments": arguments},
        {"type": "response.done", "response": {"id": "r1", "status": "completed", "output": [
            {"type": "function_call", "call_id": "c1", "name": "goto_slide", "arguments": arguments},
            {"type": "function_call", "call_id": "c2", "name": "next_slide", "arguments": ""}]}},
    )
    turn = await verify.typed_turn(conn, "skip ahead")
    assert [(c.call_id, c.name, c.args) for c in turn.calls] == [("c1", "goto_slide", {"slide_id": "qa"}),
                                                                 ("c2", "next_slide", {})]
    assert str(turn.calls[0]) == "goto_slide(slide_id='qa')" and str(turn.calls[1]) == "next_slide()"


async def test_typed_turn_reports_a_billing_close_as_fatal():
    conn = ScriptedConn({"type": "response.created", "response": {"id": "r1"}}, (4429, "insufficient quota"))
    turn = await verify.typed_turn(conn, "hi")
    assert turn.closed and turn.close_code == 4429 and turn.fatal
    assert turn.problem() == f"socket closed (code 4429 'insufficient quota') → billing refused: claim credits at " \
                             f"{CREDITS_URL}"


async def test_typed_turn_on_a_socket_closed_for_a_bad_key():
    conn = ScriptedConn()
    conn.send_error = ConnectionClosed(Close(3000, "invalid key"), None)
    turn = await verify.typed_turn(conn, "hi")
    assert turn.closed and turn.close_code == 3000 and turn.fatal
    assert "check BOSON_API_KEY" in turn.problem()


async def test_typed_turn_raises_on_insufficient_quota_in_type_or_code():
    for error in ({"type": "insufficient_quota", "message": "no credit"},
                  {"type": "invalid_request_error", "code": "insufficient_quota", "message": "no credit"}):
        with pytest.raises(VerifyError) as exc:
            await verify.typed_turn(ScriptedConn({"type": "error", "error": error}), "hi")
        assert exc.value.fatal and CREDITS_URL in str(exc.value)


async def test_typed_turn_times_out_and_keeps_the_error():
    conn = ScriptedConn({"type": "error", "error": {"type": "invalid_request_error", "message": "bad item"}})
    turn = await verify.typed_turn(conn, "hi", timeout=0.05)
    assert turn.timed_out and not turn.closed and not turn.fatal
    assert turn.problem() == "no response.done before the timeout (error event: invalid_request_error: bad item)"


# ── errors and hints ─────────────────────────────────────────────────────────────────────────────────────────


def test_http_errors_map_to_hints_like_the_smoke_helper():
    quota = verify.http_error("TTS", FakeResponse(429, {"error": {"type": "insufficient_quota", "message": "x"}}))
    assert quota.fatal and CREDITS_URL in str(quota) and "HTTP 429 insufficient_quota" in str(quota)
    by_code = verify.http_error("TTS", FakeResponse(429, {"error": {"type": "invalid_request_error",
                                                                    "code": "insufficient_quota"}}))
    assert by_code.fatal and CREDITS_URL in str(by_code)
    busy = verify.http_error("TTS", FakeResponse(429, {"error": {"type": "rate_limit_error", "message": "slow"}}))
    assert not busy.fatal and "retry with backoff" in str(busy)
    auth = verify.http_error("TTS", FakeResponse(401, {"error": {"message": "bad key"}}))
    assert auth.fatal and "check BOSON_API_KEY" in str(auth)
    html = verify.http_error("TTS", FakeResponse(502, None, text="<html>Bad gateway</html>"))
    assert not html.fatal and "HTTP 502: <html>Bad gateway</html>" in str(html)


def test_close_codes_and_handshakes_map_to_hints():
    assert verify.closed_error(4429).fatal and CREDITS_URL in str(verify.closed_error(4429))
    assert verify.closed_error(3000).fatal and "check BOSON_API_KEY" in str(verify.closed_error(3000))
    assert not verify.closed_error(1000).fatal and not verify.closed_error(1013).fatal
    body = json.dumps({"error": {"type": "insufficient_quota"}}).encode()
    refused = verify.handshake_error(InvalidStatus(Response(429, "Too Many Requests", Headers(), body)))
    assert refused.fatal and CREDITS_URL in str(refused)
    unauthorized = verify.handshake_error(InvalidStatus(Response(401, "Unauthorized", Headers(), b"nope")))
    assert unauthorized.fatal and "check BOSON_API_KEY" in str(unauthorized)


async def test_open_session_turns_connect_failures_into_hints(live_settings):
    async def unauthorized(url, **kwargs):
        raise InvalidStatus(Response(401, "Unauthorized", Headers(), b""))

    async def offline(url, **kwargs):
        raise OSError("nodename nor servname provided")

    async def slow(url, **kwargs):
        raise TimeoutError()

    with pytest.raises(VerifyError) as exc:
        await verify.open_session(Ctx(live_settings, connect=unauthorized), {})
    assert exc.value.fatal and "HTTP 401" in str(exc.value)
    with pytest.raises(VerifyError) as exc:
        await verify.open_session(Ctx(live_settings, connect=offline), {})
    assert not exc.value.fatal and "cannot reach wss://api.boson.ai/v1/realtime: nodename" in str(exc.value)
    with pytest.raises(VerifyError) as exc:
        await verify.open_session(Ctx(live_settings, connect=slow), {})
    assert "TimeoutError" in str(exc.value)


async def test_open_session_fails_fatally_on_a_billing_error_and_closes(live_settings):
    def no_credit(conn: FakeRealtime) -> None:
        conn.inbox = asyncio.Queue()  # the server answers the first session.update with an error, then closes
        conn.push({"type": "error", "error": {"type": "insufficient_quota", "message": "claim your credit"}})
        conn.drop(4429, "insufficient quota")

    server = FakeServer(on_created=no_credit)
    with pytest.raises(VerifyError) as exc:
        await verify.open_session(Ctx(live_settings, connect=server.connect), {"type": "realtime"})
    assert exc.value.fatal and CREDITS_URL in str(exc.value)
    assert server.connections[0].client_closed


async def test_open_session_sends_the_session_update_first_and_reads_the_ack(live_settings):
    server = FakeServer()
    config = verify.session_config(live_settings, voice="marcus", instructions="hi")
    opened = await verify.open_session(Ctx(live_settings, connect=server.connect), config)
    url, kwargs = server.calls[0]
    assert url == "wss://api.boson.ai/v1/realtime?model=higgs-realtime"
    assert kwargs["additional_headers"] == {"Authorization": f"Bearer {KEY}"} and kwargs["max_size"] is None
    assert server.connections[0].sent == [{"type": "session.update", "session": config}]
    assert opened.session["audio"]["output"]["voice"] == "marcus"
    assert [event["type"] for event in opened.events] == ["session.created"]


# ── session config and acks ──────────────────────────────────────────────────────────────────────────────────


def test_session_config_is_the_apps(live_settings):
    config = verify.session_config(live_settings, voice="chloe", instructions="BASE", tools=[{"name": "x"}])
    assert config == {
        "type": "realtime", "model": "higgs-realtime", "instructions": "BASE", "output_modalities": ["audio"],
        "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                            "transcription": {"model": "higgs-stt-3.1", "language": "en"},
                            "turn_detection": {"type": "semantic_vad"}},
                  "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "chloe"}},
        "tools": [{"name": "x"}], "tool_choice": "auto"}
    hold = Settings(api_key=KEY, mic_mode="hold", temperature=0.6, full_session_update=True)
    config = verify.session_config(hold, voice="chloe", instructions="BASE")
    assert config["audio"]["input"]["turn_detection"] is None and config["temperature"] == 0.6
    assert verify.per_turn_session(hold, config, "NEW") == {**config, "instructions": "NEW"}
    assert verify.per_turn_session(live_settings, config, "NEW") == {"type": "realtime", "instructions": "NEW"}


def test_session_fields_tell_absent_from_changed():
    full = verify.session_config(Settings(), voice="chloe", instructions="x", tools=[{}, {}])
    fields = verify.session_fields(full)
    assert fields == {"voice": "chloe", "turn_detection": {"type": "semantic_vad"},
                      "transcription": {"model": "higgs-stt-3.1", "language": "en"}, "tools": 2,
                      "output_modalities": ["audio"]}
    bare = verify.session_fields({"instructions": "x", "tools": None})
    assert bare["tools"] == 0 and verify.absent_fields(bare) == ["voice", "turn_detection", "transcription",
                                                                 "output_modalities"]
    assert verify.diff_fields(fields, bare) == ["tools: 2 → 0"]
    assert verify.diff_fields(fields, {**fields, "voice": "default"}) == ["voice: 'chloe' → 'default'"]


# ── tool calls, scenarios, talks ─────────────────────────────────────────────────────────────────────────────


def test_check_call_adapts_to_either_parse_call_signature(test_talk):
    class Parsed:
        def __init__(self, args: dict):
            self.args = args

    def with_call_id(call_id, name, arguments, talk):
        assert call_id == "c1"
        return Parsed({"slide_id": json.loads(arguments)["slide_id"].lower()})

    def design_signature(name, arguments, talk):
        return "unknown slide_id 'nope'"

    def broken(*args):
        raise RuntimeError("half-written module")

    call = ToolCallSeen("c1", "goto_slide", json.dumps({"slide_id": "QA"}))
    resolved = verify.check_call(call, test_talk, with_call_id)
    assert resolved.error is None and resolved.args == {"slide_id": "qa"}
    assert verify.check_call(call, test_talk, design_signature).error == "unknown slide_id 'nope'"
    assert verify.check_call(call, test_talk, broken).error == "unknown slide_id 'QA'"  # local fallback
    assert verify.check_call(ToolCallSeen("c2", "dance", "{}"), test_talk).error == "unknown tool 'dance'"
    assert verify.check_call(ToolCallSeen("c3", "next_slide", ""), test_talk).error is None


def test_check_call_with_the_real_tools_module(test_talk):
    from relay import tools

    parse_call = getattr(tools, "parse_call", None)
    checked = verify.check_call(ToolCallSeen("c1", "goto_slide", json.dumps({"slide_id": "qa"})), test_talk,
                                parse_call)
    assert checked.error is None and checked.args.get("slide_id") == "qa"
    assert verify.check_call(ToolCallSeen("c2", "dance", "{}"), test_talk, parse_call).error


def test_scenarios_follow_the_run_of_show_on_the_test_talk(test_talk):
    scenarios = verify.build_scenarios(test_talk)
    assert [(s.name, s.phase, s.slide_id, s.expect) for s in scenarios] == [
        ("opening", "opening", "title", "start_presentation"), ("comment", "presenting", "how", "resume_script"),
        ("plain", "presenting", "how", "resume_script"), ("redirect", "presenting", "how", "goto_slide"),
        ("question", "qa", "qa", "handoff_to_human")]
    assert "take it away" in scenarios[0].say and "not an engineer" in scenarios[2].say
    redirect = scenarios[3]
    assert redirect.expect_slide == "qa" and redirect.also == ("next_slide",)
    assert redirect.say == "That's a lot of detail, let's skip to the questions."
    assert scenarios[1].cut_word and scenarios[1].cut_sentence.startswith("Every single word")


def test_scenarios_on_the_stage_talk():
    try:
        talk = load_talk(verify.ROOT / "talks" / "relay-intro")
    except Exception:
        pytest.skip("talks/relay-intro is missing or invalid right now")
    ids = {slide.id for slide in talk.slides}
    if not {"how", "details", "demo", "qa"} <= ids:
        pytest.skip("talks/relay-intro no longer has the Appendix A slide ids")
    scenarios = {s.name: s for s in verify.build_scenarios(talk)}
    assert scenarios["plain"].slide_id == "how"
    assert scenarios["redirect"].slide_id == "details" and scenarios["redirect"].expect_slide == "demo"
    assert scenarios["redirect"].say == "That's a lot of detail, let's skip to the demo."


def test_scenario_accepts_only_the_intended_valid_call():
    scenario = verify.Scenario("redirect", "presenting", "how", "skip", "goto_slide", "qa", ("next_slide",))

    def checked(name: str, args: dict, error: str | None = None) -> verify.CheckedCall:
        return verify.CheckedCall(ToolCallSeen("c", name, json.dumps(args)), error, args)

    assert scenario.accepts(checked("goto_slide", {"slide_id": "qa"}))
    assert scenario.accepts(checked("next_slide", {}))
    assert not scenario.accepts(checked("goto_slide", {"slide_id": "how"}))
    assert not scenario.accepts(checked("goto_slide", {"slide_id": "qa"}, "rejected"))
    assert not scenario.accepts(checked("resume_script", {}))
    assert not scenario.accepts(None)
    assert scenario.want() == "goto_slide(slide_id='qa') or next_slide()"


def test_tool_outputs_look_like_the_apps(test_talk):
    def out(name: str, args: dict | None = None, error: str | None = None, slide: str = "how") -> dict:
        call = verify.CheckedCall(ToolCallSeen("c", name, json.dumps(args or {})), error, args or {})
        return verify.app_tool_output(call, test_talk, slide)

    assert out("start_presentation", slide="title") == {"ok": True, "slide": "how"}
    assert out("resume_script") == {"ok": True, "slide": "how"}
    assert out("goto_slide", {"slide_id": "qa"}) == {"ok": True, "slide": "qa"}
    assert out("next_slide") == {"ok": True, "slide": "qa"}
    assert out("next_slide", slide="qa") == {"error": "no next slide"}
    assert out("handoff_to_human") == {"ok": True}
    assert out("goto_slide", error="unknown slide_id 'x'") == {"error": "unknown slide_id 'x'"}
    event = verify.tool_output("c9", {"ok": True})
    assert event["item"] == {"type": "function_call_output", "call_id": "c9", "output": '{"ok": true}'}


def test_resolve_talk_falls_back_to_the_test_talk(tmp_path):
    talk, note = verify.resolve_talk("talks/test")
    assert talk.id == "test" and note == ""
    talk, note = verify.resolve_talk("talks/no-such-talk")
    assert talk.id == "test" and note == "talks/no-such-talk not found → using talks/test"
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "slides.md").write_text('<!-- .slide: id="a" -->\n# A\n', encoding="utf-8")
    (broken / "script.yaml").write_text("talk: broken\nvoice: alloy\npresenter: Ken\nslides: []\n", encoding="utf-8")
    talk, note = verify.resolve_talk(str(broken))
    assert talk.id == "test" and "fails validation" in note


# ── tag analysis ─────────────────────────────────────────────────────────────────────────────────────────────


def words_of(raw: list[dict]) -> list[TimedWord]:
    return verify.parse_timestamps(raw)


def test_parse_timestamps_converts_seconds_and_keeps_null():
    assert verify.parse_timestamps(None) is None
    assert verify.parse_timestamps([]) == []
    assert verify.parse_timestamps([{"word": "Hi", "start": 0.12, "end": 0.3}, {"word": "bad"}]) == [
        TimedWord("Hi", 120, 300)]


def test_tag_findings_pass_when_the_pause_widens_the_gap():
    text = verify.TAGGED_TEXT
    tagged = words_of(timed_words(text, pause_after=4, pause_ms=600))
    plain = words_of(timed_words(text, pause_after=4, pause_ms=90))
    findings = verify.tag_findings(text, tagged, plain, 6000)
    assert findings.status == "PASS"
    assert findings.leak == "no tag text in the word list"
    assert findings.pause == "pause gap [works. | Every] 600 ms with the tag, 90 ms without (+510 ms)"
    assert "words: 19 from TTS (tags removed), 19 in the text" in findings.lines


def test_tag_findings_flag_tag_names_partial_tags_and_a_missing_pause():
    text = verify.TAGGED_TEXT
    named = words_of([{"word": "enthusiasm", "start": 0.0, "end": 0.1}, *timed_words(text, pause_after=4,
                                                                                         pause_ms=600)])
    findings = verify.tag_findings(text, named, words_of(timed_words(text)), 6000)
    # bare tag names are reported but don't fail F3: relay.render drops them from the word list
    assert findings.status == "PASS" and "bare tag names show up as words: enthusiasm" in findings.leak
    assert "the render drops these" in findings.leak
    partial = words_of([{"word": "<|prosody:pause", "start": 1.0, "end": 1.1}, *timed_words(text)])
    assert verify.tag_findings(text, partial, None, 6000).status == "FAIL"
    glued = timed_words(text, pause_after=4, pause_ms=600)
    glued[0]["word"] = "<|emotion:enthusiasm|><|prosody:expressive_high|>Here"
    findings = verify.tag_findings(text, words_of(glued), words_of(timed_words(text, pause_after=4)), 6000)
    assert findings.status == "PASS" and "the render strips" in findings.leak
    flat = verify.tag_findings(text, words_of(timed_words(text)), words_of(timed_words(text)), 6000)
    assert flat.status == "LISTEN" and "(+0 ms)" in flat.pause
    late = verify.tag_findings(text, words_of(timed_words(text)), None, 1000)
    assert any("timings run past the audio" in line for line in late.lines)


# ── checks, end to end on fakes ──────────────────────────────────────────────────────────────────────────────


async def test_check_voices_writes_the_ab_clips_and_prints_transcripts(live_settings):
    tts = FakeTTS(audio_ms=500)
    server = FakeServer(reply_ms=600, transcript=lambda conn, said: said)
    ctx = Ctx(live_settings, connect=server.connect, post=tts, echo=quiet)
    result = await verify.check_voices(ctx, voices=("chloe", "marcus"))
    assert result.status == "LISTEN"
    out = live_settings.out_dir / "verify" / "voices"
    for voice in ("chloe", "marcus"):
        assert wav_info(out / f"{voice}_ab.wav") == (1, 2, RATE, 24 * (500 + 700 + 600))
    assert wav_info(out / "all_voices.wav") == (1, 2, RATE, 24 * (2 * 1800 + 1500))
    url, kwargs = tts.calls[0]
    assert url == "https://api.boson.ai/v1/audio/speech"
    assert kwargs["json"] == {"model": "higgs-tts-3", "input": verify.DEFAULT_LINE, "voice": "chloe",
                              "response_format": "wav", "timestamps": True}
    assert kwargs["headers"] == {"Authorization": f"Bearer {KEY}"}
    conn = server.connections[1]
    assert conn.sent[0]["session"]["audio"]["output"]["voice"] == "marcus"
    assert conn.sent[0]["session"]["instructions"] == verify.REPEAT_INSTRUCTIONS
    assert conn.types() == ["session.update", "conversation.item.create", "response.create"]
    assert conn.client_closed
    text = "\n".join(result.lines)
    assert f'Realtime said: "{verify.DEFAULT_LINE}" (100% of the words match the line)' in text
    assert "first audio" in text and "dBFS" in text
    assert "word for word for 2/2" in result.summary and "By ear" in result.summary


async def test_check_voices_converts_odd_tts_wavs_and_reports_a_failed_voice(live_settings):
    stereo = riff(fmt_chunk(1, 2, 48_000, 16), pcm16(*([1000, 3000] * 48_000)))  # 1 s at 48 kHz stereo

    def respond(body: dict) -> FakeResponse:
        if body["voice"] == "marcus":
            return FakeResponse(500, {"error": {"type": "server_error", "message": "boom"}})
        return FakeResponse(200, tts_body(body["input"], wav=stereo))

    server = FakeServer(transcript=lambda conn, said: "Something else entirely.")
    ctx = Ctx(live_settings, connect=server.connect, post=FakeTTS(respond), echo=quiet)
    result = await verify.check_voices(ctx, voices=("chloe", "marcus"), line="One line, with 3 digits.")
    assert result.status == "LISTEN"
    text = "\n".join(result.lines)
    assert "converted wav 48000 Hz 2 ch 16-bit PCM → 24000 Hz mono" in text
    assert "marcus: FAILED: TTS: HTTP 500 server_error: boom" in text
    assert "the line has digits" in text
    assert "loudness differs" in text  # a 2000-level square-ish TTS clip vs the 8000-amplitude reply tone
    assert "FAILED: marcus" in result.summary and "word for word for 0/1" in result.summary
    assert wav_info(live_settings.out_dir / "verify" / "voices" / "chloe_ab.wav")[3] == 24 * (1000 + 700 + 600)


async def test_check_voices_stops_on_billing(live_settings):
    tts = FakeTTS(lambda body: FakeResponse(429, {"error": {"type": "insufficient_quota", "message": "claim"}}))
    result = await verify.run_check(Ctx(live_settings, connect=FakeServer().connect, post=tts, echo=quiet),
                                    "voices", {})
    assert result.status == "FAIL" and result.fatal
    assert CREDITS_URL in result.summary and len(tts.calls) == 1


async def test_check_tags_passes_with_a_wider_pause_and_writes_both_wavs(live_settings):
    def respond(body: dict) -> FakeResponse:
        pause = 600 if "<|prosody:pause|>" in body["input"] else 90
        return FakeResponse(200, tts_body(body["input"], audio_ms=6000,
                                          words=timed_words(body["input"], pause_after=4, pause_ms=pause)))

    tts = FakeTTS(respond)
    result = await verify.check_tags(Ctx(live_settings, post=tts, echo=quiet))
    assert result.status == "PASS", result.lines
    assert [call[1]["json"]["input"] for call in tts.calls] == [verify.TAGGED_TEXT, strip_tags(verify.TAGGED_TEXT)]
    out = live_settings.out_dir / "verify" / "tags"
    assert wav_info(out / "tagged.wav")[2:] == (RATE, 24 * 6000)
    assert json.loads((out / "tagged.json").read_text())["input"] == verify.TAGGED_TEXT
    text = "\n".join(result.lines)
    assert "Here@100-300" in text  # the word list with timings
    assert "+510 ms" in result.summary and "no tag text in the word list" in result.summary


async def test_check_tags_fails_without_timestamps(live_settings):
    tts = FakeTTS(lambda body: FakeResponse(200, tts_body(body["input"], words=None)))
    result = await verify.check_tags(Ctx(live_settings, post=tts, echo=quiet))
    assert result.status == "FAIL" and "timestamps null" in result.summary


async def test_check_latency_reports_medians_and_where_the_rest_is_measured(live_settings):
    server = FakeServer()
    result = await verify.check_latency(Ctx(live_settings, connect=server.connect, echo=quiet), runs=3)
    assert result.status == "PASS", result.lines
    assert len(server.connections) == 4 and all(conn.client_closed for conn in server.connections)
    assert [conn.types().count("response.create") for conn in server.connections] == [3, 0, 0, 0]
    text = "\n".join(result.lines)
    assert "typed turn → first audio: median" in text and "n=3" in text
    assert "reconnect (old socket closed → new session.created): median" in text
    assert "measured in the browser (status strip) and the app event log" in text
    assert "measured in the browser and the app event log" in result.summary


async def test_check_latency_keeps_its_numbers_when_a_reconnect_fails(live_settings):
    server = FakeServer(refuse=lambda n: OSError("network is down") if n == 3 else None)
    result = await verify.check_latency(Ctx(live_settings, connect=server.connect, echo=quiet), runs=3)
    assert result.status == "FAIL"
    text = "\n".join(result.lines)
    assert "stopped early: cannot reach" in text and "n=3" in text  # the three turns still count
    assert all(conn.client_closed for conn in server.connections)


async def test_check_session_passes_when_the_partial_update_keeps_everything(live_settings):
    server = FakeServer(stale_ack=True)  # a late ack of the first update must not be read as the second's
    result = await verify.check_session(Ctx(live_settings, connect=server.connect, echo=quiet), talk="talks/test")
    assert result.status == "PASS", result.lines
    conn = server.connections[0]
    assert conn.sent[0]["session"]["audio"]["output"]["voice"] == "chloe"
    assert len(conn.sent[0]["session"]["tools"]) == 5 and "Phase: opening" in conn.sent[0]["session"]["instructions"]
    assert conn.sent[1]["session"].keys() == {"type", "instructions"}
    assert "Phase: presenting" in conn.sent[1]["session"]["instructions"]
    assert "new instructions applied (echoed in session.updated)" in result.lines
    assert "Keep RELAY_FULL_SESSION_UPDATE off" in result.summary and "temperature 0.7" in result.summary
    out = live_settings.out_dir / "verify" / "session"
    assert json.loads((out / "updated.json").read_text())["instructions"] == conn.sent[1]["session"]["instructions"]


async def test_check_session_fails_when_the_partial_update_resets_voice_and_vad(live_settings):
    def reset(session: dict, update: dict) -> dict:
        out = merged(session, update)
        out["audio"]["output"]["voice"] = "default"
        out["audio"]["input"]["turn_detection"] = None
        return out

    server = FakeServer(partial=reset)
    result = await verify.check_session(Ctx(live_settings, connect=server.connect, echo=quiet), talk="talks/test")
    assert result.status == "FAIL"
    assert "voice: 'chloe' → 'default'" in result.summary and "RELAY_FULL_SESSION_UPDATE=1" in result.summary
    assert "did not stick" not in result.summary


async def test_check_session_listens_when_the_ack_leaves_fields_out(live_settings):
    server = FakeServer(ack=lambda session: {"type": "realtime", "instructions": session["instructions"]})
    result = await verify.check_session(Ctx(live_settings, connect=server.connect, echo=quiet), talk="talks/test")
    assert result.status == "LISTEN"
    assert "the acks leave out: output_modalities, tools, transcription, turn_detection, voice" in result.lines


async def test_check_session_flags_a_first_update_that_did_not_stick(live_settings):
    def server_vad(session: dict) -> dict:
        session["audio"]["input"]["turn_detection"] = {"type": "server_vad"}
        return session

    server = FakeServer(resolve=server_vad)
    result = await verify.check_session(Ctx(live_settings, connect=server.connect, echo=quiet), talk="talks/test")
    assert result.status == "FAIL"
    assert "the full session.update did not stick (turn_detection: 'semantic_vad' → 'server_vad')" in result.summary


async def test_check_tools_counts_real_calls_and_never_sends_response_create_after_outputs(live_settings):
    server = FakeServer(tool=app_policy("qa"))
    lines: list[str] = []
    result = await verify.check_tools(Ctx(live_settings, connect=server.connect, echo=lines.append),
                                      talk="talks/test", runs=10)
    assert result.status == "PASS", result.lines
    assert result.summary.startswith("10/10 typed turns (100%) ended with the right real function_call")
    assert len(server.connections) == 2  # a fresh session per pass through the five scenarios
    for conn in server.connections:
        assert len(conn.sent[0]["session"]["tools"]) == 5
        turns = conn.sent[1:]
        assert [event["type"] for event in turns] == ["session.update", "conversation.item.create",
                                                      "response.create", "conversation.item.create"] * 5
        for update in turns[0::4]:
            assert update["session"].keys() == {"type", "instructions"}
        outputs = [event["item"] for event in turns[3::4]]
        assert all(item["type"] == "function_call_output" and isinstance(item["output"], str) for item in outputs)
        assert [json.loads(item["output"]) for item in outputs] == [
            {"ok": True, "slide": "how"}, {"ok": True, "slide": "how"}, {"ok": True, "slide": "how"},
            {"ok": True, "slide": "qa"}, {"ok": True}]
        phases = [update["session"]["instructions"].split("Phase: ")[1].split("\n")[0] for update in turns[0::4]]
        assert phases == ["opening", "presenting", "presenting", "presenting", "qa"]
    out = live_settings.out_dir / "verify" / "tools"
    log = json.loads((out / "turns.json").read_text())
    assert len(log) == 10 and all(entry["ok"] for entry in log)
    assert all(entry["prompt_words"] > 50 for entry in log)
    assert (out / "turn01_opening.wav").is_file() and (out / "turn10_question.wav").is_file()
    assert "  … turn 1/10 opening: ok" in lines  # short progress while it runs; the block has the details
    assert any(line.startswith(" 1 ok   opening  @title: want start_presentation(); got start_presentation()")
               for line in result.lines)
    sizes = next(line for line in result.lines if line.startswith("prompt (BASE + STATE) words: "))
    assert [part.split()[0] for part in sizes.split(": ")[1].split(", ")] == [
        "opening", "comment", "plain", "redirect", "question"]


async def test_check_tools_fails_below_ninety_percent_and_names_the_misses(live_settings):
    def narrates(conn: FakeRealtime, said: str) -> tuple[str, dict] | None:
        return None if "engineer" in said else app_policy("qa")(conn, said)

    server = FakeServer(tool=narrates, transcript=lambda conn, said: "As I was saying, resume_script.")
    result = await verify.check_tools(Ctx(live_settings, connect=server.connect, echo=quiet),
                                      talk="talks/test", runs=10)
    assert result.status == "FAIL"
    assert "8/10 typed turns (80%)" in result.summary and "misses: plain ×2" in result.summary
    assert any("MISS plain" in line and "said a tool name aloud instead?" in line for line in result.lines)


async def test_check_tools_accepts_next_slide_and_rejects_a_wrong_target(live_settings):
    def next_slide(conn: FakeRealtime, said: str) -> tuple[str, dict]:
        return ("next_slide", {}) if "skip to" in said else app_policy("qa")(conn, said)

    server = FakeServer(tool=next_slide)
    result = await verify.check_tools(Ctx(live_settings, connect=server.connect, echo=quiet),
                                      talk="talks/test", runs=5)
    assert result.status == "PASS", result.lines
    server = FakeServer(tool=app_policy("how"))
    result = await verify.check_tools(Ctx(live_settings, connect=server.connect, echo=quiet),
                                      talk="talks/test", runs=5)
    assert result.status == "FAIL" and "misses: redirect ×1" in result.summary


async def test_check_tools_falls_back_to_the_test_talk_and_survives_a_drop(live_settings):
    def drop_on_plain(conn: FakeRealtime, said: str) -> tuple[str, dict] | None:
        if "engineer" in said:
            conn.drop(1011, "server error")
            return None
        return app_policy("qa")(conn, said)

    server = FakeServer(tool=drop_on_plain)
    result = await verify.check_tools(Ctx(live_settings, connect=server.connect, echo=quiet),
                                      talk="talks/no-such-talk", runs=5)
    assert result.lines[0] == "talks/no-such-talk not found → using talks/test"
    assert result.status == "FAIL" and "4/5" in result.summary  # the dropped turn is a miss, the next one reconnects
    assert len(server.connections) == 2


async def test_check_tools_stops_on_a_billing_close(live_settings):
    def billing(conn: FakeRealtime, said: str) -> None:
        conn.drop(4429, "insufficient quota")

    result = await verify.run_check(Ctx(live_settings, connect=FakeServer(tool=billing).connect, echo=quiet),
                                    "tools", {"talk": "talks/test", "runs": 3})
    assert result.status == "FAIL" and result.fatal and CREDITS_URL in result.summary


async def test_check_idle_logs_the_close_and_proves_recovery(live_settings):
    def idle_out(conn: FakeRealtime) -> None:
        if conn.index == 0:
            conn.push({"type": "session.idle_timeout"})
            conn.drop(1000, "idle timeout")

    server = FakeServer(on_created=idle_out, transcript=lambda conn, said: "I'm back online.")
    result = await verify.check_idle(Ctx(live_settings, connect=server.connect, echo=quiet), wait=5)
    assert result.status == "PASS", result.lines
    text = "\n".join(result.lines)
    assert "socket closed: code 1000 'idle timeout'" in text and "session.idle_timeout" in text
    assert "then a typed turn answered" in text and "I'm back online." in result.summary
    out = live_settings.out_dir / "verify" / "idle"
    events = json.loads((out / "events.json").read_text())
    assert [event["type"] for event in events] == ["session.created", "session.idle_timeout", "(socket closed)"]
    assert events[-1]["code"] == 1000
    assert events[0]["session"]["voice"] == "chloe"  # sessions are logged by their key fields only
    assert (out / "recovery.wav").is_file()
    assert server.connections[1].types() == ["session.update", "conversation.item.create", "response.create"]


async def test_check_idle_with_the_socket_still_open_and_a_failed_reconnect(live_settings):
    server = FakeServer(refuse=lambda n: OSError("no route") if n == 2 else None)
    result = await verify.check_idle(Ctx(live_settings, connect=server.connect, echo=quiet), wait=0.05)
    assert result.status == "FAIL"
    assert "the socket was still open after 0.05 s" in result.summary
    assert "the reconnect FAILED: cannot reach" in result.summary
    assert server.connections[0].client_closed


# ── running checks ───────────────────────────────────────────────────────────────────────────────────────────


async def test_run_checks_stops_all_on_billing_and_prints_dated_lines(monkeypatch, live_settings):
    async def listen(ctx, **options):
        return Result("voices", "LISTEN", ["by ear"], summary="clips written.")

    async def billing(ctx, **options):
        raise VerifyError("Realtime closed the socket (code 4429)", hint=verify.CLOSE_HINTS[4429], fatal=True)

    async def never(ctx, **options):
        raise AssertionError("must not run after a billing failure")

    monkeypatch.setitem(verify.CHECKS, "voices", listen)
    monkeypatch.setitem(verify.CHECKS, "tags", billing)
    monkeypatch.setitem(verify.CHECKS, "latency", never)
    lines: list[str] = []
    code = await verify.run_checks(Ctx(live_settings, echo=lines.append), ["voices", "tags", "latency"],
                                   now=lambda: NOW)
    assert code == 1
    output = "\n".join(lines)
    assert "stopping: billing / key problems fail every later check the same way" in output
    assert ("- **F3 tags + timestamps** (2026-10-23 18:05, `python -m relay.verify tags`): FAIL. could not run: "
            "Realtime closed the socket (code 4429) → billing refused: claim credits at ") in output
    assert "== overview ==" in output and "  latency" not in output


async def test_run_checks_reports_a_crash_and_carries_on(monkeypatch, live_settings):
    async def crash(ctx, **options):
        raise RuntimeError("boom")

    async def fast(ctx, **options):
        return Result("latency", "PASS", summary="fast.")

    monkeypatch.setitem(verify.CHECKS, "tags", crash)
    monkeypatch.setitem(verify.CHECKS, "latency", fast)
    lines: list[str] = []
    assert await verify.run_checks(Ctx(live_settings, echo=lines.append), ["tags", "latency"], now=lambda: NOW) == 1
    output = "\n".join(lines)
    assert "FAIL. crashed: RuntimeError: boom" in output and "`python -m relay.verify latency`): PASS. fast." in output


async def test_run_checks_exits_0_when_nothing_failed(monkeypatch, live_settings):
    async def listen(ctx, **options):
        return Result("voices", "LISTEN", summary="by ear.")

    monkeypatch.setitem(verify.CHECKS, "voices", listen)
    lines: list[str] = []
    assert await verify.run_checks(Ctx(live_settings, echo=lines.append), ["voices"], {"voices": {}},
                                   now=lambda: NOW) == 0
    assert "== overview ==" not in "\n".join(lines)  # one check: its own block is enough
