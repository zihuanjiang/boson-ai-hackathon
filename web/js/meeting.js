// @ts-check
/**
 * The meeting window around the deck (D55: Relay presents in what looks like a video call). The topbar (the talk,
 * the Higgs Realtime link, backup / fake / server badges, the meeting clock, the info button), the participant tiles
 * (Relay: a speaking ring driven by its real output level, what it is doing, the tool ticker; You: the mic level,
 * muted or not, and a hint when someone talks into the muted mic), the meeting chat (what was said, tool calls,
 * slides, typed questions), the toolbar (mic, panels, slides, More, Leave) and the "you left" card.
 * DOM only: main.js owns the socket and every action. It calls these methods from its message handlers, and the
 * controls call back through MeetingHandlers.
 */
import { RT_LABELS } from "./status.js";
import { talkTitle } from "./talk.js";
import { clockText, meterLevel, toolLabel } from "./util.js";

/** @typedef {import("./talk.js").Talk} Talk */
/** @typedef {import("./talk.js").TalkSlide} TalkSlide */
/** @typedef {"next"|"prev"|"pause"|"human"|"relay"|"type"} ControlAction */

/**
 * What the Relay tile shows, from the backend's `state` (and whether a countdown runs).
 * @typedef {object} MeetingState
 * @property {string} state  IDLE | NARRATING | PAUSED | LISTENING | REPLYING
 * @property {string} phase  opening | presenting | qa | human
 * @property {string|null} narration  live | backup | rendered while NARRATING, else null
 * @property {boolean} thinking
 * @property {string|null} banner
 * @property {boolean} countdown  a countdown runs (its banner says "Starting…" or "Resuming…")
 */

/**
 * @typedef {object} MeetingHandlers
 * @property {(phase: "tap"|"down"|"up") => void} onMic  the mic button: a click (tap mode), or press / release (hold)
 * @property {(action: ControlAction) => void} onAction  ◀ / ▶ and the More menu's presenter actions
 * @property {() => void} onChecklist  More → the audio and connection check (the join dialog)
 * @property {(text: string) => void} onTyped  a question sent from the chat box
 * @property {() => void} onLeave  Leave: Relay stops and the presenter has the floor
 * @property {() => void} onLayout  the shared screen changed size: re-fit the slides
 */

export const SPEAKING_LEVEL = 0.02; // Relay's output RMS above this = speaking; below it = quiet
export const SPEAKING_HOLD_MS = 250; // a speaking border outlasts the last loud frame by this much
export const MIC_SPEAKING_LEVEL = 0.06; // mic RMS above this with the mic open = you are speaking
export const MUTED_TALK_LEVEL = 0.12; // mic RMS above this with the mic closed = someone talks into a muted mic…
export const MUTED_TALK_MS = 1200; // …for this long (gaps between words shorter than MUTED_GAP_MS don't count)…
const MUTED_GAP_MS = 400;
export const RELAY_QUIET_MS = 500; // …while Relay has been quiet this long (so it is not Relay's voice in the mic)
export const HINT_EVERY_MS = 20000;
export const HINT_SHOW_MS = 5000;
const CHAT_MAX_LINES = 200;
/** @type {ReadonlySet<string>} */
const MENU_ACTIONS = new Set(["pause", "human", "relay", "prev", "next", "type"]);

/**
 * What the Relay tile says it is doing.
 * @param {MeetingState} s
 * @param {string} presenter
 */
export function relayStatus(s, presenter) {
  switch (s.state) {
    case "NARRATING":
      return s.narration === "backup" ? "Presenting · backup audio" : "Presenting";
    case "LISTENING":
      return "Listening…";
    case "REPLYING":
      return s.thinking ? "Thinking…" : "Answering";
    case "PAUSED":
      return "Paused";
    default:
      break;
  }
  if (s.countdown) return s.banner || "Resuming…";
  if (s.phase === "opening") return `Waiting for ${presenter}`;
  if (s.phase === "qa") return "Waiting for a question";
  if (s.phase === "human") return `${presenter} has the floor`;
  return "Waiting";
}

/**
 * The mic button's label and tooltip. Tap mode: a click interrupts Relay and opens the mic ("Unmute"); with the mic
 * open, a click sends what was said now ("Mute"). Hold mode: hold it like Space.
 * @param {string} mode  tap | hold
 * @param {boolean} open  the backend has the mic open
 */
export function micButtonText(mode, open) {
  if (mode === "hold") {
    const title = open ? "Release to send it to Relay" : "Hold to interrupt Relay and speak (hold Space)";
    return { label: "Hold to talk", title };
  }
  return open
    ? { label: "Mute", title: "Done talking: send it to Relay now (Space)" }
    : { label: "Unmute", title: "Interrupt Relay and speak (Space)" };
}

/**
 * @param {ParentNode} root
 * @param {string} selector
 * @returns {HTMLElement}
 */
function part(root, selector) {
  const el = root.querySelector(selector);
  if (!(el instanceof HTMLElement)) throw new Error(`meeting: missing ${selector} in index.html`);
  return el;
}

/** @param {HTMLElement} el @param {string} name @param {boolean} on */
function setFlag(el, name, on) {
  const value = String(on);
  if (el.getAttribute(name) !== value) el.setAttribute(name, value);
}

export class Meeting {
  /**
   * @param {HTMLElement} root  the .meeting element
   * @param {MeetingHandlers} handlers
   */
  constructor(root, handlers) {
    this.root = root;
    this.handlers = handlers;
    this.presenter = "the presenter";
    /** @type {MeetingState} */
    this.current = { state: "IDLE", phase: "opening", narration: null, thinking: false, banner: null, countdown: false };
    /** @type {string|null} */
    this.slideId = null;
    this.slideTitle = "";
    /** @type {number|null} performance.now() when you joined (the clock counts from it) */
    this.joinedAt = null;
    this.clockShown = "";
    this.micMode = "tap";
    this.open = false;
    this.holding = false; // hold mode: the mic button is pressed
    // levels (performance.now() times)
    this.relayLoudAt = -Infinity;
    /** @type {number|null} */
    this.relayQuietSince = null;
    this.relayRingLevel = -1;
    this.youLoudAt = -Infinity;
    /** @type {number|null} */
    this.talkSince = null;
    this.talkLast = -Infinity;
    this.hintAt = -Infinity;
    /** @type {ReturnType<typeof setTimeout>|undefined} */
    this.hintTimer = undefined;
    /** @type {string|null} a typed question whose caption echo is still to come (shown once, not twice) */
    this.typedEcho = null;
    this.unread = 0;

    this.talkNameEl = part(root, ".talk-name");
    this.liveBadge = part(root, ".live-badge");
    this.liveText = part(root, ".live-text");
    this.backupBadge = part(root, ".backup-badge");
    this.fakePill = part(root, ".fake-pill");
    this.serverPill = part(root, ".server-pill");
    this.clockEl = part(root, ".clock");
    this.infoButton = part(root, ".info-button");
    this.info = part(root, ".strip");
    this.people = part(root, ".people");
    this.relayTile = part(root, ".tile-relay");
    this.relayStatusEl = part(root, ".tile-relay .tile-status");
    this.relayRing = part(root, ".tile-relay .ring");
    this.youTile = part(root, ".tile-you");
    this.youStatusEl = part(root, ".tile-you .tile-status");
    this.youRing = part(root, ".tile-you .ring");
    this.youMic = part(root, ".tile-you .mic-ico");
    this.youRole = part(root, ".tile-you .role");
    this.youInitial = part(root, ".tile-you .avatar-text");
    this.hintEl = part(root, ".tile-hint");
    this.chat = part(root, ".chat");
    this.chatLog = part(root, ".chat-log");
    this.chatForm = part(root, ".chat-compose");
    this.chatInput = /** @type {HTMLInputElement} */ (part(root, ".chat-input"));
    this.micButton = part(root, ".mic-button");
    this.micLabel = part(root, ".mic-button .tb-label");
    this.peopleButton = part(root, ".people-button");
    this.chatButton = part(root, ".chat-button");
    this.chatBadge = part(root, ".chat-button .tb-badge");
    this.ccButton = part(root, ".cc-button");
    this.moreButton = part(root, ".more-button");
    this.menu = part(root, ".more-menu");
    this.leftCard = part(root, ".left-card");
    this.presenterNames = Array.from(root.querySelectorAll(".presenter-name"));
    this.install();
    this.renderMic();
    this.renderYou();
    this.renderRelay();
  }

  install() {
    const h = this.handlers;
    const mic = this.micButton;
    mic.addEventListener("click", () => {
      if (this.micMode !== "hold") h.onMic("tap");
    });
    mic.addEventListener("pointerdown", (/** @type {PointerEvent} */ e) => {
      if (this.micMode !== "hold" || e.button > 0) return;
      e.preventDefault(); // no focus ring, no text selection while held
      this.holding = true;
      h.onMic("down");
    });
    const release = () => {
      if (!this.holding) return;
      this.holding = false;
      h.onMic("up");
    };
    for (const type of ["pointerup", "pointercancel", "pointerleave"]) mic.addEventListener(type, release);
    part(this.root, ".prev-button").addEventListener("click", () => h.onAction("prev"));
    part(this.root, ".next-button").addEventListener("click", () => h.onAction("next"));
    this.peopleButton.addEventListener("click", () => this.togglePeople());
    this.chatButton.addEventListener("click", () => this.toggleChat());
    part(this.root, ".chat-close").addEventListener("click", () => this.toggleChat(false));
    this.ccButton.addEventListener("click", () => this.toggleCaptions());
    this.infoButton.addEventListener("click", () => this.toggleInfo());
    this.moreButton.addEventListener("click", () => this.toggleMenu());
    this.menu.addEventListener("click", (/** @type {MouseEvent} */ e) => this.onMenu(e));
    part(this.root, ".leave-button").addEventListener("click", () => {
      this.closeMenus();
      h.onLeave();
    });
    part(this.root, ".rejoin-button").addEventListener("click", () => this.showLeft(false));
    this.chatForm.addEventListener("submit", (/** @type {SubmitEvent} */ e) => this.onChatSubmit(e));
    document.addEventListener("pointerdown", (e) => {
      const target = e.target instanceof Element ? e.target : null;
      if (!this.menu.hidden && !target?.closest(".more")) this.closeMenus(); // a click anywhere else
    });
  }

  // ---- talk, state, links

  /** @param {Talk} talk */
  talk(talk) {
    this.presenter = talk.presenter || "the presenter";
    const title = talkTitle(talk);
    this.talkNameEl.textContent = title;
    this.talkNameEl.hidden = !title || title.trim().toLowerCase() === "relay"; // the brand next to it says that
    this.fakePill.hidden = !talk.fake_audio;
    this.youRole.textContent = `${this.presenter} · host`;
    this.youInitial.textContent = (this.presenter.trim()[0] ?? "Y").toUpperCase();
    for (const el of this.presenterNames) el.textContent = this.presenter;
    this.renderRelay();
  }

  /** @param {MeetingState} s */
  state(s) {
    this.current = s;
    this.root.dataset.state = s.state;
    this.root.dataset.narration = s.narration ?? "";
    this.backupBadge.hidden = s.narration !== "backup";
    if (s.state === "LISTENING") this.hideHint();
    this.renderRelay();
  }

  renderRelay() {
    const s = this.current;
    this.relayStatusEl.textContent = relayStatus(s, this.presenter);
    this.relayTile.classList.toggle("thinking", s.state === "REPLYING" && s.thinking);
  }

  /**
   * The Higgs Realtime link: green "Higgs Realtime" when ready, grey with the status otherwise.
   * @param {string} status
   * @param {string} detail
   */
  connection(status, detail) {
    this.liveBadge.dataset.status = status;
    this.liveText.textContent = status === "ready" ? "Higgs Realtime" : `Higgs Realtime ${RT_LABELS[status] ?? status}`;
    this.liveBadge.title = `Higgs Realtime: ${status}${detail ? ` (${detail})` : ""}`;
  }

  /** Our own server (the deck socket). @param {boolean} up */
  server(up) {
    this.serverPill.hidden = up;
  }

  /** You joined (Arm): the clock starts. @param {number} now  performance.now() */
  join(now) {
    this.joinedAt = now;
    this.tick(now);
  }

  /** The meeting clock (called every animation frame; writes only when the second changes). @param {number} now */
  tick(now) {
    if (this.joinedAt === null) return;
    const text = clockText(now - this.joinedAt);
    if (text === this.clockShown) return;
    this.clockShown = text;
    this.clockEl.textContent = text;
  }

  // ---- mic and levels

  /**
   * tap | hold. Every `state` message repeats it: only a change counts (a press in progress keeps its release).
   * @param {string} mode
   */
  setMicMode(mode) {
    if (mode === this.micMode) return;
    this.micMode = mode;
    this.holding = false;
    this.renderMic();
  }

  /** The backend has the mic open (frames stream to Relay). @param {boolean} open */
  micOpen(open) {
    this.open = open;
    if (open) this.hideHint();
    this.renderMic();
    this.renderYou();
  }

  renderMic() {
    const { label, title } = micButtonText(this.micMode, this.open);
    this.micLabel.textContent = label;
    this.micButton.title = title;
    setFlag(this.micButton, "aria-pressed", this.open);
    this.micButton.dataset.muted = String(!this.open);
  }

  renderYou() {
    this.youMic.dataset.muted = String(!this.open);
    this.youStatusEl.textContent = this.open ? "Relay is listening" : "";
    if (!this.open) {
      setFlag(this.youTile, "data-speaking", false);
      this.youRing.style.setProperty("--level", "0");
    }
  }

  /**
   * Your microphone's level (every mic frame, ~10 per second, open or not). Open: the speaking border and ring.
   * Closed: someone talking into it for a while, with Relay quiet, gets a hint that the mic is muted.
   * @param {number} level  RMS 0..1 of the last frame
   */
  micLevel(level) {
    const now = performance.now();
    if (level > MIC_SPEAKING_LEVEL) this.youLoudAt = now;
    if (this.open) {
      setFlag(this.youTile, "data-speaking", now - this.youLoudAt < SPEAKING_HOLD_MS);
      this.youRing.style.setProperty("--level", meterLevel(level).toFixed(2));
    }
    if (level > MUTED_TALK_LEVEL) {
      if (this.talkSince === null || now - this.talkLast > MUTED_GAP_MS) this.talkSince = now;
      this.talkLast = now;
      if (this.talkingIntoMutedMic(now)) this.showHint(now);
    }
  }

  /**
   * Someone has been talking into the closed mic, with Relay quiet, while Relay has the floor. Not while the
   * presenter has it (the opening, a human slide, H): talking with the mic closed is then the point, and the hint
   * would pop up on the projector during the presenter's own lines.
   * @param {number} now
   */
  talkingIntoMutedMic(now) {
    const quiet = this.relayQuietSince;
    return (
      this.joinedAt !== null &&
      !this.open &&
      this.current.state !== "LISTENING" &&
      this.current.phase !== "opening" &&
      this.current.phase !== "human" &&
      this.talkSince !== null &&
      now - this.talkSince >= MUTED_TALK_MS &&
      quiet !== null &&
      now - quiet >= RELAY_QUIET_MS &&
      now - this.hintAt >= HINT_EVERY_MS
    );
  }

  /** @param {number} now */
  showHint(now) {
    this.hintAt = now;
    this.hintEl.hidden = false;
    clearTimeout(this.hintTimer);
    this.hintTimer = setTimeout(() => this.hideHint(), HINT_SHOW_MS);
  }

  hideHint() {
    clearTimeout(this.hintTimer);
    this.hintEl.hidden = true;
  }

  /**
   * Relay's audible level (every animation frame while joined): the speaking border (with a short hold, so it
   * doesn't flicker between words) and the ring around its avatar.
   * @param {number} level  RMS 0..1
   */
  outputLevel(level) {
    const now = performance.now();
    if (level > SPEAKING_LEVEL) {
      this.relayLoudAt = now;
      this.relayQuietSince = null;
    } else if (this.relayQuietSince === null) {
      this.relayQuietSince = now;
    }
    const speaking = now - this.relayLoudAt < SPEAKING_HOLD_MS;
    setFlag(this.relayTile, "data-speaking", speaking);
    const ring = speaking ? Math.round(meterLevel(level) * 50) / 50 : 0; // steps of 0.02: few style writes
    if (ring !== this.relayRingLevel) {
      this.relayRingLevel = ring;
      this.relayRing.style.setProperty("--level", String(ring));
    }
  }

  // ---- chat

  /**
   * A tool call: one line per call, updated in place as its status changes.
   * @param {{call_id?: unknown, name?: unknown, args?: unknown, status?: unknown}} msg
   */
  tool(msg) {
    const key = msg.call_id === undefined || msg.call_id === null ? null : `tool:${String(msg.call_id)}`;
    const status = String(msg.status ?? "queued");
    let li = key ? this.lineByKey(key) : null;
    if (!li) {
      li = this.line("tool", "Relay", key);
      const name = document.createElement("code");
      name.className = "chat-tool";
      const chip = document.createElement("span");
      chip.className = "chat-status";
      part(li, ".chat-text").append(name, " ", chip);
    }
    part(li, ".chat-tool").textContent = toolLabel(msg.name, msg.args);
    const chip = part(li, ".chat-status");
    chip.textContent = status;
    chip.dataset.status = status;
  }

  /**
   * A final caption: what you said, Relay's answer, or one short line for a slide Relay presented live.
   * @param {{role?: unknown, text?: unknown, final?: unknown}} msg
   * @param {"narration"|"answer"|null} kind  the reply's kind (assistant captions)
   */
  caption(msg, kind) {
    if (!msg.final) return;
    const text = typeof msg.text === "string" ? msg.text.trim() : "";
    if (msg.role === "user") {
      const echo = this.typedEcho;
      this.typedEcho = null;
      if (text && text !== echo) this.say("you", "You", text);
    } else if (msg.role === "assistant") {
      if (kind === "narration") {
        this.note("presented", this.slideTitle ? `Relay presented "${this.slideTitle}"` : "Relay presented a slide");
      } else if (text) {
        this.say("relay", "Relay", text);
      }
    }
  }

  /** The deck shows a slide (a line when it is another one). @param {TalkSlide|null} slide */
  slide(slide) {
    if (!slide || slide.id === this.slideId) return;
    this.slideId = slide.id;
    this.slideTitle = slide.title || slide.id;
    this.note("slide", `Slide: ${this.slideTitle}`);
  }

  /** A typed question went out (its caption echo won't be shown twice). @param {string} text */
  typed(text) {
    this.say("you", "You", text);
    this.typedEcho = text.trim();
  }

  /**
   * A message line: who, when, what.
   * @param {"you"|"relay"} kind
   * @param {string} who
   * @param {string} text
   */
  say(kind, who, text) {
    part(this.line(kind, who, null), ".chat-text").textContent = text;
    if (this.chat.hidden) {
      this.unread += 1;
      this.renderUnread();
    }
  }

  /**
   * A system line (a slide, a slide presented).
   * @param {string} kind
   * @param {string} text
   */
  note(kind, text) {
    part(this.line(kind, "", null), ".chat-text").textContent = text;
  }

  /**
   * Append a chat line (oldest lines drop past CHAT_MAX_LINES) and keep the newest in view.
   * @param {string} kind
   * @param {string} who  "" for a system line (no header)
   * @param {string|null} key  to find the line again
   */
  line(kind, who, key) {
    const li = document.createElement("li");
    li.className = "chat-line";
    li.dataset.kind = kind;
    if (key) li.dataset.key = key;
    if (who) {
      const head = document.createElement("div");
      head.className = "chat-meta";
      const name = document.createElement("span");
      name.className = "chat-who";
      name.textContent = who;
      const time = document.createElement("span");
      time.className = "chat-time";
      time.textContent = this.joinedAt === null ? "" : clockText(performance.now() - this.joinedAt);
      head.append(name, time);
      li.append(head);
    }
    const body = document.createElement("p");
    body.className = "chat-text";
    li.append(body);
    this.chatLog.append(li);
    while (this.chatLog.children.length > CHAT_MAX_LINES) this.chatLog.firstElementChild?.remove();
    this.chatLog.scrollTop = this.chatLog.scrollHeight;
    return li;
  }

  /** @param {string} key */
  lineByKey(key) {
    const found = Array.from(this.chatLog.children).find((li) => li instanceof HTMLElement && li.dataset.key === key);
    return found instanceof HTMLElement ? found : null;
  }

  renderUnread() {
    this.chatBadge.textContent = this.unread > 9 ? "9+" : String(this.unread);
    this.chatBadge.hidden = this.unread === 0;
  }

  /** @param {SubmitEvent} e */
  onChatSubmit(e) {
    e.preventDefault();
    const text = this.chatInput.value.trim();
    this.chatInput.value = "";
    this.chatInput.blur(); // the presenter keys work again at once
    if (text) this.handlers.onTyped(text);
  }

  // ---- panels, menu, leave

  /** Participants: the tiles (hidden, the shared screen takes their room). @param {boolean} [on] */
  togglePeople(on = this.people.hidden) {
    this.people.hidden = !on;
    setFlag(this.peopleButton, "aria-pressed", on);
    this.layoutChanged();
  }

  /** @param {boolean} [on] */
  toggleChat(on = this.chat.hidden) {
    this.chat.hidden = !on;
    setFlag(this.chatButton, "aria-pressed", on);
    if (on) {
      this.unread = 0;
      this.renderUnread();
      this.chatLog.scrollTop = this.chatLog.scrollHeight;
    }
    this.layoutChanged();
  }

  /** @param {boolean} [on] */
  toggleCaptions(on = this.root.classList.contains("captions-off")) {
    this.root.classList.toggle("captions-off", !on);
    setFlag(this.ccButton, "aria-pressed", on);
    this.layoutChanged();
  }

  /** The meeting info panel (the status strip): ⓘ, the I key, or More. @param {boolean} [on] */
  toggleInfo(on = this.info.hidden) {
    this.info.hidden = !on;
    setFlag(this.infoButton, "aria-expanded", on);
  }

  /** The side column goes when both of its panels are hidden; either way the slides re-fit. */
  layoutChanged() {
    this.root.classList.toggle("side-off", this.people.hidden && this.chat.hidden);
    this.handlers.onLayout();
  }

  /** @param {boolean} [open] */
  toggleMenu(open = this.menu.hidden) {
    this.menu.hidden = !open;
    setFlag(this.moreButton, "aria-expanded", open);
  }

  /** Close the More menu; true when it was open (Esc then does only that). */
  closeMenus() {
    if (this.menu.hidden) return false;
    this.toggleMenu(false);
    return true;
  }

  /** @param {MouseEvent} e */
  onMenu(e) {
    const target = e.target instanceof Element ? e.target : null;
    const item = target?.closest("[data-action]");
    if (!(item instanceof HTMLElement)) return;
    this.closeMenus();
    const action = item.dataset.action ?? "";
    if (action === "checklist") this.handlers.onChecklist();
    else if (action === "info") this.toggleInfo();
    else if (MENU_ACTIONS.has(action)) this.handlers.onAction(/** @type {ControlAction} */ (action));
  }

  /** "You left the meeting" (after Leave), until Rejoin or any presenter key. @param {boolean} on */
  showLeft(on) {
    this.leftCard.hidden = !on;
  }
}
