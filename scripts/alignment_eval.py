"""Opt-in live evaluation of the model-based alignment (contract v5 §5; doc/alignment_replacement_prompt.md,
"Acceptance and evaluation").

    uv run python scripts/alignment_eval.py                          # every scenario, the settings' model and effort
    uv run python scripts/alignment_eval.py --models claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5 --repeat 3
    uv run python scripts/alignment_eval.py --scenario rollout-evidence-conflict --scenario budget-aligned --effort low
    uv run python scripts/alignment_eval.py --list                   # the scenarios and their labels, offline
    uv run python scripts/alignment_eval.py --fake                   # harness smoke test: an empty-result analyzer, no API call

Needs ANTHROPIC_API_KEY in the repo-root `.env` (relay.config.load_settings); exits 2 without it. Costs money: every line
of every scenario is one Claude request per model per repeat (the documents block is cached across them). Never calls
Higgs: the evaluation measures the analysis, not the voice path (transcript-to-audible-intervention latency needs the
live meeting, scripts/live_check.py).

What it does, per scenario and model: loads the meeting package (relay.talk.load_talk + relay.meeting.load_meeting),
builds a real MeetingState with a counting clock, records the scenario's clarifications (as the presentation would
have), then for each labelled line: add_utterance → build_snapshot (generation 1, the line as the new utterance) →
analyzer.analyze → reconcile → the controller's bookkeeping replicated by `Tracker` (pure, below) → compare with the
labels. The per-line outcome is the policy the controller would act on right after that line: `clarify` (a question
is voiced; the eval assumes it is heard), `suggest` (a silent suggestion card) or `silent`.

Scenario files: scripts/alignment_scenarios/<name>.yaml

    package: talks/<id>                       # the meeting package (relative to the repo root)
    title: …                                  # optional
    clarifications:                           # optional: recorded before the discussion, by "auto" (CL-1, CL-2 …)
      - {question: …, answer: …, source_ids: [REQ-2]}
    lines:
      - speaker: Bob                          # a participant of the package
        text: "…"
        expect: clarify                       # silent | suggest | clarify, or a list of the acceptable outcomes
        issue_types: [evidence_conflict]      # optional: acceptable types of the issue driving a clarify/suggest here
        cited_lines: [1, 2]                   # optional: 1-based lines the driving issue must cite (claim, commitment,
                                              #   utterance evidence or participant views)
        cited_speakers: [Alice, Bob]          # optional: participants whose words the driving issue must cite
        evidence_ids: [REQ-2, DEC-2, CL-1]    # optional: cited passages/clarifications/decisions must be among these
        decision: {contains: ["beta"], owner: Bob, plan_changed: true}   # optional: by the end of this line a decision
                                              #   exists whose text has every `contains` entry (case-insensitive; `|`
                                              #   separates alternatives); `owner` (null = nobody) and `plan_changed`
                                              #   are checked only when given
        open_questions_contain: ["rollback"]  # optional: after this line an open issue or open question mentions each
        closed_contain: ["Friday"]            # optional: after this line no open issue mentions it, but a closed one does

Reported per issue category (evidence_conflict, hidden_disagreement, ambiguous_decision; "none" for lines without a
labelled category): necessary interventions missed, unnecessary interruptions, attribution (type, cited utterances
and participants), evidence relevance, decision and owner capture, open-question retention and closure; per model:
policy agreement, latency p50/p95 (transcript-to-analysis), usage and a cost estimate from PRICES_USD_PER_MTOK.
Every raw outcome goes to out/alignment_eval/<timestamp>-<model>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from relay import semantic  # noqa: E402
from relay.config import OUT, Settings, load_settings  # noqa: E402
from relay.meeting import OPEN_ISSUE_STATUSES, MeetingPackage, MeetingState, load_meeting  # noqa: E402
from relay.talk import Talk, load_talk  # noqa: E402

SCENARIOS_DIR = HERE / "alignment_scenarios"
OUT_DIR = OUT / "alignment_eval"
OUTCOMES = ("silent", "suggest", "clarify")
RANK = {outcome: n for n, outcome in enumerate(OUTCOMES)}
ISSUE_TYPES = semantic.ISSUE_TYPES
CATEGORIES = (*ISSUE_TYPES, "none")
ALIGNMENT_ANSWER_WINDOW = 4  # utterances after a question before an unanswered issue is left "open" (contract §3.5)
SPEAKABLE = ("pending", "suggested")  # issue statuses the controller may still voice a clarification for
LINE_KEYS = {"speaker", "text", "expect", "issue_types", "cited_lines", "cited_speakers", "evidence_ids", "decision",
             "open_questions_contain", "closed_contain", "note"}
DECISION_KEYS = {"contains", "owner", "plan_changed"}
SCENARIO_KEYS = {"package", "title", "notes", "clarifications", "lines"}
CLARIFICATION_KEYS = {"question", "answer", "source_ids"}
MIN_DUPLICATE_OVERLAP = 0.6  # content-word overlap for "the same decision again" (contract §3.4: same_decision)

# Anthropic API list prices in USD per million tokens, as of 2026-10-24 (first-party API; cache write = 1.25 × input
# for the five-minute cache; cache read as listed for Opus 5.5 / Sonnet 5.5, 0.1 × input assumed for Haiku 5.5, whose
# input/output prices apply to prompts up to 100K tokens). Check https://platform.claude.com/docs/en/about-claude/pricing
# before quoting a number; the estimate here is for comparing the three models on the same scenarios.
PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


# --- scenarios -----------------------------------------------------------------------------------------------------------

class ScenarioError(ValueError):
    def __init__(self, path: Path | str, problems: list[str]):
        self.problems = problems
        super().__init__(f"{path}: {len(problems)} problem(s)\n" + "\n".join(f"  {p}" for p in problems))


@dataclass(frozen=True)
class Line:
    index: int  # 1-based position in the scenario
    speaker: str
    text: str
    expect: tuple[str, ...]  # the acceptable policy outcomes right after this line
    issue_types: tuple[str, ...] = ()
    cited_lines: tuple[int, ...] = ()
    cited_speakers: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] | None = None
    decision: dict | None = None
    open_questions_contain: tuple[str, ...] = ()
    closed_contain: tuple[str, ...] = ()

    @property
    def requires_clarify(self) -> bool:
        """A necessary intervention: the only acceptable outcome is a spoken clarification."""
        return self.expect == ("clarify",)


@dataclass(frozen=True)
class Scenario:
    name: str
    path: Path
    package: str
    title: str
    notes: str
    clarifications: tuple[dict, ...]
    lines: tuple[Line, ...]

    @property
    def category(self) -> str:
        """The scenario's issue category: the first labelled issue type, else "none" (a plain decision, all silent)."""
        return next((line.issue_types[0] for line in self.lines if line.issue_types), "none")


def _str_list(value: Any, where: str, problems: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        problems.append(f"{where}: must be a list of non-empty strings")
        return ()
    return tuple(v.strip() for v in value)


def _parse_line(raw: Any, k: int, problems: list[str]) -> Line | None:
    where = f"lines[{k}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: each line is a mapping (speaker, text, expect, …)")
        return None
    unknown = sorted(str(key) for key in set(raw) - LINE_KEYS)
    if unknown:
        problems.append(f"{where}: unknown keys {', '.join(unknown)} (known: {', '.join(sorted(LINE_KEYS))})")
    speaker, text = raw.get("speaker"), raw.get("text")
    if not isinstance(speaker, str) or not speaker.strip():
        problems.append(f"{where}: `speaker` is required")
        speaker = ""
    if not isinstance(text, str) or not text.strip():
        problems.append(f"{where}: `text` is required")
        text = ""
    expect = raw.get("expect")
    if isinstance(expect, str):
        expect = [expect]
    if not isinstance(expect, list) or not expect or any(e not in OUTCOMES for e in expect):
        problems.append(f"{where}: `expect` must be one of {', '.join(OUTCOMES)} or a list of them, got {expect!r}")
        expect = []
    expect = tuple(sorted(dict.fromkeys(expect), key=RANK.get))
    issue_types = _str_list(raw.get("issue_types"), f"{where}.issue_types", problems)
    for issue_type in issue_types:
        if issue_type not in ISSUE_TYPES:
            problems.append(f"{where}.issue_types: {issue_type!r} is not one of {', '.join(ISSUE_TYPES)}")
    cited_raw = raw.get("cited_lines") or []
    if not isinstance(cited_raw, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in cited_raw):
        problems.append(f"{where}.cited_lines: must be a list of 1-based line numbers")
        cited_raw = []
    for n in cited_raw:
        if not 1 <= n <= k + 1:
            problems.append(f"{where}.cited_lines: line {n} is not this line or an earlier one (1–{k + 1})")
    evidence_ids = None
    if raw.get("evidence_ids") is not None:
        evidence_ids = _str_list(raw.get("evidence_ids"), f"{where}.evidence_ids", problems)
    decision = raw.get("decision")
    if decision is not None:
        if not isinstance(decision, dict):
            problems.append(f"{where}.decision: must be a mapping {{contains: [...], owner: <name>|null}}")
            decision = None
        else:
            unknown = sorted(str(key) for key in set(decision) - DECISION_KEYS)
            if unknown:
                problems.append(f"{where}.decision: unknown keys {', '.join(unknown)}")
            contains = _str_list(decision.get("contains"), f"{where}.decision.contains", problems)
            if not contains:
                problems.append(f"{where}.decision.contains: at least one substring is required")
            if "owner" in decision and decision["owner"] is not None and not isinstance(decision["owner"], str):
                problems.append(f"{where}.decision.owner: a participant name or null")
            if "plan_changed" in decision and not isinstance(decision["plan_changed"], bool):
                problems.append(f"{where}.decision.plan_changed: true or false")
            decision = {"contains": list(contains), **{key: decision[key] for key in ("owner", "plan_changed")
                                                      if key in decision}}
    return Line(k + 1, speaker.strip(), " ".join(text.split()), expect, issue_types, tuple(int(n) for n in cited_raw),
                _str_list(raw.get("cited_speakers"), f"{where}.cited_speakers", problems), evidence_ids, decision,
                _str_list(raw.get("open_questions_contain"), f"{where}.open_questions_contain", problems),
                _str_list(raw.get("closed_contain"), f"{where}.closed_contain", problems))


def load_scenario(path: Path | str) -> Scenario:
    """Parse and check one scenario file (the schema only; `check_scenario` cross-checks it with its package)."""
    path = Path(path)
    problems: list[str] = []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError) as exc:
        raise ScenarioError(path, [f"unreadable or invalid YAML: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ScenarioError(path, ["must be a mapping with package and lines"])
    unknown = sorted(str(key) for key in set(data) - SCENARIO_KEYS)
    if unknown:
        problems.append(f"unknown keys {', '.join(unknown)} (known: {', '.join(sorted(SCENARIO_KEYS))})")
    package = data.get("package")
    if not isinstance(package, str) or not package.strip():
        problems.append("`package` is required (talks/<id>)")
        package = ""
    clarifications: list[dict] = []
    raw_cl = data.get("clarifications") or []
    if not isinstance(raw_cl, list):
        problems.append("`clarifications` must be a list of {question, answer, source_ids}")
        raw_cl = []
    for k, item in enumerate(raw_cl):
        where = f"clarifications[{k}]"
        if not isinstance(item, dict) or not isinstance(item.get("question"), str) or not isinstance(item.get("answer"), str):
            problems.append(f"{where}: needs `question` and `answer` strings")
            continue
        unknown = sorted(str(key) for key in set(item) - CLARIFICATION_KEYS)
        if unknown:
            problems.append(f"{where}: unknown keys {', '.join(unknown)}")
        clarifications.append({"question": " ".join(item["question"].split()), "answer": " ".join(item["answer"].split()),
                               "source_ids": list(_str_list(item.get("source_ids"), f"{where}.source_ids", problems))})
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        problems.append("`lines` must be a non-empty list")
        raw_lines = []
    lines = [line for line in (_parse_line(raw, k, problems) for k, raw in enumerate(raw_lines)) if line is not None]
    if problems:
        raise ScenarioError(path, problems)
    title = data.get("title") if isinstance(data.get("title"), str) else path.stem
    notes = data.get("notes") if isinstance(data.get("notes"), str) else ""
    return Scenario(path.stem, path, package.strip(), title.strip(), notes.strip(), tuple(clarifications), tuple(lines))


def check_scenario(scenario: Scenario, package: MeetingPackage) -> list[str]:
    """Problems of a scenario against its package: speakers, cited participants and owners must be participants,
    evidence ids must be the package's statements or the scenario's own clarifications."""
    problems: list[str] = []
    names = {p.name.casefold(): p.name for p in package.participants}
    known_ids = {st.id for st in package.statements} | {f"CL-{n}" for n in range(1, len(scenario.clarifications) + 1)}
    for cl_n, cl in enumerate(scenario.clarifications, 1):
        for sid in cl["source_ids"]:
            if package.statement(sid) is None:
                problems.append(f"clarifications[{cl_n - 1}]: source {sid} is not in {scenario.package}")
    for line in scenario.lines:
        where = f"line {line.index}"
        if line.speaker.casefold() not in names:
            problems.append(f"{where}: speaker {line.speaker!r} is not a participant ({', '.join(names.values())})")
        for name in line.cited_speakers:
            if name.casefold() not in names:
                problems.append(f"{where}: cited speaker {name!r} is not a participant")
        for eid in line.evidence_ids or ():
            if eid not in known_ids:
                problems.append(f"{where}: evidence id {eid} is neither a statement of {scenario.package} nor a "
                                "clarification of this scenario")
        owner = (line.decision or {}).get("owner")
        if isinstance(owner, str) and owner.casefold() not in names:
            problems.append(f"{where}: decision owner {owner!r} is not a participant")
    return problems


def scenario_files(directory: Path = SCENARIOS_DIR) -> list[Path]:
    return sorted(p for p in directory.glob("*.yaml") if p.is_file())


def load_scenarios(names: Iterable[str] | None = None, directory: Path = SCENARIOS_DIR) -> list[Scenario]:
    """Every scenario of the directory (or the named ones), validated; raises ScenarioError on the first bad file."""
    files = scenario_files(directory)
    by_name = {path.stem: path for path in files}
    wanted = list(dict.fromkeys(names)) if names else list(by_name)
    missing = [name for name in wanted if name not in by_name]
    if missing:
        raise ScenarioError(directory, [f"unknown scenario {name!r} (known: {', '.join(by_name) or 'none'})"
                                        for name in missing])
    return [load_scenario(by_name[name]) for name in wanted]


# --- the controller's bookkeeping, without the controller ------------------------------------------------------------------

@dataclass
class LineResult:
    """What one analysis did to the meeting state, and the policy outcome the controller would act on."""

    policy: str = "silent"
    spoken: list[dict] = field(default_factory=list)  # issues voiced now (clarify)
    suggested: list[dict] = field(default_factory=list)  # issues shown as silent suggestions now
    issues: list[dict] = field(default_factory=list)  # every valid issue of this analysis with its policy and status
    updates: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)  # Decision.to_json() of what was recorded now
    duplicates: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    aged_open: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # reconcile's reasons
    error: str | None = None


def _content_words(text: str) -> set[str]:
    return {w for w in (semantic.normalize_quote(text)).replace(",", " ").split() if len(w) >= 4}


class Tracker:
    """The controller's `on_analysis` bookkeeping replicated as pure state changes (contract §3, items 1–5), so the
    evaluation needs no Realtime session: new issues are added `pending`; a `clarify` policy on a pending or
    suggested issue marks it `asked` (the eval assumes the question is voiced and heard), `suggest` on a pending one
    marks it `suggested`; updates change statuses; confirmed proposals are recorded `by="analysis"` and resolve the
    issues they name; asked issues with no answer within ALIGNMENT_ANSWER_WINDOW utterances are left `open`.
    `plan_changed` follows the controller's rule (`semantic.plan_changed`): the proposal supersedes a decision, or a
    `plan_change` claim sits on one of its utterances (or the claims of the issues it resolves) or cites its sources."""

    def __init__(self, state: MeetingState) -> None:
        self.state = state

    def apply(self, snapshot: semantic.Snapshot, outcome: semantic.AnalysisOutcome) -> LineResult:
        result = LineResult()
        if outcome.error or outcome.result is None:
            result.error = outcome.error or "no result"
            result.aged_open = self._age(snapshot)
            return result
        rec = semantic.reconcile(outcome.result, snapshot)
        result.dropped = list(rec.dropped)
        state = self.state
        for assumption in rec.assumptions:
            entry = {"speaker": assumption.speaker, "utterance_id": assumption.utterance_id, "topic": assumption.topic,
                     "interpretation": assumption.interpretation}
            if not any((a.get("speaker"), a.get("topic"), a.get("interpretation")) ==
                       (entry["speaker"], entry["topic"], entry["interpretation"]) for a in state.assumptions):
                state.assumptions.append(entry)
        for valid in rec.issues:
            policy, reason = semantic.decide_policy(valid, snapshot)
            m = valid.model
            fields = dict(issue_type=m.issue_type, topic=m.topic, claim=m.claim, claim_utterance_ids=valid.claim_utterance_ids,
                          participant_views=valid.participant_views, missing_details=m.missing_details,
                          evidence=[{"kind": e["kind"], "id": e["id"], "quote": e["quote"]} for e in valid.evidence],
                          material_consequence=m.material_consequence, commitment=m.commitment, confidence=m.confidence,
                          recommendation=m.recommendation, question=m.question, revision=snapshot.revision,
                          commitment_utterance_ids=valid.commitment_utterance_ids)
            record = state.update_issue(valid.existing_id, **fields) if valid.existing_id else None
            new = record is None
            if new:
                record = state.add_issue(status="pending", policy=policy, **fields)
            spoke = suggested = False
            if policy == "clarify" and record.status in SPEAKABLE:
                record.status, record.policy, record.asked_at_revision = "asked", "clarify", snapshot.revision
                spoke = True
            elif policy == "suggest" and record.status == "pending":
                record.status, record.policy = "suggested", "suggest"
                suggested = True
            summary = self._issue_summary(record, valid, snapshot, policy, reason, new)
            result.issues.append(summary)
            if spoke:
                result.spoken.append(summary)
            elif suggested:
                result.suggested.append(summary)
        result.policy = "clarify" if result.spoken else "suggest" if result.suggested else "silent"
        for upd in rec.updates:
            if upd.status == "still_open":
                continue
            record = state.issue(upd.issue_id)
            if record is None:
                continue
            settled = list(dict.fromkeys([*record.settled_details, *upd.settled]))
            state.update_issue(upd.issue_id, status=upd.status, revision=snapshot.revision, note=upd.note,
                               settled_details=settled,
                               resolution_utterance_ids=list(dict.fromkeys([*record.resolution_utterance_ids,
                                                                            *upd.utterance_ids])))
            result.updates.append({"issue_id": upd.issue_id, "status": upd.status, "utterance_ids": upd.utterance_ids,
                                   "settled": upd.settled, "note": upd.note})
        for prop in rec.proposals:
            m = prop.model
            if m.status == "confirmed":
                duplicate = self._duplicate_of(m.decision)
                if duplicate is not None:
                    result.duplicates.append(f"{duplicate.id}: {m.decision}")
                    continue
                resolved_claims = [uid for iid in prop.resolves_issue_ids if state.issue(iid) is not None
                                   for uid in state.issue(iid).claim_utterance_ids]
                decision = state.add_decision(m.decision, prop.owner, m.follow_up, prop.source_ids, by="analysis",
                                              supersedes=prop.supersedes,
                                              agreement_utterance_ids=prop.agreement_utterance_ids,
                                              owner_utterance_ids=prop.owner_utterance_ids,
                                              plan_changed=semantic.plan_changed(prop, rec.claims, resolved_claims))
                for issue_id in prop.resolves_issue_ids:
                    record = state.issue(issue_id)
                    if record is not None and record.is_open:
                        state.update_issue(issue_id, status="resolved", revision=snapshot.revision,
                                           resolution_utterance_ids=list(dict.fromkeys(
                                               [*record.resolution_utterance_ids, *prop.agreement_utterance_ids])),
                                           note=record.note or f"settled by {decision.id}")
                        result.updates.append({"issue_id": issue_id, "status": "resolved", "by_decision": decision.id,
                                               "utterance_ids": prop.agreement_utterance_ids, "settled": [], "note": ""})
                state.candidate_decisions = [c for c in state.candidate_decisions
                                             if semantic.normalize_quote(c.get("decision")) != semantic.normalize_quote(m.decision)]
                result.decisions.append(decision.to_json())
            elif m.status in ("proposed", "nearing"):
                candidate = {"decision": m.decision, "status": m.status, "owner": prop.owner, "follow_up": m.follow_up,
                             "agreement_utterance_ids": prop.agreement_utterance_ids,
                             "owner_utterance_ids": prop.owner_utterance_ids, "source_ids": prop.source_ids}
                state.candidate_decisions = [c for c in state.candidate_decisions
                                             if semantic.normalize_quote(c.get("decision")) != semantic.normalize_quote(m.decision)]
                state.candidate_decisions.append(candidate)
                result.candidates.append(f"{m.status}: {m.decision}")
            elif m.status == "rejected":
                state.candidate_decisions = [c for c in state.candidate_decisions
                                             if semantic.normalize_quote(c.get("decision")) != semantic.normalize_quote(m.decision)]
                result.candidates.append(f"rejected: {m.decision}")
            elif m.status == "superseded" and prop.supersedes:
                for old in state.decisions:
                    if old.id == prop.supersedes:
                        old.status = "superseded"
                result.candidates.append(f"superseded {prop.supersedes}: {m.decision}")
        result.aged_open = self._age(snapshot)
        return result

    def _issue_summary(self, record, valid: semantic.ValidIssue, snapshot: semantic.Snapshot, policy: str, reason: str,
                       new: bool) -> dict:
        utterance_ids = list(dict.fromkeys([*valid.claim_utterance_ids, *valid.commitment_utterance_ids,
                                            *[e["id"] for e in valid.evidence if e["kind"] == "utterance"],
                                            *[v["utterance_id"] for v in valid.participant_views]]))
        speakers = []
        for uid in utterance_ids:
            u = snapshot.utterance(uid)
            if u and u.get("speaker") and u["speaker"] not in speakers:
                speakers.append(u["speaker"])
        return {"issue_id": record.id, "new": new, "issue_type": record.issue_type, "topic": record.topic,
                "claim": record.claim, "question": record.question, "missing_details": list(record.missing_details),
                "commitment": record.commitment, "confidence": record.confidence,
                "recommendation": record.recommendation, "policy": policy, "reason": reason, "status": record.status,
                "utterance_ids": utterance_ids, "speakers": speakers,
                "evidence_ids": valid.evidence_ids("source", "clarification", "decision"),
                "evidence": [{"kind": e["kind"], "id": e["id"], "quote": e["quote"]} for e in valid.evidence],
                "participant_views": list(valid.participant_views), "dropped": list(valid.dropped)}

    def _duplicate_of(self, decision: str):
        words = _content_words(decision)
        for old in self.state.decisions:
            if old.status != "confirmed":
                continue
            if semantic.normalize_quote(old.decision) == semantic.normalize_quote(decision):
                return old
            theirs = _content_words(f"{old.decision} {old.follow_up}")
            if words and len(words & theirs) / len(words) >= MIN_DUPLICATE_OVERLAP:
                return old
        return None

    def _age(self, snapshot: semantic.Snapshot) -> list[str]:
        aged = []
        for issue in self.state.open_issues():
            if issue.status == "asked" and snapshot.revision - issue.revision > ALIGNMENT_ANSWER_WINDOW:
                self.state.update_issue(issue.id, status="open")
                aged.append(issue.id)
        return aged


# --- comparing with the labels ---------------------------------------------------------------------------------------------

def mentions(text: Any, pattern: str) -> bool:
    """Case-insensitive substring test; `|` separates alternatives ("thirty thousand|30,000")."""
    hay = " ".join(str(text or "").split()).casefold()
    return any(alt.strip().casefold() in hay for alt in pattern.split("|") if alt.strip())


def issue_text(issue) -> str:
    data = issue.to_json() if hasattr(issue, "to_json") else dict(issue)
    parts = [data.get("topic"), data.get("claim"), data.get("question"), data.get("material_consequence"),
             *(data.get("missing_details") or []), *(data.get("settled_details") or [])]
    return " ".join(str(p) for p in parts if p)


def decision_text(decision) -> str:
    data = decision.to_json() if hasattr(decision, "to_json") else dict(decision)
    return f"{data.get('decision', '')} {data.get('follow_up', '')}"


def match_decision(label: dict, decisions: Iterable) -> dict:
    """The best match of a `decision` label among the recorded decisions: text first, then owner and plan_changed."""
    best: dict = {"captured": False, "text_ok": False, "owner_ok": None, "plan_changed_ok": None, "found": None}
    for decision in decisions:
        data = decision.to_json() if hasattr(decision, "to_json") else dict(decision)
        if not all(mentions(decision_text(data), pattern) for pattern in label["contains"]):
            continue
        owner_ok = None
        if "owner" in label:
            expected = label["owner"]
            actual = data.get("owner") or ""
            owner_ok = (not actual) if expected is None else actual.casefold() == str(expected).casefold()
        plan_ok = None
        if "plan_changed" in label:
            plan_ok = bool(data.get("plan_changed")) == bool(label["plan_changed"])
        candidate = {"captured": owner_ok is not False and plan_ok is not False, "text_ok": True, "owner_ok": owner_ok,
                     "plan_changed_ok": plan_ok, "found": data}
        if candidate["captured"]:
            return candidate
        if not best["text_ok"]:
            best = candidate
    return best


def compare_line(line: Line, result: LineResult, state: MeetingState, utterance_by_line: dict[int, str],
                 scenario_category: str) -> dict:
    """One line's report: the policy outcome against `expect`, and every label the line carries."""
    driving = result.spoken if result.policy == "clarify" else result.suggested if result.policy == "suggest" else []
    actual_types = list(dict.fromkeys(i["issue_type"] for i in driving))
    category = line.issue_types[0] if line.issue_types else scenario_category
    report: dict[str, Any] = {
        "line": line.index, "speaker": line.speaker, "expected": list(line.expect), "actual": result.policy,
        "policy_ok": result.policy in line.expect, "requires_clarify": line.requires_clarify,
        "missed": line.requires_clarify and result.policy != "clarify",
        "unnecessary": result.policy == "clarify" and "clarify" not in line.expect,
        "category": category, "actual_types": actual_types,
        "actual_category": actual_types[0] if actual_types else category,
        "error": result.error,
    }
    if driving and (line.issue_types or line.cited_lines or line.cited_speakers):
        required = {utterance_by_line[n] for n in line.cited_lines if n in utterance_by_line}
        cited = {uid for issue in driving for uid in issue["utterance_ids"]}
        speakers = {s.casefold() for issue in driving for s in issue["speakers"]}
        type_ok = any(t in line.issue_types for t in actual_types) if line.issue_types else True
        missing_utterances = sorted(required - cited)
        missing_speakers = [s for s in line.cited_speakers if s.casefold() not in speakers]
        report["attribution"] = {"ok": type_ok and not missing_utterances and not missing_speakers, "type_ok": type_ok,
                                 "cites_ok": not missing_utterances, "speakers_ok": not missing_speakers,
                                 "missing_utterances": missing_utterances, "missing_speakers": missing_speakers,
                                 "issue_ids": [i["issue_id"] for i in driving]}
    if driving and line.evidence_ids is not None:
        cited_docs = list(dict.fromkeys(eid for issue in driving for eid in issue["evidence_ids"]))
        off_target = [eid for eid in cited_docs if eid not in line.evidence_ids]
        report["evidence"] = {"ok": not off_target, "cited": cited_docs, "off_target": off_target}
    if line.decision is not None:
        report["decision"] = {"label": line.decision, **match_decision(line.decision, state.decisions), "late": False}
    if line.open_questions_contain:
        texts = [issue_text(i) for i in state.open_issues()] + [q.text for q in state.open_questions if not q.resolved]
        missing = [p for p in line.open_questions_contain if not any(mentions(t, p) for t in texts)]
        report["retained"] = {"ok": not missing, "missing": missing}
    if line.closed_contain:
        every = [issue_text(i) for i in state.issues]
        still_open = [issue_text(i) for i in state.open_issues()]
        never_raised = [p for p in line.closed_contain if not any(mentions(t, p) for t in every)]
        not_closed = [p for p in line.closed_contain if any(mentions(t, p) for t in still_open)]
        report["closed"] = {"ok": not never_raised and not not_closed, "never_raised": never_raised,
                            "still_open": not_closed}
    checks = [report["policy_ok"]] + [report[key]["ok"] for key in ("attribution", "evidence", "retained", "closed")
                                      if key in report]
    if "decision" in report:
        checks.append(report["decision"]["captured"])
    report["ok"] = all(checks) and not result.error
    return report


# --- running ---------------------------------------------------------------------------------------------------------------

class Counter:
    """The meeting clock of the evaluation: one second per call, deterministic."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> float:
        self.n += 1
        return float(self.n)


_PACKAGES: dict[str, tuple[Talk, MeetingPackage]] = {}


def load_package(package: str) -> tuple[Talk, MeetingPackage]:
    """talk + meeting of `talks/<id>` (relative to the repo root), cached; raises TalkError/ValueError when broken."""
    if package not in _PACKAGES:
        talk = load_talk(ROOT / package)
        meeting = load_meeting(talk)
        if meeting is None:
            raise ValueError(f"{package}: not a meeting package (no agenda.yaml)")
        _PACKAGES[package] = (talk, meeting)
    return _PACKAGES[package]


def _outcome_record(outcome: semantic.AnalysisOutcome) -> dict:
    record = {"latency_ms": outcome.latency_ms, "error": outcome.error, "model": outcome.model, "usage": outcome.usage,
              "truncated": outcome.truncated, "refused": outcome.refused, "request_id": outcome.request_id,
              "result": outcome.result.model_dump() if outcome.result is not None else None}
    if outcome.error and outcome.raw_text:
        record["raw_text"] = outcome.raw_text
    return record


async def run_scenario(scenario: Scenario, analyzer: semantic.Analyzer, *, run: int = 1,
                       on_line: Any | None = None) -> dict:
    """One scenario against one analyzer: the full record (every raw outcome, the tracker's work, the comparison)."""
    _, package = load_package(scenario.package)
    problems = check_scenario(scenario, package)
    if problems:
        raise ScenarioError(scenario.path, problems)
    state = MeetingState(package, Counter())
    state.set_phase("discussion")
    for cl in scenario.clarifications:
        state.add_clarification(cl["question"], cl["answer"], cl["source_ids"], by="auto")
    tracker = Tracker(state)
    utterance_by_line: dict[int, str] = {}
    records: list[dict] = []
    for line in scenario.lines:
        utterance = state.add_utterance(line.text, line.speaker, "typed")
        utterance_by_line[line.index] = utterance.id
        snapshot = semantic.build_snapshot(package, state, generation=1, new_utterance_ids=[utterance.id])
        t0 = time.monotonic()
        outcome = await analyzer.analyze(snapshot)
        result = tracker.apply(snapshot, outcome)
        comparison = compare_line(line, result, state, utterance_by_line, scenario.category)
        record = {"line": line.index, "speaker": line.speaker, "text": line.text, "utterance_id": utterance.id,
                  "snapshot": {"revision": snapshot.revision, "documents_chars": len(snapshot.documents_text()),
                               "user_chars": len(snapshot.user_text()), "passages": len(snapshot.passages),
                               "utterances": len(snapshot.utterances), "open_issues": len(snapshot.issues),
                               "passages_truncated": snapshot.passages_truncated},
                  "outcome": _outcome_record(outcome), "wall_ms": round((time.monotonic() - t0) * 1000),
                  "tracker": asdict(result), "comparison": comparison}
        records.append(record)
        if on_line is not None:
            on_line(scenario, line, record)
    labelled = [line.decision for line in scenario.lines if line.decision is not None]
    for record, line in zip(records, scenario.lines):
        cmp = record["comparison"].get("decision")
        if cmp and not cmp["captured"]:
            final = match_decision(line.decision, state.decisions)
            if final["captured"]:
                cmp["late"] = True
                cmp["found"] = final["found"]
    unexpected = [d.to_json() for d in state.decisions
                  if not any(match_decision(label, [d])["text_ok"] for label in labelled)]
    return {"scenario": scenario.name, "title": scenario.title, "package": scenario.package, "run": run,
            "category": scenario.category, "lines": records, "decisions": [d.to_json() for d in state.decisions],
            "unexpected_decisions": unexpected, "issues": [i.to_json() for i in state.issues],
            "notes": state.notes()}


# --- summarising -----------------------------------------------------------------------------------------------------------

def percentile(values: list[int | float], p: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))])


def estimate_cost(model: str, usage: dict) -> float | None:
    """USD for the summed usage at PRICES_USD_PER_MTOK; None for a model the table does not know."""
    prices = PRICES_USD_PER_MTOK.get(model)
    if prices is None:
        return None
    return round(((usage.get("input_tokens") or 0) * prices["input"] + (usage.get("output_tokens") or 0) * prices["output"]
                  + (usage.get("cache_read_input_tokens") or 0) * prices["cache_read"]
                  + (usage.get("cache_creation_input_tokens") or 0) * prices["cache_write"]) / 1_000_000, 4)


def _empty_category() -> dict:
    return {"expected": 0, "missed": 0, "unnecessary": 0, "attribution_ok": 0, "attribution_total": 0,
            "evidence_ok": 0, "evidence_total": 0, "decisions_ok": 0, "decisions_total": 0, "decisions_late": 0,
            "owner_ok": 0, "owner_total": 0, "retained_ok": 0, "retained_total": 0, "closed_ok": 0, "closed_total": 0}


def summarize(model: str, runs: list[dict]) -> dict:
    """Per-category counts, policy agreement, latency, usage and cost over every line of every run of one model."""
    categories = {c: _empty_category() for c in CATEGORIES}
    lines = policy_ok = errors = unexpected = 0
    latencies: list[int] = []
    usage = {key: 0 for key in USAGE_KEYS}
    mismatches: list[dict] = []
    for run in runs:
        unexpected += len(run["unexpected_decisions"])
        for record in run["lines"]:
            cmp = record["comparison"]
            cat, actual_cat = cmp["category"], cmp["actual_category"]
            lines += 1
            policy_ok += cmp["policy_ok"]
            outcome = record["outcome"]
            if outcome["error"]:
                errors += 1
            else:
                latencies.append(outcome["latency_ms"])
            for key in USAGE_KEYS:
                usage[key] += (outcome.get("usage") or {}).get(key) or 0
            if cmp["requires_clarify"]:
                categories[cat]["expected"] += 1
                categories[cat]["missed"] += cmp["missed"]
            if cmp["unnecessary"]:
                categories[actual_cat]["unnecessary"] += 1
            if "attribution" in cmp:
                categories[actual_cat]["attribution_total"] += 1
                categories[actual_cat]["attribution_ok"] += cmp["attribution"]["ok"]
            if "evidence" in cmp:
                categories[actual_cat]["evidence_total"] += 1
                categories[actual_cat]["evidence_ok"] += cmp["evidence"]["ok"]
            if "decision" in cmp:
                d = cmp["decision"]
                categories[cat]["decisions_total"] += 1
                categories[cat]["decisions_ok"] += d["captured"]
                categories[cat]["decisions_late"] += d["late"]
                if "owner" in d["label"]:
                    categories[cat]["owner_total"] += 1
                    categories[cat]["owner_ok"] += d["owner_ok"] is True
            if "retained" in cmp:
                categories[cat]["retained_total"] += 1
                categories[cat]["retained_ok"] += cmp["retained"]["ok"]
            if "closed" in cmp:
                categories[cat]["closed_total"] += 1
                categories[cat]["closed_ok"] += cmp["closed"]["ok"]
            if not cmp["ok"]:
                mismatches.append({"scenario": run["scenario"], "run": run["run"], "line": record["line"],
                                   "speaker": record["speaker"], "expected": cmp["expected"], "actual": cmp["actual"],
                                   "failed": [key for key in ("attribution", "evidence", "retained", "closed")
                                              if key in cmp and not cmp[key]["ok"]]
                                   + (["decision"] if "decision" in cmp and not cmp["decision"]["captured"] else [])
                                   + (["policy"] if not cmp["policy_ok"] else [])
                                   + (["error"] if cmp["error"] else [])})
    return {"model": model, "runs": len(runs), "scenarios": len({r["scenario"] for r in runs}), "lines": lines,
            "policy_ok": policy_ok, "errors": errors, "unexpected_decisions": unexpected, "categories": categories,
            "latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95),
                           "max": max(latencies) if latencies else None, "n": len(latencies)},
            "usage": usage, "cost_usd": estimate_cost(model, usage), "mismatches": mismatches}


def _ratio(ok: int, total: int) -> str:
    return f"{ok}/{total}" if total else "-"


def format_summary(summary: dict, effort: str) -> str:
    cats = summary["categories"]
    head = (f"== {summary['model']} (effort {effort}): {summary['scenarios']} scenario(s), {summary['runs']} run(s), "
            f"{summary['lines']} lines, {summary['errors']} error(s)")
    header = (f"{'category':<20} {'expected':>8} {'missed':>6} {'unnecessary':>11} {'attribution':>11} {'evidence':>8} "
              f"{'decisions':>9} {'late':>4} {'owner':>5} {'retained':>8} {'closed':>6}")
    rows = [head, header]
    for name in CATEGORIES:
        c = cats[name]
        rows.append(f"{name:<20} {c['expected']:>8} {c['missed']:>6} {c['unnecessary']:>11} "
                    f"{_ratio(c['attribution_ok'], c['attribution_total']):>11} "
                    f"{_ratio(c['evidence_ok'], c['evidence_total']):>8} "
                    f"{_ratio(c['decisions_ok'], c['decisions_total']):>9} {c['decisions_late']:>4} "
                    f"{_ratio(c['owner_ok'], c['owner_total']):>5} {_ratio(c['retained_ok'], c['retained_total']):>8} "
                    f"{_ratio(c['closed_ok'], c['closed_total']):>6}")
    lat = summary["latency_ms"]
    usage = summary["usage"]
    cost = summary["cost_usd"]
    rows.append(f"policy agreement {summary['policy_ok']}/{summary['lines']} lines; unexpected decisions "
                f"{summary['unexpected_decisions']}; latency p50 {lat['p50']} ms, p95 {lat['p95']} ms, max {lat['max']} ms "
                f"({lat['n']} analyses)")
    rows.append(f"tokens: input {usage['input_tokens']}, cache read {usage['cache_read_input_tokens']}, cache write "
                f"{usage['cache_creation_input_tokens']}, output {usage['output_tokens']}; estimated cost "
                f"{'$' + format(cost, '.4f') if cost is not None else 'n/a (model not in the price table)'}")
    for m in summary["mismatches"]:
        rows.append(f"  {m['scenario']} run {m['run']} L{m['line']} {m['speaker']}: expected {'|'.join(m['expected'])}, "
                    f"got {m['actual']}; failed {', '.join(m['failed'])}")
    return "\n".join(rows)


# --- CLI -------------------------------------------------------------------------------------------------------------------

def _print_line(scenario: Scenario, line: Line, record: dict) -> None:
    cmp, outcome = record["comparison"], record["outcome"]
    mark = "ok  " if cmp["ok"] else "MISS"
    detail = f"{cmp['actual']}" + (f"[{','.join(cmp['actual_types'])}]" if cmp["actual_types"] else "")
    extra = []
    if outcome["error"]:
        extra.append(f"error: {outcome['error']}")
    for key in ("attribution", "evidence", "retained", "closed"):
        if key in cmp and not cmp[key]["ok"]:
            extra.append(f"{key} failed")
    if "decision" in cmp:
        extra.append("decision " + ("captured" if cmp["decision"]["captured"] else "missed"))
    print(f"  {mark} {scenario.name} L{line.index} {line.speaker}: {detail} (expected {'|'.join(line.expect)}) "
          f"{outcome['latency_ms']} ms{'; ' + '; '.join(extra) if extra else ''}", flush=True)


def list_scenarios(scenarios: list[Scenario]) -> str:
    rows = []
    for s in scenarios:
        rows.append(f"{s.name}: {s.title} [{s.package}, {len(s.lines)} lines, {len(s.clarifications)} clarification(s)]")
        for line in s.lines:
            labels = []
            if line.issue_types:
                labels.append(f"types {'/'.join(line.issue_types)}")
            if line.decision:
                labels.append("decision")
            if line.open_questions_contain:
                labels.append("retained")
            if line.closed_contain:
                labels.append("closed")
            rows.append(f"  L{line.index} {line.speaker}: expect {'|'.join(line.expect)}"
                        f"{' (' + ', '.join(labels) + ')' if labels else ''}")
    return "\n".join(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python scripts/alignment_eval.py",
                                     description="Live evaluation of the Claude alignment analysis on labelled scenarios.")
    parser.add_argument("--models", default=None, help="comma-separated model ids (default: the settings' model)")
    parser.add_argument("--scenario", action="append", default=None, help="scenario name (repeatable; default: all)")
    parser.add_argument("--repeat", type=int, default=1, help="runs per scenario and model (default 1)")
    parser.add_argument("--effort", default=None, choices=("low", "medium", "high"), help="output_config.effort")
    parser.add_argument("--timeout", type=float, default=None, help="seconds per request (default: the settings')")
    parser.add_argument("--out", default=None, help=f"output directory (default {OUT_DIR})")
    parser.add_argument("--list", action="store_true", help="print the scenarios and their labels, then exit")
    parser.add_argument("--fake", action="store_true", help="harness smoke test with an empty-result FakeAnalyzer (no API)")
    return parser


async def evaluate(models: list[str], scenarios: list[Scenario], *, settings: Settings, effort: str, repeat: int,
                   out_dir: Path, fake: bool, quiet: bool = False) -> list[dict]:
    """Every model over every scenario `repeat` times; one JSON per model in `out_dir`; the summaries."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    summaries = []
    for model in models:
        if fake:
            analyzer: semantic.Analyzer = semantic.FakeAnalyzer(model=model)
        else:
            analyzer = semantic.ClaudeAnalyzer(replace(settings, alignment_model=model, alignment_effort=effort))
        runs: list[dict] = []
        for scenario in scenarios:
            for n in range(1, repeat + 1):
                runs.append(await run_scenario(scenario, analyzer, run=n, on_line=None if quiet else _print_line))
        summary = summarize(model, runs)
        summaries.append(summary)
        path = out_dir / f"{stamp}-{model}.json"
        path.write_text(json.dumps({"model": model, "effort": effort, "fake": fake, "generated_at": stamp,
                                    "settings": {"timeout_s": settings.alignment_timeout_s,
                                                 "max_tokens": settings.alignment_max_tokens},
                                    "prices_usd_per_mtok": PRICES_USD_PER_MTOK.get(model), "summary": summary,
                                    "scenarios": runs}, indent=1, ensure_ascii=False), encoding="utf-8")
        if not quiet:
            print(format_summary(summary, effort))
            print(f"written: {path}")
    return summaries


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        scenarios = load_scenarios(args.scenario)
    except ScenarioError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.list:
        print(list_scenarios(scenarios))
        return 0
    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2
    settings = load_settings(alignment_effort=args.effort, alignment_timeout_s=args.timeout)
    if not args.fake and not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set: put it in the repo-root .env (see .env.example; Anthropic billing, separate "
              "from Boson) or run with --fake for a harness smoke test without API calls.", file=sys.stderr)
        return 2
    models = [m.strip() for m in (args.models or settings.alignment_model).split(",") if m.strip()]
    if not models:
        print("--models names no model", file=sys.stderr)
        return 2
    out_dir = Path(args.out).resolve() if args.out else OUT_DIR
    for scenario in scenarios:
        try:
            _, package = load_package(scenario.package)
        except Exception as exc:  # noqa: BLE001 - a broken package is reported, not traced
            print(f"{scenario.path}: package {scenario.package}: {exc}", file=sys.stderr)
            return 1
        problems = check_scenario(scenario, package)
        if problems:
            print(ScenarioError(scenario.path, problems), file=sys.stderr)
            return 1
    print(f"alignment eval: {len(scenarios)} scenario(s) x {len(models)} model(s) x {args.repeat} run(s) = "
          f"{sum(len(s.lines) for s in scenarios) * len(models) * args.repeat} analyses"
          f"{' (fake analyzer: no API calls)' if args.fake else f' (effort {settings.alignment_effort})'}", flush=True)
    asyncio.run(evaluate(models, scenarios, settings=settings, effort=settings.alignment_effort, repeat=args.repeat,
                         out_dir=out_dir, fake=args.fake))
    return 0


if __name__ == "__main__":
    sys.exit(main())
