// @ts-check
/**
 * AudioWorklet "pcm-player": plays one live reply at a time from a queue of Float32 frames (24 kHz mono, the
 * AudioContext rate, so one sample in = one sample out and `played` is exact for conversation.item.truncate).
 *
 * Main thread → worklet (port messages, all keyed by the reply id):
 *   {type: "start", reply}            reset; accept audio for this reply only
 *   {type: "push", reply, samples}    enqueue a Float32Array (Int16Array also accepted)
 *   {type: "end", reply}              no more audio for this reply: post `drained` once the queue is empty
 *   {type: "flush", reply|null}       drop everything (null = whatever is playing), after a 10 ms fade-out
 * Worklet → main thread:
 *   {type: "progress", reply, played, time}   played = samples output for this reply, about every 100 ms, and at
 *                                              once when output (re)starts after the queue ran dry (the reply's
 *                                              first audio, or audio after a generation stall)
 *   {type: "drained", reply, played, time}    once, after `end` with an empty queue
 *   {type: "flushed", reply, played, time}    once the flush (and its fade) is done
 * `time` is the AudioContext time (currentTime) of the report.
 */
/* global AudioWorkletProcessor, registerProcessor, currentTime */

const PROGRESS_EVERY = 2400; // samples (100 ms at 24 kHz)
const FADE = 240; // samples (10 ms): a hard cut mid-waveform clicks on the speakers

class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.reset(null);
    this.port.onmessage = (event) => this.onMessage(event.data);
  }

  /** @param {number|null} reply */
  reset(reply) {
    /** @type {Float32Array[]} */
    this.queue = [];
    this.head = 0; // read position inside queue[0]
    this.queued = 0; // samples waiting
    this.reply = reply; // the reply being played; null = idle, pushes ignored
    this.played = 0;
    this.ended = false;
    this.sinceProgress = 0;
    this.starved = true; // no audio to output: the next sample out (re)starts playback and is reported at once
    this.fadeLeft = 0; // > 0 while a flush fades out
  }

  /** @param {any} msg */
  onMessage(msg) {
    if (!msg || typeof msg !== "object") return;
    switch (msg.type) {
      case "start":
        this.reset(msg.reply ?? null);
        break;
      case "push": {
        if (this.reply === null || msg.reply !== this.reply || this.ended || this.fadeLeft > 0) return;
        let samples = msg.samples;
        if (samples instanceof Int16Array) samples = Float32Array.from(samples, (v) => v / 0x8000);
        if (!(samples instanceof Float32Array) || samples.length === 0) return;
        this.queue.push(samples);
        this.queued += samples.length;
        break;
      }
      case "end":
        if (this.reply !== null && msg.reply === this.reply) this.ended = true;
        break;
      case "flush":
        if (msg.reply === null || msg.reply === undefined || msg.reply === this.reply) {
          if (this.reply !== null && this.queued > 0) this.fadeLeft = FADE;
          else this.finishFlush();
        }
        break;
      default:
        break;
    }
  }

  finishFlush() {
    const reply = this.reply;
    const played = this.played;
    this.reset(null);
    this.port.postMessage({ type: "flushed", reply, played, time: currentTime });
  }

  /**
   * @param {Float32Array[][]} _inputs
   * @param {Float32Array[][]} outputs
   */
  process(_inputs, outputs) {
    const channels = outputs[0];
    const out = channels && channels[0];
    if (!out) return true;
    const fading = this.fadeLeft > 0;
    const limit = fading ? Math.min(out.length, this.fadeLeft) : out.length;
    let filled = 0;
    while (filled < limit && this.queue.length) {
      const frame = this.queue[0];
      const n = Math.min(limit - filled, frame.length - this.head);
      out.set(frame.subarray(this.head, this.head + n), filled);
      filled += n;
      this.head += n;
      this.queued -= n;
      if (this.head >= frame.length) {
        this.queue.shift();
        this.head = 0;
      }
    }
    if (fading) {
      for (let i = 0; i < filled; i++) out[i] *= (this.fadeLeft - i) / FADE;
    }
    out.fill(0, filled);
    for (let c = 1; c < channels.length; c++) channels[c].set(out);

    if (this.reply === null) return true;
    this.played += filled;
    if (fading) {
      this.fadeLeft -= filled;
      if (this.fadeLeft <= 0 || this.queued <= 0) this.finishFlush();
      return true;
    }
    this.sinceProgress += filled;
    const restarted = this.starved && filled > 0;
    this.starved = filled < out.length;
    if (restarted || this.sinceProgress >= PROGRESS_EVERY) {
      // A restart is reported at once: the main thread extrapolates `played` between reports, and must not count
      // the time the queue stood empty as output.
      this.sinceProgress = 0;
      this.port.postMessage({ type: "progress", reply: this.reply, played: this.played, time: currentTime });
    }
    if (this.ended && this.queued <= 0) {
      this.port.postMessage({ type: "drained", reply: this.reply, played: this.played, time: currentTime });
      this.reset(null);
    }
    return true; // keep the node alive between replies
  }
}

registerProcessor("pcm-player", PcmPlayer);
