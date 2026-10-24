"""scripts/alignment_eval.py and its inputs, offline (contract v5 §5): the scenario files load and validate against
their packages, talks/budget-review is a model-based meeting package with auto ids and no checks.yaml, and the
harness (Tracker bookkeeping, label comparison, summary, CLI) is proven with a scripted FakeAnalyzer. No API call:
the live evaluation itself is opt-in (`uv run python scripts/alignment_eval.py` with ANTHROPIC_API_KEY)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from relay import semantic
from relay.config import ROOT, Settings
from relay.meeting import load_demo, load_meeting
from relay.talk import check_talk, load_talk

SCRIPT = ROOT / "scripts" / "alignment_eval.py"  # scripts/ is not a package: load the module by path
_spec = importlib.util.spec_from_file_location("alignment_eval", SCRIPT)
ev = importlib.util.module_from_spec(_spec)
sys.modules["alignment_eval"] = ev  # dataclasses resolve their module through sys.modules
_spec.loader.exec_module(ev)

BUDGET = ROOT / "talks" / "budget-review"
REQUIRED_SCENARIOS = {"rollout-evidence-conflict", "budget-evidence-conflict", "budget-aligned", "hidden-disagreement",
                      "ambiguous-rollback-owner", "ambiguous-rollback-owner-assigned-earlier",
                      "paraphrase-and-pronouns", "negations-questions-hypotheticals-quotes", "unrelated-agreement",
                      "plan-change-confirmed", "partial-correction", "plain-agreed-decision"}


def result(**fields) -> dict:
    return {"schema_version": 1, "candidate_claims": [], "participant_assumptions": [], "issues": [], "updates": [],
            "decision_proposals": [], **fields}


def issue(**fields) -> dict:
    base = {"existing_issue_id": None, "issue_type": "evidence_conflict", "topic": "Friday's release stage",
            "claim": "Friday's announcement goes to all customers as a production launch", "claim_utterance_ids": ["U1"],
            "participant_views": [], "missing_details": [],
            "evidence": [{"kind": "source", "id": "REQ-2", "quote": "the beta release, not the production release"},
                         {"kind": "clarification", "id": "CL-1", "quote": "Friday is the beta release"}],
            "material_consequence": "customers outside the beta hear of a launch that is not happening",
            "commitment": "proposed", "commitment_utterance_ids": [], "recommendation": "suggest", "confidence": "high",
            "explanation": "REQ-2 and CL-1 say beta", "question": "Before we confirm, does Friday's announcement go to beta users only?"}
    return {**base, **fields}


def proposal(**fields) -> dict:
    base = {"decision": "Friday's announcement goes to beta users only", "status": "confirmed", "owner": "Bob",
            "owner_utterance_ids": ["U3"], "agreement_utterance_ids": ["U3"],
            "follow_up": "Bob sends the beta release notes on Friday", "source_ids": ["REQ-2", "DEC-2"],
            "supersedes_decision_id": None, "resolves_issue_ids": ["IS1"], "explanation": "Carol's correction"}
    return {**base, **fields}


def demo_script(snapshot: semantic.Snapshot) -> dict:
    """The analysis a good model gives for rollout-evidence-conflict: pending after Bob, spoken when Carol confirms,
    settled with a decision and an owner after Carol's correction."""
    if snapshot.revision == 1:
        return result(candidate_claims=[{"utterance_id": "U1", "text": "Friday is a production launch to all customers",
                                         "kind": "proposal", "agrees_with_sources": False, "evidence_ids": ["REQ-2"]}],
                      issues=[issue()])
    if snapshot.revision == 2:
        return result(issues=[issue(existing_issue_id="IS1", commitment="confirming", commitment_utterance_ids=["U2"],
                                    recommendation="clarify")])
    return result(updates=[{"issue_id": "IS1", "status": "resolved", "utterance_ids": ["U3"],
                            "settled": ["audience: beta users only"], "note": "Carol corrected the audience"}],
                  decision_proposals=[proposal()])


def scenario(name: str) -> ev.Scenario:
    return ev.load_scenarios([name])[0]


async def run(name_or_scenario, script) -> dict:
    sc = scenario(name_or_scenario) if isinstance(name_or_scenario, str) else name_or_scenario
    return await ev.run_scenario(sc, semantic.FakeAnalyzer(script), run=1)


# --- the second corpus -------------------------------------------------------------------------------------------------

def test_budget_review_is_a_clean_model_based_meeting_package():
    assert check_talk(BUDGET) == []  # no errors and no warnings
    talk = load_talk(BUDGET)
    package = load_meeting(talk)
    assert package.title == "Q1 marketing budget review" and package.host == "Dana"
    assert [(p.name, p.role) for p in package.participants] == [("Dana", "Finance lead"), ("Eli", "Marketing"),
                                                               ("Fay", "Engineering lead")]
    assert [p.id for p in package.phases] == ["presentation", "discussion", "wrapup"]
    assert "who runs it" in package.phase("discussion").prompt and package.discussion_slide == "discussion"
    assert not (BUDGET / "checks.yaml").exists() and not (BUDGET / "demo.yaml").exists()
    assert package.checks is None and load_demo(talk, package) is None
    assert [st.id for st in package.statements] == [f"FACTS-{n}" for n in range(1, 10)]  # auto ids in file order
    first = package.statement("FACTS-1")
    assert "$30,000" in first.text and "10 February" in first.text and first.location.startswith("sources/facts.md:")
    assert first.doc_title == "Spring campaign fact sheet" and first.doc_id == "facts"
    assert "1 March" in package.statement("FACTS-3").text and "pause" in package.statement("FACTS-5").text
    assert "90 days" in package.statement("FACTS-7").text
    chunks = [chunk for slide in talk.relay_slides for chunk in slide.chunks]
    assert [c.id for c in chunks] == ["approved-budget", "campaign-start", "data-retention"]
    assert all(len(c.say) <= 300 and not any(ch.isdigit() for ch in c.say) for c in chunks)


# --- the scenario files -------------------------------------------------------------------------------------------------

def test_every_scenario_file_loads_and_matches_its_package():
    scenarios = ev.load_scenarios()
    names = {s.name for s in scenarios}
    assert REQUIRED_SCENARIOS <= names, REQUIRED_SCENARIOS - names
    assert {s.package for s in scenarios} == {"talks/rollout-review", "talks/budget-review"}
    for s in scenarios:
        _, package = ev.load_package(s.package)
        assert ev.check_scenario(s, package) == [], s.name
        assert s.lines and all(line.expect and set(line.expect) <= set(ev.OUTCOMES) for line in s.lines), s.name
        assert all(line.index == k + 1 for k, line in enumerate(s.lines))
    by_name = {s.name: s for s in scenarios}
    demo = by_name["rollout-evidence-conflict"]
    assert demo.clarifications[0]["source_ids"] == ["REQ-2", "REQ-3"] and demo.category == "evidence_conflict"
    assert [line.expect for line in demo.lines] == [("silent", "suggest"), ("clarify",), ("silent",)]
    assert demo.lines[1].requires_clarify and not demo.lines[0].requires_clarify
    assert demo.lines[2].decision == {"contains": ["beta"], "owner": "Bob"}
    assert by_name["plain-agreed-decision"].category == "none"
    assert by_name["ambiguous-rollback-owner"].category == "ambiguous_decision"
    assert all(line.expect == ("silent",) for line in by_name["ambiguous-rollback-owner-assigned-earlier"].lines)
    quiet = by_name["negations-questions-hypotheticals-quotes"]
    assert not any(line.requires_clarify for line in quiet.lines)
    assert by_name["plan-change-confirmed"].lines[2].decision["plan_changed"] is True
    assert any(line.decision for line in by_name["budget-aligned"].lines)


def test_scenario_schema_problems_are_listed(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("package: talks/rollout-review\nextra: 1\nlines:\n"
                   "  - {speaker: Bob, text: hi, expect: maybe, issue_types: [nope], cited_lines: [3]}\n"
                   "  - {speaker: Bob, text: hi, expect: silent, decision: {contains: [], owner: 7}}\n"
                   "  - {text: hi, expect: silent}\n", encoding="utf-8")
    with pytest.raises(ev.ScenarioError) as info:
        ev.load_scenario(bad)
    joined = " | ".join(info.value.problems)
    for needle in ("unknown keys extra", "`expect` must be one of", "'nope' is not one of", "line 3 is not this line",
                   "contains: at least one substring", "owner: a participant name or null", "`speaker` is required"):
        assert needle in joined, needle
    with pytest.raises(ev.ScenarioError):
        ev.load_scenarios(["no-such-scenario"])
    stranger = tmp_path / "stranger.yaml"
    stranger.write_text("package: talks/rollout-review\nlines:\n"
                        "  - {speaker: Zed, text: hi, expect: silent, cited_speakers: [Dana], evidence_ids: [FACTS-1],"
                        " decision: {contains: [x], owner: Eli}}\n", encoding="utf-8")
    sc = ev.load_scenario(stranger)
    problems = " | ".join(ev.check_scenario(sc, ev.load_package("talks/rollout-review")[1]))
    assert "speaker 'Zed'" in problems and "cited speaker 'Dana'" in problems
    assert "evidence id FACTS-1" in problems and "decision owner 'Eli'" in problems


# --- the harness: tracker, comparison, summary ------------------------------------------------------------------------------

async def test_demo_scenario_passes_with_a_good_scripted_analysis():
    report = await run("rollout-evidence-conflict", demo_script)
    lines = report["lines"]
    assert [rec["comparison"]["actual"] for rec in lines] == ["suggest", "clarify", "silent"]
    assert all(rec["comparison"]["ok"] for rec in lines), [rec["comparison"] for rec in lines if not rec["comparison"]["ok"]]
    first, second, third = lines
    assert first["tracker"]["suggested"][0]["issue_id"] == "IS1" and first["tracker"]["suggested"][0]["status"] == "suggested"
    assert first["comparison"]["attribution"]["ok"] and first["comparison"]["evidence"] == {
        "ok": True, "cited": ["REQ-2", "CL-1"], "off_target": []}
    spoken = second["tracker"]["spoken"][0]
    assert spoken["issue_id"] == "IS1" and spoken["status"] == "asked" and not spoken["new"]
    assert spoken["utterance_ids"] == ["U1", "U2"] and spoken["speakers"] == ["Bob", "Carol"]
    assert second["comparison"]["attribution"] == {"ok": True, "type_ok": True, "cites_ok": True, "speakers_ok": True,
                                                  "missing_utterances": [], "missing_speakers": [], "issue_ids": ["IS1"]}
    assert not second["comparison"]["missed"] and not second["comparison"]["unnecessary"]
    assert third["tracker"]["updates"][0]["status"] == "resolved" and third["tracker"]["decisions"][0]["owner"] == "Bob"
    decision = third["comparison"]["decision"]
    assert decision["captured"] and decision["owner_ok"] and decision["found"]["by"] == "analysis" and not decision["late"]
    assert third["comparison"]["closed"] == {"ok": True, "never_raised": [], "still_open": []}
    assert report["issues"][0]["status"] == "resolved" and report["issues"][0]["resolution_utterance_ids"] == ["U3"]
    assert report["issues"][0]["settled_details"] == ["audience: beta users only"]
    assert report["decisions"][0]["agreement_utterance_ids"] == ["U3"] and report["decisions"][0]["source_ids"] == ["REQ-2", "DEC-2"]
    assert report["notes"]["action_items"][0]["owner"] == "Bob" and report["unexpected_decisions"] == []
    assert first["snapshot"]["passages"] == 11 and third["snapshot"]["open_issues"] == 1  # IS1 was open before line 3
    assert first["outcome"]["result"]["issues"][0]["topic"] == "Friday's release stage"  # the raw outcome is kept
    summary = ev.summarize("fake-analyzer", [report])
    cat = summary["categories"]["evidence_conflict"]
    assert (cat["expected"], cat["missed"], cat["unnecessary"]) == (1, 0, 0)
    assert (cat["attribution_ok"], cat["attribution_total"], cat["evidence_ok"], cat["evidence_total"]) == (2, 2, 2, 2)
    assert (cat["decisions_ok"], cat["decisions_total"], cat["owner_ok"], cat["owner_total"]) == (1, 1, 1, 1)
    assert (cat["closed_ok"], cat["closed_total"]) == (1, 1) and summary["policy_ok"] == 3 == summary["lines"]
    assert summary["errors"] == 0 and summary["mismatches"] == [] and summary["cost_usd"] is None
    text = ev.format_summary(summary, "low")
    assert "evidence_conflict" in text and "policy agreement 3/3" in text and "n/a" in text


async def test_an_empty_analysis_counts_as_missed_intervention_and_missed_decision():
    report = await run("rollout-evidence-conflict", ())
    cmp = [rec["comparison"] for rec in report["lines"]]
    assert [c["actual"] for c in cmp] == ["silent"] * 3 and [c["policy_ok"] for c in cmp] == [True, False, True]
    assert cmp[1]["missed"] and not cmp[1]["unnecessary"]
    assert not cmp[2]["decision"]["captured"] and cmp[2]["closed"]["never_raised"] == ["Friday|announce|production"]
    summary = ev.summarize("fake-analyzer", [report])
    assert summary["categories"]["evidence_conflict"]["missed"] == 1 and summary["policy_ok"] == 2
    assert [m["failed"] for m in summary["mismatches"]] == [["policy"], ["closed", "decision"]]


async def test_a_premature_clarification_is_an_unnecessary_interruption():
    def eager(snapshot):
        if snapshot.revision == 1:
            return result(issues=[issue(commitment="committed", commitment_utterance_ids=["U1"], recommendation="clarify")])
        return result()

    report = await run("rollout-evidence-conflict", eager)
    first = report["lines"][0]["comparison"]
    assert first["actual"] == "clarify" and first["unnecessary"] and not first["policy_ok"]
    summary = ev.summarize("fake-analyzer", [report])
    assert summary["categories"]["evidence_conflict"]["unnecessary"] == 1
    # the issue was asked at line 1; nobody answered: after ALIGNMENT_ANSWER_WINDOW utterances it would be left open
    assert report["issues"][0]["status"] == "asked"


async def test_off_target_evidence_and_a_wrong_owner_are_reported_not_hidden():
    def sloppy(snapshot):
        if snapshot.revision == 2:
            return result(issues=[issue(commitment="confirming", commitment_utterance_ids=["U2"], recommendation="clarify",
                                        evidence=[{"kind": "source", "id": "REQ-7", "quote": "twice a day"}])])
        if snapshot.revision == 3:
            return result(decision_proposals=[proposal(owner="Carol", resolves_issue_ids=[])])
        return result()

    report = await run("rollout-evidence-conflict", sloppy)
    second, third = report["lines"][1]["comparison"], report["lines"][2]["comparison"]
    assert second["actual"] == "clarify" and second["evidence"] == {"ok": False, "cited": ["REQ-7"], "off_target": ["REQ-7"]}
    assert not second["ok"] and second["attribution"]["ok"]
    assert third["decision"]["text_ok"] and third["decision"]["owner_ok"] is False and not third["decision"]["captured"]
    assert third["closed"]["still_open"] == ["Friday|announce|production"]  # nobody resolved IS1
    summary = ev.summarize("fake-analyzer", [report])
    cat = summary["categories"]["evidence_conflict"]
    assert (cat["evidence_ok"], cat["evidence_total"], cat["owner_ok"], cat["owner_total"]) == (0, 1, 0, 1)


async def test_late_decisions_unexpected_decisions_plan_change_and_aging():
    package = "talks/rollout-review"
    lines = tuple(ev.Line(k + 1, speaker, text, ("silent",) if k else ("clarify",), **extra) for k, (speaker, text, extra) in enumerate([
        ("Bob", "I'll announce Friday's production launch to all customers.", {"issue_types": ("evidence_conflict",)}),
        ("Carol", "Fine.", {"decision": {"contains": ["Friday|production"], "plan_changed": True}}),
        ("Alice", "We are changing the plan on purpose: production for everyone on Friday.", {}),
        ("Bob", "Noted.", {}),
        ("Carol", "Moving on to the review time.", {}),
        ("Alice", "Ten on Tuesday then.", {"open_questions_contain": ("Friday",)}),
    ]))
    sc = ev.Scenario("inline", Path("inline.yaml"), package, "inline", "", (), lines)

    def script(snapshot):
        if snapshot.revision == 1:
            return result(issues=[issue(commitment="confirming", commitment_utterance_ids=["U1"], recommendation="clarify")])
        if snapshot.revision == 3:
            return result(candidate_claims=[{"utterance_id": "U3", "text": "production for everyone on Friday",
                                             "kind": "plan_change", "agrees_with_sources": False, "evidence_ids": []}],
                          decision_proposals=[proposal(decision="Production goes to all customers on Friday", owner=None,
                                                       owner_utterance_ids=[], agreement_utterance_ids=["U3"],
                                                       follow_up=None, source_ids=["REQ-3"], resolves_issue_ids=[]),
                                              proposal(decision="Lunch is at noon", owner="Zed", owner_utterance_ids=[],
                                                       agreement_utterance_ids=["U3"], follow_up=None, source_ids=[],
                                                       resolves_issue_ids=[])])
        return result()

    report = await run(sc, script)
    second = report["lines"][1]["comparison"]["decision"]
    assert not second["captured"] and second["late"] and second["found"]["plan_changed"] is True  # inferred from the claim
    assert [d["decision"] for d in report["unexpected_decisions"]] == ["Lunch is at noon"]
    assert report["decisions"][1]["owner"] == ""  # "Zed" is not a participant: owner unknown, never invented
    assert report["issues"][0]["status"] == "open"  # asked at revision 1, unanswered past the window
    assert report["lines"][5]["comparison"]["retained"]["ok"]  # an open issue still counts as retained
    assert "IS1" in report["lines"][5]["tracker"]["aged_open"] or "IS1" in report["lines"][4]["tracker"]["aged_open"]
    summary = ev.summarize("fake-analyzer", [report])
    cat = summary["categories"]["evidence_conflict"]
    assert cat["decisions_late"] == 1 and cat["decisions_ok"] == 0 and summary["unexpected_decisions"] == 1
    assert cat["retained_ok"] == 1 and cat["expected"] == 1 and cat["missed"] == 0


async def test_duplicate_confirmations_and_candidates_do_not_pile_up():
    def repeat_decision(snapshot):
        if snapshot.revision == 1:
            return result(decision_proposals=[proposal(status="nearing", agreement_utterance_ids=["U1"], owner_utterance_ids=[],
                                                       resolves_issue_ids=[])])
        return result(decision_proposals=[proposal(agreement_utterance_ids=[f"U{snapshot.revision}"], owner_utterance_ids=[],
                                                   resolves_issue_ids=[])])

    report = await run("rollout-evidence-conflict", repeat_decision)
    first, second, third = (rec["tracker"] for rec in report["lines"])
    assert first["candidates"] == ["nearing: Friday's announcement goes to beta users only"] and first["decisions"] == []
    assert len(second["decisions"]) == 1 and third["decisions"] == [] and third["duplicates"]
    assert len(report["decisions"]) == 1 and report["notes"]["decisions"][0]["id"] == "D1"


async def test_an_analyzer_error_is_recorded_and_the_meeting_goes_on():
    report = await run("budget-aligned", [RuntimeError("down"), result(), result()])
    first = report["lines"][0]
    assert first["outcome"]["error"] == "RuntimeError: down" and first["tracker"]["error"] == "RuntimeError: down"
    assert first["comparison"]["error"] and not first["comparison"]["ok"] and first["comparison"]["actual"] == "silent"
    summary = ev.summarize("fake-analyzer", [report])
    assert summary["errors"] == 1 and summary["latency_ms"]["n"] == 2 and summary["mismatches"][0]["failed"][-1] == "error"


def test_helpers_mentions_match_decision_percentile_and_cost():
    assert ev.mentions("We stay at $30,000 this quarter", "thirty thousand|30,000") and not ev.mentions("x", "y|z")
    decisions = [{"decision": "Friday is beta only", "follow_up": "Bob sends notes", "owner": "Bob", "plan_changed": False}]
    assert ev.match_decision({"contains": ["beta", "notes"], "owner": "bob"}, decisions)["captured"]
    assert ev.match_decision({"contains": ["beta"], "owner": None}, decisions)["owner_ok"] is False
    assert ev.match_decision({"contains": ["beta"], "plan_changed": True}, decisions)["plan_changed_ok"] is False
    assert ev.match_decision({"contains": ["beta"]}, decisions) == {"captured": True, "text_ok": True, "owner_ok": None,
                                                                    "plan_changed_ok": None, "found": decisions[0]}
    assert not ev.match_decision({"contains": ["production"]}, decisions)["text_ok"]
    assert ev.percentile([], 0.5) is None and ev.percentile([5, 1, 3], 0.5) == 3 and ev.percentile([1, 2, 3, 100], 0.95) == 100
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 2_000_000,
             "cache_creation_input_tokens": 100_000}
    assert ev.estimate_cost("claude-opus-5-5", usage) == pytest.approx(4.0 + 2.0 + 0.4 + 0.5)
    assert ev.estimate_cost("claude-haiku-5-5", usage) == pytest.approx(0.1 + 0.05 + 0.02 + 0.0125)
    assert ev.estimate_cost("some-other-model", usage) is None
    assert set(ev.PRICES_USD_PER_MTOK) == {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}


# --- the CLI --------------------------------------------------------------------------------------------------------------

@pytest.fixture
def no_env(monkeypatch):
    """`load_settings` without the machine's .env: no key unless the test says so."""
    monkeypatch.setattr(ev, "load_settings",
                        lambda **overrides: Settings(**{k: v for k, v in overrides.items() if v is not None}))


def test_cli_lists_scenarios_and_rejects_unknown_ones(capsys):
    assert ev.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "rollout-evidence-conflict:" in out and "L2 Carol: expect clarify" in out
    assert ev.main(["--scenario", "no-such-scenario"]) == 1
    assert "unknown scenario" in capsys.readouterr().err


def test_cli_exits_2_without_a_key_and_never_calls_the_api(no_env, capsys, monkeypatch):
    monkeypatch.setattr(ev.semantic, "ClaudeAnalyzer", lambda *a, **k: pytest.fail("no analyzer may be built without a key"))
    assert ev.main([]) == 2
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY is not set" in err and "--fake" in err
    assert ev.main(["--repeat", "0", "--fake"]) == 2


def test_cli_fake_run_writes_the_report(no_env, tmp_path, capsys):
    code = ev.main(["--fake", "--models", "fake-x", "--scenario", "plain-agreed-decision", "--scenario", "budget-aligned",
                    "--out", str(tmp_path), "--effort", "medium"])
    assert code == 0
    out = capsys.readouterr().out
    assert "2 scenario(s) x 1 model(s) x 1 run(s) = 6 analyses (fake analyzer" in out
    assert "== fake-x (effort medium): 2 scenario(s), 2 run(s), 6 lines, 0 error(s)" in out
    files = list(tmp_path.glob("*-fake-x.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["fake"] is True and data["summary"]["lines"] == 6 and [s["scenario"] for s in data["scenarios"]] == [
        "plain-agreed-decision", "budget-aligned"]
    assert data["summary"]["categories"]["none"]["decisions_total"] == 2  # both labelled decisions were missed by the fake
    assert data["scenarios"][1]["lines"][0]["snapshot"]["passages"] == 9  # the budget fact sheet's FACTS-1 … FACTS-9
    assert data["scenarios"][1]["lines"][0]["outcome"]["result"]["issues"] == []
