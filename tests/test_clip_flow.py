"""The online meeting's participant clips in the controller (contract v3.1 §3), through tests/test_controller.py's
recording ports and manual clock, on the small meeting package of tests/test_meeting_flow.py plus a demo.yaml.

A clip frame is `{type: mic, source: clip, pcm, speaker, clip, reply, played_ms}`: the deck streams it while it plays a
prerecorded participant, in every state. The participant segmenter's start decides: a barge-in on the facilitator
(narration, an answer, an intervention), a question in a pause, or a line of the discussion. `--input mic` ignores
clip frames and keeps today's tap floor control.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from relay.config import Settings
from relay.controller import (
    ERR_OFFLINE_CLIP, IDLE, INTERVENING, LISTENING, NARRATING, PAUSED, REPLYING, ROOM, WRAPUP_WAIT_S, Cursor,
)
from relay.talk import Talk, load_talk
from tests.conftest import synthetic_chunks
from tests.test_controller import AUDIO_100MS, Harness, call
from tests.test_demo_e2e import write_demo_meeting
from tests.test_meeting_flow import (
    ALICE_QUESTION, ANSWER, BOB_LINE, CAROL_CONFIRM, CAROL_FIX, TONE, creates, outputs, present_first_point,
    respond, room_line, to_discussion, to_intervention,
)

SILENCE = AUDIO_100MS
TAIL_FRAMES = 12  # the deck streams 1.2 s of silence after a clip, flagged `tail` (its first frame ends the line)
FALLBACK_END_FRAMES = 11  # frames without the flag: the participant segmenter's own end (CLIP_END_MS, 1.1 s)


@pytest.fixture
def demo_talk(tmp_path: Path) -> Talk:
    return load_talk(write_demo_meeting(tmp_path / "meeting"))


@pytest.fixture
def mk(demo_talk: Talk):
    made: list[Harness] = []

    def factory(*, settings: Settings | None = None, ready: bool = True) -> Harness:
        harness = Harness(demo_talk, synthetic_chunks(demo_talk), settings or Settings(narration="live"),
                          ready=ready)
        made.append(harness)
        return harness

    yield factory
    for harness in made:
        harness.check_invariants()


@pytest.fixture
def h(mk) -> Harness:
    return mk()


async def frame(h: Harness, pcm: str = TONE, *, speaker: str = "Alice", clip: str = "alice-question",
                reply: int | None = None, played: int | None = None, at: dict | None = None,
                tail: bool | None = False) -> None:
    msg = {"type": "mic", "source": "clip", "pcm": pcm, "speaker": speaker, "clip": clip, "reply": reply,
           "played_ms": played}
    if tail is not None:  # (None: a deck that doesn't flag its tail)
        msg["tail"] = tail
    if at is not None:
        msg["at"] = at
    await h.send(msg)


async def speak(h: Harness, frames: int = 3, **kwargs) -> None:
    """`frames` × 100 ms of a participant's voice (the segmenter starts after 200 ms of it)."""
    for _ in range(frames):
        await frame(h, TONE, **kwargs)


async def stop(h: Harness, **kwargs) -> None:
    """The clip's silent tail, as the deck streams it: its first frame ends the participant's line."""
    for _ in range(TAIL_FRAMES):
        await frame(h, SILENCE, tail=True, **kwargs)


def appended_ms(h: Harness) -> int:
    return sum(len(base64.b64decode(e["audio"])) for e in h.r("input_audio_buffer.append")) // 48


def order(h: Harness, *kinds: str) -> list[str]:
    """The channel:type of the timeline entries of these types, in order."""
    return [f"{channel}:{item.get('type')}" for channel, item in h.timeline if item.get("type") in kinds]


# --- the presentation ------------------------------------------------------------------------------------------


async def test_the_demo_talk_defaults_to_clips_and_hello_describes_the_script(h: Harness):
    assert (h.c.input_mode, h.c.demo is not None) == ("clips", True)
    await h.c.on_browser_connected()
    hello = h.b("hello")[0]
    assert hello["input"] == "clips" and hello["snapshot"]["input"] == "clips"
    demo = hello["demo"]
    assert demo["input"] == "clips" and demo["voices"] == {"Alice": "chloe", "Bob": "jake", "Carol": "eleanor"}
    assert [step["id"] for step in demo["steps"]] == ["start", "alice-question", "bob-announce", "carol-finalize",
                                                      "carol-correct", "wrap"]
    alice = demo["steps"][1]
    assert (alice["speaker"], alice["say"], alice["audio"]) == ("Alice", ALICE_QUESTION, None)  # no clips served here
    assert alice["when"] == {"talking_point": "wednesday-production", "heard_ms": 500, "timeout_ms": 90_000}
    assert demo["steps"][0]["action"] == "start"


async def test_a_clip_during_the_narration_barges_in_like_space_and_the_point_resumes(h: Harness):
    await present_first_point(h, transcript="Release four point three ships in two stages, and Friday is",
                              audio_ms=1000)
    h.reset()
    await frame(h, reply=1, played=600)
    assert h.c.state == NARRATING  # 100 ms of voice: not yet an utterance
    await frame(h, reply=1, played=700)  # 200 ms: Alice is talking over the facilitator
    assert h.b("barge_in") == [{"type": "barge_in", "speaker": "Alice", "clip": "alice-question", "played_ms": 700,
                                "reply": 1, "state": NARRATING}]
    assert order(h, "barge_in", "reply_flush") == ["browser:barge_in", "browser:reply_flush"]
    assert h.r("conversation.item.truncate")[0]["audio_end_ms"] == 700  # what the page had played of it
    assert h.c.cursor == Cursor("plan", 0, live=True, heard="Release four point three ships in two", heard_ms=700)
    turn = h.c.turn
    assert (h.c.state, turn.source, turn.speaker, turn.clip, turn.origin) == (LISTENING, "clip", "Alice",
                                                                             "alice-question", "narration")
    assert h.b("mic") == []  # no microphone: the deck streams the clip by itself
    # (its response was done already: nothing to cancel)
    assert h.realtime.types()[:3] == ["conversation.item.truncate", "session.update", "input_audio_buffer.clear"]
    assert appended_ms(h) == 200  # the utterance from its first voiced frame (the preroll was silence)
    await speak(h, 2, reply=1, played=700)
    assert appended_ms(h) == 400 and h.c.state == LISTENING
    await stop(h)  # 700 ms of the silent tail: the end of her turn
    assert h.c.state == REPLYING and h.c.turn.ended_by == "vad"
    assert h.realtime.types()[-2:] == ["input_audio_buffer.commit", "response.create"]
    await h.ev("input_audio_buffer.committed", item_id="item_alice")
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="item_alice",
               transcript=ALICE_QUESTION)
    assert h.last("caption") == {"type": "caption", "role": "user", "text": ALICE_QUESTION, "final": True,
                                 "reply": h.c.turn.reply, "speaker": "Alice"}

    # the same round trip as a tapped question: lookup → answer from it → resume at the interrupted point
    await respond(h, call("lookup_evidence", {"query": "Friday beta release"}), transcript="", audio_ms=0)
    assert outputs(h)[-1][1]["results"][0]["source_id"] == "REQ-2"
    await h.drained()
    await respond(h, call("set_presentation_state", {"action": "resume"}), transcript=ANSWER, audio_ms=900)
    await h.drained()
    assert h.c.mstate.clarifications[0].question == ALICE_QUESTION
    assert (h.c.state, h.c.status) == (NARRATING, "resuming")
    assert creates(h)[-1]["metadata"]["talking_point"] == "friday-beta"
    assert 'the room last heard you say: "Release four point three ships in two"' in creates(h)[-1]["instructions"]
    entry = [data for kind, data in h.logs if kind == "barge_in"]
    assert entry == [{"speaker": "Alice", "clip": "alice-question", "played_ms": 700, "reply": 1,
                      "state": NARRATING, "kind": "narration", "deck_reply": 1}]
    metric = [data for kind, data in h.logs if kind == "metric" and data["name"] == "reply_latency_ms"][-1]
    assert metric["from"] == "end of speech (segmenter)"


async def test_a_clip_during_an_answer_cuts_it_and_keeps_the_turns_origin(h: Harness):
    await present_first_point(h)
    await h.typed("Is Friday beta?", reply=1, played=500)
    await respond(h, transcript="Friday is the beta, and production", audio_ms=1200, done=False)
    answer = h.c.turn.reply
    h.reset()
    await speak(h, 2, reply=answer, played=400)
    assert h.b("barge_in")[0]["state"] == REPLYING and h.b("barge_in")[0]["reply"] == answer
    assert h.b("reply_flush") == [{"type": "reply_flush", "reply": answer}]
    assert h.r("conversation.item.truncate")[0]["audio_end_ms"] == 400
    assert (h.c.state, h.c.turn.origin, h.c.turn.speaker) == (LISTENING, "narration", "Alice")


async def test_a_clip_in_the_opening_is_a_turn_without_a_cut(h: Harness):
    await speak(h, 2)
    assert h.b("barge_in") == [] and "conversation.item.truncate" not in h.realtime.types()
    assert (h.c.state, h.c.turn.origin, h.c.turn.source) == (LISTENING, "idle", "clip")
    assert "Alice is about to hand you the floor" in h.instructions()
    await stop(h)
    assert h.realtime.types()[-2:] == ["input_audio_buffer.commit", "response.create"]
    await respond(h, call("set_presentation_state", {"action": "start"}), transcript="Hello all.", audio_ms=400)
    await h.drained()
    assert (h.c.state, creates(h)[-1]["metadata"]["talking_point"]) == (NARRATING, "friday-beta")


async def test_a_clip_while_paused_is_a_turn_that_goes_back_to_the_pause(h: Harness):
    await present_first_point(h)
    await h.key("pause", reply=1, played=300)
    assert h.c.state == PAUSED
    h.reset()
    await speak(h, 2)
    assert h.b("barge_in") == [] and (h.c.state, h.c.turn.origin) == (LISTENING, "paused")
    await stop(h)
    await respond(h, transcript="Sure.", audio_ms=300)
    await h.drained()
    assert h.c.state == PAUSED


async def test_a_clip_cuts_the_backup_narration_at_the_frames_position(mk):
    h = mk(settings=Settings(narration="rendered"))
    await h.key("relay")
    assert (h.c.state, h.c.narration) == (NARRATING, "rendered")
    words = h.words("plan", 0)
    at = h.at(words[6].start + 5)
    h.reset()
    await speak(h, 2, at=at)
    assert h.b("barge_in")[0]["reply"] is None
    cut = h.last("cut")
    assert (cut["slide"], cut["chunk"]) == ("plan", 0) and h.c.state == LISTENING


async def test_a_clip_joins_a_floor_opened_by_space(h: Harness):
    await present_first_point(h)
    await h.key("space", reply=1, played=300)
    turn = h.c.turn
    h.reset()
    await speak(h, 3)
    assert h.c.turn is turn and (turn.speaker, turn.speech_started) == ("Alice", True)
    assert appended_ms(h) == 300 and h.b("barge_in") == []
    await stop(h)
    assert h.c.state == REPLYING and h.realtime.types()[-2:] == ["input_audio_buffer.commit", "response.create"]


async def test_esc_during_a_clip_turn_drops_the_rest_of_the_clip(h: Harness):
    await present_first_point(h)
    await speak(h, 2, reply=1, played=500)
    await h.key("esc")
    assert h.c.state == NARRATING  # back to the point
    h.reset()
    await speak(h, 3)  # the rest of the clip goes nowhere
    await stop(h)
    assert h.r("input_audio_buffer.append") == [] and "input_audio_buffer.commit" not in h.realtime.types()


async def test_offline_the_participants_are_not_heard(mk):
    h = mk()
    await present_first_point(h)
    h.realtime.ready = False
    await speak(h, 2, reply=1, played=500)
    assert h.c.state == NARRATING and h.b("barge_in") == []
    assert h.errors() == [ERR_OFFLINE_CLIP]
    await stop(h)
    assert h.c.state == NARRATING


# --- the discussion --------------------------------------------------------------------------------------------


async def test_clips_in_the_room_are_lines_labelled_by_the_clip_and_never_answered(h: Harness):
    await to_discussion(h)
    h.reset()
    await speak(h, 3, speaker="Bob", clip="bob-announce")  # no echo window: the facilitator is never in this input
    assert appended_ms(h) == 300 and h.c.state == ROOM
    await stop(h, speaker="Bob", clip="bob-announce")
    assert h.realtime.types()[-1] == "input_audio_buffer.commit" and "response.create" not in h.realtime.types()
    await h.send({"type": "speaker", "name": "Carol"})  # the selector changing afterwards doesn't relabel it
    await h.ev("input_audio_buffer.committed", item_id="item_bob")
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="item_bob", transcript=BOB_LINE)
    utterance = h.b("transcript")[0]["utterance"]
    assert (utterance["speaker"], utterance["text"], utterance["source"]) == ("Bob", BOB_LINE, "clip")
    assert [m["event"]["type"] for m in h.b("alignment")].count("conflict") == 2
    assert h.c.state == ROOM


async def test_the_whole_discussion_from_clips(h: Harness):
    await to_discussion(h)
    for speaker, clip, text, item in (("Bob", "bob-announce", BOB_LINE, "i1"),
                                      ("Carol", "carol-finalize", CAROL_CONFIRM, "i2")):
        await speak(h, 3, speaker=speaker, clip=clip)
        await stop(h, speaker=speaker, clip=clip)
        await h.ev("input_audio_buffer.committed", item_id=item)
        await h.ev("conversation.item.input_audio_transcription.completed", item_id=item, transcript=text)
    assert h.c.state == INTERVENING and creates(h)[-1]["metadata"]["relay"] == "intervention"
    await respond(h, transcript="One clarification: Friday is beta. Beta users only?", audio_ms=800)
    await h.drained()
    await speak(h, 3, speaker="Carol", clip="carol-correct")
    await stop(h, speaker="Carol", clip="carol-correct")
    await h.ev("input_audio_buffer.committed", item_id="i3")
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="i3", transcript=CAROL_FIX)
    assert creates(h)[-1]["metadata"]["relay"] == "decision"
    await respond(h, transcript="Confirmed.", audio_ms=400)
    assert h.c.mstate.decisions[0].owner == "Bob"
    assert [u.speaker for u in h.c.mstate.transcript] == ["Bob", "Carol", "Carol"]


async def test_a_clip_during_the_intervention_barges_in_and_becomes_a_line(h: Harness):
    await to_intervention(h)
    await respond(h, transcript="One clarification before we confirm, Friday is", audio_ms=1500, done=False)
    turn = h.c.turn
    h.reset()
    await speak(h, 2, speaker="Carol", clip="carol-correct", reply=turn.reply, played=900)
    assert h.b("barge_in") == [{"type": "barge_in", "speaker": "Carol", "clip": "carol-correct", "played_ms": 900,
                                "reply": turn.reply, "state": INTERVENING}]
    assert h.b("reply_flush") == [{"type": "reply_flush", "reply": turn.reply}]
    assert h.r("conversation.item.truncate")[0]["audio_end_ms"] == 900
    assert h.c.state == ROOM and h.c.turn is None
    assert appended_ms(h) == 200
    await stop(h, speaker="Carol", clip="carol-correct")
    await h.ev("response.done", response={"id": turn.response_id, "status": "cancelled", "output": []})
    assert h.realtime.types()[-1] == "input_audio_buffer.commit"  # held until the cut response was done
    await h.ev("input_audio_buffer.committed", item_id="item_carol")
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="item_carol", transcript=CAROL_FIX)
    assert h.b("transcript")[-1]["utterance"]["speaker"] == "Carol"
    assert creates(h)[-1]["metadata"]["relay"] == "decision"  # her line resolved it


async def test_a_clip_cutting_the_decision_still_saves_the_draft(h: Harness):
    await to_intervention(h)
    await respond(h, transcript="One clarification.", audio_ms=500)
    await h.drained()
    await room_line(h, "Carol", CAROL_FIX)
    assert h.c.state == INTERVENING and h.c.turn.kind == "decision"
    await speak(h, 2, speaker="Bob", clip="bob-announce")
    assert h.c.state == ROOM and h.c.mstate.decisions[0].by == "auto"


async def test_wrap_up_right_after_the_resolving_line_waits_for_its_transcript(h: Harness):
    await to_intervention(h)
    await respond(h, transcript="One clarification: Friday is beta. Beta users only?", audio_ms=800)
    await h.drained()
    await speak(h, 3, speaker="Carol", clip="carol-correct")
    await stop(h, speaker="Carol", clip="carol-correct")
    assert h.realtime.types()[-1] == "input_audio_buffer.commit"
    await h.key("phase")  # Wrap up while her line is still being transcribed
    assert (h.c.agenda_phase, h.c.state, h.c.status) == ("wrapup", IDLE, "wrapping_up")
    assert creates(h)[-1]["metadata"]["relay"] == "intervention"  # no summary yet
    await h.ev("input_audio_buffer.committed", item_id="item_carol")
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="item_carol", transcript=CAROL_FIX)
    [decision] = h.c.mstate.decisions
    assert (decision.owner, decision.by) == ("Bob", "auto")  # what her line settled is in the notes…
    assert h.c.mstate.open_questions == []  # …not an open question
    summary = creates(h)[-1]
    assert summary["metadata"]["relay"] == "summary" and h.c.state == REPLYING
    assert f"- {decision.id} (owner Bob" in summary["instructions"]  # the summary is told the decision
    assert h.b("meeting")[-1]["notes"]["decisions"][0]["owner"] == "Bob"


async def test_the_wrap_up_waits_for_a_last_line_two_seconds_at_most(h: Harness):
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await speak(h, 3, speaker="Carol", clip="carol-finalize")
    await h.key("phase")  # her line, still being spoken, is committed; its transcript never comes
    assert h.c.status == "wrapping_up" and creates(h)[-1]["metadata"]["relay"] == "handover"
    await h.clock.advance(WRAPUP_WAIT_S - 0.1)
    assert creates(h)[-1]["metadata"]["relay"] == "handover"
    await h.clock.advance(0.1)
    assert creates(h)[-1]["metadata"]["relay"] == "summary" and h.c.status == "wrapping_up"
    assert len(h.c.mstate.open_questions) == 2  # Bob's two conflicts, nobody settled them


async def test_a_decision_still_waiting_for_the_facilitator_at_the_wrap_up_is_saved_as_drafted(h: Harness):
    await to_intervention(h)
    await respond(h, transcript="One clarification.", audio_ms=500)
    await h.drained()
    await speak(h, 3, speaker="Bob", clip="bob-announce")  # Bob is talking…
    await room_line(h, "Carol", CAROL_FIX)  # …when the typed correction settles it: the decision waits for his line
    assert h.c.state == ROOM and h.c._flows[0][0] == "decision" and h.c.mstate.decisions == []
    await h.key("phase")
    assert [(d.owner, d.by) for d in h.c.mstate.decisions] == [("Bob", "auto")]


async def test_the_facilitator_never_speaks_up_over_a_participant(h: Harness):
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await speak(h, 3, speaker="Carol", clip="carol-finalize")  # Carol is talking…
    await room_line(h, "Alice", CAROL_CONFIRM)  # …when a typed confirmation opens the gate
    assert h.b("alignment")[-1]["event"]["type"] == "intervene"
    assert h.c.state == ROOM  # it waits for her line
    await stop(h, speaker="Carol", clip="carol-finalize")
    assert h.c.state == ROOM
    await h.ev("input_audio_buffer.committed", item_id="item_c")
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="item_c", transcript="Hm.")
    assert h.c.state == INTERVENING  # after it


async def test_the_facilitator_never_speaks_up_over_a_participant_who_has_just_started(h: Harness):
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await frame(h, TONE, speaker="Alice", clip="x")  # Alice's first 100 ms: an onset, not an utterance yet
    await room_line(h, "Carol", CAROL_CONFIRM)  # the gate opens now
    assert h.c.state == ROOM and h.c._flows[0][0] == "intervention"  # held: Alice is talking
    await speak(h, 2, speaker="Alice", clip="x")
    assert h.b("barge_in") == [] and h.b("reply_flush") == []  # nothing started, so nothing to cut
    await stop(h, speaker="Alice", clip="x")
    assert h.c.state == ROOM
    await h.ev("input_audio_buffer.committed", item_id="item_alice")
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="item_alice",
               transcript="Let me check my calendar.")
    assert h.c.state == INTERVENING and h.c.turn.kind == "intervention"  # after her line, not over it


async def test_an_onset_that_comes_to_nothing_holds_the_intervention_only_briefly(h: Harness):
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await frame(h, TONE, speaker="Alice", clip="x")  # a click, or a cough
    await room_line(h, "Carol", CAROL_CONFIRM)
    assert h.c.state == ROOM
    await frame(h, SILENCE, speaker="Alice", clip="x")
    assert h.c.state == ROOM
    await frame(h, SILENCE, speaker="Alice", clip="x")  # 200 ms of silence: not an utterance after all
    assert h.c.state == INTERVENING and h.c.turn.kind == "intervention"


async def test_an_intervention_cut_before_a_word_of_it_was_heard_is_asked_after_the_line(h: Harness):
    await to_intervention(h)  # the question is asked for; its first audio hasn't come yet
    asked = h.c.turn
    h.reset()
    await speak(h, 2, speaker="Alice", clip="x")  # Alice starts talking (Play next) before it says a word
    assert h.b("barge_in") == []  # nothing was heard: nobody was interrupted
    assert h.b("reply_flush") == [{"type": "reply_flush", "reply": asked.reply}]
    assert h.c.state == ROOM and h.c._flows[0][0] == "intervention"
    await stop(h, speaker="Alice", clip="x")
    await h.ev("input_audio_buffer.committed", item_id="item_alice")
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="item_alice",
               transcript="One second, my screen froze.")
    assert h.c.state == INTERVENING and h.c.turn.kind == "intervention" and h.c.turn is not asked
    assert [c["metadata"]["relay"] for c in creates(h)].count("intervention") == 2


async def test_a_pause_inside_a_recorded_clip_never_splits_the_participants_turn(h: Harness):
    """A recording (talks/<id>/clips/) may pause longer than the room mic's 0.7 s between its sentences: the line ends
    where the clip ends, so its second sentence never barges in on the answer to the first."""
    await present_first_point(h, audio_ms=3000, done=True)
    await speak(h, 6, reply=1, played=900)  # "Wait, I thought Friday was production."
    assert h.c.state == LISTENING and len(h.b("barge_in")) == 1
    for _ in range(9):  # a 0.9 s pause inside the recording
        await frame(h, SILENCE)
    assert h.c.state == LISTENING
    await speak(h, 3)  # "Which one is it?"
    assert h.c.state == LISTENING and len(h.b("barge_in")) == 1
    await stop(h)
    assert (h.c.state, h.c.turn.ended_by) == (REPLYING, "vad")
    assert h.realtime.types().count("input_audio_buffer.commit") == 1


async def test_two_clips_back_to_back_are_two_lines(h: Harness):
    await to_discussion(h)
    h.reset()
    await speak(h, 3, speaker="Bob", clip="bob-announce")
    await speak(h, 3, speaker="Carol", clip="carol-finalize")  # Carol's clip cuts Bob's off, no pause between
    await stop(h, speaker="Carol", clip="carol-finalize")
    assert h.realtime.types().count("input_audio_buffer.commit") == 2
    for item, text in (("i1", BOB_LINE), ("i2", CAROL_CONFIRM)):
        await h.ev("input_audio_buffer.committed", item_id=item)
        await h.ev("conversation.item.input_audio_transcription.completed", item_id=item, transcript=text)
    assert [(u.speaker, u.text) for u in h.c.mstate.transcript] == [("Bob", BOB_LINE), ("Carol", CAROL_CONFIRM)]


async def test_clip_frames_without_the_tail_flag_end_on_the_segmenters_silence(h: Harness):
    await to_discussion(h)
    h.reset()
    await speak(h, 3, speaker="Bob", clip="bob-announce", tail=None)
    for _ in range(FALLBACK_END_FRAMES - 1):
        await frame(h, SILENCE, speaker="Bob", clip="bob-announce", tail=None)
    assert "input_audio_buffer.commit" not in h.realtime.types()  # 1 s of silence: still his line
    await frame(h, SILENCE, speaker="Bob", clip="bob-announce", tail=None)
    assert h.realtime.types()[-1] == "input_audio_buffer.commit"


async def test_reset_forgets_a_clip_being_heard(h: Harness):
    await to_discussion(h)
    await speak(h, 3, speaker="Bob", clip="bob-announce")
    assert h.c._clip_seg.in_speech
    h.reset()
    await h.key("reset")
    assert not h.c._clip_seg.in_speech and h.c._clip_target is None
    assert h.realtime.restarts == 1  # (the old session, and whatever was committed to it, is replaced)
    h.reset()
    await stop(h, speaker="Bob", clip="bob-announce")  # the rest of that clip: nothing
    assert h.realtime.types() == [] and h.c.state == IDLE


async def test_a_new_session_forgets_the_clip_being_heard(h: Harness):
    await to_discussion(h)
    await speak(h, 3, speaker="Bob", clip="bob-announce")
    await h.c.on_realtime_status("reconnecting", "connection lost")
    await h.c.on_realtime_status("ready")
    assert h.c._clip_target is None and not h.c._clip_seg.in_speech


# --- --input mic: today's behaviour ------------------------------------------------------------------------------


async def test_input_mic_ignores_clip_frames_and_keeps_the_tap(mk):
    h = mk(settings=Settings(narration="live", input="mic"))
    assert (h.c.input_mode, h.c._clip_seg) == ("mic", None)
    await present_first_point(h)
    h.reset()
    await speak(h, 5, reply=1, played=500)
    await stop(h)
    assert h.c.state == NARRATING and h.realtime.items == [] and h.b("barge_in") == []
    assert [kind for kind, _ in h.logs].count("clip_frame_ignored") == 1
    await h.key("space", reply=1, played=600)  # the tap floor control, as before
    assert h.c.state == LISTENING and h.b("mic")[-1] == {"type": "mic", "open": True}
    for _ in range(3):
        await h.send({"type": "mic", "pcm": TONE})
    await h.mic(8)
    assert h.c.state == REPLYING


async def test_a_plain_meeting_without_a_demo_is_mic(tmp_path: Path):
    from tests.test_meeting_flow import write_meeting

    talk = load_talk(write_meeting(tmp_path / "plain"))
    h = Harness(talk, synthetic_chunks(talk), Settings(narration="live"))
    assert (h.c.input_mode, h.c.demo, h.c.demo_payload) == ("mic", None, None)
    await h.c.on_browser_connected()
    assert h.b("hello")[0]["demo"] is None and h.b("hello")[0]["input"] == "mic"


async def test_clip_frames_without_audio_or_from_strangers_are_harmless(h: Harness):
    await present_first_point(h)
    await h.send({"type": "mic", "source": "clip", "pcm": ""})
    await h.send({"type": "mic", "source": "clip", "pcm": 12})
    await speak(h, 2, speaker="Mallory", clip="x" * 80, reply=1, played=200)  # not a participant: unlabelled
    assert h.b("barge_in")[-1]["speaker"] is None and h.b("barge_in")[-1]["clip"] is None
    assert h.c.state == LISTENING


def test_the_session_is_the_same_for_both_inputs(mk):
    """The facilitator's session doesn't depend on where the participants' audio comes from: manual turns."""
    clips, mic = mk(), mk(settings=Settings(narration="live", input="mic"))
    assert clips.c.session_config() == mic.c.session_config()
    assert clips.c.session_config()["audio"]["input"]["turn_detection"] is None
