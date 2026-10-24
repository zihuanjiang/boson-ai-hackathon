// @ts-check
/**
 * The observer page's stand-in for the audio (contract v5 §3). The page the Zoom bot shares as its screen
 * (`?view=observer`) has no AudioContext: the bot is the deck, it plays the facilitator's replies into Zoom and the
 * backup narration too. The observer only renders, so it needs the *positions* those players would report, on the
 * wall clock:
 * - the backup narration (`play`): the NarrationPlayer's silent path already runs a chunk on a performance.now()
 *   timer from `offset_ms` to its `duration_ms` when there is no AudioContext (main.js uses it as is);
 * - a live reply: this player. Reply audio reaches the observer as it reaches the bot, and the bot plays it in real
 *   time from the first chunk on (restarting from now when it ran dry), so the heard position is "samples received,
 *   paced at 24 kHz from the first chunk's arrival": ReplyPlayer's own extrapolation (playedEstimate, the restart on
 *   a dry player), with performance.now() in place of the AudioContext clock and no worklet. A timer stands in for the
 *   worklet's progress reports (the live caption reveals words with it, never ahead of what the room heard: the same
 *   liveRevealCount, capped at four words a second until the reply's audio is all in) and for its `drained` (after
 *   reply_end, once the clock has passed the last sample). Zoom adds its own latency on top (hundreds of ms, the
 *   screen share more): a fixed offset is not modelled.
 */
import { ReplyPlayer } from "./audio.js";
import { samplesToMs } from "./util.js";

export const OBSERVER_TICK_MS = 100; // like the pcm-player worklet's progress reports

/** The AudioEngine's shape, as ReplyPlayer reads it, on the wall clock: no context, no worklets, no latency. */
class WallClock {
  constructor() {
    /** @type {null} */
    this.ctx = null;
    this.workletsReady = false;
    /** @type {null} */
    this.replyBus = null;
  }

  /** Seconds, like AudioContext.currentTime. */
  now() {
    return performance.now() / 1000;
  }

  latency() {
    return 0;
  }
}

/** @typedef {ConstructorParameters<typeof ReplyPlayer>[1]} ReplyHandlers */

export class ObserverReplyPlayer extends ReplyPlayer {
  /** @param {ReplyHandlers} handlers */
  constructor(handlers) {
    super(/** @type {any} */ (new WallClock()), handlers);
    /** @type {ReturnType<typeof setInterval>|undefined} */
    this.timer = undefined;
  }

  /** The clock runs while a reply plays: progress every OBSERVER_TICK_MS, drained once it has played out. */
  run() {
    if (this.timer === undefined && this.status === "playing") this.timer = setInterval(() => this.tick(), OBSERVER_TICK_MS);
  }

  halt() {
    clearInterval(this.timer);
    this.timer = undefined;
  }

  tick() {
    if (this.status !== "playing" || this.reply === null) {
      this.halt();
      return;
    }
    this.handlers.onProgress?.();
    if (this.ended && this.playedNow() >= this.received) {
      this.halt();
      this.played = this.received;
      this.status = "drained";
      this.handlers.onDrained(this.reply, samplesToMs(this.received));
    }
  }

  /** @param {number} reply */
  start(reply) {
    super.start(reply);
    this.run();
  }

  /** @param {number|null} reply @param {unknown} b64 */
  push(reply, b64) {
    super.push(reply, b64);
    this.run();
  }

  /** `reply_end`: no more audio; drained once the clock has played what was received. @param {number|null} reply */
  end(reply) {
    if (!this.accept(reply) || this.ended) return;
    this.ended = true;
    this.tick();
  }

  /** @param {number|null} [reply] */
  flush(reply = null) {
    const snap = super.flush(reply);
    if (snap) this.halt();
    return snap;
  }

  reset() {
    super.reset();
    this.halt();
  }
}
