// The meeting window (contract D55, §6): the real index.html + main.js in the fake browser. The join dialog, the
// Relay tile's status for every state, the mic button in tap and hold mode, Leave / Rejoin, the I key, the More
// menu, the panels (and deck.layout() after each), the chat, the speaking rings and the muted-mic hint.
//   jsc -m tests/web/test_meeting.mjs   (from the repo root; tests/web/run.sh runs everything)
import { advance, clock, env, frame, install, settle } from "./fake_browser.mjs";

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

const words = (text, t0 = 100) =>
  text.split(" ").map((w, i) => ({ word: w.replace(/[.,]/g, ""), start: t0 + 300 * i, end: t0 + 300 * i + 200, sentence: 0 }));
const chunk = (slide, index, text, fragment = null) => ({
  index,
  text,
  fragment,
  duration_ms: 300 * text.split(" ").length + 200,
  words: words(text),
  sentences: [],
  audio: `/media/${slide}/${index}.wav`,
});
env.talk = {
  id: "test",
  version: "v1",
  presenter: "Ken",
  voice: "default",
  mic_mode: "tap",
  narration_mode: "live",
  fake_audio: true,
  slides: [
    { id: "title", index: 0, speaker: "human", mode: null, title: "Relay test talk", fragments: 0, chunks: [] },
    { id: "how", index: 1, speaker: "relay", mode: null, title: "How it works", fragments: 2, chunks: [chunk("how", 0, "One two three.", 1)] },
    { id: "qa", index: 2, speaker: "relay", mode: "qa", title: "Questions?", fragments: 0, chunks: [chunk("qa", 0, "Ask me.")] },
  ],
};
for (const s of env.talk.slides) for (const c of s.chunks) env.media.set(c.audio, c.duration_ms / 1000);

const { doc } = install(read("web/index.html"));
const $ = (sel) => doc.querySelector(sel);
const sock = () => env.socket();
const sent = (type) => sock().sent.filter((m) => m.type === type);
const lastSent = (type) => sent(type).at(-1);
const server = (msg) => sock().receive(msg);
function press(key, code = "", extra = {}) {
  return fireWindow("keydown", {
    type: "keydown",
    key,
    code,
    repeat: false,
    target: doc.activeElement ?? doc.body,
    timeStamp: clock.now,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    ...extra,
  });
}
const status = () => $(".tile-relay .tile-status").textContent;
const state = (st, extra = {}) =>
  server({ type: "state", state: st, phase: "presenting", slide: "how", banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null, ...extra });
const chatLines = () => doc.querySelectorAll(".chat-log .chat-line").map((li) => `${li.dataset.kind}: ${li.querySelector(".chat-text").textContent}`);
const keys = () => sent("key").map((k) => k.key);
const micNode = () => env.worklets["mic-capture"];
const micFrame = (rms) => micNode().emit({ type: "frame", pcm: new Int16Array(2400), rms });

async function run() {
  await import("../../web/js/main.js");
  await settle();
  sock().open();

  // ---- the join dialog and the meeting's names, before joining
  eq("dialog title = the talk", $(".panel-title").textContent, "Relay test talk");
  eq("dialog sub line", $(".panel-sub").textContent, "Relay presents · Ken hosts");
  eq("join button", $(".arm-button").textContent, "Join with Computer Audio");
  eq("the narration row is the backup (live mode)", $('[data-item="narration"] .label').textContent, "Backup narration");
  eq("topbar: the talk", [$(".talk-name").textContent, $(".talk-name").hidden], ["Relay test talk", false]);
  check("topbar: Fake badge (fake renders)", !$(".fake-pill").hidden);
  eq("You tile: initial and role", [$(".tile-you .avatar-text").textContent, $(".tile-you .role").textContent], ["K", "Ken · host"]);
  eq("presenter name in the More menu and the left card", doc.querySelectorAll(".presenter-name").map((e) => e.textContent), ["Ken", "Ken"]);
  eq("Relay tile before anything: waiting for the presenter", status(), "Waiting for Ken");
  eq("live badge before the server says", [$(".live-badge").dataset.status, $(".live-text").textContent], ["offline", "Higgs Realtime offline"]);
  eq("speaker labels on the captions", [$(".cap-live .cap-speaker").textContent, $(".cap-narration .cap-speaker").textContent, $(".cap-user .cap-speaker").textContent], ["Relay", "Relay", "You"]);

  // ---- a talk without a meeting looks exactly as before On the Same Page (contract v3 §7)
  eq("plain talk: Relay's names", [$(".brand-name").textContent, $(".tile-relay .name").textContent, $(".tile-relay .role").textContent, $(".viewing-text").textContent, $(".tile-you .name").textContent], ["Relay", "Relay", "AI co-presenter", "You are viewing Relay's screen", "You"]);
  eq("…Relay in the menu, the keys and the left card", [...doc.querySelectorAll(".ai-name"), ...doc.querySelectorAll(".ai-name-cap")].map((e) => e.textContent), ["Relay", "Relay", "Relay", "Relay", "Relay", "Relay"]);
  eq(
    "…no agenda strip, tabs, callout, speaker picker or Restart meeting",
    [$(".agenda").hidden, $(".tabs").hidden, $(".callout").hidden, $(".speakers").hidden, $(".menu-reset").hidden, $(".meeting").classList.contains("has-agenda")],
    [true, true, true, true, true, false],
  );
  eq("…the chat is the chat", [$(".chat").hidden, $(".chat-title").hidden, $(".chat-log").hidden, $(".chat-button .tb-label").textContent, $(".chat-input").getAttribute("placeholder")], [true, false, false, "Chat", "Ask Relay a question…"]);
  eq("…the typed box asks Relay", [$(".typed-label").textContent, $(".typed-input").getAttribute("placeholder")], ["Ask Relay", "Type a question · Enter sends · Esc cancels"]);
  $(".mic-button").fire("click");
  eq("mic button before joining: no key", sent("key").length, 0);
  check("…the join dialog is what it shows", !$(".overlay").hidden);
  press("i");
  check("I opens the meeting info before joining too", !$(".strip").hidden);
  press("I");
  check("I again closes it", $(".strip").hidden);

  server({ type: "hello", talk: "test", version: "v1", mic_mode: "tap", narration_mode: "rendered", snapshot: { state: "IDLE", phase: "opening", slide: "title", banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null, connection: { status: "connecting", detail: null }, talk: "test" } });
  eq("a rendered-mode server: the narration row is the narration", $('[data-item="narration"] .label').textContent, "Narration");
  server({ type: "hello", talk: "test", version: "v1", mic_mode: "tap", narration_mode: "live", snapshot: { state: "IDLE", phase: "opening", slide: "title", banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null, connection: { status: "ready", detail: null }, talk: "test" } });
  eq("…and live again: the backup", $('[data-item="narration"] .label').textContent, "Backup narration");
  server({ type: "slide", slide: "title", fragment: null });
  server({ type: "connection", status: "ready", detail: null });
  eq("live badge: ready", [$(".live-badge").dataset.status, $(".live-text").textContent], ["ready", "Higgs Realtime"]);
  server({ type: "connection", status: "reconnecting", detail: "socket closed" });
  eq("live badge: reconnecting, with why on hover", [$(".live-badge").dataset.status, $(".live-text").textContent, $(".live-badge").title], ["reconnecting", "Higgs Realtime reconnecting…", "Higgs Realtime: reconnecting (socket closed)"]);
  server({ type: "connection", status: "ready", detail: null });
  eq("chat: the slide on screen", chatLines(), ["slide: Slide: Relay test talk"]);

  // ---- join: every check passes, so the dialog closes by itself 600 ms later
  $(".arm-button").fire("click");
  await settle(10);
  eq("joined", sent("arm").length, 1);
  check("the dialog waits a moment", !$(".overlay").hidden);
  advance(599);
  check("…599 ms: still up", !$(".overlay").hidden);
  advance(1);
  check("…600 ms: closed by itself", $(".overlay").hidden);
  frame();
  eq("the clock starts at the join", $(".clock").textContent, "00:00");
  advance(65_000);
  frame();
  eq("…and counts", $(".clock").textContent, "01:05");

  // ---- the Relay tile says what Relay is doing
  const cases = [
    [{ state: "IDLE", phase: "opening" }, "Waiting for Ken"],
    [{ state: "LISTENING" }, "Listening…"],
    [{ state: "REPLYING", thinking: true }, "Thinking…"],
    [{ state: "REPLYING" }, "Answering"],
    [{ state: "NARRATING", narration: "live" }, "Presenting"],
    [{ state: "NARRATING", narration: "rendered" }, "Presenting"],
    [{ state: "NARRATING", narration: "backup" }, "Presenting · backup audio"],
    [{ state: "PAUSED" }, "Paused"],
    [{ state: "IDLE", phase: "qa" }, "Waiting for a question"],
    [{ state: "IDLE", phase: "human" }, "Ken has the floor"],
    [{ state: "IDLE" }, "Waiting"],
  ];
  for (const [msg, want] of cases) {
    state(msg.state, msg);
    eq(`Relay tile in ${JSON.stringify(msg)}`, status(), want);
  }
  state("REPLYING", { thinking: true });
  check("thinking pulses the tile", $(".tile-relay").classList.contains("thinking"));
  state("NARRATING", { narration: "backup" });
  eq("backup narration: the topbar says so", [$(".backup-badge").hidden, $(".meeting").dataset.state, $(".meeting").dataset.narration], [false, "NARRATING", "backup"]);
  server({ type: "state", state: "NARRATING", phase: "presenting", slide: "how", banner: null, thinking: false, can_resume: false, mic_mode: "tap" });
  eq("an older server (no narration field): Presenting, no backup badge", [status(), $(".backup-badge").hidden], ["Presenting", true]);
  state("IDLE", { banner: "Resuming…" });
  server({ type: "countdown", seconds: 2 });
  eq("the resume countdown", status(), "Resuming…");
  server({ type: "countdown", seconds: 0 });
  eq("…cancelled", status(), "Waiting");
  state("IDLE", { phase: "opening", banner: "Starting…" });
  server({ type: "countdown", seconds: 2 });
  eq("the opening watchdog's countdown", status(), "Starting…");
  press("Escape");
  eq("a key cancels a countdown on the tile too", status(), "Waiting for Ken");
  state("IDLE", { phase: "opening" });

  // ---- the mic button, tap mode
  const mic = $(".mic-button");
  eq("tap: Unmute", [$(".mic-button .tb-label").textContent, mic.title, mic.getAttribute("aria-pressed")], ["Unmute", "Interrupt Relay and speak (Space)", "false"]);
  let n = sent("key").length;
  mic.fire("click");
  eq("Unmute = Space", [sent("key").length - n, lastSent("key").key], [1, "space"]);
  server({ type: "mic", open: true });
  state("LISTENING");
  eq("mic open: Mute", [$(".mic-button .tb-label").textContent, mic.title, mic.getAttribute("aria-pressed")], ["Mute", "Done talking: send it to Relay now (Space)", "true"]);
  eq("You tile: unmuted, Relay listening", [$(".tile-you .mic-ico").dataset.muted, $(".tile-you .tile-status").textContent], ["false", "Relay is listening"]);
  micFrame(0.1);
  eq("talking with the mic open: your speaking border", $(".tile-you").dataset.speaking, "true");
  advance(300);
  micFrame(0.01);
  eq("…gone in a pause", $(".tile-you").dataset.speaking, "false");
  mic.fire("click");
  eq("Mute = Space again (send it now)", lastSent("key").key, "space");
  server({ type: "mic", open: false });
  state("REPLYING", { thinking: true });
  eq("mic closed: Unmute, muted icon", [$(".mic-button .tb-label").textContent, $(".tile-you .mic-ico").dataset.muted, $(".tile-you .tile-status").textContent], ["Unmute", "true", ""]);
  micFrame(0.3);
  eq("talking with the mic closed: no speaking border", $(".tile-you").dataset.speaking, "false");
  state("IDLE", { phase: "opening" });

  // ---- hold mode: press = Space down, release (or leaving the button) = Space up
  state("IDLE", { phase: "opening", mic_mode: "hold" });
  eq("hold: Hold to talk", $(".mic-button .tb-label").textContent, "Hold to talk");
  n = sent("key").length;
  mic.fire("click");
  eq("hold: a click alone sends nothing", sent("key").length, n);
  mic.fire("pointerdown", { button: 0, pointerId: 1 });
  eq("hold: press = space", lastSent("key").key, "space");
  server({ type: "mic", open: true });
  state("LISTENING", { mic_mode: "hold" }); // every state repeats the mode: the press goes on
  mic.fire("pointerup", { button: 0, pointerId: 1 });
  advance(150);
  eq("hold: release = space_up (after the last mic frame)", lastSent("key").key, "space_up");
  server({ type: "mic", open: false });
  state("IDLE", { phase: "opening", mic_mode: "hold" });
  mic.fire("pointerdown", { button: 0, pointerId: 1 });
  mic.fire("pointerleave", { pointerId: 1 });
  advance(150);
  eq("hold: sliding off the button releases too", keys().slice(-2), ["space", "space_up"]);
  n = sent("key").length;
  mic.fire("pointerup", { pointerId: 1 });
  advance(150);
  eq("…once", sent("key").length, n);
  mic.fire("pointerdown", { button: 2, pointerId: 1 });
  eq("hold: a right click is not a press", sent("key").length, n);
  state("IDLE", { phase: "opening", mic_mode: "tap" });
  eq("back to tap", $(".mic-button .tb-label").textContent, "Unmute");

  // ---- ◀ ▶, Leave and Rejoin
  $(".next-button").fire("click");
  $(".prev-button").fire("click");
  eq("▶ and ◀ = next and prev", keys().slice(-2), ["next", "prev"]);
  $(".leave-button").fire("click");
  eq("Leave = the human key", lastSent("key").key, "human");
  check("…and the left card", !$(".left-card").hidden);
  $(".rejoin-button").fire("click");
  check("Rejoin hides it", $(".left-card").hidden);
  $(".leave-button").fire("click");
  press("r");
  eq("any presenter key hides it too (and acts)", [$(".left-card").hidden, lastSent("key").key], [true, "relay"]);

  // ---- the meeting info panel: ⓘ and I (not in a text box)
  $(".info-button").fire("click");
  eq("ⓘ opens the meeting info", [$(".strip").hidden, $(".info-button").getAttribute("aria-expanded")], [false, "true"]);
  press("i");
  check("I closes it", $(".strip").hidden);
  n = sent("key").length;
  press("t");
  press("i");
  check("I in the typed box is just a letter", $(".strip").hidden);
  press("Escape");
  eq("…and no key went out", sent("key").length, n);

  // ---- More (the fake DOM doesn't bubble: a click on an item reaches the menu's listener with that target)
  const more = $(".more-button");
  const item = (action) => doc.querySelector(`.more-menu [data-action="${action}"]`);
  const choose = (action, target = item(action)) => $(".more-menu").fire("click", { target });
  more.fire("click");
  eq("More opens its menu", [$(".more-menu").hidden, more.getAttribute("aria-expanded")], [false, "true"]);
  choose("pause");
  eq("More → pause = P, menu closed", [lastSent("key").key, $(".more-menu").hidden, more.getAttribute("aria-expanded")], ["pause", true, "false"]);
  for (const action of ["human", "relay", "prev", "next"]) {
    more.fire("click");
    choose(action, item(action).querySelector("span")); // the label inside the item
    eq(`More → ${action}`, lastSent("key").key, action);
  }
  more.fire("click");
  choose("type");
  check("More → type a question focuses the box", !$(".typed").hidden && doc.activeElement === $(".typed-input"));
  press("Escape");
  more.fire("click");
  choose("info");
  check("More → meeting info", !$(".strip").hidden);
  $(".info-button").fire("click");
  more.fire("click");
  choose("checklist");
  check("More → audio check opens the join dialog (Relay waiting)", !$(".overlay").hidden);
  $(".done-button").fire("click");
  check("…Close closes it", $(".overlay").hidden);
  state("NARRATING", { narration: "live" });
  more.fire("click");
  choose("checklist");
  check("…not while Relay presents", $(".overlay").hidden);
  check("…and says why", doc.querySelectorAll(".toast").some((t) => /opens while Relay is waiting/.test(t.textContent)));
  state("IDLE", { phase: "presenting" });
  n = sent("key").length;
  more.fire("click");
  press("Escape");
  eq("Esc over the open menu only closes it", [$(".more-menu").hidden, sent("key").length], [true, n]);
  more.fire("click");
  fireDocument("pointerdown", { target: $(".share") });
  check("a click elsewhere closes it", $(".more-menu").hidden);
  more.fire("click");
  fireDocument("pointerdown", { target: more });
  check("…but not a click on More itself", !$(".more-menu").hidden);
  more.fire("click");
  check("More again closes it", $(".more-menu").hidden);

  // ---- panels: each change re-fits the slides
  let layouts = env.layouts;
  $(".people-button").fire("click");
  eq("Participants off: the tiles go, and the side column with them", [$(".people").hidden, $(".people-button").getAttribute("aria-pressed"), $(".meeting").classList.contains("side-off")], [true, "false", true]);
  eq("…the slides re-fit", env.layouts - layouts, 1);
  $(".chat-button").fire("click");
  eq("Chat on: the side column is back for it", [$(".chat").hidden, $(".chat-button").getAttribute("aria-pressed"), $(".meeting").classList.contains("side-off")], [false, "true", false]);
  $(".people-button").fire("click");
  $(".chat-close").fire("click");
  eq("tiles back, chat closed", [$(".people").hidden, $(".chat").hidden], [false, true]);
  $(".cc-button").fire("click");
  eq("Captions off", [$(".meeting").classList.contains("captions-off"), $(".cc-button").getAttribute("aria-pressed")], [true, "false"]);
  $(".cc-button").fire("click");
  check("Captions on", !$(".meeting").classList.contains("captions-off"));
  eq("every panel change re-fit the slides", env.layouts - layouts, 6);
  const observer = env.resizeObservers[0];
  eq("a ResizeObserver watches the shared screen", observer && observer.targets.map((t) => t.className), ["share"]);
  layouts = env.layouts;
  observer.trigger();
  eq("…and re-fits the slides when it changes size", env.layouts - layouts, 1);

  // ---- chat: tools (updated in place), what was said, slides, typed questions; unread count while closed
  server({ type: "tool", call_id: "c1", name: "goto_slide", args: { slide_id: "qa" }, status: "queued" });
  const toolLine = () => doc.querySelector('.chat-line[data-key="tool:c1"]');
  eq("a tool call", [toolLine().querySelector(".chat-tool").textContent, toolLine().querySelector(".chat-status").textContent], ['goto_slide("qa")', "queued"]);
  const count = doc.querySelectorAll(".chat-line").length;
  server({ type: "tool", call_id: "c1", name: "goto_slide", args: { slide_id: "qa" }, status: "applied" });
  eq("…its status updates in place", [doc.querySelectorAll(".chat-line").length, toolLine().querySelector(".chat-status").dataset.status], [count, "applied"]);
  server({ type: "slide", slide: "how", fragment: null });
  server({ type: "slide", slide: "how", fragment: null });
  server({ type: "caption", role: "user", text: "What does Relay do?", final: false, reply: 4 });
  server({ type: "caption", role: "user", text: "What does Relay do?", final: true, reply: 4 });
  server({ type: "reply_start", reply: 4, kind: "answer" });
  server({ type: "caption", role: "assistant", text: "It presents", final: false, reply: 4 });
  server({ type: "caption", role: "assistant", text: "It presents your slides.", final: true, reply: 4 });
  eq("chat: a slide once, finals only", chatLines().slice(-3), ["slide: Slide: How it works", "you: What does Relay do?", "relay: It presents your slides."]);
  eq("unread messages while the chat is closed", [$(".chat-button .tb-badge").hidden, $(".chat-button .tb-badge").textContent], [false, "2"]);
  $(".chat-button").fire("click");
  check("opening the chat reads them", $(".chat-button .tb-badge").hidden);
  $(".chat-input").value = "  Can you skip to the questions?  ";
  $(".chat-compose").fire("submit");
  eq("a question typed in the chat goes to Relay", [lastSent("typed").text, $(".chat-input").value], ["Can you skip to the questions?", ""]);
  server({ type: "caption", role: "user", text: "Can you skip to the questions?", final: true, reply: 5 });
  eq("…shown once (its caption echo is not repeated)", chatLines().filter((l) => l.includes("skip to the questions")).length, 1);
  server({ type: "reply_start", reply: 6, kind: "narration" });
  server({ type: "caption", role: "assistant", text: "(fake) Live narration of slide how.", final: true, reply: 6 });
  eq("a slide presented live: one short line", chatLines().at(-1), 'presented: Relay presented "How it works"');
  $(".chat-button").fire("click");

  // ---- Relay's speaking ring follows its real output level
  env.outputLevel = 0.1;
  frame();
  eq("Relay audible: speaking border and ring", [$(".tile-relay").dataset.speaking, Number($(".tile-relay .ring").style["--level"]) > 0.3], ["true", true]);
  env.outputLevel = 0;
  advance(100);
  frame();
  eq("…held through a short gap", $(".tile-relay").dataset.speaking, "true");
  advance(200);
  frame();
  eq("…then quiet", [$(".tile-relay").dataset.speaking, $(".tile-relay .ring").style["--level"]], ["false", "0"]);

  // ---- someone talks into the muted mic while Relay is quiet: a hint, at most every 20 s, for 5 s
  state("IDLE", { phase: "presenting" });
  const talk = (ms, rms = 0.2) => {
    for (let t = 0; t < ms; t += 100) {
      micFrame(rms);
      advance(100);
      frame();
    }
  };
  advance(600);
  frame();
  talk(1000);
  check("talking 1 s into the muted mic: no hint yet", $(".tile-hint").hidden);
  talk(300);
  check("1.2 s: the hint", !$(".tile-hint").hidden);
  advance(5000);
  check("…gone after 5 s", $(".tile-hint").hidden);
  talk(3000);
  check("…not again within 20 s", $(".tile-hint").hidden);
  advance(12000);
  frame();
  talk(1300);
  check("…again after 20 s", !$(".tile-hint").hidden);
  server({ type: "mic", open: true });
  check("the mic opening hides it", $(".tile-hint").hidden);
  server({ type: "mic", open: false });
  advance(21000);
  frame();
  env.outputLevel = 0.1;
  talk(1500);
  check("not while Relay speaks (its own voice in the mic)", $(".tile-hint").hidden);
  env.outputLevel = 0;
  state("LISTENING");
  advance(1000);
  frame();
  talk(1500);
  check("not while Relay listens", $(".tile-hint").hidden);
  for (const phase of ["opening", "human"]) {
    state("IDLE", { phase });
    advance(21000);
    frame();
    talk(1500);
    check(`not while the presenter has the floor (${phase}): it's their own voice`, $(".tile-hint").hidden);
  }
  state("IDLE", { phase: "qa" });
  advance(21000);
  frame();
  talk(1300);
  check("…but on the Q&A slide, waiting for a question: the hint", !$(".tile-hint").hidden);
  state("IDLE", { phase: "presenting" });

  // ---- a plain talk ignores the meeting's messages (an odd server): nothing shows, nothing breaks
  const before = doc.querySelectorAll(".chat-line").length;
  server({ type: "meeting", phase: "discussion", decisions: [{ id: "D1", decision: "x", owner: "Bob" }] });
  server({ type: "evidence", lookup: { id: "E1", query: "q", results: [{ source_id: "REQ-1", excerpt: "x" }] } });
  server({ type: "transcript", utterance: { id: "U1", text: "hello", speaker: null } });
  server({ type: "alignment", event: { id: "AL1", type: "intervene", utterance_id: "U1" } });
  server({ type: "reply_start", reply: 40, kind: "intervention" });
  eq(
    "plain talk: the meeting's messages change nothing",
    [$(".agenda").hidden, $(".callout").hidden, [".lookup", ".utt", ".align-card", ".note"].reduce((n, sel) => n + doc.querySelectorAll(sel).length, 0), doc.querySelectorAll(".chat-line").length, $(".tabs").hidden],
    [true, true, 0, before, true],
  );
  server({ type: "reply_flush", reply: 40 });

  // ---- our own server drops: the topbar says so
  sock().drop(1006);
  check("server offline pill", !$(".server-pill").hidden);
  advance(300);
  sock().open();
  check("…gone when it is back", $(".server-pill").hidden);

  eq("no console errors", env.errors, []);
  print(`meeting window: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} meeting check(s) failed`);
}

try {
  await run();
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
