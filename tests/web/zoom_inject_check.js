// Runs web/zoom/inject.js under JavaScriptCore's jsc with stubs for the browser (tests/test_zoom_bot.py):
//   jsc tests/web/zoom_inject_check.js -- web/zoom/inject.js
// Checks the bot's player: push queues back to back on the context clock, position counts audible ms, a chunk for a
// new key replaces the old one, flush drops the queue; getUserMedia hands out the fed stream; drain returns the tap's
// float32 audio and the player's position; the active speaker is read from the DOM and never the bot's own name.
// Prints "ok" lines and exits non-zero on the first failure.
"use strict";
const injectPath = arguments[0] || "web/zoom/inject.js";
let failures = 0;
function check(cond, what) {
  if (cond) print(`ok   ${what}`);
  else {
    failures++;
    print(`FAIL ${what}`);
  }
}

// ---- browser stubs ---------------------------------------------------------------------------------------------
if (typeof atob !== "function") {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  globalThis.btoa = (bin) => {
    let out = "";
    for (let i = 0; i < bin.length; i += 3) {
      const a = bin.charCodeAt(i), b = bin.charCodeAt(i + 1), c = bin.charCodeAt(i + 2);
      const n = (a << 16) | ((b || 0) << 8) | (c || 0);
      out += chars[(n >> 18) & 63] + chars[(n >> 12) & 63] + (isNaN(b) ? "=" : chars[(n >> 6) & 63]) +
        (isNaN(c) ? "=" : chars[n & 63]);
    }
    return out;
  };
  globalThis.atob = (b64) => {
    let out = "";
    const clean = b64.replace(/=+$/, "");
    let bits = 0, acc = 0;
    for (const ch of clean) {
      acc = (acc << 6) | chars.indexOf(ch);
      bits += 6;
      if (bits >= 8) {
        bits -= 8;
        out += String.fromCharCode((acc >> bits) & 255);
      }
    }
    return out;
  };
}
let clock = 0; // the audio context clock (seconds), advanced by the test
class AudioNode {
  constructor(ctx) {
    this.context = ctx;
  }
  connect() {}
}
class AudioDestinationNode extends AudioNode {}
class GainNode extends AudioNode {
  constructor(ctx) {
    super(ctx);
    this.gain = { value: 1 };
  }
}
class BufferSource extends AudioNode {
  constructor(ctx) {
    super(ctx);
    this.started = null;
    this.stopped = false;
  }
  start(at) {
    this.started = at;
  }
  stop() {
    this.stopped = true;
  }
}
class ScriptProcessor extends AudioNode {}
class MediaStreamTrack {
  constructor(kind) {
    this.kind = kind;
    this.id = `${kind}-track`;
    this.label = "";
  }
  clone() {
    return new MediaStreamTrack(this.kind);
  }
}
class MediaStream {
  constructor(tracks = []) {
    this.tracks = tracks;
  }
  getAudioTracks() {
    return this.tracks.filter((t) => t.kind === "audio");
  }
  getVideoTracks() {
    return this.tracks.filter((t) => t.kind === "video");
  }
}
const sources = [];
class AudioContext {
  constructor(opts) {
    this.sampleRate = (opts && opts.sampleRate) || 48000;
    this.destination = new AudioDestinationNode(this);
    this.state = "running";
  }
  get currentTime() {
    return clock;
  }
  createMediaStreamDestination() {
    const node = new AudioNode(this);
    node.stream = new MediaStream([new MediaStreamTrack("audio")]);
    return node;
  }
  createGain() {
    return new GainNode(this);
  }
  createBuffer(channels, length, rate) {
    const data = new Float32Array(length);
    return { length, sampleRate: rate, duration: length / rate, getChannelData: () => data };
  }
  createBufferSource() {
    const src = new BufferSource(this);
    sources.push(src);
    return src;
  }
  createScriptProcessor() {
    return new ScriptProcessor(this);
  }
  createMediaStreamSource() {
    return new AudioNode(this);
  }
  resume() {
    return Promise.resolve();
  }
}
globalThis.AudioNode = AudioNode;
globalThis.AudioDestinationNode = AudioDestinationNode;
globalThis.AudioContext = AudioContext;
globalThis.MediaStream = MediaStream;
globalThis.performance = { now: () => clock * 1000 };
globalThis.setInterval = () => 0;
globalThis.HTMLMediaElement = function () {};
Object.defineProperty(HTMLMediaElement.prototype, "srcObject", {
  configurable: true,
  set(v) {
    this._src = v;
  },
  get() {
    return this._src;
  },
});
const domNodes = [];
globalThis.document = {
  createElement: () => ({
    width: 0,
    height: 0,
    getContext: () => ({ fillRect() {}, beginPath() {}, arc() {}, fill() {}, fillText() {} }),
    captureStream: () => new MediaStream([new MediaStreamTrack("video")]),
  }),
  querySelectorAll: (sel) => domNodes.filter((n) => n.matches(sel)),
};
globalThis.navigator = { mediaDevices: {} };
globalThis.window = globalThis;
globalThis.console = { log() {} };

// ---- load the bot ----------------------------------------------------------------------------------------------
load(injectPath);
const bot = window.__bot;
check(bot && typeof bot.push === "function" && typeof bot.drain === "function", "window.__bot is installed");

// ---- the mouth -------------------------------------------------------------------------------------------------
function pcmB64(ms, value = 0.25) {
  const n = (24000 * ms) / 1000;
  let bin = "";
  const v = Math.round(value * 32767);
  for (let i = 0; i < n; i++) bin += String.fromCharCode(v & 255, (v >> 8) & 255);
  return btoa(bin);
}
check(JSON.stringify(bot.position()) === JSON.stringify({ key: null, played_ms: null, total_ms: 0, drained: true, playing: false }),
  "position() without a player");
clock = 10;
let p = bot.push(pcmB64(500), 1);
check(p.key === 1 && p.total_ms === 500 && p.played_ms === 0 && !p.drained, "push queues 500 ms for reply 1");
check(sources.length === 1 && Math.abs(sources[0].started - 10.03) < 1e-9, "the first chunk starts 30 ms from now");
p = bot.push(pcmB64(300), 1);
check(p.total_ms === 800 && Math.abs(sources[1].started - 10.53) < 1e-9, "the next chunk is scheduled back to back");
clock = 10.43; // 400 ms into the first chunk
p = bot.position();
check(p.played_ms === 400 && p.playing && !p.drained, "position counts 400 ms heard");
clock = 10.9; // past the end of the second chunk (10.83)
p = bot.position();
check(p.played_ms === 800 && p.drained && !p.playing, "the player drains when the clock passes the queue's end");
// a gap: the queue ran dry, the next chunk starts now (not at the old end) and the gap is not counted as heard
p = bot.push(pcmB64(200), 1);
check(Math.abs(sources[2].started - 10.9) < 1e-9 && p.total_ms === 1000 && p.played_ms === 800, "after a gap the chunk starts now");
clock = 11.0;
check(bot.position().played_ms === 900, "the gap itself is not counted");
// a newer reply replaces the player: its sources stop, the new one starts fresh
p = bot.push(pcmB64(1000), 2);
check(p.key === 2 && p.total_ms === 1000 && p.played_ms === 0, "a chunk for reply 2 replaces reply 1");
check(sources[2].stopped, "reply 1's still-playing source was stopped");
clock = 11.4;
const flushed = bot.flush(2);
check(flushed.flushed && flushed.key === 2 && flushed.played_ms === 370, "flush(2) reports what was heard (370 ms)");
check(bot.position().key === null && bot.position().drained, "nothing plays after the flush");
const nothing = bot.flush(2);
check(nothing.flushed === false, "flushing a key that does not play is a no-op");
const playKey = bot.push(pcmB64(100), "play:7");
check(playKey.key === "play:7", "a backup narration chunk is keyed by its play id");
check(bot.flush(99).flushed === false && bot.flush(null).flushed === true, "flush(null) drops whatever plays");

// ---- the devices (promises: jsc runs the job queue once the script yields, so the rest is async) ---------------
(async () => {
const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
check(stream && stream.getAudioTracks().length === 1 && stream.getVideoTracks().length === 1,
  "getUserMedia hands out the fed microphone and the canvas camera");
const devices = await navigator.mediaDevices.enumerateDevices();
check(devices && devices.length === 3 && devices.every((d) => d.label.startsWith("Facilitator (AI)")),
  "enumerateDevices lists only the bot's devices");

// ---- the ears --------------------------------------------------------------------------------------------------
const zoomCtx = new AudioContext({ sampleRate: 48000 });
const zoomNode = new GainNode(zoomCtx);
let processor = null;
zoomCtx.createScriptProcessor = () => (processor = new ScriptProcessor(zoomCtx));
zoomNode.connect(zoomCtx.destination);
check(processor !== null && typeof processor.onaudioprocess === "function", "a node → destination gets a tap");
const samples = new Float32Array(4096);
for (let i = 0; i < samples.length; i++) samples[i] = 0.5 * Math.sin((2 * Math.PI * 1000 * i) / 48000);
processor.onaudioprocess({ inputBuffer: { getChannelData: () => samples }, outputBuffer: { getChannelData: () => new Float32Array(4096) } });
let d = bot.drain();
check(d.rate === 48000 && d.tap === "webaudio-1" && d.peak_db === -9, `drain reports the 48 kHz tap at ${d.peak_db} dBFS`);
const bytes = atob(d.pcm);
check(bytes.length === 4096 * 4, "drain returns the tapped float32 audio since the last drain");
const view = new DataView(new ArrayBuffer(8));
for (let i = 0; i < 8; i++) view.setUint8(i, bytes.charCodeAt(i));
check(Math.abs(view.getFloat32(4, true) - samples[1]) < 1e-6, "…as little-endian float32");
check(bot.drain().pcm === "", "a second drain has nothing new");
check(d.position && d.position.key === null, "drain carries the player's position");
check(Array.isArray(d.events) && d.events.some((e) => e.includes("injected")), "drain carries the page's log lines");

// ---- the active speaker ----------------------------------------------------------------------------------------
domNodes.push({ matches: (sel) => sel === '[class*="speaker-active"] [class*="name"]', textContent: "  Facilitator (AI)  " });
check(bot.speaker().name === null, "the bot's own tile is never the speaker");
domNodes.push({ matches: (sel) => sel === '[class*="speaker-active"] [class*="name"]', textContent: "Ken Jiang (Host)\n" });
const who = bot.speaker();
check(who.name === "Ken Jiang" && who.how === '[class*="speaker-active"] [class*="name"]', `the active speaker is ${who.name}`);
check(bot.drain().speaker === "Ken Jiang", "drain carries the active speaker");
domNodes.length = 0;
check(bot.speaker().name === null, "no indicator → no speaker");
domNodes.push({ matches: (sel) => sel === '[class*="speaker-active"] [class*="name"]', textContent: "Facilitator (AI) (Host)" });
check(bot.speaker().name === null, "the bot as host is still never the speaker");

if (failures) print(`${failures} check(s) failed`);
else print("all checks passed");
})().catch((err) => print(`FAIL harness error: ${err}`));
