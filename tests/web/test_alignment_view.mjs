// Model-based alignment on the deck (contract v5 §4): the real index.html + main.js in the fake browser with a meeting
// talk whose alignment is Claude's. The Meeting info line and the topbar badge (ready, analysing, unavailable, legacy,
// off; the last analysis), the claim and assumption chips, and a card of each kind of issue: an evidence conflict
// (the claim against the passage, the sources with the model's quotes, the question; pending → asked → partly
// settled → resolved, the decision), a hidden disagreement (each participant's words and reading, no source;
// withdrawn), an ambiguous decision (the proposal and its missing detail, never a source; a silent suggestion that
// becomes the intervention's card; asked, then open when nobody answers), the backend's issue list catching a card
// up, and a legacy-mode event with the kind added (as today, plus the kind in the title).
//   jsc -m tests/web/test_alignment_view.mjs   (from the repo root; tests/web/run.sh runs everything)
import { advance, clock, env, install, settle } from "./fake_browser.mjs";
import { int16ToBase64 } from "../../web/js/util.js";

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

const point = (slide, index, text) => ({ index, text, fragment: null, duration_ms: 2000, words: [], sentences: [], audio: `/media/${slide}/${index}.wav` });
const REQ2 = "Stage one of the rollout: on Friday 24 October the beta release goes to the beta group only.";
const REQ3 = "Stage two of the rollout: the production release goes to all customers on Wednesday 29 October.";
const MEETING = {
  title: "Release 4.3 rollout review",
  host: "Alice",
  participants: [
    { name: "Alice", role: "Product manager" },
    { name: "Bob", role: "Customer communications" },
    { name: "Carol", role: "Engineering lead" },
  ],
  phases: [
    { id: "presentation", title: "Rollout plan", prompt: "" },
    { id: "discussion", title: "Launch announcement", prompt: "What goes out on Friday, and who sends it?" },
    { id: "wrapup", title: "Decisions and next steps", prompt: "" },
  ],
  documents: [
    { id: "requirements", title: "Rollout plan v3", statements: [{ id: "REQ-2", text: REQ2 }, { id: "REQ-3", text: REQ3 }] },
    { id: "decisions", title: "Decisions log", statements: [{ id: "DEC-2", text: "Announcements follow the release stage and go only to that stage's audience." }] },
  ],
  discussion_slide: "discussion",
};
const CLAUDE = { mode: "claude", model: "claude-opus-5-5", available: true };
const RUNNER = { ...CLAUDE, in_flight: false, waiting: false, last_latency_ms: null, last_error: null, analyses: 0, last_policy: null };
env.talk = {
  id: "rollout-review",
  version: "v1",
  presenter: "Alice",
  voice: "default",
  mic_mode: "tap",
  narration_mode: "live",
  fake_audio: false,
  slides: [
    { id: "agenda", index: 0, speaker: "human", mode: null, title: "Release 4.3 rollout review", fragments: 0, chunks: [] },
    { id: "plan", index: 1, speaker: "relay", mode: null, title: "The rollout plan", fragments: 0, chunks: [point("plan", 0, "Friday is beta.")] },
    { id: "discussion", index: 2, speaker: "human", mode: null, title: "Launch announcement", fragments: 0, chunks: [] },
  ],
  meeting: MEETING,
  alignment: CLAUDE,
};
for (const s of env.talk.slides) for (const c of s.chunks) env.media.set(c.audio, 2);

const { doc } = install(read("web/index.html"));
const $ = (sel) => doc.querySelector(sel);
const $$ = (sel) => doc.querySelectorAll(sel);
const text = (sel) => ($(sel) ? $(sel).textContent.replace(/\s+/g, " ").trim() : null);
const t = (el) => el.textContent;
const sock = () => env.socket();
const sent = (type) => sock().sent.filter((m) => m.type === type);
const server = (msg) => sock().receive(msg);
const selected = () => doc.querySelectorAll(".tab").filter((x) => x.getAttribute("aria-selected") === "true").map((x) => x.dataset.tab);
const node = () => env.worklets["pcm-player"];
const SECOND = int16ToBase64(new Int16Array(24000).fill(800));
let checkpoint = { slide_id: "agenda", talking_point_id: null, status: "idle" };
const state = (st, extra = {}) =>
  server({ type: "state", state: st, phase: "presenting", slide: "plan", banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null, agenda_phase: "presentation", status: checkpoint.status, talking_point: checkpoint.talking_point_id, checkpoint, ...extra });
const base = { title: MEETING.title, host: "Alice", started: true, participants: MEETING.participants, clarifications: [], decisions: [], action_items: [], open_questions: [], notes: null };
const agenda = (phase) => MEETING.phases.map((p) => ({ ...p, current: p.id === phase }));
const meetingMsg = (phase, extra = {}) => server({ type: "meeting", ...base, phase, agenda: agenda(phase), checkpoint, ...extra });
const hello = (extra = {}) =>
  server({ type: "hello", talk: "rollout-review", version: "v1", mic_mode: "tap", narration_mode: "live", meeting: MEETING, snapshot: { state: "IDLE", phase: "opening", slide: "agenda", connection: { status: "ready", detail: null }, agenda_phase: "presentation", speaker: null }, ...extra });
const utterance = (id, speaker, words, at_s, source = "voice") => server({ type: "transcript", utterance: { id, text: words, speaker, at_s, phase: "discussion", source } });
const alignment = (event) => server({ type: "alignment", event });
const chipOf = (c) => `${c.textContent}:${c.dataset.tone}`;
const rowText = (d) => [...d.children].map((c) => c.textContent).join(" ");
const prevUtt = (card) => {
  const items = $(".transcript").children;
  return items[items.indexOf(card) - 1].dataset.utt;
};
const spoken = (reply) => {
  server({ type: "reply_start", reply, kind: "intervention" });
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "intervening" };
  state("INTERVENING", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  for (let i = 0; i < 2; i++) server({ type: "reply_audio", reply, pcm: SECOND });
  server({ type: "reply_end", reply });
  node().emit({ type: "drained", reply, played: 48000, time: clock.now / 1000 });
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "listening" };
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
};
const BOB = "I'll draft the announcement for Friday's production launch to all customers.";
const CL1 = { id: "CL-1", question: "Wait, I thought Friday was production. Which one is it?", answer: "The approved rollout plan says beta on Friday and production next Wednesday.", source_ids: ["REQ-2", "REQ-3"], at_s: 41.2, by: "auto" };

async function run() {
  await import("../../web/js/main.js");
  await settle();
  sock().open();

  // ---- Meeting info: who checks the discussion (from /api/talk, the hello, state and meeting)
  eq("Meeting info: the Alignment line from /api/talk", [$(".strip-alignment").hidden, text(".alignment-text"), $(".alignment-badge").dataset.status, $(".strip-analysis").hidden, $(".alignment-pill").hidden], [false, "Claude (claude-opus-5-5) · ready", "ready", true, true]);
  hello({ alignment: { ...CLAUDE, available: false } });
  eq("the hello says no key: unavailable, and the topbar badge", [text(".alignment-text"), $(".alignment-badge").dataset.status, $(".alignment-pill").hidden, text(".alignment-pill")], ["Claude (claude-opus-5-5) · unavailable (no key)", "unavailable", false, "Alignment unavailable"]);
  state("IDLE", { alignment: { ...RUNNER, in_flight: true } });
  eq("a state with an analysis in flight: analysing, the badge goes", [text(".alignment-text"), $(".alignment-badge").dataset.status, $(".alignment-pill").hidden, $(".strip-analysis").hidden], ["Claude (claude-opus-5-5) · analysing…", "analysing", true, true]);
  meetingMsg("presentation", { alignment: { ...RUNNER, last_latency_ms: 1840, analyses: 1, last_policy: { issue_id: "IS1", decision: "clarify", reason: "a cited passage, clarification or decision; confirming; high confidence" } } });
  eq("a meeting with the last analysis: ready, the latency and the policy decision", [text(".alignment-text"), $(".alignment-badge").dataset.status, $(".strip-analysis").hidden, text(".analysis-label"), text(".analysis-text")], [
    "Claude (claude-opus-5-5) · ready",
    "ready",
    false,
    "Last analysis:",
    "1.84 s (1 so far) · policy: IS1: clarify (a cited passage, clarification or decision; confirming; high confidence)",
  ]);
  hello({ alignment: CLAUDE });
  eq("a hello again (mode, model, available only) keeps the last analysis", [text(".alignment-text"), $(".strip-analysis").hidden], ["Claude (claude-opus-5-5) · ready", false]);
  state("IDLE", { alignment: { ...RUNNER, analyses: 2, last_latency_ms: 2100, last_error: "timeout after 20 s", last_policy: "silent" } });
  eq("an error in the last analysis", text(".analysis-text"), "2.10 s (2 so far) · policy: silent · error: timeout after 20 s");

  // ---- join, hand over to the discussion
  $(".arm-button").fire("click");
  await settle(10);
  advance(700);
  eq("joined", sent("arm").length, 1);
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "listening" };
  server({ type: "slide", slide: "discussion", fragment: null });
  server({ type: "mic", open: true });
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  meetingMsg("discussion", { clarifications: [CL1] });
  eq("the discussion: the Transcript tab", selected(), ["transcript"]);

  // ---- claim and assumption chips
  utterance("U1", "Alice", "Production is next Wednesday, right?", 90.1);
  utterance("U2", "Bob", BOB, 95.5);
  alignment({ type: "claim", utterance_id: "U1", text: "Production is next Wednesday", kind: "question", agrees: true, evidence_ids: ["REQ-3"], label: "" });
  alignment({ type: "claim", utterance_id: "U2", text: "The announcement goes to all customers on Friday", kind: "proposal", agrees: false, evidence_ids: ["REQ-2", "DEC-2"], label: "" });
  alignment({ type: "claim", utterance_id: "U2", text: "Bob drafts the announcement", kind: "assertion", agrees: null, evidence_ids: [], label: "" });
  alignment({ type: "assumption", speaker: "Bob", utterance_id: "U2", topic: "Friday's release", interpretation: "Friday is the production release" });
  eq("claim chips: the model's text, green / amber / grey; the assumption grey", [$$('[data-utt="U1"] .claim').map(chipOf), $$('[data-utt="U2"] .claim').map(chipOf)], [
    ["Production is next Wednesday:agree"],
    ["The announcement goes to all customers on Friday:conflict", "Bob drafts the announcement:open", "assumes: Friday is the production release:open"],
  ]);
  eq("…the assumption chip is marked, and says what it is", [$('[data-utt="U2"] .claim[data-kind="assumption"]').title, $('[data-utt="U2"] .claim').title], [
    "Bob seems to assume this about Friday's release: an attributed reading, not an established fact",
    "Differs from the sources (proposal)",
  ]);
  alignment({ type: "assumption", speaker: "Bob", utterance_id: "U2", topic: "Friday's release", interpretation: "Friday is the production release" });
  eq("…the same assumption again adds nothing", $$('[data-utt="U2"] .claim').length, 3);

  // ---- an evidence conflict: the claim against the passage; pending → asked → partly settled → resolved
  const EV1 = [
    { kind: "source", id: "REQ-2", quote: "the beta release goes to the beta group only", text: REQ2 },
    { kind: "clarification", id: "CL-1", quote: "beta on Friday and production next Wednesday", text: CL1.answer },
  ];
  const IS1 = {
    issue_id: "IS1", issue_type: "evidence_conflict", topic: "Friday's announcement audience", label: "Friday's announcement audience", slot: "IS1",
    claim: "The announcement goes to all customers on Friday", claimed_label: "The announcement goes to all customers on Friday",
    evidence: EV1, participant_views: [], missing_details: [], source_ids: ["REQ-2"], clarification_ids: ["CL-1"], decision_ids: [],
    established_label: "the beta release goes to the beta group only", commitment: "proposed", confidence: "high", recommendation: "silent",
  };
  alignment({ id: "AL1", type: "conflict", ...IS1, utterance_id: "U2", at_s: 95.5, quote: BOB, quote_speaker: "Bob", quote_utterance_id: "U2", policy: "silent", has_action: false, repeat: false });
  eq("a conflict chip names the topic and the kind", $$('[data-utt="U2"] .claim').map(chipOf).at(-1), "Friday's announcement audience · evidence conflict:conflict");
  eq("…and no card yet (the policy was silent)", $$(".align-card").length, 0);
  utterance("U3", "Carol", "Sounds good. Let's lock that in.", 99.0);
  alignment({
    id: "AL2", type: "intervene", ...IS1, commitment: "confirming", recommendation: "clarify", conflict_id: "AL1", conflict_ids: ["AL1"], related: [],
    question: "Has the audience changed? The plan I have says beta users only on Friday.", quote: BOB, quote_utterance_id: "U2", quote_speaker: "Bob",
    confirm_quote: "Sounds good. Let's lock that in.", utterance_id: "U3", at_s: 99.0, query: "Friday's announcement audience The announcement goes to all customers on Friday", revision: 3,
  });
  const card1 = () => $('.align-card[data-issue-type="evidence_conflict"]');
  check("the evidence-conflict card", card1() !== null);
  eq("…title, kind, pending", [text(".align-card .align-title"), card1().dataset.kind, card1().dataset.status, text(".align-card .align-status")], ["Alignment check · evidence conflict", "intervention", "pending", "Pending"]);
  eq("…after Carol's line", prevUtt(card1()), "U3");
  eq("…heard: Bob's words, and when", [text(".align-card .align-heard .align-label"), text(".align-card .align-heard .align-quote")], ["Heard · Bob · 01:35", `“${BOB}”`]);
  eq("…the claim against what the source says", $$(".align-diff").map(rowText), ["Friday's announcement audience The announcement goes to all customers on Friday ≠ the beta release goes to the beta group only"]);
  eq("…Sources say: the passage and the earlier clarification, with the model's quotes", [$(".align-sources").hidden, $$(".align-ref").map((r) => `${r.querySelector(".src-chip").textContent}:${r.dataset.kind}:${r.querySelector(".align-ref-title").textContent}:${r.querySelector(".align-ref-text").textContent}`)], [
    false,
    ["REQ-2:source:Rollout plan v3:“the beta release goes to the beta group only”", "CL-1:clarification:Earlier clarification:“beta on Friday and production next Wednesday”"],
  ]);
  eq("…the question, in its own block", [$(".align-question").hidden, text(".align-question .align-label"), text(".align-question-text")], [false, "Question", "Has the audience changed? The plan I have says beta users only on Friday."]);
  eq("…before confirming: Carol", [text(".align-confirm .align-label"), text(".align-confirm .align-quote")], ["Before confirming · Carol", "“Sounds good. Let's lock that in.”"]);
  eq("…no views, proposal or missing details on an evidence conflict", [$(".align-views").hidden, $(".align-proposed").hidden, $(".align-missing").hidden, $(".align-diffs").hidden], [true, true, true, false]);
  spoken(7);
  eq("the facilitator speaks: Asked", [card1().dataset.status, text(".align-card .align-status")], ["asked", "Asked"]);
  utterance("U4", "Carol", "Beta users only on Friday. Which channel we use is still open.", 110.2);
  alignment({
    type: "resolved", issue_id: "IS1", intervene_id: "AL2", conflict_id: "AL1", label: "Friday's announcement audience", partial: true,
    settled: ["audience: beta users only"], remaining: ["channel"], note: "Beta users only; the channel is still open", value_label: "Beta users only; the channel is still open",
    utterance_ids: ["U4"], owner: null, plan_changed: false, follow_up_quote: "Beta users only on Friday. Which channel we use is still open.",
  });
  eq("partly settled: what was settled, what is still open, no owner", [card1().dataset.status, text(".align-card .align-status"), $$(".align-result").map(t), $$(".align-note").map(t), $(".align-outcome .owner-chip")], [
    "partial",
    "Partly settled",
    ["Partly settled · Friday's announcement audience: Beta users only; the channel is still open"],
    ["Settled: audience: beta users only", "Still open: channel"],
    null,
  ]);
  utterance("U5", "Alice", "Email, and Bob sends it.", 112.0);
  alignment({
    type: "resolved", issue_id: "IS1", intervene_id: "AL2", conflict_id: "AL1", label: "Friday's announcement audience", partial: false,
    settled: ["audience: beta users only", "channel: email"], remaining: [], note: "Beta users only, by email", value_label: "Beta users only, by email",
    utterance_ids: ["U4", "U5"], owner: "Bob", plan_changed: false, follow_up_quote: "Email, and Bob sends it.",
  });
  eq("then settled: the line rewritten, nothing still open, the owner", [card1().dataset.status, text(".align-card .align-status"), $$(".align-result").map(t), $$(".align-note").map(t), text(".align-outcome .owner-chip")], [
    "resolved",
    "Resolved",
    ["Confirmed · Friday's announcement audience: Beta users only, by email"],
    ["Settled: audience: beta users only; channel: email"],
    "Owner: Bob",
  ]);
  alignment({ type: "decision_draft", issue_ids: ["IS1"], decision: "Friday's announcement goes to beta users only, by email", owner: "Bob", follow_up: "Bob sends it", source_ids: ["REQ-2"], utterance_ids: ["U4", "U5"], plan_changed: false, supersedes: null, by: "analysis" });
  eq("the decision it drafted, on the card", text(".align-decision"), "To the notes: Friday's announcement goes to beta users only, by email");

  // ---- a hidden disagreement: each participant's words and reading, no source; withdrawn before it was asked
  utterance("U6", "Alice", "So Friday's release is the beta, and we announce it then.", 120.0);
  utterance("U7", "Bob", "Right, Friday's release goes out to everyone.", 122.0);
  utterance("U8", "Carol", "Great, Friday it is.", 124.0);
  const IS2 = {
    issue_id: "IS2", issue_type: "hidden_disagreement", topic: "Friday's release", label: "Friday's release", slot: "IS2", claim: "Friday's release", claimed_label: "Friday's release",
    evidence: [{ kind: "utterance", id: "U6", quote: "Friday's release is the beta" }, { kind: "utterance", id: "U7", quote: "goes out to everyone" }],
    participant_views: [
      { speaker: "Alice", utterance_id: "U6", quote: "Friday's release is the beta", view: "beta release" },
      { speaker: "Bob", utterance_id: "U7", quote: "goes out to everyone", view: "production release" },
    ],
    missing_details: [], source_ids: [], clarification_ids: [], decision_ids: [], established_label: "", commitment: "confirming", confidence: "medium", recommendation: "clarify",
  };
  alignment({ id: "AL3", type: "conflict", ...IS2, utterance_id: "U7", at_s: 122, quote: "Right, Friday's release goes out to everyone.", quote_speaker: "Bob", quote_utterance_id: "U7", policy: "clarify", has_action: true, repeat: false });
  alignment({
    id: "AL4", type: "intervene", ...IS2, conflict_id: "AL3", conflict_ids: ["AL3"], related: [], question: "Before we confirm Friday, do we mean the beta release or the production release?",
    quote: "Right, Friday's release goes out to everyone.", quote_utterance_id: "U7", quote_speaker: "Bob", confirm_quote: "Great, Friday it is.", utterance_id: "U8", at_s: 124, query: "Friday's release", revision: 8,
  });
  const card2 = () => $('.align-card[data-issue-type="hidden_disagreement"]');
  const in2 = (sel) => card2().querySelector(sel);
  check("the hidden-disagreement card", card2() !== null);
  eq("…title, after Carol's line, pending", [in2(".align-title").textContent, prevUtt(card2()), in2(".align-status").textContent], ["Alignment check · hidden disagreement", "U8", "Pending"]);
  eq("…heard: Bob's line", in2(".align-heard .align-label").textContent, "Heard · Bob · 02:02");
  eq("…Views: each participant's own words and what they take them to mean", [in2(".align-views").hidden, card2().querySelectorAll(".align-view").map(t)], [
    false,
    ["Alice · “Friday's release is the beta” → beta release", "Bob · “goes out to everyone” → production release"],
  ]);
  eq("…no sources (none settles it), no heard-vs-established row, no proposal", [in2(".align-sources").hidden, in2(".align-diffs").hidden, card2().querySelectorAll(".align-ref").length, in2(".align-proposed").hidden, in2(".align-missing").hidden], [true, true, 0, true, true]);
  eq("…the question", in2(".align-question-text").textContent, "Before we confirm Friday, do we mean the beta release or the production release?");
  eq("…the conflict chip on Bob's line", $$('[data-utt="U7"] .claim').map(chipOf), ["Friday's release · hidden disagreement:conflict"]);
  utterance("U9", "Bob", "Sorry, I meant the beta. Beta only on Friday.", 126.0);
  alignment({ type: "withdrawn", issue_id: "IS2", intervene_id: "AL4", conflict_id: "AL3", label: "Friday's release", note: "Bob corrected himself: the beta release", utterance_ids: ["U9"] });
  eq("Bob corrects himself first: withdrawn", [card2().dataset.status, in2(".align-status").textContent, card2().querySelectorAll(".align-result").map(t)], ["withdrawn", "Withdrawn", ["Withdrawn · Friday's release: Bob corrected himself: the beta release"]]);

  // ---- an ambiguous decision: a silent suggestion with the proposal and its missing detail, never a source; then
  //      the intervention takes the card over; asked; open when nobody answers (from the backend's issue list)
  utterance("U10", "Carol", "We deploy the new model on Monday.", 130.0);
  utterance("U11", "Alice", "Agreed, Monday.", 131.0);
  const IS3 = {
    issue_id: "IS3", issue_type: "ambiguous_decision", topic: "Rollback owner for Monday's deployment", label: "Rollback owner for Monday's deployment", slot: "IS3",
    claim: "Deploy the new model on Monday", claimed_label: "Deploy the new model on Monday",
    evidence: [{ kind: "source", id: "REQ-3", quote: "the production release", text: REQ3 }], participant_views: [], missing_details: ["who owns rollback if the deployment fails"],
    source_ids: ["REQ-3"], clarification_ids: [], decision_ids: [], established_label: "", commitment: "nearing", confidence: "medium", recommendation: "suggest",
  };
  alignment({ id: "AL5", type: "conflict", ...IS3, utterance_id: "U10", at_s: 130, quote: "We deploy the new model on Monday.", quote_speaker: "Carol", quote_utterance_id: "U10", policy: "suggest", has_action: true, repeat: false });
  alignment({
    id: "AL6", type: "suggestion", ...IS3, conflict_id: "AL5", conflict_ids: ["AL5"], related: [], question: "Who owns rollback if Monday's deployment fails?",
    quote: "We deploy the new model on Monday.", quote_utterance_id: "U10", quote_speaker: "Carol", utterance_id: "U11", at_s: 131, query: "Rollback owner", revision: 11,
  });
  const card3 = () => $('.align-card[data-issue-type="ambiguous_decision"]');
  const in3 = (sel) => card3().querySelector(sel);
  check("the ambiguous-decision card, as a silent suggestion", card3() !== null);
  eq("…dashed, 'Possible issue (not raised)', Silent, after Alice's line", [card3().dataset.kind, in3(".align-title").textContent, card3().dataset.status, in3(".align-status").textContent, prevUtt(card3())], [
    "suggestion",
    "Possible issue (not raised)",
    "suggestion",
    "Silent",
    "U11",
  ]);
  eq("…Proposed and Unresolved", [in3(".align-proposed").hidden, in3(".align-proposed-text").textContent, in3(".align-missing").hidden, card3().querySelectorAll(".align-missing-item").map(t)], [
    false,
    "Deploy the new model on Monday",
    false,
    ["who owns rollback if the deployment fails"],
  ]);
  eq("…never a source that supposedly specifies the owner; no heard-vs-established row, no views", [in3(".align-sources").hidden, card3().querySelectorAll(".align-ref").length, in3(".align-diffs").hidden, in3(".align-views").hidden], [true, 0, true, true]);
  eq("…the question", in3(".align-question-text").textContent, "Who owns rollback if Monday's deployment fails?");
  eq("…heard: Carol", in3(".align-heard .align-label").textContent, "Heard · Carol · 02:10");
  eq("three cards so far", $$(".align-card").length, 3);
  utterance("U12", "Bob", "Monday it is, let's go.", 133.0);
  alignment({
    id: "AL7", type: "intervene", ...IS3, commitment: "confirming", recommendation: "clarify", conflict_id: "AL5", conflict_ids: ["AL5"], related: [], question: "Who owns rollback if Monday's deployment fails?",
    quote: "We deploy the new model on Monday.", quote_utterance_id: "U10", quote_speaker: "Carol", confirm_quote: "Monday it is, let's go.", utterance_id: "U12", at_s: 133, query: "Rollback owner", revision: 12,
  });
  eq("the suggestion becomes the intervention's card: one card, retitled, pending, after Bob's line, with his line", [$$(".align-card").length, card3().dataset.kind, in3(".align-title").textContent, in3(".align-status").textContent, prevUtt(card3()), in3(".align-confirm .align-quote").textContent], [
    3,
    "intervention",
    "Alignment check · unresolved detail",
    "Pending",
    "U12",
    "“Monday it is, let's go.”",
  ]);
  eq("…still one missing detail, still no source", [card3().querySelectorAll(".align-missing-item").length, card3().querySelectorAll(".align-ref").length], [1, 0]);
  spoken(9);
  eq("asked; the settled cards don't move", [in3(".align-status").textContent, card1().dataset.status, card2().dataset.status], ["Asked", "resolved", "withdrawn"]);
  for (const [id, at] of [["U13", 140], ["U14", 141], ["U15", 142], ["U16", 143], ["U17", 144]]) utterance(id, null, "Mm-hm.", at);
  const ISSUES = [
    { id: "IS1", issue_type: "evidence_conflict", topic: "Friday's announcement audience", status: "resolved", claim: "", question: "" },
    { id: "IS2", issue_type: "hidden_disagreement", topic: "Friday's release", status: "withdrawn", claim: "", question: "" },
    { id: "IS3", issue_type: "ambiguous_decision", topic: "Rollback owner for Monday's deployment", status: "open", claim: "Deploy the new model on Monday", question: "Who owns rollback if Monday's deployment fails?" },
  ];
  meetingMsg("discussion", { clarifications: [CL1], issues: ISSUES });
  eq("nobody answered: the backend's issue list opens the card", [card3().dataset.status, in3(".align-status").textContent, card1().dataset.status, card2().dataset.status], ["open", "Open", "resolved", "withdrawn"]);
  meetingMsg("discussion", { clarifications: [CL1], issues: [{ ...ISSUES[2], status: "asked" }, { ...ISSUES[0], status: "pending" }] });
  eq("…and never takes a card back", [card3().dataset.status, card1().dataset.status], ["open", "resolved"]);
  meetingMsg("discussion", { clarifications: [CL1], issues: [{ ...ISSUES[2], status: "partially_resolved" }] });
  eq("…only forward (settled later, caught up from the record)", [card3().dataset.status, in3(".align-status").textContent], ["partial", "Partly settled"]);
  alignment({ id: "AL6b", type: "suggestion", ...IS3, conflict_id: "AL5", conflict_ids: ["AL5"], related: [], question: "Who owns rollback?", quote: "We deploy the new model on Monday.", quote_utterance_id: "U10", quote_speaker: "Carol", utterance_id: "U17", at_s: 144 });
  eq("a suggestion about an issue with a card adds no second card", $$(".align-card").length, 3);

  // ---- legacy mode as the controller now forwards it: a configured slot and values, plus the kind and topic
  utterance("U18", "Bob", "And the beta notes go to all customers too.", 150);
  const LEGACY = { slot: "friday_audience", label: "Friday's audience", issue_type: "evidence_conflict", topic: "Friday's audience", claimed: "all_customers", claimed_label: "all customers", established: "beta_users", established_label: "beta users", source_ids: ["DEC-2"], clarification_ids: [], decision_ids: [] };
  alignment({ id: "AL8", type: "conflict", ...LEGACY, utterance_id: "U18", at_s: 150, has_action: true, sentence: "And the beta notes go to all customers too.", repeat: false });
  eq("a legacy conflict with the kind added: the chip as today", $$('[data-utt="U18"] .claim').map(chipOf), ["Friday's audience: all customers:conflict"]);
  utterance("U19", "Carol", "Fine, let's finalize that.", 151);
  alignment({
    id: "AL9", type: "intervene", ...LEGACY, utterance_id: "U19", at_s: 151, conflict_id: "AL8", conflict_ids: ["AL8"], related: [],
    quote: "And the beta notes go to all customers too.", quote_utterance_id: "U18", quote_speaker: "Bob", confirm_quote: "Fine, let's finalize that.", has_action: true, query: "Friday's audience all customers beta users",
  });
  const card4 = () => $$(".align-card").at(-1);
  const in4 = (sel) => card4().querySelector(sel);
  eq("a legacy intervention with the kind: the card as today, the kind in its title", [in4(".align-title").textContent, prevUtt(card4()), card4().querySelectorAll(".align-diff").map(rowText), card4().querySelectorAll(".align-ref").map((r) => `${r.querySelector(".src-chip").textContent}:${r.dataset.kind}:${r.querySelector(".align-ref-title").textContent}`), in4(".align-ref-text").textContent.slice(0, 16), in4(".align-question").hidden, in4(".align-views").hidden, in4(".align-proposed").hidden], [
    "Alignment check · evidence conflict",
    "U19",
    ["Friday's audience all customers ≠ beta users"],
    ["DEC-2:source:Decisions log"],
    "“Announcements f",
    true,
    true,
    true,
  ]);
  alignment({ id: "AL10", type: "resolved", ...LEGACY, utterance_id: "U20", at_s: 160, conflict_id: "AL8", intervene_id: "AL9", value: "beta_users", value_label: "beta users", plan_changed: true, owner: "", follow_up_quote: "Beta users, and we change the plan.", utterance_ids: ["U18", "U20"] });
  eq("…resolved as today (a plan change)", [card4().dataset.status, card4().querySelectorAll(".align-result").map(t), card4().querySelectorAll(".align-note").length], ["resolved", ["Plan changed · Friday's audience: beta users"], 0]);

  // ---- the other modes in Meeting info
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion", alignment: { ...RUNNER, mode: "legacy", model: "" } });
  eq("--alignment legacy: 'legacy patterns', no badge, no analysis line", [text(".alignment-text"), $(".alignment-badge").dataset.status, $(".alignment-pill").hidden, $(".strip-analysis").hidden], ["legacy patterns", "legacy", true, true]);
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion", alignment: { mode: "off", model: "", available: false } });
  eq("--alignment off", [text(".alignment-text"), $(".alignment-badge").dataset.status, $(".alignment-pill").hidden], ["off", "off", true]);
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion", alignment: { ...RUNNER, available: false } });
  eq("Claude mode with no analyzer, from a state: the badge", [text(".alignment-text"), $(".alignment-pill").hidden], ["Claude (claude-opus-5-5) · unavailable (no key)", false]);

  // ---- a restart: the cards go, the status line stays
  checkpoint = { slide_id: "agenda", talking_point_id: null, status: "idle" };
  state("IDLE", { agenda_phase: "presentation", phase: "opening", slide: "agenda", alignment: RUNNER });
  eq("a restart: the cards and chips go, the Alignment line stays", [$$(".align-card").length, $$(".claim").length, $(".strip-alignment").hidden, text(".alignment-text"), $(".alignment-pill").hidden], [0, 0, false, "Claude (claude-opus-5-5) · ready", true]);

  eq("no console errors", env.errors, []);
  print(`alignment view: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} alignment view check(s) failed`);
}

try {
  await run();
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
