// @ts-check
/**
 * Web Audio for the deck: one AudioContext at 24 kHz, made in the Join click (Arm); the narration
 * player (decoded wav chunks, the listener's exact position, chunk_ended on natural ends only); the live-reply player
 * (pcm-player worklet, played-sample count, drained), which also plays Relay's live narration; the microphone
 * (mic-capture worklet → 100 ms PCM16 frames + RMS); an analyser on both output buses for Relay's speaking ring.
 * Everything runs at 24 kHz, so a sample out is a sample in and positions need no resampling maths.
 */
import { RATE, base64ToInt16, int16ToFloat, isNewerReply, listenerPositionMs, playedEstimate, rms, samplesToMs } from "./util.js";

const WORKLETS = ["/web/worklets/pcm-player.js", "/web/worklets/mic-capture.js"];
export const STOP_FADE_S = 0.012; // narration fades out over 12 ms on stop: a hard cut clicks on the speakers
export const MIC_CONSTRAINTS = Object.freeze({
  audio: { echoCancellation: true, noiseSuppression: false, autoGainControl: true, channelCount: 1 },
});

/** @typedef {{play_id: number, slide: string, chunk: number, t_ms: number}} Position */

/** @param {unknown} err */
function message(err) {
  return err instanceof Error ? err.message : String(err);
}

export class AudioEngine {
  constructor() {
    /** @type {AudioContext|null} */
    this.ctx = null;
    /** @type {GainNode|null} */
    this.narrationBus = null;
    /** @type {GainNode|null} */
    this.replyBus = null;
    /** @type {AnalyserNode|null} taps both buses: what Relay sounds like right now (the meeting's speaking ring) */
    this.analyser = null;
    /** @type {Float32Array|null} */
    this.levelBuffer = null;
    this.workletsReady = false;
    /** @type {(state: string) => void} */
    this.onStateChange = () => {};
  }

  /**
   * Create (once) and resume the AudioContext. Call synchronously inside a user gesture (the Arm click), or Chrome
   * leaves it suspended without a word.
   * @returns {Promise<void>}
   */
  unlock() {
    if (!this.ctx) {
      const w = /** @type {any} */ (window);
      const Ctor = w.AudioContext || w.webkitAudioContext;
      if (!Ctor) throw new Error("Web Audio is not available in this browser");
      /** @type {AudioContext} */
      const ctx = new Ctor({ sampleRate: RATE, latencyHint: "interactive" });
      this.ctx = ctx;
      this.narrationBus = ctx.createGain();
      this.narrationBus.connect(ctx.destination);
      this.replyBus = ctx.createGain();
      this.replyBus.connect(ctx.destination);
      if (typeof ctx.createAnalyser === "function") {
        // A side tap (Chrome pulls an analyser without a path to the destination): no effect on what plays.
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 1024; // ~43 ms at 24 kHz: about one animation frame and a half
        this.narrationBus.connect(analyser);
        this.replyBus.connect(analyser);
        this.analyser = analyser;
        this.levelBuffer = new Float32Array(analyser.fftSize);
      }
      ctx.onstatechange = () => this.onStateChange(ctx.state);
    }
    return this.ctx.resume();
  }

  /** Relay's audible level now: RMS (0..1) of the last ~43 ms of narration and reply audio; 0 without an analyser. */
  outputLevel() {
    if (!this.analyser || !this.levelBuffer) return 0;
    this.analyser.getFloatTimeDomainData(/** @type {any} */ (this.levelBuffer));
    return rms(this.levelBuffer);
  }

  /** Resume a context that got suspended (from a key or pointer handler: resuming needs a user gesture). */
  ensureRunning() {
    const ctx = this.ctx;
    if (ctx && ctx.state !== "running" && ctx.state !== "closed") ctx.resume().catch(() => {});
  }

  get running() {
    return this.ctx !== null && this.ctx.state === "running";
  }

  /** Load the two worklet modules (needs a secure context: http://localhost is one, a LAN IP is not). */
  async loadWorklets() {
    const ctx = this.ctx;
    if (!ctx) throw new Error("no AudioContext");
    if (!ctx.audioWorklet) throw new Error("AudioWorklet unavailable: open the deck on http://localhost");
    await Promise.all(WORKLETS.map((url) => ctx.audioWorklet.addModule(url)));
    this.workletsReady = true;
  }

  /** Seconds between the render clock and what the listener hears (outputLatency + baseLatency). */
  latency() {
    const ctx = /** @type {any} */ (this.ctx);
    if (!ctx) return 0;
    const out = Number(ctx.outputLatency);
    const base = Number(ctx.baseLatency);
    return (Number.isFinite(out) ? out : 0) + (Number.isFinite(base) ? base : 0);
  }

  /** AudioContext time (s). */
  now() {
    return this.ctx ? this.ctx.currentTime : 0;
  }
}

/**
 * @typedef {object} PlayRequest
 * @property {number} playId
 * @property {string} slide
 * @property {number} chunk
 * @property {number} offsetMs
 * @property {number} durationMs  talk JSON duration (replaced by the decoded audio's own)
 * @property {string} key  "<slide>/<chunk>"
 * @property {string|null} url
 */

/**
 * @typedef {PlayRequest & {
 *   state: "loading"|"playing", clock: "audio"|"silent", startedAt: number, stopped: boolean,
 *   source: AudioBufferSourceNode|null, gain: GainNode|null, timer: ReturnType<typeof setTimeout>|undefined
 * }} Playback
 */

export class NarrationPlayer {
  /**
   * @param {AudioEngine} engine
   * @param {{onEnded: (playId: number) => void, onSilent?: (req: PlayRequest, why: string) => void}} handlers
   *   onEnded fires only when a chunk plays to its natural end, never after stop() or a newer play().
   */
  constructor(engine, handlers) {
    this.engine = engine;
    this.handlers = handlers;
    /** @type {Map<string, AudioBuffer>} */
    this.buffers = new Map();
    /** @type {Map<string, Promise<AudioBuffer|null>>} */
    this.loading = new Map();
    /** @type {Map<string, string>} */
    this.errors = new Map();
    /** @type {Playback|null} */
    this.current = null;
  }

  /** A chunk is playing (or about to: its audio is still loading). */
  get active() {
    return this.current !== null;
  }

  /**
   * Fetch + decode one chunk (cached). Resolves null on failure; the reason is kept in `errors`.
   * @param {string} key
   * @param {string} url
   * @returns {Promise<AudioBuffer|null>}
   */
  load(key, url) {
    const cached = this.buffers.get(key);
    if (cached) return Promise.resolve(cached);
    const pending = this.loading.get(key);
    if (pending) return pending;
    const ctx = this.engine.ctx;
    if (!ctx) return Promise.resolve(null);
    const job = (async () => {
      try {
        const res = await fetch(url, { cache: "no-cache" });
        if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
        const buffer = await ctx.decodeAudioData(await res.arrayBuffer());
        this.buffers.set(key, buffer);
        this.errors.delete(key);
        return buffer;
      } catch (err) {
        this.errors.set(key, message(err));
        return null;
      } finally {
        this.loading.delete(key);
      }
    })();
    this.loading.set(key, job);
    return job;
  }

  /**
   * Fetch + decode every chunk, a few at a time, reporting progress.
   * @param {{key: string, url: string}[]} items
   * @param {(done: number, total: number, failed: number) => void} onProgress
   * @returns {Promise<{total: number, failed: number}>}
   */
  async preload(items, onProgress, concurrency = 4) {
    let next = 0;
    let done = 0;
    let failed = 0;
    onProgress(0, items.length, 0);
    const worker = async () => {
      while (next < items.length) {
        const item = items[next++];
        const buffer = await this.load(item.key, item.url);
        done++;
        if (!buffer) failed++;
        onProgress(done, items.length, failed);
      }
    };
    await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, worker));
    return { total: items.length, failed };
  }

  /**
   * Play a chunk from `offsetMs`. Whatever was playing stops first and never reports chunk_ended. Audio that is not
   * loaded yet is loaded now; audio that cannot be loaded "plays" silently on a timer so captions and the deck
   * still move on (and the problem is reported through onSilent).
   * @param {PlayRequest} req
   */
  play(req) {
    this.stop();
    /** @type {Playback} */
    const cur = {
      ...req,
      state: "loading",
      clock: "audio",
      startedAt: 0,
      stopped: false,
      source: null,
      gain: null,
      timer: undefined,
    };
    this.current = cur;
    const buffer = this.buffers.get(req.key);
    if (buffer) {
      this.start(cur, buffer);
      return;
    }
    if (!this.engine.ctx || !req.url) {
      this.startSilent(cur, this.engine.ctx ? "no audio for this chunk" : "audio is not armed");
      return;
    }
    this.load(req.key, req.url).then((loaded) => {
      if (this.current !== cur || cur.stopped) return;
      if (loaded) this.start(cur, loaded);
      else this.startSilent(cur, this.errors.get(req.key) ?? "audio failed to load");
    });
  }

  /**
   * @param {Playback} cur
   * @param {AudioBuffer} buffer
   */
  start(cur, buffer) {
    const ctx = /** @type {AudioContext} */ (this.engine.ctx);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    const gain = ctx.createGain();
    source.connect(gain);
    gain.connect(/** @type {GainNode} */ (this.engine.narrationBus));
    source.onended = () => {
      try {
        source.disconnect();
        gain.disconnect();
      } catch {
        // already disconnected
      }
      this.finish(cur);
    };
    cur.durationMs = Math.round(buffer.duration * 1000);
    const when = ctx.currentTime;
    source.start(when, Math.min(Math.max(0, cur.offsetMs / 1000), buffer.duration));
    cur.source = source;
    cur.gain = gain;
    cur.startedAt = when;
    cur.clock = "audio";
    cur.state = "playing";
  }

  /**
   * @param {Playback} cur
   * @param {string} why
   */
  startSilent(cur, why) {
    cur.clock = "silent";
    cur.state = "playing";
    cur.startedAt = performance.now() / 1000;
    cur.timer = setTimeout(() => this.finish(cur), Math.max(0, cur.durationMs - cur.offsetMs));
    this.handlers.onSilent?.(cur, why);
  }

  /** onended / silent timer: a natural end, unless stop() or a newer play() came first. @param {Playback} cur */
  finish(cur) {
    if (this.current !== cur || cur.stopped) return;
    this.current = null;
    this.handlers.onEnded(cur.playId);
  }

  /** Where the listener is in the current chunk, or null when nothing plays. @returns {Position|null} */
  position() {
    const cur = this.current;
    if (!cur) return null;
    let t = cur.offsetMs;
    if (cur.state === "playing") {
      t =
        cur.clock === "audio"
          ? listenerPositionMs(this.engine.now(), cur.startedAt, cur.offsetMs, this.engine.latency(), cur.durationMs)
          : listenerPositionMs(performance.now() / 1000, cur.startedAt, cur.offsetMs, 0, cur.durationMs);
    }
    return { play_id: cur.playId, slide: cur.slide, chunk: cur.chunk, t_ms: Math.max(0, Math.round(t)) };
  }

  /** Stop now (12 ms fade). Never reports chunk_ended. Returns where the listener was, or null. */
  stop() {
    const cur = this.current;
    if (!cur) return null;
    const at = this.position();
    cur.stopped = true;
    this.current = null;
    clearTimeout(cur.timer);
    const ctx = this.engine.ctx;
    if (ctx && cur.source && cur.gain) {
      const t = ctx.currentTime;
      try {
        cur.gain.gain.cancelScheduledValues(t);
        cur.gain.gain.setValueAtTime(cur.gain.gain.value, t);
        cur.gain.gain.linearRampToValueAtTime(0, t + STOP_FADE_S);
        cur.source.stop(t + STOP_FADE_S);
      } catch {
        try {
          cur.source.stop();
        } catch {
          // never started or already stopped
        }
      }
    }
    return at;
  }
}

/** @typedef {"idle"|"playing"|"drained"|"flushed"} ReplyStatus */

export class ReplyPlayer {
  /**
   * @param {AudioEngine} engine
   * @param {{
   *   onStart?: (reply: number) => void,
   *   onProgress?: () => void,
   *   onDrained: (reply: number, playedMs: number) => void,
   * }} handlers  onDrained fires once per reply, after `end` and the last sample (never for a flushed reply)
   */
  constructor(engine, handlers) {
    this.engine = engine;
    this.handlers = handlers;
    /** @type {AudioWorkletNode|null} */
    this.node = null;
    /** @type {number|null} the reply being played (or the last one) */
    this.reply = null;
    /** @type {ReplyStatus} */
    this.status = "idle";
    this.ended = false;
    this.received = 0; // samples handed to the worklet
    this.played = 0; // samples output, as last reported by the worklet
    this.reportTime = 0; // AudioContext time of that report
  }

  /** Create the worklet node (after AudioEngine.loadWorklets). */
  attach() {
    const ctx = this.engine.ctx;
    if (!ctx || !this.engine.workletsReady || this.node) return this.node !== null;
    const node = new AudioWorkletNode(ctx, "pcm-player", {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    });
    node.connect(/** @type {GainNode} */ (this.engine.replyBus));
    node.port.onmessage = (event) => this.onWorklet(event.data);
    this.node = node;
    return true;
  }

  /** A reply is playing (started, not drained or flushed yet). */
  get active() {
    return this.status === "playing";
  }

  /** @param {any} msg */
  onWorklet(msg) {
    if (!msg || typeof msg !== "object" || msg.reply !== this.reply || this.status !== "playing") return;
    if (typeof msg.played === "number") {
      this.played = msg.played;
      this.reportTime = typeof msg.time === "number" ? msg.time : this.engine.now();
    }
    if (msg.type === "progress") {
      this.handlers.onProgress?.();
    } else if (msg.type === "drained") {
      this.status = "drained";
      this.handlers.onDrained(/** @type {number} */ (this.reply), samplesToMs(this.played));
    }
  }

  /**
   * `reply_start`: get ready for a new reply (a repeated start for the same reply is ignored).
   * @param {number} reply
   */
  start(reply) {
    if (reply === this.reply && this.status !== "idle") return;
    this.reply = reply;
    this.status = "playing";
    this.ended = false;
    this.received = 0;
    this.played = 0;
    this.reportTime = this.engine.now();
    this.node?.port.postMessage({ type: "start", reply });
    this.handlers.onStart?.(reply);
  }

  /**
   * Accept a message for `reply`? A newer reply that never had its reply_start (e.g. a reconnect lost it) starts
   * now; older replies and the audio of a flushed reply are dropped.
   * @param {number|null} reply
   */
  accept(reply) {
    if (reply === null) return false;
    if (reply !== this.reply) {
      if (!isNewerReply(reply, this.reply)) return false;
      this.start(reply);
    }
    return this.status === "playing";
  }

  /**
   * `reply_audio`: enqueue base64 PCM16.
   * @param {number|null} reply
   * @param {unknown} b64
   */
  push(reply, b64) {
    if (typeof b64 !== "string" || !b64 || !this.accept(reply)) return;
    const samples = int16ToFloat(base64ToInt16(b64));
    if (!samples.length) return;
    if (this.received === 0 || this.playedNow() >= this.received) {
      // Playback (re)starts with this audio: the reply's first audio, or audio after the player ran dry (a
      // generation or network stall). Extrapolate from now, not from the last report before the gap, or the gap
      // would count as heard audio in reply_played_ms (conversation.item.truncate) and in the live caption.
      this.played = this.received;
      this.reportTime = this.engine.now();
    }
    this.received += samples.length;
    this.node?.port.postMessage({ type: "push", reply, samples }, [samples.buffer]);
  }

  /** `reply_end`: no more audio; `drained` follows once the player is empty. @param {number|null} reply */
  end(reply) {
    if (!this.accept(reply) || this.ended) return;
    this.ended = true;
    if (this.node) {
      this.node.port.postMessage({ type: "end", reply });
      return;
    }
    // No worklet (audio failed at Arm): nothing can play, so the reply is drained at once.
    this.status = "drained";
    const done = /** @type {number} */ (reply);
    queueMicrotask(() => this.handlers.onDrained(done, 0));
  }

  /**
   * Drop the reply's audio now (local key action or `reply_flush`). Returns what had been heard, or null when no
   * reply was playing.
   * @param {number|null} [reply]  default: the current one
   * @returns {{reply: number, played_ms: number}|null}
   */
  flush(reply = null) {
    if (this.status !== "playing" || this.reply === null) return null;
    if (reply !== null && reply !== this.reply) return null;
    const snap = this.snapshot();
    this.status = "flushed";
    this.node?.port.postMessage({ type: "flush", reply: this.reply });
    return snap;
  }

  /**
   * Forget the current reply and its number (the deck socket closed: a restarted server numbers its replies from 1
   * again, and a reply_start whose id equals a finished reply's would otherwise be ignored as a repeat).
   */
  reset() {
    if (this.status === "playing") this.node?.port.postMessage({ type: "flush", reply: null });
    this.reply = null;
    this.status = "idle";
    this.ended = false;
    this.received = 0;
    this.played = 0;
    this.reportTime = this.engine.now();
  }

  /** Samples output so far, extrapolated between the worklet's ~100 ms reports. */
  playedNow() {
    return playedEstimate(this.played, this.reportTime, this.engine.now(), this.received);
  }

  /** Samples the listener has heard so far: output minus the output latency. */
  heardNow() {
    return Math.max(0, this.playedNow() - Math.round(this.engine.latency() * RATE));
  }

  /**
   * The playing reply and how much of it the listener has heard (for conversation.item.truncate), or null.
   * @returns {{reply: number, played_ms: number}|null}
   */
  snapshot() {
    if (this.status !== "playing" || this.reply === null) return null;
    return { reply: this.reply, played_ms: samplesToMs(this.heardNow()) };
  }
}

export class Microphone {
  /**
   * @param {AudioEngine} engine
   * @param {{onFrame: (pcm: Int16Array, rms: number) => void, onLost?: (why: string) => void}} handlers
   */
  constructor(engine, handlers) {
    this.engine = engine;
    this.handlers = handlers;
    /** @type {MediaStream|null} */
    this.stream = null;
    /** @type {AudioWorkletNode|null} */
    this.node = null;
    this.label = "";
  }

  /**
   * Ask for the microphone. Call it inside the Arm click so the permission prompt shows at once.
   * @returns {Promise<MediaStream>}
   */
  static request() {
    const md = typeof navigator !== "undefined" ? navigator.mediaDevices : undefined;
    if (!md || typeof md.getUserMedia !== "function") {
      return Promise.reject(new Error("no microphone API here: open the deck on http://localhost (a secure context)"));
    }
    try {
      return md.getUserMedia(MIC_CONSTRAINTS);
    } catch (err) {
      return Promise.reject(err);
    }
  }

  /**
   * mic → capture worklet → zero-gain node → destination (without a path to the destination Chrome never calls
   * the worklet's process()). Frames: 2400 samples = 100 ms of PCM16 + their RMS.
   * @param {MediaStream} stream
   */
  attach(stream) {
    const ctx = this.engine.ctx;
    /** @type {AudioWorkletNode} */
    let node;
    try {
      if (!ctx || !this.engine.workletsReady) throw new Error("audio engine not ready");
      const source = ctx.createMediaStreamSource(stream);
      node = new AudioWorkletNode(ctx, "mic-capture", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
        channelCountMode: "explicit",
        channelInterpretation: "speakers",
      });
      const sink = ctx.createGain();
      sink.gain.value = 0;
      source.connect(node);
      node.connect(sink);
      sink.connect(ctx.destination);
    } catch (err) {
      stream.getTracks().forEach((track) => track.stop()); // don't leave the mic (and its indicator) on
      throw err;
    }
    node.port.onmessage = (event) => {
      const msg = event.data;
      if (msg && msg.type === "frame" && msg.pcm instanceof Int16Array) this.handlers.onFrame(msg.pcm, Number(msg.rms) || 0);
    };
    const track = stream.getAudioTracks()[0];
    if (track) {
      this.label = track.label || "";
      track.addEventListener("ended", () => this.handlers.onLost?.("the microphone was disconnected"));
    }
    this.stream = stream;
    this.node = node;
  }
}
