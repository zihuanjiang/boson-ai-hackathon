// @ts-check
/**
 * The deck control socket (`/ws`, JSON text frames; protocol in doc/design.md §4). Reconnects with backoff and
 * says `hello` on every (re)connect. A close with code 4000 means another deck took over: no reconnect then, or the
 * two tabs would keep kicking each other off.
 * An observer (contract v5 §3: the page the Zoom bot shares as its screen, `?view=observer`) connects to
 * `/ws/observe`, a read-only copy of the deck's feed, says `hello {client: "observer"}` and nothing else, ever: every
 * other message is dropped here, whatever the page's handlers try.
 */
import { backoffMs } from "./util.js";

export const CLOSE_REPLACED = 4000;
const MAX_BUFFERED = 256 * 1024; // bytes queued in the socket before droppable frames (mic) are skipped

/** @typedef {{type: string, [field: string]: any}} Msg */
/** @typedef {"deck"|"observer"} Client */

/**
 * @typedef {object} SocketHandlers
 * @property {(msg: Msg) => void} onMessage
 * @property {() => void} [onOpen]  after `hello` went out
 * @property {(info: {code: number, replaced: boolean, retryMs: number|null}) => void} [onClose]
 */

/** ws:// or wss:// URL of the deck socket on this page's server. @param {Location} loc */
export function deckSocketUrl(loc) {
  return `${loc.protocol === "https:" ? "wss:" : "ws:"}//${loc.host}/ws`;
}

/** ws:// or wss:// URL of the observer socket (`/ws/observe`): the deck's feed, read-only. @param {Location} loc */
export function observerSocketUrl(loc) {
  return `${deckSocketUrl(loc)}/observe`;
}

/**
 * Is this page the observer (the facilitator's shared screen in Zoom)? `?view=observer` (or `?view=zoom`, the
 * contract's other spelling) in the query, or `#observer` as the hash.
 * @param {{search?: unknown, hash?: unknown}} loc
 */
export function isObserverView(loc) {
  const search = typeof loc.search === "string" ? loc.search : "";
  const hash = typeof loc.hash === "string" ? loc.hash : "";
  return /[?&]view=(observer|zoom)(?:&|$)/.test(search) || /^#observer$/i.test(hash);
}

export class DeckSocket {
  /**
   * @param {string} url
   * @param {SocketHandlers} handlers
   * @param {{client?: Client}} [options]  client: who says hello (default "deck"); an observer sends nothing else
   */
  constructor(url, handlers, options = {}) {
    this.url = url;
    this.handlers = handlers;
    /** @type {Client} */
    this.client = options.client ?? "deck";
    /** @type {WebSocket|null} */
    this.ws = null;
    this.attempt = 0;
    /** @type {ReturnType<typeof setTimeout>|undefined} */
    this.timer = undefined;
    this.stopped = false;
    this.replaced = false;
  }

  get open() {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }

  connect() {
    clearTimeout(this.timer);
    if (this.stopped || this.ws) return;
    /** @type {WebSocket} */
    let ws;
    try {
      ws = new WebSocket(this.url);
    } catch (err) {
      console.warn("[relay] socket:", err);
      this.retry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.replaced = false;
      this.send({ type: "hello", client: this.client });
      this.handlers.onOpen?.();
    };
    ws.onmessage = (event) => {
      if (this.ws !== ws || typeof event.data !== "string") return;
      this.attempt = 0; // the server is really talking to us: reset the backoff
      /** @type {unknown} */
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        console.warn("[relay] socket: dropped a frame that is not JSON");
        return;
      }
      if (!msg || typeof msg !== "object" || typeof (/** @type {any} */ (msg).type) !== "string") return;
      this.handlers.onMessage(/** @type {Msg} */ (msg));
    };
    ws.onerror = () => {}; // a close event always follows; the browser already logs the reason
    ws.onclose = (event) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.replaced = event.code === CLOSE_REPLACED;
      const retryMs = this.replaced || this.stopped ? null : this.retry();
      this.handlers.onClose?.({ code: event.code, replaced: this.replaced, retryMs });
    };
  }

  /** Schedule the next attempt; returns its delay. */
  retry() {
    const delay = backoffMs(this.attempt++);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.connect(), delay);
    return delay;
  }

  /** Reconnect now (e.g. "take over" after another deck replaced this one). */
  reconnectNow() {
    this.stopped = false;
    this.attempt = 0;
    this.connect();
  }

  /**
   * Send one message; false when the socket is not open (the message is dropped, never queued: a stale key press
   * replayed after a reconnect would do the wrong thing). An observer sends nothing but `hello`.
   * @param {Msg} msg
   */
  send(msg) {
    if (this.client === "observer" && msg.type !== "hello") return false;
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    try {
      ws.send(JSON.stringify(msg));
      return true;
    } catch (err) {
      console.warn("[relay] socket send failed:", err);
      return false;
    }
  }

  /** Like send, but skipped while the socket is backed up (for high-rate frames such as `mic`). @param {Msg} msg */
  sendDroppable(msg) {
    if (this.ws && this.ws.bufferedAmount > MAX_BUFFERED) return false;
    return this.send(msg);
  }
}
