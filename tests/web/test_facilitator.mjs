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
eq("speakers: participants, then the room", F.speakerChoices(info.participants).map((c) => [c.name, c.label]), [["Alice", "Alice"], ["Bob", "Bob"], [null, "Room"]]);
eq("the facilitator's own reply kinds", ["intervention", "decision", "summary", "handover", "answer", "narration"].map((k) => F.FACILITATOR_KINDS.has(k)), [true, true, true, true, false, false]);

print(`facilitator.js: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} facilitator check(s) failed`);
