"""relay.alignment: claims, what is established, and the Engine's gate (contract §3.3 + the coordinator's
clarifications of 2026-10-23: hedged questions, group resolution, finditer negation, curly apostrophes).

The scenario tests replay the brief's §3 lines (contract §8) on the inline package of tests/test_meeting.py; the last
test replays them on talks/rollout-review when it exists.
"""

from __future__ import annotations

import json
import re

import pytest

from relay.alignment import (Claim, Engine, Established, clauses, established, extract, normalize, question_flags,
                             statement_values, value_hits)
from relay.meeting import Checks, MeetingState, Slot, load_meeting
from tests.test_meeting import DECISIONS, REQUIREMENTS, ROLLOUT, FakeClock, meeting_package

ALICE = "Wait, I thought Friday was production. Which one is it?"
BOB = "I'll draft the announcement for Friday's production launch to all customers."
CAROL_CONFIRM = "Sounds good. Let's finalize that."
CAROL_RESOLVE = "Yes, beta users only. Bob owns the announcement."
WEDNESDAY = "Production is next Wednesday, right?"
CLARIFIED = "Friday is the beta release; production is next Wednesday."
DRAFT = "Friday's release stage: beta; Friday's announcement audience: beta users"
COMMON = {"id", "type", "slot", "label", "utterance_id", "at_s"}


@pytest.fixture
def package(tmp_path):
    return meeting_package(tmp_path)


@pytest.fixture
def checks(package) -> Checks:
    return package.checks


class Room:
    """A meeting state + engine; `say` adds an utterance and observes it."""

    def __init__(self, package):
        self.package = package
        self.state = MeetingState(package, FakeClock())
        self.engine = Engine(package, self.state)

    def say(self, text: str, speaker: str | None = None) -> list[dict]:
        return self.engine.observe(self.state.add_utterance(text, speaker, "typed"))

    def kinds(self, text: str, speaker: str | None = None) -> list[str]:
        return [event["type"] for event in self.say(text, speaker)]


@pytest.fixture
def room(package) -> Room:
    return Room(package)


def of(events: list[dict], kind: str) -> list[dict]:
    return [event for event in events if event["type"] == kind]


def custom_checks(**values) -> Checks:
    """A one-slot Checks with no context (every clause), from value → [regex]."""
    compiled = {value: tuple(re.compile(p, re.I) for p in patterns) for value, patterns in values.items()}
    return Checks((Slot("stage", "Stage", (), compiled),), (), (), (), 3)


# --- clauses and questions ----------------------------------------------------------------------------------------------

def test_clauses():
    assert clauses("Friday is beta. Is it? Yes! Fine; okay… so beta but production is Wednesday") == [
        "Friday is beta.", "Is it?", "Yes!", "Fine;", "okay…", "so beta", "production is Wednesday"]
    assert clauses("Fine. But production is Wednesday") == ["Fine.", "But production is Wednesday"]
    assert clauses("Release 4.3 ships on Friday.") == ["Release 4.3 ships on Friday."]  # no split inside numbers
    assert clauses("  ") == [] and clauses("") == []
    assert clauses("Beta, BUT production") == ["Beta,", "production"]


def test_normalize_straightens_quotes():
    assert normalize("I’ll  draft “Friday’s”\nlaunch") == "I'll draft \"Friday's\" launch"


@pytest.mark.parametrize("text,flags", [
    ("Is Friday beta?", [True]),
    ("Friday is beta?", [True]),
    ("So, is Friday beta", [True]),  # a filler, then a question word
    ("Friday is beta.", [False]),
    ("Who owns the announcement", [True]),
    (ALICE, [True, True]),  # the hedge counts: the same utterance asks a question
    ("Wait, I thought Friday was production.", [False]),  # a hedge alone is not a question
    ("I'll draft the production announcement for Friday. Is that okay?", [False, True]),
    ("Friday was production, wasn't it. Right?", [True, True]),
    ("I assumed Friday is production. Is it?", [True, True]),
    ("Friday is production, or is it. Hmm?", [True, True]),
    ("We said beta. Didn't we?", [False, True]),
])
def test_question_flags(text, flags):
    assert question_flags(clauses(text)) == flags


# --- extraction -------------------------------------------------------------------------------------------------------

def test_alice_yields_only_a_question_claim(checks):
    assert extract(ALICE, checks) == [Claim("friday_stage", "production", "Wait, I thought Friday was production.",
                                            False, True)]


def test_bob_yields_two_claims(checks):
    assert extract(BOB, checks) == [Claim("friday_stage", "production", BOB, False, False),
                                    Claim("friday_audience", "all_customers", BOB, False, False)]


def test_curly_apostrophes(checks):
    curly = BOB.replace("'", "’")
    assert extract(curly, checks) == extract(BOB, checks)


@pytest.mark.parametrize("text", [WEDNESDAY, "Production launch to all customers.", "The beta group is ready.",
                                  "Is Friday beta or production?",  # two values in one clause: ambiguous
                                  "Friday is beta or production."])
def test_no_claims(checks, text):
    assert [c for c in extract(text, checks) if not c.negated] == []


def test_splits_at_but_and_semicolons(checks):
    assert extract("Friday is beta but production is Wednesday", checks) == [
        Claim("friday_stage", "beta", "Friday is beta", False, False)]
    assert [(c.slot, c.value) for c in extract(CLARIFIED, checks)] == [("friday_stage", "beta")]


@pytest.mark.parametrize("text,value,negated", [
    ("Friday is beta, not production.", "production", True),
    ("Friday is beta, not production.", "beta", False),
    ("Friday isn't production.", "production", True),
    ("Friday is never production.", "production", True),
    ("No production on Friday.", "production", True),
    ("Not production on Friday.", "production", True),
    ("On Friday we ship beta instead of production.", "production", True),
    ("On Friday we ship beta instead of the production.", "production", True),
    ("Friday goes to beta users rather than all customers.", "all_customers", True),
    ("Friday won't go to all customers.", "all_customers", True),
    ("No, Friday is production.", "production", False),  # a clause-initial "No," is an interjection
    ("There is no reason for Friday to be production.", "production", False),  # more than three words away
    ("Friday: not now, production.", "production", False),  # never across a comma
])
def test_negation(checks, text, value, negated):
    claims = {c.value: c for c in extract(text, checks)}
    assert claims[value].negated is negated, claims


def test_negated_claims_do_not_make_a_clause_ambiguous(checks):
    claims = extract("Friday is beta, not production.", checks)
    assert [(c.value, c.negated) for c in claims] == [("beta", False), ("production", True)]


def test_value_hits_use_each_match_start():
    # a leading group: the value starts at "full", so "not full production" is negated
    slot = custom_checks(prod=[r"\bfull production\b"], beta=[r"\bbeta\b"]).slots[0]
    assert value_hits("It is not full production", slot) == {"prod": True}
    # several matches: asserted when any one is not negated
    slot = custom_checks(prod=[r"\bproduction\b"], beta=[r"\bbeta\b"]).slots[0]
    assert value_hits("Not production, production it is", slot) == {"prod": False}
    assert value_hits("Not production and never production", slot) == {"prod": True}


def test_lookaround_guards_in_value_patterns():
    # like the demo's checks.yaml: production followed by Wednesday (within eight words) does not count
    checks = custom_checks(production=[r"\bproduction\b(?!(?:\W+\w+){0,8}?\W+wednesday)"], beta=[r"\bbeta\b"])
    assert [(c.value, c.negated) for c in extract("Beta on Friday and production next Wednesday.", checks)] == [
        ("beta", False)]
    assert [(c.value, c.negated) for c in extract("Beta on Friday and production too.", checks)] == []  # ambiguous


def test_empty_context_matches_every_clause():
    checks = custom_checks(production=[r"\bproduction\b"], beta=[r"\bbeta\b"])
    assert extract("Production it is.", checks) == [Claim("stage", "production", "Production it is.", False, False)]


def test_extract_without_checks():
    assert extract(BOB, None) == []  # type: ignore[arg-type]


# --- established ------------------------------------------------------------------------------------------------------

def test_established_from_statements(package):
    assert statement_values(package) == {"friday_stage": {"beta": ["REQ-1"]},
                                         "friday_audience": {"beta_users": ["REQ-1"]}}
    assert established(package, None) == {
        "friday_stage": Established("friday_stage", "beta", ["REQ-1"]),
        "friday_audience": Established("friday_audience", "beta_users", ["REQ-1"])}
    assert established(None, None) == {} and statement_values(None) == {}


def test_statements_that_disagree_leave_the_slot_unset(tmp_path):
    requirements = REQUIREMENTS + "- [REQ-9] On Friday production goes live.\n"
    package = meeting_package(tmp_path, sources={"requirements.md": requirements, "decisions.md": DECISIONS})
    assert statement_values(package)["friday_stage"] == {"beta": ["REQ-1"], "production": ["REQ-9"]}
    assert set(established(package, None)) == {"friday_audience"}


def test_a_clarification_adds_its_id(package):
    state = MeetingState(package, FakeClock())
    state.add_clarification(ALICE, CLARIFIED, ["REQ-1"], "auto")
    result = established(package, state)
    assert result["friday_stage"] == Established("friday_stage", "beta", ["REQ-1"], ["CL-1"], [])
    assert result["friday_audience"].clarification_ids == []  # the answer said nothing about the audience


def test_the_question_of_a_clarification_is_not_used(package):
    state = MeetingState(package, FakeClock())
    state.add_clarification("Friday is production, isn't it?", "No.", [])
    assert established(package, state)["friday_stage"].value == "beta"


def test_a_clarification_overrides_statements_and_the_latest_wins(package):
    state = MeetingState(package, FakeClock())
    state.add_clarification("q", "Friday is production now.", ["REQ-2", "NOPE-1"])
    result = established(package, state)["friday_stage"]
    # no statement says production: the clarification's own (known) sources are cited
    assert result == Established("friday_stage", "production", ["REQ-2"], ["CL-1"], [])
    state.add_clarification("q", "Friday is the beta after all.", [])
    assert established(package, state)["friday_stage"] == Established("friday_stage", "beta", ["REQ-1"], ["CL-2"], [])
    state.add_clarification("q", "Friday stays beta.", [])
    assert established(package, state)["friday_stage"].clarification_ids == ["CL-2", "CL-3"]


def test_decisions_override_clarifications(package):
    state = MeetingState(package, FakeClock())
    state.add_decision("Friday's release stage: production instead of beta", "Bob", "", [])
    state.add_clarification("q", "Friday is beta.", [])  # later in time, but a weaker kind
    result = established(package, state)["friday_stage"]
    assert (result.value, result.decision_ids, result.clarification_ids, result.source_ids) == (
        "production", ["D1"], [], [])


def test_an_ambiguous_item_sets_nothing(package):
    state = MeetingState(package, FakeClock())
    state.add_clarification("q", "Friday is beta or production.", [])
    assert established(package, state)["friday_stage"] == Established("friday_stage", "beta", ["REQ-1"])


def test_the_draft_decision_establishes_both_slots(package):
    state = MeetingState(package, FakeClock())
    state.add_decision(DRAFT, "Bob", "", ["REQ-1"])
    result = established(package, state)
    assert (result["friday_stage"].value, result["friday_stage"].decision_ids) == ("beta", ["D1"])
    assert (result["friday_audience"].value, result["friday_audience"].decision_ids) == ("beta_users", ["D1"])


def test_no_checks_nothing_established(tmp_path):
    package = meeting_package(tmp_path, checks=None)
    assert established(package, MeetingState(package, FakeClock())) == {}


# --- the brief's scenario (contract §8) -----------------------------------------------------------------------------------

def test_the_brief_scenario(package):
    room = Room(package)
    state = room.state
    # the interruption in the presentation: a question claim only, nothing to show or raise
    assert room.say(ALICE, "Alice") == []
    state.add_clarification(ALICE, CLARIFIED, ["REQ-1"], "auto")
    state.set_phase("discussion")

    events = room.say(BOB, "Bob")
    assert [e["type"] for e in events] == ["claim", "conflict", "claim", "conflict"]
    assert [e["agrees"] for e in of(events, "claim")] == [False, False]
    stage, audience = of(events, "conflict")
    assert {k: stage[k] for k in ("slot", "label", "claimed", "claimed_label", "established", "established_label",
                                  "source_ids", "clarification_ids", "decision_ids", "has_action", "sentence",
                                  "repeat", "utterance_id")} == {
        "slot": "friday_stage", "label": "Friday's release stage", "claimed": "production",
        "claimed_label": "production", "established": "beta", "established_label": "beta", "source_ids": ["REQ-1"],
        "clarification_ids": ["CL-1"], "decision_ids": [], "has_action": True, "sentence": BOB, "repeat": False,
        "utterance_id": "U2"}
    assert (audience["slot"], audience["claimed_label"], audience["established_label"], audience["has_action"],
            audience["clarification_ids"]) == ("friday_audience", "all customers", "beta users", True, [])

    events = room.say(CAROL_CONFIRM, "Carol")
    assert [e["type"] for e in events] == ["intervene"]  # once, for both conflicts
    intervene = events[0]
    assert intervene["conflict_id"] == stage["id"] and intervene["conflict_ids"] == [stage["id"], audience["id"]]
    assert (intervene["slot"], intervene["claimed"], intervene["established"]) == ("friday_stage", "production", "beta")
    assert intervene["source_ids"] == ["REQ-1"] and intervene["clarification_ids"] == ["CL-1"]
    assert intervene["quote"] == BOB and intervene["quote_speaker"] == "Bob" and intervene["quote_utterance_id"] == "U2"
    assert intervene["confirm_quote"] == CAROL_CONFIRM and intervene["utterance_id"] == "U3"
    assert [r["slot"] for r in intervene["related"]] == ["friday_audience"]
    assert intervene["related"][0]["claimed"] == "all_customers" and intervene["related"][0]["conflict_id"] == \
        audience["id"]
    assert all(word in intervene["query"] for word in ("release stage", "production", "beta", "all customers"))

    events = room.say(CAROL_RESOLVE, "Carol")
    assert [e["type"] for e in events] == ["resolved", "resolved", "decision_draft"]
    resolved_stage, resolved_audience, draft = events
    assert {k: resolved_stage[k] for k in ("slot", "conflict_id", "intervene_id", "value", "value_label",
                                           "plan_changed", "owner", "follow_up_quote", "utterance_ids")} == {
        "slot": "friday_stage", "conflict_id": stage["id"], "intervene_id": intervene["id"], "value": "beta",
        "value_label": "beta", "plan_changed": False, "owner": "Bob", "follow_up_quote": CAROL_RESOLVE,
        "utterance_ids": ["U2", "U4"]}
    assert (resolved_audience["value"], resolved_audience["owner"]) == ("beta_users", "Bob")
    assert {k: draft[k] for k in ("decision", "owner", "follow_up", "source_ids", "clarification_ids", "slots",
                                  "conflict_ids", "plan_changed", "supersedes", "utterance_ids")} == {
        "decision": DRAFT, "owner": "Bob", "follow_up": "Bob owns the announcement.", "source_ids": ["REQ-1"],
        "clarification_ids": ["CL-1"], "slots": ["friday_stage", "friday_audience"],
        "conflict_ids": [stage["id"], audience["id"]], "plan_changed": False, "supersedes": None,
        "utterance_ids": ["U2", "U4"]}

    # the controller saves it; the decision then backs both values
    decision = state.add_decision(draft["decision"], draft["owner"], draft["follow_up"], draft["source_ids"], "model")
    assert established(package, state)["friday_audience"].decision_ids == [decision.id]

    assert room.say(WEDNESDAY, "Alice") == []  # no conflict: it is about Wednesday, and a question
    assert room.engine.unresolved() == []
    assert [e["id"] for e in state.alerts] == [f"AL{n}" for n in range(1, len(state.alerts) + 1)]
    assert all(COMMON <= set(event) for event in state.alerts)
    json.dumps(state.alerts)


def test_the_scenario_is_deterministic(package):
    def run() -> list[dict]:
        room = Room(package)
        for speaker, text in (("Alice", ALICE), ("Bob", BOB), ("Carol", CAROL_CONFIRM), ("Carol", CAROL_RESOLVE)):
            room.say(text, speaker)
        return room.state.alerts

    assert run() == run()


def test_a_hedge_without_a_question_is_a_claim(room):
    assert room.kinds("Wait, I thought Friday was production.") == ["claim", "conflict"]


def test_an_action_claim_followed_by_a_question(room):
    events = room.say("I'll draft the production announcement for Friday. Is that okay?", "Bob")
    assert [e["type"] for e in events] == ["claim", "conflict"] and events[1]["has_action"] is True
    assert room.kinds("Sounds good.") == ["intervene"]


def test_curly_apostrophes_in_the_engine(room):
    events = room.say(BOB.replace("'", "’"), "Bob")
    assert [e["has_action"] for e in of(events, "conflict")] == [True, True]  # \bi'll\b matched "I’ll"
    assert room.kinds("Sounds good. Let’s finalize that.") == ["intervene"]


# --- the gate -----------------------------------------------------------------------------------------------------------

def test_a_repeated_conflict_is_not_raised_twice(room):
    first = of(room.say(BOB, "Bob"), "conflict")
    assert room.kinds(CAROL_CONFIRM) == ["intervene"]
    again = room.say("So I'll send the production launch announcement to all customers on Friday.", "Bob")
    repeats = of(again, "conflict")
    assert [c["repeat"] for c in repeats] == [True, True]
    assert [c["raised_id"] for c in repeats] == [c["id"] for c in first]
    assert room.kinds("Agreed.") == []
    assert len(of(room.state.alerts, "intervene")) == 1


def test_a_pending_conflict_said_twice_is_raised_once(room):
    first = of(room.say("Friday is production."), "conflict")[0]
    assert first["has_action"] is False
    refreshed = of(room.say(BOB, "Bob"), "conflict")
    assert [c["repeat"] for c in refreshed] == [False, False]
    [intervene] = room.say(CAROL_CONFIRM)
    assert intervene["conflict_id"] == refreshed[0]["id"] != first["id"]  # the latest sentence is quoted
    assert intervene["quote"] == BOB and len(intervene["conflict_ids"]) == 2


def test_a_resolved_conflict_can_come_up_again(room):
    room.say(BOB, "Bob")
    room.say(CAROL_CONFIRM)
    draft = room.say(CAROL_RESOLVE)[-1]
    room.state.add_decision(draft["decision"], draft["owner"], draft["follow_up"], draft["source_ids"])
    events = room.say("Actually, I'll launch production to all customers on Friday.", "Bob")
    conflicts = of(events, "conflict")
    assert [c["repeat"] for c in conflicts] == [False, False] and conflicts[0]["decision_ids"] == ["D1"]
    assert room.kinds("Agreed.") == ["intervene"]


def test_a_conflict_without_action_or_confirmation_is_a_suggestion_after_the_window(room):
    conflict = of(room.say("Friday is production."), "conflict")[0]
    assert conflict["has_action"] is False
    assert room.kinds("Sounds good.") == []  # a confirmation, but nothing consequential: stay silent
    assert room.kinds("What about the rollback?") == []
    assert room.kinds("Carol decides on rollbacks.") == []  # still inside the window (three utterances)
    [suggestion] = room.say("Okay.")
    assert suggestion["type"] == "suggestion" and suggestion["conflict_id"] == conflict["id"]
    assert (suggestion["utterance_id"], suggestion["observed_utterance_id"]) == ("U1", "U5")
    assert (suggestion["claimed"], suggestion["established"], suggestion["source_ids"]) == ("production", "beta",
                                                                                            ["REQ-1"])
    assert suggestion["quote"] == "Friday is production." and suggestion["has_action"] is False
    assert room.kinds("Fine.") == []  # once
    room.say("Friday is production.")
    for text in ("a", "b", "c", "d"):
        assert room.kinds(text) == []  # the same suggestion is never shown twice
    [open_item] = room.engine.unresolved()
    assert open_item["status"] == "expired" and open_item["slot"] == "friday_stage"
    assert open_item["text"] == ("Friday's release stage: beta (REQ-1) or production, as said in the discussion "
                                 "(“Friday is production.”)?")


def test_a_confirmation_with_an_action_opens_the_gate(room):
    room.say("Friday is production.")
    assert room.kinds("Agreed, let's do it.") == ["intervene"]


def test_the_window_edge(package):
    room = Room(package)
    room.say(BOB, "Bob")
    room.say("Hmm.")
    room.say("Okay.")
    assert room.kinds(CAROL_CONFIRM) == ["intervene"]  # the third utterance after the conflict: still pending
    late = Room(package)
    late.say(BOB, "Bob")
    for text in ("Hmm.", "Okay.", "Right."):
        late.say(text)
    assert late.kinds(CAROL_CONFIRM) == ["suggestion", "suggestion"]  # too late: silent suggestions, no intervene


def test_a_confirmation_in_the_same_utterance(room):
    events = room.say("I'll send the production launch announcement to all customers on Friday. Agreed.", "Bob")
    assert [e["type"] for e in events] == ["claim", "conflict", "claim", "conflict", "intervene"]


def test_a_question_does_not_confirm(room):
    room.say(BOB, "Bob")
    assert room.kinds("Should we confirm that?") == []
    assert room.kinds("Sounds good.") == ["intervene"]


def test_no_conflict_without_evidence(tmp_path):
    room = Room(meeting_package(tmp_path, sources={"requirements.md": "# Plan\n- [REQ-2] Production on Wednesday.\n"}))
    events = room.say(BOB, "Bob")
    assert [(e["type"], e["agrees"]) for e in events] == [("claim", None), ("claim", None)]
    assert room.kinds(CAROL_CONFIRM) == []


def test_without_checks_the_engine_is_silent(tmp_path):
    package = meeting_package(tmp_path, checks=None)
    room = Room(package)
    assert room.say(BOB) == [] and room.engine.unresolved() == [] and room.engine.flush() == []
    state = MeetingState(None, FakeClock())
    assert Engine(None, state).observe(state.add_utterance(BOB, None)) == []


# --- resolution -----------------------------------------------------------------------------------------------------------

def raised(package) -> Room:
    room = Room(package)
    room.say(BOB, "Bob")
    assert room.kinds(CAROL_CONFIRM, "Carol") == ["intervene"]
    return room


def test_a_plan_change(package):
    room = raised(package)
    events = room.say("We moved production to Friday, it's changed.", "Carol")
    assert [e["type"] for e in events] == ["claim", "resolved", "decision_draft"]
    claim, resolved, draft = events
    assert claim["agrees"] is True  # the answer itself is the new plan
    assert (resolved["slot"], resolved["value"], resolved["plan_changed"]) == ("friday_stage", "production", True)
    assert draft["decision"] == "Friday's release stage: production instead of beta"
    assert (draft["plan_changed"], draft["source_ids"], draft["slots"], draft["supersedes"]) == (
        True, [], ["friday_stage"], None)
    # the audience was not answered: still open
    assert [(item["slot"], item["status"]) for item in room.engine.unresolved()] == [("friday_audience", "asked")]
    # the new plan holds before and after the decision is saved
    assert [(e["type"], e.get("agrees")) for e in room.say("Friday is production.")] == [("claim", True)]
    room.state.add_decision(draft["decision"], "", "", draft["source_ids"])
    assert established(package, room.state)["friday_stage"] == Established("friday_stage", "production", [], [], ["D1"])
    conflicts = of(room.say("Friday is beta."), "conflict")
    assert conflicts[0]["established"] == "production" and conflicts[0]["decision_ids"] == ["D1"]


def test_a_plan_change_supersedes_the_earlier_decision(package):
    room = Room(package)
    room.state.add_decision("Friday's release stage: beta", "", "", ["REQ-1"])
    room.say(BOB, "Bob")
    room.say(CAROL_CONFIRM)
    draft = room.say("It has changed: we moved Friday to production.")[-1]
    assert draft["type"] == "decision_draft" and draft["supersedes"] == "D1" and draft["plan_changed"] is True


def test_one_answer_settles_everything_one_intervention_asked(package):
    room = raised(package)
    # names only the audience value (no "beta" word): the stage conflict raised with it resolves too
    events = room.say("Right, just the forty customers. Bob owns it.", "Carol")
    assert [(e["type"], e.get("value")) for e in events] == [("resolved", "beta"), ("resolved", "beta_users"),
                                                             ("decision_draft", None)]
    assert events[-1]["decision"] == DRAFT and events[-1]["owner"] == "Bob"


def test_an_answer_that_keeps_a_raised_claim_does_not_settle_it(package):
    room = raised(package)
    events = room.say("Yes, beta users only, but the production launch stays.", "Carol")
    assert [(e["type"], e["slot"]) for e in events] == [("resolved", "friday_audience"),
                                                        ("decision_draft", "friday_audience")]
    assert events[-1]["decision"] == "Friday's announcement audience: beta users"
    assert [item["slot"] for item in room.engine.unresolved()] == ["friday_stage"]


@pytest.mark.parametrize("answer", ["Yes.", "Is it beta users only?", "Yes, production.", "Hmm, let me think.",
                                    "Beta or production, who knows?"])
def test_answers_that_do_not_resolve(package, answer):
    room = raised(package)
    assert [e for e in room.say(answer) if e["type"] in ("resolved", "decision_draft")] == []
    assert {item["status"] for item in room.engine.unresolved()} == {"asked"}


def test_a_no_interjection_still_resolves(package):
    room = raised(package)
    assert room.kinds("No, beta users only. Bob owns it.") == ["resolved", "resolved", "decision_draft"]


def test_the_confirming_utterance_itself_never_resolves(room):
    room.say(BOB, "Bob")
    assert room.kinds("Sounds good, yes, beta users only.") == ["intervene"]


def test_an_unanswered_intervention_expires(package):
    room = raised(package)  # intervene at utterance 2; resolve window = max(4, 2 × 3) = 6
    for n in range(6):
        assert room.kinds(f"Something else {n}.") == []
    assert room.kinds(CAROL_RESOLVE) == []  # too late
    assert {item["status"] for item in room.engine.unresolved()} == {"unanswered"}
    again = of(room.say(BOB, "Bob"), "conflict")
    assert [c["repeat"] for c in again] == [True, True]  # asked once, not again
    assert room.kinds("Agreed.") == []


@pytest.mark.parametrize("answer,speaker,bob_speaker,owner", [
    (CAROL_RESOLVE, "Carol", "Bob", "Bob"),  # a participant + an ownership pattern
    ("Yes, beta users only. Carol will send it, not Bob.", "Alice", "Bob", "Carol"),  # the first name named
    ("Yes, beta users only. I'll send it.", "Carol", "Bob", "Carol"),  # "I'll" + the speaker
    ("Yes, beta users only. I’ll send it.", "Carol", "Bob", "Carol"),
    ("Yes, beta users only.", "Carol", "Bob", "Bob"),  # else whoever said "I'll draft…" in the conflict
    ("Yes, beta users only.", "Carol", None, None),
    ("Yes, beta users only. Dave owns it.", "Carol", None, None),  # Dave is not a participant
    ("Yes, beta users only. I'll send it.", "Room", None, None),  # no speaker label
])
def test_the_owner(package, answer, speaker, bob_speaker, owner):
    room = Room(package)
    room.say(BOB, bob_speaker)
    room.say(CAROL_CONFIRM, "Carol")
    events = room.say(answer, speaker)
    assert [e["owner"] for e in events if e["type"] in ("resolved", "decision_draft")] == [owner] * 3


@pytest.mark.parametrize("answer,speaker,follow_up", [
    (CAROL_RESOLVE, "Carol", "Bob owns the announcement."),  # the sentence that gave the owner the task
    ("Yes, beta users only. I'll send it.", "Carol", "I'll send it."),
    ("Yes, beta users only.", "Carol", "Yes, beta users only."),  # no such sentence: the whole answer
])
def test_the_drafts_follow_up_is_the_owners_task_not_the_whole_answer(package, answer, speaker, follow_up):
    room = Room(package)
    room.say(BOB, "Bob")
    room.say(CAROL_CONFIRM, "Carol")
    events = room.say(answer, speaker)
    draft = next(e for e in events if e["type"] == "decision_draft")
    assert draft["follow_up"] == follow_up
    assert all(e["follow_up_quote"] == answer for e in events if e["type"] == "resolved")  # (the card's quote)


def test_public_resolve(package):
    room = raised(package)
    events = room.engine.resolve("friday_stage", "beta", ["U9"])
    assert [(e["type"], e["slot"], e["value"], e["plan_changed"], e["utterance_id"]) for e in events] == [
        ("resolved", "friday_stage", "beta", False, "U9")]
    assert events[0] in room.state.alerts
    assert [item["slot"] for item in room.engine.unresolved()] == ["friday_audience"]
    assert room.engine.resolve("nope", "beta") == [] and room.engine.resolve("friday_stage", "gamma") == []
    pending = Room(package)
    pending.say("Friday is production.")
    [event] = pending.engine.resolve("friday_stage", "production", ["U1"])
    assert event["plan_changed"] is True and event["follow_up_quote"] == "Friday is production."
    assert [(e["type"], e["agrees"]) for e in pending.say("Friday is production.")] == [("claim", True)]


def test_flush_turns_pending_conflicts_into_suggestions(room):
    room.say("Friday is production.")
    [suggestion] = room.engine.flush()
    assert suggestion["type"] == "suggestion" and suggestion["observed_utterance_id"] is None
    assert room.engine.flush() == [] and suggestion in room.state.alerts


def test_reset(room):
    room.say(BOB, "Bob")
    room.say(CAROL_CONFIRM)
    room.engine.reset()
    room.state.reset()
    events = room.say(BOB, "Bob")
    assert events[0]["id"] == "AL1" and [c["repeat"] for c in of(events, "conflict")] == [False, False]
    assert room.engine.unresolved()[0]["status"] == "pending"


def test_events_are_json_with_the_common_fields(package):
    room = raised(package)
    room.say(CAROL_RESOLVE, "Carol")
    room.say("Friday is production.")
    for text in ("a", "b", "c", "d"):
        room.say(text)
    kinds = {event["type"] for event in room.state.alerts}
    assert kinds == {"claim", "conflict", "intervene", "resolved", "decision_draft", "suggestion"}
    for event in room.state.alerts:
        assert COMMON <= set(event), event
        assert json.loads(json.dumps(event)) == event


# --- the demo package ---------------------------------------------------------------------------------------------------

@pytest.mark.skipif(not (ROLLOUT / "agenda.yaml").is_file(), reason="talks/rollout-review not written yet")
def test_the_brief_scenario_on_the_demo_package():
    package = load_meeting(ROLLOUT)
    room = Room(package)
    assert room.say(ALICE, "Alice") == []
    room.state.add_clarification(ALICE, "The approved rollout plan says beta on Friday and production next "
                                        "Wednesday.", ["REQ-2"], "auto")
    room.state.set_phase("discussion")
    events = room.say(BOB, "Bob")
    conflicts = of(events, "conflict")
    assert [(c["slot"], c["claimed"], c["established"], c["has_action"]) for c in conflicts] == [
        ("friday_stage", "production", "beta", True), ("friday_audience", "all_customers", "beta_users", True)]
    assert "CL-1" in conflicts[0]["clarification_ids"] and conflicts[0]["source_ids"]
    assert room.kinds(CAROL_CONFIRM, "Carol") == ["intervene"]
    events = room.say(CAROL_RESOLVE, "Carol")
    assert [e["type"] for e in events] == ["resolved", "resolved", "decision_draft"]
    assert events[-1]["owner"] == "Bob" and not events[-1]["plan_changed"]
    assert of(room.say(WEDNESDAY, "Alice"), "conflict") == []
    assert room.engine.unresolved() == []


@pytest.mark.skipif(not (ROLLOUT / "agenda.yaml").is_file(), reason="talks/rollout-review not written yet")
def test_a_plan_change_on_the_demo_package():
    room = raised(load_meeting(ROLLOUT))
    events = room.say("We moved production to Friday, it's changed.", "Carol")
    assert [(e["type"], e.get("plan_changed")) for e in events if e["type"] == "resolved"] == [("resolved", True)]
