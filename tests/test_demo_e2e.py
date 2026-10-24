"""The online meeting end to end (contract v3.1, doc/online_meeting_patch.md): real uvicorn + server + Controller +
RealtimeLink + the fake Realtime, and relay.demo_driver as the page's Demo Audio Controller — it plays the facilitator's
replies in real time and streams the prerecorded participants' clips over /ws as 100 ms `source: "clip"` frames, in
real time, each one when its demo.yaml cue holds. The microphone is never used.

The fake Realtime can't transcribe, so it "hears" the clips' lines in the order they are committed (FakeOptions
transcripts). The clips are fake: short speech-like tone bursts generated through relay.clips (the gaps are shortened
for the inline fixture; the rollout-review run keeps its own cues).

**Legacy alignment** (contract v5 §5): the demo scripts' cues (`after_reply: intervention`, `decision`) were written
for the regex engine over checks.yaml, so these runs go through tests/test_e2e.py's `running_app`, which pins
`alignment="legacy"`; the model-based discussion is in tests/test_semantic_flow.py.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import httpx
import pytest

from relay import clips as clip_store
from relay import fake_realtime
from relay.config import ROOT, Settings
from relay.demo_driver import DemoDeck, fetch_clips
from relay.fake_realtime import FakeConnector
from relay.meeting import load_demo
from relay.talk import load_talk
from relay.tts import TTSResult, pcm_to_wav
from tests.test_e2e import check_tool_outputs, running_app
from tests.test_e2e import pytestmark as _e2e_mark  # noqa: F401 - the same "can bind a port" skip
from tests.test_meeting_flow import ALICE_QUESTION, BOB_LINE, CAROL_CONFIRM, CAROL_FIX, write_meeting

pytestmark = _e2e_mark

DEMO_YAML = f"""voices: {{Alice: chloe, Bob: jake, Carol: eleanor}}
steps:
  - id: start
    action: start
    when: {{phase: presentation}}
  - id: alice-question
    speaker: Alice
    say: "{ALICE_QUESTION}"
    when: {{talking_point: wednesday-production, heard_ms: 500}}
  - id: bob-announce
    speaker: Bob
    say: "{BOB_LINE}"
    when: {{phase: discussion, quiet_ms: 400}}
  - id: carol-finalize
    speaker: Carol
    say: "{CAROL_CONFIRM}"
    when: {{after: bob-announce, gap_ms: 900}}
  - id: carol-correct
    speaker: Carol
    say: "{CAROL_FIX}"
    when: {{after_reply: intervention, gap_ms: 300}}
  - id: wrap
    action: phase
    when: {{after_reply: decision, gap_ms: 300}}
"""

# The demo packages with a demo.yaml, each with the cue timeout scale its run needs at the fake's pace (3 s per talking
# point). The facilitator's self-introduction (submission review) presents six points after the interruption, so its
# demo.yaml gives Bob's cue (counted from Alice's clip) its own 3 min timeout; at scale 0.25 that is 45 s against the
# ~23 s the fake takes, the same scale the rollout review needs for its default 90 s cues (22.5 s).
DEMO_PACKAGES = [(ROOT / "talks" / "rollout-review", 0.25), (ROOT / "talks" / "submission-review", 0.25),
                 (ROOT / "talks" / "project-overview", 0.25)]  # (D88: guided talking points, its own 3 min Bob cue)


def speech_like(say: str, voice: str) -> TTSResult:
    """A short fake clip (~1.1 s): three tone bursts with short gaps, one utterance to the participant segmenter
    (the words only matter to the fake Realtime's transcripts)."""
    rate, pcm = 24_000, bytearray()
    for ms, loud in ((100, False), (300, True), (80, False), (300, True), (80, False), (250, True), (100, False)):
        n = rate * ms // 1000
        pcm += struct.pack(f"<{n}h", *((int(6000 * math.sin(2 * math.pi * 230 * i / rate)) if loud else 0)
                                       for i in range(n)))
    return TTSResult(pcm_to_wav(bytes(pcm)), None)


def write_demo_meeting(root: Path) -> Path:
    write_meeting(root)
    (root / "demo.yaml").write_text(DEMO_YAML, encoding="utf-8")
    return root


async def run_demo(tmp_path: Path, talk_dir: Path, *, narration_s: float,
                   timeout_scale: float = 0.25) -> tuple[DemoDeck, FakeConnector]:
    """Fake clips into tmp_path, the server on them, and the whole demo script through the scripted page.
    `timeout_scale` shortens every cue's timeout_ms (90 s by default) for the fake's pace."""
    talk = load_talk(talk_dir)
    demo = load_demo(talk)
    report = clip_store.generate_clips(talk, demo, Settings(), out_root=tmp_path, synth=lambda s, v: speech_like(s, v).wav)
    assert report.ok, report
    lines = tuple(step.say for step in demo.clips)
    connector = FakeConnector(think_s=0.05, speed=50.0, narration_s=narration_s, reply_s=0.8, transcripts=lines)
    async with running_app(tmp_path, connector, talk_dir=talk_dir, narration="live", turn_detection=None) as \
            (app, port):
        assert app.state.input == "clips" and app.state.controller.input_mode == "clips"
        import websockets

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            payload = (await http.get("/api/talk")).json()

            async def get(url: str) -> bytes:
                response = await http.get(url)
                assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
                return response.content

            async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=None) as ws:
                deck = DemoDeck(ws, payload["demo"], {}, talk=payload, strict=True, timeout_scale=timeout_scale)
                hello = await deck.handshake()
                assert hello["demo"] == payload["demo"] and hello["input"] == "clips"
                deck.clips = await fetch_clips(f"http://127.0.0.1:{port}", hello["demo"], get)
                assert sorted(deck.clips) == sorted(step.id for step in demo.clips)
                await deck.run()
                await deck.until(lambda m: m.get("type") == "state" and m.get("banner") == "Meeting wrapped up",
                                 "the meeting wrapped up", 0, 20)
                await deck.close()
        check_tool_outputs(connector)
    return deck, connector


def log_entries(tmp_path: Path, talk_id: str) -> list[dict]:
    return [json.loads(line) for path in sorted((tmp_path / "logs").glob(f"{talk_id}-*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()]


async def test_the_online_meeting_demo_end_to_end_with_prerecorded_participants(tmp_path, monkeypatch):
    # The decision as live (gotchas 2026-10-23 20:15): a save_decision call alone, nothing said, then the spoken
    # confirmation as a follow-up of its own. (talks/rollout-review's run below keeps the fake's spoken decision.)
    monkeypatch.setitem(fake_realtime.FLOW_TEXT, "decision", "")
    # (not under tmp_path/talks/: the generated clips go to <out>/talks/<id>/clips/, which would be the package's own
    # recordings folder)
    talk_dir = write_demo_meeting(tmp_path / "packages" / "meeting")
    deck, connector = await run_demo(tmp_path, talk_dir, narration_s=1.4)
    checks = deck.assess(owner="Bob")
    assert all(ok for ok, _ in checks.values()), checks
    assert not any(result.timed_out or result.unavailable for result in deck.results)

    # The barge-in: Alice's clip, streamed while the facilitator presented wednesday-production, cut it at what the
    # page had played (the truncate carries the frame's played_ms), with no tap and no microphone.
    inbound = deck.inbound()
    barge = next(m for m in inbound if m.get("type") == "barge_in")
    assert (barge["speaker"], barge["clip"], barge["state"]) == ("Alice", "alice-question", "NARRATING")
    assert 500 <= barge["played_ms"] < 1400  # her cue (heard_ms 500) held on that point's own narration
    first = connector.connections[0]
    truncates = [e for e in first.received if e["type"] == "conversation.item.truncate"]
    assert truncates[0]["audio_end_ms"] == barge["played_ms"] > 0
    question = next(m for m in inbound if m.get("type") == "caption" and m.get("role") == "user")
    assert (question["text"], question["speaker"]) == (ALICE_QUESTION, "Alice")
    turn = inbound[inbound.index(barge):inbound.index(question)]
    assert [m.get("open") for m in turn if m.get("type") == "mic"] == []  # no microphone for a participant's clip
    flush = next(m for m in turn if m.get("type") == "reply_flush")
    assert flush["reply"] == barge["reply"]  # the narration the room was hearing, flushed right after the cue
    sent = [e["type"] for e in first.received if e["type"] not in ("input_audio_buffer.append", "response.cancel")]
    cut = sent.index("conversation.item.truncate")
    assert sent[cut:cut + 3] == ["conversation.item.truncate", "session.update", "input_audio_buffer.clear"]
    assert sent[cut + 3:cut + 5] == ["input_audio_buffer.commit", "response.create"]  # the segmenter's end
    appended = [e for e in first.received[first.received.index(truncates[0]):] if e["type"] in (
        "input_audio_buffer.append", "input_audio_buffer.commit")]
    assert appended[0]["type"] == "input_audio_buffer.append"  # the preroll + speech, then the rest, then the commit
    narrations = [e["response"]["metadata"]["talking_point"] for e in first.received
                  if e["type"] == "response.create" and (e.get("response") or {}).get("metadata", {}).get("relay")
                  == "narration"]
    assert narrations == ["friday-beta", "wednesday-production", "wednesday-production", "beta-notes"]

    # The discussion: the clips' prepared speaker labels, never diarization.
    lines = [(m["utterance"]["speaker"], m["utterance"]["text"], m["utterance"]["source"])
             for m in inbound if m.get("type") == "transcript"]
    assert lines == [("Bob", BOB_LINE, "clip"), ("Carol", CAROL_CONFIRM, "clip"), ("Carol", CAROL_FIX, "clip")]
    kinds = [m["kind"] for m in inbound if m.get("type") == "reply_start" and m["kind"] != "narration"]
    # lookup, then answer; the decision: the silent call, then the confirmation
    assert kinds == ["answer", "answer", "handover", "intervention", "decision", "decision", "summary"]
    silent, spoken = [deck.replies[m["reply"]] for m in inbound if m.get("type") == "reply_start"
                      and m["kind"] == "decision"]
    assert silent.audio_bytes == 0 and spoken.audio_bytes > 0
    # the script's Wrap up (after_reply decision) waited for the confirmation the room heard, and never cut it
    assert spoken.drained_at is not None and spoken.flushed_at is None
    wrap = next(r for r in deck.results if r.id == "wrap")
    assert wrap.fired_at >= spoken.drained_at - deck.t0
    errors = [m["message"] for m in inbound if m.get("type") == "error"]
    assert errors == []

    entries = log_entries(tmp_path, "meeting")
    assert [(e["speaker"], e["clip"], e["state"]) for e in entries if e["kind"] == "barge_in"] == [
        ("Alice", "alice-question", "NARRATING")]
    assert not any(e["kind"] in ("controller_error", "narration_failed", "clip_unheard") for e in entries)


@pytest.mark.parametrize("talk_dir, timeout_scale", DEMO_PACKAGES, ids=[path.name for path, _ in DEMO_PACKAGES])
async def test_the_demo_packages_end_to_end(tmp_path, talk_dir, timeout_scale):
    """talks/<id>/demo.yaml as written (its own cues), over the same stack with fake clips."""
    if not (talk_dir / "demo.yaml").is_file():
        pytest.skip(f"{talk_dir.name} has no demo.yaml")
    deck, _ = await run_demo(tmp_path, talk_dir, narration_s=3.0, timeout_scale=timeout_scale)
    checks = deck.assess(owner="Bob")
    assert all(ok for ok, _ in checks.values()), checks
    assert not any(result.timed_out or result.unavailable for result in deck.results)
    entries = log_entries(tmp_path, talk_dir.name)
    barges = [e for e in entries if e["kind"] == "barge_in"]
    assert [e["clip"] for e in barges] == ["alice-question"]
    heard_ms = next(s for s in deck.demo["steps"] if s["id"] == "alice-question")["when"].get("heard_ms", 0)
    assert barges[0]["played_ms"] >= heard_ms and barges[0]["state"] == "NARRATING"
    assert not any(e["kind"] in ("controller_error", "narration_failed") for e in entries)
