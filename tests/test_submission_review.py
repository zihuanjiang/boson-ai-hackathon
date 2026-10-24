"""talks/submission-review: the facilitator introduces On the Same Page (contract v4). The package loads with no
issues, its say lines obey the narration rules, its relay slides fit the prompt's screen text, and the demo's lines go
through the real alignment engine the way the run of show says: the sources establish "online" (LIM-2 alone),
Alice's interruption is a question, Bob's claim is one consequential conflict citing LIM-2 and the facilitator's
answer (CL-1), Carol's confirmation opens the gate, and her correction resolves it with owner Bob.

The in_person value fires only on a capability claim ("works in a meeting room", "in-person meetings too"), never on a
mention: the facilitator's own answer to Alice is recorded as CL-1 and can override the sources if it asserts one value,
and the discussion is literally about which limitations to write down, so the true sentences mention the in-person case
all the time. The answers that only describe the limitation therefore assert nothing (CL-1 recorded, LIM-2 alone
establishes online), the limitation sentences stay silent, and the capability claims still conflict.

The checks mirror .scratch/same-page/intro/content/run_intro_lines.py (which prints the details)."""

from __future__ import annotations

import re

import pytest

from relay import alignment
from relay.config import ROOT
from relay.evidence import entries, search, search_for_question
from relay.meeting import MeetingState, load_demo, load_meeting, validate_meeting
from relay.prompts import SCREEN_MAX_CHARS, screen_text
from relay.talk import check_talk, load_talk
from relay.textutil import abbreviations, acronyms, ends_sentence, has_digits, mid_sentence_breaks, strip_tags

TALK = ROOT / "talks" / "submission-review"
POINTS = ["problem", "idea", "one-session", "interrupt-answer", "listen-check", "live-numbers", "what-is-live",
          "limits-scope", "limits-room"]
ANSWER = ("No: it's built for online meetings, where each participant's audio arrives as its own stream. A shared room "
          "microphone would need echo cancellation, which is future work. I'll get back to the slide.")
# What the facilitator is likely to say to Alice from LIM-2 / LIM-3 (recorded as CL-1): each must establish online.
ANSWERS = [
    ANSWER,
    "Not yet. It's designed for online meetings, not in-person ones. Each participant's audio arrives as its own digital "
    "stream, so a single microphone that also hears the facilitator would need echo cancellation first.",
    "Right now it works for online meetings only. A normal meeting room with one microphone would need echo "
    "cancellation, which is future work.",
    "It doesn't work in a normal meeting room yet. It's built for online meetings, where each participant's audio comes "
    "in as a separate stream.",
    "Good question. It is built for online meetings, where every participant's audio arrives as its own stream. One "
    "shared microphone would also pick up the facilitator's own voice, so that needs echo cancellation, which is future "
    "work.",
    "Online meetings only for now. In-person meetings with a single room microphone would need echo cancellation, which "
    "we consider future work. Back to the pipeline.",
    "No. The documents say it is built for online meetings, not for a meeting room with one microphone: a shared "
    "microphone would hear the facilitator itself, and echo cancellation is future work.",
    "Online only. Each participant's audio arrives as a separate digital stream, so the facilitator never hears itself. "
    "A shared room mic is future work.",
]
# Answers that only describe the limitation, without naming online meetings: CL-1 is recorded but asserts nothing, so
# the sources alone establish online (never in_person, which needs a capability claim).
LIMITATION_ANSWERS = [
    "No. A normal meeting room with one microphone is a limitation of this submission: the facilitator would hear its own "
    "speaker output. I'll get back to the slide.",
    "No, the in-person case is a limitation of this submission.",
    "That's a limitation we list: in-person meetings. A single microphone that hears the facilitator needs echo "
    "cancellation first.",
    "That setup is a known limitation: a single shared microphone in a meeting room is out of scope for this submission, "
    "because the facilitator hears itself. I'll get back to the slide.",
    "A normal meeting room with one microphone is exactly the case that is not supported yet: the facilitator would hear "
    "its own output and would need echo cancellation.",
    "It doesn't work in a normal meeting room.",
]
BOBS = ["I'll write that it works for in person meetings too as long as there's a microphone in the room",
        "I’ll write that it works for in-person meetings too, as long as there’s a microphone in the room.",
        "i'll write that it works for in person meetings too, as long as theres a microphone in the room.",
        "I will write that it works for in-person meetings too, as long as there is a microphone in the room."]
# Other capability claims the check must still catch (so the patterns cannot be tightened into silence).
CAPABILITY_CLAIMS = ["I'll say it's for in-person meetings too.", "I'll write that in-person meetings work too.",
                     "I'll put that it supports hybrid meetings.", "I'll say it also works in a meeting room."]
FINS = ["Great. Let's finalize that.", "Great, let's finalise that.", "Great lets finalize that", "Great, let’s finalize that."]
CORS = ["Yes online meetings only Bob owns the write up", "Yes. Online meetings only. Bob owns the write-up.",
        "Yes, online meetings only, Bob owns the write-up.", "Yeah, online meetings only. Bob owns the writeup.",
        "Yes, on line meetings only. Bob owns the write-up.",  # the speech-to-text spelling
        "Yes. Online meetings only. Bob owns the write-up. In-person is a limitation."]  # a mention is not a claim


@pytest.fixture(scope="module")
def talk():
    return load_talk(TALK)


@pytest.fixture(scope="module")
def pkg(talk):
    return load_meeting(talk)


@pytest.fixture(scope="module")
def demo(talk, pkg):
    return load_demo(talk, pkg)


@pytest.fixture(scope="module")
def say(demo):
    return {step.id: step.say for step in demo.clips}


def types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def by_type(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["type"] == kind]


def run(pkg, lines: list[tuple[str, str]], clarification: str | None = ANSWER) -> list[list[dict]]:
    """The discussion lines through a fresh engine, after the facilitator's answer to Alice (CL-1) unless None."""
    state = MeetingState(pkg, clock=lambda: 0.0)
    if clarification:
        state.add_clarification("Wait, does this work in a normal meeting room with just one microphone?", clarification,
                                ["LIM-2", "LIM-3"], by="auto")
    state.set_phase("discussion")
    engine = alignment.Engine(pkg, state)
    return [engine.observe(state.add_utterance(text, speaker, "voice")) for speaker, text in lines]


# --- the package ---------------------------------------------------------------------------------------------------

def test_the_package_validates_with_no_errors_and_no_warnings():
    assert check_talk(TALK) + validate_meeting(TALK) == []


def test_nine_talking_points_on_four_relay_slides_and_six_demo_steps(talk, pkg, demo):
    assert [slide.id for slide in talk.slides] == ["agenda", "background", "pipeline", "results", "limits", "discussion"]
    assert [chunk.id for slide in talk.relay_slides for chunk in slide.chunks] == POINTS
    assert all(slide.fragments == len(slide.chunks) for slide in talk.relay_slides)  # one fragment per point
    assert pkg.title == "On the Same Page: submission review" and pkg.host == "Alice"
    assert pkg.participant_names == ["Alice", "Bob", "Carol"] and pkg.discussion_slide == "discussion"
    assert pkg.phase("discussion").prompt == "Which limitations go in the write-up, and who writes it?"
    assert [step.id for step in demo.steps] == ["start", "alice-question", "bob-claim", "carol-finalize", "carol-correct",
                                                "wrap"]
    assert len(demo.clips) == 4 and demo.voices == {"Alice": "chloe", "Bob": "jake", "Carol": "eleanor"}
    cue = demo.step("alice-question").when
    assert cue["talking_point"] in POINTS and (cue["talking_point"], cue["heard_ms"]) == ("interrupt-answer", 1500)
    assert demo.step("wrap").when == {"after_reply": "decision", "phase": "discussion", "quiet_ms": 2000, "timeout_ms": 90000}
    # Bob's clock starts when Alice's clip ends, and six talking points come first (~100 s live): its own 3 min timeout
    assert demo.step("bob-claim").when == {"phase": "discussion", "quiet_ms": 1500, "timeout_ms": 180000}


def test_every_say_obeys_the_narration_rules_and_stays_true_on_the_backup(talk):
    for slide in talk.relay_slides:
        for chunk in slide.chunks:
            text = strip_tags(chunk.say)
            assert len(chunk.say) <= 300 and ends_sentence(chunk.say), chunk.id
            assert not has_digits(chunk.say) and not re.search(r"[()\[\]]|https?://|www\.", text), chunk.id
            assert not acronyms(chunk.say) and not abbreviations(chunk.say) and not mid_sentence_breaks(chunk.say), chunk.id
            assert not re.search(r"\b(?:I am saying|I'm saying|everything I say|live right now)\b", text, re.I), chunk.id
    words = sum(len(strip_tags(c.say).split()) for s in talk.relay_slides for c in s.chunks)
    assert 170 <= words <= 220  # ≤ ~85 s at the measured pace of voice `default` (about 150 words a minute)
    agenda = talk.slides_md.split("---")[0]
    assert "prerecorded participants" in agenda and "the facilitator is live" in agenda  # the disclosure line


def test_every_relay_slides_screen_text_fits_the_prompt_unclipped(talk):
    for slide in talk.relay_slides:
        text = screen_text(talk, slide)
        assert len(text) < SCREEN_MAX_CHARS and not text.endswith("…"), (slide.id, len(text))
        assert "&nbsp;" not in text, slide.id


# --- the sources and the alignment check ---------------------------------------------------------------------------

def test_the_sources_establish_online_from_lim_2_alone(pkg):
    assert [s.id for s in pkg.statements] == ([f"DES-{n}" for n in range(1, 9)] + [f"LIM-{n}" for n in range(1, 8)]
                                             + [f"RES-{n}" for n in range(1, 5)])
    asserting = [s.id for s in pkg.statements
                 if any(not c.negated for c in alignment.extract(s.text, pkg.checks))]
    assert asserting == ["LIM-2"]
    assert alignment.extract(pkg.statement("LIM-3").text, pkg.checks) == []
    est = alignment.established(pkg, None)
    assert set(est) == {"meeting_setting"}
    assert (est["meeting_setting"].value, est["meeting_setting"].source_ids) == ("online", ["LIM-2"])


def test_alices_interruption_is_a_question_that_retrieves_lim_2_and_lim_3(pkg, say):
    alice = say["alice-question"]
    claims = alignment.extract(alice, pkg.checks)
    assert claims and all(c.question for c in claims) and {c.value for c in claims} == {"in_person"}
    corpus = entries(pkg, None)
    assert {r["source_id"] for r in search(alice, corpus)[:2]} == {"LIM-2", "LIM-3"}
    for query in ["meeting room single microphone", "in-person meeting support", "does it work with one microphone",
                  "microphone", "hardware requirements"]:
        assert {r["source_id"] for r in search_for_question(query, alice, corpus)[:2]} == {"LIM-2", "LIM-3"}, query


@pytest.mark.parametrize("answer", ANSWERS)
def test_the_facilitators_likely_answers_establish_online_as_cl_1(pkg, say, answer):
    state = MeetingState(pkg, clock=lambda: 0.0)
    state.add_clarification(say["alice-question"], answer, ["LIM-2"], by="auto")
    est = alignment.established(pkg, state)["meeting_setting"]
    assert (est.value, est.source_ids, est.clarification_ids) == ("online", ["LIM-2"], ["CL-1"])


@pytest.mark.parametrize("answer", LIMITATION_ANSWERS)
def test_an_answer_that_only_describes_the_limitation_asserts_nothing(pkg, say, answer):
    assert alignment._asserted(answer, pkg.checks).get("meeting_setting", []) == []
    state = MeetingState(pkg, clock=lambda: 0.0)
    state.add_clarification(say["alice-question"], answer, ["LIM-2", "LIM-3"], by="auto")
    est = alignment.established(pkg, state)["meeting_setting"]
    assert (est.value, est.source_ids, est.clarification_ids) == ("online", ["LIM-2"], [])  # CL-1 recorded, not cited
    # and the discussion still runs: Bob conflicts (LIM-2 alone), Carol's confirmation intervenes, her correction resolves
    bob, finalize, correct = run(pkg, [("Bob", say["bob-claim"]), ("Carol", say["carol-finalize"]),
                                       ("Carol", say["carol-correct"])], clarification=answer)
    assert by_type(bob, "conflict")[0]["clarification_ids"] == [] and types(finalize) == ["intervene"]
    assert by_type(correct, "decision_draft")[0]["owner"] == "Bob"


def test_the_discussion_lines_through_the_real_engine(pkg, say):
    clock = [0.0]
    state = MeetingState(pkg, clock=lambda: clock[0])
    state.add_clarification(say["alice-question"], ANSWER, ["LIM-2", "LIM-3"], by="auto")
    state.set_phase("discussion")
    engine = alignment.Engine(pkg, state)
    events = []
    for speaker, text in [("Bob", say["bob-claim"]), ("Carol", say["carol-finalize"]), ("Carol", say["carol-correct"])]:
        clock[0] += 4
        events.append(engine.observe(state.add_utterance(text, speaker, "voice")))
    bob, finalize, correct = events

    assert types(bob) == ["claim", "conflict"]  # one conflict, silent: nobody has confirmed anything
    conflict = bob[1]
    assert (conflict["claimed"], conflict["established"], conflict["has_action"]) == ("in_person", "online", True)
    assert (conflict["source_ids"], conflict["clarification_ids"]) == (["LIM-2"], ["CL-1"])

    assert types(finalize) == ["intervene"]
    gate = finalize[0]
    assert gate["conflict_ids"] == [conflict["id"]] and gate["quote_speaker"] == "Bob"
    assert (gate["quote"], gate["confirm_quote"]) == (say["bob-claim"], say["carol-finalize"])
    assert (gate["source_ids"], gate["clarification_ids"]) == (["LIM-2"], ["CL-1"])
    hits = [r["source_id"] for r in search(gate["query"], entries(pkg, state))]
    assert hits[0] in ("LIM-2", "CL-1")  # the intervention's evidence on screen

    assert types(correct) == ["claim", "resolved", "decision_draft"]
    resolved, draft = correct[1], correct[2]
    assert (resolved["value"], resolved["owner"], resolved["plan_changed"]) == ("online", "Bob", False)
    assert (draft["owner"], draft["source_ids"], draft["clarification_ids"]) == ("Bob", ["LIM-2"], ["CL-1"])
    assert (draft["decision"], draft["follow_up"]) == ("Where it works: online", "Bob owns the write-up.")
    state.add_decision(draft["decision"], draft["owner"], draft["follow_up"], draft["source_ids"], by="auto")
    assert [(d["owner"], d["source_ids"]) for d in state.notes()["decisions"]] == [("Bob", ["LIM-2"])]
    assert [(a["owner"], a["text"]) for a in state.notes()["action_items"]] == [("Bob", "Bob owns the write-up.")]
    assert engine.flush() == [] and engine.unresolved() == []


def test_without_the_clarification_the_sources_alone_still_catch_it(pkg, say):
    bob, finalize, correct = run(pkg, [("Bob", say["bob-claim"]), ("Carol", say["carol-finalize"]),
                                       ("Carol", say["carol-correct"])], clarification=None)
    assert by_type(bob, "conflict")[0]["clarification_ids"] == [] and types(finalize) == ["intervene"]
    assert by_type(correct, "decision_draft")[0]["owner"] == "Bob"


@pytest.mark.parametrize("line", BOBS)
def test_speech_to_text_variants_of_bobs_claim(pkg, say, line):
    bob, finalize, correct = run(pkg, [("Bob", line), ("Carol", say["carol-finalize"]), ("Carol", say["carol-correct"])])
    assert types(bob) == ["claim", "conflict"] and bob[1]["has_action"]
    assert types(finalize) == ["intervene"] and by_type(correct, "decision_draft")[0]["owner"] == "Bob"


@pytest.mark.parametrize("line", CAPABILITY_CLAIMS)
def test_other_capability_claims_are_still_caught(pkg, say, line):
    bob, finalize, correct = run(pkg, [("Bob", line), ("Carol", say["carol-finalize"]), ("Carol", say["carol-correct"])])
    assert types(bob) == ["claim", "conflict"] and (bob[1]["claimed"], bob[1]["has_action"]) == ("in_person", True)
    assert types(finalize) == ["intervene"] and by_type(correct, "decision_draft")[0]["owner"] == "Bob"


@pytest.mark.parametrize("line", FINS)
def test_speech_to_text_variants_of_carols_confirmation(pkg, say, line):
    _, finalize, correct = run(pkg, [("Bob", say["bob-claim"]), ("Carol", line), ("Carol", say["carol-correct"])])
    assert types(finalize) == ["intervene"] and by_type(correct, "decision_draft")


@pytest.mark.parametrize("line", CORS)
def test_speech_to_text_variants_of_carols_correction(pkg, say, line):
    _, _, correct = run(pkg, [("Bob", say["bob-claim"]), ("Carol", say["carol-finalize"]), ("Carol", line)])
    drafts = by_type(correct, "decision_draft")
    assert len(drafts) == 1 and drafts[0]["owner"] == "Bob" and by_type(correct, "resolved")[0]["value"] == "online"


@pytest.mark.parametrize("lines", [
    [("Alice", "Remote participants worked well today."), ("Carol", "Great, let's finalize that.")],
    [("Alice", "Should we mention the room mic?"), ("Carol", "Great, let's finalize that.")],
    [("Bob", "I'll write that it is English only."), ("Carol", "Great, let's finalize that.")],
    [("Bob", "I'll write that it works for online meetings, where each participant has their own stream."),
     ("Carol", "Great, let's finalize that.")],
    [("Bob", "In-person meetings would need echo cancellation, so I'll list that as future work."),
     ("Carol", "Great, let's finalize that.")],
    [("Alice", "Does it work in a meeting room?"), ("Carol", "Great, let's finalize that.")],
    # the true sentences of a discussion about which limitations to write down: a mention is not a claim
    [("Bob", "I'll write that in-person meetings are a limitation."), ("Carol", "Great, let's finalize that.")],
    [("Bob", "I'll list in-person meetings under limitations."), ("Carol", "Great, let's finalize that.")],
    [("Bob", "I'll put the meeting room limitation in the write-up."), ("Carol", "Great, let's finalize that.")],
    [("Bob", "We should mention the in-person limitation."), ("Carol", "Great, let's finalize that.")],
    [("Bob", "In-person meetings also need echo cancellation, so I'll list that as future work."),
     ("Carol", "Great, let's finalize that.")],
    [("Bob", "In-person meetings are a limitation too."), ("Carol", "Great, let's finalize that.")],
    [("Bob", "It is fine to list in-person meetings as a limitation."), ("Carol", "Great, let's finalize that.")],
    [("Bob", "The meeting room case is also future work."), ("Carol", "Great, let's finalize that.")],
], ids=lambda lines: lines[0][1][:40])
def test_lines_that_must_stay_silent(pkg, lines):
    flat = [t for events in run(pkg, lines) for t in types(events)]
    assert "intervene" not in flat and "conflict" not in flat
    assert all(e.get("agrees") in (True, None) for events in run(pkg, lines) for e in events)


def test_a_claim_that_agrees_and_a_question_make_no_noise(pkg):
    (agree,) = run(pkg, [("Alice", "Remote participants worked well today.")])
    assert [(e["type"], e["value"], e["agrees"]) for e in agree] == [("claim", "online", True)]
    assert run(pkg, [("Alice", "Should we mention the room mic?")]) == [[]]
    assert run(pkg, [("Bob", "Put it on line three of the write-up.")]) == [[]]  # "on line" is online, "on line three" is not


def test_write_up_is_not_an_action_but_writing_is(pkg):
    actions = pkg.checks.actions
    assert not any(p.search("The write-up is due tonight.") for p in actions)
    for text in ["I'll write up the limitations.", "I'll say that", "we will tell them", "put it in the write-up",
                 "claim that it works", "submit it tonight"]:
        assert any(p.search(text) for p in actions), text
    assert any(p.search("Bob owns the write-up.") for p in pkg.checks.ownership)
