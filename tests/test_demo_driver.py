"""relay.demo_driver, the scripted page of the e2e tests and scripts/live_check.py, evaluates demo.yaml's cues and
fills the clip frames the way the page does (web/js/demo.js cueStatus, main.js syncDemo / sendClipFrame, audio.js
ReplyPlayer.snapshot). These pin the cases where the two used to disagree (integration of contract v3.1)."""

from __future__ import annotations

import asyncio
import json
import time

from relay.demo_driver import BYTES_PER_MS, DemoDeck, Reply


class FakeWS:
    """A deck socket: records what the deck sends; nothing arrives unless pushed."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        raw = await self._inbox.get()
        if raw is None:
            raise StopAsyncIteration
        return raw


def deck_with(steps: list[dict] | None = None, clips: dict[str, bytes] | None = None, **kw) -> tuple[DemoDeck, FakeWS]:
    ws = FakeWS()
    return DemoDeck(ws, {"steps": steps or []}, clips or {}, **kw), ws


def playing(deck: DemoDeck, number: int, kind: str, *, heard_s: float, audio_ms: int = 5000) -> Reply:
    """A reply that has been playing for `heard_s` (its audio arrived, reply_end not yet)."""
    reply = Reply(number, kind, audio_bytes=audio_ms * BYTES_PER_MS, first_audio=time.monotonic() - heard_s)
    deck.replies[number] = reply
    return reply


async def test_a_talking_point_cue_holds_while_that_point_resumes_as_on_the_page():
    deck, _ = deck_with()
    try:
        playing(deck, 3, "narration", heard_s=2.0)
        when = {"talking_point": "rollback-trigger", "heard_ms": 1500}
        now = time.monotonic()
        for status, holds in (("presenting", True), ("resuming", True), ("listening", False), ("answering", False)):
            deck.state = {"talking_point": "rollback-trigger", "status": status}
            assert deck.cue_holds(when, 0.0, now) is holds, status
        deck.state = {"talking_point": "rollback-trigger", "status": "presenting"}
        assert not deck.cue_holds({"talking_point": "rollback-trigger", "heard_ms": 2500}, 0.0, now)
        assert not deck.cue_holds({"talking_point": "friday-beta", "heard_ms": 0}, 0.0, now)
    finally:
        await deck.close()


async def test_a_talking_point_cue_holds_on_the_backup_narration_while_realtime_is_up():
    """A live narration that failed twice: the backup presents the point (`play`, no reply). Alice may cut in on it as
    on the page — but not while Realtime is down: the backend drops clips then (she'd talk over it unheard)."""
    deck, _ = deck_with()
    try:
        deck.state = {"talking_point": "rollback-trigger", "status": "presenting", "slide": "rollback",
                      "checkpoint": {"slide_id": "rollback", "talking_point_id": "rollback-trigger",
                                     "status": "presenting"}}
        deck.connection = "ready"
        when = {"talking_point": "rollback-trigger", "heard_ms": 1500}
        deck._chunk = {"play_id": 1, "slide": "rollback", "chunk": 0, "offset": 0, "start": time.monotonic() - 1.0}
        assert not deck.cue_holds(when, 0.0, time.monotonic())  # 1 s of it heard
        deck._chunk["start"] = time.monotonic() - 1.6
        assert deck.cue_holds(when, 0.0, time.monotonic())
        deck.connection = "reconnecting"
        assert not deck.cue_holds(when, 0.0, time.monotonic())
        deck.connection = "ready"
        deck._chunk["slide"] = "plan"
        assert not deck.cue_holds(when, 0.0, time.monotonic())  # another slide's narration
        deck._react({"type": "connection", "status": "offline", "detail": None})
        assert deck.connection == "offline"
    finally:
        await deck.close()


async def test_the_previous_points_narration_never_counts_for_the_next_point():
    deck, _ = deck_with()
    try:
        old = playing(deck, 2, "narration", heard_s=3.0, audio_ms=2400)
        old.ended, old.drained_at = True, time.monotonic() - 0.4
        deck.replies[3] = Reply(3, "narration")  # reply_start of the next point: no audio yet
        deck.state = {"talking_point": "rollback-trigger", "status": "presenting"}
        now = time.monotonic()
        assert not deck.cue_holds({"talking_point": "rollback-trigger", "heard_ms": 1500}, 0.0, now)
        assert deck.cue_holds({"talking_point": "rollback-trigger"}, 0.0, now)  # heard 0 ≥ 0, as the page says
        # the page's snapshot exists from reply_start: the frames carry the new reply at 0 ms
        assert deck.position(now) == {"reply": 3, "played_ms": 0, "at": None}
    finally:
        await deck.close()


async def test_clip_frames_carry_nulls_when_no_reply_plays_and_never_an_older_reply():
    deck, _ = deck_with()
    try:
        assert deck.position(time.monotonic()) == {"reply": None, "played_ms": None, "at": None}
        playing(deck, 2, "narration", heard_s=1.0)  # never drained nor flushed here (a lost message)…
        done = playing(deck, 3, "answer", heard_s=2.0, audio_ms=1200)
        done.ended, done.drained_at = True, time.monotonic()
        # …but the page has one current reply, the newest, and it played out
        assert deck.position(time.monotonic()) == {"reply": None, "played_ms": None, "at": None}
        live = playing(deck, 4, "narration", heard_s=0.8)
        where = deck.position(time.monotonic())
        assert where["reply"] == 4 and 750 <= where["played_ms"] <= 900
        live.flushed_at = time.monotonic()
        assert deck.position(time.monotonic())["reply"] is None
    finally:
        await deck.close()


async def test_a_reply_replaced_by_a_newer_one_never_drains_as_on_the_page():
    deck, ws = deck_with()
    try:
        old = playing(deck, 5, "answer", heard_s=0.0, audio_ms=200)
        deck._react({"type": "reply_end", "reply": 5})
        deck._react({"type": "reply_start", "reply": 6, "kind": "answer"})  # the page's player resets for it
        await asyncio.sleep(0.4)
        assert old.drained_at is None and not [m for m in ws.sent if m["type"] == "reply_drained"]
        assert deck.position(time.monotonic())["reply"] == 6
    finally:
        await deck.close()


async def test_gap_ms_counts_from_both_after_and_after_reply():
    deck, _ = deck_with()
    try:
        now = time.monotonic()
        armed = now - 5.0
        question = playing(deck, 7, "intervention", heard_s=4.0, audio_ms=1000)
        question.ended, question.drained_at = True, now - 2.0
        when = {"after": "carol-finalize", "after_reply": "intervention", "gap_ms": 1000}
        deck._ended["carol-finalize"] = now - 0.5
        assert not deck.cue_holds(when, armed, now)  # (the clip ended only 0.5 s ago)
        deck._ended["carol-finalize"] = now - 1.5
        assert deck.cue_holds(when, armed, now)
        question.drained_at = now - 0.5
        assert not deck.cue_holds(when, armed, now)  # (the reply drained only 0.5 s ago)
    finally:
        await deck.close()


async def test_after_reply_counts_from_the_first_such_reply_since_the_previous_step():
    deck, _ = deck_with()
    try:
        now = time.monotonic()
        for number, ago in ((8, 2.5), (9, 0.3)):
            reply = playing(deck, number, "decision", heard_s=ago + 1, audio_ms=1000)
            reply.ended, reply.drained_at = True, now - ago
        assert deck.cue_holds({"after_reply": "decision", "gap_ms": 2000}, now - 5.0, now)
        assert not deck.cue_holds({"after_reply": "decision", "gap_ms": 2000}, now - 1.0, now)  # (only 9 counts)
        # a reply whose kind never arrived is an answer, as the page calls it
        reply = playing(deck, 10, None, heard_s=2.0, audio_ms=1000)
        reply.ended, reply.drained_at = True, now - 0.1
        assert deck.cue_holds({"after_reply": "answer"}, now - 1.0, now)
    finally:
        await deck.close()


async def test_a_silent_reply_never_counts_for_after_reply_as_on_the_page():
    """Live, a decision often comes as a save_decision call alone (no audio: the page's player drains it at once),
    then the spoken confirmation as the next reply of the same kind: only that one is the cue."""
    deck, _ = deck_with()
    try:
        now = time.monotonic()
        deck.replies[9] = Reply(9, "decision", ended=True, drained_at=now - 2.5)  # nothing was heard
        when = {"after_reply": "decision", "gap_ms": 2000}
        assert not deck.cue_holds(when, now - 5.0, now)
        spoken = playing(deck, 10, "decision", heard_s=4.0, audio_ms=3500)
        spoken.ended, spoken.drained_at = True, now - 0.5
        assert not deck.cue_holds(when, now - 5.0, now)  # (the confirmation ended only 0.5 s ago)
        assert deck.cue_holds(when, now - 5.0, now + 1.5)
    finally:
        await deck.close()


async def test_a_quiet_cue_after_a_reply_waits_while_the_facilitator_still_speaks_up():
    """talks/rollout-review's wrap cue (after_reply decision + phase discussion + quiet_ms): a spoken decision reply
    with a lookup gets a follow-up after it; the backend stays INTERVENING until that one has played."""
    deck, _ = deck_with()
    try:
        when = {"after_reply": "decision", "phase": "discussion", "quiet_ms": 2000}
        now = time.monotonic()
        first = playing(deck, 9, "decision", heard_s=4.0, audio_ms=1000)
        first.ended, first.drained_at = True, now - 2.5
        deck.state = {"agenda_phase": "discussion", "state": "INTERVENING"}
        deck._quiet_since = now - 2.5
        assert not deck.cue_holds(when, now - 10.0, now)  # the follow-up is still to come
        deck.state = {"agenda_phase": "discussion", "state": "ROOM"}
        deck._quiet_since = now - 1.0
        assert not deck.cue_holds(when, now - 10.0, now)
        deck._quiet_since = now - 2.1
        assert deck.cue_holds(when, now - 10.0, now)
    finally:
        await deck.close()


async def test_the_backup_narration_is_audible_and_its_position_rides_along():
    deck, _ = deck_with()
    try:
        deck.state = {"agenda_phase": "discussion", "state": "ROOM"}
        deck._quiet_since = time.monotonic() - 5.0
        assert deck.cue_holds({"phase": "discussion", "quiet_ms": 1500}, 0.0, time.monotonic())
        deck._chunk = {"play_id": 4, "slide": "rollback", "chunk": 1, "offset": 300, "start": time.monotonic() - 1.0}
        deck._observe_quiet()
        assert deck.audible(time.monotonic()) and deck.quiet_ms(time.monotonic()) == 0.0
        assert not deck.cue_holds({"phase": "discussion", "quiet_ms": 1500}, 0.0, time.monotonic())
        at = deck.position(time.monotonic())["at"]
        assert (at["play_id"], at["slide"], at["chunk"]) == (4, "rollback", 1) and 1250 <= at["t_ms"] <= 1450
        deck._stop_chunk()
        deck._observe_quiet()
        assert not deck.audible(time.monotonic()) and deck.position(time.monotonic())["at"] is None
    finally:
        await deck.close()


async def test_after_may_name_an_action_step_or_a_skipped_clip():
    pcm = bytes(100 * BYTES_PER_MS)  # 0.1 s of a clip
    steps = [{"id": "start", "action": "start", "when": {"timeout_ms": 1000}},
             {"id": "missing", "speaker": "Alice", "say": "no clip for me", "when": {"after": "start"}},
             {"id": "bob", "speaker": "Bob", "say": "hi", "when": {"after": "missing", "timeout_ms": 1500}},
             {"id": "carol", "speaker": "Carol", "say": "yes", "when": {"after": "start", "timeout_ms": 1500}}]
    deck, ws = deck_with(steps, {"bob": pcm, "carol": pcm}, strict=True)
    try:
        results = await asyncio.wait_for(deck.run(), 10)
    finally:
        await deck.close()
    assert [(r.id, r.timed_out, r.unavailable) for r in results] == [
        ("start", False, False), ("missing", False, True), ("bob", False, False), ("carol", False, False)]
    keys = [m for m in ws.sent if m["type"] == "key"]
    assert keys == [{"type": "key", "key": "relay", "at": None, "reply": None, "reply_played_ms": None}]
    frames = [m for m in ws.sent if m["type"] == "mic"]
    assert {m["clip"] for m in frames} == {"bob", "carol"} and all(m["source"] == "clip" for m in frames)
    assert all(set(m) == {"type", "source", "pcm", "speaker", "clip", "tail", "reply", "played_ms", "at"}
               for m in frames)
    carol = [m["tail"] for m in frames if m["clip"] == "carol"]  # (the last clip: nothing cuts its silence)
    assert carol == [False] + [True] * 12  # 0.1 s of clip, then its 1.2 s of silence, flagged


async def test_the_phase_action_carries_the_reply_heard_then_silences_it():
    steps = [{"id": "wrap", "action": "phase", "when": {}}]
    deck, ws = deck_with(steps)
    try:
        reply = playing(deck, 5, "answer", heard_s=1.0, audio_ms=3000)
        await asyncio.wait_for(deck.run(), 5)
    finally:
        await deck.close()
    key = next(m for m in ws.sent if m["type"] == "key")
    assert key["key"] == "phase" and key["reply"] == 5 and 950 <= key["reply_played_ms"] <= 1100
    assert reply.flushed_at is not None and reply.drained_at is None  # (the page's silence(): it never drains)
    assert deck.position(time.monotonic())["reply"] is None
