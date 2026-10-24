// The simulated online meeting on the deck (contract v3.1 §4–§5): the real index.html + main.js in the fake browser
// with a meeting talk whose demo input is `clips`. No getUserMedia; the join dialog says the mic is off and the
// participants are prerecorded, and preloads their clips; the participants' tiles (a "Recorded" tag, a ring from their
// own clip's analyser, never the facilitator's), the topbar badge, the routing line, the demo strip (Run demo, the
// next step and its cue, Play next); each clip on its participant's bus while its samples stream as `mic` frames
// (source "clip", speaker, clip, reply, played_ms), paced on the audio clock, after a `speaker` message, then 1.2 s of
// silence; the whole script driven by the page's events (the talking point heard, the quiet discussion, Bob's clip
// ending, the intervention and the decision drained) to the script pressing Wrap up; the barge_in cue; Play next by
// hand (disclosed); Stop and Restart stop a clip at once.
//   jsc -m tests/web/test_demo_view.mjs   (from the repo root; tests/web/run.sh runs everything)
import { advance, clock, env, frame, install, settle } from "./fake_browser.mjs";
/* global fireWindow */
import { CLIP_SAMPLES, SAY, clipSamples, serve } from "./demo_fixture.mjs";
import { base64ToInt16, int16ToBase64 } from "../../web/js/util.js";

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

serve(env, "clips");
const { doc } = install(read("web/index.html"));
const $ = (sel) => doc.querySelector(sel);
const $$ = (sel) => doc.querySelectorAll(sel);
const text = (sel) => ($(sel) ? $(sel).textContent.replace(/\s+/g, " ").trim() : null);
const sock = () => env.socket();
const out = () => sock().sent;
const sent = (type) => out().filter((m) => m.type === type);
const lastSent = (type) => sent(type).at(-1);
const server = (msg) => sock().receive(msg);
const ctx = () => env.contexts[0];
const node = () => env.worklets["pcm-player"];
const status = () => $(".tile-relay .tile-status").textContent;
const tile = (name) => $(`.tile-participant[data-name="${name}"]`);
const clipFrames = (clip) => sent("mic").filter((m) => m.source === "clip" && (clip === undefined || m.clip === clip));
const isSilent = (m) => base64ToInt16(m.pcm).every((v) => v === 0);
const SECOND = int16ToBase64(new Int16Array(24000).fill(800));
const progress = (reply, seconds) => node().emit({ type: "progress", reply, played: Math.round(seconds * 24000), time: clock.now / 1000 });
const drained = (reply, seconds = 1) => node().emit({ type: "drained", reply, played: Math.round(seconds * 24000), time: clock.now / 1000 });
let checkpoint = { slide_id: "agenda", talking_point_id: null, status: "idle" };
const state = (st, extra = {}) =>
  server({ type: "state", state: st, phase: "presenting", slide: checkpoint.slide_id, banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null, agenda_phase: "presentation", status: checkpoint.status, talking_point: checkpoint.talking_point_id, checkpoint, ...extra });
const chatTexts = () => $$(".chat-line .chat-text").map((t) => t.textContent);
const demoText = () => text(".demo-next-text");
const ms = (samples) => (samples * 1000) / 24000;

async function run() {
  await import("../../web/js/main.js");
  await settle();
  sock().open();

  // ---- before joining: the disclosure, the tiles, the strip; the mic is off
  check("the meeting is a simulated one", $(".meeting").classList.contains("has-demo"));
  eq("topbar badge", [$(".demo-pill").hidden, text(".demo-pill")], [false, "Simulated participants · prerecorded audio"]);
  eq("the room's tile gives way to the participants'", [$(".tile-you").hidden, $$(".tile-participant").length], [true, 3]);
  eq("…Alice, Bob and Carol with their roles", $$(".tile-participant").map((t) => `${t.querySelector(".name").textContent}: ${t.querySelector(".role").textContent}`), [
    "Alice: Product manager",
    "Bob: Customer communications",
    "Carol: Engineering lead",
  ]);
  eq("…each tagged Recorded, with an initial", $$(".tile-participant").map((t) => `${t.querySelector(".tile-tag").textContent} ${t.querySelector(".avatar-text").textContent}`), ["Recorded A", "Recorded B", "Recorded C"]);
  check("…the tag says what it means", tile("Bob").querySelector(".tile-tag").title.includes("prerecorded clip"));
  eq("the demo strip", [$(".demo").hidden, text(".demo-run"), text(".demo-play"), demoText()], [false, "Run demo", "Play next", "Next: Run demo starts the presentation (as R does)"]);
  eq("…its tooltip counts the steps", $(".demo-next").title, "Step 1 of 6");
  eq("Meeting info: the routing line", [$(".strip-routing").hidden, text(".strip-routing")], [false, "Higgs input: participant clips only (facilitator audio is not sent)"]);
  eq("join dialog: Microphone off", [$('[data-item="mic"]').dataset.status, text('[data-item="mic"] .label'), text('[data-item="mic"] .detail')], ["ok", "Microphone", "off — participants are prerecorded"]);
  eq("…the participant clips row", [$('[data-item="clips"]').hidden, text('[data-item="clips"] .label'), text('[data-item="clips"] .detail')], [false, "Participant clips", "4 clips, loaded when you join"]);
  eq("…the disclosure", [$(".panel-demo").hidden, text(".panel-demo").startsWith("An online meeting, simulated: the participants are prerecorded.")], [false, true]);
  check("…the hint: no microphone", text(".panel-hint").includes("The microphone stays off"));
  eq("…the keys legend: Space types, it doesn't talk", text('.keys [data-key="space"] dd'), "type a line (the microphone is off)");
  eq("the mic button: off", [text(".mic-button .tb-label"), $(".mic-button").dataset.muted, $(".mic-button").getAttribute("aria-pressed")], ["Mic off", "true", "false"]);
  $(".demo-run").fire("click");
  eq("Run demo before joining: the join dialog, no key", [sent("key").length, $(".overlay").hidden], [0, false]);

  // ---- join: no getUserMedia; the clips preload with the narration
  server({ type: "hello", talk: "rollout-review", version: "v1", mic_mode: "tap", narration_mode: "live", meeting: env.talk.meeting, demo: env.talk.demo, snapshot: { state: "IDLE", phase: "opening", slide: "agenda", connection: { status: "ready", detail: null }, agenda_phase: "presentation", status: "idle", checkpoint, speaker: null } });
  $(".arm-button").fire("click");
  await settle(12);
  advance(700);
  eq("joined", sent("arm").length, 1);
  eq("getUserMedia was never called", env.gum.length, 0);
  check("…no capture worklet node", env.worklets["mic-capture"] === undefined);
  eq("the clips were fetched", env.fetches.filter((u) => u.startsWith("/clips/")).sort(), ["/clips/alice-question.wav", "/clips/bob-announce.wav", "/clips/carol-correct.wav", "/clips/carol-finalize.wav"]);
  eq("…Participant clips 4/4", [$('[data-item="clips"]').dataset.status, text('[data-item="clips"] .detail')], ["ok", "4/4"]);
  eq("…mic row still off", text('[data-item="mic"] .detail'), "off — participants are prerecorded");
  check("a clean join closes the dialog", $(".overlay").hidden);
  eq("one analyser per participant, besides the facilitator's", ctx().analysers.length, 4);
  const facilitatorAnalyser = ctx().analysers[0];
  const [aliceAnalyser] = ctx().analysers.slice(1);

  // ---- Run demo: the script's first step starts the presentation (R), disclosed
  $(".demo-run").fire("click");
  eq("Run demo: the start step presses R", [sent("key").map((k) => k.key), text(".demo-run"), $(".demo-run").getAttribute("aria-pressed")], [["relay"], "Stop demo", "true"]);
  check("…disclosed in the chat", chatTexts().includes("The demo script started the presentation"));
  eq("…next: Alice, on her cue", demoText(), "Next: Alice — while the facilitator presents rollback-trigger");
  check("…the tooltip has her line", $(".demo-next").title.includes(SAY["alice-question"]));
  eq("…nothing plays yet", [sent("speaker").length, clipFrames().length], [0, 0]);

  // ---- the facilitator presents: not yet the talking point, then rollback-trigger, heard 1.0 s, then 1.6 s → Alice
  checkpoint = { slide_id: "plan", talking_point_id: "friday-beta", status: "presenting" };
  server({ type: "slide", slide: "plan", fragment: null });
  state("NARRATING", { narration: "live" });
  server({ type: "reply_start", reply: 3, kind: "narration" });
  for (let i = 0; i < 3; i++) server({ type: "reply_audio", reply: 3, pcm: SECOND });
  progress(3, 2.5);
  eq("another talking point heard 2.5 s: Alice waits", clipFrames().length, 0);
  drained(3, 3);
  checkpoint = { slide_id: "rollback", talking_point_id: "rollback-trigger", status: "presenting" };
  server({ type: "slide", slide: "rollback", fragment: null });
  state("NARRATING", { narration: "live", slide: "rollback" });
  server({ type: "reply_start", reply: 4, kind: "narration" });
  for (let i = 0; i < 4; i++) server({ type: "reply_audio", reply: 4, pcm: SECOND });
  progress(4, 1.0);
  eq("rollback-trigger heard ~1 s: Alice waits (no timer: the next progress decides)", [clipFrames().length, clock.timers.length > 0], [0, true]);
  const before = out().length;
  progress(4, 1.6);
  const speakerAt = out().findIndex((m, i) => i >= before && m.type === "speaker");
  const firstFrame = out().findIndex((m, i) => i >= before && m.type === "mic");
  eq("heard 1.6 s: the speaker message, then the clip's first frame", [out()[speakerAt], speakerAt >= 0 && speakerAt < firstFrame], [{ type: "speaker", name: "Alice" }, true]);
  const f0 = out()[firstFrame];
  eq("…a clip frame: source, speaker, clip, the facilitator's reply", [f0.source, f0.speaker, f0.clip, f0.reply], ["clip", "Alice", "alice-question", 4]);
  check("…with the heard ms (≈ 1.6 s minus the output latency)", f0.played_ms >= 1500 && f0.played_ms <= 1600, String(f0.played_ms));
  const pcm0 = base64ToInt16(f0.pcm);
  const alice = clipSamples("alice-question");
  check("…the clip's own first 100 ms", pcm0.length === 2400 && pcm0.every((v, i) => v === alice[i]));
  eq("…no other keys: the deck doesn't cut the facilitator (the backend decides)", [lastSent("key").key, node().port.posted.at(-1).type], ["relay", "push"]);
  const source = ctx().sources.at(-1);
  eq("the clip plays from now on a buffer of its samples", [source.buffer.length, source.startArgs[0] === clock.now / 1000], [CLIP_SAMPLES["alice-question"], true]);
  const bus = source.outputs[0].outputs[0];
  check("…on Alice's bus: to the speakers and her own analyser", bus.outputs.includes(ctx().destination) && bus.outputs.includes(aliceAnalyser));
  check("…not into the facilitator's analyser", !facilitatorAnalyser.inputs.includes(bus) && !bus.outputs.includes(facilitatorAnalyser));
  eq("Alice's tile speaks", [tile("Alice").dataset.playing, text('.tile-participant[data-name="Alice"] .tile-status'), tile("Bob").dataset.playing], ["true", "Speaking", "false"]);
  eq("captions: her line, labelled recorded", [$(".cap-user-text").textContent, $(".cap-user .cap-speaker").textContent], [SAY["alice-question"], "Alice (recorded)"]);
  eq("the strip: playing", [demoText(), $(".demo-play").disabled], ["Playing: Alice (recorded)", true]);
  eq("the speaker picker follows the clip", $$(".speaker").filter((b) => b.getAttribute("aria-checked") === "true").map((b) => b.textContent), ["Alice"]);

  // the rings: each from its own analyser
  aliceAnalyser.level = 0.3;
  env.outputLevel = 0;
  frame();
  eq("Alice's clip is loud, the facilitator's output quiet: her ring, not the facilitator's", [tile("Alice").dataset.speaking, $(".tile-relay").dataset.speaking], ["true", "false"]);
  aliceAnalyser.level = 0;
  facilitatorAnalyser.level = 0.3;
  advance(300);
  frame();
  eq("…and the other way round", [tile("Alice").dataset.speaking, $(".tile-relay").dataset.speaking], ["false", "true"]);
  facilitatorAnalyser.level = undefined;

  // pacing: frames go out as their audio starts playing (the audio clock), not faster
  const t0 = source.startArgs[0];
  const n1 = clipFrames("alice-question").length;
  eq("by 0.3 s: frames 0–3 (0, 0.1, 0.2, 0.3 s)", n1, 4);
  advance(40);
  eq("nothing early", clipFrames("alice-question").length, 4);
  eq("…the clip's 8400 samples: 2400 × 3 + 1200", clipFrames("alice-question").map((m) => base64ToInt16(m.pcm).length), [2400, 2400, 2400, 1200]);
  check("…the played_ms moves with the facilitator's reply", clipFrames("alice-question")[3].played_ms > f0.played_ms);
  check("…frame times on the audio clock", Math.abs(ctx().currentTime - t0 - 0.34) < 1e-6);

  // ---- the barge-in: the backend heard Alice while the facilitator spoke; it cuts the reply
  server({ type: "barge_in", speaker: "Alice", clip: "alice-question", played_ms: 1720 });
  eq("the facilitator's tile: Interrupted by Alice", [status(), $(".tile-relay").dataset.interrupted], ["Interrupted by Alice", "true"]);
  check("…a line in the chat", chatTexts().some((t) => t.startsWith("Alice interrupted the facilitator") && t.includes("1.7 s")));
  server({ type: "reply_flush", reply: 4 });
  eq("the cut: the reply flushed by the backend's word", node().port.posted.at(-1).type, "flush");
  // (a clip turn opens no mic: the backend goes LISTENING and the deck keeps streaming the clip)
  checkpoint = { slide_id: "rollback", talking_point_id: "rollback-trigger", status: "answering" };
  state("LISTENING", { slide: "rollback" });
  eq("…still interrupted (2 s), the mic still off", [status(), text(".mic-button .tb-label")], ["Interrupted by Alice", "Mic off"]);
  eq("…captions: her line, no Space hint", [$(".cap-user .cap-speaker").textContent, $(".cap-hint").hidden], ["Alice (recorded)", true]);
  const cutAt = clipFrames("alice-question").length;
  advance(150);
  const afterCut = clipFrames("alice-question")[cutAt];
  eq("frames after the cut: no reply playing", [afterCut.reply, afterCut.played_ms], [null, null]);
  advance(1840);
  eq("…for 2 s", status(), "Interrupted by Alice");
  advance(10);
  eq("…after 2 s the tile says what it does", [status(), $(".tile-relay").dataset.interrupted], ["Listening…", "false"]);
  const aliceFrames = clipFrames("alice-question");
  eq("Alice's clip: 4 audio frames then 12 of silence", [aliceFrames.length, aliceFrames.slice(4).every(isSilent), aliceFrames.slice(0, 4).some(isSilent)], [16, true, false]);
  eq("…the silence flagged `tail` (the backend ends her line where the clip ends)", aliceFrames.map((m) => m.tail), [...Array(4).fill(false), ...Array(12).fill(true)]);
  eq("…her tile is quiet again, the script on Bob", [tile("Alice").dataset.playing, demoText()], ["false", "Next: Bob — once the discussion is open and the facilitator has been quiet 1.5 s"]);
  advance(1000);
  eq("…the stream is idle", clipFrames().length, 16);

  // ---- the turn: Higgs's transcript of Alice (the backend labels a clip turn's captions), the lookup, the answer,
  // the resume
  server({ type: "caption", role: "user", text: "Wait, I thought Friday was production. Which one is it?", final: true, reply: 5, speaker: "Alice" });
  eq("the user line is Alice's (recorded), not You", [$(".cap-user .cap-speaker").textContent, $$(".chat-line .chat-who").map((w) => w.textContent).at(-1)], ["Alice (recorded)", "Alice (recorded)"]);
  state("REPLYING", { thinking: true, slide: "rollback" });
  server({ type: "evidence", lookup: { id: "E1", query: "Friday rollout", by: "model", purpose: "answer", at_s: 40.5, results: [{ source_id: "REQ-2", kind: "source", title: "Rollout plan v3", excerpt: "Stage one of the rollout: on Friday 24 October the beta release goes to the beta group only.", score: 4 }] } });
  check("the evidence callout", !$(".callout").hidden && text(".callout-item .src-chip") === "REQ-2");
  server({ type: "reply_start", reply: 5, kind: "answer" });
  server({ type: "reply_audio", reply: 5, pcm: SECOND });
  server({ type: "reply_end", reply: 5 });
  drained(5);
  checkpoint = { slide_id: "rollback", talking_point_id: "rollback-trigger", status: "resuming" };
  state("NARRATING", { narration: "live", slide: "rollback" });
  server({ type: "reply_start", reply: 6, kind: "narration" });
  server({ type: "reply_audio", reply: 6, pcm: SECOND });
  progress(6, 0.9);
  eq("the resume: Bob still waits for the discussion", clipFrames().length, 16);
  // Space: no microphone here, so it neither cuts the facilitator nor opens a floor: it opens the typed box
  const keysAtSpace = sent("key").length;
  const postedAtSpace = node().port.posted.length;
  fireWindow("keydown", { type: "keydown", key: " ", code: "Space", repeat: false, target: doc.body, timeStamp: clock.now, preventDefault() {} });
  eq("Space in clips mode: no key, nothing cut, the typed box", [sent("key").length - keysAtSpace, node().port.posted.slice(postedAtSpace).some((p) => p.type === "flush"), $(".typed").hidden], [0, false, false]);
  fireWindow("keyup", { type: "keyup", key: " ", code: "Space", repeat: false, target: $(".typed-input"), timeStamp: clock.now, preventDefault() {} });
  $(".typed-input").blur();
  eq("…its release sends nothing either", sent("key").length - keysAtSpace, 0);
  drained(6);

  // ---- the hand-over, the discussion: Bob after 1.5 s of quiet
  server({ type: "reply_start", reply: 7, kind: "handover" });
  state("REPLYING", { slide: "rollback" });
  server({ type: "reply_audio", reply: 7, pcm: SECOND });
  advance(400);
  drained(7);
  checkpoint = { slide_id: "discussion", talking_point_id: null, status: "listening" };
  server({ type: "slide", slide: "discussion", fragment: null });
  server({ type: "mic", open: true });
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  server({ type: "meeting", title: "x", phase: "discussion", checkpoint, clarifications: [], decisions: [], action_items: [], open_questions: [], notes: null });
  eq("the discussion: no room mic (the participants are clips)", [text(".mic-button .tb-label"), $(".mic-button").dataset.room], ["Mic off", "false"]);
  eq("…captions: the facilitator listens", [$(".cap-tag").textContent, $(".cap-hint").hidden], ["Listening…", true]);
  advance(1490);
  eq("1.49 s of quiet: Bob waits", clipFrames("bob-announce").length, 0);
  advance(10);
  eq("1.5 s: Bob's clip, after his speaker message", [lastSent("speaker"), clipFrames("bob-announce").length], [{ type: "speaker", name: "Bob" }, 1]);
  eq("…no facilitator reply to report", [clipFrames("bob-announce")[0].reply, clipFrames("bob-announce")[0].played_ms], [null, null]);
  server({ type: "transcript", utterance: { id: "U1", text: "I'll announce Friday's production launch to all customers.", speaker: "Bob", at_s: 95.5, phase: "discussion", source: "clip" } });
  eq("the transcript: Bob, tagged recorded", [text('[data-utt="U1"] .utt-who'), text('[data-utt="U1"] .utt-source')], ["Bob", "recorded"]);
  eq("…captions: Bob (recorded)", $(".cap-user .cap-speaker").textContent, "Bob (recorded)");
  const bobEnd = ms(CLIP_SAMPLES["bob-announce"]);
  const gap = env.talk.demo.steps.find((s) => s.id === "carol-finalize").when.gap_ms;
  advance(bobEnd + gap - 10);
  eq(`Carol waits ${gap / 1000} s after Bob's clip ended`, clipFrames("carol-finalize").length, 0);
  advance(20);
  eq("…then speaks", [lastSent("speaker").name, clipFrames("carol-finalize").length >= 1], ["Carol", true]);
  const bobSilence = clipFrames("bob-announce").filter(isSilent).length;
  const due = Math.floor(gap / 100);
  check(`…Bob's silence gave way to Carol's clip (${due - 1} or ${due} of its 12 frames, as the timers fall)`, bobSilence === due || bobSilence === due - 1, String(bobSilence));
  const bobStreamed = clipFrames("bob-announce").reduce((s, m) => s + ms(base64ToInt16(m.pcm).length), 0);
  check("…one stream, never ahead of real time (Bob's ≤ the time before Carol)", bobStreamed <= bobEnd + gap + 1e-6, String(bobStreamed));

  // ---- the intervention, then Carol's correction 0.8 s after it drained. A reply nobody heard (no audio: the player
  // drains it at once) is no cue: Carol never answers a question that wasn't asked aloud.
  advance(ms(CLIP_SAMPLES["carol-finalize"]) + 300);
  const intervening = { slide_id: "discussion", talking_point_id: null, status: "intervening" };
  const listening = { slide_id: "discussion", talking_point_id: null, status: "listening" };
  checkpoint = intervening;
  server({ type: "reply_start", reply: 8, kind: "intervention" });
  state("INTERVENING", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  server({ type: "reply_end", reply: 8 });
  drained(8, 0);
  checkpoint = listening;
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  advance(1500);
  eq("a silent reply drained 1.5 s ago: Carol waits", clipFrames("carol-correct").length, 0);
  checkpoint = intervening;
  server({ type: "reply_start", reply: 9, kind: "intervention" });
  state("INTERVENING", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  server({ type: "reply_audio", reply: 9, pcm: SECOND });
  advance(1500);
  eq("the intervention plays: Carol waits", clipFrames("carol-correct").length, 0);
  drained(9);
  checkpoint = listening;
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  advance(790);
  eq("0.79 s after: still waiting", clipFrames("carol-correct").length, 0);
  advance(10);
  eq("0.8 s after the intervention drained: Carol's correction", clipFrames("carol-correct").length, 1);

  // ---- the decision, as live: a silent save_decision reply, then the spoken confirmation as its own reply (same
  // kind). The script presses Wrap up once the confirmation has played and the room has been quiet 2 s, never over it.
  advance(ms(CLIP_SAMPLES["carol-correct"]) + 1300);
  checkpoint = intervening;
  server({ type: "reply_start", reply: 10, kind: "decision" });
  state("INTERVENING", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  server({ type: "reply_end", reply: 10 });
  drained(10, 0);
  const keys = sent("key").length;
  advance(400);
  server({ type: "reply_start", reply: 11, kind: "decision" });
  for (let i = 0; i < 4; i++) server({ type: "reply_audio", reply: 11, pcm: SECOND });
  server({ type: "reply_end", reply: 11 });
  for (let t = 1; t <= 36; t++) {
    advance(100);
    progress(11, t / 10);
  }
  const confirmationCut = () => node().port.posted.some((p) => p.type === "flush" && p.reply === 11);
  eq("3.6 s into the spoken confirmation (4 s after the silent reply): no key, nothing cut", [sent("key").length - keys, confirmationCut()], [0, false]);
  advance(400);
  drained(11, 4);
  checkpoint = listening;
  state("ROOM", { agenda_phase: "discussion", phase: "human", slide: "discussion" });
  advance(1990);
  eq("1.99 s after the confirmation: no key yet", sent("key").length, keys);
  advance(10);
  eq("2 s of quiet after it: the script presses Wrap up", [sent("key").length - keys, lastSent("key").key, confirmationCut()], [1, "phase", false]);
  check("…disclosed in the chat", chatTexts().includes("The demo script pressed “Wrap up”"));
  eq("the script is done", [demoText(), $(".demo-run").disabled, text(".demo-run"), $(".demo-play").disabled], ["Demo script done", true, "Run demo", true]);
  eq("no Play next ever used", chatTexts().some((t) => t.includes("played by hand")), false);

  // ---- Restart meeting: the script starts over; a timed-out cue → waiting → Play next by hand (disclosed)
  const more = $(".more-button");
  const reset = $(".menu-reset");
  more.fire("click");
  $(".more-menu").fire("click", { target: reset });
  $(".more-menu").fire("click", { target: reset });
  eq("Restart meeting", lastSent("key").key, "reset");
  checkpoint = { slide_id: "agenda", talking_point_id: null, status: "idle" };
  state("IDLE", { agenda_phase: "presentation", phase: "opening", slide: "agenda" });
  eq("…the script back at its start, stopped", [demoText(), text(".demo-run"), $(".demo-run").disabled], ["Next: Run demo starts the presentation (as R does)", "Run demo", false]);
  $(".demo-run").fire("click");
  eq("Run demo again starts the presentation again", [lastSent("key").key, demoText()], ["relay", "Next: Alice — while the facilitator presents rollback-trigger"]);
  advance(89_000);
  eq("no cue for 89 s: not waiting yet", $(".demo-play").dataset.waiting, "false");
  advance(1_000);
  eq("90 s: waiting, Play next highlighted", [$(".demo-play").dataset.waiting, demoText().includes("waiting"), demoText().endsWith("Play next plays it by hand")], ["true", true, true]);
  const framesBefore = clipFrames().length;
  $(".demo-play").fire("click");
  eq("Play next: Alice's clip now", [lastSent("speaker").name, clipFrames().length - framesBefore], ["Alice", 1]);
  eq("…disclosed: the tile, the captions, the strip, the chat", [
    text('.tile-participant[data-name="Alice"] .tile-status'),
    $(".cap-user .cap-speaker").textContent,
    demoText(),
    chatTexts().includes("Alice's clip played by hand (Play next)"),
  ], ["Speaking · played by hand", "Alice (recorded · played by hand)", "Playing: Alice (recorded · played by hand)", true]);

  // ---- Stop demo stops the clip at once (its second of silence still goes out)
  advance(100);
  const stopSource = ctx().sources.at(-1);
  const audioBefore = clipFrames().filter((m) => !isSilent(m)).length;
  $(".demo-run").fire("click");
  eq("Stop demo: the clip stops now", [text(".demo-run"), stopSource.stopAt !== null, tile("Alice").dataset.playing], ["Run demo", true, "false"]);
  advance(1500);
  eq("…no more of the clip, 12 frames of silence", [clipFrames().filter((m) => !isSilent(m)).length - audioBefore, clipFrames().slice(-12).every(isSilent)], [0, true]);
  eq("…Alice is next again (Run carries on there)", demoText().startsWith("Next: Alice"), true);

  // ---- Restart mid-clip: the stream stops at once
  $(".demo-play").fire("click");
  advance(100);
  more.fire("click");
  $(".more-menu").fire("click", { target: reset });
  $(".more-menu").fire("click", { target: reset });
  const atReset = clipFrames().length;
  advance(2000);
  eq("Restart mid-clip: not one more frame", clipFrames().length - atReset, 0);
  eq("…the script back at the start", demoText(), "Next: Run demo starts the presentation (as R does)");
  $(".demo-play").fire("click");
  eq("Play next on the start step: R, by hand", [lastSent("key").key, demoText()], ["relay", "Next: Alice — while the facilitator presents rollback-trigger"]);

  // ---- a dropped server stops the clip (Play next with no server says why)
  $(".demo-play").fire("click");
  sock().drop(1006);
  const atDrop = ctx().sources.at(-1);
  check("a lost server stops the clip", atDrop.stopAt !== null && tile("Alice").dataset.playing === "false");
  $(".demo-play").fire("click");
  check("…Play next while offline says so", $$(".toast").some((t) => t.textContent.includes("not played")));
  advance(300);
  sock().open();
  check("…and no physical mic frames ever", sent("mic").every((m) => m.source === "clip"));

  // ---- the server was restarted meanwhile (a clean take): its hello is back in the opening, so the script starts over
  check("before its hello: the script mid-way", demoText().startsWith("Next: Alice"));
  const opening = { slide_id: "agenda", talking_point_id: null, status: "idle" };
  server({ type: "hello", talk: "rollout-review", version: "v1", mic_mode: "tap", narration_mode: "live", meeting: env.talk.meeting, demo: env.talk.demo, snapshot: { state: "IDLE", phase: "opening", slide: "agenda", connection: { status: "ready", detail: null }, agenda_phase: "presentation", status: "idle", checkpoint: opening, speaker: null } });
  eq("a restarted server: the script back at its start", demoText(), "Next: Run demo starts the presentation (as R does)");
  $(".demo-run").fire("click");
  eq("…Run demo starts the presentation again", [lastSent("key").key, demoText()], ["relay", "Next: Alice — while the facilitator presents rollback-trigger"]);
  $(".demo-run").fire("click"); // (stop it again)

  // ---- a question typed after a clip (the run of show's fallback) is live: never labelled "(recorded)"
  $(".typed").hidden = false;
  $(".typed-input").value = "Is Friday beta or production?";
  $(".typed").fire("submit", { preventDefault() {} });
  eq("a typed question: sent, labelled with the speaker picked", [lastSent("typed").text, $(".cap-user .cap-speaker").textContent], ["Is Friday beta or production?", "Alice"]);
  server({ type: "caption", role: "user", text: "Is Friday beta or production?", final: true, reply: 3 }); // the echo: no speaker
  eq("…the backend's echo keeps it live, not recorded", $(".cap-user .cap-speaker").textContent, "Alice");

  eq("no console errors", env.errors, []);
  print(`demo view: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} demo view check(s) failed`);
}

try {
  await run();
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
