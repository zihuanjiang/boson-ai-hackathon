// Relay presenting live on the deck (contract D52, §4): a narration is a reply (reply_start kind "narration" →
// reply_audio → reply_end → the deck's reply_drained) with the slide shown whole, its caption paged a sentence at a
// time. P flushes it (NARRATING), Esc doesn't (only an answer, REPLYING), Space cuts it and reports what the room
// heard. The rendered backup still arrives as play/cut and keeps its karaoke; an older server's replies (no kind)
// stay answers. This deck has no analyser: Relay's speaking ring falls back to "a reply is playing".
//   jsc -m tests/web/test_live_narration.mjs   (from the repo root; tests/web/run.sh runs everything)
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

const timed = (text) => text.split(" ").map((w, i) => ({ word: w.replace(/[.,]/g, ""), start: 100 + 300 * i, end: 300 + 300 * i, sentence: 0 }));
const HOW = "One two three four.";
env.talk = {
  id: "test",
  version: "v1",
  presenter: "Ken",
  voice: "default",
  mic_mode: "tap",
  narration_mode: "live",
  fake_audio: false,
  slides: [
    { id: "title", index: 0, speaker: "human", mode: null, title: "Title", fragments: 0, chunks: [] },
    {
      id: "how",
      index: 1,
      speaker: "relay",
      mode: null,
      title: "How it works",
      fragments: 3,
      chunks: [{ index: 0, text: HOW, fragment: 1, duration_ms: 1500, words: timed(HOW), sentences: [], audio: "/media/how/0.wav" }],
    },
  ],
};
env.media.set("/media/how/0.wav", 1.5);
env.noAnalyser = true;

const { doc } = install(read("web/index.html"));
const $ = (sel) => doc.querySelector(sel);
const sock = () => env.socket();
const sent = (type) => sock().sent.filter((m) => m.type === type);
const lastSent = (type) => sent(type).at(-1);
const server = (msg) => sock().receive(msg);
const press = (key, code = "") =>
  fireWindow("keydown", { type: "keydown", key, code, repeat: false, target: doc.body, timeStamp: clock.now, preventDefault() {} });
const node = () => env.worklets["pcm-player"];
const posted = () => node().port.posted.map((m) => `${m.type}:${m.reply}`);
const progress = (reply, played) => node().emit({ type: "progress", reply, played, time: clock.now / 1000 });
const SECOND = int16ToBase64(new Int16Array(24000).fill(800)); // 1 s of audio
const liveText = () => $(".cap-live-text").textContent;
const status = () => $(".tile-relay .tile-status").textContent;
const narrating = (narration = "live") =>
  server({ type: "state", state: "NARRATING", phase: "presenting", slide: "how", banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration });
// Five sentences (~230 characters): far more than the caption box holds.
const LONG =
  "Here is the problem we solve. Talks take months to build and minutes to give. Most people freeze when the lights come up. Relay presents for you, and you can cut in anytime. It answers in the same voice and picks up where it left off.";
const LONG_WORDS = LONG.split(" ");

async function run() {
  await import("../../web/js/main.js");
  await settle();
  sock().open();
  server({ type: "hello", talk: "test", version: "v1", mic_mode: "tap", narration_mode: "live", snapshot: { state: "IDLE", phase: "opening", slide: "title", banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null, connection: { status: "ready", detail: null }, talk: "test" } });
  server({ type: "slide", slide: "title", fragment: null });
  $(".arm-button").fire("click");
  await settle(10);
  advance(700);
  eq("joined, no analyser in this browser", [sent("arm").length, env.contexts[0].analysers.length], [1, 0]);

  // ---- the opening answer left a question on the captions; then Relay presents 'how' live
  server({ type: "caption", role: "user", text: "Take it away, Relay.", final: true, reply: 1 });
  server({ type: "slide", slide: "how", fragment: null });
  eq("the slide Relay presents shows whole (all fragments)", Reveal.calls.at(-1), [1, 0, 2]);
  narrating();
  server({ type: "reply_start", reply: 7, kind: "narration" });
  eq("Relay presents", [status(), $(".meeting").dataset.narration], ["Presenting", "live"]);
  eq("the narration plays through the reply player", posted().at(-1), "start:7");
  check("…and the last question's caption line goes", $(".cap-user").hidden);
  for (let i = 0; i < 10; i++) server({ type: "reply_audio", reply: 7, pcm: SECOND });
  server({ type: "caption", role: "assistant", text: LONG, final: false, reply: 7 });
  frame();
  eq("Relay audible (no analyser: a reply is playing): speaking", $(".tile-relay").dataset.speaking, "true");
  progress(7, 24000 * 8);
  const page = liveText();
  const end = LONG_WORDS.indexOf(page.split(" ").at(-1));
  check("a long narration shows one page: the sentence being said, not the whole text", page.length <= 120 && page.length > 0 && page !== LONG, JSON.stringify(page));
  check("…ending with a word heard, starting a sentence", end > 0 && /^[A-Z]/.test(page), JSON.stringify(page));
  server({ type: "reply_end", reply: 7 });
  server({ type: "caption", role: "assistant", text: LONG, final: true, reply: 7 });
  node().emit({ type: "drained", reply: 7, played: 240000, time: clock.now / 1000 });
  eq("a narration drains like any reply", lastSent("reply_drained"), { type: "reply_drained", reply: 7, played_ms: 10000 });
  check("at its end, its last sentence shows", liveText().endsWith("left off.") && liveText().length <= 120, JSON.stringify(liveText()));
  eq("the chat: one short line, not the whole text", doc.querySelectorAll(".chat-line").map((li) => li.querySelector(".chat-text").textContent).at(-1), 'Relay presented "How it works"');
  advance(300);
  frame();
  eq("…and quiet after", $(".tile-relay").dataset.speaking, "false");

  // ---- P while Relay presents live: the narration is flushed, and the backend hears what was heard
  server({ type: "reply_start", reply: 8, kind: "narration" });
  for (let i = 0; i < 3; i++) server({ type: "reply_audio", reply: 8, pcm: SECOND });
  server({ type: "caption", role: "assistant", text: "As I was saying, the browser counts the samples.", final: false, reply: 8 });
  progress(8, 24000);
  press("p");
  eq("P flushes the live narration", posted().at(-1), "flush:8");
  eq("P carries the reply and what the room heard", [lastSent("key").key, lastSent("key").reply, lastSent("key").reply_played_ms], ["pause", 8, 975]);
  check("…and the caption freezes on it", / —$/.test(liveText()), JSON.stringify(liveText()));
  server({ type: "reply_flush", reply: 8 });
  server({ type: "state", state: "PAUSED", phase: "presenting", slide: "how", banner: "Paused — P to resume", thinking: false, can_resume: true, mic_mode: "tap", narration: null });
  eq("paused", [status(), $(".cap-tag").textContent], ["Paused", "Paused"]);
  let mark = node().port.posted.length;
  press("p");
  eq("P again: nothing to silence, the backend resumes", [lastSent("key").key, node().port.posted.length], ["pause", mark]);

  // ---- Esc while Relay presents live does nothing locally (like during a rendered narration)
  narrating();
  server({ type: "reply_start", reply: 9, kind: "narration" });
  for (let i = 0; i < 3; i++) server({ type: "reply_audio", reply: 9, pcm: SECOND });
  server({ type: "caption", role: "assistant", text: "As I was saying, Relay trims its memory to match.", final: false, reply: 9 });
  progress(9, 12000);
  mark = node().port.posted.length;
  press("Escape");
  eq("Esc doesn't touch the narration", [node().port.posted.length, lastSent("key").key], [mark, "esc"]);
  check("…its caption keeps going", !/ —$/.test(liveText()) && liveText().length > 0, JSON.stringify(liveText()));

  // ---- Space cuts it: flushed at once, the key carries what the room heard (for conversation.item.truncate)
  advance(200);
  press(" ", "Space");
  eq("Space flushes the narration", posted().at(-1), "flush:9");
  eq("…and reports the reply and how much was heard", [lastSent("key").key, lastSent("key").reply, lastSent("key").reply_played_ms], ["space", 9, 675]);
  const cut = liveText();
  check("…its caption freezes on the words heard", / —$/.test(cut), JSON.stringify(cut));
  server({ type: "reply_flush", reply: 9 });
  server({ type: "mic", open: true });
  server({ type: "state", state: "LISTENING", phase: "presenting", slide: "how", banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null });
  eq("listening: what Relay was saying stays up", [$(".cap-tag").textContent, liveText(), $(".captions").dataset.mode], ["Listening…", cut, "live"]);
  server({ type: "caption", role: "user", text: "Wait, how does it know?", final: true, reply: 10 });
  server({ type: "mic", open: false });
  server({ type: "state", state: "REPLYING", phase: "presenting", slide: "how", banner: null, thinking: true, can_resume: false, mic_mode: "tap", narration: null });
  server({ type: "reply_start", reply: 10, kind: "answer" });
  eq("the answer: the question stays, the answer replaces the cut line", [$(".cap-user-text").textContent, $(".cap-user").hidden, liveText()], ["Wait, how does it know?", false, ""]);
  server({ type: "reply_audio", reply: 10, pcm: SECOND });
  progress(10, 4800);
  press("Escape");
  eq("Esc during an answer still cuts it", posted().at(-1), "flush:10");

  // ---- the backup: rendered narration arrives as play/cut, with its karaoke
  narrating("backup");
  eq("backup audio on the tile and the topbar", [status(), $(".backup-badge").hidden], ["Presenting · backup audio", false]);
  server({ type: "play", play_id: 1, slide: "how", chunk: 0, offset_ms: 0, fragment: 1, resume_word: null });
  await settle();
  eq("karaoke captions", $(".captions").dataset.mode, "narration");
  advance(500);
  frame();
  check("…following the audio", doc.querySelectorAll(".cap-words .w.past").length >= 1);
  const source = env.contexts[0].sources.at(-1);
  press("p");
  check("P stops the rendered narration", source.stopAt !== null);
  eq("…with its position", [lastSent("key").key, lastSent("key").at && lastSent("key").at.play_id], ["pause", 1]);

  // ---- an older server: no kind, no narration field: replies are answers
  server({ type: "state", state: "REPLYING", phase: "presenting", slide: "how", banner: null, thinking: false, can_resume: false, mic_mode: "tap" });
  server({ type: "caption", role: "user", text: "Old question?", final: true, reply: 11 });
  server({ type: "reply_start", reply: 11 });
  check("an answer keeps the question's line", !$(".cap-user").hidden);
  server({ type: "caption", role: "assistant", text: "An old answer.", final: true, reply: 11 });
  eq("…and the chat shows the answer itself", doc.querySelectorAll(".chat-line").map((li) => li.querySelector(".chat-text").textContent).at(-1), "An old answer.");

  eq("no console errors", env.errors, []);
  print(`live narration: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} live-narration check(s) failed`);
}

try {
  await run();
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
