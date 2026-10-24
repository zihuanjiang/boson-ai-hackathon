"""Model-based alignment in the controller (contract v5 §3; doc/alignment_replacement_prompt.md): the controller driven
through tests/test_controller.py's recording ports and manual clock, with a scripted `semantic.FakeAnalyzer` (or a
gated one) standing in for Claude, on tests/test_meeting_flow.py's meeting package **without checks.yaml**.

These tests prove orchestration, validation and the deterministic policy, not semantic accuracy (the guide: injected
fakes for unit and integration tests; `scripts/alignment_eval.py` is the opt-in live evaluation). Each scripted result
is keyed by a fragment of the newest utterance and cites the snapshot's own ids, as the real analyzer must; nothing
here touches the legacy regex engine.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import pytest

from relay import semantic
from relay.config import Settings
from relay.controller import (
    ALIGNMENT_ANSWER_WINDOW, ALIGNMENT_HOLD_S, ALIGNMENT_WRAPUP_WAIT_S, BANNER_WRAPPING, INTERVENING, REPLY_WAIT_S,
    ROOM, ROOM_ECHO_S,
)
from relay.meeting import JOINED_ROLE
from relay.talk import Talk, load_talk
from tests.conftest import synthetic_chunks
from tests.test_clip_flow import SILENCE, voice
from tests.test_controller import Harness, call
from tests.test_meeting_flow import (
    ALICE_QUESTION, ANSWER, BOB_LINE, CAROL_CONFIRM, CAROL_FIX, TONE, creates, outputs, present_first_point, respond,
    room_line, to_discussion, write_meeting,
)

REQ2_QUOTE = "the beta release goes to the beta group only"
DEC1_QUOTE = "the Friday beta is announced to beta users only"
CL1_QUOTE = "Friday is the beta release for the beta group"
TOPIC = "Friday's release and its audience"
CLAIM = "Announce Friday as a production launch to all customers"
QUESTION = "Before we confirm: didn't we establish Friday is the beta? Should the announcement go to beta users only?"
DECISION = "Friday is the beta release, announced to beta users only"
FOLLOW_UP = "Bob announces the beta to beta users"
BOB_FIX = "Actually, scratch that: Friday is the beta, beta users only."


# --- the package, the harness, the scripted analyzer ---------------------------------------------------------------

@pytest.fixture
def meeting_talk(tmp_path: Path) -> Talk:
    root = write_meeting(tmp_path / "meeting")
    (root / "checks.yaml").unlink()  # the model-based check needs no extraction rules (guide, acceptance case 1)
    return load_talk(root)


CLAUDE = Settings(narration="live", alignment="claude")


@pytest.fixture
def mk(meeting_talk: Talk):
    made: list[Harness] = []

    def factory(script: Any = (), *, analyzer: Any = None, settings: Settings | None = None, ready: bool = True,
                no_key: bool = False) -> Harness:
        if analyzer is None and not no_key:
            analyzer = semantic.FakeAnalyzer(script)
        harness = Harness(meeting_talk, synthetic_chunks(meeting_talk), settings or CLAUDE, ready=ready,
                          analyzer=analyzer)
        made.append(harness)
        return harness

    yield factory
    for harness in made:
        harness.check_invariants()


class Gated(semantic.FakeAnalyzer):
    """Each analysis waits for its own release (`await release(n)` for the n-th call, from 0), so a test can land
    lines, a reset or the wrap-up while one is "in flight"."""

    def __init__(self, script: Any = ()) -> None:
        super().__init__(script)
        self.gates: list[asyncio.Event] = []

    async def analyze(self, snapshot: semantic.Snapshot) -> semantic.AnalysisOutcome:
        gate = asyncio.Event()
        self.gates.append(gate)
        await gate.wait()
        return await super().analyze(snapshot)

    async def release(self, n: int) -> None:
        for _ in range(200):
            if len(self.gates) > n:
                break
            await asyncio.sleep(0)
        assert len(self.gates) > n, f"analysis {n} never started"
        self.gates[n].set()
        await spin()


async def spin(rounds: int = 30) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def settle(h: Harness) -> None:
    """Every analysis requested so far has come back and been applied."""
    assert await h.c._analysis.drain(2.0)
    await spin(3)


def script(rules: dict[str, Callable[[semantic.Snapshot], dict]]) -> Callable[[semantic.Snapshot], dict]:
    """The analyzer's scripted answers: the first rule whose fragment is in the newest utterance builds the result
    from the snapshot (so it cites the snapshot's ids); anything else is ordinary discussion (the empty result)."""

    def answer(snapshot: semantic.Snapshot) -> dict:
        newest = snapshot.utterance(snapshot.new_utterance_ids[-1]) if snapshot.new_utterance_ids else None
        for fragment, rule in rules.items():
            if newest is not None and fragment in newest["text"]:
                return rule(snapshot)
        return {}

    return answer


def uid(snapshot: semantic.Snapshot, fragment: str) -> str:
    return next(u["id"] for u in snapshot.utterances if fragment in u["text"])


def open_issue(snapshot: semantic.Snapshot) -> str:
    return snapshot.issues[0]["id"]


def ev(kind: str, cited: str, quote: str) -> dict:
    return {"kind": kind, "id": cited, "quote": quote}


def claim(utterance_id: str, text: str, kind: str = "proposal", agrees: bool | None = False,
          evidence: tuple[str, ...] = ()) -> dict:
    return {"utterance_id": utterance_id, "text": text, "kind": kind, "agrees_with_sources": agrees,
            "evidence_ids": list(evidence)}


def issue(**fields: Any) -> dict:
    base = {"existing_issue_id": None, "issue_type": "evidence_conflict", "topic": TOPIC, "claim": CLAIM,
            "claim_utterance_ids": [], "participant_views": [], "missing_details": [], "evidence": [],
            "material_consequence": "customers would be told the wrong release", "commitment": "proposed",
            "commitment_utterance_ids": [], "recommendation": "silent", "confidence": "high", "explanation": "x",
            "question": QUESTION}
    return {**base, **fields}


def update(issue_id: str, status: str, utterance_ids: list[str], settled: tuple[str, ...] = (), note: str = "") -> dict:
    return {"issue_id": issue_id, "status": status, "utterance_ids": utterance_ids, "settled": list(settled),
            "note": note}


def proposal(**fields: Any) -> dict:
    base = {"decision": DECISION, "status": "confirmed", "owner": None, "owner_utterance_ids": [],
            "agreement_utterance_ids": [], "follow_up": None, "source_ids": [], "supersedes_decision_id": None,
            "resolves_issue_ids": [], "explanation": "y"}
    return {**base, **fields}


def rollout_rules(*, cite_clarification: bool = False, partial: bool = False) -> Callable:
    """The brief's story: Bob's conflicting proposal (silent: nobody confirms yet), Carol's confirmation (clarify),
    Carol's fix (resolved, a confirmed decision owned by Bob)."""

    def bob(s: semantic.Snapshot) -> dict:
        u1 = uid(s, "production launch")
        return {"candidate_claims": [claim(u1, "Friday is a production launch for all customers", evidence=("REQ-2",))],
                "issues": [issue(claim_utterance_ids=[u1],
                                 evidence=[ev("source", "REQ-2", REQ2_QUOTE), ev("source", "DEC-1", DEC1_QUOTE)])]}

    def confirm(s: semantic.Snapshot) -> dict:
        evidence = [ev("source", "REQ-2", REQ2_QUOTE), ev("source", "DEC-1", DEC1_QUOTE)]
        if cite_clarification:
            evidence.append(ev("clarification", "CL-1", CL1_QUOTE))
        missing = ["Friday's audience", "who sends the announcement"] if partial else []
        return {"issues": [issue(existing_issue_id=open_issue(s), claim_utterance_ids=[uid(s, "production launch")],
                                 commitment_utterance_ids=[uid(s, "finalize")], evidence=evidence,
                                 missing_details=missing, commitment="confirming", recommendation="clarify")]}

    def fix(s: semantic.Snapshot) -> dict:
        u3, iid = uid(s, "beta users only"), open_issue(s)
        if partial:
            return {"updates": [update(iid, "partially_resolved", [u3], settled=("Friday's audience",),
                                       note="beta users only")]}
        return {"updates": [update(iid, "resolved", [u3], settled=("Friday's audience",), note="beta users only")],
                "decision_proposals": [proposal(owner="Bob", owner_utterance_ids=[u3], agreement_utterance_ids=[u3],
                                                follow_up=FOLLOW_UP, source_ids=["REQ-2", "DEC-1"],
                                                resolves_issue_ids=[iid])]}

    return script({"production launch": bob, "finalize": confirm, "beta users only": fix})


def kinds(h: Harness) -> list[str]:
    return [msg["event"]["type"] for msg in h.b("alignment")]


def logged(h: Harness, kind: str) -> list[dict]:
    return [data for name, data in h.logs if name == kind]


async def finish_turn(h: Harness) -> None:
    """The facilitator's reply plays out, and any confirm follow-up it needs."""
    await h.drained()
    while h.c.state == INTERVENING and creates(h)[-1].get("metadata", {}).get("relay") == "followup":
        await respond(h, transcript="Noted: Bob owns it.", audio_ms=300)
        await h.drained()


async def voice_line(h: Harness, text: str, speaker: str | None, item: str) -> None:
    """One participant line from the Zoom bot's ears: voice frames, the segmenter's silence, the transcript."""
    for _ in range(3):
        await voice(h, speaker=speaker)
    for _ in range(7):
        await voice(h, SILENCE, speaker=speaker)
    await h.ev("input_audio_buffer.committed", item_id=item)
    await h.ev("conversation.item.input_audio_transcription.completed", item_id=item, transcript=text)


# --- availability ---------------------------------------------------------------------------------------------------

async def test_claude_mode_without_a_key_is_unavailable_and_never_falls_back(mk):
    """Contract v5 §0: no analyzer (no ANTHROPIC_API_KEY) → `unavailable`, the discussion transcribed and recorded,
    nobody checks it; the regex engine never runs in its place."""
    h = mk(no_key=True)
    assert (h.c.alignment_mode, h.c.engine, h.c._analysis) == ("claude", None, None)
    assert h.c.alignment_info() == {"mode": "claude", "model": "claude-opus-5-5", "available": False}
    assert h.c.alignment_status()["last_error"] == "ANTHROPIC_API_KEY missing"
    assert logged(h, "alignment_unavailable")[0]["mode"] == "claude"
    await h.c.on_browser_connected()
    assert h.b("hello")[0]["alignment"] == {"mode": "claude", "model": "claude-opus-5-5", "available": False}
    await to_discussion(h)
    h.reset()
    await room_line(h, "Bob", BOB_LINE)
    await room_line(h, "Carol", CAROL_CONFIRM)
    assert [u.text for u in h.c.mstate.transcript] == [BOB_LINE, CAROL_CONFIRM]
    assert h.b("alignment") == [] and h.c.state == ROOM and "response.create" not in h.realtime.types()
    assert h.c.mstate.to_json()["alignment"]["available"] is False


# --- the three issue kinds -------------------------------------------------------------------------------------------

async def test_an_evidence_conflict_is_asked_at_the_confirmation_and_the_fix_is_saved(mk):
    """Acceptance 1, 6, 9: a conflicting proposal stays silent; the confirmation brings one question with the cited
    passages on screen; the fix resolves the issue and the facilitator's save_decision (matching the validated
    proposal) records the decision with its owner and the utterances that agreed; the wrap-up has no open question."""
    h = mk(rollout_rules())
    assert h.c.meeting.checks is None
    assert h.c.alignment_info() == {"mode": "claude", "model": "fake-analyzer", "available": True}
    await to_discussion(h)
    h.reset()

    await room_line(h, "Bob", BOB_LINE)
    await settle(h)
    snapshot = h.c.analyzer.calls[0]
    assert snapshot.new_utterance_ids == ("U1",) and snapshot.utterances[0]["speaker"] == "Bob"
    assert [p["id"] for p in snapshot.passages] == ["DEC-1", "DEC-2", "REQ-1", "REQ-2", "REQ-3"]
    assert kinds(h) == ["claim", "conflict"]
    chip, conflict = (msg["event"] for msg in h.b("alignment"))
    assert (chip["text"], chip["agrees"], chip["speaker"]) == ("Friday is a production launch for all customers",
                                                              False, "Bob")
    assert (conflict["issue_type"], conflict["policy"], conflict["quote"], conflict["quote_speaker"]) == (
        "evidence_conflict", "silent", BOB_LINE, "Bob")
    assert conflict["source_ids"] == ["REQ-2", "DEC-1"] and conflict["established_label"] == REQ2_QUOTE
    assert h.c.state == ROOM and "response.create" not in h.realtime.types()  # nobody is confirming: silent
    assert (h.c.mstate.issue("IS1").status, h.c.mstate.issue("IS1").policy) == ("pending", "silent")
    assert logged(h, "intervention_policy")[-1]["decision"] == "silent"

    await room_line(h, "Carol", CAROL_CONFIRM)
    await settle(h)
    assert kinds(h)[-2:] == ["conflict", "intervene"]
    intervene = h.b("alignment")[-1]["event"]
    assert (intervene["issue_id"], intervene["question"], intervene["confirm_quote"], intervene["utterance_id"]) == (
        "IS1", QUESTION, CAROL_CONFIRM, "U2")
    lookup = h.b("evidence")[-1]["lookup"]
    assert (lookup["by"], lookup["purpose"]) == ("engine", "intervention")
    assert [result["source_id"] for result in lookup["results"]][:2] == ["REQ-2", "DEC-1"]  # the cited ones first
    request = creates(h)[-1]
    assert request["metadata"]["relay"] == "intervention" and request["tool_choice"] == "none"
    assert f'Ask this, or a close paraphrase: "{QUESTION}"' in request["instructions"]
    assert "a conflict with the documents" in request["instructions"] and "[REQ-2]" in request["instructions"]
    assert h.c.state == INTERVENING and h.c.mstate.issue("IS1").status == "pending"  # asked once the room hears it
    await respond(h, transcript=QUESTION, audio_ms=1500)
    assert h.c.mstate.issue("IS1").status == "asked"
    await h.drained()
    assert h.c.state == ROOM

    await room_line(h, "Carol", CAROL_FIX)
    await settle(h)
    assert kinds(h)[-2:] == ["resolved", "decision_draft"]
    resolved, draft = (msg["event"] for msg in h.b("alignment")[-2:])
    assert (resolved["owner"], resolved["partial"], resolved["follow_up_quote"]) == ("Bob", False, CAROL_FIX)
    assert (draft["by"], draft["silent"], draft["agreement_utterance_ids"], draft["owner"]) == (
        "analysis", False, ["U3"], "Bob")
    assert h.c.mstate.issue("IS1").status == "resolved"
    assert creates(h)[-1]["metadata"]["relay"] == "decision"
    await respond(h, call("save_decision", {"decision": DECISION, "owner": "Bob", "follow_up": FOLLOW_UP,
                                            "source_ids": ["REQ-2", "DEC-1"]}),
                  transcript="Confirmed: Friday is the beta, announced to beta users only, and Bob owns it.",
                  audio_ms=900)
    decision = h.c.mstate.decisions[0]
    assert (decision.decision, decision.owner, decision.by, decision.source_ids) == (
        DECISION, "Bob", "model", ["REQ-2", "DEC-1"])
    assert decision.agreement_utterance_ids == ["U3"] and h.c.mstate.action_items[0].owner == "Bob"
    await finish_turn(h)
    assert h.c.state == ROOM

    await h.key("phase")
    notes = h.last("meeting")["notes"]
    assert notes["decisions"][0]["owner"] == "Bob" and notes["open_questions"] == [] and notes["issues"] == []
    assert creates(h)[-1]["metadata"]["relay"] == "summary"


async def test_a_clarification_from_the_presentation_is_evidence_in_the_discussion(mk):
    """Acceptance 17: the answer to Alice's interruption is recorded (CL-1) and travels in every later snapshot with its
    id, answer, sources and status; the discussion's intervention cites it and its instructions name it."""
    h = mk(rollout_rules(cite_clarification=True))
    await present_first_point(h, transcript="Release four point three ships in two stages, and Friday is",
                              audio_ms=1000)
    await h.typed(ALICE_QUESTION, reply=1, played=600)
    await respond(h, call("lookup_evidence", {"query": "Friday beta release"}), transcript="", audio_ms=0)
    await h.drained(reply=2)
    await respond(h, call("set_presentation_state", {"action": "resume"}), transcript=ANSWER, audio_ms=1200)
    await h.drained(reply=3)
    clarification = h.c.mstate.clarifications[0]
    assert (clarification.id, clarification.answer, clarification.status) == ("CL-1", ANSWER, "answered")

    await h.key("phase")  # the agenda button: the hand-over, then the room
    await respond(h, transcript="That's the plan; over to you.", audio_ms=500)
    await h.drained()
    assert (h.c.state, h.c.agenda_phase) == (ROOM, "discussion")
    await room_line(h, "Bob", BOB_LINE)
    await settle(h)
    carried = h.c.analyzer.calls[-1].clarifications[0]
    assert (carried["id"], carried["answer"], carried["status"]) == ("CL-1", ANSWER, "answered")
    assert carried["source_ids"] == clarification.source_ids

    await room_line(h, "Carol", CAROL_CONFIRM)
    await settle(h)
    intervene = h.b("alignment")[-1]["event"]
    assert intervene["type"] == "intervene" and intervene["clarification_ids"] == ["CL-1"]
    assert "Earlier in this meeting it was clarified (CL-1)" in creates(h)[-1]["instructions"]
    await h.key("phase")
    assert h.last("meeting")["notes"]["clarifications"][0]["id"] == "CL-1"  # survives into the notes


async def test_a_hidden_disagreement_names_both_readings_without_a_document(mk):
    """Acceptance 14: Alice means the beta, Bob production; the group moves to agree. The question shows both
    participants' own words, no document lookup; Alice's answer settles it."""
    alice = "For me Friday means the beta release to the beta group."
    bob = "Right, Friday's release, I'll tell everyone it's production."
    carol = "Great, so we all agree on Friday."
    answer = "The beta, for the beta group only."
    question = "Alice described Friday as the beta and Bob as production: which release do we mean?"

    def agree(s: semantic.Snapshot) -> dict:
        u1, u2, u3 = uid(s, "beta release"), uid(s, "production"), uid(s, "all agree")
        return {"participant_assumptions": [{"speaker": "Bob", "utterance_id": u2, "topic": "Friday's release",
                                             "interpretation": "production for everyone"}],
                "issues": [issue(issue_type="hidden_disagreement", topic="What Friday's release is",
                                 claim="Friday's release", claim_utterance_ids=[u2], commitment_utterance_ids=[u3],
                                 participant_views=[
                                     {"speaker": "Alice", "utterance_id": u1, "quote": "the beta release",
                                      "view": "the beta"},
                                     {"speaker": "Bob", "utterance_id": u2, "quote": "it's production",
                                      "view": "production"}],
                                 commitment="confirming", recommendation="clarify", question=question)]}

    def settles(s: semantic.Snapshot) -> dict:
        u4, iid = uid(s, "beta group only"), open_issue(s)
        return {"updates": [update(iid, "resolved", [u4], note="the beta")],
                "decision_proposals": [proposal(decision="Friday's release is the beta for the beta group",
                                                agreement_utterance_ids=[u4], resolves_issue_ids=[iid])]}

    h = mk(script({"all agree": agree, "beta group only": settles}))
    await to_discussion(h)
    h.reset()
    await room_line(h, "Alice", alice)
    await room_line(h, "Bob", bob)
    await room_line(h, "Carol", carol)
    await settle(h)
    assert kinds(h) == ["assumption", "conflict", "intervene"]
    assert h.b("alignment")[0]["event"]["speaker"] == "Bob"
    intervene = h.b("alignment")[-1]["event"]
    assert intervene["issue_type"] == "hidden_disagreement" and intervene["source_ids"] == []
    assert [(v["speaker"], v["quote"]) for v in intervene["participant_views"]] == [
        ("Alice", "the beta release"), ("Bob", "it's production")]
    assert h.b("evidence") == []  # no document lookup: the card shows the participants' words
    instructions = creates(h)[-1]["instructions"]
    assert 'Alice said "the beta release"' in instructions and 'Bob said "it\'s production"' in instructions
    assert "Name both readings neutrally" in instructions and "a hidden disagreement" in instructions
    await respond(h, transcript=question, audio_ms=1200)
    await h.drained()
    await room_line(h, "Alice", answer)
    await settle(h)
    assert h.c.mstate.issue("IS1").status == "resolved" and kinds(h)[-2:] == ["resolved", "decision_draft"]


async def test_an_unresolved_owner_is_asked_and_an_unanswered_question_stays_open(mk):
    """Acceptance 15 (no answer): the group commits while rollback has no owner. The question is asked without any
    contradicting document; nobody answers within ALIGNMENT_ANSWER_WINDOW lines, so the issue is open and the wrap-up
    keeps it as an open question."""
    commit = "Let's move the status page on Wednesday, agreed?"
    agreed = "Agreed, Wednesday."
    question = "Who owns the rollback if the status page move goes wrong?"

    def ambiguous(s: semantic.Snapshot) -> dict:
        return {"issues": [issue(issue_type="ambiguous_decision", topic="the status page move",
                                 claim="Move the status page on Wednesday", claim_utterance_ids=[uid(s, "status page")],
                                 commitment_utterance_ids=[uid(s, "Agreed, Wednesday")],
                                 missing_details=["who owns the rollback"], commitment="committed",
                                 recommendation="clarify", question=question)]}

    h = mk(script({"Agreed, Wednesday": ambiguous}))
    await to_discussion(h)
    h.reset()
    await room_line(h, "Carol", commit)
    await room_line(h, "Bob", agreed)
    await settle(h)
    intervene = h.b("alignment")[-1]["event"]
    assert (intervene["type"], intervene["issue_type"], intervene["missing_details"]) == (
        "intervene", "ambiguous_decision", ["who owns the rollback"])
    assert h.b("evidence") == [] and intervene["source_ids"] == []  # never a fabricated source
    assert "Not settled yet: who owns the rollback." in creates(h)[-1]["instructions"]
    await respond(h, transcript=question, audio_ms=1000)
    await h.drained()
    assert h.c.mstate.issue("IS1").status == "asked"
    for n in range(ALIGNMENT_ANSWER_WINDOW + 1):
        await room_line(h, "Alice", f"Unrelated point number {n}.")
        await settle(h)
    assert h.c.mstate.issue("IS1").status == "open"
    await h.key("phase")
    assert [q["text"] for q in h.last("meeting")["notes"]["open_questions"]] == [question]
    assert h.c.mstate.decisions == []  # nobody confirmed an owner: nothing invented


async def test_an_assigned_owner_is_recorded_even_when_the_facilitators_save_does_not_match(mk):
    """Acceptance 15 (answered) and the decision check (contract v5 §3): Carol assigns rollback to Bob; the facilitator's
    save_decision must match the validated proposal, so an invented one is rejected and the proposal is saved
    by=analysis with its owner, follow-up (an action item) and provenance."""
    question = "Who owns the rollback if the status page move goes wrong?"
    decision = "Move the status page on Wednesday; Bob owns the rollback"

    def ambiguous(s: semantic.Snapshot) -> dict:
        return {"issues": [issue(issue_type="ambiguous_decision", topic="the status page move",
                                 claim="Move the status page on Wednesday", claim_utterance_ids=[uid(s, "status page")],
                                 commitment_utterance_ids=[uid(s, "Agreed, Wednesday")],
                                 missing_details=["who owns the rollback"], commitment="committed",
                                 recommendation="clarify", question=question)]}

    def assigned(s: semantic.Snapshot) -> dict:
        u, iid = uid(s, "Bob owns the rollback"), open_issue(s)
        return {"updates": [update(iid, "resolved", [u], settled=("who owns the rollback",), note="Bob")],
                "decision_proposals": [proposal(decision=decision, owner="Bob", owner_utterance_ids=[u],
                                                agreement_utterance_ids=[u], follow_up="Bob owns the rollback",
                                                resolves_issue_ids=[iid])]}

    h = mk(script({"Agreed, Wednesday": ambiguous, "Bob owns the rollback": assigned}))
    await to_discussion(h)
    await room_line(h, "Carol", "Let's move the status page on Wednesday, agreed?")
    await room_line(h, "Bob", "Agreed, Wednesday.")
    await settle(h)
    await respond(h, transcript=question, audio_ms=1000)
    await h.drained()
    await room_line(h, "Carol", "Bob owns the rollback.")
    await settle(h)
    assert creates(h)[-1]["metadata"]["relay"] == "decision"
    await respond(h, call("save_decision", {"decision": "Cancel the release and tell all customers", "owner": "Bob"}),
                  transcript="Saved.", audio_ms=400)
    await finish_turn(h)
    rejected = logged(h, "decision_rejected")[-1]
    assert "doesn't match the one the group confirmed" in rejected["why"]
    saved = h.c.mstate.decisions[0]
    assert (saved.decision, saved.owner, saved.by, saved.agreement_utterance_ids) == (decision, "Bob", "analysis", ["U3"])
    assert [(item.owner, item.text) for item in h.c.mstate.action_items] == [("Bob", "Bob owns the rollback")]


# --- silence, invented support, plain decisions ----------------------------------------------------------------------

async def test_invented_citations_and_unsupported_issues_never_reach_speech(mk):
    """Acceptance 7: an unknown passage, a quote that isn't in the cited passage, an issue without a real utterance and
    a "confirmed" decision nobody agreed to are dropped or kept silent, and the reasons are logged."""
    def invented(s: semantic.Snapshot) -> dict:
        return {"issues": [issue(claim_utterance_ids=[uid(s, "production launch")],
                                 commitment_utterance_ids=[uid(s, "finalize")],
                                 evidence=[ev("source", "REQ-9", "Friday is production"),
                                           ev("source", "REQ-2", "Friday is production for everyone")],
                                 commitment="confirming", recommendation="clarify"),
                           issue(claim_utterance_ids=["U99"], commitment="confirming", recommendation="clarify")],
                "decision_proposals": [proposal(agreement_utterance_ids=["U42"])]}

    h = mk(script({"finalize": invented}))
    await to_discussion(h)
    h.reset()
    await room_line(h, "Bob", BOB_LINE)
    await room_line(h, "Carol", CAROL_CONFIRM)
    await settle(h)
    assert kinds(h) == ["conflict"]
    conflict = h.b("alignment")[0]["event"]
    assert conflict["policy"] == "silent" and conflict["evidence"] == [] and conflict["source_ids"] == []
    assert h.c.state == ROOM and "response.create" not in h.realtime.types()
    assert h.c.mstate.decisions == [] and len(h.c.mstate.issues) == 1
    dropped = " | ".join(logged(h, "analysis_result")[-1]["dropped"])
    for reason in ("unknown source REQ-9", "quote not in source REQ-2", "no valid claim or commitment utterance",
                   "confirmed without a valid agreement utterance"):
        assert reason in dropped, reason
    assert "cites no passage" in logged(h, "intervention_policy")[-1]["reason"]
    assert h.c.mstate.issue("IS1").evidence_ok is False
    await h.key("phase")  # review 2026-10-24: nor does it reach the notes at the wrap-up
    assert h.last("meeting")["notes"]["open_questions"] == [] and kinds(h) == ["conflict"]


async def test_a_plain_agreed_decision_is_recorded_silently_and_a_tentative_one_is_only_a_candidate(mk):
    """Acceptance 16: decisions are captured without any conflict and without an interruption; a proposal nobody has
    confirmed is a candidate, never a decision."""
    proposing = "I'll send the beta release notes on Friday."
    agreeing = "Agreed, Bob sends them Friday."
    decision = "Bob sends the beta release notes on Friday"

    def tentative(s: semantic.Snapshot) -> dict:
        u1 = uid(s, "release notes")
        return {"decision_proposals": [proposal(decision=decision, status="proposed", owner="Bob",
                                                owner_utterance_ids=[u1])]}

    def confirmed(s: semantic.Snapshot) -> dict:
        u1, u2 = uid(s, "release notes"), uid(s, "Agreed, Bob")
        return {"decision_proposals": [proposal(decision=decision, owner="Bob", owner_utterance_ids=[u1],
                                                agreement_utterance_ids=[u2], follow_up="Send the beta release notes",
                                                source_ids=["REQ-2"])]}

    h = mk(script({"release notes": tentative, "Agreed, Bob": confirmed}))
    await to_discussion(h)
    h.reset()
    await room_line(h, "Bob", proposing)
    await settle(h)
    assert h.c.mstate.decisions == [] and h.c.mstate.candidate_decisions[0]["status"] == "proposed"
    await room_line(h, "Carol", agreeing)
    await settle(h)
    saved = h.c.mstate.decisions[0]
    assert (saved.decision, saved.owner, saved.by, saved.source_ids) == (decision, "Bob", "analysis", ["REQ-2"])
    assert (saved.agreement_utterance_ids, saved.owner_utterance_ids) == (["U2"], ["U1"])
    assert h.c.mstate.candidate_decisions == []  # the candidate became the decision
    draft = h.b("alignment")[-1]["event"]
    assert (draft["type"], draft["silent"], draft["decision_id"]) == ("decision_draft", True, saved.id)
    assert "response.create" not in h.realtime.types() and h.c.state == ROOM  # nothing was said
    assert h.tool_statuses()[-1][1:] == ("save_decision", "auto")  # disclosed as an automatic action
    assert logged(h, "decision_recorded_silently")[-1]["id"] == saved.id


async def test_a_confirmed_plan_change_is_recorded_with_its_provenance_and_the_documents_unchanged(mk):
    """Acceptance 9 and 13: the group explicitly changes the plan; the decision says so (plan_changed), keeps the
    utterances that changed it, and the source passage stays as it was."""
    change = "We're changing the plan: Friday goes to production for everyone."
    agree = "Agreed, we change the plan, production on Friday."
    decision = "Friday's release becomes production for all customers"

    def changed(s: semantic.Snapshot) -> dict:
        u1, u2 = uid(s, "changing the plan"), uid(s, "we change the plan")
        return {"candidate_claims": [claim(u1, "the plan changes: Friday is production", kind="plan_change",
                                           evidence=("REQ-2",))],
                "decision_proposals": [proposal(decision=decision, agreement_utterance_ids=[u2],
                                                source_ids=["REQ-2"])]}

    h = mk(script({"we change the plan": changed}))
    await to_discussion(h)
    original = h.c.meeting.statement("REQ-2").text
    await room_line(h, "Alice", change)
    await room_line(h, "Carol", agree)
    await settle(h)
    saved = h.c.mstate.decisions[0]
    assert (saved.decision, saved.plan_changed, saved.by) == (decision, True, "analysis")
    assert h.b("alignment")[-1]["event"]["utterance_ids"] == ["U2"]
    assert h.c.meeting.statement("REQ-2").text == original and h.c.state == ROOM


# --- timing: in flight, held, discarded, failed, wrap-up -------------------------------------------------------------

def correction_rules() -> Callable:
    return script({
        "production launch": lambda s: {"issues": [issue(claim_utterance_ids=[uid(s, "production launch")],
                                                         evidence=[ev("source", "REQ-2", REQ2_QUOTE)])]},
        "finalize": lambda s: {"issues": [issue(existing_issue_id=open_issue(s),
                                                claim_utterance_ids=[uid(s, "production launch")],
                                                commitment_utterance_ids=[uid(s, "finalize")],
                                                evidence=[ev("source", "REQ-2", REQ2_QUOTE)],
                                                commitment="confirming", recommendation="clarify")]},
        "scratch that": lambda s: {"updates": [update(open_issue(s), "withdrawn", [uid(s, "scratch that")],
                                                      note="Bob corrected himself")]},
    })


async def test_a_correction_landing_while_the_analysis_runs_prevents_the_obsolete_question(mk):
    """Acceptance 8: the verdict for Carol's confirmation arrives while Bob's correction is still being analysed: the
    intervention is held, then withdrawn by the newer analysis; nothing is said."""
    analyzer = Gated(correction_rules())
    h = mk(analyzer=analyzer)
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await analyzer.release(0)
    await room_line(h, "Carol", CAROL_CONFIRM)  # analysis 1 (revision 2) in flight
    assert h.c.alignment_status()["in_flight"] is True and h.last("state")["alignment"]["in_flight"] is True
    await room_line(h, "Bob", BOB_FIX)  # revision 3 waits for it
    assert h.c.alignment_status()["waiting"] is True
    h.reset()
    await analyzer.release(1)
    assert kinds(h)[-1] == "intervene"  # on screen…
    assert "response.create" not in h.realtime.types() and h.c.state == ROOM  # …but held for the newer analysis
    assert logged(h, "intervention_held")[-1]["newer"] == 3
    await analyzer.release(2)
    newest = analyzer.calls[2]  # rebuilt after the verdict was applied: it knows IS1 and that U3 is the new line
    assert newest.new_utterance_ids == ("U3",) and [i["id"] for i in newest.issues] == ["IS1"]
    assert h.c.mstate.issue("IS1").status == "withdrawn" and kinds(h)[-1] == "withdrawn"
    assert logged(h, "intervention_withdrawn")[-1]["issue_id"] == "IS1"
    await h.clock.advance(ALIGNMENT_HOLD_S + 1)
    assert "response.create" not in h.realtime.types() and h.c.state == ROOM


async def test_a_held_intervention_goes_ahead_after_the_hold(mk):
    """ALIGNMENT_HOLD_S bounds the wait: a newer analysis that doesn't come back in time doesn't silence the
    facilitator; the question is then asked."""
    analyzer = Gated(correction_rules())
    h = mk(analyzer=analyzer)
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await analyzer.release(0)
    await room_line(h, "Carol", CAROL_CONFIRM)
    await room_line(h, "Alice", "Hmm, let me think about Friday.")
    await analyzer.release(1)
    assert h.c.state == ROOM and logged(h, "intervention_held")
    await h.clock.advance(ALIGNMENT_HOLD_S + 0.1)
    assert logged(h, "intervention_hold_expired")[-1]["issue_id"] == "IS1"
    assert h.c.state == INTERVENING and creates(h)[-1]["metadata"]["relay"] == "intervention"
    await analyzer.release(2)  # (ordinary discussion: the question stands)
    await respond(h, transcript=QUESTION, audio_ms=800)
    assert h.c.mstate.issue("IS1").status == "asked"


async def test_a_reset_discards_a_late_result_of_the_old_meeting(mk):
    """Acceptance 11 (restart, late results): the generation changes, the old analysis is dropped when it lands."""
    analyzer = Gated(rollout_rules())
    h = mk(analyzer=analyzer)
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await h.key("reset")
    h.reset()
    await analyzer.release(0)
    assert h.b("alignment") == [] and h.c.mstate.issues == [] and h.c.mstate.transcript == []
    discarded = logged(h, "analysis_discarded")[-1]
    assert (discarded["generation"], discarded["current"]) == (0, 1)
    assert h.c.alignment_status()["in_flight"] is False


async def test_a_failed_analysis_only_shows_in_the_status_and_the_meeting_goes_on(mk):
    """Acceptance 11 (timeout, rate limiting): an error outcome changes nothing but the status; the next line is
    analysed as usual."""
    h = mk([TimeoutError("timeout after 20 s"), {}])
    await to_discussion(h)
    h.reset()
    await room_line(h, "Bob", BOB_LINE)
    await settle(h)
    status = h.c.alignment_status()
    assert (status["last_error"], status["analyses"]) == ("TimeoutError: timeout after 20 s", 1)
    assert h.last("meeting")["alignment"]["last_error"] == "TimeoutError: timeout after 20 s"
    assert logged(h, "analysis_error")[-1]["error"] == "TimeoutError: timeout after 20 s"
    assert h.b("alignment") == [] and h.c.state == ROOM
    await room_line(h, "Carol", CAROL_CONFIRM)
    await settle(h)
    assert (h.c.alignment_status()["last_error"], h.c.alignment_status()["analyses"]) == (None, 2)


async def test_the_wrap_up_waits_for_the_analysis_in_flight_and_keeps_its_issue_open(mk):
    """Contract v5 §3 wrap-up: an analysis in flight is waited for; what it raises is not spoken any more (a silent
    suggestion) but stays an open question in the notes; then the summary."""
    analyzer = Gated(rollout_rules())
    h = mk(analyzer=analyzer)
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await analyzer.release(0)
    await room_line(h, "Carol", CAROL_CONFIRM)  # in flight
    h.reset()
    await h.key("phase")
    assert h.c.banner == BANNER_WRAPPING and h.r("response.create") == []
    await analyzer.release(1)
    assert "intervene" not in kinds(h)
    suggestion = h.b("alignment")[-1]["event"]
    assert (suggestion["type"], suggestion["at_wrapup"], suggestion["issue_id"]) == ("suggestion", True, "IS1")
    assert [q["text"] for q in h.last("meeting")["notes"]["open_questions"]] == [QUESTION]
    assert h.r("response.create")[-1]["response"]["metadata"]["relay"] == "summary"


async def test_the_wrap_up_does_not_wait_forever_for_an_analysis(mk):
    analyzer = Gated(rollout_rules())
    h = mk(analyzer=analyzer)
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await analyzer.release(0)
    await room_line(h, "Carol", CAROL_CONFIRM)
    await h.key("phase")
    await h.clock.advance(ALIGNMENT_WRAPUP_WAIT_S + 0.1)
    assert creates(h)[-1]["metadata"]["relay"] == "summary"
    assert [q["text"] for q in h.last("meeting")["notes"]["open_questions"]] == [QUESTION]  # from Bob's issue
    await analyzer.release(1)  # lands after the wrap-up: recorded, never spoken
    assert "intervene" not in kinds(h)


async def test_an_intervention_never_heard_leaves_its_issue_pending(mk):
    """An intervention that never makes a sound (here: no response at all) doesn't count as asked: the issue stays
    pending and the next analysis decides again; it is not re-queued by itself."""
    h = mk(rollout_rules())
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await room_line(h, "Carol", CAROL_CONFIRM)
    await settle(h)
    assert h.c.state == INTERVENING
    requests = len(creates(h))
    await h.clock.advance(REPLY_WAIT_S + 0.1)
    assert h.c.state == ROOM and h.c.mstate.issue("IS1").status == "pending"
    assert logged(h, "intervention_unheard")[-1]["issue_id"] == "IS1"
    assert len(creates(h)) == requests


async def test_a_partial_correction_keeps_the_rest_open(mk):
    """Acceptance 10: the fix settles the audience but not who sends it; the card says what is settled and what is
    still open, nothing is decided, and the wrap-up keeps the question."""
    h = mk(rollout_rules(partial=True))
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await room_line(h, "Carol", CAROL_CONFIRM)
    await settle(h)
    await respond(h, transcript=QUESTION, audio_ms=900)
    await h.drained()
    await room_line(h, "Carol", CAROL_FIX)
    await settle(h)
    resolved = h.b("alignment")[-1]["event"]
    assert (resolved["type"], resolved["partial"], resolved["settled"], resolved["remaining"]) == (
        "resolved", True, ["Friday's audience"], ["who sends the announcement"])
    assert h.c.mstate.issue("IS1").status == "partially_resolved" and h.c.mstate.decisions == []
    await h.key("phase")
    assert [q["text"] for q in h.last("meeting")["notes"]["open_questions"]] == [QUESTION]


# --- the Zoom bot's live participant stream --------------------------------------------------------------------------

async def test_zoom_voice_lines_without_a_speaker_are_analysed_and_can_be_a_hidden_disagreement(mk):
    """`--input voice` (the Zoom bot): lines arrive as `source: "voice"`, labelled only when Zoom showed the speaker.
    An unlabelled line goes to the analysis with speaker null; the model's guess of its speaker is never used, and a
    labelled line against an unlabelled one can still be asked about."""
    alice = "For me Friday means the beta release to the beta group."
    unknown = "Friday's release is production, I'll tell everyone."
    agreed = "Sounds good, Friday it is."

    def disagree(s: semantic.Snapshot) -> dict:
        u1, u2, u3 = uid(s, "beta release"), uid(s, "is production"), uid(s, "Friday it is")
        return {"issues": [issue(issue_type="hidden_disagreement", topic="What Friday's release is",
                                 claim="Friday's release", claim_utterance_ids=[u2], commitment_utterance_ids=[u3],
                                 participant_views=[
                                     {"speaker": "Alice", "utterance_id": u1, "quote": "the beta release",
                                      "view": "the beta"},
                                     {"speaker": "Bob", "utterance_id": u2, "quote": "is production",
                                      "view": "production"}],
                                 commitment="confirming", recommendation="clarify",
                                 question="Is Friday's release the beta or production?")]}

    h = mk(script({"Friday it is": disagree}), settings=Settings(narration="live", alignment="claude", input="voice"))
    assert h.c.input_mode == "voice"
    await to_discussion(h)
    await voice_line(h, alice, "Alice", "item_a")
    await voice_line(h, unknown, None, "item_b")
    await voice_line(h, agreed, None, "item_c")
    await settle(h)
    seen = h.c.analyzer.calls[-1].utterances
    assert [(u["speaker"], u["source"]) for u in seen] == [("Alice", "voice"), (None, "voice"), (None, "voice")]
    intervene = h.b("alignment")[-1]["event"]
    assert intervene["type"] == "intervene" and intervene["issue_type"] == "hidden_disagreement"
    assert [v["speaker"] for v in intervene["participant_views"]] == ["Alice", None]  # never the model's "Bob"
    instructions = creates(h)[-1]["instructions"]
    assert 'someone said "is production"' in instructions and "Bob" not in instructions.split("They seem")[1]


async def test_a_zoom_name_off_the_agenda_who_spoke_can_own_a_decision_and_one_who_never_spoke_cannot(mk):
    """Real people in Zoom keep their own names: Dana Lee (not on the agenda) spoke, so the analysis sees her in the
    roster and a decision she was assigned is saved with her as its owner (her action item too). Erin never said a
    word in the meeting: an owner "Erin" is not a participant, as for any invented name, and the owner stays unknown."""
    proposing = "I'll send the beta release notes on Friday."
    agreeing = "Agreed, Dana sends them Friday."
    decision = "Dana sends the beta release notes on Friday"

    def confirmed(s: semantic.Snapshot) -> dict:
        u1, u2 = uid(s, "release notes"), uid(s, "Agreed, Dana")
        return {"decision_proposals": [proposal(decision=decision, owner="dana lee", owner_utterance_ids=[u1],
                                                agreement_utterance_ids=[u2], follow_up="Send the beta release notes",
                                                source_ids=["REQ-2"])]}

    def stranger(s: semantic.Snapshot) -> dict:
        u = uid(s, "Erin writes")
        return {"decision_proposals": [proposal(decision="Erin writes the support FAQ", owner="Erin",
                                                owner_utterance_ids=[u], agreement_utterance_ids=[u])]}

    h = mk(script({"Agreed, Dana": confirmed, "Erin writes": stranger}),
           settings=Settings(narration="live", alignment="claude", input="voice"))
    await to_discussion(h)
    h.reset()
    await voice_line(h, proposing, "Dana Lee", "item_dana")
    await voice_line(h, agreeing, "Carol", "item_carol")
    await settle(h)
    people = h.c.analyzer.calls[-1].participants
    assert [p["name"] for p in people] == ["Alice", "Bob", "Carol", "Dana Lee"] and people[-1]["role"] == JOINED_ROLE
    saved = h.c.mstate.decisions[0]
    assert (saved.decision, saved.owner, saved.by) == (decision, "Dana Lee", "analysis")
    assert [(a.owner, a.text) for a in h.c.mstate.action_items] == [("Dana Lee", "Send the beta release notes")]
    await voice_line(h, "Agreed, and Erin writes the support FAQ.", "Carol", "item_erin")
    await settle(h)
    assert "owner 'Erin' is not a participant" in " | ".join(logged(h, "analysis_result")[-1]["dropped"])
    assert all(d.owner != "Erin" for d in h.c.mstate.decisions) and "Erin" not in h.c.mstate.participant_names


# --- review 2026-10-24: regressions ---------------------------------------------------------------------------------

async def test_a_newer_verdict_below_clarify_retires_the_queued_question(mk):
    """A held question whose issue the newer analysis downgrades (suggest) is never spoken."""
    rules = correction_rules()
    downgrade = script({"Hold on": lambda s: {"issues": [issue(existing_issue_id=open_issue(s),
                                                               claim_utterance_ids=[uid(s, "production launch")],
                                                               evidence=[ev("source", "REQ-2", REQ2_QUOTE)],
                                                               commitment="proposed", recommendation="suggest")]}})
    analyzer = Gated(lambda s: downgrade(s) or rules(s))
    h = mk(analyzer=analyzer)
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await analyzer.release(0)
    await room_line(h, "Carol", CAROL_CONFIRM)
    await room_line(h, "Alice", "Hold on, let's not lock that in yet.")
    await analyzer.release(1)
    assert logged(h, "intervention_held")
    await analyzer.release(2)
    assert logged(h, "intervention_withdrawn")[-1]["reason"] == "policy suggest"
    requests = len(creates(h))
    await h.clock.advance(ALIGNMENT_HOLD_S + 1)
    assert len(creates(h)) == requests and h.c.state == ROOM
    assert h.c.mstate.issue("IS1").status == "suggested"


async def test_a_line_still_being_transcribed_holds_the_question_and_a_withdrawal_cuts_it_unheard(mk):
    analyzer = Gated(correction_rules())
    h = mk(analyzer=analyzer)
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await analyzer.release(0)
    await room_line(h, "Carol", CAROL_CONFIRM)  # analysis 1 in flight
    await h.send({"type": "speaker", "name": "Bob"})
    await h.clock.advance(ROOM_ECHO_S + 0.01)
    for _ in range(3):
        await h.send({"type": "mic", "pcm": TONE})
    for _ in range(8):
        await h.mic()  # Bob's correction ends: committed, its transcript still on the way
    assert h.c._room_line_in_flight()
    await analyzer.release(1)
    assert h.c.state == ROOM and logged(h, "intervention_held")[-1]["line_pending"] is True
    await h.clock.advance(ALIGNMENT_HOLD_S + 0.1)  # the transcript is late: the hold runs out, the question starts
    assert h.c.state == INTERVENING
    await h.ev("input_audio_buffer.committed", item_id="item_fix")
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="item_fix", transcript=BOB_FIX)
    await analyzer.release(2)
    assert h.c.mstate.issue("IS1").status == "withdrawn"
    assert logged(h, "intervention_cancelled")[-1]["issue_id"] == "IS1" and h.c.state == ROOM


async def test_outside_the_decision_flow_save_decision_needs_a_confirmed_proposal(mk):
    h = mk()
    await to_discussion(h)
    await h.clock.advance(ROOM_ECHO_S + 0.01)
    for _ in range(3):
        await h.send({"type": "mic", "pcm": TONE})
    await h.key("space")  # someone asks the facilitator directly
    for _ in range(8):
        await h.mic()
    await respond(h, call("save_decision", {"decision": "Launch Friday for all customers", "owner": "Bob"}),
                  transcript="", audio_ms=0)
    assert outputs(h)[-1][1]["error"].startswith("not saved: decisions go into the notes")
    assert h.c.mstate.decisions == []


async def test_the_decision_turn_saves_the_proposal_once_whatever_the_call_says(mk):
    h = mk(rollout_rules())
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await room_line(h, "Carol", CAROL_CONFIRM)
    await settle(h)
    await respond(h, transcript=QUESTION, audio_ms=800)
    await h.drained()
    await room_line(h, "Carol", CAROL_FIX)
    await settle(h)
    await respond(h, call("save_decision", {"decision": "Friday production release announced to all users",
                                            "owner": "Alice", "follow_up": "Alice emails every customer"}),
                  call("save_decision", {"decision": DECISION, "owner": "Bob"}), transcript="Saved.", audio_ms=300)
    assert [(d.decision, d.owner, d.follow_up) for d in h.c.mstate.decisions] == [(DECISION, "Bob", FOLLOW_UP)]
    assert [(a.owner, a.text) for a in h.c.mstate.action_items] == [("Bob", FOLLOW_UP)]
    assert logged(h, "decision_as_proposed") and outputs(h)[-1][1]["note"] == "already saved"


async def test_a_decision_on_its_way_is_not_confirmed_twice(mk):
    rules = rollout_rules()
    again = script({"thanks": lambda s: {"decision_proposals": [proposal(
        decision="Friday is the beta, announced only to beta users", owner="Bob",
        owner_utterance_ids=[uid(s, "thanks")], agreement_utterance_ids=[uid(s, "thanks")])]}})
    analyzer = Gated(lambda s: again(s) or rules(s))
    h = mk(analyzer=analyzer)
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await analyzer.release(0)
    await room_line(h, "Carol", CAROL_CONFIRM)
    await analyzer.release(1)
    await respond(h, transcript=QUESTION, audio_ms=800)
    await h.drained()
    await room_line(h, "Carol", CAROL_FIX)
    await room_line(h, "Bob", "Great, thanks.")
    await analyzer.release(2)  # the fix: the decision turn starts
    assert creates(h)[-1]["metadata"]["relay"] == "decision"
    await analyzer.release(3)  # Bob's line re-reports it, paraphrased
    assert logged(h, "decision_duplicate")[-1]["of"] == "pending"
    assert kinds(h).count("decision_draft") == 1


async def test_a_late_analysis_after_the_wrap_up_closes_what_it_settled(mk):
    analyzer = Gated(rollout_rules())
    h = mk(analyzer=analyzer)
    await to_discussion(h)
    await room_line(h, "Bob", BOB_LINE)
    await analyzer.release(0)
    await room_line(h, "Carol", CAROL_FIX)  # analysis 1 in flight
    await h.key("phase")
    await h.clock.advance(ALIGNMENT_WRAPUP_WAIT_S + 0.1)
    assert [q.text for q in h.c.mstate.open_questions if not q.resolved] == [QUESTION]
    await analyzer.release(1)  # resolves IS1 after the wrap-up
    assert h.c.mstate.issue("IS1").status == "resolved"
    assert [q.text for q in h.c.mstate.open_questions if not q.resolved] == []
