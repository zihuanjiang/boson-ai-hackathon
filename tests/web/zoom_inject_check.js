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
// A small fake DOM for the speaker strategies: a tree of El, the selectors inject.js uses (`*`, `[attr]`,
// `[attr*="v"]`, `[attr="v"]`, compounds of those, descendant combinators), contains(), and getComputedStyle with
// pseudo-elements (an El's `style` and `pseudo["::after"]` override the defaults).
function parseCompound(s) {
  if (s === "*") return [() => true];
  const preds = [];
  const re = /\[([\w-]+)(?:(\*?=)"([^"]*)")?\]/g;
  let m;
  let used = 0;
  while ((m = re.exec(s))) {
    const [, name, op, val] = m;
    used += m[0].length;
    preds.push((el) => {
      const v = el.getAttribute(name);
      if (v === null || v === undefined) return false;
      return !op ? true : op === "*=" ? v.includes(val) : v === val;
    });
  }
  if (used !== s.length) throw new Error(`the fake DOM can't match ${s}`);
  return preds;
}
function matchesSel(el, sel) {
  const parts = sel.trim().split(/\s+/).map(parseCompound);
  const ok = (e, preds) => preds.every((p) => p(e));
  if (!ok(el, parts[parts.length - 1])) return false;
  let i = parts.length - 2;
  for (let anc = el.parentElement; i >= 0 && anc; anc = anc.parentElement) if (ok(anc, parts[i])) i--;
  return i < 0;
}
class El {
  constructor(tag, cls = "", opts = {}, kids = []) {
    this.tagName = tag.toUpperCase();
    this.className = cls;
    this.attrs = opts.attrs || {};
    this.ownText = opts.text || "";
    this.style = opts.style || {};
    this.pseudo = opts.pseudo || {};
    this.children = [];
    this.parentElement = null;
    for (const k of kids) this.append(k);
  }
  append(k) {
    k.parentElement = this;
    this.children.push(k);
    return k;
  }
  get id() {
    return this.attrs.id || "";
  }
  get textContent() {
    return this.ownText + this.children.map((c) => c.textContent).join("");
  }
  getAttribute(name) {
    if (name === "class") return this.className || null;
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
  }
  descendants() {
    const out = [];
    const walk = (e) => {
      for (const c of e.children) {
        out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  querySelectorAll(sel) {
    return this.descendants().filter((e) => matchesSel(e, sel));
  }
  contains(other) {
    for (let e = other; e; e = e.parentElement) if (e === this) return true;
    return false;
  }
  getBoundingClientRect() {
    return { x: 0, y: 0, width: 160, height: 90 };
  }
}
const el = (tag, cls, opts, kids) => new El(tag, cls, opts, kids);
const STYLE_DEFAULTS = { outlineStyle: "none", outlineWidth: "0px", outlineColor: "rgb(0, 0, 0)", boxShadow: "none" };
for (const side of ["Top", "Left", "Bottom", "Right"]) {
  STYLE_DEFAULTS[`border${side}Style`] = "none";
  STYLE_DEFAULTS[`border${side}Width`] = "0px";
  STYLE_DEFAULTS[`border${side}Color`] = "rgb(0, 0, 0)";
}
globalThis.getComputedStyle = (node, pseudo) => ({ ...STYLE_DEFAULTS, ...(pseudo ? node.pseudo[pseudo] || {} : node.style) });
let domRoot = new El("body");
globalThis.document = {
  title: "Zoom Meeting",
  createElement: () => ({
    width: 0,
    height: 0,
    getContext: () => ({ fillRect() {}, beginPath() {}, arc() {}, fill() {}, fillText() {} }),
    captureStream: () => new MediaStream([new MediaStreamTrack("video")]),
  }),
  querySelectorAll: (sel) => domRoot.querySelectorAll(sel),
};
function page(...kids) {
  domRoot = new El("body", "", {}, kids);
  clock += 1.5; // past the roster's and the speaker's caches
}
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
const GREEN = "rgb(35, 217, 89)";
const tile = (name, opts = {}, cls = "gallery-video-container__video-frame") =>
  el("div", cls, opts, [el("div", "video-avatar__avatar-footer", {}, [el("span", "video-avatar__avatar-name", { text: name })])]);
const gallery = (...tiles) => el("div", "gallery-video-container__wrap", {}, tiles);

// the class marker (speaker view)
page(el("div", "speaker-active-container__video-frame", {}, [el("div", "video-avatar__avatar-name", { text: "  Facilitator (AI)  " })]));
check(bot.speaker(true).name === null, "the bot's own tile is never the speaker");
page(el("div", "speaker-active-container__video-frame", {}, [el("div", "video-avatar__avatar-name", { text: "Ken Jiang (Host)\n" })]));
let who = bot.speaker(true);
check(who.name === "Ken Jiang" && who.how === "class" && who.detail === '[class*="speaker-active"] [class*="name"]',
  `class marker: the active speaker is ${who.name} (${who.how}: ${who.detail})`);
d = bot.drain();
check(d.speaker === "Ken Jiang" && d.speaker_how === "class" && d.roster.join() === "Ken Jiang", "drain carries the speaker, how and the roster");
check(d.events.some((e) => e.includes("active speaker read via class")) && d.events.some((e) => e.includes("speaker: Ken Jiang (via class)")),
  "the strategy that worked and the speaker change are logged");
page();
check(bot.speaker(true).name === null, "no indicator → no speaker");
page(el("div", "speaker-active-container__video-frame", {}, [el("div", "video-avatar__avatar-name", { text: "Facilitator (AI) (Host)" })]));
check(bot.speaker(true).name === null, "the bot as host is still never the speaker");

// the colour classifier on its own
check(bot.isActiveColor(GREEN) && bot.isActiveColor("#23d959") && bot.isActiveColor("rgb(173, 255, 47)"), "Zoom's green (and yellow-green) is the active frame");
check(!bot.isActiveColor("rgb(14, 114, 237)") && !bot.isActiveColor("rgb(255, 255, 255)") && !bot.isActiveColor("rgb(255, 165, 0)") &&
  !bot.isActiveColor("rgba(35, 217, 89, 0.1)") && !bot.isActiveColor("none"), "blue, white, orange and a faint green are not");
check(bot.isActiveColor(`rgba(0, 0, 0, 0) 0px 0px 0px 0px, ${GREEN} 0px 0px 0px 2px inset`), "…found inside a box-shadow list");

// the green frame (gallery view, no active class anywhere): on the tile, on an overlay inside it, on its ::after
const solid = { borderTopStyle: "solid", borderTopWidth: "2px", borderTopColor: GREEN };
page(gallery(tile("Ken Jiang"), tile("Dana Lee", { style: solid }), tile("Facilitator (AI)")));
who = bot.speaker(true);
check(who.name === "Dana Lee" && who.how === "frame", `green border: ${who.name} (${who.how}: ${who.detail})`);
page(gallery(tile("Ken Jiang"),
  el("div", "gallery-video-container__video-frame", {}, [
    el("div", "video-avatar__overlay", { style: { boxShadow: `${GREEN} 0px 0px 0px 3px inset` } }),
    el("div", "video-avatar__avatar-footer", {}, [el("span", "video-avatar__avatar-name", { text: "Dana Lee" })])])));
check(bot.speaker(true).name === "Dana Lee", "green box-shadow on an overlay inside the tile");
page(gallery(tile("Ken Jiang", { pseudo: { "::after": { outlineStyle: "solid", outlineWidth: "3px", outlineColor: GREEN } } }), tile("Dana Lee")));
check(bot.speaker(true).name === "Ken Jiang", "green outline on the tile's ::after");
page(gallery(tile("Ken Jiang"), tile("Dana Lee"), tile("Facilitator (AI)", { style: solid })));
check(bot.speaker(true).name === null, "the bot's own green frame is nobody's (and two others: no fallback)");

// an aria-label (free text needs a roster name when there is a roster)
page(gallery(tile("Ken Jiang"), tile("Dana Lee")), el("div", "toast", { attrs: { "aria-label": "You appear to be talking while muted" } }));
check(bot.speaker(true).name === null, "an aria-label sentence that names nobody on the roster is no speaker");
page(gallery(tile("Ken Jiang"), tile("Dana Lee")), el("div", "sr-only", { attrs: { "aria-label": "Dana Lee is talking" } }));
who = bot.speaker(true);
check(who.name === "Dana Lee" && who.how === "aria", `aria-label: ${who.name} (${who.how})`);

// Zoom's captions: the latest line names its speaker; a line that stopped changing goes stale
const captions = (name, text) => el("div", "live-transcription-subtitle__box", {}, [
  el("div", "live-transcription-subtitle__item", {}, [el("span", "live-transcription-subtitle__display-name", { text: name }),
    el("span", "live-transcription-subtitle__text", { text })])]);
page(gallery(tile("Ken Jiang"), tile("Dana Lee")), captions("Dana Lee", "we ship on Friday"));
who = bot.speaker(true);
check(who.name === "Dana Lee" && who.how === "captions", `captions: ${who.name} (${who.how}: ${who.detail})`);
clock += 1;
check(bot.speaker(true).name === "Dana Lee", "…while the line is fresh");
clock += 3;
check(bot.speaker(true).name === null, "…and nobody once it hasn't changed for 3 s");
domRoot.children[1].children[0].children[1].ownText = "we ship on Friday, to beta users";
check(bot.speaker(true).name === "Dana Lee", "…until it changes again");
page(gallery(tile("Ken Jiang"), tile("Dana Lee")), captions("Ken Jiang", "agreed"));
check(bot.speaker(true).name === "Ken Jiang", "the next speaker's line");

// the fallback: exactly one other person on the roster; the participants panel's aria-labels join the roster
page(gallery(tile("Ken Jiang"), tile("Facilitator (AI)")));
who = bot.speaker(true);
check(who.name === "Ken Jiang" && who.how === "only", `only one other person: ${who.name} (${who.how})`);
page(gallery(tile("Ken Jiang")), el("div", "participants-list", {}, [
  el("div", "participants-item__item-layout", { attrs: { "aria-label": "Erin Wu (Host, me), Computer audio unmuted, video off" } })]));
check(JSON.stringify(bot.roster()) === JSON.stringify(["Ken Jiang", "Erin Wu"]) && bot.speaker(true).name === null,
  "the participants panel's rows join the roster (two others: no fallback)");

// the cache: a read within 300 ms is the cached one
page(gallery(tile("Ken Jiang"), tile("Dana Lee", { style: solid })));
check(bot.speaker(true).name === "Dana Lee", "frame again");
domRoot = new El("body", "", {}, [gallery(tile("Ken Jiang", { style: solid }), tile("Dana Lee"))]);
clock += 0.1;
check(bot.speaker().name === "Dana Lee", "within 300 ms the cached speaker");
clock += 0.3;
check(bot.speaker().name === "Ken Jiang", "after 300 ms a fresh read");
check(bot.speakerChanges >= 6, `speaker changes counted (${bot.speakerChanges})`);

// the probe: what the page offered, as plain JSON
page(gallery(tile("Ken Jiang"), tile("Dana Lee", { style: solid }), tile("Facilitator (AI)")),
  el("div", "toast", { attrs: { "aria-label": "Mute my microphone" } }), captions("Dana Lee", "hello"));
const probe = JSON.parse(JSON.stringify(bot.probe()));
check(probe.roster.join() === "Ken Jiang,Dana Lee" && probe.speaker.name === "Dana Lee" && Array.isArray(probe.tried) &&
  probe.tried.length >= 1, `probe: roster ${probe.roster}, speaker ${probe.speaker.name}, tried ${probe.tried}`);
check(probe.tiles.length >= 6 && probe.tiles.some((t) => t.green && t.text === "Dana Lee") &&
  probe.tiles.every((t) => typeof t.cls === "string" && typeof t.border === "string"), `probe: ${probe.tiles.length} tile-like elements with their computed frame`);
check(probe.greens.length >= 1 && probe.greens[0].text === "Dana Lee", "probe: every green-framed element");
check(probe.aria.some((a) => a.aria === "Mute my microphone") && probe.captions.some((c) => c.text.includes("hello")),
  "probe: the aria-labels and the caption candidates");
page();
const empty = bot.probe();
check(empty.roster.length === 0 && empty.speaker.name === null && empty.tried.some((t) => t.startsWith("only:")), "probe of an empty page");

if (failures) print(`${failures} check(s) failed`);
else print("all checks passed");
})().catch((err) => print(`FAIL harness error: ${err}`));
