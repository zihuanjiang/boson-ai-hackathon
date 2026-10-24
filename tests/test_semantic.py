"""relay.semantic (contract v5 §1): the strict schema, snapshot building and bounding, quote normalisation,
reconciliation of a model result against the snapshot, the deterministic policy, the fake analyzer, the runner's
coalescing, and the Claude adapter against a stubbed SDK client. No network anywhere."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from relay import semantic
from relay.config import Settings
from relay.meeting import MeetingState, load_meeting
from tests.test_meeting import FakeClock, make_meeting

BOB = "I'll announce Friday's production launch to all customers."
CAROL = "Great, let's finalize that."
FIX = "Yes, beta users only. Bob owns the announcement."


@pytest.fixture
def package(tmp_path):
    return load_meeting(make_meeting(tmp_path, checks=None))  # no checks.yaml: model-based alignment needs none


@pytest.fixture
def state(package):
    s = MeetingState(package, FakeClock())
    s.set_phase("discussion")
    return s


def snap(package, state, *new_ids, **kwargs) -> semantic.Snapshot:
    return semantic.build_snapshot(package, state, generation=1, new_utterance_ids=list(new_ids), **kwargs)


def result(**fields) -> semantic.AnalysisResult:
    base = {"schema_version": 1, "candidate_claims": [], "participant_assumptions": [], "issues": [], "updates": [],
            "decision_proposals": []}
    return semantic.AnalysisResult.model_validate({**base, **fields})


def issue(**fields) -> dict:
    base = {"existing_issue_id": None, "issue_type": "evidence_conflict", "topic": "Friday's release stage",
            "claim": "Friday is a production launch", "claim_utterance_ids": [], "participant_views": [],
            "missing_details": [], "evidence": [], "material_consequence": None, "commitment": "confirming",
            "commitment_utterance_ids": [], "recommendation": "clarify", "confidence": "high",
            "explanation": "x", "question": "Is Friday beta or production?"}
    return {**base, **fields}


def proposal(**fields) -> dict:
    base = {"decision": "Friday is a beta release announced to beta users only", "status": "confirmed",
            "owner": "Bob", "owner_utterance_ids": [], "agreement_utterance_ids": [], "follow_up": "Bob sends the notes",
            "source_ids": ["REQ-1"], "supersedes_decision_id": None, "resolves_issue_ids": [], "explanation": ""}
    return {**base, **fields}


# --- schema -----------------------------------------------------------------------------------------------------------

def test_schema_is_strict_and_self_contained():
    schema = semantic.result_schema()
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in schema and "title" not in text
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
    item = schema["properties"]["issues"]["items"]
    assert item["additionalProperties"] is False and "question" in item["required"]
    assert item["properties"]["issue_type"]["enum"] == list(semantic.ISSUE_TYPES)
    assert schema["properties"]["decision_proposals"]["items"]["properties"]["status"]["enum"] == [
        "proposed", "nearing", "confirmed", "rejected", "superseded"]
    assert semantic.result_schema() is not schema  # a copy each time: a caller can't corrupt the cache


def test_empty_result_validates_and_the_schema_version_is_fixed():
    assert semantic.empty_result().schema_version == semantic.SCHEMA_VERSION == 1


# --- snapshot ---------------------------------------------------------------------------------------------------------

def test_snapshot_holds_every_passage_the_discussion_and_the_records(package, state):
    state.add_clarification("Is Friday beta?", "Friday is the beta release.", ["REQ-1"], "auto")
    u1 = state.add_utterance(BOB, "Bob", "clip")
    s = snap(package, state, u1.id)
    assert s.generation == 1 and s.revision == 1 and s.phase == "discussion" and s.new_utterance_ids == (u1.id,)
    assert [p["id"] for p in s.passages] == [st.id for st in package.statements]
    req1 = s.passage("REQ-1")
    assert req1["location"].startswith("sources/requirements.md:") and req1["title"] == "Rollout plan v3"
    assert req1["doc"] == "requirements" and req1["text"] == package.statement("REQ-1").text
    assert s.utterance(u1.id)["text"] == BOB and s.utterance("U99") is None and s.passage("REQ-1")["text"]
    assert s.clarifications[0]["id"] == "CL-1" and s.clarifications[0]["status"] == "answered"
    assert s.discussion_prompt == "What goes out on Friday, and who sends it?"
    assert [p["name"] for p in s.participants] == ["Alice", "Bob", "Carol"]
    blocks = s.system_blocks()
    assert blocks[0]["text"] == semantic.INSTRUCTION and blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert "[REQ-1]" in blocks[1]["text"] and "# Rollout plan v3 (requirements)" in blocks[1]["text"]
    body = s.user_body()
    assert list(body)[-2:] == ["utterances", "new_utterance_ids"]  # the transcript last: the stable parts first
    assert s.user_text().startswith("MEETING SNAPSHOT (JSON):")


def test_snapshot_lists_open_issues_with_their_utterances_and_skips_closed_ones(package, state):
    u1 = state.add_utterance(BOB, "Bob", "clip")
    u2 = state.add_utterance(CAROL, "Carol", "clip")
    open_issue = state.add_issue(issue_type="evidence_conflict", topic="t", claim="c", claim_utterance_ids=[u1.id],
                                 participant_views=[], missing_details=["who"], evidence=[], material_consequence=None,
                                 commitment="confirming", confidence="high", recommendation="clarify", question="q?",
                                 revision=2, commitment_utterance_ids=[u2.id], status="asked")
    closed = state.add_issue(issue_type="ambiguous_decision", topic="t2", claim=None, claim_utterance_ids=[u1.id],
                             participant_views=[], missing_details=[], evidence=[], material_consequence=None,
                             commitment="none", confidence="low", recommendation="silent", question=None, revision=2,
                             status="resolved")
    s = snap(package, state)
    assert [i["id"] for i in s.issues] == [open_issue.id] and closed.id not in {i["id"] for i in s.issues}
    assert s.issues[0]["utterance_ids"] == [u1.id, u2.id] and s.issues[0]["missing_details"] == ["who"]


def test_snapshot_bounding_drops_unreferenced_utterances_oldest_first_then_passages(package, state):
    u1 = state.add_utterance(BOB, "Bob", "clip")
    state.add_issue(issue_type="evidence_conflict", topic="t", claim="c", claim_utterance_ids=[u1.id],
                    participant_views=[], missing_details=[], evidence=[], material_consequence=None,
                    commitment="proposed", confidence="high", recommendation="suggest", question=None, revision=1)
    for n in range(300):
        state.add_utterance(f"Filler {n}: lunch, weather, parking, nothing about the release at all.", "Carol")
    newest = state.transcript[-1]
    full = snap(package, state, newest.id)
    size = len(full.documents_text()) + len(full.user_text())
    assert size <= semantic.MAX_SNAPSHOT_CHARS and full.utterances_dropped > 0 and not full.passages_truncated
    assert full.utterance(u1.id) is not None and full.utterance(newest.id) is not None
    assert full.utterance(state.transcript[1].id) is None  # the oldest filler went first
    assert full.user_body()["utterances_dropped_before_these"] == full.utterances_dropped
    tight = snap(package, state, max_chars=1000)  # the five-statement fixture: only the documents are left to cut
    assert tight.passages_truncated and semantic.MIN_PASSAGES_KEPT <= len(tight.passages) < len(package.statements)
    order = [st.id for st in package.statements]
    kept = [p["id"] for p in tight.passages]
    assert kept == sorted(kept, key=order.index)  # document order
    assert "others exist" in tight.documents_text()


def test_snapshot_without_a_package(state):
    bare = MeetingState(None, FakeClock())
    bare.set_phase("discussion")
    bare.add_utterance("hello", None)
    s = semantic.build_snapshot(None, bare, generation=3, new_utterance_ids=["U1"])
    assert s.passages == () and s.participants == () and "(none supplied)" in s.documents_text()


# --- quotes -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("quote,text,expected", [
    ("Friday’s production launch", BOB, True),
    ("  friday's PRODUCTION launch to all customers.  ", BOB, True),
    ('"Great, let\'s finalize that"', CAROL, True),
    ("beta users only", FIX, True),
    ("production launch to beta users", BOB, False),
    ("", BOB, False),
    ("a", "a", False),
])
def test_quote_matches_after_normalisation(quote, text, expected):
    assert semantic.quote_matches(quote, text) is expected


def test_normalize_quote():
    assert semantic.normalize_quote("  “Hello,   World!”  ") == "hello, world"
    assert semantic.normalize_quote(None) == ""


# --- reconcile --------------------------------------------------------------------------------------------------------

def test_reconcile_keeps_vouched_evidence_and_drops_the_rest(package, state):
    state.add_clarification("Is Friday beta?", "Friday is the beta release to the beta group.", ["REQ-1"], "auto")
    u1 = state.add_utterance(BOB, "Bob", "clip")
    s = snap(package, state, u1.id)
    req1 = package.statement("REQ-1").text
    rec = semantic.reconcile(result(issues=[issue(
        claim_utterance_ids=[u1.id, "U9"], commitment_utterance_ids=[u1.id],
        evidence=[{"kind": "source", "id": "REQ-1", "quote": req1[:40]},
                  {"kind": "source", "id": "REQ-1", "quote": "something it never says"},
                  {"kind": "source", "id": "REQ-77", "quote": req1[:40]},
                  {"kind": "clarification", "id": "CL-1", "quote": "the beta release"},
                  {"kind": "decision", "id": "D1", "quote": "x"},
                  {"kind": "utterance", "id": u1.id, "quote": "production launch"}])]), s)
    assert len(rec.issues) == 1
    valid = rec.issues[0]
    assert valid.claim_utterance_ids == [u1.id] and valid.commitment_utterance_ids == [u1.id]
    assert [e["id"] for e in valid.evidence] == ["REQ-1", "CL-1", u1.id]
    assert valid.evidence[2]["speaker"] == "Bob" and valid.evidence[0]["text"] == req1
    assert valid.evidence_ok and "cited passage" in valid.evidence_reason
    assert valid.evidence_ids("source") == ["REQ-1"] and valid.evidence_ids("clarification", "decision") == ["CL-1"]
    joined = " | ".join(valid.dropped)
    assert "unknown utterance U9" in joined and "quote not in source REQ-1" in joined
    assert "unknown source REQ-77" in joined and "unknown decision D1" in joined
    assert rec.dropped == valid.dropped


def test_reconcile_drops_an_issue_without_any_valid_utterance(package, state):
    s = snap(package, state)
    rec = semantic.reconcile(result(issues=[issue(claim_utterance_ids=["U5"])]), s)
    assert rec.issues == [] and any("no valid claim or commitment utterance: dropped" in d for d in rec.dropped)


def test_reconcile_existing_issue_ids(package, state):
    u1 = state.add_utterance(BOB, "Bob", "clip")
    known = state.add_issue(issue_type="evidence_conflict", topic="t", claim="c", claim_utterance_ids=[u1.id],
                            participant_views=[], missing_details=[], evidence=[], material_consequence=None,
                            commitment="proposed", confidence="high", recommendation="suggest", question=None,
                            revision=1)
    s = snap(package, state)
    rec = semantic.reconcile(result(issues=[
        issue(existing_issue_id=known.id, claim_utterance_ids=[u1.id],
              evidence=[{"kind": "source", "id": "REQ-1", "quote": "beta group"}]),
        issue(existing_issue_id="IS42", claim_utterance_ids=[u1.id],
              evidence=[{"kind": "source", "id": "REQ-1", "quote": "beta group"}])]), s)
    assert [v.existing_id for v in rec.issues] == [known.id, None]
    assert any("unknown existing issue IS42" in d for d in rec.dropped)


def test_reconcile_evidence_requirements_per_category(package, state):
    u1 = state.add_utterance("Friday is our beta release to the beta group.", "Alice", "clip")
    u2 = state.add_utterance("Friday is production for everyone, let's announce it.", "Bob", "clip")
    u3 = state.add_utterance("Agreed on Friday.", "Carol", "clip")
    u4 = state.add_utterance("I'll send it to everyone then.", "Bob", "clip")
    s = snap(package, state, u4.id)
    rec = semantic.reconcile(result(issues=[
        issue(issue_type="evidence_conflict", claim_utterance_ids=[u2.id],
              evidence=[{"kind": "utterance", "id": u2.id, "quote": "Friday is production"}]),  # no passage: not ok
        issue(issue_type="hidden_disagreement", claim_utterance_ids=[u2.id], commitment_utterance_ids=[u3.id],
              participant_views=[{"speaker": "Alice", "utterance_id": u1.id, "quote": "beta release", "view": "beta"},
                                 {"speaker": "Bob", "utterance_id": u2.id, "quote": "Friday is production",
                                  "view": "production"}]),
        issue(issue_type="hidden_disagreement", claim_utterance_ids=[u2.id],  # two lines, both Bob's
              participant_views=[{"speaker": "Bob", "utterance_id": u2.id, "quote": "production", "view": "p"},
                                 {"speaker": "Bob", "utterance_id": u4.id, "quote": "everyone", "view": "p"}]),
        issue(issue_type="hidden_disagreement", claim_utterance_ids=[u2.id],
              participant_views=[{"speaker": "Bob", "utterance_id": u2.id, "quote": "production", "view": "p"}]),
        issue(issue_type="ambiguous_decision", claim_utterance_ids=[u3.id], missing_details=["who owns rollback"],
              evidence=[]),
        issue(issue_type="ambiguous_decision", claim_utterance_ids=[u3.id], missing_details=["  "]),
    ]), s)
    oks = [(v.issue_type, v.evidence_ok) for v in rec.issues]
    assert oks == [("evidence_conflict", False), ("hidden_disagreement", True), ("hidden_disagreement", False),
                   ("hidden_disagreement", False), ("ambiguous_decision", True), ("ambiguous_decision", False)]
    assert "cites no passage" in rec.issues[0].evidence_reason
    assert "two participants' own words" in rec.issues[1].evidence_reason
    assert "all from one participant" in rec.issues[2].evidence_reason
    assert "fewer than two utterances" in rec.issues[3].evidence_reason
    assert "names no missing detail" in rec.issues[5].evidence_reason
    views = rec.issues[1].participant_views
    assert [v["speaker"] for v in views] == ["Alice", "Bob"]  # the utterance's speaker wins


def test_unlabelled_lines_keep_an_unknown_speaker_and_can_still_disagree(package, state):
    """A Zoom call labels speakers best effort (contract v5, the Zoom integration): an unlabelled line is nobody's in
    particular. The model may not name its speaker, and two lines, one labelled and one not, can still be a hidden
    disagreement; two lines by the same labelled person cannot."""
    u1 = state.add_utterance("Friday is our beta release to the beta group.", "Alice", "voice")
    u2 = state.add_utterance("Friday is production for everyone, let's announce it.", None, "voice")
    u3 = state.add_utterance("Sounds good, Friday it is.", None, "voice")
    s = snap(package, state, u3.id)
    views = [{"speaker": "Alice", "utterance_id": u1.id, "quote": "beta release", "view": "beta"},
             {"speaker": "Bob", "utterance_id": u2.id, "quote": "Friday is production", "view": "production"}]
    rec = semantic.reconcile(result(
        issues=[issue(issue_type="hidden_disagreement", claim_utterance_ids=[u2.id], commitment_utterance_ids=[u3.id],
                      participant_views=views),
                issue(issue_type="hidden_disagreement", claim_utterance_ids=[u2.id],
                      participant_views=[{**views[1], "speaker": None},
                                         {"speaker": None, "utterance_id": u3.id, "quote": "Friday it is",
                                          "view": "agrees"}])],
        participant_assumptions=[{"speaker": "Bob", "utterance_id": u2.id, "topic": "Friday", "interpretation": "p"}]),
        s)
    first, second = rec.issues
    assert [v["speaker"] for v in first.participant_views] == ["Alice", None]  # never the model's "Bob"
    assert first.evidence_ok and "partly unlabelled" in first.evidence_reason
    assert second.evidence_ok  # two unlabelled lines: may be two people (the question is neutral either way)
    assert rec.assumptions[0].speaker is None
    assert semantic.decide_policy(first, s)[0] == "clarify"


def test_reconcile_view_quotes_and_speakers(package, state):
    u1 = state.add_utterance(BOB, "Bob", "clip")
    s = snap(package, state)
    rec = semantic.reconcile(result(issues=[issue(
        issue_type="hidden_disagreement", claim_utterance_ids=[u1.id],
        participant_views=[{"speaker": "Carol", "utterance_id": u1.id, "quote": "production launch", "view": "x"},
                           {"speaker": "Alice", "utterance_id": u1.id, "quote": "not in there", "view": "y"},
                           {"speaker": "Alice", "utterance_id": "U8", "quote": "production", "view": "z"}])]), s)
    valid = rec.issues[0]
    assert valid.participant_views == [{"speaker": "Bob", "utterance_id": u1.id, "quote": "production launch",
                                        "view": "x"}]
    assert any("view quote not in" in d for d in valid.dropped) and any("unknown utterance U8" in d for d in valid.dropped)


def test_reconcile_updates(package, state):
    u1 = state.add_utterance(BOB, "Bob", "clip")
    known = state.add_issue(issue_type="evidence_conflict", topic="t", claim="c", claim_utterance_ids=[u1.id],
                            participant_views=[], missing_details=[], evidence=[], material_consequence=None,
                            commitment="confirming", confidence="high", recommendation="clarify", question="q?",
                            revision=1, status="asked")
    s = snap(package, state)
    rec = semantic.reconcile(result(updates=[
        {"issue_id": known.id, "status": "resolved", "utterance_ids": [u1.id, "U7"], "settled": [], "note": "ok"},
        {"issue_id": known.id, "status": "withdrawn", "utterance_ids": ["U7"], "settled": [], "note": "no"},
        {"issue_id": known.id, "status": "still_open", "utterance_ids": [], "settled": [], "note": ""},
        {"issue_id": "IS9", "status": "resolved", "utterance_ids": [u1.id], "settled": [], "note": ""}]), s)
    assert [(u.issue_id, u.status, u.utterance_ids) for u in rec.updates] == [
        (known.id, "resolved", [u1.id]), (known.id, "still_open", [])]
    assert any("withdrawn: no valid utterance" in d for d in rec.dropped) and any("unknown issue IS9" in d for d in rec.dropped)


def test_reconcile_proposals(package, state):
    u1 = state.add_utterance(BOB, "Bob", "clip")
    u2 = state.add_utterance(FIX, "Carol", "clip")
    d1 = state.add_decision("Old plan", "Alice", "", ["REQ-1"])
    s = snap(package, state, u2.id)
    rec = semantic.reconcile(result(decision_proposals=[
        proposal(agreement_utterance_ids=[u2.id], owner="bob", owner_utterance_ids=[u2.id, "U9"],
                 source_ids=["REQ-1", "NOPE-1", "DEC-1"], supersedes_decision_id=d1.id, resolves_issue_ids=["IS1"]),
        proposal(status="confirmed", agreement_utterance_ids=[]),
        proposal(status="proposed", agreement_utterance_ids=[], owner="Zed", supersedes_decision_id="D9"),
        proposal(decision="   ", agreement_utterance_ids=[u2.id]),
    ]), s)
    assert len(rec.proposals) == 2
    first, second = rec.proposals
    assert first.owner == "Bob" and not first.owner_unsupported and first.agreement_utterance_ids == [u2.id]
    assert first.owner_utterance_ids == [u2.id] and first.source_ids == ["REQ-1", "DEC-1"]
    assert first.supersedes == d1.id and first.resolves_issue_ids == []
    assert second.owner is None and second.owner_unsupported and second.supersedes is None
    joined = " | ".join(rec.dropped)
    assert "confirmed without a valid agreement utterance" in joined and "empty decision" in joined
    assert "unknown source NOPE-1" in joined and "owner 'Zed' is not a participant" in joined


def test_reconcile_claims_and_assumptions(package, state):
    u1 = state.add_utterance(BOB, "Bob", "clip")
    s = snap(package, state)
    rec = semantic.reconcile(result(
        candidate_claims=[{"utterance_id": u1.id, "text": "t", "kind": "proposal", "agrees_with_sources": False,
                           "evidence_ids": ["REQ-1", "ZZZ-1"]},
                          {"utterance_id": "U4", "text": "t", "kind": "other", "agrees_with_sources": None,
                           "evidence_ids": []}],
        participant_assumptions=[{"speaker": "Alice", "utterance_id": u1.id, "topic": "x", "interpretation": "y"},
                                 {"speaker": "Bob", "utterance_id": "U4", "topic": "x", "interpretation": "y"}]), s)
    assert [c.evidence_ids for c in rec.claims] == [["REQ-1"]]
    assert [a.speaker for a in rec.assumptions] == ["Bob"]  # attributed to the utterance's speaker
    assert sum("unknown utterance U4" in d for d in rec.dropped) == 2


# --- policy -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("recommendation,confidence,commitment,question,evidence_ok,expected", [
    ("clarify", "high", "confirming", "Which one?", True, "clarify"),
    ("clarify", "medium", "committed", "Which one?", True, "clarify"),
    ("clarify", "low", "confirming", "Which one?", True, "suggest"),
    ("clarify", "high", "proposed", "Which one?", True, "suggest"),
    ("clarify", "high", "nearing", "Which one?", True, "suggest"),
    ("clarify", "high", "confirming", "", True, "suggest"),
    ("clarify", "high", "confirming", "Which one?", False, "silent"),
    ("suggest", "high", "proposed", None, True, "suggest"),
    ("suggest", "high", "none", None, False, "silent"),
    ("silent", "high", "confirming", "Which one?", True, "silent"),
    ("silent", "high", "nearing", None, True, "silent"),
])
def test_decide_policy(package, state, recommendation, confidence, commitment, question, evidence_ok, expected):
    u1 = state.add_utterance(BOB, "Bob", "clip")
    s = snap(package, state)
    evidence = [{"kind": "source", "id": "REQ-1", "quote": "beta group"}] if evidence_ok else []
    rec = semantic.reconcile(result(issues=[issue(claim_utterance_ids=[u1.id], recommendation=recommendation,
                                                  confidence=confidence, commitment=commitment, question=question,
                                                  evidence=evidence)]), s)
    decision, reason = semantic.decide_policy(rec.issues[0], s)
    assert decision == expected and reason


# --- fake analyzer and the runner ---------------------------------------------------------------------------------------

async def test_fake_analyzer_scripts_results_dicts_exceptions_and_runs_dry(package, state):
    s = snap(package, state)
    fake = semantic.FakeAnalyzer([result(), {"issues": []}, RuntimeError("boom"), {"issues": "not a list"}])
    outcomes = [await fake.analyze(s) for _ in range(5)]
    assert [o.result is not None for o in outcomes] == [True, True, False, False, True]
    assert outcomes[2].error == "RuntimeError: boom" and outcomes[3].error.startswith("invalid output")
    assert outcomes[4].result == semantic.empty_result() and len(fake.calls) == 5 and fake.results == outcomes
    by_fn = semantic.FakeAnalyzer(lambda snapshot: {"issues": []} if snapshot.revision == 0 else ValueError("nope"))
    assert (await by_fn.analyze(s)).result is not None
    state.add_utterance("x", None)
    assert (await by_fn.analyze(snap(package, state))).error == "ValueError: nope"


async def test_runner_coalesces_requests_and_delivers_in_order(package, state):
    gate = asyncio.Event()
    fake = semantic.FakeAnalyzer(gate=gate)
    seen: list[int] = []

    async def on_result(snapshot, outcome):
        seen.append(snapshot.revision)
        assert outcome.result is not None

    runner = semantic.AnalysisRunner(fake, on_result)
    first = snap(package, state)
    state.add_utterance("a", None)
    second = snap(package, state)
    state.add_utterance("b", None)
    third = snap(package, state)
    runner.request(first)
    runner.request(second)
    runner.request(third)
    assert runner.in_flight and runner.waiting and runner.busy and runner.current_revision == 2
    gate.set()
    assert await runner.drain(5.0)
    assert seen == [0, 2] and runner.analyses == 2 and not runner.busy and runner.current_revision is None
    assert runner.status() == {"in_flight": False, "waiting": False, "analyses": 2, "last_latency_ms": fake.results[-1].latency_ms,
                               "last_error": None}


async def test_runner_cancel_drops_only_what_waits(package, state):
    gate = asyncio.Event()
    fake = semantic.FakeAnalyzer(gate=gate)
    seen = []

    async def on_result(snapshot, outcome):
        seen.append(snapshot.revision)

    runner = semantic.AnalysisRunner(fake, on_result)
    runner.request(snap(package, state))
    state.add_utterance("a", None)
    runner.request(snap(package, state))
    runner.cancel()
    assert runner.in_flight and not runner.waiting
    gate.set()
    assert await runner.drain(5.0) and seen == [0]


async def test_runner_survives_a_raising_analyzer_and_a_raising_callback(package, state):
    class Broken:
        model = "broken"

        async def analyze(self, snapshot):
            raise RuntimeError("socket died")

    logged = []
    delivered = []

    async def on_result(snapshot, outcome):
        delivered.append(outcome.error)
        raise ValueError("callback bug")

    runner = semantic.AnalysisRunner(Broken(), on_result, log=lambda kind, data: logged.append((kind, data)))
    runner.request(snap(package, state))
    assert await runner.drain(5.0)
    assert delivered == ["analyzer raised RuntimeError: socket died"] and runner.last_error == delivered[0]
    assert logged and logged[0][0] == "analysis_callback_error" and "callback bug" in logged[0][1]["error"]
    assert not runner.busy  # the failure didn't leave it stuck


async def test_runner_drain_times_out_while_an_analysis_hangs(package, state):
    gate = asyncio.Event()
    runner = semantic.AnalysisRunner(semantic.FakeAnalyzer(gate=gate), lambda s, o: asyncio.sleep(0))
    runner.request(snap(package, state))
    assert not await runner.drain(0.05)
    gate.set()
    assert await runner.drain(5.0)


# --- the Claude adapter against a stub client ------------------------------------------------------------------------------

class _StubSDK:
    class APITimeoutError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    class APIStatusError(Exception):
        def __init__(self, status_code: int, message: str):
            super().__init__(message)
            self.status_code = status_code
            self.message = message

    class RateLimitError(Exception):
        pass


class StubClient:
    def __init__(self, respond):
        self.respond = respond
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.respond(kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def response(text: str | None, *, stop="end_turn", request_id="req_1", details=None):
    content = [SimpleNamespace(type="text", text=text)] if text is not None else [SimpleNamespace(type="thinking")]
    usage = SimpleNamespace(input_tokens=1200, output_tokens=80, cache_read_input_tokens=900,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason=stop, content=content, usage=usage, _request_id=request_id,
                           stop_details=details)


def analyzer(client) -> semantic.ClaudeAnalyzer:
    settings = Settings(anthropic_api_key="sk-ant-test", alignment_model="claude-opus-5-5", alignment_effort="low",
                        alignment_timeout_s=7.5, alignment_max_tokens=1234)
    created = semantic.ClaudeAnalyzer(settings, client=client)
    created._anthropic = _StubSDK  # the exception classes the adapter catches
    return created


def wire_issue(**fields) -> dict:
    base = {"existing_issue_id": None, "issue_type": "evidence_conflict", "topic": "Friday's release stage",
            "claim": "Friday is a production launch", "claim_utterance_ids": [], "evidence": [], "missing_details": [],
            "material_consequence": None, "commitment": "confirming", "commitment_utterance_ids": [],
            "confidence": "high", "recommendation": "clarify", "question": "Is Friday beta or production?"}
    return {**base, **fields}


def wire(**fields) -> str:
    return json.dumps({"candidate_claims": [], "issues": [], "updates": [], "decision_proposals": [], **fields})


def count_fields(node) -> int:
    if isinstance(node, dict):
        own = len(node["properties"]) if isinstance(node.get("properties"), dict) else 0
        return own + sum(count_fields(value) for value in node.values())
    if isinstance(node, list):
        return sum(count_fields(item) for item in node)
    return 0


def test_the_wire_schema_stays_within_what_the_api_compiles():
    """Live 2026-10-24: the API refuses a structured-output schema whose compiled grammar is too large. The full
    AnalysisResult (52 fields) and the wire form plus two explanation fields (43) were refused on opus, sonnet and
    haiku 5.5; the wire form (41 fields in 6 object types) compiles on all three. A new field must replace one."""
    schema = semantic.wire_schema()
    assert count_fields(schema) <= 41 and json.dumps(schema).count('"properties"') <= 6
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
    assert "$ref" not in json.dumps(schema) and "explanation" not in json.dumps(schema)


def test_the_wire_result_becomes_the_analysis_result():
    """Assumptions travel as claims of kind assumption, participant views as utterance evidence with a view; the
    speakers are left to reconcile (the transcript's), the schema version is the application's."""
    text = wire(
        candidate_claims=[{"utterance_id": "U2", "kind": "proposal", "topic": "Friday", "text": "production launch",
                           "agrees_with_sources": False, "evidence_ids": ["REQ-1"]},
                          {"utterance_id": "U2", "kind": "assumption", "topic": "Friday's release",
                           "text": "production for everyone", "agrees_with_sources": None, "evidence_ids": []}],
        issues=[wire_issue(issue_type="hidden_disagreement", evidence=[
            {"kind": "utterance", "id": "U1", "quote": "beta release", "view": "the beta"},
            {"kind": "utterance", "id": "U2", "quote": "production", "view": "production"},
            {"kind": "utterance", "id": "U3", "quote": "agreed", "view": "  "},
            {"kind": "source", "id": "REQ-1", "quote": "two stages", "view": ""}])],
        updates=[{"issue_id": "IS1", "status": "resolved", "utterance_ids": ["U4"], "settled": [], "note": "beta"}],
        decision_proposals=[{"decision": "Friday is beta", "status": "confirmed", "owner": "Bob",
                             "owner_utterance_ids": ["U4"], "agreement_utterance_ids": ["U4"], "follow_up": None,
                             "source_ids": ["REQ-1"], "supersedes_decision_id": None, "resolves_issue_ids": ["IS1"]}])
    converted = semantic.WireResult.model_validate_json(text).to_result()
    assert converted.schema_version == semantic.SCHEMA_VERSION
    assert [(c.kind, c.text) for c in converted.candidate_claims] == [("proposal", "production launch")]
    (assumption,) = converted.participant_assumptions
    assert (assumption.speaker, assumption.topic, assumption.interpretation) == (None, "Friday's release",
                                                                                 "production for everyone")
    (issue_,) = converted.issues
    assert [(v.utterance_id, v.view, v.speaker) for v in issue_.participant_views] == [("U1", "the beta", None),
                                                                                       ("U2", "production", None)]
    assert [(e.kind, e.id) for e in issue_.evidence] == [("utterance", "U3"), ("source", "REQ-1")]
    assert converted.updates[0].status == "resolved" and converted.decision_proposals[0].owner == "Bob"


async def test_claude_analyzer_sends_the_structured_request_and_parses_the_result(package, state):
    u1 = state.add_utterance(BOB, "Bob", "clip")
    s = snap(package, state, u1.id)
    payload = wire(issues=[wire_issue(claim_utterance_ids=[u1.id])])
    client = StubClient(lambda kw: response(payload))
    outcome = await analyzer(client).analyze(s)
    assert outcome.error is None and outcome.result.issues[0].claim_utterance_ids == [u1.id]
    assert outcome.model == "claude-opus-5-5" and outcome.request_id == "req_1" and outcome.latency_ms >= 0
    assert outcome.usage == {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 900,
                             "cache_creation_input_tokens": 0}
    assert not outcome.truncated and not outcome.refused and outcome.raw_text == payload
    (call,) = client.calls
    assert call["model"] == "claude-opus-5-5" and call["max_tokens"] == 1234
    assert call["output_config"] == {"format": {"type": "json_schema", "schema": semantic.wire_schema()},
                                     "effort": "low"}
    assert call["system"] == s.system_blocks() and call["messages"] == [{"role": "user", "content": s.user_text()}]
    assert "sk-ant-test" not in json.dumps(call)  # the key travels in the client, never in the request body


async def test_claude_analyzer_handles_refusal_truncation_and_bad_output(package, state):
    s = snap(package, state)
    refused = await analyzer(StubClient(lambda kw: response(None, stop="refusal", details=SimpleNamespace(
        category="x", explanation="declined")))).analyze(s)
    assert refused.refused and refused.error == "refused: declined" and refused.result is None
    cut = await analyzer(StubClient(lambda kw: response('{"candidate_claims": [', stop="max_tokens"))).analyze(s)
    assert cut.truncated and cut.error == "truncated at max_tokens" and cut.raw_text.startswith('{"cand')
    garbage = await analyzer(StubClient(lambda kw: response("not json at all"))).analyze(s)
    assert garbage.error.startswith("invalid output:") and garbage.result is None
    wrong = await analyzer(StubClient(lambda kw: response(wire(issues=[{}])))).analyze(s)
    assert wrong.error.startswith("invalid output:")
    no_text = await analyzer(StubClient(lambda kw: response(None))).analyze(s)
    assert no_text.error == "no text block in the response"


async def test_claude_analyzer_turns_sdk_errors_into_outcomes(package, state):
    s = snap(package, state)
    cases = {
        _StubSDK.APITimeoutError(): "timeout after 7.5 s",
        _StubSDK.RateLimitError(): "rate limited (HTTP 429)",
        _StubSDK.APIStatusError(500, "upstream exploded"): "HTTP 500: upstream exploded",
        _StubSDK.APIConnectionError("dns"): "connection error: dns",
        KeyError("odd"): "KeyError: 'odd'",
    }
    for exc, message in cases.items():
        outcome = await analyzer(StubClient(lambda kw, exc=exc: exc)).analyze(s)
        assert outcome.error == message and outcome.result is None, (exc, outcome.error)


def test_settings_defaults_and_validation():
    s = Settings()
    assert s.alignment == "claude" and s.alignment_model == "claude-opus-5-5" and s.alignment_effort == "low"
    assert s.alignment_timeout_s == 20.0 and s.alignment_max_tokens == 4000 and s.anthropic_api_key == ""
    assert "anthropic_api_key" not in repr(Settings(anthropic_api_key="secret"))
