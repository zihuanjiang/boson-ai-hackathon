// The deck client when things go wrong at the venue: mic permission denied, no AudioWorklet (insecure context),
// one narration chunk missing. Arm must still complete, keys must still work, narration must still advance.
//   jsc -m tests/web/test_flow_degraded.mjs   (from the repo root; tests/web/run.sh runs everything)
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

const words = (text) => {
  let t = 100;
  return text.split(" ").map((w) => {
    const word = { word: w.replace(/[.]/g, ""), start: t, end: t + 200, sentence: 0 };
    t += 300;
    return word;
  });
};
env.talk = {
  id: "test",
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
      title: "How it works",
      fragments: 2,
      chunks: [
        { index: 0, text: "One two three.", fragment: 1, duration_ms: 1200, words: words("One two three."), audio: "/media/how/0.wav" },
        { index: 1, text: "Four five six.", fragment: 2, duration_ms: 1500, words: words("Four five six."), audio: "/media/how/1.wav" },
      ],
    },
  ],
};
env.media.set("/media/how/0.wav", 1.2); // how/1 is missing (404)
env.gumError = Object.assign(new Error("Permission denied"), { name: "NotAllowedError" });
env.noWorklet = true;

const { doc } = install(read("web/index.html"));
const $ = (sel) => doc.querySelector(sel);
const sock = () => env.socket();
const sent = (type) => sock().sent.filter((m) => m.type === type);
const lastSent = (type) => sent(type).at(-1);
const server = (msg) => sock().receive(msg);
const space = () =>
  fireWindow("keydown", {
    type: "keydown",
    key: " ",
    code: "Space",
    repeat: false,
    target: doc.body,
    timeStamp: clock.now,
    preventDefault() {
      this.defaultPrevented = true;
    },
  });

try {
  await import("../../web/js/main.js");
  await settle();
  sock().open();
  server({ type: "connection", status: "error", detail: "BOSON_API_KEY missing" });
  eq("realtime error in checklist", $('[data-item="realtime"]').dataset.status, "fail");
  eq("realtime error dot", $(".rt").dataset.status, "error");

  $(".arm-button").fire("click");
  await settle(10);
  eq("arm still sent", sent("arm").length, 1);
  eq("audio: no worklet → fail", $('[data-item="audio"]').dataset.status, "fail");
  eq("mic: denied → fail", $('[data-item="mic"]').dataset.status, "fail");
  check("mic denial explains what to do", $('[data-item="mic"] .detail').textContent.includes("allow the microphone"));
  eq("narration: one chunk failed", $('[data-item="narration"]').dataset.status, "fail");
  check("failures reported to the server log", sent("log").length >= 3, JSON.stringify(sent("log")));
  check("log levels are warnings", sent("log").every((m) => m.level === "warning"));

  // keys still work
  space();
  eq("space still sent", lastSent("key")?.key, "space");

  // narration still plays (decoded chunk) and advances; the missing chunk plays silently on a timer
  server({ type: "play", play_id: 1, slide: "how", chunk: 0, offset_ms: 0, fragment: 1, resume_word: null });
  const src = env.contexts[0].sources.at(-1);
  check("decoded chunk plays", src && src.startArgs !== null);
  advance(1200);
  src.end();
  eq("chunk_ended 1", lastSent("chunk_ended"), { type: "chunk_ended", play_id: 1 });
  server({ type: "play", play_id: 2, slide: "how", chunk: 1, offset_ms: 300, fragment: 2, resume_word: null });
  await settle();
  check("silent chunk warned", env.warnings.some((w) => w.includes("how/1 plays without audio")));
  advance(600);
  frame();
  check("silent chunk still drives the karaoke", [...doc.querySelectorAll(".cap-words .w.past")].length >= 1);
  advance(700);
  eq("silent chunk ends on its duration", lastSent("chunk_ended"), { type: "chunk_ended", play_id: 2 });

  // a reply without a worklet: nothing can play, so it is drained at once (the turn moves on)
  server({ type: "reply_start", reply: 1 });
  server({ type: "reply_audio", reply: 1, pcm: "AAAA" });
  server({ type: "reply_end", reply: 1 });
  await settle();
  eq("reply drained at once without a worklet", lastSent("reply_drained"), { type: "reply_drained", reply: 1, played_ms: 0 });

  eq("no console errors", env.errors, []);
  print(`degraded flow: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} degraded-flow check(s) failed`);
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
