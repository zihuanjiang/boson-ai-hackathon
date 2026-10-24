// @ts-check
/**
 * The Demo Audio Controller (contract v3.1 §4, doc/online_meeting_patch.md §3): the simulated online meeting.
 * Alice, Bob and Carol are prerecorded participant clips (`talk.demo`, from talks/<id>/demo.yaml). A clip plays on
 * the speakers through its participant's own bus (with its own analyser: their tile's speaking ring, never the
 * facilitator's), and the same samples stream to the backend as 100 ms `mic` frames (`source: "clip"`, the speaker,
 * the clip, and where the facilitator's reply is), each frame sent when its audio starts playing on the AudioContext
 * clock, then 1.2 s of silence. The physical microphone stays off and the facilitator's own audio never goes into
 * that stream, so a clip that starts while the facilitator speaks is a genuine barge-in, detected by the backend.
 * The script (DemoController) plays each clip when its cue holds: the facilitator audibly presenting a talking point,
 * the discussion open with the facilitator quiet, an earlier clip or a reply of the facilitator's ended. Cues are
 * evaluated on the page's events (state, reply start / progress / drained, clip end); timers only wait out the gaps,
 * the quiet and the timeouts. Play next is the manual fallback, disclosed as "played by hand". Nothing of the
 * facilitator is prerecorded here: only the participants are.
 * No DOM. The pure parts (normalizeDemo, the cue words, cueStatus, parseWav, ClipStream, DemoController) are
 * unit-tested under JavaScriptCore; ClipPlayer is the Web Audio part.
 */
import { MIC_FRAME_SAMPLES, RATE, int16ToFloat, floatToInt16, rms } from "./util.js";

/** @typedef {import("./audio.js").AudioEngine} AudioEngine */

/**
 * What a step waits for. Every key given must hold; each step also waits for the previous one.
 * @typedef {object} DemoCue
 * @property {string|null} talking_point  the facilitator is audibly presenting this talking point…
 * @property {number|null} heard_ms  …and its reply has been heard this long
 * @property {string|null} phase  the agenda phase is this one…
 * @property {number|null} quiet_ms  …and the facilitator has been quiet this long (state ROOM or IDLE)
 * @property {string|null} after  this (earlier) step's clip ended…
 * @property {string|null} after_reply  a reply of this kind drained after the previous step…
 * @property {number|null} gap_ms  …this long ago
 * @property {number} timeout_ms  after this long the strip says the step is waiting (Play next plays it by hand)
 */
/**
 * @typedef {object} DemoStep
 * @property {string} id
 * @property {string|null} speaker  the participant whose clip this is (null for an action)
 * @property {string} say  the clip's words (its label; the TTS input of a generated clip)
 * @property {"phase"|"start"|null} action  the script presses the phase button / starts the presentation
 * @property {DemoCue} when
 * @property {string|null} audio  the clip's URL (/clips/<id>.wav); null: unavailable
 * @property {number} duration_ms
 * @property {boolean} recorded  a recording (talks/<id>/clips/) rather than a generated clip
 * @property {boolean} fake  a placeholder tone (relay.clips --fake), not a voice
 */
/**
 * `/api/talk.demo` / `hello.demo`: the demo script and the backend's input (`clips`: the participants are the
 * clips, the mic stays off; `mic`: the room mic as before, and the page ignores the script; `voice`: a live
 * participant stream, the Zoom bot's (contract v5), and the page ignores the script too).
 * @typedef {{steps: DemoStep[], input: "clips"|"mic"|"voice"}} DemoInfo
 */
/**
 * What the page knows when a cue is evaluated.
 * @typedef {object} DemoFacts
 * @property {string} state  the backend's state (IDLE, NARRATING, LISTENING, REPLYING, ROOM, INTERVENING, PAUSED)
 * @property {string|null} agendaPhase  presentation | discussion | wrapup
 * @property {{talking_point_id: string|null, status: string}|null} checkpoint
 * @property {string|null} replyKind  the kind of the facilitator reply playing now (null: none)
 * @property {number|null} heardMs  how much of that reply has been heard
 * @property {boolean} audible  the facilitator is audible now (a reply with audio, or narration)
 */
/**
 * @typedef {object} CueHistory
 * @property {Map<string, number>} ended  step id → when it finished (ms)
 * @property {{kind: string, at: number}[]} drained  facilitator replies that were heard and played to their end
 * @property {number} prevAt  when the previous step finished (or the run started)
 * @property {number|null} quietSince  since when the facilitator has been quiet (null: audible now)
 */

export const FRAME_MS = (MIC_FRAME_SAMPLES / RATE) * 1000; // 100 ms per `mic` frame
export const TAIL_MS = 1200; // silence streamed after a clip, so the backend hears where it ends
export const DEFAULT_TIMEOUT_MS = 90_000;
export const PUMP_MS = 20; // how often the frame pump looks at the audio clock
export const MIC_OFF = "off — participants are prerecorded";
export const ROUTING = "participant clips only (facilitator audio is not sent)";
/** reply_start kinds an `after_reply` cue can name */
export const REPLY_KINDS = new Set(["narration", "answer", "handover", "intervention", "decision", "summary"]);
/** the checkpoint statuses that count as "presenting that talking point" */
const PRESENTING = new Set(["presenting", "resuming"]);
/** the states in which the facilitator is not speaking for a `quiet_ms` cue */
const QUIET_STATES = new Set(["ROOM", "IDLE"]);
const EPS_MS = 0.5;
/** @type {Record<string, string>} */
const PHASE_WORDS = { presentation: "presentation", discussion: "discussion", wrapup: "wrap-up" };
/** @type {Record<string, string>} */
const REPLY_WORDS = {
  narration: "talking point",
  answer: "answer",
  handover: "hand-over",
  intervention: "question",
  decision: "confirmation",
  summary: "summary",
};

/** @param {unknown} v */
function str(v) {
  return typeof v === "string" ? v : v === null || v === undefined ? "" : String(v);
}

/** @param {unknown} v @returns {string|null} */
function strOrNull(v) {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/** @param {unknown} v @returns {number|null} */
function num(v) {
  if (v === null || v === undefined || v === "" || typeof v === "boolean") return null;
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

/** @param {unknown} raw @returns {DemoCue} */
export function normalizeCue(raw) {
  const w = raw && typeof raw === "object" && !Array.isArray(raw) ? /** @type {any} */ (raw) : {};
  return {
    talking_point: strOrNull(w.talking_point),
    heard_ms: num(w.heard_ms),
    phase: strOrNull(w.phase),
    quiet_ms: num(w.quiet_ms),
    after: strOrNull(w.after),
    after_reply: strOrNull(w.after_reply), // the backend validates the kind (REPLY_KINDS)
    gap_ms: num(w.gap_ms),
    timeout_ms: num(w.timeout_ms) ?? DEFAULT_TIMEOUT_MS,
  };
}

/** @param {any} s @returns {DemoStep} */
function normalizeStep(s) {
  const action = s.action === "phase" || s.action === "start" ? s.action : null;
  return {
    id: str(s.id).trim(),
    speaker: action ? null : strOrNull(s.speaker),
    say: action ? "" : str(s.say).trim(),
    action,
    when: normalizeCue(s.when),
    audio: action ? null : strOrNull(s.audio),
    duration_ms: num(s.duration_ms) ?? 0,
    recorded: Boolean(s.recorded),
    fake: Boolean(s.fake),
  };
}

/**
 * `/api/talk.demo` / `hello.demo`; null for a talk without demo.yaml.
 * @param {unknown} raw
 * @returns {DemoInfo|null}
 */
export function normalizeDemo(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const d = /** @type {any} */ (raw);
  const steps = (Array.isArray(d.steps) ? d.steps : [])
    .filter((/** @type {unknown} */ s) => s && typeof s === "object")
    .map(normalizeStep)
    .filter((/** @type {DemoStep} */ s) => s.id);
  return { steps, input: d.input === "mic" || d.input === "voice" ? d.input : "clips" };
}

/** The participants are the clips (and the physical mic stays off). @param {DemoInfo|null|undefined} demo */
export function clipsMode(demo) {
  return Boolean(demo) && /** @type {DemoInfo} */ (demo).input === "clips";
}

/** How a participant's clip is labelled (captions, the chat). @param {string} name @param {boolean} [manual] */
export function clipLabel(name, manual = false) {
  return `${name || "Participant"} (recorded${manual ? " · played by hand" : ""})`;
}

/** "1.5 s", "0.6 s", "2 s". @param {number} ms */
function secs(ms) {
  return `${Math.round(ms / 100) / 10} s`;
}

/**
 * What a step waits for, in words: "while the facilitator presents rollback-trigger", "0.6 s after Bob's clip".
 * @param {DemoStep} step
 * @param {DemoStep[]} [steps]  the script (for the speaker of an `after` step)
 */
export function cueText(step, steps = []) {
  const w = step.when;
  const gap = w.gap_ms ? `${secs(w.gap_ms)} ` : "";
  /** @type {string[]} */
  const parts = [];
  if (w.talking_point) parts.push(`while the facilitator presents ${w.talking_point}`);
  if (w.phase) {
    const phase = PHASE_WORDS[w.phase] ?? w.phase;
    parts.push(w.quiet_ms !== null ? `once the ${phase} is open and the facilitator has been quiet ${secs(w.quiet_ms)}` : `once the ${phase} is open`);
  }
  if (w.after) {
    const before = steps.find((s) => s.id === w.after);
    const what = before && before.speaker ? `${before.speaker}'s clip` : before && before.action ? "the script's step" : w.after;
    parts.push(`${gap}after ${what}`);
  }
  if (w.after_reply) parts.push(`${gap}after the facilitator's ${REPLY_WORDS[w.after_reply] ?? w.after_reply}`);
  return parts.length ? parts.join(" and ") : "right away";
}

/**
 * Who or what a step is: the participant, or the script pressing a button.
 * @param {DemoStep} step
 * @param {string} [phaseLabel]  what the phase button says now ("Wrap up")
 */
export function stepWho(step, phaseLabel = "") {
  if (step.action === "phase") return `“${phaseLabel || "the phase button"}” (pressed by the script)`;
  if (step.action === "start") return "the presentation (started by the script)";
  return step.speaker || step.id;
}

/**
 * @typedef {object} DemoView  what the strip shows (DemoController.view())
 * @property {boolean} running
 * @property {boolean} done
 * @property {DemoStep|null} next
 * @property {DemoStep|null} playing
 * @property {boolean} manual  the clip playing was played by hand
 * @property {boolean} waiting  the next step's cue timed out (or its clip is unavailable)
 * @property {boolean} available  the next step can play
 * @property {number} index
 * @property {number} total
 */

/**
 * The agenda strip's demo line and its tooltip.
 * @param {DemoView} view
 * @param {DemoStep[]} steps
 * @param {string} [phaseLabel]
 * @returns {{text: string, title: string}}
 */
export function demoLine(view, steps, phaseLabel = "") {
  if (view.playing) {
    const label = clipLabel(view.playing.speaker ?? "", view.manual);
    return { text: `Playing: ${label}`, title: `${label}: “${view.playing.say}”` };
  }
  if (view.done || !view.next) return { text: "Demo script done", title: `All ${view.total} steps of the demo script ran` };
  const step = view.next;
  if (step.action === "start") {
    return { text: "Next: Run demo starts the presentation (as R does)", title: `Step ${view.index + 1} of ${view.total}` };
  }
  const who = stepWho(step, phaseLabel);
  if (!view.available) {
    return { text: `Next: ${who} — clip unavailable · Play next skips it`, title: `The clip for ${step.id} could not be loaded` };
  }
  const cue = cueText(step, steps);
  const say = step.say ? `: “${step.say}”` : "";
  const counter = `Step ${view.index + 1} of ${view.total}`;
  if (view.waiting) {
    return { text: `Next: ${who} — waiting ${cue} · Play next plays it by hand`, title: `${counter}${say}. Its cue has not come yet: ${cue}.` };
  }
  return { text: `Next: ${who} — ${cue}`, title: `${counter}${say}` };
}

/**
 * Does the step's cue hold now? `wakeAt` (ms): when a timed part of it (a gap, the quiet) will hold, if it will;
 * the event-driven parts (the talking point heard, the phase, a reply drained, a clip ended) wait for the next event.
 * @param {DemoStep} step
 * @param {DemoFacts} facts
 * @param {CueHistory} hist
 * @param {number} now  ms
 * @returns {{ready: boolean, wakeAt: number|null}}
 */
export function cueStatus(step, facts, hist, now) {
  const w = step.when;
  /** @type {number[]} */
  const times = [];
  let blocked = false;
  if (w.talking_point) {
    const cp = facts.checkpoint;
    const presenting =
      cp !== null &&
      cp.talking_point_id === w.talking_point &&
      PRESENTING.has(cp.status) &&
      facts.replyKind === "narration" &&
      facts.heardMs !== null &&
      facts.heardMs >= (w.heard_ms ?? 0);
    if (!presenting) blocked = true;
  }
  if (w.phase) {
    if (facts.agendaPhase !== w.phase) blocked = true;
    if (w.quiet_ms !== null) {
      if (!QUIET_STATES.has(facts.state) || facts.audible || hist.quietSince === null) blocked = true;
      else times.push(hist.quietSince + w.quiet_ms);
    }
  }
  if (w.after) {
    const at = hist.ended.get(w.after);
    if (at === undefined) blocked = true;
    else times.push(at + (w.gap_ms ?? 0));
  }
  if (w.after_reply) {
    const drained = hist.drained.find((d) => d.kind === w.after_reply && d.at >= hist.prevAt);
    if (!drained) blocked = true;
    else times.push(drained.at + (w.gap_ms ?? 0));
  }
  const at = times.length ? Math.max(...times) : now;
  if (blocked) return { ready: false, wakeAt: at > now + EPS_MS ? at : null };
  return at <= now + EPS_MS ? { ready: true, wakeAt: null } : { ready: false, wakeAt: at };
}

// ---------------------------------------------------------------- WAV

/**
 * A PCM16 WAV (RIFF/WAVE, format 1, 16 bits): its rate, channels and samples (interleaved), or null for anything
 * else (the caller then lets the browser decode it).
 * @param {ArrayBuffer} buffer
 * @returns {{rate: number, channels: number, samples: Int16Array}|null}
 */
export function parseWav(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 44) return null;
  const view = new DataView(buffer);
  const tag = (/** @type {number} */ at) => String.fromCharCode(view.getUint8(at), view.getUint8(at + 1), view.getUint8(at + 2), view.getUint8(at + 3));
  if (tag(0) !== "RIFF" || tag(8) !== "WAVE") return null;
  let at = 12;
  /** @type {{format: number, channels: number, rate: number, bits: number}|null} */
  let fmt = null;
  while (at + 8 <= buffer.byteLength) {
    const id = tag(at);
    const size = view.getUint32(at + 4, true);
    const body = at + 8;
    if (id === "fmt " && size >= 16) {
      fmt = { format: view.getUint16(body, true), channels: view.getUint16(body + 2, true), rate: view.getUint32(body + 4, true), bits: view.getUint16(body + 14, true) };
    } else if (id === "data") {
      if (!fmt || fmt.format !== 1 || fmt.bits !== 16 || fmt.channels < 1) return null;
      // a streamed WAV may say 0 or 0xFFFFFFFF: the data then runs to the end of the file
      const end = size === 0 || size === 0xffffffff ? buffer.byteLength : Math.min(buffer.byteLength, body + size);
      const n = Math.floor((end - body) / 2);
      const samples = new Int16Array(n);
      for (let i = 0; i < n; i++) samples[i] = view.getInt16(body + 2 * i, true);
      return { rate: fmt.rate, channels: fmt.channels, samples };
    }
    at = body + size + (size % 2);
  }
  return null;
}

/**
 * A mono PCM16 WAV of `samples` (tests, and nothing else in the page).
 * @param {Int16Array} samples
 * @param {number} [rate]
 */
export function wavBytes(samples, rate = RATE) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const put = (/** @type {number} */ at, /** @type {string} */ s) => {
    for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i));
  };
  put(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  put(8, "WAVE");
  put(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  put(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) view.setInt16(44 + 2 * i, samples[i], true);
  return buffer;
}

// ---------------------------------------------------------------- the participant input stream

/** @typedef {{speaker: string, clip: string}} ClipMeta */

/**
 * @typedef {object} StreamIO
 * @property {() => number} now  the AudioContext clock (s): frames go out as their audio starts playing
 * @property {(pcm: Int16Array, meta: ClipMeta, silent: boolean) => void} send  one frame
 * @property {(meta: ClipMeta) => void} [onAudioEnd]  the clip's audio played to its end (the silence follows)
 * @property {() => void} [onIdle]  the last frame of the silence went out
 */

/**
 * The one participant input stream to the backend (Higgs takes one audio input; speaker identity rides along in the
 * frames, from the clip's metadata). A clip's samples go out in 100 ms frames, frame k when sample k × 2400 starts
 * playing (t0 + k × 0.1 s on the audio clock); then 1.2 s of zero frames, each once its 100 ms has passed (like a mic).
 * A new clip replaces what is left of the silence, so the stream never runs ahead of real time, and two streams
 * never overlap. `pump()` is called often (a 20 ms timer); it never sends a frame early.
 */
export class ClipStream {
  /** @param {StreamIO} io */
  constructor(io, { frameSamples = MIC_FRAME_SAMPLES, rate = RATE, tailMs = TAIL_MS } = {}) {
    this.io = io;
    this.frameSamples = frameSamples;
    this.rate = rate;
    this.tailFrames = Math.round(tailMs / ((frameSamples / rate) * 1000));
    /**
     * @type {{samples: Int16Array, meta: ClipMeta, t0: number, next: number, audioFrames: number, ended: boolean}|null}
     *   next: the next frame to send (audio frames first, then the silence)
     */
    this.cur = null;
    this.zeros = new Int16Array(frameSamples);
  }

  /** A clip or its silence is streaming. */
  get active() {
    return this.cur !== null;
  }

  /** The clip's audio is still playing (not its silence). */
  get playing() {
    return this.cur !== null && !this.cur.ended;
  }

  /**
   * Stream `samples` as playing from audio time `t0` (s).
   * @param {Int16Array} samples
   * @param {ClipMeta} meta
   * @param {number} t0
   */
  start(samples, meta, t0) {
    this.cur = { samples, meta, t0, next: 0, audioFrames: Math.ceil(samples.length / this.frameSamples), ended: false };
    this.pump();
  }

  /**
   * When frame k of the current clip is due (audio clock, s): a clip frame as it starts playing, a frame of the
   * silence after it once its 100 ms has passed.
   * @param {number} k
   */
  frameAt(k) {
    const cur = /** @type {NonNullable<ClipStream["cur"]>} */ (this.cur);
    const step = this.frameSamples / this.rate;
    if (k < cur.audioFrames) return cur.t0 + k * step;
    return cur.t0 + cur.samples.length / this.rate + (k - cur.audioFrames + 1) * step;
  }

  /** Send every frame whose audio has started playing; report the clip's end and the stream going idle. */
  pump() {
    const cur = this.cur;
    if (!cur) return;
    const now = this.io.now();
    const total = cur.audioFrames + this.tailFrames;
    while (this.cur === cur && cur.next < total && this.frameAt(cur.next) <= now + 1e-6) {
      const k = cur.next++;
      if (k < cur.audioFrames) {
        const from = k * this.frameSamples;
        this.io.send(cur.samples.subarray(from, Math.min(cur.samples.length, from + this.frameSamples)), cur.meta, false);
      } else {
        this.io.send(this.zeros, cur.meta, true);
      }
    }
    if (this.cur !== cur) return;
    if (!cur.ended && now + 1e-6 >= cur.t0 + cur.samples.length / this.rate) {
      cur.ended = true;
      this.io.onAudioEnd?.(cur.meta);
    }
    if (this.cur === cur && cur.ended && cur.next >= total) {
      this.cur = null;
      this.io.onIdle?.();
    }
  }

  /**
   * Stop the clip now. With `tail`, the 1.2 s of silence still follows from now (the backend hears the clip end);
   * without it the stream stops at once (a reset: the backend starts over anyway).
   * @param {boolean} tail
   */
  stop(tail) {
    const cur = this.cur;
    if (!cur) return;
    if (!tail) {
      this.cur = null;
      this.io.onIdle?.();
      return;
    }
    if (!cur.ended) {
      // no more of the clip: it ends with the last frame sent (playing now), and the silence follows that frame
      const keep = Math.min(cur.samples.length, cur.next * this.frameSamples);
      cur.samples = cur.samples.subarray(0, keep);
      cur.audioFrames = cur.next;
      cur.ended = true; // stopped, not played to its end: no onAudioEnd
    }
    this.pump();
  }
}

// ---------------------------------------------------------------- the clips on the speakers

/**
 * @typedef {object} Clip
 * @property {Int16Array} samples  24 kHz mono PCM16: what streams to the backend
 * @property {AudioBuffer} buffer  the same samples, for the speakers
 */
/** @typedef {{gain: GainNode, analyser: AnalyserNode|null, data: Float32Array|null}} Bus */

export class ClipPlayer {
  /** @param {AudioEngine} engine */
  constructor(engine) {
    this.engine = engine;
    /** @type {Map<string, Clip>} */
    this.clips = new Map();
    /** @type {Map<string, string>} step id → why its clip can't play */
    this.errors = new Map();
    /** @type {Map<string, Bus>} participant → their bus */
    this.buses = new Map();
    /** @type {{source: AudioBufferSourceNode, gain: GainNode, speaker: string}|null} */
    this.current = null;
  }

  /** @param {string} id */
  has(id) {
    return this.clips.has(id);
  }

  /** The participants with a bus (their tiles' rings). */
  speakers() {
    return [...this.buses.keys()];
  }

  /**
   * A participant's bus: a gain to the speakers and its own analyser, never the facilitator's (whose analyser taps
   * only the narration and reply buses).
   * @param {string} name
   * @returns {Bus|null}
   */
  bus(name) {
    const known = this.buses.get(name);
    if (known) return known;
    const ctx = this.engine.ctx;
    if (!ctx) return null;
    const gain = ctx.createGain();
    gain.connect(ctx.destination);
    /** @type {AnalyserNode|null} */
    let analyser = null;
    if (typeof ctx.createAnalyser === "function") {
      analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      gain.connect(analyser);
    }
    const bus = { gain, analyser, data: analyser ? new Float32Array(analyser.fftSize) : null };
    this.buses.set(name, bus);
    return bus;
  }

  /**
   * Fetch one step's clip; the same samples feed the speakers and the stream. A 24 kHz mono PCM16 WAV is used as is;
   * anything else is decoded (and resampled) by the AudioContext.
   * @param {DemoStep} step
   * @returns {Promise<boolean>}
   */
  async load(step) {
    if (this.clips.has(step.id)) return true;
    const ctx = this.engine.ctx;
    try {
      if (!ctx) throw new Error("audio is not on");
      if (!step.audio) throw new Error("no clip for this step");
      const res = await fetch(step.audio, { cache: "no-cache" });
      if (!res.ok) throw new Error(`${step.audio}: HTTP ${res.status}`);
      const bytes = await res.arrayBuffer();
      const wav = parseWav(bytes);
      /** @type {Int16Array} */
      let samples;
      if (wav && wav.rate === RATE && wav.channels === 1) {
        samples = wav.samples;
      } else {
        const decoded = await ctx.decodeAudioData(bytes.slice(0));
        if (typeof decoded.getChannelData !== "function") throw new Error(`${step.audio}: cannot decode`);
        samples = floatToInt16(decoded.getChannelData(0));
      }
      if (!samples.length) throw new Error(`${step.audio}: empty`);
      const buffer = ctx.createBuffer(1, samples.length, RATE);
      buffer.getChannelData(0).set(int16ToFloat(samples));
      this.clips.set(step.id, { samples, buffer });
      this.errors.delete(step.id);
      return true;
    } catch (err) {
      this.errors.set(step.id, err instanceof Error ? err.message : String(err));
      return false;
    }
  }

  /**
   * Every clip step's clip, a few at a time; each speaker gets their bus.
   * @param {DemoStep[]} steps
   * @param {(done: number, total: number, failed: number) => void} onProgress
   * @returns {Promise<{total: number, failed: number}>}
   */
  async preload(steps, onProgress) {
    const clipSteps = steps.filter((s) => !s.action);
    for (const s of clipSteps) if (s.speaker) this.bus(s.speaker);
    let done = 0;
    let failed = 0;
    onProgress(0, clipSteps.length, 0);
    await Promise.all(
      clipSteps.map(async (s) => {
        const ok = await this.load(s);
        done++;
        if (!ok) failed++;
        onProgress(done, clipSteps.length, failed);
      }),
    );
    return { total: clipSteps.length, failed };
  }

  /**
   * Play a step's clip on its speaker's bus, now. Returns when it starts (audio clock) and the samples to stream,
   * or null (not loaded, no audio).
   * @param {DemoStep} step
   * @returns {{t0: number, samples: Int16Array}|null}
   */
  play(step) {
    const clip = this.clips.get(step.id);
    const ctx = this.engine.ctx;
    const bus = this.bus(step.speaker ?? step.id);
    if (!clip || !ctx || !bus) return null;
    this.stop();
    const source = ctx.createBufferSource();
    source.buffer = clip.buffer;
    const gain = ctx.createGain();
    source.connect(gain);
    gain.connect(bus.gain);
    const t0 = ctx.currentTime;
    const current = { source, gain, speaker: step.speaker ?? "" };
    source.onended = () => {
      try {
        source.disconnect();
        gain.disconnect();
      } catch {
        // already disconnected
      }
      if (this.current === current) this.current = null;
    };
    source.start(t0);
    this.current = current;
    return { t0, samples: clip.samples };
  }

  /** Stop the clip playing (a 12 ms fade: a hard cut clicks). */
  stop() {
    const cur = this.current;
    this.current = null;
    const ctx = this.engine.ctx;
    if (!cur || !ctx) return;
    const t = ctx.currentTime;
    try {
      cur.gain.gain.cancelScheduledValues(t);
      cur.gain.gain.setValueAtTime(cur.gain.gain.value, t);
      cur.gain.gain.linearRampToValueAtTime(0, t + 0.012);
      cur.source.stop(t + 0.012);
    } catch {
      try {
        cur.source.stop();
      } catch {
        // never started or already stopped
      }
    }
  }

  /** A participant's audible level now: RMS of their bus's last ~43 ms (0 without an analyser). @param {string} name */
  level(name) {
    const bus = this.buses.get(name);
    if (!bus || !bus.analyser || !bus.data) return 0;
    bus.analyser.getFloatTimeDomainData(/** @type {any} */ (bus.data));
    return rms(bus.data);
  }
}

// ---------------------------------------------------------------- the script

/**
 * @typedef {object} DemoIO
 * @property {() => number} now  ms (performance.now)
 * @property {(step: DemoStep, manual: boolean) => boolean} play  start a step's clip (false: it could not start)
 * @property {(step: DemoStep, manual: boolean) => boolean} act  run an action step (press the phase button, start)
 * @property {(step: DemoStep) => boolean} available  a step can run (its clip loaded, or it is an action)
 * @property {() => void} onChange  the view changed (re-render the strip)
 * @property {(fn: () => void, ms: number) => any} [setTimer]
 * @property {(id: any) => void} [clearTimer]
 */

/**
 * The demo script: one step at a time, in order; each plays when its cue holds (cueStatus), or by hand (Play next).
 * The page feeds it what happens (observe, replyDrained, clipEnded); it never decides anything about the
 * facilitator, it only decides when a participant speaks.
 */
export class DemoController {
  /**
   * @param {DemoStep[]} steps
   * @param {DemoIO} io
   */
  constructor(steps, io) {
    this.steps = steps;
    this.io = io;
    this.setTimer = io.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = io.clearTimer ?? ((id) => clearTimeout(id));
    this.running = false;
    this.index = 0; // the next step
    /** @type {DemoStep|null} the step whose clip is playing */
    this.playing = null;
    /** @type {Map<string, number>} */
    this.ended = new Map();
    /** @type {{kind: string, at: number}[]} */
    this.drained = [];
    this.prevAt = io.now();
    this.waitSince = io.now();
    /** @type {number|null} */
    this.quietSince = io.now();
    /** @type {DemoFacts} */
    this.facts = { state: "IDLE", agendaPhase: null, checkpoint: null, replyKind: null, heardMs: null, audible: false };
    /** @type {Set<string>} steps played by hand */
    this.manual = new Set();
    /** @type {Set<string>} unavailable steps skipped by hand */
    this.skipped = new Set();
    this.waiting = false;
    this.firing = false; // a step is starting (see fire)
    /** @type {any} */
    this.timer = undefined;
  }

  get next() {
    return this.steps[this.index] ?? null;
  }

  get done() {
    return this.index >= this.steps.length;
  }

  /** Run the script (from where it stopped). False when there is nothing left to run. */
  run() {
    if (this.running || this.done) return false;
    const now = this.io.now();
    this.running = true;
    if (this.index === 0 && this.ended.size === 0) this.prevAt = now;
    this.waitSince = now;
    this.waiting = false;
    this.evaluate();
    return true;
  }

  /** Stop the script (the caller stops the clip); Run carries on from the step that was playing or next. */
  stop() {
    this.running = false;
    this.playing = null;
    this.waiting = false;
    this.clearTimer(this.timer);
    this.timer = undefined;
    this.io.onChange();
  }

  /** Back to the first step, stopped (Restart meeting). */
  reset() {
    this.stop();
    const now = this.io.now();
    this.index = 0;
    this.ended.clear();
    this.drained = [];
    this.manual.clear();
    this.skipped.clear();
    this.prevAt = now;
    this.waitSince = now;
    this.io.onChange();
  }

  /** What the page knows now (on every state, reply start / progress / drain, flush, …). @param {Partial<DemoFacts>} facts */
  observe(facts) {
    Object.assign(this.facts, facts);
    if (this.facts.audible) this.quietSince = null;
    else if (this.quietSince === null) this.quietSince = this.io.now();
    this.evaluate();
  }

  /**
   * A facilitator reply of `kind` played to its end. The page reports only replies the room heard (a silent tool call
   * is no cue: its spoken follow-up comes as the next reply).
   * @param {string} kind
   */
  replyDrained(kind) {
    this.drained.push({ kind, at: this.io.now() });
    if (this.drained.length > 64) this.drained.shift();
    this.evaluate();
  }

  /** A clip's audio played to its end. @param {string} id */
  clipEnded(id) {
    if (!this.playing || this.playing.id !== id) return;
    this.playing = null;
    this.complete(id);
  }

  /** @param {string} id */
  complete(id) {
    const now = this.io.now();
    this.ended.set(id, now);
    this.prevAt = now;
    this.index++;
    this.waitSince = now;
    this.waiting = false;
    if (this.done) this.running = false; // the script has run
    this.evaluate();
  }

  /**
   * Play next (the manual fallback): the next step now, whatever its cue; an unavailable clip is skipped. The script
   * keeps running (or not) as it was.
   */
  playNext() {
    const step = this.next;
    if (this.playing || !step) return false;
    if (!this.io.available(step)) {
      this.skipped.add(step.id);
      this.complete(step.id);
      return true;
    }
    return this.fire(step, true);
  }

  /**
   * Run a step. The page's handlers may report back while it starts (a key press cuts a reply → observe): those
   * reports don't re-evaluate until it has started, or the same step would run twice.
   * @param {DemoStep} step
   * @param {boolean} manual
   */
  fire(step, manual) {
    this.clearTimer(this.timer);
    this.timer = undefined;
    this.firing = true;
    let ok = false;
    try {
      ok = step.action ? this.io.act(step, manual) : this.io.play(step, manual);
    } finally {
      this.firing = false;
    }
    if (!ok) return false;
    if (manual) this.manual.add(step.id);
    if (step.action) {
      this.complete(step.id);
      return true;
    }
    this.playing = step;
    this.waiting = false;
    this.io.onChange();
    return true;
  }

  /** Check the next step's cue; sleep until its gap or timeout, or until the next event. */
  evaluate() {
    if (this.firing) return;
    this.clearTimer(this.timer);
    this.timer = undefined;
    const step = this.next;
    if (this.running && !this.playing && step) {
      const now = this.io.now();
      /** @type {number[]} */
      const wake = [];
      const available = this.io.available(step);
      if (available) {
        const hist = { ended: this.ended, drained: this.drained, prevAt: this.prevAt, quietSince: this.quietSince };
        const st = cueStatus(step, this.facts, hist, now);
        if (st.ready) {
          if (!this.fire(step, false)) this.stop();
          return;
        }
        if (st.wakeAt !== null) wake.push(st.wakeAt);
      }
      const timeoutAt = this.waitSince + step.when.timeout_ms;
      this.waiting = !available || now >= timeoutAt;
      if (!this.waiting) wake.push(timeoutAt);
      if (wake.length) this.timer = this.setTimer(() => this.evaluate(), Math.max(0, Math.min(...wake) - now));
    }
    this.io.onChange();
  }

  /** @returns {DemoView} */
  view() {
    const next = this.next;
    return {
      running: this.running,
      done: this.done,
      next,
      playing: this.playing,
      manual: this.playing !== null && this.manual.has(this.playing.id),
      waiting: this.waiting,
      available: next ? this.io.available(next) : true,
      index: this.index,
      total: this.steps.length,
    };
  }
}
