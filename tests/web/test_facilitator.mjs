// Unit tests for web/js/facilitator.js (On the Same Page, contract v3 §6–§7): the normalisers of the meeting package
// and the new messages (as relay/meeting.py, relay/alignment.py and the controller send them), and the words the
// agenda strip, the tiles and the panels show.
//   jsc -m tests/web/test_facilitator.mjs   (from the repo root; tests/web/run.sh runs everything)
import * as F from "../../web/js/facilitator.js";

let passed = 0;
let failed = 0;
function check(name, ok, detail = "") {
  if (ok) passed++;
  else {
    failed++;
    print(`FAIL ${name}${detail ? `: ${detail}` : ""}`);
  }
}
function eq(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  check(name, g === w, `got ${g}, want ${w}`);
}

// ---- the meeting package (controller.meeting_info: hello.meeting, /api/talk.meeting)
const raw = {
  title: "Release 4.3 rollout review",
  host: "Alice",
  participants: [{ name: "Alice", role: "Product manager" }, { name: "Bob", role: "Customer communications" }, { name: "" }, null],
  phases: [
    { id: "presentation", title: "Rollout plan", prompt: "" },
    { id: "discussion", title: "Launch announcement", prompt: "What goes out on Friday, and who sends it?" },
    { id: "wrapup", title: "Decisions and next steps", prompt: "" },
  ],
  documents: [{ id: "requirements", title: "Rollout plan v3", statements: [{ id: "REQ-2", text: "Friday: beta to the beta group." }, { text: "no id" }] }],
  discussion_slide: "discussion",
};
const info = F.normalizeMeetingInfo(raw);
eq("participants (nameless ones dropped)", info.participants, [{ name: "Alice", role: "Product manager" }, { name: "Bob", role: "Customer communications" }]);
eq("phases", info.phases.map((p) => [p.id, p.title]), [["presentation", "Rollout plan"], ["discussion", "Launch announcement"], ["wrapup", "Decisions and next steps"]]);
eq("documents: statements with ids only", info.documents, [{ id: "requirements", title: "Rollout plan v3", statements: [{ id: "REQ-2", text: "Friday: beta to the beta group." }] }]);
eq("title, host, discussion slide", [info.title, info.host, info.discussion_slide], ["Release 4.3 rollout review", "Alice", "discussion"]);
eq("a plain talk: null", [F.normalizeMeetingInfo(null), F.normalizeMeetingInfo(undefined), F.normalizeMeetingInfo([]), F.normalizeMeetingInfo("x")], [null, null, null, null]);
const sparse = F.normalizeMeetingInfo({});
eq("a sparse package: the three default phases, no documents", [sparse.phases.map((p) => p.title), sparse.documents, sparse.discussion_slide], [["Presentation", "Discussion", "Wrap-up"], [], null]);

// ---- the meeting state (MeetingState.to_json() + type + checkpoint, flat)
const msg = {
  type: "meeting",
  title: "Release 4.3 rollout review",
  host: "Alice",
  phase: "discussion",
  started: true,
  agenda: [
    { id: "presentation", title: "Rollout plan", prompt: "", current: false },
    { id: "discussion", title: "Launch announcement", prompt: "What goes out on Friday?", current: true },
    { id: "wrapup", title: "Decisions and next steps", prompt: "", current: false },
  ],
  participants: [{ name: "Alice", role: "Product manager" }],
  checkpoint: { slide_id: "discussion", talking_point_id: null, status: "listening" },
  clarifications: [{ id: "CL-1", question: "Is Friday production?", answer: "Friday is beta; production is Wednesday.", source_ids: ["REQ-2", "REQ-3"], at_s: 41.2, by: "auto" }],
  decisions: [{ id: "D1", decision: "Friday: beta release; announcement to beta users only", owner: "Bob", follow_up: "Bob notifies beta users only.", source_ids: ["REQ-2", "DEC-2"], at_s: 130.5, by: "model", supersedes: null }],
  action_items: [{ id: "A1", owner: "Bob", text: "Bob notifies beta users only.", decision_id: "D1" }],
  open_questions: [{ id: "Q1", text: "Who briefs support?", at_s: 140, resolved: false }],
  notes: null,
};
const st = F.normalizeMeetingState(msg, info);
eq("phase and agenda", [st.phase, st.agenda.map((p) => p.id)], ["discussion", ["presentation", "discussion", "wrapup"]]);
eq("checkpoint", st.checkpoint, { slide_id: "discussion", talking_point_id: null, status: "listening" });
eq("clarification", st.clarifications[0], { id: "CL-1", question: "Is Friday production?", answer: "Friday is beta; production is Wednesday.", source_ids: ["REQ-2", "REQ-3"], at_s: 41.2, by: "auto" });
eq("decision", [st.decisions[0].owner, st.decisions[0].by, st.decisions[0].supersedes, st.decisions[0].source_ids], ["Bob", "model", null, ["REQ-2", "DEC-2"]]);
eq("action item, open question", [st.action_items[0], st.open_questions[0].resolved], [{ id: "A1", owner: "Bob", text: "Bob notifies beta users only.", decision_id: "D1" }, false]);
eq("no notes before the wrap-up; no transcript or lookups in the message", [st.notes, st.transcript, st.lookups], [null, null, null]);
const nested = F.normalizeMeetingState({ type: "meeting", meeting: { phase: "wrapup", notes: { title: "x", phase: "wrapup", generated_at_s: 200.25, decisions: [] } } }, info);
eq("nested under `meeting`; notes at the wrap-up; the agenda from the package", [nested.phase, nested.notes, nested.agenda.length], ["wrapup", { title: "x", phase: "wrapup", generated_at_s: 200.25 }, 3]);
const odd = F.normalizeMeetingState({ decisions: [{ decision: "no id" }, 7], clarifications: "x", checkpoint: "x" }, null);
eq("odd fields: nothing breaks", [odd.phase, odd.decisions, odd.clarifications, odd.checkpoint, odd.agenda.length], ["presentation", [], [], null, 3]);

// ---- evidence, transcript, alignment
const lookup = F.normalizeLookup({
  type: "evidence",
  lookup: { id: "E1", query: "Friday rollout", results: [{ source_id: "REQ-2", kind: "source", title: "Rollout plan v3", excerpt: "Friday: beta.", score: 3.2 }, { kind: "x" }], by: "model", purpose: "answer", at_s: 40 },
});
eq("lookup", lookup, { id: "E1", query: "Friday rollout", results: [{ source_id: "REQ-2", kind: "source", title: "Rollout plan v3", excerpt: "Friday: beta.", score: 3.2 }], by: "model", purpose: "answer", at_s: 40 });
eq("a flat lookup works too", F.normalizeLookup({ id: "E2", query: "q", results: [] }).by, "model");
const utt = F.normalizeUtterance({ type: "transcript", utterance: { id: "U3", text: " I'll draft it. ", speaker: "Bob", at_s: 95.5, phase: "discussion", source: "typed" } });
eq("utterance", utt, { id: "U3", text: "I'll draft it.", speaker: "Bob", at_s: 95.5, phase: "discussion", source: "typed" });
eq("an unlabelled utterance", F.normalizeUtterance({ id: "U4", text: "x", speaker: null }).speaker, null);
const ev = F.normalizeEvent({ type: "alignment", event: { id: "AL5", type: "intervene", slot: "friday_stage", label: "Friday's release stage", utterance_id: "U4", at_s: 99, conflict_id: "AL2", source_ids: ["REQ-2", 3], clarification_ids: ["CL-1"], related: [{ conflict_id: "AL3" }] } });
eq("event: common fields, id lists cleaned, the rest kept", [ev.id, ev.type, ev.utterance_id, ev.source_ids, ev.clarification_ids, ev.conflict_id, ev.related.length], ["AL5", "intervene", "U4", ["REQ-2"], ["CL-1"], "AL2", 1]);
eq("an event without a label: the slot", F.normalizeEvent({ type: "claim", slot: "friday_stage" }).label, "friday_stage");
eq("a legacy event: slot and label as given, no issue, no records", [ev.issue_type, ev.issue_id, ev.topic, ev.evidence, ev.participant_views], ["", null, "", [], []]);
// model-based alignment (contract v5 §4): an event names its issue; slot and label fall back to it
const modelEv = F.normalizeEvent({
  type: "alignment",
  event: {
    id: "AL9", type: "conflict", issue_id: "IS2", issue_type: "hidden_disagreement", topic: "Friday's release", claim: "Friday's release goes to everyone",
    evidence: [{ kind: "utterance", id: "U5", quote: "the beta" }, { kind: "x" }, null, { kind: "source", quote: "no id" }],
    participant_views: [{ speaker: "Alice", utterance_id: "U5", quote: "Friday's release is the beta", view: "beta release" }, { utterance_id: "U6" }],
    missing_details: ["owner", "", 3], conflict_ids: ["AL2", 3],
  },
});
eq("a model-derived event: slot = the issue id, label = the topic", [modelEv.slot, modelEv.label, modelEv.issue_id, modelEv.issue_type, modelEv.topic], ["IS2", "Friday's release", "IS2", "hidden_disagreement", "Friday's release"]);
eq(
  "…evidence and views as records (no id, empty: dropped), the string lists cleaned",
  [modelEv.evidence.map((r) => [r.kind, r.id, r.quote, r.text, r.speaker]), modelEv.participant_views, modelEv.missing_details, modelEv.conflict_ids],
  [[["utterance", "U5", "the beta", "", null]], [{ speaker: "Alice", utterance_id: "U5", quote: "Friday's release is the beta", view: "beta release" }], ["owner"], ["AL2"]],
);
eq("an event with neither slot nor issue id: its own id", [F.normalizeEvent({ type: "intervene", id: "AL3" }).slot, F.normalizeEvent({ type: "intervene", id: "AL3" }).label], ["AL3", "AL3"]);
eq("a claim with none of them: empty (its chip is keyed by its text)", [F.normalizeEvent({ type: "claim", text: "x" }).slot, F.normalizeEvent({ type: "claim", text: "x" }).label], ["", ""]);
eq("a label given wins over the topic", F.normalizeEvent({ type: "conflict", issue_id: "IS1", topic: "t", label: "L" }).label, "L");
eq(
  "issue kinds and card titles",
  [F.cardTitle({ issue_type: "evidence_conflict" }), F.cardTitle({ issue_type: "hidden_disagreement" }), F.cardTitle({ issue_type: "ambiguous_decision" }), F.cardTitle({ issue_type: "" }), F.cardTitle({})],
  ["Alignment check · evidence conflict", "Alignment check · hidden disagreement", "Alignment check · unresolved detail", "Alignment check", "Alignment check"],
);
eq("an event that doesn't say its kind is an evidence conflict", [F.issueTypeOf({}), F.issueTypeOf({ issue_type: "zzz" }), F.issueTypeOf({ issue_type: "ambiguous_decision" })], ["evidence_conflict", "evidence_conflict", "ambiguous_decision"]);
eq("the suggestion title", F.SUGGESTION_TITLE, "Possible issue (not raised)");
eq("card statuses from the backend's", ["pending", "suggested", "asked", "partially_resolved", "resolved", "withdrawn", "open", "zzz"].map(F.cardStatus), ["pending", "suggestion", "asked", "partial", "resolved", "withdrawn", "open", ""]);
eq("status words", Object.values(F.CARD_STATUS_WORDS), ["Pending", "Asked", "Resolved", "Partly settled", "Withdrawn", "Open", "Silent"]);
eq(
  "a card moves forward only",
  [F.cardMoves("asked", "open"), F.cardMoves("partial", "resolved"), F.cardMoves("resolved", "open"), F.cardMoves("pending", "suggestion"), F.cardMoves("pending", "withdrawn"), F.cardMoves("open", "pending"), F.cardMoves("asked", "zzz")],
  [true, true, false, false, true, false, false],
);
// the alignment status (state.alignment, meeting.alignment; hello and /api/talk: mode, model, available only)
const brief = F.normalizeAlignment({ mode: "claude", model: "claude-opus-5-5", available: true });
eq("alignment status (hello, /api/talk): mode, model, available; not full", [brief.mode, brief.model, brief.available, brief.full, brief.last_latency_ms, brief.last_policy], ["claude", "claude-opus-5-5", true, false, null, ""]);
const full = F.normalizeAlignment({ mode: "claude", model: "claude-opus-5-5", available: true, in_flight: false, waiting: false, last_latency_ms: 1840, last_error: null, analyses: 3, last_policy: { issue_id: "IS1", decision: "clarify", reason: "a cited passage; confirming; high confidence" } });
eq("…the runner's record (state, meeting): full, the policy as words", [full.full, full.last_latency_ms, full.analyses, full.last_policy], [true, 1840, 3, "IS1: clarify (a cited passage; confirming; high confidence)"]);
eq("…a policy as a string, missing, or without an issue", [F.normalizeAlignment({ mode: "claude", last_policy: "silent" }).last_policy, F.normalizeAlignment({ mode: "claude" }).last_policy, F.normalizeAlignment({ mode: "claude", last_policy: { policy: "suggest" } }).last_policy], ["silent", "", "suggest"]);
eq("…none", [F.normalizeAlignment(null), F.normalizeAlignment("x"), F.normalizeAlignment([])], [null, null, null]);
eq("the Meeting info line: ready, with the last analysis", F.alignmentWords(full), {
  status: "ready",
  text: "Claude (claude-opus-5-5) · ready",
  detail: "1.84 s (3 so far) · policy: IS1: clarify (a cited passage; confirming; high confidence)",
  title: "Alignment: the Claude API (claude-opus-5-5) reads the discussion; Higgs Realtime transcribes and speaks",
});
eq("…analysing (in flight or waiting)", [F.alignmentWords({ ...full, in_flight: true }).status, F.alignmentWords({ ...full, waiting: true }).text], ["analysing", "Claude (claude-opus-5-5) · analysing…"]);
eq("…unavailable (no key)", [F.alignmentWords({ ...brief, available: false }).status, F.alignmentWords({ ...brief, available: false }).text, F.alignmentWords({ ...brief, available: false }).detail], ["unavailable", "Claude (claude-opus-5-5) · unavailable (no key)", ""]);
eq("…an error, a sub-second latency", F.alignmentWords({ ...full, last_latency_ms: 640, analyses: null, last_policy: "", last_error: "HTTP 429" }).detail, "640 ms · error: HTTP 429");
eq("…legacy patterns, off, none", [F.alignmentWords(F.normalizeAlignment({ mode: "legacy", available: true })), F.alignmentWords(F.normalizeAlignment({ mode: "off" })).text, F.alignmentWords(null).status], [
  { status: "legacy", text: "legacy patterns", detail: "", title: "Alignment: the legacy pattern checks of checks.yaml, not the model" },
  "off",
  "off",
]);
const withIssues = F.normalizeMeetingState({ ...msg, issues: [{ id: "IS1", issue_type: "evidence_conflict", topic: "Friday's audience", status: "asked", claim: "all customers", question: "Beta users only?" }, { topic: "no id" }], alignment: { mode: "claude", model: "m", available: true, in_flight: true } }, info);
eq("meeting state: the issues and the alignment status (none in a message without them)", [withIssues.issues, withIssues.alignment.in_flight, st.issues, st.alignment], [
  [{ id: "IS1", issue_type: "evidence_conflict", topic: "Friday's audience", status: "asked", claim: "all customers", question: "Beta users only?" }],
  true,
  [],
  null,
]);

// ---- words
eq("phase button: presentation", F.phaseButton("presentation"), { label: "Start discussion", title: "End the presentation: the facilitator hands over and listens to the discussion", disabled: false });
eq("phase button: discussion", [F.phaseButton("discussion").label, F.phaseButton("discussion").disabled], ["Wrap up", false]);
eq("phase button: wrap-up", [F.phaseButton("wrapup").label, F.phaseButton("wrapup").disabled], ["Done", true]);
const slides = [{ id: "agenda" }, { id: "plan" }, { id: "rollback" }];
eq("checkpoint text", F.checkpointText({ slide_id: "plan", talking_point_id: "friday-beta", status: "presenting" }, slides), "Slide 2 · friday-beta · presenting");
eq("checkpoint without a talking point", F.checkpointText({ slide_id: "rollback", talking_point_id: null, status: "wrapping_up" }, slides), "Slide 3 · wrapping up");
eq("checkpoint of an unknown slide, and none", [F.checkpointWhere({ slide_id: "zzz", talking_point_id: null, status: "idle" }, slides), F.checkpointText(null, slides)], ["zzz", "idle"]);
eq("who looked it up", [F.lookupBy({ by: "model", purpose: "answer" }), F.lookupBy({ by: "engine", purpose: "intervention" })], ["Facilitator looked up", "Alignment check"]);
eq("meeting time", [F.atText(95.5), F.atText(3725), F.atText(null)], ["01:35", "1:02:05", ""]);
eq("value labels", [F.valueLabel("all_customers"), F.valueLabel(null)], ["all customers", ""]);
const index = F.sourceIndex(info, st, [lookup]);
eq("source index: statements, clarifications, decisions", [index.get("REQ-2").title, index.get("CL-1").kind, index.get("CL-1").text, index.get("D1").kind], ["Rollout plan v3", "clarification", "Friday is beta; production is Wednesday.", "decision"]);
eq("id kinds: known, else by shape", [F.idKind("REQ-2", index), F.idKind("CL-7"), F.idKind("D3"), F.idKind("DEC-1")], ["source", "clarification", "decision", "source"]);
eq("claim chip: agrees", F.claimChip({ type: "claim", label: "Friday's release stage", value: "beta", value_label: "beta", agrees: true }), { text: "Friday's release stage: beta", tone: "agree", title: "Matches the sources" });
eq("claim chip: nothing established", F.claimChip({ type: "claim", label: "L", value: "x_y", agrees: null }).tone, "open");
eq("conflict chip", F.claimChip({ type: "conflict", label: "Friday's audience", claimed: "all_customers", established: "beta_users" }), { text: "Friday's audience: all customers", tone: "conflict", title: "Heard all customers; the sources say beta users" });
// model-based alignment: a claim is its text; an assumption a grey "assumes: …"; a conflict names its topic and kind
eq(
  "claim chip: the model's text, by whether it agrees",
  [F.claimChip({ type: "claim", label: "", text: "The announcement goes to all customers", kind: "proposal", agrees: false }), F.claimChip({ type: "claim", label: "", text: "Production is Wednesday", kind: "assertion", agrees: true }).tone, F.claimChip({ type: "claim", label: "", text: "We could ship Monday", kind: "proposal", agrees: null }).tone],
  [{ text: "The announcement goes to all customers", tone: "conflict", title: "Differs from the sources (proposal)" }, "agree", "open"],
);
eq("assumption chip", F.claimChip({ type: "assumption", speaker: "Bob", topic: "Friday's release", interpretation: "Friday is the production release" }), {
  text: "assumes: Friday is the production release",
  tone: "open",
  title: "Bob seems to assume this about Friday's release: an attributed reading, not an established fact",
});
eq(
  "a model-derived conflict chip names the topic and the kind",
  [
    F.claimChip({ type: "conflict", issue_id: "IS1", issue_type: "evidence_conflict", label: "Friday's audience", claimed_label: "all customers", established_label: "the beta group only" }),
    F.claimChip({ type: "conflict", issue_id: "IS2", issue_type: "hidden_disagreement", label: "Friday's release", claimed_label: "Friday's release goes to everyone", established_label: "" }).text,
    F.claimChip({ type: "conflict", issue_id: "IS3", issue_type: "ambiguous_decision", label: "Rollback owner", claim: "Deploy Monday" }).title,
  ],
  [{ text: "Friday's audience · evidence conflict", tone: "conflict", title: "Heard all customers; the sources say the beta group only" }, "Friday's release · hidden disagreement", "Heard Deploy Monday; a material detail is unresolved"],
);
eq("speakers: participants, then the room", F.speakerChoices(info.participants).map((c) => [c.name, c.label]), [["Alice", "Alice"], ["Bob", "Bob"], [null, "Room"]]);
eq("the facilitator's own reply kinds", ["intervention", "decision", "summary", "handover", "answer", "narration"].map((k) => F.FACILITATOR_KINDS.has(k)), [true, true, true, true, false, false]);

print(`facilitator.js: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} facilitator check(s) failed`);
