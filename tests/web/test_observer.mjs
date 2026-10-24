// The observer page (contract v5 §3): the real index.html + main.js in the fake browser, booted with `?view=observer`
// as the Zoom bot shares it. It connects to /ws/observe, says hello as an observer and nothing else, ever (the whole
// feed replayed, keys pressed, a reconnect); it creates no AudioContext, asks for no microphone, loads no worklet,
// shows no join dialog, runs no demo script; it renders every message like a joined deck: slides, the backup
// narration's karaoke and the live captions on the wall clock, the "In Zoom" participants (a ring when the backend
// heard one), the evidence callout and tabs, the alignment card, the notes, the chat, the Live in Zoom badge, and
// its controls that would send are hidden or dead.
//   jsc -m tests/web/test_observer.mjs   (from the repo root; tests/web/run.sh runs everything)
import { advance, clock, env, frame, install, settle } from "./fake_browser.mjs";
/* global fireWindow */
import { MEETING, serve } from "./demo_fixture.mjs";
import { DeckSocket, isObserverView, observerSocketUrl } from "../../web/js/socket.js";
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

// ---- the view switch (pure)
eq("observer view: ?view=observer, ?view=zoom (the contract's other spelling), #observer", [
  isObserverView({ search: "?view=observer" }),
  isObserverView({ search: "?a=1&view=zoom" }),
  isObserverView({ hash: "#observer" }),
  isObserverView({ search: "?view=observer&x=1", hash: "" }),
], [true, true, true, true]);
eq("…not a deck", [isObserverView({}), isObserverView({ search: "?view=deck" }), isObserverView({ search: "?observer" }), isObserverView({ search: "", hash: "#how" })], [false, false, false, false]);
eq("observer socket url", observerSocketUrl({ protocol: "https:", host: "h:1" }), "wss://h:1/ws/observe");

// ---- the talk: talks/submission-review's shape in voice mode (its demo.yaml stays; the server says input voice)
serve(env, "voice");
function timed(text) {
  const words = [];
  let t = 150;
  let sentence = 0;
  text.split(/\s+/).filter(Boolean).forEach((w) => {
    const d = 120 + 45 * w.replace(/[^a-z0-9]/gi, "").length;
    words.push({ word: w.replace(/[.,!?]/g, ""), start: t, end: t + d, sentence });
    t += d;
    if (/[.!?]$/.test(w)) {
      sentence++;
      t += 350;
    } else t += 90;
  });
  return { words, duration_ms: t + 250 };
}
const PLAN0 = "Friday is beta. Wednesday is production.";
const plan0 = timed(PLAN0);
env.talk.slides[1].chunks[0] = { index: 0, text: PLAN0, fragment: null, duration_ms: plan0.duration_ms, words: plan0.words, sentences: [], audio: "/media/plan/0.wav" };

const { doc } = install(read("web/index.html"));
globalThis.location.search = "?view=observer";
const $ = (sel) => doc.querySelector(sel);
const $$ = (sel) => doc.querySelectorAll(sel);
const text = (sel) => ($(sel) ? $(sel).textContent.replace(/\s+/g, " ").trim() : null);
const sock = () => env.socket();
const out = () => sock().sent;
const server = (msg) => sock().receive(msg);
const press = (key, code = "") => fireWindow("keydown", { type: "keydown", key, code, repeat: false, target: doc.body, timeStamp: clock.now, preventDefault() {} });
const status = () => text(".tile-relay .tile-status");
const tile = (name) => $(`.tile-participant[data-name="${name}"]`);
const words = () => [...$$(".cap-words .w")];
const wordClasses = (cls) => words().filter((w) => w.classList.contains(cls)).map((w) => w.textContent);
const chatLines = () => $$(".chat-line").map((li) => `${li.querySelector(".chat-who")?.textContent ?? "-"}: ${li.querySelector(".chat-text").textContent}`);
const SECOND = int16ToBase64(new Int16Array(24000).fill(800));
let checkpoint = { slide_id: "agenda", talking_point_id: null, status: "idle" };
const state = (st, extra = {}) =>
  server({ type: "state", state: st, phase: "presenting", slide: checkpoint.slide_id, banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null, agenda_phase: "presentation", status: checkpoint.status, talking_point: checkpoint.talking_point_id, checkpoint, ...extra });
const base = { title: MEETING.title, host: "Alice", started: true, participants: MEETING.participants, clarifications: [], decisions: [], action_items: [], open_questions: [], notes: null };
const meetingMsg = (phase, extra = {}) => server({ type: "meeting", ...base, phase, agenda: MEETING.phases.map((p) => ({ ...p, current: p.id === phase })), checkpoint, ...extra });
const hello = (snapshot) =>
  server({ type: "hello", talk: "rollout-review", version: "v1", mic_mode: "tap", narration_mode: "live", meeting: MEETING, input: "voice", demo: env.talk.demo, snapshot: { state: "IDLE", phase: "opening", slide: "agenda", banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null, connection: { status: "ready", detail: null }, agenda_phase: "presentation", status: "idle", talking_point: null, checkpoint, speaker: null, input: "voice", ...snapshot } });
/** The greeting bundle an observer gets on connect (Controller.greeting(), fanned out by the hub). */
function greet() {
  hello({});
  server({ type: "stop" });
  server({ type: "mic", open: false });
  server({ type: "slide", slide: "agenda", fragment: null });
  state("IDLE", { phase: "opening" });
  server({ type: "connection", status: "ready", detail: null });
  meetingMsg("presentation");
}
const BOB = "I'll draft the announcement for Friday's production launch to all customers.";
const STAGE = { slot: "friday_stage", label: "Friday's release stage" };

async function run() {
  await import("../../web/js/main.js");
  await settle();

  // ---- boot: the observer socket, hello as an observer, no audio, no mic, no join dialog, no keys
  eq("talk fetched", env.fetches[0], "/api/talk");
  eq("one socket, the observer's", [env.sockets.length, sock().url], [1, "ws://127.0.0.1:8000/ws/observe"]);
  sock().open();
  eq("hello as an observer, nothing else", out(), [{ type: "hello", client: "observer" }]);
  eq("no AudioContext, no getUserMedia, no worklets", [env.contexts.length, env.gum.length, env.modules.length, Object.keys(env.worklets).length], [0, 0, 0, 0]);
  check("the join dialog is hidden", $(".overlay").hidden);
  eq("the page title is fixed (the share picker matches it)", doc.title, "On the Same Page");
  check("the window knows it is the observer", $(".meeting").classList.contains("observer"));
  eq("the toolbar: no mic, no Leave; the note in their place", [$(".mic-button").hidden, $(".leave-button").hidden, $(".observer-note").hidden, text(".observer-note")], [true, true, false, "Shared from the facilitator's Zoom window"]);
  eq("no chat box, no Restart meeting, no speaker picker", [$(".chat-compose").hidden, $(".menu-reset").hidden, $(".speakers").hidden], [true, true, true]);
  eq("◀ ▶ and the phase button are dead", [$(".prev-button").disabled, $(".next-button").disabled, $(".phase-button").disabled], [true, true, true]);
  eq("the clock runs from the start", $(".clock").textContent, "00:00");
  advance(61_000);
  frame();
  eq("…and counts", $(".clock").textContent, "01:01");
  eq("the brand and the agenda strip (a meeting talk)", [text(".brand-name"), $(".agenda").hidden, $$(".agenda-step").length], ["On the Same Page", false, 3]);
  check("the side panel is open on Evidence (as on a deck)", !$(".chat").hidden && $('.tab[data-tab="evidence"]').getAttribute("aria-selected") === "true");

  // keys: nothing is installed, nothing goes out
  press(" ", "Space");
  press("r");
  press("ArrowRight");
  press("ArrowLeft");
  press("p");
  press("Escape");
  eq("keys send nothing", out().length, 1);
  $(".phase-button").fire("click");
  $(".prev-button").fire("click");
  $(".next-button").fire("click");
  $(".leave-button").fire("click");
  $(".mic-button").fire("click");
  $(".strip").fire("click");
  eq("the controls send nothing and open no dialog", [out().length, $(".overlay").hidden], [1, true]);

  // ---- the greeting bundle (input voice): the badge, the participants in Zoom, no demo script
  greet();
  eq("the Live in Zoom badge", [$(".zoom-pill").hidden, text(".zoom-pill"), $(".meeting").classList.contains("in-zoom")], [false, "Live in Zoom · participants are real people", true]);
  eq("the participants' tiles replace the room's", [$(".tile-you").hidden, $$(".tile-participant").length, $(".meeting").classList.contains("has-people")], [true, 3, true]);
  eq("…tagged In Zoom, not Recorded", $$(".tile-participant").map((t) => `${t.querySelector(".tile-tag").textContent}:${t.dataset.kind}:${t.querySelector(".name").textContent}`), ["In Zoom:live:Alice", "In Zoom:live:Bob", "In Zoom:live:Carol"]);
  check("…and said to be real", tile("Bob").getAttribute("aria-label").includes("a participant in the Zoom meeting") && tile("Bob").querySelector(".tile-tag").title.includes("real participant"));
  eq("no demo script (its payload says input voice): no strip, no disclosure, no has-demo", [$(".demo").hidden, $(".demo-pill").hidden, $(".panel-demo").hidden, $(".meeting").classList.contains("has-demo"), $(".strip-routing").hidden], [true, true, true, false, true]);
  eq("the greeting rendered: slide 1, waiting, Realtime ready, the checkpoint", [Reveal.calls.at(-1), status(), $(".live-badge").dataset.status, text(".checkpoint-where"), text(".checkpoint-status")], [[0, 0, -1], "Waiting for Alice", "ready", "Slide 1", "idle"]);
  eq("the phase button stays dead after the meeting message", [text(".phase-button"), $(".phase-button").disabled], ["Start discussion", true]);
  eq("still only the hello", out().length, 1);

  // ---- the backup narration: `play` runs the karaoke on the wall clock (no audio clock here)
  checkpoint = { slide_id: "plan", talking_point_id: "friday-beta", status: "presenting" };
  server({ type: "slide", slide: "plan", fragment: null });
  server({ type: "play", play_id: 1, slide: "plan", chunk: 0, offset_ms: 0, fragment: null, resume_word: null });
  state("NARRATING", { narration: "backup" });
  eq("the slide and the backup badge", [Reveal.calls.at(-1), $(".backup-badge").hidden, status()], [[1, 0, -1], false, "Presenting · backup audio"]);
  eq("narration captions, nothing said yet", [$(".captions").dataset.mode, wordClasses("past").length], ["narration", 0]);
  eq("…the first sentence as the page", words().map((w) => w.textContent).join(" "), "Friday is beta.");
  frame();
  eq("the facilitator's ring while the (bot's) narration plays", $(".tile-relay").dataset.speaking, "true");
  advance(400);
  frame();
  check("400 ms in: the karaoke moved on the fake clock", wordClasses("past").length >= 1 || wordClasses("now").length === 1, JSON.stringify([wordClasses("past"), wordClasses("now")]));
  advance(plan0.words[2].start - 400 + 10);
  frame();
  eq("…on the third word: two said, one being said", [wordClasses("past"), wordClasses("now")], [["Friday", "is"], ["beta."]]);
  eq("no chunk_ended, no toast about missing audio (the bot plays it)", [out().length, $$(".toast").length], [1, 0]);
  advance(plan0.duration_ms);
  frame();
  eq("the chunk ran out at its duration: everything said, still nothing sent", [words().every((w) => w.classList.contains("past")), out().length], [true, 1]);
  check("…no warning about audio", !env.warnings.some((w) => w.includes("plays without audio")), env.warnings.join(" | "));
  advance(300);
  frame();
  eq("…the ring goes once it ended", $(".tile-relay").dataset.speaking, "false");

  // ---- a live answer: reply_start, the audio (the bot plays it), the caption revealed on the wall clock, drained
  const QUESTION = "Wait, I thought Friday was production. Which one is it?";
  server({ type: "mic", open: true });
  checkpoint = { slide_id: "plan", talking_point_id: "friday-beta", status: "answering" };
  state("LISTENING");
  eq("listening: the flag, no Space hint (the observer has no Space)", [$(".cap-tag").textContent, $(".cap-hint").hidden], ["Listening…", true]);
  server({ type: "mic", open: false });
  server({ type: "caption", role: "user", text: QUESTION, final: true, reply: 5, speaker: "Alice" });
  eq("the question, labelled with who Zoom heard (not recorded)", [$(".cap-user-text").textContent, $(".cap-user .cap-speaker").textContent], [QUESTION, "Alice"]);
  eq("…in the chat as Alice's", chatLines().at(-1), `Alice: ${QUESTION}`);
  eq("…Alice's tile speaks for a moment", [tile("Alice").dataset.speaking, text('.tile-participant[data-name="Alice"] .tile-status'), tile("Bob").dataset.speaking], ["true", "Speaking", "false"]);
  advance(2600);
  eq("…then not", [tile("Alice").dataset.speaking, text('.tile-participant[data-name="Alice"] .tile-status')], ["false", ""]);
  state("REPLYING", { thinking: true });
  server({ type: "tool", call_id: "c1", name: "lookup_evidence", args: { query: "Friday rollout" }, status: "applied" });
  server({ type: "evidence", lookup: { id: "E1", query: "Friday rollout", by: "model", purpose: "answer", at_s: 40.5, results: [{ source_id: "REQ-2", kind: "source", title: "Rollout plan v3", excerpt: MEETING.documents[0].statements[0].text, score: 4 }] } });
  eq("the tool call in the ticker and the chat", [$$(".ticker .tool-name").map((e) => e.textContent), chatLines().some((l) => l.includes("lookup_evidence"))], [['lookup_evidence("Friday rollout")'], true]);
  eq("the evidence callout and tab", [$(".callout").hidden, text(".callout-kind"), $$(".callout-item .src-chip").map((c) => c.textContent), text(".lookup .lookup-query")], [false, "Evidence", ["REQ-2"], "“Friday rollout”"]);
  const ANSWER = "The approved rollout plan says beta on Friday and production next Wednesday."; // 12 words
  server({ type: "reply_start", reply: 5, kind: "answer" });
  state("REPLYING");
  for (let i = 0; i < 3; i++) server({ type: "reply_audio", reply: 5, pcm: SECOND }); // 3 s of audio, all at once
  server({ type: "caption", role: "assistant", text: ANSWER, final: false, reply: 5 });
  eq("live mode, nothing revealed before the audio has played in Zoom", [$(".captions").dataset.mode, $(".cap-live-text").textContent], ["live", ""]);
  frame();
  eq("the facilitator answers, its ring on", [status(), $(".tile-relay").dataset.speaking], ["Answering", "true"]);
  advance(1000);
  eq("1 s in: four words heard (4 of 12 by the audio's share; the 4 words/s cap allows 5)", $(".cap-live-text").textContent, "The approved rollout plan");
  advance(1000);
  eq("2 s in: eight", $(".cap-live-text").textContent, "The approved rollout plan says beta on Friday");
  server({ type: "caption", role: "assistant", text: ANSWER, final: true, reply: 5 });
  server({ type: "reply_end", reply: 5 });
  eq("reply_end before the audio played out: not drained yet, the caption keeps its pace", [$(".cap-live-text").textContent, out().length], ["The approved rollout plan says beta on Friday", 1]);
  advance(1100);
  eq("3 s in: the reply played out: the whole answer", $(".cap-live-text").textContent, ANSWER);
  eq("…in the chat and the Chat tab, no reply_drained sent", [chatLines().at(-1), out().length], [`Facilitator: ${ANSWER}`, 1]);
  advance(300);
  frame();
  eq("…the ring goes", $(".tile-relay").dataset.speaking, "false");
  state("NARRATING", { narration: "live" });
  // Relay presenting live: a narration reply with its audio; a reply_flush from the backend (the bot was cut)
  server({ type: "reply_start", reply: 6, kind: "narration" });
  server({ type: "reply_audio", reply: 6, pcm: SECOND });
  server({ type: "caption", role: "assistant", text: "Stage two is production on Wednesday.", final: false, reply: 6 });
  advance(500);
  eq("a live narration reply reveals too (0.5 s: 3 of 6 words by the audio's share and the cap alike)", $(".cap-live-text").textContent, "Stage two is");
  server({ type: "barge_in", speaker: "Bob", played_ms: 480, reply: 6, state: "NARRATING" });
  server({ type: "reply_flush", reply: 6 });
  eq("a barge-in: the facilitator's tile says who, the caption freezes with a dash, Bob's tile speaks", [status(), $(".cap-live-text").textContent, tile("Bob").dataset.speaking], ["Interrupted by Bob", "Stage two is —", "true"]);
  check("…a chat line", chatLines().some((l) => l.includes("Bob interrupted the facilitator")));
  advance(2100);
  eq("…2 s later the tile says what it does", status(), "Presenting");

  // ---- the discussion: the room mic open (the bot streams Zoom), transcript lines, the alignment card
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "listening" };
  server({ type: "slide", slide: "discussion", fragment: null });
  server({ type: "mic", open: true });
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  meetingMsg("discussion");
  eq("the discussion: Transcript tab, listening to the room, no room mic button, no picker, no hint", [
    $$(".tab").filter((t) => t.getAttribute("aria-selected") === "true").map((t) => t.dataset.tab),
    status(),
    $(".mic-button").hidden,
    $(".speakers").hidden,
    $(".cap-tag").textContent,
    $(".cap-hint").hidden,
  ], [["transcript"], "Listening to the room", true, true, "Listening…", true]);
  eq("…Wrap up shows, dead", [text(".phase-button"), $(".phase-button").disabled], ["Wrap up", true]);
  server({ type: "transcript", utterance: { id: "U1", text: "Production is next Wednesday, right?", speaker: "Alice", at_s: 90.1, phase: "discussion", source: "voice" } });
  eq("a transcript line: who, when, what; on the captions; her tile speaks", [text('[data-utt="U1"] .utt-who'), text('[data-utt="U1"] .utt-time'), text('[data-utt="U1"] .utt-text'), $(".cap-user .cap-speaker").textContent, tile("Alice").dataset.speaking, tile("Bob").dataset.speaking], ["Alice", "01:30", "Production is next Wednesday, right?", "Alice", "true", "false"]);
  check("…a voice line has no source tag", $('[data-utt="U1"] .utt-source') === null);
  server({ type: "transcript", utterance: { id: "U2", text: BOB, speaker: "Bob", at_s: 95.5, phase: "discussion", source: "voice" } });
  server({ type: "alignment", event: { id: "AL1", type: "claim", ...STAGE, utterance_id: "U2", at_s: 95.5, value: "production", value_label: "production", sentence: BOB, agrees: false } });
  server({ type: "alignment", event: { id: "AL2", type: "conflict", ...STAGE, utterance_id: "U2", at_s: 95.5, claimed: "production", claimed_label: "production", established: "beta", established_label: "beta", source_ids: ["REQ-2"], clarification_ids: [], decision_ids: [], has_action: true, sentence: BOB, repeat: false } });
  eq("Bob's claim chip, amber", $$('[data-utt="U2"] .claim').map((c) => `${c.textContent}:${c.dataset.tone}`), ["Friday's release stage: production:conflict"]);
  server({ type: "transcript", utterance: { id: "U3", text: "Sounds good. Let's finalize that.", speaker: "Carol", at_s: 99.0, phase: "discussion", source: "voice" } });
  server({ type: "alignment", event: { id: "AL5", type: "intervene", ...STAGE, utterance_id: "U3", at_s: 99.0, conflict_id: "AL2", claimed: "production", claimed_label: "production", established: "beta", established_label: "beta", source_ids: ["REQ-2"], clarification_ids: [], decision_ids: [], quote: BOB, quote_utterance_id: "U2", quote_speaker: "Bob", confirm_quote: "Sounds good. Let's finalize that.", has_action: true, conflict_ids: ["AL2"], related: [], query: "Friday's release stage production beta" } });
  eq("the alignment card, pending", [$(".align-card") !== null, $(".align-card").dataset.status, $$(".align-diff").map((d) => [...d.children].map((c) => c.textContent).join(" "))], [true, "pending", ["Friday's release stage production ≠ beta"]]);
  server({ type: "evidence", lookup: { id: "E3", query: "Friday's release stage production beta", by: "engine", purpose: "intervention", at_s: 99.1, results: [{ source_id: "REQ-2", kind: "source", title: "Rollout plan v3", excerpt: MEETING.documents[0].statements[0].text, score: 4 }] } });
  eq("the alignment check's evidence over the share", [$(".callout").hidden, $(".callout").dataset.kind, text(".callout-kind")], [false, "intervention", "Alignment check"]);
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "intervening" };
  server({ type: "reply_start", reply: 7, kind: "intervention" });
  state("INTERVENING", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  server({ type: "reply_audio", reply: 7, pcm: SECOND });
  const INTERVENTION = "One clarification before we confirm: we established that Friday is a beta release, not production.";
  server({ type: "caption", role: "assistant", text: INTERVENTION, final: true, reply: 7 });
  eq("the facilitator speaks up: the card is asked, the tile says so", [$(".align-card").dataset.status, status(), text(".checkpoint-status")], ["asked", "Speaking up", "intervening"]);
  server({ type: "reply_end", reply: 7 });
  advance(1100);
  eq("…its words in the transcript once played", $$('.utt[data-who="facilitator"] .utt-text').map((e) => e.textContent), [INTERVENTION]);
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "listening" };
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  server({ type: "transcript", utterance: { id: "U4", text: "Yes, beta users only. Bob owns the announcement.", speaker: "Carol", at_s: 110.2, phase: "discussion", source: "voice" } });
  server({ type: "alignment", event: { id: "AL6", type: "resolved", ...STAGE, utterance_id: "U4", at_s: 110.2, conflict_id: "AL2", intervene_id: "AL5", value: "beta", value_label: "beta", plan_changed: false, owner: "Bob", utterance_ids: ["U2", "U4"] } });
  eq("resolved, with the owner", [$(".align-card").dataset.status, $$(".align-result").map((r) => r.textContent), text(".align-outcome .owner-chip")], ["resolved", ["Confirmed · Friday's release stage: beta"], "Owner: Bob"]);
  const D1 = { id: "D1", decision: "Friday: beta release; announcement to beta users only", owner: "Bob", follow_up: "", source_ids: ["REQ-2"], at_s: 112.0, by: "model", supersedes: null };
  meetingMsg("discussion", { decisions: [D1] });
  eq("the decision in the notes and over the share", [text('[data-section="decisions"] .note-text'), $(".callout").dataset.kind, text(".callout-kind")], [D1.decision, "decision", "Added to the notes"]);

  // ---- a countdown, an error, an unlabelled line (the room's), the wrap-up
  server({ type: "countdown", seconds: 2 });
  eq("the countdown shows", [$(".banner").hidden, $(".banner-count").textContent], [false, "2"]);
  server({ type: "countdown", seconds: 0 });
  server({ type: "error", message: "No reply" });
  check("an error is a toast", $$(".toast").some((t) => t.textContent === "No reply"));
  server({ type: "caption", role: "user", text: "Can we move on?", final: true, reply: 8 });
  eq("a user line without a speaker is the room's", [chatLines().at(-1), $(".cap-user .cap-speaker").textContent], ["Room: Can we move on?", "Carol"]);
  server({ type: "mic", open: false });
  server({ type: "reply_start", reply: 9, kind: "summary" });
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "wrapping_up" };
  state("REPLYING", { agenda_phase: "wrapup", phase: "human", slide: "discussion" });
  meetingMsg("wrapup", { decisions: [D1], notes: { title: MEETING.title, phase: "wrapup", generated_at_s: 130.4 } });
  eq("wrapping up: the Notes tab, the strip done", [status(), $$(".tab").filter((t) => t.getAttribute("aria-selected") === "true").map((t) => t.dataset.tab), $$(".agenda-step").map((li) => li.dataset.state), text(".phase-button"), $(".phase-button").disabled], ["Wrapping up", ["notes"], ["done", "done", "current"], "Done", true]);

  // ---- a reconnect: hello as an observer again, and nothing else
  const before = env.sockets.length;
  sock().drop(1006);
  check("the server pill while disconnected", !$(".server-pill").hidden);
  advance(300);
  eq("reconnects to the observer socket", [env.sockets.length, sock().url], [before + 1, "ws://127.0.0.1:8000/ws/observe"]);
  sock().open();
  eq("hello as an observer again, no arm, no speaker", out(), [{ type: "hello", client: "observer" }]);
  greet();
  eq("greeted again: rendered, nothing sent", [status(), out().length], ["Waiting for Alice", 1]);
  check("a restarted server's hello never set up the demo script", $(".demo").hidden && !$(".meeting").classList.contains("has-demo"));

  // ---- the observer's socket drops anything but hello, whatever a handler would try (the safety net)
  const guard = new DeckSocket("ws://x/ws/observe", { onMessage() {} }, { client: "observer" });
  guard.connect();
  sock().open();
  eq("the socket drops anything but hello", [guard.send({ type: "key", key: "space" }), guard.send({ type: "log", level: "info", message: "x" }), sock().sent], [false, false, [{ type: "hello", client: "observer" }]]);

  eq("no AudioContext was ever created", env.contexts.length, 0);
  eq("no console errors", env.errors, []);
  print(`observer: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} observer check(s) failed`);
}

try {
  await run();
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
