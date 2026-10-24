"""scripts/zoom_bot.py, the facilitator in Zoom (contract v5 §2, §5): its pure parts (the join link, the speaker's
name, the frames, the §8 assessment of a recorded run, the terminal keys), the fake Zoom page, the ZoomDeck's mouth
and ears over a fake socket, the injected page script under jsc, and the whole dry run over uvicorn + the fake
Realtime. Playwright is never imported."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import math
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from relay.audio import BYTES_PER_MS, FRAME_BYTES, sine
from relay.config import ROOT
from relay.demo_driver import Reply

SCRIPT = ROOT / "scripts" / "zoom_bot.py"
INJECT = ROOT / "web" / "zoom" / "inject.js"
HARNESS = ROOT / "tests" / "web" / "zoom_inject_check.js"
JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc")


def load_bot():
    spec = importlib.util.spec_from_file_location("zoom_bot", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("zoom_bot", module)
    spec.loader.exec_module(module)
    return module


bot = load_bot()


class FakeWS:
    """A deck socket: records what the deck sends; nothing arrives unless pushed."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def push(self, msg: dict) -> None:
        self._inbox.put_nowait(json.dumps(msg))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        raw = await self._inbox.get()
        if raw is None:
            raise StopAsyncIteration
        return raw


def b64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


def tone_pcm24(ms: int) -> bytes:
    n = 24 * ms
    return struct.pack(f"<{n}h", *(int(8000 * math.sin(2 * math.pi * 300 * i / 24000)) for i in range(n)))


# --- pure helpers ---------------------------------------------------------------------------------------------------


def test_the_join_link_gives_the_meeting_id_and_the_encoded_passcode():
    assert bot.meeting_target("https://us05web.zoom.us/j/85685615991?pwd=abcDEF123") == ("85685615991", "abcDEF123")
    assert bot.meeting_target("https://utoronto.zoom.us/j/8568561599") == ("8568561599", None)
    assert bot.meeting_target("https://app.zoom.us/wc/join/85685615991") == ("85685615991", None)
    assert bot.meeting_target(" 85685615991 ") == ("85685615991", None)
    with pytest.raises(ValueError):
        bot.meeting_target("https://zoom.us/profile")
    assert bot.join_url("app.zoom.us", "85685615991", "a b") == "https://app.zoom.us/wc/join/85685615991?pwd=a%20b"
    assert bot.join_url("app.zoom.us", "85685615991", None) == "https://app.zoom.us/wc/join/85685615991"


def test_the_speaker_name_is_tidied_and_never_the_bot():
    assert bot.clean_speaker("  Ken   Jiang (Host)\n") == "Ken Jiang"
    assert bot.clean_speaker("Alice (Me)") == "Alice" and bot.clean_speaker("Bob (Guest)") == "Bob"
    assert bot.clean_speaker("Facilitator (AI)") is None
    assert bot.clean_speaker("Facilitator (AI) (Host)") is None
    assert bot.clean_speaker("facilitator (ai)") is None
    assert bot.clean_speaker("Unmute") is None and bot.clean_speaker("") is None and bot.clean_speaker(None) is None
    assert bot.clean_speaker("x" * 61) is None and bot.clean_speaker("x" * 60) == "x" * 60
    assert bot.clean_speaker("Carol", bot_name="Carol") is None


def test_a_mic_frame_is_the_voice_path_frame():
    frame = bot.mic_frame(b"\x01\x02", "Alice", {"reply": 4, "played_ms": 1500, "at": None})
    assert frame == {"type": "mic", "source": "voice", "pcm": "AQI=", "speaker": "Alice", "reply": 4,
                     "played_ms": 1500, "at": None}
    assert bot.mic_frame(b"", None, {})["reply"] is None


def test_the_terminal_keys_become_key_events_typed_lines_and_quit():
    parser = bot.KeyParser()
    assert parser.feed(" rR") == [("key", "space"), ("key", "relay"), ("key", "relay")]
    assert parser.feed("\x1b[C\x1b[D") == [("key", "next"), ("key", "prev")]
    assert parser.feed("pHn0") == [("key", "pause"), ("key", "human"), ("key", "phase"), ("key", "reset")]
    assert parser.feed("x") == []  # not a key
    assert parser.feed("t") == [] and parser.typing == ""
    assert parser.feed("hello worlds\x7f") == [] and parser.typing == "hello world"
    assert parser.feed("\r") == [("typed", "hello world")] and parser.typing is None
    assert parser.feed("tdropped\x1b") == [] and parser.typing is None  # Esc drops the typed line
    assert parser.feed("t\n") == []  # an empty line sends nothing
    assert parser.feed("q") == [("quit", None)] and parser.feed("\x03") == [("quit", None)]
    assert parser.feed("\x1b[A") == [("key", "prev")]  # (an unknown escape sequence is swallowed)
    assert parser.feed("\x1b[Z") == []


def recorded_run(*, decision_after_fix: bool = True, with_barge: bool = True) -> list[dict]:
    """A recorded run (frames.jsonl entries) of the submission review as the dry run sees it."""
    t = [0.0]

    def at(dt: float, **msg) -> dict:
        t[0] += dt
        return {"t": round(t[0], 3), "dir": "in", **msg}

    frames = [
        at(1.0, type="state", state="NARRATING", talking_point="interrupt-answer", status="presenting"),
        at(0.1, type="reply_start", reply=4, kind="narration"),
    ]
    if with_barge:
        frames += [at(1.8, type="barge_in", speaker="Alice", clip=None, played_ms=1834, reply=4, state="NARRATING"),
                   at(0.0, type="reply_flush", reply=4)]
    frames += [
        at(0.5, type="reply_start", reply=5, kind="answer"),
        at(0.1, type="evidence", lookup={"id": "E1", "query": "one microphone?", "by": "model",
                                         "results": [{"source_id": "LIM-2"}, {"source_id": "LIM-3"}]}),
        at(0.2, type="reply_start", reply=6, kind="answer"),
        at(0.1, type="caption", role="assistant", final=True, reply=6, text="Online meetings only [LIM-2]."),
        at(1.0, type="state", state="NARRATING", talking_point="interrupt-answer", status="resuming"),
        at(3.0, type="reply_start", reply=13, kind="handover"),
        at(1.0, type="state", state="ROOM", agenda_phase="discussion", status="listening"),
        at(2.0, type="transcript", utterance={"id": "U1", "text": "in person too", "speaker": "Bob", "source": "voice"}),
        at(1.0, type="transcript", utterance={"id": "U2", "text": "let's finalize", "speaker": "Carol", "source": "voice"}),
        at(0.1, type="reply_start", reply=14, kind="intervention"),
    ]
    decision = at(0.0, type="meeting", decisions=[{"decision": "online", "owner": "Bob"}])
    frames += [
        at(2.0, type="transcript", utterance={"id": "U3", "text": "online only, Bob owns it", "speaker": "Carol",
                                              "source": "voice"}),
    ]
    if decision_after_fix:
        decision["t"] = round(t[0] + 0.5, 3)
    frames.append(decision)
    frames += [
        at(2.0, type="state", state="IDLE", banner="Meeting wrapped up"),
        at(0.0, type="meeting", phase="wrapup", decisions=[{"decision": "online", "owner": "Bob"}],
           notes={"decisions": [{"decision": "online", "owner": "Bob"}]}),
        {"t": round(t[0], 3), "dir": "out", "type": "reply_drained", "reply": 6, "played_ms": 1000},
    ]
    return sorted(frames, key=lambda f: f["t"])


def test_the_assessment_reads_the_definition_of_done_off_a_recorded_run():
    checks = bot.assess(recorded_run(), participants=["Alice", "Bob", "Carol"])
    assert set(checks) == {"barge_in", "lookup_answer", "resumed", "intervention", "silent", "decision", "notes",
                           "labels"}
    assert all(ok for ok, _ in checks.values()), checks
    assert "1834 ms in (point interrupt-answer)" in checks["barge_in"][1]
    assert "LIM-2" in checks["lookup_answer"][1] and "resumed at interrupt-answer" in checks["resumed"][1]
    assert checks["intervention"][1].startswith("0.1 s after the confirming line")


def test_the_assessment_fails_a_decision_recorded_before_the_correction_and_a_run_without_a_barge_in():
    checks = bot.assess(recorded_run(decision_after_fix=False), participants=["Alice", "Bob", "Carol"])
    assert not checks["decision"][0] and "before the correction" in checks["decision"][1]
    assert checks["notes"][0]
    checks = bot.assess(recorded_run(with_barge=False), participants=["Alice", "Bob", "Carol"])
    assert not checks["barge_in"][0] and not checks["resumed"][0]
    checks = bot.assess(recorded_run(), participants=["Alice", "Bob"])  # Carol is a stranger here
    assert not checks["labels"][0]
    assert bot.assess([], participants=[])["intervention"] == (False, "the facilitator never spoke up")


def test_the_summary_lists_replies_barge_ins_and_the_notes():
    summary = bot.summarize(recorded_run(), joined=True, shared=False)
    assert summary["joined"] is True and summary["shared"] is False
    assert [(r["reply"], r["kind"]) for r in summary["replies"]][:3] == [(4, "narration"), (5, "answer"), (6, "answer")]
    assert next(r for r in summary["replies"] if r["reply"] == 4)["flushed"]
    assert next(r for r in summary["replies"] if r["reply"] == 6)["drained_ms"] == 1000
    assert summary["barge_ins"][0]["speaker"] == "Alice" and summary["wrapped_up"]
    assert [u["speaker"] for u in summary["utterances"]] == ["Bob", "Carol", "Carol"]
    assert summary["notes"]["decisions"][0]["owner"] == "Bob" and summary["evidence"][0]["top"] == ["LIM-2", "LIM-3"]


# --- the fake Zoom page -----------------------------------------------------------------------------------------------


async def test_the_fake_zoom_page_plays_a_clip_in_real_time_and_names_its_speaker():
    page = bot.FakeZoomPage()
    first = await page.drain()
    assert first["rate"] == 48000 and first["pcm"] == "" and first["speaker"] is None and first["position"]["drained"]
    await asyncio.sleep(0.1)
    quiet = await page.drain()
    n = len(base64.b64decode(quiet["pcm"])) // 4
    assert 4000 <= n <= 6500 and set(base64.b64decode(quiet["pcm"])) == {0}  # ~100 ms of 48 kHz silence
    ends = page.speak("alice-question", "Alice", tone_pcm24(150))
    assert page.speaking == "Alice" and ends - time.monotonic() <= 0.151
    await asyncio.sleep(0.2)
    loud = await page.drain()
    samples = base64.b64decode(loud["pcm"])
    assert loud["speaker"] is None  # (the clip ended inside this drain)
    assert len(samples) // 4 >= 7200 and any(samples[:7200 * 4])  # the clip's 7200 samples at 48 kHz, then silence
    assert set(samples[-800:]) == {0}


async def test_the_fake_zoom_page_counts_what_the_mouth_is_given():
    page = bot.FakeZoomPage()
    pos = await page.push(b64(bytes(500 * BYTES_PER_MS)), 1)
    assert pos["key"] == 1 and pos["total_ms"] == 500 and not pos["drained"]
    pos = await page.push(b64(bytes(300 * BYTES_PER_MS)), 1)
    assert pos["total_ms"] == 800 and page.pushed[1] == 800 * BYTES_PER_MS
    await asyncio.sleep(0.25)
    pos = await page.position()
    assert 150 <= pos["played_ms"] <= 350 and pos["playing"]
    pos = await page.push(b64(bytes(100 * BYTES_PER_MS)), 2)  # a newer reply replaces the player
    assert pos["key"] == 2 and pos["total_ms"] == 100 and page.flushes[0][0] == 1
    await asyncio.sleep(0.2)
    assert (await page.position())["drained"]
    out = await page.flush(2)
    assert out["flushed"] and (await page.position())["key"] is None
    assert (await page.flush(2))["flushed"] is False


# --- the ZoomDeck: mouth, ears, keys ----------------------------------------------------------------------------------


async def settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


async def test_the_mouth_queues_reply_audio_and_drains_the_reply_when_the_page_has_played_it():
    ws, page = FakeWS(), bot.FakeZoomPage()
    deck = bot.ZoomDeck(ws, page, server="http://127.0.0.1:1", talk=None)
    try:
        ws.push({"type": "reply_start", "reply": 1, "kind": "answer"})
        ws.push({"type": "reply_audio", "reply": 1, "pcm": b64(bytes(200 * BYTES_PER_MS))})
        ws.push({"type": "reply_audio", "reply": 1, "pcm": b64(bytes(100 * BYTES_PER_MS))})
        await settle()
        assert page.pushed == {1: 300 * BYTES_PER_MS}
        where = deck.position(time.monotonic())
        assert where["reply"] == 1 and 0 <= where["played_ms"] <= 100
        ws.push({"type": "reply_end", "reply": 1})
        await settle(0.15)
        assert not [m for m in ws.sent if m["type"] == "reply_drained"]  # 300 ms not played yet
        await settle(0.3)
        drained = [m for m in ws.sent if m["type"] == "reply_drained"]
        assert drained == [{"type": "reply_drained", "reply": 1, "played_ms": 300}]
        assert deck.current_reply() is None
    finally:
        await deck.close()


async def test_a_flush_and_a_mic_open_drop_the_pages_queue_and_a_flushed_reply_never_drains():
    ws, page = FakeWS(), bot.FakeZoomPage()
    deck = bot.ZoomDeck(ws, page, server="http://127.0.0.1:1")
    try:
        ws.push({"type": "reply_start", "reply": 2, "kind": "narration"})
        ws.push({"type": "reply_audio", "reply": 2, "pcm": b64(bytes(2000 * BYTES_PER_MS))})
        await settle()
        ws.push({"type": "reply_flush", "reply": 2})
        ws.push({"type": "reply_end", "reply": 2})
        await settle(0.1)
        assert page.flushes and page.flushes[0][0] == 2 and (await page.position())["key"] is None
        assert deck.replies[2].flushed_at is not None and deck.position(time.monotonic())["reply"] is None
        await settle(0.1)
        assert not [m for m in ws.sent if m["type"] == "reply_drained"]
        # the backend opens the mic during an answer: the page silences it (not during an intervention)
        ws.push({"type": "reply_start", "reply": 3, "kind": "answer"})
        ws.push({"type": "reply_audio", "reply": 3, "pcm": b64(bytes(1000 * BYTES_PER_MS))})
        await settle()
        ws.push({"type": "mic", "open": True})
        await settle()
        assert deck.replies[3].flushed_at is not None and (await page.position())["key"] is None
        ws.push({"type": "reply_start", "reply": 4, "kind": "intervention"})
        ws.push({"type": "reply_audio", "reply": 4, "pcm": b64(bytes(1000 * BYTES_PER_MS))})
        await settle()
        ws.push({"type": "mic", "open": True})
        await settle()
        assert deck.replies[4].flushed_at is None and (await page.position())["key"] == 4
    finally:
        await deck.close()


async def test_a_newer_reply_resets_the_player_and_the_older_one_never_drains():
    ws, page = FakeWS(), bot.FakeZoomPage()
    deck = bot.ZoomDeck(ws, page, server="http://127.0.0.1:1")
    try:
        ws.push({"type": "reply_start", "reply": 5, "kind": "answer"})
        ws.push({"type": "reply_audio", "reply": 5, "pcm": b64(bytes(100 * BYTES_PER_MS))})
        ws.push({"type": "reply_end", "reply": 5})
        ws.push({"type": "reply_start", "reply": 6, "kind": "answer"})
        await settle(0.1)
        assert (await page.position())["key"] is None  # the page's player was reset for reply 6
        await settle(0.3)
        assert not [m for m in ws.sent if m["type"] == "reply_drained"]
        assert deck.position(time.monotonic())["reply"] == 6
        ws.push({"type": "reply_end", "reply": 6})  # a silent reply drains at once
        await settle(0.1)
        assert [m["reply"] for m in ws.sent if m["type"] == "reply_drained"] == [6]
    finally:
        await deck.close()


async def test_the_ears_resample_the_tap_into_voice_frames_with_the_speaker_and_the_position():
    ws, page = FakeWS(), bot.FakeZoomPage()
    deck = bot.ZoomDeck(ws, page, server="http://127.0.0.1:1")
    try:
        ws.push({"type": "reply_start", "reply": 7, "kind": "narration"})
        ws.push({"type": "reply_audio", "reply": 7, "pcm": b64(bytes(3000 * BYTES_PER_MS))})
        await settle(0.12)
        tone = sine(1000, 0.25, 48_000, amplitude=0.5)  # 250 ms of the tap
        await deck.hear({"events": ["1.0s tap webaudio-1: context at 48000 Hz"], "rate": 48000,
                         "pcm": b64(tone.tobytes()), "peak_db": -9, "position": await page.position(),
                         "speaker": "Ken Jiang (Host)"})
        frames = [m for m in ws.sent if m["type"] == "mic"]
        assert len(frames) == 2 and deck._framer.carried == 50 * BYTES_PER_MS  # 250 ms → 2 frames + 50 ms carried
        assert all(m["source"] == "voice" and m["speaker"] == "Ken Jiang" and len(base64.b64decode(m["pcm"]))
                   == FRAME_BYTES for m in frames)
        assert frames[0]["reply"] == 7 and 50 <= frames[0]["played_ms"] <= 300 and frames[0]["at"] is None
        assert len(deck.heard) == 12_000  # 250 ms at 24 kHz = 6000 samples of PCM16
        assert deck.page_events == ["1.0s tap webaudio-1: context at 48000 Hz"] and deck.speakers_seen == {"Ken Jiang": 1}
        await deck.hear({"rate": 48000, "pcm": "", "position": await page.position(), "speaker": "Facilitator (AI)"})
        assert deck.speaker is None
        # the ears loop itself, on the fake page: silence streams as frames in real time
        page.speak("x", "Bob", tone_pcm24(120))
        ears = asyncio.create_task(deck.ears())
        await settle(0.45)
        deck.stopped.set()
        await asyncio.wait_for(ears, 2)
        frames = [m for m in ws.sent if m["type"] == "mic"][2:]
        assert 4 <= len(frames) <= 7 and frames[0]["speaker"] == "Bob" and frames[-1]["speaker"] is None
    finally:
        await deck.close()


async def test_keys_carry_the_position_and_cut_as_the_page_does():
    ws, page = FakeWS(), bot.FakeZoomPage()
    deck = bot.ZoomDeck(ws, page, server="http://127.0.0.1:1")
    try:
        ws.push({"type": "reply_start", "reply": 8, "kind": "answer"})  # no audio yet
        await settle()
        await deck.key("space")
        key = ws.sent[-1]
        assert key == {"type": "key", "key": "space", "at": None, "reply": 8, "reply_played_ms": 0}
        assert deck.replies[8].flushed_at is None  # a silent reply is left to the backend
        ws.push({"type": "reply_audio", "reply": 8, "pcm": b64(bytes(2000 * BYTES_PER_MS))})
        await settle(0.12)
        await deck.key("space")
        key = ws.sent[-1]
        assert key["reply"] == 8 and 50 <= key["reply_played_ms"] <= 300 and deck.replies[8].flushed_at is not None
        await settle()
        assert (await page.position())["key"] is None
        await deck.key("relay")
        assert ws.sent[-1] == {"type": "key", "key": "relay", "at": None, "reply": None, "reply_played_ms": None}
        ws.push({"type": "reply_start", "reply": 9, "kind": "decision"})
        ws.push({"type": "reply_audio", "reply": 9, "pcm": b64(bytes(1000 * BYTES_PER_MS))})
        await settle()
        await deck.typed("Noted, thanks")
        typed = ws.sent[-1]
        assert typed["type"] == "typed" and typed["text"] == "Noted, thanks" and typed["speaker"] is None
        assert typed["reply"] == 9 and deck.replies[9].flushed_at is not None
    finally:
        await deck.close()


async def test_a_backup_chunk_without_audio_plays_for_its_duration_then_chunk_ended():
    talk = {"slides": [{"id": "s1", "chunks": [{"index": 0, "duration_ms": 200}]}]}
    ws, page = FakeWS(), bot.FakeZoomPage()
    deck = bot.ZoomDeck(ws, page, server="http://127.0.0.1:1", talk=talk)  # (no server: the fetch fails)
    try:
        ws.push({"type": "play", "play_id": 3, "slide": "s1", "chunk": 0, "offset_ms": 50})
        await settle(0.05)
        assert deck.audible(time.monotonic()) and deck.position(time.monotonic())["at"]["play_id"] == 3
        await settle(0.4)
        assert [m for m in ws.sent if m["type"] == "chunk_ended"] == [{"type": "chunk_ended", "play_id": 3}]
        assert not deck.audible(time.monotonic())
        ws.push({"type": "play", "play_id": 4, "slide": "s1", "chunk": 0, "offset_ms": 0})
        await settle(0.05)
        ws.push({"type": "stop"})
        await settle(0.4)
        assert [m["play_id"] for m in ws.sent if m["type"] == "chunk_ended"] == [3]  # never for a stopped chunk
    finally:
        await deck.close()


# --- the injected page script -----------------------------------------------------------------------------------------


@pytest.mark.skipif(not JSC.exists(), reason="JavaScriptCore's jsc is macOS only")
def test_the_injected_page_script_under_jsc():
    result = subprocess.run([str(JSC), str(HARNESS), "--", str(INJECT)], capture_output=True, text=True, timeout=60)
    failed = [line for line in result.stdout.splitlines() if line.startswith("FAIL")]
    assert result.returncode == 0 and not failed and "all checks passed" in result.stdout, \
        result.stdout + result.stderr


# --- the dry run, end to end ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("talk_id", ["submission-review", "rollout-review"])
async def test_the_dry_runs_scripted_analysis_survives_the_real_validation(talk_id):
    """DRY_RUN_ANALYSIS stands in for Claude in the dry run: its quotes must be verbatim in the talk's passages (else
    reconcile drops them and the dry run would prove nothing), and its verdicts must give one question at the
    confirmation and a resolution with Bob as the owner."""
    from relay import semantic
    from relay.meeting import MeetingState, load_demo, load_meeting
    from relay.talk import load_talk

    talk = load_talk(ROOT / "talks" / talk_id)
    package, demo = load_meeting(talk), load_demo(talk)
    analyzer = bot.dry_run_analyzer(talk, demo)
    assert analyzer is not None and analyzer.model == "dry-run-script"
    spec = bot.DRY_RUN_ANALYSIS[talk_id]
    says = {step.id: step.say for step in demo.clips}
    state = MeetingState(package, lambda: 0.0)
    state.set_phase("discussion")
    seen = 0

    async def after(step: str, speaker: str) -> tuple[semantic.Reconciled, semantic.Snapshot]:
        nonlocal seen
        state.add_utterance(says[spec[step]], speaker, "voice")
        new = [u.id for u in state.transcript[seen:]]
        seen = len(state.transcript)
        snapshot = semantic.build_snapshot(package, state, generation=0, new_utterance_ids=new)
        outcome = await analyzer.analyze(snapshot)
        assert outcome.error is None
        return semantic.reconcile(outcome.result, snapshot), snapshot

    rec, snap = await after("claim", "Bob")
    (claim,) = rec.issues
    assert claim.evidence_ok and not claim.dropped and semantic.decide_policy(claim, snap)[0] == "silent"
    record = state.add_issue(status="pending", issue_type="evidence_conflict", topic=claim.model.topic,
                             claim=claim.model.claim, claim_utterance_ids=claim.claim_utterance_ids,
                             participant_views=[], missing_details=[], evidence=claim.evidence,
                             material_consequence=None, commitment="proposed", confidence="high",
                             recommendation="silent", question=claim.model.question, revision=1)
    rec, snap = await after("confirm", "Carol")
    (confirm,) = rec.issues
    assert confirm.existing_id == record.id and semantic.decide_policy(confirm, snap)[0] == "clarify"
    rec, snap = await after("fix", "Carol")
    assert [(u.issue_id, u.status) for u in rec.updates] == [(record.id, "resolved")]
    (decision,) = rec.proposals
    assert (decision.owner, decision.resolves_issue_ids, decision.dropped) == ("Bob", [record.id], [])


@pytest.mark.slow
@pytest.mark.skipif(not __import__("tests.test_e2e", fromlist=["_can_bind"])._can_bind(),
                    reason="local port binding is not allowed here")
async def test_the_dry_run_end_to_end_over_the_fake_realtime(tmp_path):
    """`scripts/zoom_bot.py --dry-run` as the CLI runs it, with fake renders and clips under tmp_path: the fake Zoom
    page plays the submission review's clips on their cues into the bot's ears, the bot is the deck of
    `--input voice`, and the §8 definition of done holds."""
    lines: list[str] = []
    summary = await bot.dry_run(ROOT / "talks" / "submission-review", out_root=tmp_path, out_dir=tmp_path / "zoom_bot",
                                log=lines.append, narration_s=2.5)
    checks = summary["checks"]
    assert all(c["ok"] for c in checks.values()), {k: v["seen"] for k, v in checks.items() if not v["ok"]}
    # the model-based check (the default), its analysis scripted: one analysis per discussion line at least
    assert summary["alignment"] == "claude" and summary["analyses"] >= 3
    assert any("scripted stand-in for Claude" in line for line in lines)
    assert summary["wrapped_up"] and not summary["backup_plays"] and not summary["errors"]
    assert not any(step["timed_out"] or step["unavailable"] for step in summary["steps"])
    assert summary["barge_ins"][0]["speaker"] == "Alice" and summary["barge_ins"][0]["state"] == "NARRATING"
    assert 1500 <= summary["barge_ins"][0]["played_ms"] < 2500
    assert [u["speaker"] for u in summary["utterances"]] == ["Bob", "Carol", "Carol"]
    assert set(summary["speakers_seen"]) == {"Alice", "Bob", "Carol"} and summary["frames_sent"] > 300
    assert summary["heard_ms"] >= 30_000
    kinds = [r["kind"] for r in summary["replies"]]
    assert kinds.count("narration") >= 9 and "intervention" in kinds and "decision" in kinds and kinds[-1] == "summary"
    cut = [r for r in summary["replies"] if r["flushed"]]
    assert len(cut) == 1 and cut[0]["kind"] == "narration"
    assert all(r["drained_ms"] is not None for r in summary["replies"] if not r["flushed"] and r["kind"] != "answer")
    out = tmp_path / "zoom_bot"
    assert (out / "frames.jsonl").is_file() and (out / "summary.json").is_file()
    heard = out / "heard.wav"
    assert heard.is_file() and heard.stat().st_size > 30_000 * BYTES_PER_MS
    assert any("Alice speaks into the fake Zoom" in line for line in lines)
