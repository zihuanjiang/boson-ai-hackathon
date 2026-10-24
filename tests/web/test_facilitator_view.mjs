// On the Same Page on the deck (contract v3 §6–§7): the real index.html + main.js in the fake browser with a meeting
// talk (talks/rollout-review's shape). The names, the join dialog, the agenda strip and its phase button, the
// checkpoint, the tabbed side panel (Evidence with the documents, Notes, Transcript with claim chips and the alignment
// card, Chat) and its unread badges, the evidence callout, the speaker picker, the room mic, Restart meeting, the new
// tile statuses, and the brief §3 discussion played as messages.
//   jsc -m tests/web/test_facilitator_view.mjs   (from the repo root; tests/web/run.sh runs everything)
import { advance, clock, env, frame, install, settle } from "./fake_browser.mjs";
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
    {
      id: "requirements",
      title: "Rollout plan v3",
      statements: [
        { id: "REQ-2", text: "Stage one of the rollout: on Friday 24 October the beta release goes to the beta group only." },
        { id: "REQ-3", text: "Stage two of the rollout: the production release goes to all customers on Wednesday 29 October." },
      ],
    },
    { id: "decisions", title: "Decisions log", statements: [{ id: "DEC-2", text: "Announcements follow the release stage and go only to that stage's audience." }, { id: "DEC-3", text: "Bob owns customer communications." }] },
  ],
  discussion_slide: "discussion",
};
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
    { id: "plan", index: 1, speaker: "relay", mode: null, title: "The rollout plan", fragments: 0, chunks: [point("plan", 0, "Friday is beta."), point("plan", 1, "Wednesday is production.")] },
    { id: "rollback", index: 2, speaker: "relay", mode: null, title: "Rollback", fragments: 0, chunks: [point("rollback", 0, "The trigger.")] },
    { id: "discussion", index: 3, speaker: "human", mode: null, title: "Launch announcement", fragments: 0, chunks: [] },
  ],
  meeting: MEETING,
};
for (const s of env.talk.slides) for (const c of s.chunks) env.media.set(c.audio, 2);

const { doc } = install(read("web/index.html"));
const $ = (sel) => doc.querySelector(sel);
const $$ = (sel) => doc.querySelectorAll(sel);
const text = (sel) => ($(sel) ? $(sel).textContent.replace(/\s+/g, " ").trim() : null);
const sock = () => env.socket();
const sent = (type) => sock().sent.filter((m) => m.type === type);
const lastSent = (type) => sent(type).at(-1);
const server = (msg) => sock().receive(msg);
const keys = () => sent("key").map((k) => k.key);
const press = (key, code = "") =>
  fireWindow("keydown", { type: "keydown", key, code, repeat: false, target: doc.activeElement ?? doc.body, timeStamp: clock.now, preventDefault() {} });
const status = () => $(".tile-relay .tile-status").textContent;
const tab = (name) => doc.querySelector(`.tab[data-tab="${name}"]`);
const badge = (name) => (tab(name).querySelector(".tab-badge").hidden ? 0 : Number(tab(name).querySelector(".tab-badge").textContent));
const selected = () => doc.querySelectorAll(".tab").filter((t) => t.getAttribute("aria-selected") === "true").map((t) => t.dataset.tab);
const node = () => env.worklets["pcm-player"];
const posted = () => node().port.posted.map((m) => `${m.type}:${m.reply}`);
const SECOND = int16ToBase64(new Int16Array(24000).fill(800));
const micFrame = (rms = 0.1) => env.worklets["mic-capture"].emit({ type: "frame", pcm: new Int16Array(2400), rms });
let checkpoint = { slide_id: "plan", talking_point_id: "friday-beta", status: "presenting" };
const state = (st, extra = {}) =>
  server({ type: "state", state: st, phase: "presenting", slide: "plan", banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null, agenda_phase: "presentation", status: checkpoint.status, talking_point: checkpoint.talking_point_id, checkpoint, ...extra });
const base = { title: MEETING.title, host: "Alice", started: true, participants: MEETING.participants, clarifications: [], decisions: [], action_items: [], open_questions: [], notes: null };
const agenda = (phase) => MEETING.phases.map((p) => ({ ...p, current: p.id === phase }));
const meetingMsg = (phase, extra = {}) => server({ type: "meeting", ...base, phase, agenda: agenda(phase), checkpoint, ...extra });
const CL1 = { id: "CL-1", question: "Wait, I thought Friday was production. Which one is it?", answer: "The approved rollout plan says beta on Friday and production next Wednesday.", source_ids: ["REQ-2", "REQ-3"], at_s: 41.2, by: "auto" };
const utterance = (id, speaker, words, at_s, source = "voice") => server({ type: "transcript", utterance: { id, text: words, speaker, at_s, phase: "discussion", source } });
const alignment = (event) => server({ type: "alignment", event });
const STAGE = { slot: "friday_stage", label: "Friday's release stage" };
const AUDIENCE = { slot: "friday_audience", label: "Friday's audience" };
const BOB = "I'll draft the announcement for Friday's production launch to all customers.";

async function run() {
  await import("../../web/js/main.js");
  await settle();
  sock().open();

  // ---- a meeting talk: On the Same Page's names, before joining
  eq("brand", text(".brand-name"), "On the Same Page");
  eq("page title", doc.title, "On the Same Page · Release 4.3 rollout review");
  eq("topbar: the meeting's title", text(".talk-name"), "Release 4.3 rollout review");
  eq("the AI's tile", [text(".tile-relay .name"), text(".tile-relay .role")], ["Facilitator", "AI facilitator"]);
  eq("…is waiting for the host", status(), "Waiting for Alice");
  eq("the room's tile", [text(".tile-you .name"), text(".tile-you .role"), text(".tile-you .avatar-text")], ["Room", "Alice · Bob · Carol", "R"]);
  eq("join dialog", [text(".panel-title"), text(".panel-sub"), text('[data-item="server"] .label')], ["Release 4.3 rollout review", "The facilitator presents · Alice hosts", "Connected to the facilitator"]);
  eq("the AI's name in the menu, the keys and the left card", [...$$(".ai-name"), ...$$(".ai-name-cap")].map((e) => e.textContent), ["the facilitator", "the facilitator", "the facilitator", "the facilitator", "the facilitator", "The facilitator"]);
  eq("presenter = the host", $$(".presenter-name").map((e) => e.textContent), ["Alice", "Alice"]);
  eq("viewing", text(".viewing-text"), "You are viewing the facilitator's screen");
  eq("captions' speaker labels", [$(".cap-live .cap-speaker").textContent, $(".cap-narration .cap-speaker").textContent, $(".cap-user .cap-speaker").textContent], ["Facilitator", "Facilitator", "Room"]);
  eq("mic title names the facilitator", $(".mic-button").title, "Interrupt the facilitator and speak (Space)");
  check("the meeting grid has the agenda row", $(".meeting").classList.contains("has-agenda"));

  // ---- the agenda strip
  check("agenda strip shown", !$(".agenda").hidden);
  eq("three steps, the first current", $$(".agenda-step").map((li) => `${li.querySelector(".step-num").textContent} ${li.querySelector(".step-title").textContent} ${li.dataset.state}`), ["1 Rollout plan current", "2 Launch announcement next", "3 Decisions and next steps next"]);
  eq("phase button", [text(".phase-button"), $(".phase-button").disabled], ["Start discussion", false]);
  $(".phase-button").fire("click");
  eq("…before joining: the join dialog, no key", [sent("key").length, $(".overlay").hidden], [0, false]);

  // ---- the side panel opens on Evidence; the documents are listed
  check("side panel open (a meeting)", !$(".chat").hidden);
  eq("tabs instead of the chat's title", [$(".tabs").hidden, $(".chat-title").hidden], [false, true]);
  eq("Evidence selected", selected(), ["evidence"]);
  eq("…its pane alone shows", [$(".evidence-pane").hidden, $(".notes-pane").hidden, $(".transcript-pane").hidden, $(".chat-log").hidden], [false, true, true, true]);
  eq("documents", $$(".doc-title").map((d) => d.textContent), ["Rollout plan v3", "Decisions log"]);
  eq("…every statement with its id", $$(".statement .src-chip").map((c) => c.textContent), ["REQ-2", "REQ-3", "DEC-2", "DEC-3"]);
  eq("…counted", text(".docs-count"), "· 2 · 4 statements");
  eq("toolbar button names the panels", text(".chat-button .tb-label"), "Panels");
  check("Restart meeting in More", !$(".menu-reset").hidden);

  // ---- hello (the package again), join
  server({ type: "hello", talk: "rollout-review", version: "v1", mic_mode: "tap", narration_mode: "live", meeting: MEETING, snapshot: { state: "IDLE", phase: "opening", slide: "agenda", banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null, connection: { status: "ready", detail: null }, talk: "rollout-review", agenda_phase: "presentation", status: "idle", talking_point: null, checkpoint: { slide_id: "agenda", talking_point_id: null, status: "idle" }, speaker: null } });
  eq("checkpoint from the hello", [text(".checkpoint-where"), text(".checkpoint-status")], ["Slide 1", "idle"]);
  meetingMsg("presentation", { checkpoint: { slide_id: "agenda", talking_point_id: null, status: "idle" } });
  $(".arm-button").fire("click");
  await settle(10);
  advance(700);
  eq("joined", sent("arm").length, 1);

  // ---- presenting: the checkpoint follows
  server({ type: "slide", slide: "plan", fragment: null });
  state("NARRATING", { narration: "live" });
  eq("checkpoint: Slide 2 · friday-beta · presenting", [text(".checkpoint-where"), text(".checkpoint-status"), $(".checkpoint-status").dataset.status], ["Slide 2 · friday-beta", "presenting", "presenting"]);
  eq("tile", status(), "Presenting");
  server({ type: "reply_start", reply: 3, kind: "narration" });
  for (let i = 0; i < 4; i++) server({ type: "reply_audio", reply: 3, pcm: SECOND });

  // ---- an interruption: Space, the lookup → the Evidence tab and the callout, the answer, the clarification
  press(" ", "Space");
  eq("Space cuts the narration", [lastSent("key").key, posted().at(-1)], ["space", "flush:3"]);
  server({ type: "reply_flush", reply: 3 });
  server({ type: "mic", open: true });
  checkpoint = { slide_id: "plan", talking_point_id: "friday-beta", status: "answering" };
  state("LISTENING");
  eq("answering", [text(".checkpoint-status"), status(), $(".mic-button .tb-label").textContent, $(".cap-hint").textContent], ["answering", "Listening…", "Mute", "Space to send · Esc to cancel"]);
  server({ type: "mic", open: false });
  server({ type: "caption", role: "user", text: "Wait, I thought Friday was production. Which one is it?", final: true, reply: 4 });
  state("REPLYING", { thinking: true });
  server({ type: "tool", call_id: "c1", name: "lookup_evidence", args: { query: "Friday rollout" }, status: "applied" });
  server({ type: "evidence", lookup: { id: "E1", query: "Friday rollout", by: "model", purpose: "answer", at_s: 40.5, results: [{ source_id: "REQ-2", kind: "source", title: "Rollout plan v3", excerpt: MEETING.documents[0].statements[0].text, score: 4 }, { source_id: "REQ-3", kind: "source", title: "Rollout plan v3", excerpt: MEETING.documents[0].statements[1].text, score: 2 }] } });
  eq("Evidence: the lookup", [text(".lookup .lookup-by"), text(".lookup .lookup-query"), text(".lookup .lookup-time")], ["Facilitator looked up", "“Friday rollout”", "00:40"]);
  eq("…its sources: id, document, excerpt", $$(".lookup .result").map((r) => `${r.querySelector(".src-chip").textContent} | ${r.querySelector(".result-title").textContent} | ${r.querySelector(".result-text").textContent.slice(0, 20)}`), ["REQ-2 | Rollout plan v3 | Stage one of the rol", "REQ-3 | Rollout plan v3 | Stage two of the rol"]);
  check("…the empty note goes", $(".lookups-empty").hidden);
  eq("…the cited statements stand out in the documents", $$(".statement").map((s) => s.dataset.cited), ["true", "true", "false", "false"]);
  eq("…on the Evidence tab: nothing unread", badge("evidence"), 0);
  check("the callout over the share", !$(".callout").hidden);
  eq("…what was looked up, and the top sources", [$(".callout").dataset.kind, text(".callout-kind"), text(".callout-query"), $$(".callout-item .src-chip").map((c) => c.textContent)], ["evidence", "Evidence", "Friday rollout", ["REQ-2", "REQ-3"]]);
  check("…with the excerpt", text(".callout-item .callout-text").startsWith("Stage one of the rollout"));
  server({ type: "reply_start", reply: 5, kind: "answer" });
  state("REPLYING");
  eq("tile: answering", status(), "Answering");
  server({ type: "caption", role: "assistant", text: "The approved rollout plan says beta on Friday and production next Wednesday.", final: true, reply: 5 });
  advance(11_000);
  check("the callout is still up after 11 s", !$(".callout").hidden);
  advance(1_100);
  check("…gone after 12 s", $(".callout").hidden);
  eq("Chat got the question and the answer (unread: it isn't the open tab)", badge("chat"), 2);
  meetingMsg("presentation", { clarifications: [CL1] });
  eq("Notes: the clarification (unread)", [badge("notes"), text('[data-section="clarifications"] .note-text')], [1, "The approved rollout plan says beta on Friday and production next Wednesday."]);
  eq("…its sources and the auto tag", [$$('[data-section="clarifications"] .src-chip').map((c) => c.textContent), text('[data-section="clarifications"] .auto-tag')], [["REQ-2", "REQ-3"], "auto"]);
  eq("…no decision yet", text('[data-section="decisions"] .note-none'), "None yet");
  tab("notes").fire("click");
  eq("opening the Notes tab reads it", [selected(), badge("notes"), $(".notes-pane").hidden], [["notes"], 0, false]);
  tab("chat").fire("click");
  eq("Chat tab", [badge("chat"), $(".chat-log").hidden, $(".chat-line .chat-who") && doc.querySelectorAll(".chat-line .chat-who").map((w) => w.textContent).at(-1)], [0, false, "Facilitator"]);

  // ---- toolbar badge: the panel closed, everything unread counts
  $(".chat-button").fire("click");
  check("panel closed", $(".chat").hidden);
  server({ type: "evidence", lookup: { id: "E2", query: "rollback", by: "model", purpose: "answer", at_s: 60, results: [] } });
  eq("closed: unread on the tab and the toolbar", [badge("evidence"), $(".chat-button .tb-badge").hidden, $(".chat-button .tb-badge").textContent], [1, false, "1"]);
  eq("…a lookup with no source says so", text('.lookup[data-id="E2"] .result-none'), "No matching source");
  $(".chat-button").fire("click");
  eq("opened on Chat: the toolbar badge goes, Evidence keeps its own", [$(".chat-button .tb-badge").hidden, badge("evidence"), selected()], [true, 1, ["chat"]]);

  // ---- the phase button: hand over to the discussion
  const n = sent("key").length;
  $(".phase-button").fire("click");
  eq("Start discussion = key phase", [sent("key").length - n, lastSent("key").key], [1, "phase"]);
  check("…one click only (locked until the phase moves)", $(".phase-button").disabled);
  $(".phase-button").fire("click");
  eq("…a second click sends nothing", sent("key").length - n, 1);
  server({ type: "reply_start", reply: 6, kind: "handover" });
  state("REPLYING", { status: "presenting" });
  eq("tile while handing over", status(), "Handing over");
  server({ type: "caption", role: "assistant", text: "That's the plan. Over to you: what goes out on Friday, and who sends it?", final: true, reply: 6 });
  node().emit({ type: "drained", reply: 6, played: 48000, time: clock.now / 1000 });
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "listening" };
  server({ type: "slide", slide: "discussion", fragment: null });
  server({ type: "mic", open: true });
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion", banner: "Discussion: What goes out on Friday, and who sends it? — the facilitator is listening" });
  meetingMsg("discussion", { clarifications: [CL1] });
  eq("the strip: discussion current, presentation done", $$(".agenda-step").map((li) => `${li.querySelector(".step-num").textContent} ${li.dataset.state}`), ["✓ done", "2 current", "3 next"]);
  eq("…Wrap up, unlocked", [text(".phase-button"), $(".phase-button").disabled], ["Wrap up", false]);
  eq("…the discussion's question", [text(".agenda-prompt"), $(".agenda-prompt").hidden], ["“What goes out on Friday, and who sends it?”", false]);
  eq("…checkpoint", [text(".checkpoint-where"), text(".checkpoint-status")], ["Slide 4", "listening"]);
  eq("the panel switched to the Transcript", selected(), ["transcript"]);
  eq("tile: listening to the room", status(), "Listening to the room");
  eq("the room mic is on", [$(".mic-button .tb-label").textContent, $(".mic-button").getAttribute("aria-pressed"), $(".mic-button").dataset.room], ["Room mic on", "true", "true"]);
  eq("…the room's tile", [$(".tile-you .mic-ico").dataset.muted, text(".tile-you .tile-status")], ["false", "The facilitator is listening"]);
  eq("…captions: listening to the room", [$(".cap-tag").textContent, $(".cap-hint").textContent], ["Listening…", "Space asks the facilitator directly"]);
  check("the speaker picker shows", !$(".speakers").hidden);
  eq("…participants and the room, the room picked", $$(".speaker").map((b) => `${b.textContent}:${b.getAttribute("aria-checked")}`), ["Alice:false", "Bob:false", "Carol:false", "Room:true"]);
  eq("typed lines are the room's now", [$(".chat-input").placeholder, text(".typed-label")], ["Type a line for the room · Enter adds it", "Room says"]);

  // ---- the room mic streams; muted here it doesn't; Esc leaves it open
  let frames = sent("mic").length;
  micFrame();
  micFrame();
  eq("room mic frames go out", sent("mic").length - frames, 2);
  $(".mic-button").fire("click");
  eq("click: muted here", [$(".mic-button .tb-label").textContent, $(".mic-button").dataset.muted, text(".tile-you .tile-status"), $(".tile-you .mic-ico").dataset.muted], ["Room mic off", "true", "Room mic muted", "true"]);
  eq("…no key went out", lastSent("key").key, "phase");
  frames = sent("mic").length;
  micFrame();
  eq("…no frames", sent("mic").length - frames, 0);
  check("…captions stop saying it listens", $(".cap-tag").hidden);
  $(".mic-button").fire("click");
  micFrame();
  eq("click again: on, frames again", [$(".mic-button .tb-label").textContent, sent("mic").length - frames], ["Room mic on", 1]);
  press("Escape");
  frames = sent("mic").length;
  micFrame();
  eq("Esc doesn't close the room mic", [lastSent("key").key, sent("mic").length - frames, $(".mic-button .tb-label").textContent], ["esc", 1, "Room mic on"]);
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion", mic_mode: "hold" });
  const keyCount = sent("key").length;
  $(".mic-button").fire("pointerdown", { button: 0, pointerId: 1 });
  $(".mic-button").fire("pointerup", { pointerId: 1 });
  advance(200);
  eq("hold mode: the room mic is still a toggle, a press is no Space", [$(".mic-button .tb-label").textContent, sent("key").length - keyCount], ["Room mic on", 0]);
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion", mic_mode: "tap" });

  // ---- the brief §3 discussion: Alice asks (no conflict), Bob claims (two conflicts), Carol confirms → intervention
  doc.querySelectorAll(".speaker").find((b) => b.textContent === "Alice").fire("click");
  eq("Alice picked", [lastSent("speaker"), text(".tile-you .name"), text(".tile-you .role"), $(".cap-user .cap-speaker").textContent], [{ type: "speaker", name: "Alice" }, "Alice", "Product manager", "Alice"]);
  utterance("U1", "Alice", "Production is next Wednesday, right?", 90.1);
  eq("Transcript: the line, who, when", [text('[data-utt="U1"] .utt-who'), text('[data-utt="U1"] .utt-time'), text('[data-utt="U1"] .utt-text')], ["Alice", "01:30", "Production is next Wednesday, right?"]);
  check("…the empty note goes", $(".transcript-empty").hidden);
  eq("…and on the captions, labelled", [$(".cap-user-text").textContent, $(".cap-user .cap-speaker").textContent], ["Production is next Wednesday, right?", "Alice"]);
  check("a question: no chip", $('[data-utt="U1"] .utt-claims').hidden);
  doc.querySelectorAll(".speaker").find((b) => b.textContent === "Bob").fire("click");
  const typedBefore = sent("typed").length;
  $(".chat-input").value = BOB;
  $(".chat-compose").fire("submit");
  eq("a typed line carries the speaker", [sent("typed").length - typedBefore, lastSent("typed").text, lastSent("typed").speaker], [1, BOB, "Bob"]);
  eq("…and isn't a chat line (the transcript shows it)", doc.querySelectorAll(".chat-line .chat-text").some((t) => t.textContent === BOB), false);
  utterance("U2", "Bob", BOB, 95.5, "typed");
  alignment({ id: "AL1", type: "claim", ...STAGE, utterance_id: "U2", at_s: 95.5, value: "production", value_label: "production", sentence: BOB, agrees: false });
  alignment({ id: "AL2", type: "conflict", ...STAGE, utterance_id: "U2", at_s: 95.5, claimed: "production", claimed_label: "production", established: "beta", established_label: "beta", source_ids: ["REQ-2"], clarification_ids: ["CL-1"], decision_ids: [], has_action: true, sentence: BOB, repeat: false });
  alignment({ id: "AL3", type: "claim", ...AUDIENCE, utterance_id: "U2", at_s: 95.5, value: "all_customers", value_label: "all customers", sentence: BOB, agrees: false });
  alignment({ id: "AL4", type: "conflict", ...AUDIENCE, utterance_id: "U2", at_s: 95.5, claimed: "all_customers", claimed_label: "all customers", established: "beta_users", established_label: "beta users", source_ids: ["DEC-2"], clarification_ids: [], decision_ids: [], has_action: true, sentence: BOB, repeat: false });
  eq("Bob's line: typed, two amber claim chips", [text('[data-utt="U2"] .utt-source'), $$('[data-utt="U2"] .claim').map((c) => `${c.textContent}:${c.dataset.tone}`)], ["typed", ["Friday's release stage: production:conflict", "Friday's audience: all customers:conflict"]]);
  eq("…and the facilitator stays silent: no card yet", $$(".align-card").length, 0);
  doc.querySelectorAll(".speaker").find((b) => b.textContent === "Carol").fire("click");
  utterance("U3", "Carol", "Sounds good. Let's finalize that.", 99.0);
  alignment({
    id: "AL5", type: "intervene", ...STAGE, utterance_id: "U3", at_s: 99.0, conflict_id: "AL2", claimed: "production", claimed_label: "production", established: "beta", established_label: "beta",
    source_ids: ["REQ-2"], clarification_ids: ["CL-1"], decision_ids: [], quote: BOB, quote_utterance_id: "U2", quote_speaker: "Bob", confirm_quote: "Sounds good. Let's finalize that.", has_action: true,
    conflict_ids: ["AL2", "AL4"], related: [{ conflict_id: "AL4", ...AUDIENCE, claimed: "all_customers", claimed_label: "all customers", established: "beta_users", established_label: "beta users", source_ids: ["DEC-2"], clarification_ids: [], decision_ids: [], quote: BOB }],
    query: "Friday's release stage production beta Friday's audience all customers beta users",
  });
  const card = () => $(".align-card");
  check("the alignment card", card() !== null);
  eq("…right after Carol's line", (() => { const items = $(".transcript").children; return items[items.indexOf(card()) - 1].dataset.utt; })(), "U3");
  eq("…pending", [card().dataset.status, text(".align-card .align-status")], ["pending", "Pending"]);
  eq("…heard: Bob's sentence", [text(".align-heard .align-label"), text(".align-heard .align-quote")], ["Heard · Bob · 01:35", `“${BOB}”`]);
  eq("…both slots: heard vs the sources", $$(".align-diff").map((d) => [...d.children].map((c) => c.textContent).join(" ")), ["Friday's release stage production ≠ beta", "Friday's audience all customers ≠ beta users"]);
  eq("…the source and the earlier clarification, with their words", $$(".align-ref").map((r) => `${r.querySelector(".src-chip").textContent}:${r.dataset.kind}:${r.querySelector(".align-ref-title").textContent}`), ["REQ-2:source:Rollout plan v3", "CL-1:clarification:Earlier clarification", "DEC-2:source:Decisions log"]);
  check("…REQ-2's text", text('.align-ref[data-kind="source"] .align-ref-text').startsWith("“Stage one of the rollout"));
  check("…CL-1's answer", text('.align-ref[data-kind="clarification"] .align-ref-text').includes("beta on Friday and production next Wednesday"));
  eq("…the line that was about to confirm it", [text(".align-confirm .align-label"), text(".align-confirm .align-quote")], ["Before confirming · Carol", "“Sounds good. Let's finalize that.”"]);
  server({ type: "evidence", lookup: { id: "E3", query: "Friday's release stage production beta", by: "engine", purpose: "intervention", at_s: 99.1, results: [{ source_id: "CL-1", kind: "clarification", title: "Clarification", excerpt: CL1.answer, score: 5 }, { source_id: "REQ-2", kind: "source", title: "Rollout plan v3", excerpt: MEETING.documents[0].statements[0].text, score: 4 }] } });
  eq("the intervention's evidence over the share", [$(".callout").hidden, $(".callout").dataset.kind, text(".callout-kind"), $$(".callout-item .src-chip").map((c) => `${c.textContent}:${c.dataset.kind}`)], [false, "intervention", "Alignment check", ["CL-1:clarification", "REQ-2:source"]]);
  eq("…in Evidence too, as the alignment check's", [text('.lookup[data-id="E3"] .lookup-by'), badge("evidence")], ["Alignment check", 2]);
  const playing = node().port.posted.length;
  server({ type: "reply_start", reply: 7, kind: "intervention" });
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "intervening" };
  state("INTERVENING", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  for (let i = 0; i < 3; i++) server({ type: "reply_audio", reply: 7, pcm: SECOND });
  eq("the facilitator speaks up: the card is asked", [card().dataset.status, text(".align-card .align-status")], ["asked", "Asked"]);
  eq("…tile and checkpoint", [status(), text(".checkpoint-status")], ["Speaking up", "intervening"]);
  server({ type: "mic", open: true });
  eq("a (re)opened room mic doesn't cut the intervention", node().port.posted.slice(playing).some((m) => m.type === "flush"), false);
  eq("…the captions don't say listening while it speaks", $(".cap-tag").hidden, true);
  const INTERVENTION = "One clarification before we confirm: we established that Friday is a beta release, not production. Should Friday's announcement go only to beta users?";
  server({ type: "caption", role: "assistant", text: INTERVENTION, final: true, reply: 7 });
  const said = $$('.utt[data-who="facilitator"]').map((li) => `${li.dataset.kind}: ${li.querySelector(".utt-text").textContent}`);
  eq("the transcript has the hand-over, then the intervention's words after the card", said, [
    "handover: That's the plan. Over to you: what goes out on Friday, and who sends it?",
    `intervention: ${INTERVENTION}`,
  ]);
  eq("…the intervention is the newest line", [$(".transcript").lastElementChild.dataset.who, text('.transcript .utt:not([data-utt]) .utt-who')], ["facilitator", "Facilitator"]);
  node().emit({ type: "drained", reply: 7, played: 72000, time: clock.now / 1000 });
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "listening" };
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });

  // ---- Carol corrects it: resolved (owner Bob) → the decision draft → the decision in the notes
  utterance("U4", "Carol", "Yes, beta users only. Bob owns the announcement.", 110.2);
  alignment({ id: "AL6", type: "resolved", ...STAGE, utterance_id: "U4", at_s: 110.2, conflict_id: "AL2", intervene_id: "AL5", value: "beta", value_label: "beta", plan_changed: false, owner: "Bob", follow_up_quote: "Yes, beta users only. Bob owns the announcement.", utterance_ids: ["U2", "U4"] });
  alignment({ id: "AL7", type: "resolved", ...AUDIENCE, utterance_id: "U4", at_s: 110.2, conflict_id: "AL4", intervene_id: "AL5", value: "beta_users", value_label: "beta users", plan_changed: false, owner: "Bob", follow_up_quote: "Yes, beta users only. Bob owns the announcement.", utterance_ids: ["U2", "U4"] });
  alignment({ id: "AL8", type: "decision_draft", ...STAGE, utterance_id: "U4", at_s: 110.2, decision: "Friday's release stage: beta; Friday's audience: beta users", owner: "Bob", follow_up: "Yes, beta users only. Bob owns the announcement.", source_ids: ["REQ-2", "DEC-2"], clarification_ids: ["CL-1"], slots: ["friday_stage", "friday_audience"], conflict_ids: ["AL2", "AL4"], plan_changed: false, supersedes: null, utterance_ids: ["U2", "U4"] });
  eq("the card is resolved", [card().dataset.status, text(".align-card .align-status")], ["resolved", "Resolved"]);
  eq("…with the outcome and the owner", [$$(".align-result").map((r) => r.textContent), text(".align-outcome .owner-chip")], [["Confirmed · Friday's release stage: beta", "Confirmed · Friday's audience: beta users"], "Owner: Bob"]);
  eq("…and the decision it drafted", text(".align-decision"), "To the notes: Friday's release stage: beta; Friday's audience: beta users");
  server({ type: "reply_start", reply: 8, kind: "decision" });
  state("INTERVENING", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  eq("the facilitator confirms the decision", status(), "Noting the decision");
  server({ type: "reply_end", reply: 8 });
  node().emit({ type: "drained", reply: 8, played: 0, time: clock.now / 1000 });
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  const D1 = { id: "D1", decision: "Friday: beta release; announcement to beta users only", owner: "Bob", follow_up: "Bob notifies beta users only.", source_ids: ["REQ-2", "DEC-2"], at_s: 112.0, by: "model", supersedes: null };
  meetingMsg("discussion", { clarifications: [CL1], decisions: [D1], action_items: [{ id: "A1", owner: "Bob", text: "Bob notifies beta users only.", decision_id: "D1" }] });
  eq("Notes: the decision (unread: the transcript is open)", [badge("notes"), text('[data-section="decisions"] .note-text'), text('[data-section="decisions"] .owner-chip'), text('[data-section="decisions"] .note-sub')], [2, "Friday: beta release; announcement to beta users only", "Owner: Bob", "Follow-up: Bob notifies beta users only."]);
  eq("…its sources, no auto tag", [$$('[data-section="decisions"] .src-chip').map((c) => c.textContent), $('[data-section="decisions"] .auto-tag')], [["REQ-2", "DEC-2"], null]);
  eq("…the action item", [text('[data-section="action_items"] .owner-chip'), text('[data-section="action_items"] .note-text')], ["Bob", "Bob notifies beta users only."]);
  eq("…counts", [text('[data-section="decisions"] .notes-count'), text('[data-section="clarifications"] .notes-count')], ["· 1", "· 1"]);
  eq("the new decision over the share", [$(".callout").hidden, $(".callout").dataset.kind, text(".callout-kind"), text(".callout-item .callout-title"), text(".callout-item .callout-text")], [false, "decision", "Added to the notes", "Owner: Bob", "Friday: beta release; announcement to beta users only"]);
  meetingMsg("discussion", { clarifications: [CL1], decisions: [D1, { ...D1, id: "D2", decision: "Support briefing on Thursday", owner: "", follow_up: "", source_ids: [], by: "auto" }] });
  eq("an automatic decision: the auto tag, no owner", [text('[data-id="D2"] .auto-tag'), $('[data-id="D2"] .owner-chip')], ["auto", null]);

  // ---- a silent suggestion (a conflict that fell out of the window unconfirmed)
  utterance("U5", "Bob", "And the beta notes go to all customers too.", 120);
  alignment({ id: "AL9", type: "conflict", ...AUDIENCE, utterance_id: "U5", at_s: 120, claimed: "all_customers", claimed_label: "all customers", established: "beta_users", established_label: "beta users", source_ids: ["DEC-2"], clarification_ids: [], decision_ids: [], has_action: false, sentence: "And the beta notes go to all customers too.", repeat: false });
  for (const [id, at] of [["U6", 121], ["U7", 122], ["U8", 123]]) utterance(id, null, "Mm-hm.", at);
  alignment({ id: "AL10", type: "suggestion", ...AUDIENCE, utterance_id: "U5", at_s: 123, conflict_id: "AL9", claimed: "all_customers", claimed_label: "all customers", established: "beta_users", established_label: "beta users", source_ids: ["DEC-2"], clarification_ids: [], decision_ids: [], quote: "And the beta notes go to all customers too.", has_action: false, observed_utterance_id: "U8" });
  const sugg = $('.align-card[data-kind="suggestion"]');
  eq("a dashed card, never said", [sugg && text('.align-card[data-kind="suggestion"] .align-title'), sugg && sugg.dataset.status], ["Possible mismatch (not raised)", "suggestion"]);
  eq("…after the line it is about", (() => { const items = $(".transcript").children; return items[items.indexOf(sugg) - 1].dataset.utt; })(), "U5");
  eq("unlabelled lines are the room's", text('[data-utt="U6"] .utt-who'), "Room");

  // ---- Space in the discussion = a direct question (the room mic hint goes; the typed line still goes out)
  press(" ", "Space");
  eq("Space: key space", lastSent("key").key, "space");
  state("LISTENING", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  eq("a direct question: the turn's mic, not the room's", [$(".mic-button .tb-label").textContent, $(".mic-button").dataset.room, $(".cap-hint").textContent], ["Mute", "false", "Space to send · Esc to cancel"]);
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });

  // ---- wrap up: the summary, the notes
  $(".phase-button").fire("click");
  eq("Wrap up = key phase", lastSent("key").key, "phase");
  server({ type: "mic", open: false });
  server({ type: "reply_start", reply: 9, kind: "summary" });
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "wrapping_up" };
  state("REPLYING", { agenda_phase: "wrapup", phase: "human", slide: "discussion", thinking: true });
  eq("wrapping up", [status(), text(".checkpoint-status"), $(".speakers").hidden, $(".mic-button .tb-label").textContent], ["Wrapping up", "wrapping up", true, "Unmute"]);
  meetingMsg("wrapup", { clarifications: [CL1], decisions: [D1], action_items: [{ id: "A1", owner: "Bob", text: "Bob notifies beta users only.", decision_id: "D1" }], open_questions: [{ id: "Q1", text: "Friday's audience: beta users (DEC-2) or all customers?", at_s: 125, resolved: false }], notes: { title: MEETING.title, phase: "wrapup", generated_at_s: 130.4 } });
  eq("the panel shows the Notes", selected(), ["notes"]);
  eq("…headed Meeting notes, with the time", [$(".notes-head").hidden, text(".notes-title"), text(".notes-time")], [false, "Meeting notes", "generated at 02:10"]);
  eq("…the open question", text('[data-section="open_questions"] .note-text'), "Friday's audience: beta users (DEC-2) or all customers?");
  eq("the strip: done", [$$(".agenda-step").map((li) => li.dataset.state), text(".phase-button"), $(".phase-button").disabled], [["done", "done", "current"], "Done", true]);
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "idle" };
  state("IDLE", { agenda_phase: "wrapup", phase: "human", slide: "discussion", banner: "Meeting wrapped up" });
  eq("the tile after the summary", status(), "Meeting wrapped up");

  // ---- Restart meeting: More → confirm → key reset; the lists go
  const more = $(".more-button");
  const reset = $(".menu-reset");
  const choose = (target) => $(".more-menu").fire("click", { target });
  more.fire("click");
  choose(reset.querySelector("span"));
  eq("first click asks, sends nothing, the menu stays", [lastSent("key").key, $(".more-menu").hidden, reset.dataset.confirm, text(".reset-label")], ["phase", false, "true", "Click again to restart: the notes and transcript are cleared"]);
  press("Escape");
  eq("Esc closes the menu and disarms it", [$(".more-menu").hidden, reset.dataset.confirm, text(".reset-label")], [true, "false", "Restart meeting"]);
  more.fire("click");
  choose(reset);
  advance(5_100);
  eq("…so does waiting", reset.dataset.confirm, "false");
  choose(reset);
  choose(reset);
  eq("two clicks: key reset", [lastSent("key").key, $(".more-menu").hidden], ["reset", true]);
  eq("…the transcript, the cards and the lookups go", [$$(".utt").length, $$(".align-card").length, $$(".lookup").length, $(".transcript-empty").hidden, $(".lookups-empty").hidden], [0, 0, 0, false, false]);
  eq("…nothing unread", ["evidence", "notes", "transcript", "chat"].map(badge), [0, 0, 0, 0]);
  checkpoint = { slide_id: "agenda", talking_point_id: null, status: "idle" };
  state("IDLE", { agenda_phase: "presentation", phase: "opening", slide: "agenda" });
  meetingMsg("presentation", { started: false });
  eq("…back to the presentation", [$$(".agenda-step").map((li) => li.dataset.state), text(".phase-button"), selected(), text('[data-section="decisions"] .note-none')], [["current", "next", "next"], "Start discussion", ["evidence"], "None yet"]);
  eq("…the captions start clean", [$(".cap-user").hidden, $(".captions").dataset.mode], [true, "idle"]);
  meetingMsg("presentation", { started: true, decisions: [D1] });
  check("…a decision after the restart is new again (callout)", text(".callout-kind") === "Added to the notes" && !$(".callout").hidden);

  // ---- a restart this page didn't ask for (another deck, the server): the phase moves back, the lists go too
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  utterance("U20", "Bob", "Let's ship it.", 300);
  eq("a new discussion line", $$(".utt").length >= 1, true);
  state("IDLE", { agenda_phase: "presentation", phase: "opening", slide: "agenda" });
  eq("back to the presentation from elsewhere: the transcript and the callout go", [$$(".utt").length, $(".callout").hidden, badge("transcript")], [0, true, 0]);

  // ---- a reconnect re-sends the speaker the deck picked; the server's snapshot doesn't override it
  sock().drop(1006);
  advance(300);
  sock().open();
  eq("reconnect: the speaker again", lastSent("speaker"), { type: "speaker", name: "Carol" });
  server({ type: "hello", talk: "rollout-review", version: "v1", mic_mode: "tap", narration_mode: "live", meeting: MEETING, snapshot: { state: "IDLE", phase: "opening", slide: "agenda", connection: { status: "ready", detail: null }, agenda_phase: "presentation", speaker: null } });
  eq("…the deck's pick stands", text(".tile-you .name"), "Room"); // the presentation: the room tile shows the room
  $(".speakers").hidden = false;
  eq("…still Carol in the picker", doc.querySelectorAll(".speaker").filter((b) => b.getAttribute("aria-checked") === "true").map((b) => b.textContent), ["Carol"]);

  eq("no console errors", env.errors, []);
  print(`facilitator view: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} facilitator view check(s) failed`);
}

try {
  await run();
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
