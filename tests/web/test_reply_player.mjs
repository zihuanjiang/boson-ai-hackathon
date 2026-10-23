// The live-reply player (web/js/audio.js ReplyPlayer) against the REAL pcm-player worklet, one 128-sample render
// quantum at a time: reply_played_ms (what conversation.item.truncate keeps) must never count a generation stall as
// played audio, and must stay within about one render quantum of what the worklet actually output.
//   jsc -m tests/web/test_reply_player.mjs   (from the repo root; tests/web/run.sh runs everything)
load("tests/web/worklet_stub.js");
load("web/worklets/pcm-player.js");
import { ReplyPlayer } from "../../web/js/audio.js";
import { RATE, int16ToBase64, samplesToMs } from "../../web/js/util.js";

let passed = 0;
let failed = 0;
function check(name, ok, detail = "") {
  if (ok) passed++;
  else {
    failed++;
    print(`FAIL ${name}${detail ? `: ${detail}` : ""}`);
  }
}

const Q = 128; // render quantum
const QUANTUM_MS = (Q / RATE) * 1000; // 5.33 ms
const DELTA = 480; // one response.output_audio.delta ≈ 20 ms
const Processor = /** @type {any} */ (globalThis).registered["pcm-player"];
const b64Cache = new Map();
const deltaB64 = (n) => {
  if (!b64Cache.has(n)) b64Cache.set(n, int16ToBase64(new Int16Array(n).fill(1000)));
  return b64Cache.get(n);
};

/**
 * One player wired to one real worklet processor. Main → worklet messages arrive before the next quantum;
 * worklet → main messages arrive `lag` quanta late (the audio thread posts asynchronously).
 */
function rig(lag = 0) {
  globalThis.currentTime = 0;
  let quanta = 0;
  const worklet = new Processor();
  /** @type {{at: number, msg: any}[]} */
  const toMain = [];
  worklet.port.postMessage = (msg) => toMain.push({ at: quanta + lag, msg });
  const engine = { ctx: {}, workletsReady: true, now: () => globalThis.currentTime, latency: () => 0 };
  const player = new ReplyPlayer(engine, { onDrained: () => {} });
  /** @type {any[]} */
  const toWorklet = [];
  player.node = /** @type {any} */ ({ port: { postMessage: (msg) => toWorklet.push(msg) } });
  const step = () => {
    while (toWorklet.length) worklet.port.onmessage({ data: toWorklet.shift() });
    worklet.process([], [[new Float32Array(Q)]]);
    quanta++;
    globalThis.currentTime = (quanta * Q) / RATE;
    while (toMain.length && toMain[0].at <= quanta) player.onWorklet(toMain.shift().msg);
  };
  const runFor = (seconds) => {
    const end = globalThis.currentTime + seconds - 1e-9;
    while (globalThis.currentTime < end) step();
  };
  const push = (n) => {
    for (let left = n; left > 0; left -= DELTA) player.push(1, deltaB64(Math.min(DELTA, left)));
  };
  /** snapshot − what the worklet really output (ms; > 0 = over-estimate) */
  const error = () => {
    const snap = player.snapshot();
    return snap ? snap.played_ms - samplesToMs(worklet.played) : 0;
  };
  return { player, worklet, step, runFor, push, error };
}

/** first segment, then nothing for `stall` s, then a 5 s burst; the error at each quantum of the next `watch` s */
function stallCase({ first, stall, lag = 0, watch = 0.11 }) {
  const r = rig(lag);
  r.player.start(1);
  r.push(first);
  r.runFor(first / RATE + stall);
  r.push(120000);
  const errors = [r.error()]; // a key press right as the burst lands, before the worklet played any of it
  for (let t = 0; t < watch; t += Q / RATE) {
    r.step();
    errors.push(r.error());
  }
  return errors;
}

// 1. The finding's case: a stall right after a progress report, a key 50 ms after the audio resumes. As built the
//    snapshot said 2976 ms while 973 ms had been output (the 2 s stall counted as heard).
{
  const errors = stallCase({ first: 9 * 2432 + 128, stall: 2 });
  const at50 = errors[Math.round(0.05 / (Q / RATE))];
  check("stall right after a report: error 50 ms after the refill ≤ 1 quantum", Math.abs(at50) <= QUANTUM_MS + 1, `${at50} ms`);
  check("…and never an over-estimate past one quantum", Math.max(...errors) <= QUANTUM_MS + 1, `${Math.max(...errors)} ms`);
}

// 2. Every alignment of the stall inside the worklet's ~100 ms report cycle, with the worklet's messages reaching
//    the main thread 0 or 3 quanta late (so the main thread's own re-base, not only the worklet's report, is tested).
for (const lag of [0, 3]) {
  let worst = -Infinity;
  let under = Infinity;
  for (let k = 0; k < 19; k++) {
    for (const stall of [0.3, 2]) {
      const errors = stallCase({ first: 9 * 2432 + k * Q + 1, stall, lag });
      worst = Math.max(worst, ...errors);
      under = Math.min(under, ...errors);
    }
  }
  check(`stall sweep (messages ${lag} quanta late): never over by more than ~1 quantum`, worst <= QUANTUM_MS + 1, `worst ${worst} ms`);
  check(`stall sweep (messages ${lag} quanta late): never under by more than the lag + 1 quantum`, under >= -((lag + 1) * QUANTUM_MS + 1), `${under} ms`);
}

// 3. A lone first delta, a 300 ms gap, then audio at 6× real time, keys anywhere in the next 100 ms.
{
  const r = rig(2);
  r.player.start(1);
  r.push(DELTA);
  r.runFor(0.32);
  let worst = -Infinity;
  for (let i = 0; i < 20; i++) {
    r.push(6 * Q); // 6× real time
    worst = Math.max(worst, r.error());
    r.step();
    worst = Math.max(worst, r.error());
  }
  check("hiccup after the first delta: no over-estimate", worst <= QUANTUM_MS + 1, `${worst} ms`);
}

// 4. Control: no stall, audio 6× ahead; the estimate tracks the output throughout.
{
  const r = rig(0);
  r.player.start(1);
  r.push(120000);
  let worst = 0;
  for (let i = 0; i < 900; i++) {
    r.step();
    worst = Math.max(worst, Math.abs(r.error()));
  }
  check("no stall: within one quantum throughout", worst <= QUANTUM_MS + 1, `${worst} ms`);
}

print(`reply player: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} reply-player check(s) failed`);
