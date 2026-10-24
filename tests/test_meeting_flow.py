"""The meeting facilitator (On the Same Page, contract v3 §1, §4–§6): the controller driven through tests/
test_controller.py's recording ports and manual clock, on a small meeting package written into tmp_path (so these
tests don't depend on talks/rollout-review).

**Legacy alignment tests** (contract v5 §5): the discussion scenarios here were written for the regex engine
(relay/alignment.py over checks.yaml) and pin the controller to `alignment="legacy"` explicitly; the default,
model-based alignment (relay/semantic.py) is covered by tests/test_semantic_flow.py. The presentation, the tools,
the room mic and the wrap-up mechanics they exercise are the same in both modes.

The brief's milestones as scenarios: an interruption → lookup_evidence round trip → spoken answer → automatic
clarification → resume at the same talking point (Milestone 1); the presentation's end → hand-over → the room; the
brief §3 discussion lines → an intervention with the engine's evidence → the resolution → save_decision validated
(an invented one rejected → the draft saved by=auto) → notes; the wrap-up; a reset (Milestone 2). Plus manual turns
with the segmenter, half duplex, typed utterances, follow-up limits and Space in the room.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import math
import struct
from pathlib import Path
from typing import Any

import pytest

from relay import prompts, tools
from relay.config import VAD_TURN_DETECTION, Settings
from relay.controller import (
    BANNER_DISCUSSION, BANNER_WRAPPED, IDLE, INTERVENING, LIMIT_OUTPUT, LISTENING, MAX_FOLLOWUPS, NARRATING,
    NOT_NOW_OUTPUT, REPLYING, ROOM, ROOM_ECHO_S, Cursor,
)
from relay.talk import Talk, load_talk
from tests.conftest import synthetic_chunks
from tests.test_controller import AUDIO_100MS, Harness, call

# --- the meeting package --------------------------------------------------------------------------------------

SLIDES_MD = """<!-- .slide: id="agenda" -->
# Rollout review

Agenda: the plan, the discussion, decisions

---

<!-- .slide: id="plan" -->
## The plan

Friday: beta to the beta group <!-- .element: class="fragment" -->

Wednesday: production for all customers <!-- .element: class="fragment" -->

---

<!-- .slide: id="comms" -->
## Communications

Release notes to the beta group on Friday

---

<!-- .slide: id="discussion" -->
## Discussion

What goes out on Friday?
"""

SCRIPT = """talk: {talk}
voice: default
presenter: Alice
slides:
  - id: agenda
    speaker: human
  - id: plan
    speaker: relay
    plain: "Like a soft opening: regulars first, everyone next week."
    chunks:
      - id: friday-beta
        say: "Release four point three ships in two stages. Friday's rollout is for our beta group."
        fragment: 1
      - id: wednesday-production
        say: "Production follows on Wednesday for all customers, after the beta review."
        fragment: 2
  - id: comms
    speaker: relay
    plain: "Like invitations in two waves."
    chunks:
      - id: beta-notes
        say: "On Friday, release notes go to the beta group only."
  - id: discussion
    speaker: human
"""

AGENDA = """title: Release 4.3 rollout review
host: Alice
participants:
  - {name: Alice, role: Product manager}
  - {name: Bob, role: Customer communications}
  - {name: Carol, role: Engineering lead}
phases:
  - {id: presentation, title: Rollout plan}
  - {id: discussion, title: Launch announcement, prompt: "What goes out on Friday, and who sends it?"}
  - {id: wrapup, title: Decisions and next steps}
discussion_slide: discussion
"""

CHECKS = r"""slots:
  - id: friday_stage
    label: "Friday's release stage"
    context: ['\bfriday']
    values:
      beta: ['\bbeta\b']
      production: ['\bproduction\b']
  - id: friday_audience
    label: "Friday's announcement audience"
    context: ['\bfriday']
    values:
      beta_users: ['\bbeta (users?|group)\b']
      all_customers: ['\ball customers\b']
actions: ['\bi''ll\b', '\blet''s\b', '\bdraft', '\bsend', '\bannounc', '\blaunch']
confirmations: ['\bsounds good\b', '\bfinali[sz]e\b', '\bagreed\b']
ownership: ['\bowns?\b', '\bwill (draft|send|own)']
window_utterances: 3
"""

REQUIREMENTS = """# Rollout plan v3

Release 4.3, staged.

- [REQ-1] Release 4.3 rolls out in two stages, beta first and then production.
- [REQ-2] On Friday 24 October the beta release goes to the beta group only, about 40 customers.
- [REQ-3] The production release goes to all customers on Wednesday 29 October, after the beta review.
"""

DECISIONS = """# Decisions log

- [DEC-1] Announcements follow the stage: the Friday beta is announced to beta users only.
- [DEC-2] Bob owns customer communications for release 4.3.
"""

ALICE_QUESTION = "Wait, I thought Friday was production. Which one is it?"
BOB_LINE = "I'll draft the announcement for Friday's production launch to all customers."
CAROL_CONFIRM = "Sounds good. Let's finalize that."
CAROL_FIX = "Yes, beta users only. Bob owns the announcement."
ANSWER = "Friday is the beta release for the beta group, not production; production is next Wednesday."


def write_meeting(root: Path) -> Path:
    """A small meeting package (the demo's story in miniature) at `root` (its folder name is the talk id)."""
    (root / "sources").mkdir(parents=True, exist_ok=True)
    (root / "slides.md").write_text(SLIDES_MD, encoding="utf-8")
    (root / "script.yaml").write_text(SCRIPT.format(talk=root.name), encoding="utf-8")
    (root / "agenda.yaml").write_text(AGENDA, encoding="utf-8")
    (root / "checks.yaml").write_text(CHECKS, encoding="utf-8")
    (root / "sources" / "requirements.md").write_text(REQUIREMENTS, encoding="utf-8")
    (root / "sources" / "decisions.md").write_text(DECISIONS, encoding="utf-8")
    return root


def tone(ms: int = 100, amplitude: int = 8000) -> str:
    """`ms` of a 440 Hz tone (about −15 dBFS): speech to the segmenter."""
    n = 24 * ms
    samples = [int(amplitude * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(n)]
    return base64.b64encode(struct.pack(f"<{n}h", *samples)).decode()


TONE = tone()


@pytest.fixture
def meeting_talk(tmp_path: Path) -> Talk:
    return load_talk(write_meeting(tmp_path / "meeting"))


LEGACY = Settings(narration="live", alignment="legacy")  # these scenarios drive the regex engine (see the docstring)


@pytest.fixture
def mk(meeting_talk: Talk):
    made: list[Harness] = []

    def factory(*, settings: Settings | None = None, ready: bool = True) -> Harness:
        chosen = dataclasses.replace(settings, alignment="legacy") if settings is not None else LEGACY
        harness = Harness(meeting_talk, synthetic_chunks(meeting_talk), chosen, ready=ready)
        made.append(harness)
        return harness

    yield factory
    for harness in made:
        harness.check_invariants()


@pytest.fixture
def h(mk) -> Harness:
    return mk()


# --- helpers ----------------------------------------------------------------------------------------------------


def creates(h: Harness) -> list[dict]:
    """The `response` objects of every response.create sent (history: `reset` keeps them)."""
    return [event.get("response") or {} for event in h.realtime.history if event.get("type") == "response.create"]


async def respond(h: Harness, *calls: dict, **kwargs: Any) -> str:
    """The server answers the latest response.create, echoing its metadata on response.created like Higgs."""
    return await h.reply(*calls, metadata=creates(h)[-1].get("metadata"), **kwargs)


def outputs(h: Harness) -> list[tuple[str, dict]]:
    """(call_id, parsed output) of every function_call_output sent, in order."""
    return [(event["item"]["call_id"], json.loads(event["item"]["output"])) for event in h.realtime.history
            if event.get("type") == "conversation.item.create" and event["item"]["type"] == "function_call_output"]


def browser_history(h: Harness, kind: str) -> list[dict]:
    return [item for item in h.browser.history if item.get("type") == kind]


async def present_first_point(h: Harness, *, transcript: str = "Release four point three ships in two stages.",
                              audio_ms: int = 1000, done: bool = True) -> str:
    """R on the agenda slide: the first talking point (plan / friday-beta) is presented live."""
    await h.key("relay")
    assert (h.c.state, h.c.cursor) == (NARRATING, Cursor("plan", 0))
    return await h.narration(transcript=transcript, audio_ms=audio_ms, done=done)


async def to_discussion(h: Harness) -> None:
    """The agenda button from the start: the hand-over line, then the room."""
    await h.key("phase")
    assert creates(h)[-1]["metadata"]["relay"] == "handover"
    await respond(h, transcript="That's the plan; over to you.", audio_ms=500)
    await h.drained()
    assert (h.c.state, h.c.agenda_phase) == (ROOM, "discussion")


async def room_line(h: Harness, speaker: str | None, text: str) -> None:
    await h.send({"type": "speaker", "name": speaker})
    await h.typed(text)


async def to_intervention(h: Harness) -> None:
    """The brief §3 lines up to the facilitator's intervention (typed lines: no Realtime needed for them)."""
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    assert h.c.state == ROOM  # a conflict, but nobody is confirming yet: silent
    await room_line(h, "Carol", CAROL_CONFIRM)
    assert h.c.state == INTERVENING


# --- the meeting session ---------------------------------------------------------------------------------------


def test_a_meeting_session_has_the_facilitators_tools_and_manual_turns(mk, meeting_talk: Talk):
    h = mk(settings=Settings(narration="live", turn_detection=dict(VAD_TURN_DETECTION)))
    config = h.c.session_config()
    assert config["audio"]["input"]["turn_detection"] is None  # a meeting needs manual turns, whatever the setting
    assert [spec["name"] for spec in config["tools"]] == list(tools.MODEL_TOOL_NAMES)
    assert config["tools"] == tools.tool_specs(meeting_talk, h.c.meeting)
    instructions = config["instructions"]
    assert instructions.startswith(prompts.facilitator_prompt(h.c.meeting) + "\n\nPhase: opening\n")
    assert "Agenda phase: presentation (Rollout plan); next: discussion (Launch announcement)" in instructions
    assert "Alice is about to hand you the floor" in instructions


async def test_hello_carries_the_meeting_and_the_state_its_checkpoint(h: Harness):
    await h.c.on_browser_connected()
    assert h.browser.types() == ["hello", "stop", "mic", "slide", "state", "connection", "meeting"]
    hello = h.b("hello")[0]
    assert hello["meeting"] == h.c.meeting.to_json()
    assert [document["id"] for document in hello["meeting"]["documents"]] == ["decisions", "requirements"]
    assert hello["meeting"]["documents"][1]["statements"][1] == {
        "id": "REQ-2", "text": "On Friday 24 October the beta release goes to the beta group only, about 40 customers."}
    assert (hello["snapshot"]["agenda_phase"], hello["snapshot"]["status"]) == ("presentation", "idle")
    state = h.last("state")
    assert (state["agenda_phase"], state["status"], state["talking_point"]) == ("presentation", "idle", None)
    assert state["checkpoint"] == {"slide_id": "agenda", "talking_point_id": None, "status": "idle"}
    meeting = h.last("meeting")
    assert meeting["phase"] == "presentation" and meeting["decisions"] == [] and meeting["notes"] is None
    assert meeting["checkpoint"] == state["checkpoint"]


async def test_the_presentation_presents_one_talking_point_per_response_then_hands_over(h: Harness):
    await h.key("relay")
    assert h.b("slide") == [{"type": "slide", "slide": "plan", "fragment": 1}]  # progressive reveal per point
    request = creates(h)[-1]
    plan = h.talk.slide("plan")
    assert request == {"instructions": prompts.talking_point_instructions(
        h.talk, h.c.meeting, plan, plan.chunks[0], seconds=request_seconds(h, "plan", 0), first=True),
        "metadata": {"relay": "narration", "reply": "1", "slide": "plan", "talking_point": "friday-beta"},
        "tool_choice": "none"}
    assert "Talking point 1 of 2 on this slide: Release four point three ships" in request["instructions"]
    assert (h.last("state")["status"], h.last("state")["talking_point"]) == ("presenting", "friday-beta")

    await h.narration(audio_ms=800)
    h.reset()
    await h.drained(reply=1)
    assert h.b("slide") == [{"type": "slide", "slide": "plan", "fragment": 2}]
    request = creates(h)[-1]
    assert request["metadata"]["talking_point"] == "wednesday-production"
    assert 'You just said (the previous point): "Release four point three ships' in request["instructions"]
    assert "If you haven't welcomed the room" not in request["instructions"]  # it presented already

    await h.narration(audio_ms=800)
    await h.drained(reply=2)
    assert (h.c.current_slide.id, creates(h)[-1]["metadata"]["talking_point"]) == ("comms", "beta-notes")
    await h.narration(audio_ms=800)
    h.reset()
    await h.drained(reply=3)  # the last relay slide is over: the hand-over
    handover = creates(h)[-1]
    assert handover == {"instructions": prompts.handover_instructions(h.c.meeting),
                        "metadata": {"relay": "handover", "reply": "4"}, "tool_choice": "none"}
    assert (h.c.state, h.c.status, h.c.agenda_phase) == (REPLYING, "presenting", "presentation")
    await respond(h, transcript="That's the plan. Over to you; I'll listen.", audio_ms=600)
    assert h.b("reply_start")[-1] == {"type": "reply_start", "reply": 4, "kind": "handover"}
    h.reset()
    await h.drained()
    assert (h.c.state, h.c.agenda_phase, h.c.current_slide.id) == (ROOM, "discussion", "discussion")
    assert h.b("slide") == [{"type": "slide", "slide": "discussion", "fragment": None}]
    assert h.b("mic") == [{"type": "mic", "open": True}]  # the deck streams the room mic from now on
    assert h.c.banner == BANNER_DISCUSSION.format(prompt="What goes out on Friday, and who sends it?")
    assert h.realtime.types() == ["input_audio_buffer.clear"]
    state = h.last("state")
    assert (state["state"], state["agenda_phase"], state["status"]) == (ROOM, "discussion", "listening")
    assert state["checkpoint"] == {"slide_id": "discussion", "talking_point_id": None, "status": "listening"}
    assert h.last("meeting")["phase"] == "discussion"


def request_seconds(h: Harness, slide: str, chunk: int) -> int:
    return h.c._point_seconds(h.talk.slide(slide), chunk)


# --- Milestone 1: interruption → evidence → answer → clarification → resume ----------------------------------


async def test_milestone_1_a_question_is_answered_from_the_documents_and_the_point_resumes(h: Harness):
    await present_first_point(h, transcript="Release four point three ships in two stages, and Friday is",
                              audio_ms=1000)
    h.reset()
    await h.typed(ALICE_QUESTION, reply=1, played=600)  # Alice cuts in
    assert h.r("conversation.item.truncate")[0]["audio_end_ms"] == 600
    assert h.c.cursor == Cursor("plan", 0, live=True, heard="Release four point three ships in", heard_ms=600)
    assert (h.c.state, h.c.status) == (REPLYING, "answering")
    assert h.realtime.types()[-1] == "response.create" and creates(h)[-1] == {}  # the question: a bare request
    instructions = h.instructions()
    assert "Talking point: friday-beta on plan:" in instructions and "You were cut off at «in»" in instructions

    # the first response is a silent lookup (live 2026-10-23: 6/6), answered at once and followed up
    h.reset()
    await respond(h, call("lookup_evidence", {"query": "Friday beta release"}), transcript="", audio_ms=0)
    (call_id, output), = outputs(h)
    assert output["results"][0]["source_id"] == "REQ-2" and "beta group only" in output["results"][0]["excerpt"]
    evidence = h.b("evidence")[0]["lookup"]
    assert (evidence["id"], evidence["query"], evidence["by"], evidence["purpose"]) == (
        "E1", "Friday beta release", "model", "answer")
    assert evidence["results"] == output["results"]
    assert h.tool_statuses() == [(call_id, "lookup_evidence", "applied")]
    followup = creates(h)[-1]
    assert followup == {"instructions": prompts.followup_instructions(
        prompts.FOLLOWUP_LOOKUP, meeting=h.c.meeting, agenda_phase="presentation", question=ALICE_QUESTION,
        results=output["results"]), "metadata": {"relay": "followup", "reply": "3"}, "tool_choice": "none"}
    # self-contained (live 2026-10-24: "answer from these results" alone made the model call get_presentation_state)
    assert f'just asked: "{ALICE_QUESTION}"' in followup["instructions"]
    assert "[REQ-2]" in followup["instructions"] and "set_presentation_state" not in followup["instructions"]
    # the output, the tools withdrawn (a per-response tool_choice was ignored live), then the ask
    assert h.realtime.types()[-3:] == ["conversation.item.create", "session.update", "response.create"]
    assert h.r("session.update")[-1]["session"] == {"type": "realtime", "tools": []}
    await h.drained(reply=2)  # the silent first reply drains on the deck: the turn goes on
    assert (h.c.state, h.c.turn.reply, h.c.thinking) == (REPLYING, 3, True)

    await respond(h, call("set_presentation_state", {"action": "resume"}), transcript=ANSWER, audio_ms=1200)
    assert h.b("reply_start")[-1] == {"type": "reply_start", "reply": 3, "kind": "answer"}
    assert h.tool_statuses()[-1][1:] == ("set_presentation_state", "queued")
    h.reset()
    await h.drained(reply=3)
    # the model didn't record a clarification: the controller does (by=auto), from the question and the answer
    clarification = h.c.mstate.clarifications[0]
    assert (clarification.id, clarification.question, clarification.answer, clarification.by) == (
        "CL-1", ALICE_QUESTION, ANSWER, "auto")
    assert clarification.source_ids == [result["source_id"] for result in output["results"][:2]]
    assert h.tool_statuses()[0][1:] == ("record_clarification", "auto")
    assert h.last("meeting")["clarifications"][0]["id"] == "CL-1"
    assert h.tool_statuses()[-1][1:] == ("set_presentation_state", "applied")
    # … and the presentation resumes the interrupted talking point, from what the room heard
    assert (h.c.state, h.c.status) == (NARRATING, "resuming")
    resumed = creates(h)[-1]
    assert resumed["metadata"] == {"relay": "narration", "reply": "4", "slide": "plan", "talking_point": "friday-beta"}
    assert 'the room last heard you say: "Release four point three ships in"' in resumed["instructions"]
    assert h.last("state")["checkpoint"] == {"slide_id": "plan", "talking_point_id": "friday-beta",
                                             "status": "resuming"}
    # the clarification is in the model's context from now on, and the next question has the tools again
    await h.narration(audio_ms=300)
    await h.key("space", reply=4, played=100)
    assert "Clarified (CL-1): Friday is the beta release" in h.instructions()
    restored = h.r("session.update")[-1]["session"]
    assert [spec["name"] for spec in restored["tools"]] == list(tools.MODEL_TOOL_NAMES) and "instructions" in restored


async def test_a_follow_up_that_says_nothing_is_asked_again_and_its_resume_dropped(h: Harness):
    """Live 2026-10-24: the follow-up after a lookup called set_presentation_state resume and said nothing, so the
    room went back to the slide without hearing the answer. It is asked again (the limit allows it), and the resume
    it queued is dropped: the countdown resumes once the answer has been heard."""
    await present_first_point(h)
    await h.typed(ALICE_QUESTION, reply=1, played=500)
    await respond(h, call("lookup_evidence", {"query": "Friday beta release"}), transcript="", audio_ms=0)
    await h.drained(reply=2)
    assert creates(h)[-1]["metadata"]["relay"] == "followup" and h.c.turn.followups == 1
    await respond(h, call("set_presentation_state", {"action": "resume"}), transcript="", audio_ms=0)
    assert h.c.turn.followups == 2 and creates(h)[-1]["metadata"]["relay"] == "followup"  # asked again
    assert h.c.turn.queued is None and h.tool_statuses()[-1][1:] == ("set_presentation_state", "discarded")
    assert 'just asked: "' in creates(h)[-1]["instructions"]
    await h.drained(reply=3)
    assert h.c.state == REPLYING  # still waiting for the answer
    await respond(h, transcript=ANSWER, audio_ms=1200)
    await h.drained(reply=4)
    assert h.c.mstate.clarifications[0].answer == ANSWER  # the answer that was heard
    assert (h.c.state, h.c.status) == (IDLE, "resuming")  # the countdown, not the dropped resume
    await h.clock.advance(2)
    assert h.last("tool")["name"] == "set_presentation_state" and h.last("tool")["status"] == "auto"
    assert (h.c.state, creates(h)[-1]["metadata"]["talking_point"]) == (NARRATING, "friday-beta")


async def test_without_a_tool_the_watchdog_resumes_the_talking_point_and_a_recorded_clarification_stays(h: Harness):
    await present_first_point(h)
    await h.typed(ALICE_QUESTION, reply=1, played=500)
    await respond(h, call("lookup_evidence", {"query": "Friday"}), transcript="", audio_ms=0)
    await respond(h, call("record_clarification", {"question": "Friday?", "answer": "Friday is beta only.",
                                                   "source_ids": ["REQ-2", "NOPE-9"]}),
                  transcript="Friday is beta only.", audio_ms=600)
    *_, (call_id, output) = outputs(h)
    assert output == {"ok": True, "id": "CL-1", "dropped_source_ids": ["NOPE-9"]}
    clarification = h.c.mstate.clarifications[0]
    assert (clarification.by, clarification.source_ids) == ("model", ["REQ-2"])
    assert len(creates(h)) == 3  # a state call with audio: no follow-up
    await h.drained()
    assert len(h.c.mstate.clarifications) == 1  # no automatic one on top
    assert (h.c.state, h.c.status) == (IDLE, "resuming")  # the countdown (no resume call)
    await h.clock.advance(2)
    assert (h.c.state, creates(h)[-1]["metadata"]["talking_point"]) == (NARRATING, "friday-beta")
    assert h.last("tool")["name"] == "set_presentation_state" and h.last("tool")["args"] == {"action": "resume"}
    assert h.last("tool")["status"] == "auto"


async def test_a_spoken_question_ends_with_the_segmenter_and_its_latency_counts_from_the_end_of_speech(h: Harness):
    await present_first_point(h)
    await h.key("space", reply=1, played=400)
    assert (h.c.state, h.c.turn.segmenter is not None) == (LISTENING, True)
    assert "listen_silence" in h.c._timers
    h.reset()
    for _ in range(3):  # 300 ms of speech: it started
        await h.send({"type": "mic", "pcm": TONE})
    spoken_until = h.clock.now()  # when the last speech frame arrived
    assert h.c.turn.speech_started and "listen_silence" not in h.c._timers
    for _ in range(6):
        await h.mic()
    assert h.c.state == LISTENING  # 600 ms of silence: not yet
    await h.clock.advance(0.5)
    await h.mic()  # 700 ms: the end of the turn — commit + response.create, as "send now" does
    assert h.c.state == REPLYING and h.c.turn.ended_by == "vad"
    assert h.realtime.types()[-2:] == ["input_audio_buffer.commit", "response.create"]
    assert h.b("mic")[-1] == {"type": "mic", "open": False}
    # the end of speech: 700 ms of stream before the frame that ended it = the end of the last speech frame
    assert h.c.turn.speech_end_at == pytest.approx(spoken_until) and h.c.turn.speech_end_estimated
    await h.ev("input_audio_buffer.committed", item_id="item_q")
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="item_q", transcript=ALICE_QUESTION)
    assert h.c.turn.user_text == ALICE_QUESTION
    await h.clock.advance(0.3)
    await respond(h, transcript="Let me check.", audio_ms=300)
    metric = [data for kind, data in h.logs if kind == "metric" and data["name"] == "reply_latency_ms"][-1]
    assert metric["from"] == "end of speech (segmenter)"


async def test_server_vad_still_works_for_a_plain_talk_and_manual_turns_are_the_default(make_plain):
    manual = make_plain(Settings(narration="rendered"))
    await manual.key("relay")
    await manual.key("space", at=manual.at(500))
    assert manual.c.turn.segmenter is not None
    vad = make_plain(Settings(narration="rendered", turn_detection=dict(VAD_TURN_DETECTION)))
    await vad.key("relay")
    await vad.key("space", at=vad.at(500))
    assert vad.c.turn.segmenter is None  # server VAD ends the turn (speech_stopped)
    await vad.speak()
    assert vad.c.state == REPLYING and "input_audio_buffer.commit" not in vad.realtime.types()


@pytest.fixture
def make_plain(test_talk, test_chunks):
    made: list[Harness] = []

    def factory(settings: Settings) -> Harness:
        harness = Harness(test_talk, test_chunks, settings)
        made.append(harness)
        return harness

    yield factory
    for harness in made:
        harness.check_invariants()


async def test_follow_ups_are_limited_per_turn(h: Harness):
    await present_first_point(h)
    await h.typed(ALICE_QUESTION, reply=1, played=500)
    for n in range(MAX_FOLLOWUPS):
        await respond(h, call("lookup_evidence", {"query": f"Friday {n}"}), transcript="", audio_ms=0)
        assert creates(h)[-1]["metadata"]["relay"] == "followup" and h.c.turn.followups == n + 1
        assert creates(h)[-1]["tool_choice"] == "none"  # a follow-up only speaks (the countdown resumes)
    await respond(h, call("lookup_evidence", {"query": "Friday again"}), transcript="", audio_ms=0)
    *_, (call_id, output) = outputs(h)
    assert json.dumps(output) == LIMIT_OUTPUT
    assert h.tool_statuses()[-1] == (call_id, "lookup_evidence", "rejected")
    assert len(creates(h)) == 1 + 1 + MAX_FOLLOWUPS  # the narration, the question, the follow-ups: no more
    assert len(h.c.mstate.lookups) == MAX_FOLLOWUPS
    await h.drained()
    assert h.c.state == IDLE and h.c.status == "resuming"  # nothing was said: the countdown resumes anyway


async def test_a_follow_up_after_a_spoken_first_response_waits_until_it_has_played(h: Harness):
    await present_first_point(h)
    await h.typed(ALICE_QUESTION, reply=1, played=500)
    await respond(h, call("lookup_evidence", {"query": "Friday beta release"}), transcript="Let me check.",
                  audio_ms=600)
    assert len(creates(h)) == 2 and h.c.turn.next_followup == prompts.FOLLOWUP_LOOKUP  # not over its audio
    await h.drained(reply=2)
    assert creates(h)[-1]["metadata"] == {"relay": "followup", "reply": "3"}
    assert h.c.turn.answers == ["Let me check."]


async def test_a_cut_during_the_first_response_drops_the_follow_up(h: Harness):
    await present_first_point(h)
    await h.typed(ALICE_QUESTION, reply=1, played=500)
    await respond(h, call("lookup_evidence", {"query": "Friday beta release"}), transcript="Let me check.",
                  audio_ms=600)
    await h.key("esc", reply=2, played=300)
    assert len(creates(h)) == 3 and creates(h)[-1]["metadata"]["relay"] == "narration"  # back to the point
    assert h.c.state == NARRATING and h.c.mstate.clarifications == []  # nothing was answered


async def test_a_late_call_of_a_response_that_was_followed_up_gets_the_cut_output(h: Harness):
    await present_first_point(h)
    await h.typed(ALICE_QUESTION, reply=1, played=500)
    rid = await respond(h, call("lookup_evidence", {"query": "Friday"}), transcript="", audio_ms=0)
    await h.ev("response.function_call_arguments.done", response_id=rid, item_id="fc_late", call_id="late",
               name="lookup_evidence", arguments='{"query": "late"}')
    assert outputs(h)[-1] == ("late", json.loads(tools.error_output("not run: the reply was cut off")))
    assert len(h.c.mstate.lookups) == 1


async def test_the_opening_starts_the_presentation_through_set_presentation_state(h: Harness):
    await h.key("space")
    assert "Alice is about to hand you the floor" in h.instructions()
    await h.speak()  # server VAD events can't end a manual turn (the segmenter does)
    assert h.c.state == LISTENING
    await h.mic(3)
    await h.key("space")  # send now
    assert h.c.state == REPLYING and h.c.turn.ended_by == "hand"
    await respond(h, call("set_presentation_state", {"action": "start"}), transcript="Hi all, let's start.",
                  audio_ms=500)
    assert outputs(h)[-1][1] == {"ok": True, "slide": "plan"}
    await h.drained()
    assert (h.c.state, creates(h)[-1]["metadata"]["talking_point"]) == (NARRATING, "friday-beta")
    assert "If you haven't welcomed the room yet" in creates(h)[-1]["instructions"]


async def test_an_intervention_that_never_comes_goes_back_to_the_room(h: Harness):
    await to_intervention(h)
    await h.clock.advance(10)
    assert h.c.state == ROOM and "No reply" in h.errors()


async def test_a_silent_state_call_gets_a_confirmation_without_tools(h: Harness):
    await present_first_point(h)
    await h.typed("Please note that Friday is beta.", reply=1, played=500)
    await respond(h, call("record_clarification", {"question": "Is Friday beta?", "answer": "Friday is beta.",
                                                   "source_ids": ["REQ-2"]}), transcript="", audio_ms=0)
    confirm = creates(h)[-1]
    assert confirm == {"instructions": prompts.followup_instructions(
        prompts.FOLLOWUP_CONFIRM, meeting=h.c.meeting, agenda_phase="presentation",
        question="Please note that Friday is beta."),
        "metadata": {"relay": "followup", "reply": "3"}, "tool_choice": "none"}
    await respond(h, transcript="Noted.", audio_ms=300)
    await h.drained()
    assert h.c.status == "resuming"


async def test_a_question_can_end_the_presentation_and_ask_for_the_state(h: Harness):
    await present_first_point(h)
    await h.typed("Can we skip to the discussion?", reply=1, played=500)
    await respond(h, call("get_presentation_state"), call("generate_meeting_notes"), transcript="", audio_ms=0)
    (_, state), (_, notes) = outputs(h)
    assert state["agenda_phase"] == "presentation" and state["talking_point"] == "friday-beta"
    assert [phase["id"] for phase in state["agenda"]] == ["presentation", "discussion", "wrapup"]
    assert notes["notes"].startswith("Meeting notes: Release 4.3 rollout review")
    await respond(h, call("set_presentation_state", {"action": "end_presentation"}), transcript="Sure.",
                  audio_ms=300)
    assert outputs(h)[-1][1] == {"ok": True, "note": "handing over to the discussion"}
    await h.drained()
    assert creates(h)[-1]["metadata"]["relay"] == "handover"


async def test_set_presentation_state_moves_the_deck_like_relays_tools(h: Harness):
    await present_first_point(h)
    await h.typed("Show the comms slide.", reply=1, played=500)
    await respond(h, call("set_presentation_state", {"action": "goto_slide", "slide_id": "comms"}),
                  call("set_presentation_state", {"action": "dance"}), transcript="Here it is.", audio_ms=300)
    (_, goto), (_, bad) = outputs(h)
    assert goto == {"ok": True, "slide": "comms"} and bad == {"error": "unknown action 'dance'"}
    await h.drained()
    assert (h.c.state, creates(h)[-1]["metadata"]["talking_point"]) == (NARRATING, "beta-notes")


# --- the discussion (Milestone 2) ------------------------------------------------------------------------------


async def test_milestone_2_the_facilitator_speaks_up_before_a_wrong_decision_and_the_fix_is_saved(h: Harness):
    await to_discussion(h)
    h.reset()
    await room_line(h, "Alice", ALICE_QUESTION)  # a question: no conflict, no event beyond nothing
    assert h.b("transcript")[0]["utterance"] == {"id": "U1", "text": ALICE_QUESTION, "speaker": "Alice",
                                                 "at_s": h.b("transcript")[0]["utterance"]["at_s"],
                                                 "phase": "discussion", "source": "typed"}
    assert [msg["event"]["type"] for msg in h.b("alignment")] == []
    await room_line(h, "Bob", BOB_LINE)
    kinds = [msg["event"]["type"] for msg in h.b("alignment")]
    assert kinds.count("conflict") == 2 and "intervene" not in kinds
    assert (h.c.state, h.realtime.items) == (ROOM, [])  # silent: nobody is confirming yet

    await room_line(h, "Carol", CAROL_CONFIRM)
    assert h.b("alignment")[-1]["event"]["type"] == "intervene"
    lookup = h.b("evidence")[-1]["lookup"]
    assert (lookup["by"], lookup["purpose"]) == ("engine", "intervention")
    assert lookup["results"][0]["source_id"] in ("REQ-2", "DEC-1")
    request = creates(h)[-1]
    assert request["metadata"] == {"relay": "intervention", "reply": str(h.c.turn.reply)}
    assert request["tool_choice"] == "none"
    assert '"I\'ll draft the announcement for Friday\'s production launch to all customers."' in \
        request["instructions"]
    assert "One clarification before we confirm" in request["instructions"]
    assert "REQ-2" in request["instructions"]
    assert (h.c.state, h.c.status) == (INTERVENING, "intervening")
    await respond(h, transcript="One clarification before we confirm: Friday is beta. Beta users only?",
                  audio_ms=1500)
    assert h.b("reply_start")[-1]["kind"] == "intervention"
    await h.drained()
    assert h.c.state == ROOM

    await room_line(h, "Carol", CAROL_FIX)
    kinds = [msg["event"]["type"] for msg in h.b("alignment")]
    assert "resolved" in kinds and kinds[-1] == "decision_draft"
    draft = h.b("alignment")[-1]["event"]
    assert draft["owner"] == "Bob"
    request = creates(h)[-1]
    assert request["metadata"]["relay"] == "decision" and "tool_choice" not in request
    assert prompts.DRAFT_PREFIX + json.dumps({"decision": draft["decision"], "owner": "Bob",
                                             "source_ids": draft["source_ids"]}) in request["instructions"]
    assert "`follow_up` as the owner's next step" in request["instructions"]  # (in its own words, not a quote)
    assert "`decision` as one short plain sentence" in request["instructions"]  # not the draft's labels
    # live 2026-10-23: the model may save an invented decision; it is rejected and the draft goes in instead
    await respond(h, call("save_decision", {"decision": "Use the new onboarding flow for the pilot program",
                                            "owner": "Alex", "follow_up": "Tell the pilot users"}),
                  transcript="Confirmed. It's in the notes.", audio_ms=900)
    (call_id, output), = [item for item in outputs(h) if item[0] == h.tool_statuses()[-2][0]]
    assert output["error"].startswith("unknown owner 'Alex'")
    assert h.tool_statuses()[-2:] == [(call_id, "save_decision", "rejected"),
                                      (h.tool_statuses()[-1][0], "save_decision", "auto")]
    decision = h.c.mstate.decisions[0]
    assert (decision.decision, decision.owner, decision.by) == (draft["decision"], "Bob", "auto")
    assert h.c.mstate.action_items[0].owner == "Bob"
    assert h.last("meeting")["decisions"][0]["by"] == "auto"
    await h.drained()
    # "Confirmed. It's in the notes." names neither the decision nor Bob (live 2026-10-24, 1.5-preview said only
    # "Let me save that decision."): a confirm follow-up tells the room what was decided before anything else happens
    assert h.c.state == INTERVENING
    confirm = creates(h)[-1]
    assert confirm["metadata"]["relay"] == "followup" and confirm["tool_choice"] == "none"
    assert "You just saved this decision to the meeting notes" in confirm["instructions"]
    assert draft["decision"] in confirm["instructions"] and "owned by Bob" in confirm["instructions"]
    await respond(h, transcript="Confirmed: Friday is beta, announced to beta users only; Bob owns it.", audio_ms=800)
    assert h.b("reply_start")[-1]["kind"] == "decision"
    await h.drained()
    assert h.c.state == ROOM

    await h.key("phase")  # wrap-up
    assert (h.c.agenda_phase, h.b("mic")[-1]) == ("wrapup", {"type": "mic", "open": False})
    meeting = h.last("meeting")
    assert meeting["phase"] == "wrapup" and meeting["notes"]["decisions"][0]["owner"] == "Bob"
    summary = creates(h)[-1]
    assert summary["metadata"]["relay"] == "summary" and summary["tool_choice"] == "none"
    assert "Bob" in summary["instructions"] and h.c.status == "wrapping_up"
    await respond(h, transcript="Friday is beta for beta users; Bob sends it.", audio_ms=900)
    assert h.b("reply_start")[-1]["kind"] == "summary"
    await h.drained()
    assert (h.c.state, h.c.banner) == (IDLE, BANNER_WRAPPED)
    await h.key("phase")
    assert (h.c.state, h.c.agenda_phase) == (IDLE, "wrapup")  # nothing after the wrap-up


async def test_a_valid_save_decision_is_kept_and_a_repeat_is_already_saved(h: Harness):
    await to_intervention(h)
    await respond(h, transcript="One clarification: beta only?", audio_ms=500)
    await h.drained()
    await room_line(h, "Carol", CAROL_FIX)
    await respond(h, call("save_decision", {"decision": "Friday: beta release, announcement to beta users only",
                                            "owner": "bob", "follow_up": "Bob notifies beta users",
                                            "source_ids": ["REQ-2", "XYZ-1"]}),
                  call("save_decision", {"decision": "Friday: beta release, announcement to beta users only.",
                                         "owner": "Bob"}),
                  transcript="", audio_ms=0)
    (_, first), (_, second) = outputs(h)[-2:]
    assert first == {"ok": True, "id": "D1", "dropped_source_ids": ["XYZ-1"]}
    assert second == {"ok": True, "note": "already saved"}
    decision = h.c.mstate.decisions[0]
    assert (decision.owner, decision.by, decision.source_ids, len(h.c.mstate.decisions)) == ("Bob", "model",
                                                                                           ["REQ-2"], 1)
    confirm = creates(h)[-1]  # saved, nothing said: a confirmation follows
    assert confirm["metadata"]["relay"] == "followup" and confirm["tool_choice"] == "none"
    # self-contained (live 2026-10-24: "confirm the decision" alone made the model greet the room)
    assert ('You just saved this decision to the meeting notes: "Friday: beta release, announcement to beta users '
            'only", owned by Bob.') in confirm["instructions"]
    assert "Don't greet or introduce yourself." in confirm["instructions"]
    await respond(h, transcript="Saved: beta users only, Bob sends it.", audio_ms=400)
    assert h.b("reply_start")[-1]["kind"] == "decision"
    await h.drained()
    assert h.c.state == ROOM


async def test_what_the_models_decision_leaves_out_comes_from_the_draft(h: Harness):
    await to_intervention(h)
    await respond(h, transcript="One clarification.", audio_ms=500)
    await h.drained()
    await room_line(h, None, CAROL_FIX)
    await respond(h, call("save_decision", {"decision": "Friday is the beta, announced to beta users only"}),
                  transcript="Confirmed, it's in the notes.", audio_ms=600)
    decision = h.c.mstate.decisions[0]
    assert (decision.decision, decision.owner, decision.by) == (
        "Friday is the beta, announced to beta users only", "Bob", "model")
    # (the action item: the sentence that gave Bob the task, not Carol's whole reply)
    assert decision.follow_up == "Bob owns the announcement." and decision.source_ids
    assert [(item.owner, item.text) for item in h.c.mstate.action_items] == [("Bob", "Bob owns the announcement.")]


async def test_a_decision_cut_off_still_saves_the_draft(h: Harness):
    await to_intervention(h)
    await respond(h, transcript="One clarification.", audio_ms=500)
    await h.drained()
    await room_line(h, "Carol", CAROL_FIX)
    assert h.c.state == INTERVENING
    await h.key("esc")
    assert h.c.state == ROOM and h.c.mstate.decisions[0].by == "auto"


async def test_the_room_mic_buffers_locally_and_commits_each_utterance(h: Harness):
    await to_discussion(h)
    await h.send({"type": "speaker", "name": "bob"})
    assert h.c.speaker == "Bob"
    h.reset()
    await h.send({"type": "mic", "pcm": TONE})  # still the facilitator's echo window: ignored
    await h.clock.advance(ROOM_ECHO_S + 0.01)
    for _ in range(3):
        await h.mic()  # silence: nothing goes to Realtime
    assert h.realtime.items == []
    for _ in range(3):
        await h.send({"type": "mic", "pcm": TONE})
    appended = h.r("input_audio_buffer.append")
    assert appended and sum(len(base64.b64decode(e["audio"])) for e in appended) >= 4800 * 3  # pre-roll + speech
    for _ in range(8):
        await h.mic()
    assert h.realtime.types()[-1] == "input_audio_buffer.commit"
    assert "response.create" not in h.realtime.types()  # transcribed, never answered
    await h.ev("input_audio_buffer.committed", item_id="item_room1")
    await h.send({"type": "speaker", "name": "Room"})  # a speaker change after the commit doesn't relabel it
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="item_room1", transcript=BOB_LINE)
    utterance = h.b("transcript")[0]["utterance"]
    assert (utterance["text"], utterance["speaker"], utterance["source"]) == (BOB_LINE, "Bob", "voice")
    assert [msg["event"]["type"] for msg in h.b("alignment")].count("conflict") == 2
    assert h.c.state == ROOM and not h.b("caption")


async def test_half_duplex_the_room_is_not_heard_while_the_facilitator_speaks(h: Harness):
    await to_intervention(h)
    h.reset()
    for _ in range(5):
        await h.send({"type": "mic", "pcm": TONE})
    assert h.r("input_audio_buffer.append") == []  # INTERVENING: ignored
    await respond(h, transcript="One clarification.", audio_ms=500)
    await h.drained()
    assert h.c.state == ROOM
    await h.send({"type": "mic", "pcm": TONE})
    await h.send({"type": "mic", "pcm": TONE})
    assert h.r("input_audio_buffer.append") == []  # the 300 ms after it drained: still its echo
    await h.clock.advance(ROOM_ECHO_S)
    for _ in range(3):
        await h.send({"type": "mic", "pcm": TONE})
    assert h.r("input_audio_buffer.append")


async def test_an_utterance_being_spoken_is_committed_when_the_facilitator_speaks_up(h: Harness):
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await h.clock.advance(ROOM_ECHO_S + 0.01)
    for _ in range(3):
        await h.send({"type": "mic", "pcm": TONE})  # someone is talking
    h.reset()
    await room_line(h, "Carol", CAROL_CONFIRM)  # typed meanwhile: the gate fires
    assert h.realtime.types()[:1] == ["input_audio_buffer.commit"]  # committed before the intervention's request
    assert h.realtime.types()[-1] == "response.create"


async def test_space_in_the_room_asks_the_facilitator_directly(h: Harness):
    await to_discussion(h)
    await h.clock.advance(ROOM_ECHO_S + 0.01)
    for _ in range(3):
        await h.send({"type": "mic", "pcm": TONE})  # mid-utterance
    h.reset()
    await h.key("space")
    assert (h.c.state, h.c.turn.origin, h.c.turn.speech_started) == (LISTENING, "room", True)
    assert "input_audio_buffer.clear" not in h.realtime.types()  # the utterance so far is the question's start
    for _ in range(8):
        await h.mic()
    assert h.c.state == REPLYING and h.realtime.types()[-2:] == ["input_audio_buffer.commit", "response.create"]
    assert h.c.status == "answering"
    await respond(h, call("lookup_evidence", {"query": "who owns announcements"}), transcript="", audio_ms=0)
    followup = creates(h)[-1]
    assert followup["tool_choice"] == "none" and "Don't call any tool." in followup["instructions"]
    await respond(h, call("set_presentation_state", {"action": "resume"}), transcript="Bob does.", audio_ms=400)
    assert outputs(h)[-1][1]["error"].startswith("not now: the presentation is over")
    await h.drained()
    assert (h.c.state, h.c.mstate.clarifications) == (ROOM, [])  # no automatic clarification in the discussion


async def test_asked_to_wrap_up_the_facilitator_ends_the_discussion(h: Harness):
    await to_discussion(h)
    await h.key("space")
    await h.mic(6)
    await h.key("space")  # "Thanks, everyone. Facilitator, please wrap us up."
    assert "call set_presentation_state with action end_presentation" in h.instructions()
    await respond(h, call("set_presentation_state", {"action": "end_presentation"}), transcript="Sure.",
                  audio_ms=300)
    assert outputs(h)[-1][1] == {"ok": True, "note": "wrapping up the meeting"}
    await h.drained()
    assert (h.c.agenda_phase, creates(h)[-1]["metadata"]["relay"]) == ("wrapup", "summary")


async def test_space_cuts_an_intervention_and_the_question_goes_back_to_the_room(h: Harness):
    await to_intervention(h)
    await respond(h, transcript="One clarification before we confirm, Friday is beta.", audio_ms=1500, done=False)
    turn = h.c.turn
    await h.key("space", reply=turn.reply, played=400)
    assert h.b("reply_flush")[-1] == {"type": "reply_flush", "reply": turn.reply}
    assert (h.c.state, h.c.turn.origin) == (LISTENING, "room")
    await h.key("esc")
    assert h.c.state == ROOM


async def test_typed_lines_in_the_room_need_no_realtime(mk):
    h = mk()
    await to_discussion(h)
    h.realtime.ready = False
    await h.c.on_realtime_status("reconnecting", "connection lost")
    h.reset()
    await room_line(h, "Bob", BOB_LINE)
    await room_line(h, "Carol", CAROL_CONFIRM)
    assert h.b("alignment")[-1]["event"]["type"] == "intervene"  # on screen…
    assert h.c.state == ROOM and h.realtime.items == []  # …but no voice without the link
    await room_line(h, "Carol", CAROL_FIX)  # the resolution is saved silently
    assert h.c.mstate.decisions[0].by == "auto" and h.c.state == ROOM
    await h.key("space")  # no facilitator to ask: the room stays open
    assert h.c.state == ROOM and h.errors()[-1].startswith("Realtime offline — the facilitator can't answer")


async def test_tool_calls_in_an_intervention_are_not_run(h: Harness):
    await to_intervention(h)
    await respond(h, call("lookup_evidence", {"query": "x"}), transcript="One clarification.", audio_ms=500)
    assert json.dumps(outputs(h)[-1][1]) == NOT_NOW_OUTPUT
    assert h.tool_statuses()[-1][2] == "rejected" and h.c.mstate.lookups[-1].by == "engine"


async def test_the_wrap_up_turns_open_conflicts_into_open_questions(h: Harness):
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)  # a conflict nobody confirmed or settled
    await h.key("phase")
    assert [msg["event"]["type"] for msg in h.b("alignment")][-2:] == ["suggestion", "suggestion"]
    questions = h.c.mstate.open_questions
    assert len(questions) == 2 and questions[0].text.startswith("Friday's release stage: beta")
    assert h.last("meeting")["open_questions"][0]["text"] == questions[0].text
    assert "Open questions:" in creates(h)[-1]["instructions"]


async def test_the_wrap_up_without_the_link_says_nothing(mk):
    h = mk()
    await to_discussion(h)
    h.realtime.ready = False
    await h.key("phase")
    assert (h.c.state, h.c.banner, h.c.agenda_phase) == (IDLE, BANNER_WRAPPED, "wrapup")


def test_legacy_mode_is_explicit_and_labelled(h: Harness):
    """Contract v5 §0: the regex engine runs only on `--alignment legacy` with a checks.yaml, and says so."""
    assert h.c.alignment_mode == "legacy" and h.c.engine is not None and h.c._analysis is None
    assert h.c.alignment_info() == {"mode": "legacy", "model": None, "available": True}
    assert h.c.alignment_status()["mode"] == "legacy" and h.c.mstate.alignment["available"] is True


async def test_legacy_events_name_their_issue_type_and_topic(h: Harness):
    """The deck shows every alignment event by issue type (contract v5 §4): the engine's events are forwarded with
    `issue_type: evidence_conflict` and the slot label as `topic`."""
    await to_intervention(h)
    events = [msg["event"] for msg in browser_history(h, "alignment")]
    assert events and all(event["issue_type"] == "evidence_conflict" for event in events)
    assert {event["topic"] for event in events if event["type"] == "conflict"} == {
        "Friday's release stage", "Friday's announcement audience"}
    intervene = [event for event in events if event["type"] == "intervene"][-1]
    assert intervene["topic"] == intervene["label"] == "Friday's release stage"
    assert h.b("hello") == []  # (the hello carries the mode too: see test_hello_carries_the_alignment_mode)


async def test_hello_carries_the_alignment_mode(h: Harness):
    await h.c.on_browser_connected()
    assert h.b("hello")[0]["alignment"] == {"mode": "legacy", "model": None, "available": True}
    assert h.last("state")["alignment"]["mode"] == "legacy" and h.last("meeting")["alignment"]["mode"] == "legacy"


async def test_reset_starts_the_meeting_again_with_a_fresh_session(h: Harness):
    await to_intervention(h)
    await respond(h, transcript="One clarification.", audio_ms=500, done=False)
    h.reset()
    await h.key("reset")
    assert h.realtime.restarts == 1
    assert (h.c.state, h.c.agenda_phase, h.c.current_slide.id, h.c.cursor) == (IDLE, "presentation", "agenda", None)
    assert h.c.mstate.transcript == [] and h.c.mstate.alerts == [] and h.c.mstate.lookups == []
    assert h.b("slide")[-1] == {"type": "slide", "slide": "agenda", "fragment": None}
    assert h.last("meeting")["phase"] == "presentation" and h.b("mic")[-1] == {"type": "mic", "open": False}
    assert h.c.phase == "opening" and not h.c._history
    # the fresh session: no summary of the old meeting
    await h.c.on_realtime_status("reconnecting", "restarting: a fresh session")
    await h.c.on_realtime_status("ready")
    await h.c.on_realtime_status("ready")
    assert "conversation.item.create" not in h.realtime.types()
    # the engine forgot too: the same lines raise the same intervention again
    await to_intervention(h)


async def test_the_deck_reconnecting_in_the_discussion_keeps_listening(h: Harness):
    await to_intervention(h)
    await respond(h, transcript="One clarification.", audio_ms=500)
    h.reset()
    await h.c.on_browser_disconnected()
    assert h.c.state == ROOM
    await h.c.on_browser_connected()
    assert h.b("mic") == [{"type": "mic", "open": True}] and h.c.state == ROOM


async def test_arrows_after_the_presentation_only_show_slides(h: Harness):
    await to_discussion(h)
    h.reset()
    await h.key("prev")
    assert h.b("slide") == [{"type": "slide", "slide": "comms", "fragment": None}]
    assert (h.c.state, creates(h)[-1]["metadata"]["relay"]) == (ROOM, "handover")  # no narration
