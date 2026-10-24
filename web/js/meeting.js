// @ts-check
/**
 * The meeting window around the deck (D55: the AI presents in what looks like a video call). The topbar (the talk,
 * the Higgs Realtime link, backup / fake / server badges, the meeting clock, the info button), the participant tiles
 * (the AI: a speaking ring driven by its real output level, what it is doing, the tool ticker; You: the mic level,
 * muted or not, and a hint when someone talks into the muted mic), the meeting chat (what was said, tool calls,
 * slides, typed questions), the toolbar (mic, panels, slides, More, Leave) and the "you left" card.
 * A meeting talk (On the Same Page, contract v3 §7: `talk.meeting` set) renames the window ("On the Same Page", the
 * "Facilitator" tile) and adds the agenda strip, the tabbed side panel (Evidence, Notes, Transcript, Chat), the
 * evidence callout, the speaker picker, the room mic and "Restart meeting" (panels.js). A simulated online meeting
 * (contract v3.1 §4: the participants are prerecorded clips, the mic is off) replaces the room's tile with one tile
 * per participant (a "Recorded" tag, a speaking ring from their own clip), shows the disclosure badge in the topbar,
 * the demo script's controls in the agenda strip and the routing line in Meeting info, and the facilitator's tile
 * says who interrupted it (a barge-in). A plain talk looks and behaves exactly like Relay. DOM only: main.js owns the
 * socket and every action. It calls these methods from its message handlers, and the controls call back through
 * MeetingHandlers.
 */
import { FACILITATOR_KINDS, idKind } from "./facilitator.js";
import { Agenda, Callout, DemoStrip, ParticipantTiles, SidePanel, Speakers } from "./panels.js";
import { RT_LABELS } from "./status.js";
import { talkTitle } from "./talk.js";
import { clockText, meterLevel, toolLabel } from "./util.js";

/** @typedef {import("./talk.js").Talk} Talk */
/** @typedef {import("./talk.js").TalkSlide} TalkSlide */
/** @typedef {import("./facilitator.js").MeetingInfo} MeetingInfo */
/** @typedef {import("./facilitator.js").MeetingState} FacilitatorState */
/** @typedef {import("./facilitator.js").Lookup} Lookup */
/** @typedef {import("./facilitator.js").Utterance} Utterance */
/** @typedef {import("./facilitator.js").AlignmentEvent} AlignmentEvent */
/** @typedef {import("./facilitator.js").Checkpoint} Checkpoint */
/** @typedef {"next"|"prev"|"pause"|"human"|"relay"|"type"} ControlAction */

/**
 * What the AI's tile shows, from the backend's `state` (and whether a countdown runs).
 * @typedef {object} MeetingState
 * @property {string} state  IDLE | NARRATING | PAUSED | LISTENING | REPLYING (| ROOM | INTERVENING in a meeting)
 * @property {string} phase  opening | presenting | qa | human
 * @property {string|null} narration  live | backup | rendered while NARRATING, else null
 * @property {boolean} thinking
 * @property {string|null} banner
 * @property {boolean} countdown  a countdown runs (its banner says "Starting…" or "Resuming…")
 * @property {string|null} [agendaPhase]  presentation | discussion | wrapup (a meeting talk)
 * @property {string|null} [status]  the checkpoint's status (a meeting talk)
 * @property {string|null} [replyKind]  the kind of the reply playing (narration, answer, intervention, …)
 */

/**
 * @typedef {object} MeetingHandlers
 * @property {(phase: "tap"|"down"|"up"|"room") => void} onMic  the mic button: a click (tap mode), press / release
 *   (hold mode), or a click on the room mic (the discussion: mute / unmute it here)
 * @property {(action: ControlAction) => void} onAction  ◀ / ▶ and the More menu's presenter actions
 * @property {() => void} onChecklist  More → the audio and connection check (the join dialog)
 * @property {(text: string) => void} onTyped  a question sent from the chat box
 * @property {() => void} onLeave  Leave: the AI stops and the presenter has the floor
 * @property {() => void} onLayout  the shared screen changed size: re-fit the slides
 * @property {() => void} [onPhase]  the agenda's phase button (a meeting talk)
 * @property {() => void} [onReset]  More → Restart meeting, confirmed (a meeting talk)
 * @property {(name: string|null) => void} [onSpeaker]  a speaker picked in the discussion (null: the room)
 * @property {() => void} [onDemoRun]  Run demo / Stop demo (a simulated online meeting)
 * @property {() => void} [onDemoPlay]  Play next: the next step now, by hand
 */

/**
 * How the window names the product and the AI.
 * @typedef {object} Names
 * @property {string} product  the topbar's brand and the page title
 * @property {string} ai  the AI mid-sentence
 * @property {string} aiCap  the AI at the start of a sentence
 * @property {string} tile  the AI's tile and chat name
 * @property {string} role  under the AI's name on its tile
 */

/** @type {Names} */
export const RELAY_NAMES = Object.freeze({ product: "Relay", ai: "Relay", aiCap: "Relay", tile: "Relay", role: "AI co-presenter" });
/** @type {Names} */
export const FACILITATOR_NAMES = Object.freeze({
  product: "On the Same Page",
  ai: "the facilitator",
  aiCap: "The facilitator",
  tile: "Facilitator",
  role: "AI facilitator",
});

export const SPEAKING_LEVEL = 0.02; // the AI's output RMS above this = speaking; below it = quiet
export const SPEAKING_HOLD_MS = 250; // a speaking border outlasts the last loud frame by this much
export const MIC_SPEAKING_LEVEL = 0.06; // mic RMS above this with the mic open = you are speaking
export const MUTED_TALK_LEVEL = 0.12; // mic RMS above this with the mic closed = someone talks into a muted mic…
export const MUTED_TALK_MS = 1200; // …for this long (gaps between words shorter than MUTED_GAP_MS don't count)…
const MUTED_GAP_MS = 400;
export const RELAY_QUIET_MS = 500; // …while the AI has been quiet this long (so it is not its voice in the mic)
export const HINT_EVERY_MS = 20000;
export const HINT_SHOW_MS = 5000;
export const RESET_CONFIRM_MS = 5000; // "Restart meeting" waits this long for its confirming second click
export const BARGE_IN_MS = 2000; // the facilitator's tile says who interrupted it this long
const CHAT_MAX_LINES = 200;
/** @type {ReadonlySet<string>} */
const MENU_ACTIONS = new Set(["pause", "human", "relay", "prev", "next", "type"]);
/** The side panel's tab for each agenda phase (it switches there when the phase moves). */
const PHASE_TABS = /** @type {const} */ ({ presentation: "evidence", discussion: "transcript", wrapup: "notes" });

/**
 * What the AI's tile says it is doing.
 * @param {MeetingState} s
 * @param {string} presenter
 */
export function relayStatus(s, presenter) {
  switch (s.state) {
    case "NARRATING":
      return s.narration === "backup" ? "Presenting · backup audio" : "Presenting";
    case "LISTENING":
      return "Listening…";
    case "ROOM":
      return "Listening to the room";
    case "INTERVENING": // the facilitator speaks on its own in the discussion (by default: an alignment question)
    case "REPLYING":
      if (s.status === "wrapping_up" || s.replyKind === "summary") return "Wrapping up";
      if (s.replyKind === "decision") return "Noting the decision";
      if (s.replyKind === "handover") return "Handing over";
      if (s.state === "INTERVENING" || s.replyKind === "intervention") return "Speaking up";
      if (s.thinking) return "Thinking…";
      return "Answering";
    case "PAUSED":
      return "Paused";
    default:
      break;
  }
  if (s.countdown) return s.banner || "Resuming…";
  if (s.agendaPhase === "wrapup") return "Meeting wrapped up";
  if (s.phase === "opening") return `Waiting for ${presenter}`;
  if (s.phase === "qa") return "Waiting for a question";
  if (s.phase === "human") return `${presenter} has the floor`;
  return "Waiting";
}

/**
 * The mic button's label and tooltip. Tap mode: a click interrupts the AI and opens the mic ("Unmute"); with the mic
 * open, a click sends what was said now ("Mute"). Hold mode: hold it like Space. The discussion's room mic (`room`):
 * on while the facilitator listens to the room; a click mutes it here (no frames go out) and unmutes it again.
 * @param {string} mode  tap | hold
 * @param {boolean} open  the backend has the mic open
 * @param {{muted: boolean}|null} [room]  the room mic of the discussion
 * @param {string} [ai]  the AI's name mid-sentence
 */
export function micButtonText(mode, open, room = null, ai = "Relay") {
  if (room) {
    return room.muted
      ? { label: "Room mic off", title: `The room mic is muted here: ${ai} hears nothing. Click to unmute (Space asks ${ai} directly)` }
      : { label: "Room mic on", title: `The room mic is on: ${ai} listens to the discussion. Click to mute it (Space asks ${ai} directly)` };
  }
  if (mode === "hold") {
    const title = open ? `Release to send it to ${ai}` : `Hold to interrupt ${ai} and speak (hold Space)`;
    return { label: "Hold to talk", title };
  }
  return open
    ? { label: "Mute", title: `Done talking: send it to ${ai} now (Space)` }
    : { label: "Unmute", title: `Interrupt ${ai} and speak (Space)` };
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

/** @param {string} text */
function initial(text) {
  return (text.trim()[0] ?? "Y").toUpperCase();
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
    /** @type {Names} */
    this.names = RELAY_NAMES;
    /** @type {MeetingState} */
    this.current = { state: "IDLE", phase: "opening", narration: null, thinking: false, banner: null, countdown: false };
    /** @type {string|null} */
    this.slideId = null;
    this.slideTitle = "";
    /** @type {{id: string}[]} the talk's slides (the checkpoint's slide number) */
    this.slides = [];
    /** @type {number|null} performance.now() when you joined (the clock counts from it) */
    this.joinedAt = null;
    this.clockShown = "";
    this.micMode = "tap";
    this.open = false;
    this.holding = false; // hold mode: the mic button is pressed
    /** @type {{muted: boolean}|null} the discussion's room mic, while the facilitator listens to the room */
    this.room = null;
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
    // a meeting talk (On the Same Page)
    this.facilitator = false;
    /** @type {MeetingInfo|null} */
    this.info = null;
    /** @type {string|null} */
    this.agendaPhase = null;
    /** @type {string|null} */
    this.speakerName = null;
    /** @type {Set<string>|null} decisions already in the notes (null until the first `meeting`) */
    this.decisionIds = null;
    /** @type {Set<string>} clarifications already in the notes */
    this.clarificationIds = new Set();
    /** @type {number|null} the backend's meeting time minus performance.now(), in ms (the transcript's clock) */
    this.meetingOffsetMs = null;
    /** @type {ReturnType<typeof setTimeout>|undefined} */
    this.resetTimer = undefined;
    // a simulated online meeting (the participants are prerecorded clips, the mic is off)
    this.clips = false;
    /** @type {Map<string, number>} participant → performance.now() of their clip's last loud frame */
    this.clipLoudAt = new Map();
    /** @type {{name: string, until: number}|null} who interrupted the facilitator (a barge-in), shown on its tile */
    this.interrupted = null;
    /** @type {ReturnType<typeof setTimeout>|undefined} */
    this.interruptTimer = undefined;

    this.brandName = part(root, ".brand-name");
    this.viewingText = part(root, ".viewing-text");
    this.talkNameEl = part(root, ".talk-name");
    this.liveBadge = part(root, ".live-badge");
    this.liveText = part(root, ".live-text");
    this.backupBadge = part(root, ".backup-badge");
    this.fakePill = part(root, ".fake-pill");
    this.serverPill = part(root, ".server-pill");
    this.clockEl = part(root, ".clock");
    this.infoButton = part(root, ".info-button");
    this.infoPanel = part(root, ".strip");
    this.people = part(root, ".people");
    this.relayTile = part(root, ".tile-relay");
    this.relayStatusEl = part(root, ".tile-relay .tile-status");
    this.relayRing = part(root, ".tile-relay .ring");
    this.relayName = part(root, ".tile-relay .name");
    this.relayRole = part(root, ".tile-relay .role");
    this.youTile = part(root, ".tile-you");
    this.youStatusEl = part(root, ".tile-you .tile-status");
    this.youRing = part(root, ".tile-you .ring");
    this.youMic = part(root, ".tile-you .mic-ico");
    this.youName = part(root, ".tile-you .name");
    this.youRole = part(root, ".tile-you .role");
    this.youInitial = part(root, ".tile-you .avatar-text");
    this.hintEl = part(root, ".tile-hint");
    this.chat = part(root, ".chat");
    this.chatLog = part(root, ".chat-log");
    this.chatForm = part(root, ".chat-compose");
    this.chatInput = /** @type {HTMLInputElement} */ (part(root, ".chat-input"));
    this.chatClose = part(root, ".chat-close");
    this.micButton = part(root, ".mic-button");
    this.micLabel = part(root, ".mic-button .tb-label");
    this.peopleButton = part(root, ".people-button");
    this.chatButton = part(root, ".chat-button");
    this.chatButtonLabel = part(root, ".chat-button .tb-label");
    this.chatBadge = part(root, ".chat-button .tb-badge");
    this.ccButton = part(root, ".cc-button");
    this.moreButton = part(root, ".more-button");
    this.menu = part(root, ".more-menu");
    this.resetItem = part(root, ".menu-reset");
    this.resetLabel = part(root, ".reset-label");
    this.leftCard = part(root, ".left-card");
    this.presenterNames = Array.from(root.querySelectorAll(".presenter-name"));
    this.agenda = new Agenda(part(root, ".agenda"), () => this.handlers.onPhase?.());
    this.speakers = new Speakers(part(root, ".speakers"), (name) => this.handlers.onSpeaker?.(name));
    this.callout = new Callout(part(root, ".callout"));
    this.side = new SidePanel(this.chat, { onChange: () => this.renderUnread() });
    this.demoPill = part(root, ".demo-pill");
    this.routingEl = part(root, ".strip-routing");
    this.demoStrip = new DemoStrip(part(root, ".demo"), {
      onRun: () => this.handlers.onDemoRun?.(),
      onPlay: () => this.handlers.onDemoPlay?.(),
    });
    this.participants = new ParticipantTiles(this.people);
    this.install();
    this.renderMic();
    this.renderYou();
    this.renderRelay();
  }

  install() {
    const h = this.handlers;
    const mic = this.micButton;
    mic.addEventListener("click", () => {
      if (this.clips) h.onAction("type"); // the mic is off: a line can still be typed
      else if (this.room) h.onMic("room");
      else if (this.micMode !== "hold") h.onMic("tap");
    });
    mic.addEventListener("pointerdown", (/** @type {PointerEvent} */ e) => {
      if (this.clips || this.room || this.micMode !== "hold" || e.button > 0) return;
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
    this.chatClose.addEventListener("click", () => this.toggleChat(false));
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
    this.slides = talk.slides;
    this.presenter = talk.meeting?.host || talk.presenter || "the presenter";
    const title = talkTitle(talk);
    this.talkNameEl.textContent = title;
    this.talkNameEl.hidden = !title || title.trim().toLowerCase() === "relay"; // the brand next to it says that
    this.fakePill.hidden = !talk.fake_audio;
    for (const el of this.presenterNames) el.textContent = this.presenter;
    if (talk.meeting) {
      this.enableMeeting(talk.meeting);
    } else {
      this.youRole.textContent = `${this.presenter} · host`;
      this.youInitial.textContent = initial(this.presenter);
    }
    this.renderYou();
    this.renderRelay();
  }

  /**
   * A meeting talk: On the Same Page's names and panels. The side panel opens on the Evidence tab.
   * @param {MeetingInfo} info
   */
  enableMeeting(info) {
    if (this.facilitator) {
      this.meetingInfo(info);
      return;
    }
    this.facilitator = true;
    this.info = info;
    this.names = FACILITATOR_NAMES;
    this.presenter = info.host || this.presenter;
    for (const el of this.presenterNames) el.textContent = this.presenter;
    this.root.classList.add("has-agenda");
    this.agenda.setup(info);
    this.side.enable(info);
    this.speakers.setup(info.participants);
    this.resetItem.hidden = false;
    this.chatButtonLabel.textContent = "Panels";
    this.chatButton.title = "Evidence, notes, transcript and chat";
    this.chatClose.setAttribute("aria-label", "Close the panel");
    this.applyNames();
    this.agendaPhaseChanged(this.agendaPhase ?? "presentation");
    this.toggleChat(true);
  }

  /** The package again (`hello.meeting`): a plain-talk page that learns it is a meeting turns into one. @param {MeetingInfo} info */
  meetingInfo(info) {
    if (!this.facilitator) {
      this.enableMeeting(info);
      return;
    }
    this.info = info;
    this.side.update(info);
  }

  /** The product's and the AI's names everywhere the page says them. */
  applyNames() {
    const n = this.names;
    this.brandName.textContent = n.product;
    this.viewingText.textContent = `You are viewing ${n.ai}'s screen`;
    this.relayName.textContent = n.tile;
    this.relayRole.textContent = n.role;
    this.hintEl.textContent = `You're muted. Press Space or click Unmute to talk to ${n.ai}.`;
    for (const el of document.querySelectorAll(".ai-name")) el.textContent = n.ai;
    for (const el of document.querySelectorAll(".ai-name-cap")) el.textContent = n.aiCap;
    part(this.root, ".share").setAttribute("aria-label", `${n.aiCap}'s shared screen`);
    part(this.root, ".leave-button").title = `Stop ${n.ai}: ${this.presenter} has the floor (H)`;
    const title = this.talkNameEl.textContent;
    document.title = title ? `${n.product} · ${title}` : n.product;
    this.renderPrompts();
    this.renderMic();
  }

  /** The chat box and the T box say who a typed line is from: a question to the AI, or (discussion) a line of the room. */
  renderPrompts() {
    const label = document.querySelector(".typed-label");
    const typed = document.querySelector(".typed-input");
    const discussion = this.facilitator && this.agendaPhase === "discussion";
    const who = this.speakerName || "the room";
    const placeholder = discussion ? `Type a line for ${who} · Enter adds it` : `Ask ${this.names.ai} a question…`;
    this.chatInput.placeholder = placeholder;
    if (!this.facilitator) return; // a plain talk keeps its own words
    if (label) label.textContent = discussion ? `${this.speakerName || "Room"} says` : `Ask ${this.names.ai}`;
    if (typed instanceof HTMLElement) {
      typed.setAttribute("placeholder", discussion ? `A line for ${who} · Enter adds it · Esc cancels` : "Type a question · Enter sends · Esc cancels");
    }
  }

  /** @param {MeetingState} s */
  state(s) {
    this.current = s;
    this.root.dataset.state = s.state;
    this.root.dataset.narration = s.narration ?? "";
    this.backupBadge.hidden = s.narration !== "backup";
    if (s.state === "LISTENING") this.hideHint();
    if (this.facilitator && s.agendaPhase) this.agendaPhaseChanged(s.agendaPhase);
    this.renderRelay();
  }

  renderRelay() {
    const s = this.current;
    const cut = this.interrupted !== null && performance.now() < this.interrupted.until ? this.interrupted.name : null;
    this.relayStatusEl.textContent = cut ? `Interrupted by ${cut}` : relayStatus(s, this.presenter);
    setFlag(this.relayTile, "data-interrupted", cut !== null);
    this.relayTile.classList.toggle("thinking", !cut && (s.state === "REPLYING" || s.state === "INTERVENING") && s.thinking);
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

  /** The meeting clock now ("" before joining). */
  timeNow() {
    return this.joinedAt === null ? "" : clockText(performance.now() - this.joinedAt);
  }

  /**
   * The backend's meeting time now (what `at_s` counts: the transcript's times), from the last timestamp it sent;
   * the join clock until there is one.
   */
  meetingTimeNow() {
    return this.meetingOffsetMs === null ? this.timeNow() : clockText(performance.now() + this.meetingOffsetMs);
  }

  /** A backend timestamp arrived: the meeting time it is now. @param {number|null} atS */
  syncMeetingClock(atS) {
    if (atS !== null && Number.isFinite(atS)) this.meetingOffsetMs = atS * 1000 - performance.now();
  }

  // ---- the facilitator (a meeting talk)

  /**
   * The agenda phase (from `state.agenda_phase` or `meeting.phase`). When it moves, the side panel shows that
   * phase's tab (presentation: Evidence, discussion: Transcript, wrap-up: Notes) and opens if it was closed.
   * @param {string} phase
   */
  agendaPhaseChanged(phase) {
    if (!this.facilitator || !phase) return;
    const moved = phase !== this.agendaPhase;
    if (moved && phase === "presentation" && (this.agendaPhase === "discussion" || this.agendaPhase === "wrapup")) {
      this.resetLocal(); // the meeting restarted (Restart meeting, from this page or not)
    }
    this.agendaPhase = phase;
    this.agenda.phase(phase);
    this.speakers.show(phase === "discussion");
    if (moved) {
      const tab = /** @type {Record<string, "evidence"|"notes"|"transcript">} */ (PHASE_TABS)[phase];
      if (tab) this.side.select(tab);
      if (this.chat.hidden && this.joinedAt !== null) this.toggleChat(true);
      this.renderPrompts();
      this.renderYou();
    }
  }

  /** The checkpoint (from `state.checkpoint` or `meeting.checkpoint`). @param {Checkpoint|null} cp */
  checkpoint(cp) {
    if (this.facilitator) this.agenda.checkpoint(cp, this.slides);
  }

  /**
   * The `meeting` message: phase, checkpoint and the notes. A decision that is new shows over the share too.
   * @param {FacilitatorState} st
   */
  meetingState(st) {
    if (!this.facilitator) return;
    // A restart (from this page or not) empties the notes (and moves the phase back: agendaPhaseChanged).
    const gone = [...(this.decisionIds ?? []), ...this.clarificationIds].length > 0 && !st.decisions.length && !st.clarifications.length;
    if (gone) this.resetLocal();
    this.clarificationIds = new Set(st.clarifications.map((c) => c.id));
    this.agendaPhaseChanged(st.phase);
    if (st.checkpoint) this.checkpoint(st.checkpoint);
    this.side.notes(st);
    const seen = this.decisionIds;
    this.decisionIds = new Set(st.decisions.map((d) => d.id));
    if (!seen) return; // the first one (a join, a reconnect): nothing is new
    const fresh = st.decisions.filter((d) => !seen.has(d.id)).at(-1);
    if (fresh) {
      this.callout.show("decision", "Added to the notes", fresh.id, [
        { id: fresh.id, kind: "decision", title: fresh.owner ? `Owner: ${fresh.owner}` : "", text: fresh.decision },
      ]);
    }
  }

  /**
   * A lookup: the Evidence tab, and its sources over the share for a while (the answer or the intervention it is for).
   * @param {Lookup} lookup
   */
  evidence(lookup) {
    if (!this.facilitator) return;
    this.syncMeetingClock(lookup.at_s);
    this.side.evidence(lookup);
    const index = this.side.index();
    const items = lookup.results.map((r) => ({
      id: r.source_id,
      kind: idKind(r.source_id, index),
      title: r.title || index.get(r.source_id)?.title || "",
      text: r.excerpt || index.get(r.source_id)?.text || "",
    }));
    const intervention = lookup.purpose === "intervention" || lookup.by === "engine";
    this.callout.show(intervention ? "intervention" : "evidence", intervention ? "Alignment check" : "Evidence", lookup.query, items);
  }

  /** An utterance of the discussion. @param {Utterance} u */
  transcript(u) {
    if (!this.facilitator) return;
    this.syncMeetingClock(u.at_s);
    this.side.utterance(u);
  }

  /** An alignment event. @param {AlignmentEvent} ev */
  alignment(ev) {
    if (this.facilitator) this.side.alignment(ev);
  }

  /** A reply started (its kind). @param {string} kind */
  replyStarted(kind) {
    if (this.facilitator && kind === "intervention") this.side.asked();
  }

  /** The speaker the room's next lines are labelled with (null: the room). @param {string|null} name */
  speaker(name) {
    this.speakerName = name;
    this.speakers.select(name);
    this.renderYou();
    this.renderPrompts();
  }

  /** Restart meeting went out: the local lists go (the notes follow the next `meeting`). */
  resetLocal() {
    this.side.reset();
    this.callout.hide();
    this.decisionIds = null;
    this.clarificationIds = new Set();
    this.meetingOffsetMs = null;
    this.agenda.unlock();
  }

  // ---- the simulated online meeting (prerecorded participants, the mic off)

  /**
   * The participants are prerecorded clips: their tiles replace the room's, the topbar says so, the agenda strip gets
   * the demo script's controls, Meeting info the routing line, and the mic button says the mic is off.
   * @param {{name: string, role: string}[]} participants
   */
  enableDemo(participants) {
    if (this.clips) return;
    this.clips = true;
    this.root.classList.add("has-demo");
    this.youTile.hidden = true;
    this.hideHint();
    this.participants.setup(participants);
    this.demoPill.hidden = false;
    this.routingEl.hidden = false;
    this.demoStrip.show(true);
    this.renderMic();
  }

  /** The demo strip: the script's state and its next step. @param {import("./panels.js").DemoStripView} view */
  demoView(view) {
    if (this.clips) this.demoStrip.render(view);
  }

  /**
   * A participant's clip started (their tile speaks; a clip played by hand says so in the chat too).
   * @param {string} name
   * @param {boolean} manual
   */
  clipStarted(name, manual) {
    this.participants.playing(name, manual);
    if (manual) this.note("demo", `${name}'s clip played by hand (Play next)`);
  }

  /** No clip plays any more. */
  clipStopped() {
    this.participants.playing(null, false);
  }

  /** Something the demo script did on its own (disclosed in the chat). @param {string} text */
  scriptNote(text) {
    this.note("demo", text);
  }

  /**
   * A participant's audible level (every animation frame while joined): their tile's border and ring, from their own
   * clip's bus, never the facilitator's output.
   * @param {string} name
   * @param {number} level  RMS 0..1
   */
  participantLevel(name, level) {
    const now = performance.now();
    if (level > SPEAKING_LEVEL) this.clipLoudAt.set(name, now);
    const speaking = now - (this.clipLoudAt.get(name) ?? -Infinity) < SPEAKING_HOLD_MS;
    this.participants.level(name, speaking, speaking ? Math.round(meterLevel(level) * 50) / 50 : 0);
  }

  /**
   * A barge-in (the backend heard a participant's clip start while the facilitator spoke and cut it): the
   * facilitator's tile says who, for BARGE_IN_MS; the chat keeps a line.
   * @param {string} name
   * @param {number|null} playedMs  how much of the facilitator's reply had been heard
   */
  bargeIn(name, playedMs) {
    const who = name || "a participant";
    this.interrupted = { name: who, until: performance.now() + BARGE_IN_MS };
    clearTimeout(this.interruptTimer);
    this.interruptTimer = setTimeout(() => {
      this.interrupted = null;
      this.renderRelay();
    }, BARGE_IN_MS);
    this.note("barge", `${who} interrupted ${this.names.ai}${playedMs !== null ? ` (${(playedMs / 1000).toFixed(1)} s into its reply)` : ""}`);
    this.renderRelay();
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

  /** The backend has the mic open (frames stream to the AI). @param {boolean} open */
  micOpen(open) {
    this.open = open;
    if (open) this.hideHint();
    this.renderMic();
    this.renderYou();
  }

  /**
   * The discussion's room mic: on (the facilitator listens to the room) or muted here; null outside the discussion.
   * @param {{muted: boolean}|null} room
   */
  roomMic(room) {
    this.room = room;
    if (room) this.holding = false;
    this.renderMic();
    this.renderYou();
  }

  renderMic() {
    if (this.clips) {
      // a simulated online meeting: the physical mic stays off (the participants are clips); a click types a line
      this.micLabel.textContent = "Mic off";
      this.micButton.title = `The microphone is off: the participants are prerecorded clips streamed to ${this.names.ai}. Click to type a line instead (T)`;
      setFlag(this.micButton, "aria-pressed", false);
      this.micButton.dataset.muted = "true";
      this.micButton.dataset.room = "false";
      return;
    }
    const { label, title } = micButtonText(this.micMode, this.open, this.room, this.names.ai);
    const live = this.room ? !this.room.muted : this.open;
    this.micLabel.textContent = label;
    this.micButton.title = title;
    setFlag(this.micButton, "aria-pressed", live);
    this.micButton.dataset.muted = String(!live);
    this.micButton.dataset.room = String(this.room !== null);
  }

  /** The room's mic is streaming (open, and not muted here). */
  get hearing() {
    return this.open && !(this.room && this.room.muted);
  }

  renderYou() {
    this.youMic.dataset.muted = String(!this.hearing);
    if (this.facilitator) {
      // the room's tile is the speaker picked, in the discussion (the picker's phase); else the room
      const p = this.agendaPhase === "discussion" ? this.info?.participants.find((x) => x.name === this.speakerName) ?? null : null;
      const name = p ? p.name : "Room";
      this.youName.textContent = name;
      this.youRole.textContent = p ? p.role || "speaking" : this.info?.participants.map((x) => x.name).join(" · ") || "the room mic";
      this.youInitial.textContent = initial(name);
    }
    const muted = this.room !== null && this.room.muted;
    this.youStatusEl.textContent = muted ? "Room mic muted" : this.open ? `${this.names.aiCap} is listening` : "";
    if (!this.hearing) {
      setFlag(this.youTile, "data-speaking", false);
      this.youRing.style.setProperty("--level", "0");
    }
  }

  /**
   * Your microphone's level (every mic frame, ~10 per second, open or not). Open: the speaking border and ring.
   * Closed: someone talking into it for a while, with the AI quiet, gets a hint that the mic is muted.
   * @param {number} level  RMS 0..1 of the last frame
   */
  micLevel(level) {
    const now = performance.now();
    if (level > MIC_SPEAKING_LEVEL) this.youLoudAt = now;
    if (this.hearing) {
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
   * Someone has been talking into the closed mic, with the AI quiet, while the AI has the floor. Not while the
   * presenter has it (the opening, a human slide, H): talking with the mic closed is then the point, and the hint
   * would pop up on the projector during the presenter's own lines.
   * @param {number} now
   */
  talkingIntoMutedMic(now) {
    const quiet = this.relayQuietSince;
    return (
      this.joinedAt !== null &&
      !this.open &&
      this.room === null &&
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
   * The AI's audible level (every animation frame while joined): the speaking border (with a short hold, so it
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
      li = this.line("tool", this.names.tile, key);
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
   * A final caption: what you said, the AI's answer, or one short line for a slide it presented live. In a meeting,
   * what the facilitator says in the discussion (and its interventions, hand-over, decisions and summary) is also a
   * line of the transcript.
   * @param {{role?: unknown, text?: unknown, final?: unknown}} msg
   * @param {string|null} kind  the reply's kind (assistant captions): narration, answer, intervention, …
   * @param {string} [who]  who a user line is from ("You"; a prerecorded participant: "Alice (recorded)")
   */
  caption(msg, kind, who = "You") {
    if (!msg.final) return;
    const text = typeof msg.text === "string" ? msg.text.trim() : "";
    if (msg.role === "user") {
      const echo = this.typedEcho;
      this.typedEcho = null;
      if (text && text !== echo) this.say("you", who, text);
    } else if (msg.role === "assistant") {
      if (kind === "narration") {
        this.note("presented", this.slideTitle ? `${this.names.aiCap} presented "${this.slideTitle}"` : `${this.names.aiCap} presented a slide`);
      } else if (text) {
        this.say("relay", this.names.tile, text);
        const discussion = this.agendaPhase === "discussion" || this.agendaPhase === "wrapup";
        if (this.facilitator && (discussion || FACILITATOR_KINDS.has(kind ?? ""))) this.side.facilitatorLine(text, kind ?? "answer", this.meetingTimeNow());
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

  /**
   * A typed question went out (its caption echo won't be shown twice). A line typed for the room in the discussion
   * is an utterance: the transcript shows it when the backend sends it back, not the chat.
   * @param {string} text
   */
  typed(text) {
    if (!(this.facilitator && this.agendaPhase === "discussion")) this.say("you", "You", text);
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
    if (this.facilitator) {
      this.side.bump("chat");
    } else if (this.chat.hidden) {
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
      time.textContent = this.timeNow();
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

  /** The toolbar's badge: unread chat lines (a meeting: everything unread on the panel's tabs, while it is closed). */
  renderUnread() {
    const n = this.facilitator ? (this.chat.hidden ? this.side.total() : 0) : this.unread;
    this.chatBadge.textContent = n > 9 ? "9+" : String(n);
    this.chatBadge.hidden = n === 0;
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

  /** The chat (a meeting: the side panel with its tabs). @param {boolean} [on] */
  toggleChat(on = this.chat.hidden) {
    this.chat.hidden = !on;
    setFlag(this.chatButton, "aria-pressed", on);
    if (on) {
      this.unread = 0;
      this.side.opened();
      this.chatLog.scrollTop = this.chatLog.scrollHeight;
    }
    this.renderUnread();
    this.layoutChanged();
  }

  /** @param {boolean} [on] */
  toggleCaptions(on = this.root.classList.contains("captions-off")) {
    this.root.classList.toggle("captions-off", !on);
    setFlag(this.ccButton, "aria-pressed", on);
    this.layoutChanged();
  }

  /** The meeting info panel (the status strip): ⓘ, the I key, or More. @param {boolean} [on] */
  toggleInfo(on = this.infoPanel.hidden) {
    this.infoPanel.hidden = !on;
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
    if (!open) this.disarmReset();
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
    const action = item.dataset.action ?? "";
    if (action === "reset") {
      this.onResetItem();
      return;
    }
    this.closeMenus();
    if (action === "checklist") this.handlers.onChecklist();
    else if (action === "info") this.toggleInfo();
    else if (MENU_ACTIONS.has(action)) this.handlers.onAction(/** @type {ControlAction} */ (action));
  }

  /** More → Restart meeting: the first click asks (the menu stays open), the second one restarts. */
  onResetItem() {
    if (this.resetItem.dataset.confirm !== "true") {
      this.resetItem.dataset.confirm = "true";
      this.resetLabel.textContent = "Click again to restart: the notes and transcript are cleared";
      clearTimeout(this.resetTimer);
      this.resetTimer = setTimeout(() => this.disarmReset(), RESET_CONFIRM_MS);
      return;
    }
    this.closeMenus();
    this.handlers.onReset?.();
  }

  disarmReset() {
    clearTimeout(this.resetTimer);
    this.resetItem.dataset.confirm = "false";
    this.resetLabel.textContent = "Restart meeting";
  }

  /** "You left the meeting" (after Leave), until Rejoin or any presenter key. @param {boolean} on */
  showLeft(on) {
    this.leftCard.hidden = !on;
  }
}
