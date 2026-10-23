// The Relay server restarts while the deck tab stays open and armed (it reconnects and re-arms by itself):
//  - same talk version (a plain restart): the deck carries on. The new server numbers its live replies from 1
//    again, so its first reply must play even when the deck's last reply was also reply 1 (drained or flushed);
//  - another version (re-render or edit, then restart): the page's slides, word timings and decoded audio are
//    stale, so the deck says why, ignores the new server meanwhile, and reloads once (then Arm again).
//   jsc -m tests/web/test_restart.mjs   (from the repo root; tests/web/run.sh runs everything)
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

const words = ["One", "two", "three"].map((word, i) => ({ word, start: 100 + 300 * i, end: 300 + 300 * i, sentence: 0 }));
env.talk = {
  id: "test",
  version: "v1",
  presenter: "Ken",
  voice: "chloe",
  mic_mode: "tap",
  fake_audio: false,
  slides: [
    { id: "title", index: 0, speaker: "human", mode: null, title: "Title", fragments: 0, chunks: [] },
    {
      id: "how",
      index: 1,
      speaker: "relay",
      mode: null,
      title: "How",
      fragments: 0,
      chunks: [{ index: 0, text: "One two three.", fragment: null, duration_ms: 1200, words, sentences: [], audio: "/media/how/0.wav" }],
    },
  ],
};
env.media.set("/media/how/0.wav", 1.2);
const { doc } = install(read("web/index.html"));
const $ = (sel) => doc.querySelector(sel);
const sock = () => env.socket();
const server = (msg) => sock().receive(msg);
const sent = (type) => sock().sent.filter((m) => m.type === type);
const node = () => env.worklets["pcm-player"];
const posted = () => node().port.posted.map((m) => `${m.type}:${m.reply}`);
const toasts = () => doc.querySelectorAll(".toast").map((t) => t.textContent);
const HALF_SECOND = int16ToBase64(new Int16Array(12000).fill(1000)); // 12000 samples = 500 ms
const space = () =>
  fireWindow("keydown", { type: "keydown", key: " ", code: "Space", repeat: false, target: doc.body, timeStamp: clock.now, preventDefault() {} });

/** The server process goes away (Ctrl-C) and a new one accepts the deck's next attempt. */
function reconnect() {
  const before = env.sockets.length;
  sock().drop(1006);
  advance(300); // the deck's backoff
  eq("reconnected", env.sockets.length, before + 1);
  sock().open();
  eq("hello + arm again", sock().sent.map((m) => m.type), ["hello", "arm"]);
}

/** The new server's greeting bundle (design §4 hello: talk, version, mic_mode, snapshot). */
function greet(version, talk = "test") {
  server({
    type: "hello",
    talk,
    version,
    mic_mode: "tap",
    snapshot: { state: "IDLE", phase: "opening", slide: "title", banner: null, thinking: false, can_resume: false, mic_mode: "tap", connection: { status: "ready", detail: null }, talk },
  });
  server({ type: "stop" });
  server({ type: "mic", open: false });
  server({ type: "slide", slide: "title", fragment: null });
  server({ type: "state", state: "IDLE", phase: "opening", slide: "title", banner: null, thinking: false, can_resume: false, mic_mode: "tap" });
}

/** The new server's opening turn: tap, mic open → close, then its reply 1 (500 ms of audio, a caption, end). */
function newServersReplyOne(label) {
  space();
  server({ type: "mic", open: true });
  server({ type: "mic", open: false });
  const mark = node().port.posted.length;
  server({ type: "reply_start", reply: 1 });
  server({ type: "reply_audio", reply: 1, pcm: HALF_SECOND });
  server({ type: "caption", role: "assistant", text: "Thanks, Ken.", final: true, reply: 1 });
  server({ type: "reply_end", reply: 1 });
  eq(`${label}: reply 1 reaches the player`, posted().slice(mark), ["start:1", "push:1", "end:1"]);
  node().emit({ type: "progress", reply: 1, played: 9000, time: clock.now / 1000 });
  eq(`${label}: its caption shows with the audio`, $(".cap-live-text").textContent, "Thanks, Ken.");
  node().emit({ type: "drained", reply: 1, played: 12000, time: clock.now / 1000 });
  eq(`${label}: reply_drained goes out`, sent("reply_drained"), [{ type: "reply_drained", reply: 1, played_ms: 500 }]);
}

async function run() {
  await import("../../web/js/main.js");
  await settle();
  sock().open();
  greet("v1");
  $(".arm-button").fire("click");
  await settle(10);
  eq("armed", sent("arm").length, 1);
  const fetches = env.fetches.length;

  // ---- run 1: one live reply (reply 1) plays out; then a plain restart (same version)
  server({ type: "reply_start", reply: 1 });
  server({ type: "reply_audio", reply: 1, pcm: HALF_SECOND });
  server({ type: "reply_end", reply: 1 });
  node().emit({ type: "drained", reply: 1, played: 12000, time: clock.now / 1000 });
  eq("run 1: reply 1 drained", sent("reply_drained").map((m) => m.reply), [1]);
  reconnect();
  greet("v1");
  newServersReplyOne("after a drained reply 1");

  // ---- run 3: its reply 1 is cut by a tap (flushed); then another plain restart
  reconnect();
  greet("v1");
  server({ type: "reply_start", reply: 1 });
  server({ type: "reply_audio", reply: 1, pcm: HALF_SECOND });
  space();
  eq("run 3: the tap flushes reply 1", posted().at(-1), "flush:1");
  reconnect();
  greet("v1");
  newServersReplyOne("after a flushed reply 1");

  eq("same version: no reload", env.reloads, 0);
  eq("same version: nothing fetched again", env.fetches.length, fetches);
  check("same version: no reload toast", !toasts().some((t) => /reload/i.test(t)), JSON.stringify(toasts()));

  // ---- a re-render, then a restart: the new server presents version v2
  reconnect();
  const keysBefore = sent("key").length;
  greet("v2");
  check("toast says why it reloads", toasts().some((t) => /changed on the server/.test(t) && /reloading/.test(t)), JSON.stringify(toasts()));
  eq("the toast shows before the reload", env.reloads, 0);
  const sources = env.contexts[0].sources.length;
  server({ type: "play", play_id: 7, slide: "how", chunk: 0, offset_ms: 0, fragment: null, resume_word: null });
  eq("meanwhile no stale narration plays", env.contexts[0].sources.length, sources);
  server({ type: "reply_start", reply: 2 });
  eq("…and no reply starts", posted().at(-1), "end:1");
  space();
  eq("…and keys do nothing", sent("key").length, keysBefore);
  advance(1500);
  eq("the page reloads once", env.reloads, 1);
  eq("the reload is remembered (loop guard)", env.session.get("relay.staleReload"), "test@v1 → test@v2");
  advance(5000);
  eq("only once", env.reloads, 1);

  eq("no console errors", env.errors, []);
  print(`restart: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} restart check(s) failed`);
}

try {
  await run();
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
