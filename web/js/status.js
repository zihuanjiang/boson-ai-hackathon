// @ts-check
/**
 * The presenter's readouts besides the meeting window itself (doc/design.md §8): the status strip, now the hidden
 * "Meeting info" panel (state, phase, slide/chunk, mic meter, Realtime dot, reply latency, MIC_MODE, fake-audio and
 * server badges), the tool ticker under the Relay tile, the banner with the countdown, error toasts, and the join
 * dialog's checklist (the pre-show check behind "Join with Computer Audio").
 */
import { formatMs, toolLabel } from "./util.js";

/** @type {Record<string, string>} */
const STATE_LABELS = {
  IDLE: "Idle",
  NARRATING: "Narrating",
  PAUSED: "Paused",
  LISTENING: "Listening",
  REPLYING: "Replying",
};
/** @type {Record<string, string>} */
const PHASE_LABELS = { opening: "Opening", presenting: "Presenting", qa: "Q&A", human: "Human" };
/** @type {Record<string, string>} Higgs Realtime link statuses, as words */
export const RT_LABELS = {
  offline: "offline",
  connecting: "connecting…",
  ready: "ready",
  reconnecting: "reconnecting…",
  error: "error",
};
const TICKER_SIZE = 3;

/**
 * @param {ParentNode} root
 * @param {string} selector
 * @returns {HTMLElement}
 */
function part(root, selector) {
  const el = root.querySelector(selector);
  if (!(el instanceof HTMLElement)) throw new Error(`status: missing ${selector} in index.html`);
  return el;
}

export class StatusStrip {
  /**
   * @param {HTMLElement} root  the strip (the meeting info panel)
   * @param {HTMLElement} ticker  the tool ticker (under the Relay tile)
   */
  constructor(root, ticker) {
    this.root = root;
    this.badge = part(root, ".state-badge");
    this.badgeText = part(root, ".state-text");
    this.phaseEl = part(root, ".phase");
    this.whereEl = part(root, ".where");
    this.ticker = ticker;
    this.latencyEl = part(root, ".latency-value");
    this.meter = part(root, ".meter");
    this.rt = part(root, ".rt");
    this.rtText = part(root, ".rt-text");
    this.micModeEl = part(root, ".micmode");
    this.fakeEl = part(root, ".fake-badge");
    this.serverEl = part(root, ".server-badge");
  }

  /**
   * @param {string} state
   * @param {string} phase
   * @param {boolean} thinking
   */
  state(state, phase, thinking) {
    this.badge.dataset.state = state;
    this.badge.classList.toggle("thinking", state === "REPLYING" && thinking);
    this.badgeText.textContent = state === "REPLYING" && thinking ? "Thinking" : STATE_LABELS[state] ?? state;
    this.phaseEl.textContent = PHASE_LABELS[phase] ?? phase;
    this.phaseEl.hidden = !phase;
  }

  /** @param {string} text  e.g. "How it works · 2/3" */
  where(text) {
    this.whereEl.textContent = text;
    this.whereEl.title = text;
  }

  /**
   * Higgs Realtime link status (dot colour, detail on hover).
   * @param {string} status
   * @param {string} detail
   */
  connection(status, detail) {
    this.rt.dataset.status = status;
    this.rtText.textContent = status === "ready" ? "Realtime" : `Realtime ${RT_LABELS[status] ?? status}`;
    this.rt.title = `Higgs Realtime: ${status}${detail ? ` (${detail})` : ""}`;
  }

  /**
   * Our own server (the deck socket).
   * @param {boolean} up
   * @param {string} [detail]
   */
  server(up, detail = "") {
    this.serverEl.hidden = up;
    this.serverEl.title = detail;
    this.root.classList.toggle("offline", !up);
  }

  /** @param {number} level  0..1 */
  micLevel(level) {
    this.meter.style.setProperty("--level", level.toFixed(3));
  }

  /** @param {boolean} open  the backend has the mic open (frames are streaming) */
  micOpen(open) {
    this.meter.classList.toggle("open", open);
    this.meter.title = open ? "Microphone: open (streaming to Relay)" : "Microphone level (closed)";
  }

  /** @param {number} ms  end of user speech → first reply audio */
  latency(ms) {
    if (Number.isFinite(ms)) this.latencyEl.textContent = formatMs(ms);
  }

  /** @param {string} mode */
  micMode(mode) {
    this.micModeEl.textContent = mode === "hold" ? "Hold" : "Tap";
    this.micModeEl.title = `MIC_MODE=${mode}: ${mode === "hold" ? "hold Space to talk" : "tap Space to talk"}`;
  }

  /** @param {boolean} on */
  fake(on) {
    this.fakeEl.hidden = !on;
  }

  /**
   * Tool ticker: the last three calls, newest first; a call's status updates in place.
   * @param {{call_id?: unknown, name?: unknown, args?: unknown, status?: unknown}} msg
   */
  tool(msg) {
    const id = String(msg.call_id ?? `${String(msg.name)}:${Date.now()}`);
    const status = String(msg.status ?? "queued");
    const label = toolLabel(msg.name, msg.args);
    let li = /** @type {HTMLElement|null} */ (
      Array.from(this.ticker.children).find((el) => el instanceof HTMLElement && el.dataset.callId === id) ?? null
    );
    if (li) {
      li.classList.remove("flash");
      void li.offsetWidth; // restart the flash animation
      li.classList.add("flash");
    } else {
      li = document.createElement("li");
      li.className = "tool";
      li.dataset.callId = id;
      const dot = document.createElement("i");
      dot.className = "tool-dot";
      const name = document.createElement("span");
      name.className = "tool-name";
      const state = document.createElement("span");
      state.className = "tool-status";
      li.append(dot, name, state);
      this.ticker.prepend(li);
      while (this.ticker.children.length > TICKER_SIZE) this.ticker.lastElementChild?.remove();
    }
    part(li, ".tool-name").textContent = label;
    part(li, ".tool-status").textContent = status;
    li.dataset.status = status;
    li.title = `${label}: ${status}`;
  }
}

const BANNER_HOLD_MS = 8000; // a banner announces a change, then fades: the Relay tile keeps the state

export class Banner {
  /** @param {HTMLElement} el */
  constructor(el) {
    this.el = el;
    this.textEl = part(el, ".banner-text");
    this.countEl = part(el, ".banner-count");
    this.message = "";
    this.seconds = 0;
    /** @type {ReturnType<typeof setInterval>|undefined} */
    this.timer = undefined;
    /** @type {ReturnType<typeof setTimeout>|undefined} */
    this.settleTimer = undefined;
  }

  /** The state's banner ("Ken has the floor", "Waiting for a question — tap Space", …). @param {unknown} message */
  text(message) {
    const next = typeof message === "string" ? message.trim() : "";
    if (next !== this.message) {
      this.message = next;
      clearTimeout(this.settleTimer);
      this.el.classList.remove("settled");
      if (next) this.settleTimer = setTimeout(() => this.el.classList.add("settled"), BANNER_HOLD_MS);
    }
    this.render();
  }

  /** A resume countdown is running. */
  get counting() {
    return this.seconds > 0;
  }

  /** Resume countdown: n > 0 starts it (ticking here), 0 cancels it. @param {number} seconds */
  countdown(seconds) {
    clearInterval(this.timer);
    this.seconds = Math.max(0, Math.floor(Number(seconds) || 0));
    if (this.seconds > 0) {
      this.timer = setInterval(() => {
        this.seconds -= 1;
        if (this.seconds <= 0) clearInterval(this.timer);
        this.render();
      }, 1000);
    }
    this.render();
  }

  render() {
    const counting = this.seconds > 0;
    this.textEl.textContent = this.message || (counting ? "Resuming" : "");
    this.countEl.textContent = counting ? String(this.seconds) : "";
    this.countEl.hidden = !counting;
    this.el.classList.toggle("counting", counting);
    this.el.hidden = !this.message && !counting;
  }
}

export class Toasts {
  /** @param {HTMLElement} el */
  constructor(el) {
    this.el = el;
  }

  /**
   * @param {string} message
   * @param {"error"|"info"} [kind]
   */
  show(message, kind = "error") {
    const text = String(message ?? "").trim();
    if (!text) return;
    for (const old of Array.from(this.el.children)) {
      if (old.textContent === text) old.remove(); // the same message again: restart it, don't stack it
    }
    const toast = document.createElement("div");
    toast.className = `toast ${kind}`;
    toast.setAttribute("role", kind === "error" ? "alert" : "status");
    toast.textContent = text;
    this.el.append(toast);
    while (this.el.children.length > 3) this.el.firstElementChild?.remove();
    setTimeout(() => {
      toast.classList.add("leaving");
      setTimeout(() => toast.remove(), 450);
    }, kind === "error" ? 5000 : 3500);
  }
}

/** @typedef {"pending"|"working"|"ok"|"warn"|"fail"} CheckStatus */

export class Checklist {
  /** @param {HTMLElement} root  the join dialog (the Arm overlay) */
  constructor(root) {
    this.root = root;
    this.titleEl = part(root, ".panel-title");
    this.subEl = part(root, ".panel-sub");
    this.meterEl = root.querySelector(".checklist .meter");
  }

  /**
   * The dialog's heading: the talk, and who presents and hosts it.
   * @param {string} title
   * @param {string} sub
   */
  heading(title, sub) {
    this.titleEl.textContent = title;
    this.subEl.textContent = sub;
  }

  /**
   * Rename a row (the narration row says whether it is the narration or the backup).
   * @param {string} item
   * @param {string} text
   */
  label(item, text) {
    const el = this.root.querySelector(`[data-item="${item}"] .label`);
    if (el) el.textContent = text;
  }

  /** Is any row failing? (Then the dialog stays up after joining, and says "Join anyway".) */
  failing() {
    return this.root.querySelector('.checklist [data-status="fail"]') !== null;
  }

  /**
   * @param {string} item  server | talk | audio | mic | narration | realtime
   * @param {CheckStatus} status
   * @param {string} [detail]
   */
  set(item, status, detail) {
    const li = this.root.querySelector(`[data-item="${item}"]`);
    if (!(li instanceof HTMLElement)) return;
    li.dataset.status = status;
    if (detail !== undefined) {
      const el = li.querySelector(".detail");
      if (el) {
        el.textContent = detail;
        if (el instanceof HTMLElement) el.title = detail;
      }
    }
  }

  /** @param {number} level  0..1 */
  meter(level) {
    if (this.meterEl instanceof HTMLElement) this.meterEl.style.setProperty("--level", level.toFixed(3));
  }
}
