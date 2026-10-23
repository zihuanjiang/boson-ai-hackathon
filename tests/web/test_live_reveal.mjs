// Live reply captions against what the room has heard. The REAL deck client (index.html + main.js in the fake
// browser) is wired to the REAL pcm-player worklet processor and run one 128-sample render quantum at a time, while
// the server side replays Higgs-like delivery: 20 ms audio deltas sent ahead of real time (or stalling), and the
// accumulated transcript arriving word by word, a sentence at a time before its audio, all at once, or after its
// audio. The caption must never run ahead of the voice, and a cut must freeze on the words heard.
//   jsc -m tests/web/test_live_reveal.mjs [-- -v]   (from the repo root; tests/web/run.sh runs everything)
import { advance, clock, env, install, settle } from "./fake_browser.mjs";

const VERBOSE = (/** @type {any} */ (globalThis).arguments ?? []).includes("-v"); // print each scenario's numbers
let passed = 0;
let failed = 0;
function check(name, ok, detail = "") {
  if (ok) passed++;
  else {
    failed++;
    print(`FAIL ${name}${detail ? `: ${detail}` : ""}`);
  }
}

// ---- the real pcm-player processor in a stubbed AudioWorkletGlobalScope
globalThis.currentTime = 0;
/** @type {Record<string, any>} */
const registered = {};
globalThis.registerProcessor = (name, cls) => {
  registered[name] = cls;
};
globalThis.AudioWorkletProcessor = class {
  constructor() {
    /** @type {any[]} */
    const posted = [];
    this.port = { onmessage: null, posted, postMessage: (msg) => posted.push(msg) };
  }
};
(0, eval)(read("web/worklets/pcm-player.js"));

// ---- boot the real deck and arm it
env.talk = {
  id: "test",
  presenter: "Ken",
  voice: "chloe",
  mic_mode: "tap",
  fake_audio: false,
  slides: [{ id: "title", index: 0, speaker: "human", mode: null, title: "Title", fragments: 0, chunks: [] }],
};
const { doc } = install(read("web/index.html"));
await import("../../web/js/main.js");
await settle();
env.socket().open();
doc.querySelector(".arm-button").fire("click");
await settle(10);
const sock = env.socket();
const node = env.worklets["pcm-player"];
if (!node) throw new Error("no pcm-player node: Arm failed");
const proc = new registered["pcm-player"]();
let toWorklet = 0;
let toMain = 0;
const pumpToWorklet = () => {
  while (toWorklet < node.port.posted.length) {
    const { transferred, ...msg } = node.port.posted[toWorklet++];
    proc.port.onmessage({ data: msg });
  }
};
const pumpToMain = () => {
  while (toMain < proc.port.posted.length) node.emit(proc.port.posted[toMain++]);
};
const liveText = () => doc.querySelector(".cap-live-text").textContent;
const countWords = (s) => s.replace(/ —$/, "").split(/\s+/).filter(Boolean).length;

// ---- the reply: the brief's plain-language answer, ~3.75 words/s
const SENTENCES = [
  "Sure.",
  "Think of me as an actor with a script and an earpiece: the script is rehearsed, the earpiece lets Ken cut in anytime, and I always know which line I stopped on.",
  "As I was saying.",
];
const ALL = SENTENCES.join(" ").split(" ");
const N = ALL.length;
const DELTA = 480; // samples per audio delta (20 ms)
const TOTAL = Math.round(((N / 3.75) * 24000) / DELTA) * DELTA;
const NDELTAS = TOTAL / DELTA;
const LATENCY = 600; // the fake AudioContext's output + base latency (25 ms) in samples
const QMS = (128 / 24000) * 1000;
const PCM = "A".repeat(1280); // 480 samples of silence, base64
const wordStart = (j) => Math.floor((j * TOTAL) / N);
const sentenceFirst = SENTENCES.map((_, s) => SENTENCES.slice(0, s).join(" ").split(" ").filter(Boolean).length);
const sentenceEnd = SENTENCES.map((s, k) => sentenceFirst[k] + s.split(" ").length);
/** words actually being heard after `heard` samples (uniform speech), the word in progress included */
const idealWords = (heard) => (heard > 0 ? Math.min(N, Math.max(1, Math.ceil((N * heard) / TOTAL))) : 0);

/** ms after reply_start at which audio delta i reaches the deck */
function deliverAt(i, audio) {
  const fast = (i * 20) / 6; // Higgs generates ~6× real time
  switch (audio) {
    case "steady":
      return fast;
    case "stall": // one 300 ms generation stall after the first 100 ms of audio
      return i < 5 ? fast : fast + 300;
    case "burst": // the finding: 120 ms of audio, a hesitation, then the rest at once
      return i < 6 ? 0 : 250;
    case "slowstart": // 1.3× real time for the first 800 ms, then 6×
      return i < 52 ? (i * 20) / 1.3 : 52 * (20 / 1.3) + ((i - 52) * 20) / 6;
    default:
      throw new Error(audio);
  }
}

/** the accumulated transcript the backend has once delta i went out */
function textAfter(i, text) {
  const last = (i + 1) * DELTA - 1;
  if (text === "all") return N;
  if (text === "word") {
    let n = 0;
    while (n < N && wordStart(n) <= last) n++;
    return n;
  }
  if (text === "sentence") {
    // a sentence's whole text arrives with its first audio delta (the tutorial traces)
    let s = 0;
    while (s < SENTENCES.length && wordStart(sentenceFirst[s]) <= last) s++;
    return s ? sentenceEnd[s - 1] : 0;
  }
  if (text === "late") {
    // a sentence's text arrives once all of its audio went out
    let s = 0;
    while (s < SENTENCES.length && (sentenceEnd[s] < N ? wordStart(sentenceEnd[s]) : TOTAL) - 1 <= last) s++;
    return s ? sentenceEnd[s - 1] : 0;
  }
  throw new Error(text);
}

let replyId = 0;
/**
 * Replay one reply; returns the worst lead (words shown − words heard), the worst lag (known words heard 150 ms ago
 * but not shown) and, with `cutAt` (ms), what the cut froze.
 */
function scenario({ audio, text, cutAt = null }) {
  const reply = ++replyId;
  /** @type {{at: number, seq: number, kind: string, words?: number}[]} */
  const events = [];
  let lastWords = 0;
  for (let i = 0; i < NDELTAS; i++) {
    const at = deliverAt(i, audio);
    const words = textAfter(i, text);
    if (words !== lastWords) events.push({ at, seq: 2 * i, kind: "caption", words }); // text first in the same ms
    lastWords = words;
    events.push({ at, seq: 2 * i + 1, kind: "audio" });
  }
  events.push({ at: deliverAt(NDELTAS - 1, audio), seq: 2 * NDELTAS, kind: "end" });
  events.sort((a, b) => a.at - b.at || a.seq - b.seq);

  const t0 = clock.now;
  sock.receive({ type: "reply_start", reply });
  let next = 0;
  let known = 0;
  let played = 0;
  let lead = 0;
  let lag = 0;
  /** @type {{heard: number, frozen: string, keyMs: number}|null} */
  let cut = null;
  const endAt = TOTAL / 24 + 800;
  while (clock.now - t0 < endAt && !cut) {
    const t = clock.now - t0;
    while (next < events.length && events[next].at <= t) {
      const e = events[next++];
      if (e.kind === "audio") sock.receive({ type: "reply_audio", reply, pcm: PCM });
      else if (e.kind === "caption") {
        known = /** @type {number} */ (e.words);
        sock.receive({ type: "caption", role: "assistant", text: ALL.slice(0, known).join(" "), final: false, reply });
      } else {
        known = N;
        sock.receive({ type: "caption", role: "assistant", text: ALL.join(" "), final: true, reply });
        sock.receive({ type: "reply_end", reply });
      }
    }
    if (cutAt !== null && t >= cutAt) {
      fireWindow("keydown", { type: "keydown", key: " ", code: "Space", repeat: false, target: doc.body, timeStamp: clock.now, preventDefault() {} });
      const key = sock.sent.filter((m) => m.type === "key").at(-1);
      cut = { heard: Math.max(0, played - LATENCY), frozen: liveText(), keyMs: key.reply_played_ms };
      break;
    }
    pumpToWorklet();
    globalThis.currentTime = clock.now / 1000;
    proc.process([], [[new Float32Array(128)]]);
    if (proc.reply === reply) played = proc.played;
    pumpToMain();
    const shown = countWords(liveText());
    const heard = Math.max(0, played - LATENCY);
    lead = Math.max(lead, shown - idealWords(heard));
    const heardLate = Math.max(0, played - LATENCY - 0.15 * 24000);
    lag = Math.max(lag, Math.min(known, heardLate > 0 ? idealWords(heardLate) : 0) - shown);
    advance(QMS);
  }
  // let a cut reply fade out, so the next scenario starts clean
  for (let i = 0; i < 8; i++) {
    pumpToWorklet();
    proc.process([], [[new Float32Array(128)]]);
    pumpToMain();
    advance(QMS);
  }
  return { lead, lag, cut, final: liveText() };
}

// ---- no cut: the caption never runs ahead of the voice, and doesn't fall far behind what is known
for (const [audio, text] of [
  ["steady", "word"],
  ["steady", "sentence"],
  ["steady", "all"],
  ["steady", "late"],
  ["stall", "word"],
  ["stall", "sentence"],
  ["burst", "all"],
  ["slowstart", "sentence"],
  ["slowstart", "late"],
]) {
  const r = scenario({ audio, text });
  if (VERBOSE) print(`${audio}/${text}: lead ${r.lead}, lag ${r.lag}`);
  check(`${audio} audio, ${text} text: never ahead of the voice (≤ the word being said)`, r.lead <= 1, `${r.lead} words ahead`);
  check(`${audio} audio, ${text} text: keeps up with what is known`, r.lag <= 2, `${r.lag} words behind`);
  check(`${audio} audio, ${text} text: the whole reply once it played`, countWords(r.final) === N, r.final);
}

// ---- a tap cuts the reply: the frozen caption holds the words heard (what conversation.item.truncate keeps)
for (const [audio, text, cutAt] of [
  ["burst", "all", 500], // the finding: 0.5 s in, the old caption froze 32 words, 2 were heard
  ["stall", "word", 900],
  ["steady", "sentence", 700],
  ["slowstart", "all", 1500],
]) {
  const r = scenario({ audio, text, cutAt });
  const c = /** @type {{heard: number, frozen: string, keyMs: number}} */ (r.cut);
  const ideal = idealWords(c.heard);
  if (VERBOSE) print(`cut ${audio}/${text} @${cutAt}: froze ${countWords(c.frozen)}, heard ${ideal}, key ${c.keyMs} ms vs ${Math.round(c.heard / 24)} ms`);
  check(`cut at ${cutAt} ms (${audio}, ${text}): ends in a dash`, / —$/.test(c.frozen), JSON.stringify(c.frozen));
  check(`cut at ${cutAt} ms (${audio}, ${text}): frozen on the words heard`, countWords(c.frozen) <= ideal + 1 && countWords(c.frozen) >= Math.min(1, ideal), `froze ${countWords(c.frozen)} words, ${ideal} heard: ${JSON.stringify(c.frozen)}`);
  check(`cut at ${cutAt} ms (${audio}, ${text}): reply_played_ms matches what was heard`, Math.abs(c.keyMs - c.heard / 24) <= 12, `${c.keyMs} ms vs ${Math.round(c.heard / 24)} ms`);
}

print(`live reveal: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} live-reveal check(s) failed`);
